// SPDX-License-Identifier: MIT
// plinius-tflite: see plinius-tflite.h.
//
// Delegates, in this order: the NNAPI delegate on the vendor's support library (if an accelerator
// is asked for), then XNNPACK for what is left, then TFLite's own kernels for the rest. The
// interpreter is built without TFLite's default delegates, so XNNPACK does not grab the graph
// before NNAPI sees it.
#include "plinius-tflite.h"

#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>

#include <dlfcn.h>
#include <unistd.h>

#include "tensorflow/lite/delegates/nnapi/nnapi_delegate.h"
#include "tensorflow/lite/delegates/xnnpack/xnnpack_delegate.h"
#include "tensorflow/lite/interpreter.h"
#include "tensorflow/lite/kernels/register.h"
#include "tensorflow/lite/model_builder.h"
#include "tensorflow/lite/nnapi/sl/public/NeuralNetworksSupportLibraryImpl.h"

#ifdef PLTFL_WITH_HYBRIS
#include <hybris/common/dlfcn.h>
#endif

#define PLTFL_EXPORT extern "C" __attribute__ ((visibility ("default")))

namespace {

// The support library of the Plinius vendor (Dimensity 7300, mt6878); PLTFL_SL_LIBRARY overrides
const char kSupportLibrary[] = "libneuralnetworks_sl_driver_mtk_prebuilt.so";
const char kSupportLibraryDir[] = "/vendor/lib64/mt6878/";

char *
dup_error (const std::string &message)
{
	return strdup (message.c_str ());
}

void
set_error (char **error, const std::string &message)
{
	if (error)
		*error = dup_error (message);
}

// Writes TFLite's messages to stderr like the default reporter, and keeps the last one for the
// error returned to the caller.
class Reporter : public tflite::ErrorReporter {
public:
	int Report (const char *format, va_list args) override
	{
		char buffer[1024];

		vsnprintf (buffer, sizeof buffer, format, args);
		fprintf (stderr, "tflite: %s\n", buffer);
		last = buffer;
		return 0;
	}
	std::string last;
};

// The support library, loaded once per process. It stays loaded until the process ends: its
// exit handlers crash, so it is never unloaded (see pltfl_exit()).
struct SupportLibrary {
	std::once_flag once;
	const NnApiSLDriverImplFL5 *driver = nullptr;
	std::string error;
} support_library;

void
load_support_library ()
{
#ifdef PLTFL_WITH_HYBRIS
	const char *name = getenv ("PLTFL_SL_LIBRARY");
	typedef NnApiSLDriverImpl *(*GetSLDriverImpl) ();
	GetSLDriverImpl get;
	NnApiSLDriverImpl *impl;
	void *lib;

	if (!name || !*name)
		name = kSupportLibrary;
	lib = hybris_dlopen (name, RTLD_NOW);
	if (!lib && !strchr (name, '/')) {
		// The library is in a subdirectory the linker namespace only searches for root
		std::string path = std::string (kSupportLibraryDir) + name;
		lib = hybris_dlopen (path.c_str (), RTLD_NOW);
	}
	if (!lib) {
		const char *message = hybris_dlerror ();
		support_library.error = std::string ("cannot load ") + name + ": " +
			(message ? message : "unknown error");
		return;
	}
	get = (GetSLDriverImpl) hybris_dlsym (lib, "ANeuralNetworks_getSLDriverImpl");
	if (!get) {
		support_library.error = std::string (name) + " has no ANeuralNetworks_getSLDriverImpl";
		return;
	}
	impl = get ();
	if (!impl || impl->implFeatureLevel < ANEURALNETWORKS_FEATURE_LEVEL_5) {
		support_library.error = "the support library is older than NNAPI feature level 5";
		return;
	}
	support_library.driver = reinterpret_cast<const NnApiSLDriverImplFL5 *> (impl);
#else
	support_library.error = "built without libhybris, no NPU";
#endif
}

const NnApiSLDriverImplFL5 *
get_support_library (std::string *error)
{
	std::call_once (support_library.once, load_support_library);
	if (!support_library.driver && error)
		*error = support_library.error;
	return support_library.driver;
}

// Whether the support library lists an NNAPI device of this name
bool
has_device (const NnApiSLDriverImplFL5 *sl, const char *accelerator, std::string *error)
{
	uint32_t count = 0;
	std::string names;

	if (sl->ANeuralNetworks_getDeviceCount (&count) != ANEURALNETWORKS_NO_ERROR) {
		*error = "the support library lists no devices";
		return false;
	}
	for (uint32_t i = 0; i < count; i++) {
		ANeuralNetworksDevice *device = nullptr;
		const char *name = nullptr;

		if (sl->ANeuralNetworks_getDevice (i, &device) != ANEURALNETWORKS_NO_ERROR ||
		    sl->ANeuralNetworksDevice_getName (device, &name) != ANEURALNETWORKS_NO_ERROR)
			continue;
		if (strcmp (name, accelerator) == 0)
			return true;
		names += names.empty () ? name : std::string (", ") + name;
	}
	*error = std::string ("no NNAPI device \"") + accelerator + "\" (there are: " + names + ")";
	return false;
}

// Cache token of a model: FNV-1a of its contents, so that a changed model file never loads the
// compilation of the old one
std::string
model_token (const tflite::FlatBufferModel &model, const PlTflOptions &options)
{
	const unsigned char *data = static_cast<const unsigned char *> (model.allocation ()->base ());
	size_t size = model.allocation ()->bytes ();
	uint64_t hash = 0xcbf29ce484222325ULL;
	char token[96];

	for (size_t i = 0; i < size; i++) {
		hash ^= data[i];
		hash *= 0x100000001b3ULL;
	}
	snprintf (token, sizeof token, "%016llx-%zu-%s-%d", (unsigned long long) hash, size,
		  options.accelerator, options.allow_fp16 ? 1 : 0);
	return token;
}

} // namespace

struct PlTfl {
	Reporter reporter;
	std::unique_ptr<tflite::FlatBufferModel> model;
	std::unique_ptr<tflite::Interpreter> interpreter;
	std::unique_ptr<tflite::StatefulNnApiDelegate> nnapi;
	TfLiteDelegate *xnnpack = nullptr;
	std::string token;
	int npu_nodes = 0;
	int total_nodes = 0;

	~PlTfl ()
	{
		// The interpreter goes first: it still refers to the delegates
		interpreter.reset ();
		nnapi.reset ();
		if (xnnpack)
			TfLiteXNNPackDelegateDelete (xnnpack);
	}
};

namespace {

bool
build_interpreter (PlTfl *self, const PlTflOptions &options)
{
	tflite::ops::builtin::BuiltinOpResolverWithoutDefaultDelegates resolver;

	self->interpreter.reset ();
	if (tflite::InterpreterBuilder (*self->model, resolver) (&self->interpreter) != kTfLiteOk ||
	    !self->interpreter)
		return false;
	if (options.threads > 0)
		self->interpreter->SetNumThreads (options.threads);
	self->total_nodes = (int) self->interpreter->nodes_size ();
	return true;
}

// Hands the operations the NPU supports to the NNAPI delegate. On failure the interpreter is gone
// and has to be built again.
bool
apply_nnapi (PlTfl *self, const PlTflOptions &options, const NnApiSLDriverImplFL5 *sl)
{
	tflite::StatefulNnApiDelegate::Options nnapi_options;
	TfLiteStatus status;
	int on_cpu = 0;

	nnapi_options.accelerator_name = options.accelerator;
	// The CPU path of NNAPI is slower than XNNPACK; what the NPU cannot do stays with us
	nnapi_options.disallow_nnapi_cpu = true;
	nnapi_options.allow_fp16 = options.allow_fp16 != 0;
	nnapi_options.execution_preference =
		tflite::StatefulNnApiDelegate::Options::kFastSingleAnswer;
	if (options.cache_dir && *options.cache_dir) {
		self->token = model_token (*self->model, options);
		nnapi_options.cache_dir = options.cache_dir;
		nnapi_options.model_token = self->token.c_str ();
	}
	self->nnapi = std::make_unique<tflite::StatefulNnApiDelegate> (sl, nnapi_options);
	status = self->interpreter->ModifyGraphWithDelegate (self->nnapi.get ());
	if (status != kTfLiteOk) {
		fprintf (stderr, "plinius-tflite: NPU not used (%s, NNAPI error %d)\n",
			 self->reporter.last.c_str (), self->nnapi->GetNnApiErrno ());
		// Only kTfLiteDelegateError promises the graph as it was; build it again in any case
		self->interpreter.reset ();
		return false;
	}
	for (int index : self->interpreter->execution_plan ())
		if (index < self->total_nodes)
			on_cpu++;
	self->npu_nodes = self->total_nodes - on_cpu;
	return true;
}

void
apply_xnnpack (PlTfl *self, const PlTflOptions &options)
{
	TfLiteXNNPackDelegateOptions xnnpack_options = TfLiteXNNPackDelegateOptionsDefault ();

	if (options.threads > 0)
		xnnpack_options.num_threads = options.threads;
	self->xnnpack = TfLiteXNNPackDelegateCreate (&xnnpack_options);
	if (!self->xnnpack)
		return;
	// On failure the graph stays as it was (TFLite's kernels run everything that is left)
	if (self->interpreter->ModifyGraphWithDelegate (self->xnnpack) != kTfLiteOk)
		fprintf (stderr, "plinius-tflite: XNNPACK not used (%s)\n", self->reporter.last.c_str ());
}

} // namespace

PLTFL_EXPORT void
pltfl_options_init (PlTflOptions *options)
{
	memset (options, 0, sizeof *options);
}

PLTFL_EXPORT int
pltfl_npu_available (const char *accelerator, char **error)
{
	std::string message;
	const NnApiSLDriverImplFL5 *sl = get_support_library (&message);

	if (sl && accelerator && has_device (sl, accelerator, &message))
		return 1;
	if (!accelerator)
		message = "no accelerator given";
	set_error (error, message);
	return 0;
}

PLTFL_EXPORT PlTfl *
pltfl_open (const char *model_path, const PlTflOptions *options_in, char **error)
{
	std::unique_ptr<PlTfl> self (new PlTfl);
	PlTflOptions options;

	if (options_in)
		options = *options_in;
	else
		pltfl_options_init (&options);

	self->model = tflite::FlatBufferModel::VerifyAndBuildFromFile (model_path, nullptr,
								      &self->reporter);
	if (!self->model) {
		set_error (error, std::string ("cannot load ") + model_path + ": " + self->reporter.last);
		return nullptr;
	}
	if (!build_interpreter (self.get (), options)) {
		set_error (error, std::string ("cannot build an interpreter for ") + model_path + ": " +
			   self->reporter.last);
		return nullptr;
	}

	if (options.accelerator && *options.accelerator) {
		std::string message;
		const NnApiSLDriverImplFL5 *sl = get_support_library (&message);

		if (!sl || !has_device (sl, options.accelerator, &message))
			fprintf (stderr, "plinius-tflite: NPU not used: %s\n", message.c_str ());
		else if (!apply_nnapi (self.get (), options, sl)) {
			self->nnapi.reset ();
			self->npu_nodes = 0;
			if (!build_interpreter (self.get (), options)) {
				set_error (error, std::string ("cannot build an interpreter for ") +
					   model_path + ": " + self->reporter.last);
				return nullptr;
			}
		}
	}
	apply_xnnpack (self.get (), options);

	if (self->interpreter->AllocateTensors () != kTfLiteOk) {
		set_error (error, std::string ("cannot allocate the tensors of ") + model_path + ": " +
			   self->reporter.last);
		return nullptr;
	}
	return self.release ();
}

PLTFL_EXPORT void
pltfl_close (PlTfl *self)
{
	delete self;
}

PLTFL_EXPORT int
pltfl_input_count (PlTfl *self)
{
	return (int) self->interpreter->inputs ().size ();
}

PLTFL_EXPORT int
pltfl_output_count (PlTfl *self)
{
	return (int) self->interpreter->outputs ().size ();
}

static TfLiteTensor *
describe (TfLiteTensor *tensor, PlTflTensorInfo *info)
{
	if (!tensor || !info)
		return tensor;
	memset (info, 0, sizeof *info);
	info->type = (PlTflType) tensor->type;
	info->ndims = tensor->dims ? tensor->dims->size : 0;
	if (info->ndims > PLTFL_MAX_DIMS)
		info->ndims = PLTFL_MAX_DIMS;
	for (int i = 0; i < info->ndims; i++)
		info->dims[i] = tensor->dims->data[i];
	info->bytes = tensor->bytes;
	info->scale = tensor->params.scale;
	info->zero_point = tensor->params.zero_point;
	info->name = tensor->name;
	return tensor;
}

PLTFL_EXPORT void *
pltfl_input (PlTfl *self, int index, PlTflTensorInfo *info)
{
	const std::vector<int> &inputs = self->interpreter->inputs ();
	TfLiteTensor *tensor;

	if (index < 0 || index >= (int) inputs.size ())
		return nullptr;
	tensor = describe (self->interpreter->tensor (inputs[index]), info);
	return tensor ? tensor->data.raw : nullptr;
}

PLTFL_EXPORT const void *
pltfl_output (PlTfl *self, int index, PlTflTensorInfo *info)
{
	const std::vector<int> &outputs = self->interpreter->outputs ();
	TfLiteTensor *tensor;

	if (index < 0 || index >= (int) outputs.size ())
		return nullptr;
	tensor = describe (self->interpreter->tensor (outputs[index]), info);
	return tensor ? tensor->data.raw : nullptr;
}

PLTFL_EXPORT int
pltfl_invoke (PlTfl *self)
{
	return self->interpreter->Invoke () == kTfLiteOk ? 0 : -1;
}

PLTFL_EXPORT int
pltfl_npu_nodes (PlTfl *self)
{
	return self->npu_nodes;
}

PLTFL_EXPORT int
pltfl_total_nodes (PlTfl *self)
{
	return self->total_nodes;
}

PLTFL_EXPORT void
pltfl_exit (int status)
{
	fflush (nullptr);
	_exit (status);
}
