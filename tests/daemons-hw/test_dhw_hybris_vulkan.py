# hybris-vulkan, the ICD core: the negotiation with the Khronos loader (only with a usable Android
# Vulkan HAL), which functions come from the HAL device, from the driver or from the ICD itself,
# the extension lists (VK_ANDROID_native_buffer hidden, the WSI extensions added), and how
# vkCreateInstance and vkCreateDevice keep the WSI extensions away from the driver and enable
# what the swapchain is built on in their place. The HAL and the driver are fakes in the
# harness, scripted per argument.

import shutil
import subprocess

import pytest

from conftest import compile_c
from dhw_harness import FAKES, PKG, kv, run
from hybris_vulkan_harness import CORE, WSI, build, generate_protocol, needs_vulkan

pytestmark = needs_vulkan

HARNESS = r"""
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static int calloc_fail_at, callocs, realloc_fail;
static void *test_calloc(size_t n, size_t size)
{
	return ++callocs == calloc_fail_at ? NULL : calloc(n, size);
}
static void *test_realloc(void *p, size_t size)
{
	return realloc_fail ? NULL : realloc(p, size);
}
#define calloc test_calloc
#define realloc test_realloc
#include "hybris_vulkan.c"
#undef calloc
#undef realloc
#include "wsi_wayland.c"

/* What the fake HAL gets wrong, from argv[1] */
static const char *hal_mode = "ok";
static int get_module_calls, opens;

static int has_opt(int argc, char **argv, const char *opt)
{
	for (int i = 2; i < argc; i++)
		if (!strcmp(argv[i], opt))
			return 1;
	return 0;
}

/* Extension lists of the fake driver, set per scenario */
static const char *instance_exts[24] = {"VK_KHR_get_physical_device_properties2", "VK_KHR_external_memory_capabilities",
				       "VK_KHR_external_semaphore_capabilities", "VK_KHR_external_fence_capabilities",
				       "VK_EXT_debug_utils"};
static uint32_t instance_ext_count = 5;
static const char *device_exts[24] = {"VK_KHR_a", "VK_ANDROID_native_buffer", "VK_KHR_external_memory_fd",
				      "VK_EXT_external_memory_dma_buf", "VK_EXT_image_drm_format_modifier",
				      "VK_KHR_external_memory", "VK_KHR_bind_memory2", "VK_KHR_external_semaphore_fd",
				      "VK_KHR_external_fence_fd"};
static uint32_t device_ext_count = 9;
static int fail_call, enum_calls, grow, fail_instance_enum;

static VkResult fill(const char **names, uint32_t total, uint32_t *count, VkExtensionProperties *props)
{
	if (!props) {
		*count = total;
		return VK_SUCCESS;
	}
	uint32_t n = *count < total ? *count : total;
	for (uint32_t i = 0; i < n; i++) {
		memset(&props[i], 0, sizeof props[i]);
		strcpy(props[i].extensionName, names[i]);
		props[i].specVersion = i + 1;
	}
	*count = n;
	return grow ? VK_INCOMPLETE : VK_SUCCESS;
}

static VkResult VKAPI_CALL drv_enumerate_device_exts(VkPhysicalDevice pd, const char *layer, uint32_t *count,
						     VkExtensionProperties *props)
{
	enum_calls++;
	if (fail_call == enum_calls)
		return VK_ERROR_OUT_OF_DEVICE_MEMORY;
	if (layer) {
		*count = 77;
		return VK_ERROR_LAYER_NOT_PRESENT;
	}
	return fill(device_exts, device_ext_count, count, props);
}
static VkResult VKAPI_CALL hal_enumerate_instance_exts(const char *layer, uint32_t *count, VkExtensionProperties *p)
{
	if (layer) {
		*count = 55;
		return VK_ERROR_LAYER_NOT_PRESENT;
	}
	if (fail_instance_enum)
		return VK_ERROR_OUT_OF_HOST_MEMORY;
	return fill(instance_exts, instance_ext_count, count, p);
}

static void print_names(const char *what, uint32_t n, const char *const *names)
{
	printf("%s", what);
	for (uint32_t i = 0; i < n; i++)
		printf("%s%s", i ? "," : "=", names[i]);
	printf("%s\n", n ? "" : "=");
}

static VkResult instance_result = VK_SUCCESS;
static VkResult VKAPI_CALL hal_create_instance(const VkInstanceCreateInfo *ci, const VkAllocationCallbacks *a, VkInstance *i)
{
	print_names("driver-instance-exts", ci->enabledExtensionCount, ci->ppEnabledExtensionNames);
	*i = (VkInstance)(uintptr_t)0x1000;
	return instance_result;
}

/* Fake device-level driver functions */
static int destroyed_devices, queue_calls;
static void VKAPI_CALL drv_destroy_device(VkDevice d, const VkAllocationCallbacks *a)
{
	destroyed_devices++;
	printf("driver-destroy-device %p\n", (void *)d);
}
static void VKAPI_CALL drv_get_device_queue(VkDevice d, uint32_t family, uint32_t index, VkQueue *q)
{
	queue_calls++;
	printf("driver-get-queue family=%u index=%u\n", family, index);
	*q = (VkQueue)(uintptr_t)0x3000;
}
static void VKAPI_CALL drv_create_image(void) {}
static int no_destroy_fn, no_queue_fn;
static PFN_vkVoidFunction VKAPI_CALL drv_gdpa(VkDevice device, const char *name)
{
	if (!strcmp(name, "vkDestroyDevice"))
		return no_destroy_fn ? NULL : (PFN_vkVoidFunction)drv_destroy_device;
	if (!strcmp(name, "vkGetDeviceQueue"))
		return no_queue_fn ? NULL : (PFN_vkVoidFunction)drv_get_device_queue;
	if (!strcmp(name, "vkCreateImage"))
		return (PFN_vkVoidFunction)drv_create_image;
	return NULL;
}
static VkResult device_result = VK_SUCCESS;
static VkResult VKAPI_CALL drv_create_device(VkPhysicalDevice pd, const VkDeviceCreateInfo *ci,
					     const VkAllocationCallbacks *a, VkDevice *d)
{
	print_names("driver-device-exts", ci->enabledExtensionCount, ci->ppEnabledExtensionNames);
	*d = (VkDevice)(uintptr_t)0x2000;
	return device_result;
}
static void VKAPI_CALL drv_enumerate_instance_version(void) {}
static void VKAPI_CALL drv_queue_submit(void) {}
static int null_enum, null_create_device;
static PFN_vkVoidFunction VKAPI_CALL drv_gipa(VkInstance instance, const char *name)
{
	printf("driver-gipa %s %s\n", instance ? "instance" : "null", name);
	if (!strcmp(name, "vkEnumerateDeviceExtensionProperties"))
		return null_enum ? NULL : (PFN_vkVoidFunction)drv_enumerate_device_exts;
	if (!strcmp(name, "vkCreateDevice"))
		return null_create_device ? NULL : (PFN_vkVoidFunction)drv_create_device;
	if (!strcmp(name, "vkGetDeviceProcAddr"))
		return (PFN_vkVoidFunction)drv_gdpa;
	if (!strcmp(name, "vkQueueSubmit"))
		return (PFN_vkVoidFunction)drv_queue_submit;
	if (!instance && !strcmp(name, "vkEnumerateInstanceVersion"))
		return (PFN_vkVoidFunction)drv_enumerate_instance_version;
	return NULL;
}

static hwvulkan_device_t fake_device;
static int dev_open(const struct hw_module_t *module, const char *id, struct hw_device_t **device)
{
	opens++;
	printf("open %s\n", id);
	if (!strcmp(hal_mode, "open-fails"))
		return -EINVAL;
	if (!strcmp(hal_mode, "open-null")) {
		*device = NULL;
		return 0;
	}
	fake_device.common.version = strcmp(hal_mode, "device-version") ? HWVULKAN_DEVICE_API_VERSION_0_1 : 0x00020000;
	fake_device.EnumerateInstanceExtensionProperties = hal_enumerate_instance_exts;
	fake_device.CreateInstance = strcmp(hal_mode, "no-create") ? hal_create_instance : NULL;
	fake_device.GetInstanceProcAddr = drv_gipa;
	*device = &fake_device.common;
	return 0;
}
static struct hw_module_methods_t methods = {.open = dev_open};
static struct hw_module_methods_t no_open_methods = {.open = NULL};
static struct hw_module_t module = {.id = "vulkan", .name = "fake Mali", .methods = &methods};

int hw_get_module(const char *id, const struct hw_module_t **out)
{
	get_module_calls++;
	printf("hw_get_module %s\n", id);
	if (!strcmp(hal_mode, "none"))
		return -ENOENT;
	if (!strcmp(hal_mode, "null-module")) {
		*out = NULL;
		return 0;
	}
	module.module_api_version = strcmp(hal_mode, "module-version") ? HWVULKAN_MODULE_API_VERSION_0_1 : 0x0002;
	module.methods = !strcmp(hal_mode, "no-methods") ? NULL : !strcmp(hal_mode, "no-open") ? &no_open_methods : &methods;
	*out = &module;
	return 0;
}

#define IS(p, f) ((p) == (PFN_vkVoidFunction)(f))
static const char *which(PFN_vkVoidFunction p)
{
	if (!p) return "null";
	if (IS(p, create_instance)) return "icd-create-instance";
	if (IS(p, enumerate_instance_extensions)) return "icd-enumerate-instance-exts";
	if (IS(p, drv_create_device)) return "driver-create-device";
	if (IS(p, drv_queue_submit)) return "driver-queue-submit";
	if (IS(p, drv_create_image)) return "driver-create-image";
	if (IS(p, drv_enumerate_instance_version)) return "driver-enumerate-instance-version";
	if (IS(p, vk_icdGetInstanceProcAddr)) return "icd-gipa";
	if (IS(p, drv_enumerate_device_exts)) return "driver-enumerate-device-exts";
	if (IS(p, enumerate_device_extensions)) return "icd-enumerate-device-exts";
	if (IS(p, create_device)) return "icd-create-device";
	if (IS(p, destroy_device)) return "icd-destroy-device";
	if (IS(p, get_device_proc_addr)) return "icd-gdpa";
	if (IS(p, create_swapchain)) return "wsi-create-swapchain";
	if (IS(p, queue_present)) return "wsi-queue-present";
	if (IS(p, surface_support)) return "wsi-surface-support";
	if (IS(p, wayland_presentation_support)) return "wsi-wayland-presentation-support";
	return "other";
}

static void list(const char *what, VkResult (*fn)(void *, uint32_t *, VkExtensionProperties *), void *ctx, uint32_t room)
{
	VkExtensionProperties props[32];
	uint32_t n = room;
	memset(props, 0, sizeof props);
	VkResult r = fn(ctx, &n, props);
	printf("%s result=%d count=%u names=", what, r, n);
	for (uint32_t i = 0; i < n; i++)
		printf("%s%s", i ? "," : "", props[i].extensionName);
	printf("\n");
}
static VkResult list_instance(void *ctx, uint32_t *n, VkExtensionProperties *p)
{
	return enumerate_instance_extensions(NULL, n, p);
}
static VkResult list_device(void *ctx, uint32_t *n, VkExtensionProperties *p)
{
	return enumerate_device_extensions((VkPhysicalDevice)ctx, NULL, n, p);
}

/* Comma-separated names from an argument (at most 24), each in its own string */
static uint32_t split(const char *s, const char **out)
{
	uint32_t n = 0;
	char *text = strdup(s);
	for (char *t = strtok(text, ","); t && n < 24; t = strtok(NULL, ","))
		out[n++] = strdup(t);
	free(text);
	return n;
}

int main(int argc, char **argv)
{
	VkInstance inst = (VkInstance)(uintptr_t)0x1000;
	VkPhysicalDevice pd = (VkPhysicalDevice)(uintptr_t)0x4000;
	VkDevice device = VK_NULL_HANDLE;
	hal_mode = argv[1];
	for (int i = 2; i < argc; i++) {
		const char *a = argv[i];
		if (!strncmp(a, "negotiate:", 10)) {
			uint32_t v = strtoul(a + 10, NULL, 0);
			VkResult r = vk_icdNegotiateLoaderICDInterfaceVersion(&v);
			printf("negotiate result=%d version=%u\n", r, v);
		} else if (!strncmp(a, "gipa-null:", 10)) {
			printf("gipa-null %s=%s\n", a + 10, which(vk_icdGetInstanceProcAddr(NULL, a + 10)));
		} else if (!strncmp(a, "gipa:", 5)) {
			printf("gipa %s=%s\n", a + 5, which(vk_icdGetInstanceProcAddr(inst, a + 5)));
		} else if (!strcmp(a, "gipa-noname")) {
			printf("gipa-noname=%s\n", which(vk_icdGetInstanceProcAddr(inst, NULL)));
		} else if (!strncmp(a, "gdpa:", 5)) {
			printf("gdpa %s=%s\n", a + 5, which(get_device_proc_addr(device, a + 5)));
		} else if (!strcmp(a, "gdpa-noname")) {
			printf("gdpa-noname=%s\n", which(get_device_proc_addr(device, NULL)));
		} else if (!strncmp(a, "instance-exts:", 14)) {
			instance_ext_count = split(a + 14, instance_exts);
		} else if (!strncmp(a, "device-exts:", 12)) {
			/* "-" alone: no extensions */
			device_ext_count = strcmp(a + 12, "-") ? split(a + 12, device_exts) : 0;
		} else if (!strncmp(a, "create-instance:", 16)) {
			const char *names[24];
			uint32_t n = strcmp(a + 16, "-") ? split(a + 16, names) : 0;
			VkInstanceCreateInfo ci = {.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,
						   .enabledExtensionCount = n, .ppEnabledExtensionNames = names};
			VkInstance out = VK_NULL_HANDLE;
			VkResult r = vk_icdGetInstanceProcAddr(NULL, "vkCreateInstance") ?
				((PFN_vkCreateInstance)vk_icdGetInstanceProcAddr(NULL, "vkCreateInstance"))(&ci, NULL, &out) : -100;
			printf("create-instance result=%d loaded=%d\n", r, hv_drv.CreateDevice != NULL);
		} else if (!strncmp(a, "create-device:", 14)) {
			const char *names[24];
			uint32_t n = strcmp(a + 14, "-") ? split(a + 14, names) : 0;
			VkDeviceQueueCreateInfo q = {.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, .queueFamilyIndex = 0, .queueCount = 1};
			if (has_opt(argc, argv, "protected-queue"))
				q.flags = VK_DEVICE_QUEUE_CREATE_PROTECTED_BIT;
			VkDeviceCreateInfo ci = {.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
						 .queueCreateInfoCount = has_opt(argc, argv, "no-queues") ? 0 : 1,
						 .pQueueCreateInfos = &q,
						 .enabledExtensionCount = n, .ppEnabledExtensionNames = names};
			VkResult r = create_device(pd, &ci, NULL, &device);
			struct hv_device *dev = r == VK_SUCCESS ? hv_device_find(device) : NULL;
			printf("create-device result=%d found=%d swapchain=%d sync_fd=%d queue=%d\n", r, dev != NULL,
			       dev ? dev->swapchain : -1, dev ? dev->sync_fd_import : -1, dev ? dev->queue != NULL : -1);
		} else if (!strcmp(a, "destroy-device")) {
			destroy_device(device, NULL);
			printf("after-destroy found=%d\n", hv_device_find(device) != NULL);
		} else if (!strcmp(a, "destroy-null-device")) {
			destroy_device(VK_NULL_HANDLE, NULL);
		} else if (!strcmp(a, "destroy-unknown-device")) {
			destroy_device((VkDevice)(uintptr_t)0x9999, NULL);
		} else if (!strcmp(a, "instance-count")) {
			uint32_t n = 99;
			VkResult r = enumerate_instance_extensions(NULL, &n, NULL);
			printf("instance-count result=%d count=%u\n", r, n);
		} else if (!strncmp(a, "instance-list:", 14)) {
			list("instance-list", list_instance, NULL, strtoul(a + 14, NULL, 0));
		} else if (!strcmp(a, "instance-layer")) {
			uint32_t n = 0;
			VkResult r = enumerate_instance_extensions("VK_LAYER_x", &n, NULL);
			printf("instance-layer result=%d count=%u\n", r, n);
		} else if (!strcmp(a, "count")) {
			uint32_t n = 99;
			VkResult r = enumerate_device_extensions(pd, NULL, &n, NULL);
			printf("count result=%d count=%u\n", r, n);
		} else if (!strncmp(a, "list:", 5)) {
			list("list", list_device, pd, strtoul(a + 5, NULL, 0));
		} else if (!strcmp(a, "layer")) {
			uint32_t n = 0;
			VkResult r = enumerate_device_extensions(pd, "VK_LAYER_x", &n, NULL);
			printf("layer result=%d count=%u\n", r, n);
		} else if (!strncmp(a, "fail-call:", 10)) {
			fail_call = enum_calls + atoi(a + 10);
		} else if (!strncmp(a, "calloc-fail:", 12)) {
			calloc_fail_at = callocs + atoi(a + 12);
		} else if (!strcmp(a, "grow")) {
			grow = 1;
		} else if (!strcmp(a, "realloc-fail")) {
			realloc_fail = 1;
		} else if (!strcmp(a, "null-enum")) {
			null_enum = 1;
		} else if (!strcmp(a, "null-create-device")) {
			null_create_device = 1;
		} else if (!strcmp(a, "no-destroy-fn")) {
			no_destroy_fn = 1;
		} else if (!strcmp(a, "no-queue-fn")) {
			no_queue_fn = 1;
		} else if (!strcmp(a, "fail-instance-enum")) {
			fail_instance_enum = 1;
		} else if (!strncmp(a, "instance-result:", 16)) {
			instance_result = atoi(a + 16);
		} else if (!strncmp(a, "device-result:", 14)) {
			device_result = atoi(a + 14);
		}
	}
	printf("calls get_module=%d open=%d destroyed=%d queue=%d\n", get_module_calls, opens, destroyed_devices, queue_calls);
	return 0;
}
"""

REQUIRED = "VK_KHR_external_memory_fd,VK_EXT_external_memory_dma_buf,VK_EXT_image_drm_format_modifier"


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    d = tmp_path_factory.mktemp("hybris_vulkan")
    return build(d, "hybris_vulkan", HARNESS,
                 replacements_wsi=[("/dev/dma_heap/mtk_mm-uncached", d / "heap_mtk_mm_uncached"),
                                   ("/dev/dma_heap/mtk_mm\"", f"{d / 'heap_mtk_mm'}\""),
                                   ("/dev/dma_heap/system", d / "heap_system")])


def call(harness, mode, *args):
    return run(harness, mode, *args).stdout


def with_instance(harness, *args, mode="ok"):
    """Negotiated and with an instance, as the loader leaves it before device calls"""
    return call(harness, mode, "negotiate:7", "create-instance:-", *args)


# Loader negotiation: the ICD exists only with a usable HAL

@pytest.mark.parametrize("mode", ["none", "null-module", "module-version", "no-methods", "no-open",
                                  "open-fails", "open-null", "device-version", "no-create"])
def test_unusable_hal_makes_the_loader_skip_the_icd(harness, mode):
    out = call(harness, mode, "negotiate:7", "gipa-null:vkCreateInstance", "negotiate:7")
    assert "negotiate result=-9 version=7" in out  # VK_ERROR_INCOMPATIBLE_DRIVER, untouched
    assert "gipa-null vkCreateInstance=null" in out
    # The HAL is looked for once per process, not on every call of the loader
    assert "calls get_module=1" in out
    opens_expected = 1 if mode in ("open-fails", "open-null", "device-version", "no-create") else 0
    assert f"open={opens_expected}" in out


def test_negotiation_settles_on_interface_5(harness):
    out = call(harness, "ok", "negotiate:7", "negotiate:5")
    assert out.count("negotiate result=0 version=5") == 2
    assert "hw_get_module vulkan" in out and "open vk0" in out
    assert "calls get_module=1 open=1" in out


def test_loader_older_than_interface_5_is_refused(harness):
    assert "negotiate result=-9 version=4" in call(harness, "ok", "negotiate:4")


# Where each function comes from

def test_global_functions(harness):
    k = kv(call(harness, "ok", "gipa-null:vkCreateInstance", "gipa-null:vkEnumerateInstanceExtensionProperties",
                "gipa-null:vkEnumerateInstanceVersion", "gipa-null:vkGetInstanceProcAddr",
                "gipa-null:vkEnumerateDeviceExtensionProperties"))
    # The ICD's own: it filters the WSI extensions out of the driver's way
    assert k["gipa-null vkCreateInstance"] == "icd-create-instance"
    assert k["gipa-null vkEnumerateInstanceExtensionProperties"] == "icd-enumerate-instance-exts"
    assert k["gipa-null vkEnumerateInstanceVersion"] == "driver-enumerate-instance-version"
    assert k["gipa-null vkGetInstanceProcAddr"] == "icd-gipa"
    # A physical-device function without an instance: whatever the driver answers
    assert k["gipa-null vkEnumerateDeviceExtensionProperties"] == "driver-enumerate-device-exts"


def test_instance_functions(harness):
    out = call(harness, "ok", "gipa:vkQueueSubmit", "gipa:vkCreateInstance", "gipa:vkDoesNotExist",
               "gipa-noname", "gipa:vkCreateDevice", "gipa:vkEnumerateDeviceExtensionProperties",
               "gipa:vkGetDeviceProcAddr", "gipa:vkDestroyDevice", "gipa:vkCreateSwapchainKHR",
               "gipa:vkQueuePresentKHR", "gipa:vkGetPhysicalDeviceSurfaceSupportKHR",
               "gipa:vkGetPhysicalDeviceWaylandPresentationSupportKHR")
    k = kv(out)
    assert k["gipa vkQueueSubmit"] == "driver-queue-submit"
    # With an instance even vkCreateInstance is the driver's answer (none here)
    assert k["gipa vkCreateInstance"] == "null"
    assert k["gipa vkDoesNotExist"] == "null"
    assert k["gipa-noname"] == "null"
    assert k["gipa vkCreateDevice"] == "icd-create-device"
    assert k["gipa vkEnumerateDeviceExtensionProperties"] == "icd-enumerate-device-exts"
    assert k["gipa vkGetDeviceProcAddr"] == "icd-gdpa"
    assert k["gipa vkDestroyDevice"] == "icd-destroy-device"
    # Device functions of the WSI through vkGetInstanceProcAddr: the loader asks without a device
    assert k["gipa vkCreateSwapchainKHR"] == "wsi-create-swapchain"
    assert k["gipa vkQueuePresentKHR"] == "wsi-queue-present"
    assert k["gipa vkGetPhysicalDeviceSurfaceSupportKHR"] == "wsi-surface-support"
    assert k["gipa vkGetPhysicalDeviceWaylandPresentationSupportKHR"] == "wsi-wayland-presentation-support"


def test_device_functions_with_and_without_swapchain(harness):
    out = with_instance(harness, "create-device:VK_KHR_a", "gdpa:vkCreateSwapchainKHR", "gdpa:vkCreateImage",
                        "gdpa:vkGetDeviceProcAddr", "gdpa:vkDestroyDevice", "gdpa-noname", "destroy-device",
                        f"device-exts:{REQUIRED}", "create-device:VK_KHR_swapchain", "gdpa:vkCreateSwapchainKHR",
                        "gdpa:vkQueuePresentKHR")
    lines = [l for l in out.splitlines() if l.startswith("gdpa")]
    # Without VK_KHR_swapchain its functions do not exist; the rest is the driver's
    assert lines[0] == "gdpa vkCreateSwapchainKHR=null"
    assert lines[1] == "gdpa vkCreateImage=driver-create-image"
    assert lines[2] == "gdpa vkGetDeviceProcAddr=icd-gdpa"
    assert lines[3] == "gdpa vkDestroyDevice=icd-destroy-device"
    assert lines[4] == "gdpa-noname=null"
    assert lines[5] == "gdpa vkCreateSwapchainKHR=wsi-create-swapchain"
    assert lines[6] == "gdpa vkQueuePresentKHR=wsi-queue-present"


# Instance extensions and vkCreateInstance

def test_instance_extensions_get_the_wsi_ones(harness):
    out = call(harness, "ok", "negotiate:7", "instance-count", "instance-list:16", "instance-list:2",
               "instance-layer")
    assert "instance-count result=0 count=7" in out
    assert ("instance-list result=0 count=7 names=VK_KHR_get_physical_device_properties2,"
            "VK_KHR_external_memory_capabilities,VK_KHR_external_semaphore_capabilities,"
            "VK_KHR_external_fence_capabilities,VK_EXT_debug_utils,VK_KHR_surface,VK_KHR_wayland_surface") in out
    assert "instance-list result=5 count=2 " in out  # VK_INCOMPLETE
    assert "instance-layer result=-6 count=55" in out


def test_driver_listing_a_wsi_extension_itself_lists_it_once(harness):
    out = call(harness, "ok", "negotiate:7", "instance-exts:VK_KHR_surface,VK_EXT_x", "instance-list:8")
    assert "instance-list result=0 count=3 names=VK_KHR_surface,VK_EXT_x,VK_KHR_wayland_surface" in out


def test_instance_without_wsi_goes_to_the_driver_unchanged(harness):
    out = call(harness, "ok", "negotiate:7", "create-instance:VK_EXT_debug_utils")
    assert "driver-instance-exts=VK_EXT_debug_utils" in out
    assert "create-instance result=0 loaded=1" in out
    # The driver functions the ICD needs are taken from the new instance
    assert "driver-gipa instance vkCreateDevice" in out
    assert "driver-gipa instance vkGetDeviceProcAddr" in out


def test_instance_with_wsi_gets_its_dependencies_instead(harness):
    out = call(harness, "ok", "negotiate:7",
               "create-instance:VK_KHR_surface,VK_KHR_wayland_surface,VK_KHR_get_physical_device_properties2")
    assert ("driver-instance-exts=VK_KHR_get_physical_device_properties2,VK_KHR_external_memory_capabilities,"
            "VK_KHR_external_semaphore_capabilities,VK_KHR_external_fence_capabilities") in out
    assert "create-instance result=0" in out


def test_wsi_dependencies_only_where_the_driver_has_them(harness):
    out = call(harness, "ok", "negotiate:7", "instance-exts:VK_KHR_external_memory_capabilities",
               "create-instance:VK_KHR_surface")
    assert "driver-instance-exts=VK_KHR_external_memory_capabilities\n" in out


def test_instance_creation_errors(harness):
    out = call(harness, "ok", "negotiate:7", "fail-instance-enum", "create-instance:VK_KHR_surface")
    assert "create-instance result=-1 loaded=0" in out
    out = call(harness, "ok", "negotiate:7", "calloc-fail:1", "create-instance:-")
    assert "create-instance result=-1 loaded=0" in out
    # A driver error comes through, nothing is taken from an instance that is not there
    out = call(harness, "ok", "negotiate:7", "instance-result:-3", "create-instance:-")
    assert "create-instance result=-3 loaded=0" in out


# Device extensions

def test_native_buffer_hidden_and_swapchain_added(harness):
    out = with_instance(harness, "count", "list:16")
    assert "count result=0 count=9" in out
    assert ("list result=0 count=9 names=VK_KHR_a,VK_KHR_external_memory_fd,VK_EXT_external_memory_dma_buf,"
            "VK_EXT_image_drm_format_modifier,VK_KHR_external_memory,VK_KHR_bind_memory2,"
            "VK_KHR_external_semaphore_fd,VK_KHR_external_fence_fd,VK_KHR_swapchain") in out


@pytest.mark.parametrize("missing", REQUIRED.split(","))
def test_no_swapchain_without_dma_buf_import(harness, missing):
    exts = ",".join(e for e in REQUIRED.split(",") if e != missing)
    out = with_instance(harness, f"device-exts:VK_KHR_a,{exts}", "list:16")
    assert "VK_KHR_swapchain" not in out.split("list result")[1]


def test_too_little_room_is_incomplete(harness):
    out = with_instance(harness, "device-exts:VK_KHR_a,VK_ANDROID_native_buffer,VK_KHR_b", "list:1", "list:0")
    assert "list result=5 count=1 names=VK_KHR_a" in out  # VK_INCOMPLETE
    assert "list result=5 count=0 names=" in out


def test_exact_room_is_success(harness):
    out = with_instance(harness, "device-exts:VK_KHR_a,VK_ANDROID_native_buffer,VK_KHR_b", "list:2")
    assert "list result=0 count=2 names=VK_KHR_a,VK_KHR_b" in out


def test_driver_without_extensions(harness):
    out = with_instance(harness, "device-exts:-", "count", "list:4")
    assert "count result=0 count=0" in out
    assert "list result=0 count=0 names=" in out


def test_layer_query_is_the_drivers_answer(harness):
    assert "layer result=-6 count=77" in with_instance(harness, "layer")  # VK_ERROR_LAYER_NOT_PRESENT


@pytest.mark.parametrize("which_call", [1, 2, 3])
def test_driver_errors_pass_through(harness, which_call):
    # 1: the check for the swapchain's needs, 2 and 3: the list itself
    out = with_instance(harness, f"fail-call:{which_call}", "list:8")
    assert "list result=-2 count=8 names=" in out  # VK_ERROR_OUT_OF_DEVICE_MEMORY, count untouched


def test_list_that_grew_in_between_counts_what_was_filled(harness):
    out = with_instance(harness, "device-exts:VK_KHR_a,VK_KHR_b", "grow", "list:8")
    assert "list result=0 count=2 names=VK_KHR_a,VK_KHR_b" in out


def test_out_of_memory(harness):
    assert "count result=-1 count=99" in with_instance(harness, "calloc-fail:1", "count")
    assert "count result=-1 count=99" in with_instance(harness, "realloc-fail", "count")


def test_without_instance_there_is_no_device_list(harness):
    out = call(harness, "ok", "negotiate:7", "count")
    assert "count result=-3 count=99" in out  # VK_ERROR_INITIALIZATION_FAILED


def test_driver_without_device_extension_function(harness):
    out = call(harness, "ok", "negotiate:7", "null-enum", "create-instance:-", "count")
    assert "count result=-3 count=99" in out


# vkCreateDevice and vkDestroyDevice

def test_device_without_swapchain_goes_to_the_driver_unchanged(harness):
    out = with_instance(harness, "create-device:VK_KHR_a", "destroy-device")
    assert "driver-device-exts=VK_KHR_a\n" in out
    assert "create-device result=0 found=1 swapchain=0 sync_fd=0 queue=1" in out
    assert "driver-get-queue family=0 index=0" in out
    assert "driver-destroy-device 0x2000" in out
    assert "after-destroy found=0" in out


def test_swapchain_brings_dma_buf_import_and_sync_files(harness):
    out = with_instance(harness, "create-device:VK_KHR_swapchain,VK_KHR_external_memory_fd")
    assert ("driver-device-exts=VK_KHR_external_memory_fd,VK_EXT_external_memory_dma_buf,"
            "VK_EXT_image_drm_format_modifier,VK_KHR_external_memory,VK_KHR_bind_memory2,"
            "VK_KHR_external_semaphore_fd,VK_KHR_external_fence_fd\n") in out
    assert "create-device result=0 found=1 swapchain=1 sync_fd=1 queue=1" in out


def test_sync_files_need_both_extensions(harness):
    out = with_instance(harness, f"device-exts:{REQUIRED},VK_KHR_external_semaphore_fd",
                        "create-device:VK_KHR_swapchain")
    assert "swapchain=1 sync_fd=0" in out


def test_swapchain_without_dma_buf_import_is_refused(harness):
    out = with_instance(harness, "device-exts:VK_KHR_external_memory_fd", "create-device:VK_KHR_swapchain")
    assert "create-device result=-7 found=0" in out  # VK_ERROR_EXTENSION_NOT_PRESENT
    assert "driver-device-exts" not in out


def test_device_list_error_refuses_the_swapchain_device(harness):
    out = with_instance(harness, "fail-call:1", "create-device:VK_KHR_swapchain")
    assert "create-device result=-2 found=0" in out


def test_driver_refusing_the_device(harness):
    out = with_instance(harness, "device-result:-7", "create-device:VK_KHR_a")
    assert "create-device result=-7 found=0" in out


@pytest.mark.parametrize("which_alloc", [1, 2])
def test_device_out_of_memory(harness, which_alloc):
    out = with_instance(harness, f"calloc-fail:{which_alloc}", "create-device:VK_KHR_a")
    assert "create-device result=-1 found=0" in out


def test_device_before_any_instance(harness):
    out = call(harness, "ok", "negotiate:7", "create-device:VK_KHR_a")
    assert "create-device result=-3 found=0" in out


def test_driver_without_create_device(harness):
    out = call(harness, "ok", "negotiate:7", "null-create-device", "create-instance:-", "create-device:VK_KHR_a")
    assert "create-device result=-3 found=0" in out


@pytest.mark.parametrize("opt", ["no-queues", "protected-queue", "no-queue-fn"])
def test_no_fallback_queue(harness, opt):
    out = with_instance(harness, opt, "create-device:VK_KHR_a")
    assert "create-device result=0 found=1 swapchain=0 sync_fd=0 queue=0" in out
    assert "driver-get-queue" not in out


def test_destroying_null_and_unknown_devices(harness):
    out = with_instance(harness, "destroy-null-device", "destroy-unknown-device")
    # The unknown one still reaches the driver, which knows it
    assert "driver-destroy-device 0x9999" in out
    out = with_instance(harness, "no-destroy-fn", "destroy-unknown-device")
    assert "driver-destroy-device" not in out


# The library as the PKGBUILD links it

def test_library_exports_only_the_icd_entry_points(tmp_path):
    if shutil.which("nm") is None:
        pytest.skip("no nm")
    protocol = generate_protocol(tmp_path)
    shutil.copy(PKG / "hybris-vulkan" / "hybris_vulkan.h", tmp_path)
    lib = compile_c(tmp_path / "libvulkan_hybris.so", [CORE, WSI, protocol],
                    flags=[f"-I{FAKES}", f"-I{tmp_path}", "-fPIC", "-shared", "-fvisibility=hidden", "-pthread",
                           "-Wl,-z,nodelete"],
                    pkgs=("wayland-client",), counted=False)
    out = subprocess.run(["nm", "-D", "--defined-only", str(lib)], capture_output=True, text=True,
                         check=True).stdout
    exported = sorted(line.split()[-1] for line in out.splitlines() if " T " in line or " D " in line or " R " in line)
    assert exported == ["vk_icdGetInstanceProcAddr", "vk_icdNegotiateLoaderICDInterfaceVersion"]
    undefined = subprocess.run(["nm", "-D", "--undefined-only", str(lib)], capture_output=True,
                               text=True, check=True).stdout
    assert "hw_get_module" in undefined
    if shutil.which("readelf"):
        dyn = subprocess.run(["readelf", "-d", str(lib)], capture_output=True, text=True, check=True).stdout
        assert "NODELETE" in dyn


def test_package_builds_what_it_installs():
    import json
    import re
    manifest = json.loads((PKG / "hybris-vulkan" / "hybris_icd.json").read_text())
    pkgbuild = (PKG / "hybris-vulkan" / "PKGBUILD").read_text()
    assert manifest["ICD"]["library_path"] == "/usr/lib/libvulkan_hybris.so"
    assert re.search(r'install -Dm755 libvulkan_hybris\.so -t "\$pkgdir/usr/lib"', pkgbuild)
    assert "-Wl,-soname,libvulkan_hybris.so" in pkgbuild
    # The loader's dlclose would take libhybris away under the driver's running threads
    assert "-Wl,-z,nodelete" in pkgbuild
    for src in ("hybris_vulkan.c", "wsi_wayland.c", "linux-dmabuf-v1-protocol.c"):
        assert src in pkgbuild.split("build()")[1]
    assert "linux-dmabuf/linux-dmabuf-v1.xml" in pkgbuild
    assert manifest["file_format_version"] == "1.0.1"
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["ICD"]["api_version"])
