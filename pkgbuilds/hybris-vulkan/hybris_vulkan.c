/*
 * hybris-vulkan: a Vulkan ICD for the Khronos loader (libvulkan.so.1) that hands every call to
 * the device's Android Vulkan driver, the "vulkan" HAL module (vulkan.mali.so on the Plinius),
 * loaded through libhybris.
 *
 * Why it can be this thin: an Android Vulkan HAL driver already follows the ICD contract for
 * dispatchable objects. Its VkInstance, VkPhysicalDevice, VkDevice, VkQueue and VkCommandBuffer
 * begin with a pointer-sized slot that the driver fills with ICD_LOADER_MAGIC
 * (hwvulkan_dispatch_t) and never reads again; Android's loader puts its dispatch table there,
 * and so does the Khronos loader. Measured on the Mali-G615 MC2 (driver r44p1, Vulkan 1.3.247):
 * all five objects carry 0x01CDC0DE, and buffer fills and compute dispatches give the right
 * results after the slot was overwritten. So most of the driver's functions go to the loader
 * unchanged.
 *
 * What this file adds:
 *  - it opens the HAL once and refuses the loader's interface negotiation when there is none,
 *    so the loader quietly skips this ICD where no Android driver is present;
 *  - it hides VK_ANDROID_native_buffer from the device extensions. That extension is the private
 *    interface between Android's own loader and the driver (swapchain images from gralloc), not
 *    something an application may enable;
 *  - window system integration for Wayland (wsi_wayland.c): the driver has none of its own, on
 *    Android the platform loader builds it. This file adds VK_KHR_surface,
 *    VK_KHR_wayland_surface and VK_KHR_swapchain to the extension lists, keeps them away from
 *    the driver when an application enables them, and enables the driver extensions the
 *    swapchain is built on (dma-buf import, sync files) in their place.
 *
 * The driver is bionic code: the process needs libhybris' environment (HYBRIS_LD_LIBRARY_PATH,
 * the TLS padding from /etc/ld.so.preload, GLIBC_TUNABLES), as for EGL. The session has it.
 */
#include <stdlib.h>
#include <string.h>

#include <hardware/hardware.h>

#include "hybris_vulkan.h"

/* From Android's hardware/hwvulkan.h, which android-headers does not ship. The ABI has not
 * changed since Android 7: module API 0.1, device API 0.1.0, three entry points after the
 * common device header. */
#define HWVULKAN_HARDWARE_MODULE_ID "vulkan"
#define HWVULKAN_DEVICE_0 "vk0"
#define HWVULKAN_MODULE_API_VERSION_0_1 0x0001u
#define HWVULKAN_DEVICE_API_VERSION_0_1 0x00010000u

typedef struct hwvulkan_device_t {
	struct hw_device_t common;
	PFN_vkEnumerateInstanceExtensionProperties EnumerateInstanceExtensionProperties;
	PFN_vkCreateInstance CreateInstance;
	PFN_vkGetInstanceProcAddr GetInstanceProcAddr;
} hwvulkan_device_t;

/* The loader interface this ICD speaks. 5: the loader leaves the apiVersion check to the ICD
 * and passes surfaces as VkIcdSurfaceBase; 6 and 7 add only Windows adapter enumeration and
 * vk_icdGetPhysicalDeviceProcAddr rules that do not change anything here. */
#define ICD_INTERFACE_VERSION 5u

#define ARRAY_SIZE(a) (sizeof(a) / sizeof(*(a)))

/* Device extensions the driver reports that no application may see */
static const char *const hidden_device_extensions[] = {
	"VK_ANDROID_native_buffer",
};

/* Instance extensions the WSI needs on a Vulkan 1.0 instance (on 1.1 they are core); added
 * when the application enables a surface extension and the driver has them */
static const char *const wsi_instance_dependencies[] = {
	VK_KHR_GET_PHYSICAL_DEVICE_PROPERTIES_2_EXTENSION_NAME,
	VK_KHR_EXTERNAL_MEMORY_CAPABILITIES_EXTENSION_NAME,
	VK_KHR_EXTERNAL_SEMAPHORE_CAPABILITIES_EXTENSION_NAME,
	VK_KHR_EXTERNAL_FENCE_CAPABILITIES_EXTENSION_NAME,
};

/* Device extensions a swapchain cannot do without: its images are dma-bufs imported with an
 * explicit LINEAR layout (the driver cannot export them, measured 2026-11-20) */
static const char *const swapchain_required_extensions[] = {
	VK_KHR_EXTERNAL_MEMORY_FD_EXTENSION_NAME,
	VK_EXT_EXTERNAL_MEMORY_DMA_BUF_EXTENSION_NAME,
	VK_EXT_IMAGE_DRM_FORMAT_MODIFIER_EXTENSION_NAME,
};

/* What those need on a Vulkan 1.0 device, and the sync file import for acquiring; added when
 * the driver has them */
static const char *const swapchain_optional_extensions[] = {
	VK_KHR_EXTERNAL_MEMORY_EXTENSION_NAME,
	VK_KHR_BIND_MEMORY_2_EXTENSION_NAME,
	VK_KHR_IMAGE_FORMAT_LIST_EXTENSION_NAME,
	VK_KHR_SAMPLER_YCBCR_CONVERSION_EXTENSION_NAME,
	VK_KHR_GET_MEMORY_REQUIREMENTS_2_EXTENSION_NAME,
	VK_KHR_DEDICATED_ALLOCATION_EXTENSION_NAME,
	VK_KHR_MAINTENANCE_1_EXTENSION_NAME,
	VK_KHR_EXTERNAL_SEMAPHORE_EXTENSION_NAME,
	VK_KHR_EXTERNAL_FENCE_EXTENSION_NAME,
	VK_KHR_EXTERNAL_SEMAPHORE_FD_EXTENSION_NAME,
	VK_KHR_EXTERNAL_FENCE_FD_EXTENSION_NAME,
};

struct hv_driver hv_drv;

static pthread_once_t hal_once = PTHREAD_ONCE_INIT;
static const hwvulkan_device_t *hal;

static pthread_mutex_t devices_lock = PTHREAD_MUTEX_INITIALIZER;
static struct hv_device *devices;

static void open_hal(void)
{
	const struct hw_module_t *module = NULL;
	struct hw_device_t *device = NULL;

	if (hw_get_module(HWVULKAN_HARDWARE_MODULE_ID, &module) != 0 || !module)
		return;
	/* Android's loader refuses other versions, so must we: the layout below would not hold */
	if (module->module_api_version != HWVULKAN_MODULE_API_VERSION_0_1 || !module->methods ||
	    !module->methods->open)
		return;
	if (module->methods->open(module, HWVULKAN_DEVICE_0, &device) != 0 || !device)
		return;
	const hwvulkan_device_t *d = (const hwvulkan_device_t *)device;
	if (device->version != HWVULKAN_DEVICE_API_VERSION_0_1 || !d->EnumerateInstanceExtensionProperties ||
	    !d->CreateInstance || !d->GetInstanceProcAddr) {
		/* A device we cannot use stays open: closing it would unload a driver that may keep
		 * state for the module, and the process does not try again anyway */
		return;
	}
	hal = d;
}

static bool name_in(const char *const *names, size_t n, const char *name)
{
	for (size_t i = 0; i < n; i++)
		if (!strncmp(names[i], name, VK_MAX_EXTENSION_NAME_SIZE))
			return true;
	return false;
}

static bool extension_in(const VkExtensionProperties *list, uint32_t n, const char *name)
{
	for (uint32_t i = 0; i < n; i++)
		if (!strncmp(list[i].extensionName, name, VK_MAX_EXTENSION_NAME_SIZE))
			return true;
	return false;
}

/* The driver's extension list in a new array (*out, free it), with Vulkan's two-call idiom.
 * enumerate(ctx, count, props) is the driver's function bound to its first argument. */
typedef VkResult (*enumerate_fn)(void *ctx, uint32_t *count, VkExtensionProperties *props);

static VkResult driver_list(enumerate_fn enumerate, void *ctx, VkExtensionProperties **out, uint32_t *n)
{
	*out = NULL;
	*n = 0;
	VkResult r = enumerate(ctx, n, NULL);
	if (r != VK_SUCCESS)
		return r;
	VkExtensionProperties *all = calloc(*n ? *n : 1, sizeof *all);
	if (!all)
		return VK_ERROR_OUT_OF_HOST_MEMORY;
	r = enumerate(ctx, n, all);
	/* VK_INCOMPLETE would mean the list grew in between; n is then what was filled in */
	if (r < 0) {
		free(all);
		return r;
	}
	*out = all;
	return VK_SUCCESS;
}

/* The driver's list without the hidden names, plus the extra ones it lacks, to the application
 * with Vulkan's two-call idiom */
static VkResult answer_list(enumerate_fn enumerate, void *ctx, const char *const *hidden, size_t n_hidden,
			    const VkExtensionProperties *extra, uint32_t n_extra, uint32_t *count,
			    VkExtensionProperties *properties)
{
	VkExtensionProperties *all;
	uint32_t n;
	VkResult r = driver_list(enumerate, ctx, &all, &n);
	if (r != VK_SUCCESS)
		return r;
	VkExtensionProperties *list = realloc(all, (n + n_extra ? n + n_extra : 1) * sizeof *list);
	if (!list) {
		free(all);
		return VK_ERROR_OUT_OF_HOST_MEMORY;
	}
	uint32_t kept = 0;
	for (uint32_t i = 0; i < n; i++)
		if (!name_in(hidden, n_hidden, list[i].extensionName))
			list[kept++] = list[i];
	for (uint32_t i = 0; i < n_extra; i++)
		if (!extension_in(list, kept, extra[i].extensionName))
			list[kept++] = extra[i];

	if (!properties) {
		*count = kept;
		free(list);
		return VK_SUCCESS;
	}
	uint32_t out = *count < kept ? *count : kept;
	memcpy(properties, list, out * sizeof *list);
	*count = out;
	free(list);
	return out < kept ? VK_INCOMPLETE : VK_SUCCESS;
}

/* Instance level */

static VkResult hal_instance_extensions(void *ctx, uint32_t *count, VkExtensionProperties *props)
{
	return hal->EnumerateInstanceExtensionProperties(NULL, count, props);
}

static VKAPI_ATTR VkResult VKAPI_CALL enumerate_instance_extensions(const char *layer, uint32_t *count,
								   VkExtensionProperties *properties)
{
	/* The driver has no layers; whatever it answers for one stays its answer */
	if (layer)
		return hal->EnumerateInstanceExtensionProperties(layer, count, properties);
	return answer_list(hal_instance_extensions, NULL, NULL, 0, hv_wsi_instance_extensions,
			   hv_wsi_instance_extension_count, count, properties);
}

static bool wsi_instance_extension(const char *name)
{
	return extension_in(hv_wsi_instance_extensions, hv_wsi_instance_extension_count, name);
}

static bool wsi_device_extension(const char *name)
{
	return extension_in(hv_wsi_device_extensions, hv_wsi_device_extension_count, name);
}

static void load_instance_functions(VkInstance instance)
{
	/* The same pointers for every instance: a second instance stores what is already there */
	hv_drv.GetInstanceProcAddr = hal->GetInstanceProcAddr;
	hv_drv.CreateDevice = (PFN_vkCreateDevice)hal->GetInstanceProcAddr(instance, "vkCreateDevice");
	hv_drv.EnumerateDeviceExtensionProperties = (PFN_vkEnumerateDeviceExtensionProperties)hal->GetInstanceProcAddr(
		instance, "vkEnumerateDeviceExtensionProperties");
	hv_drv.GetDeviceProcAddr = (PFN_vkGetDeviceProcAddr)hal->GetInstanceProcAddr(instance, "vkGetDeviceProcAddr");
}

/* The application's create info without the WSI extensions (the driver does not know them),
 * with the instance extensions the WSI is built on in their place */
static VKAPI_ATTR VkResult VKAPI_CALL create_instance(const VkInstanceCreateInfo *info,
						     const VkAllocationCallbacks *allocator, VkInstance *instance)
{
	const char **names = calloc(info->enabledExtensionCount + ARRAY_SIZE(wsi_instance_dependencies) + 1,
				    sizeof *names);
	if (!names)
		return VK_ERROR_OUT_OF_HOST_MEMORY;
	uint32_t n = 0;
	bool wsi = false;
	for (uint32_t i = 0; i < info->enabledExtensionCount; i++) {
		const char *name = info->ppEnabledExtensionNames[i];
		if (wsi_instance_extension(name))
			wsi = true;
		else
			names[n++] = name;
	}
	if (wsi) {
		VkExtensionProperties *have;
		uint32_t n_have;
		VkResult r = driver_list(hal_instance_extensions, NULL, &have, &n_have);
		if (r != VK_SUCCESS) {
			free(names);
			return r;
		}
		for (size_t i = 0; i < ARRAY_SIZE(wsi_instance_dependencies); i++) {
			const char *dep = wsi_instance_dependencies[i];
			if (extension_in(have, n_have, dep) && !name_in(names, n, dep))
				names[n++] = dep;
		}
		free(have);
	}
	VkInstanceCreateInfo copy = *info;
	copy.enabledExtensionCount = n;
	copy.ppEnabledExtensionNames = names;
	VkResult r = hal->CreateInstance(&copy, allocator, instance);
	free(names);
	if (r == VK_SUCCESS)
		load_instance_functions(*instance);
	return r;
}

/* Device level */

static VkResult driver_device_extensions(void *ctx, uint32_t *count, VkExtensionProperties *props)
{
	return hv_drv.EnumerateDeviceExtensionProperties((VkPhysicalDevice)ctx, NULL, count, props);
}

static VKAPI_ATTR VkResult VKAPI_CALL enumerate_device_extensions(VkPhysicalDevice physical_device,
								 const char *layer, uint32_t *count,
								 VkExtensionProperties *properties)
{
	if (!hv_drv.EnumerateDeviceExtensionProperties)
		return VK_ERROR_INITIALIZATION_FAILED;
	if (layer)
		return hv_drv.EnumerateDeviceExtensionProperties(physical_device, layer, count, properties);

	/* VK_KHR_swapchain only where the driver has what it is built on */
	VkExtensionProperties *have;
	uint32_t n_have;
	VkResult r = driver_list(driver_device_extensions, physical_device, &have, &n_have);
	if (r != VK_SUCCESS)
		return r;
	bool wsi = true;
	for (size_t i = 0; i < ARRAY_SIZE(swapchain_required_extensions); i++)
		wsi = wsi && extension_in(have, n_have, swapchain_required_extensions[i]);
	free(have);
	return answer_list(driver_device_extensions, physical_device, hidden_device_extensions,
			   ARRAY_SIZE(hidden_device_extensions), hv_wsi_device_extensions,
			   wsi ? hv_wsi_device_extension_count : 0, count, properties);
}

#define DEVICE_FN(dev, name) ((dev)->name = (PFN_vk##name)hv_drv.GetDeviceProcAddr((dev)->handle, "vk" #name))

static void load_device_functions(struct hv_device *dev)
{
	DEVICE_FN(dev, DestroyDevice);
	DEVICE_FN(dev, GetDeviceQueue);
	DEVICE_FN(dev, QueueSubmit);
	DEVICE_FN(dev, CreateImage);
	DEVICE_FN(dev, DestroyImage);
	DEVICE_FN(dev, GetImageSubresourceLayout);
	DEVICE_FN(dev, GetImageMemoryRequirements);
	DEVICE_FN(dev, AllocateMemory);
	DEVICE_FN(dev, FreeMemory);
	DEVICE_FN(dev, BindImageMemory);
	DEVICE_FN(dev, GetMemoryFdPropertiesKHR);
	DEVICE_FN(dev, CreateFence);
	DEVICE_FN(dev, DestroyFence);
	DEVICE_FN(dev, WaitForFences);
	DEVICE_FN(dev, ResetFences);
	DEVICE_FN(dev, ImportSemaphoreFdKHR);
	DEVICE_FN(dev, ImportFenceFdKHR);
}

struct hv_device *hv_device_find(VkDevice device)
{
	pthread_mutex_lock(&devices_lock);
	struct hv_device *dev = devices;
	while (dev && dev->handle != device)
		dev = dev->next;
	pthread_mutex_unlock(&devices_lock);
	return dev;
}

/* VK_KHR_swapchain stays away from the driver; the extensions it is built on go in its place */
static VKAPI_ATTR VkResult VKAPI_CALL create_device(VkPhysicalDevice physical_device, const VkDeviceCreateInfo *info,
						   const VkAllocationCallbacks *allocator, VkDevice *device)
{
	if (!hv_drv.CreateDevice || !hv_drv.GetDeviceProcAddr)
		return VK_ERROR_INITIALIZATION_FAILED;
	struct hv_device *dev = calloc(1, sizeof *dev);
	const char **names = calloc(info->enabledExtensionCount + ARRAY_SIZE(swapchain_required_extensions) +
					    ARRAY_SIZE(swapchain_optional_extensions) + 1,
				    sizeof *names);
	if (!dev || !names) {
		free(dev);
		free(names);
		return VK_ERROR_OUT_OF_HOST_MEMORY;
	}
	uint32_t n = 0;
	for (uint32_t i = 0; i < info->enabledExtensionCount; i++) {
		const char *name = info->ppEnabledExtensionNames[i];
		if (wsi_device_extension(name))
			dev->swapchain = true;
		else
			names[n++] = name;
	}
	VkResult r = VK_SUCCESS;
	if (dev->swapchain) {
		VkExtensionProperties *have;
		uint32_t n_have;
		r = driver_list(driver_device_extensions, physical_device, &have, &n_have);
		for (size_t i = 0; r == VK_SUCCESS && i < ARRAY_SIZE(swapchain_required_extensions); i++) {
			const char *ext = swapchain_required_extensions[i];
			if (!extension_in(have, n_have, ext))
				r = VK_ERROR_EXTENSION_NOT_PRESENT;
			else if (!name_in(names, n, ext))
				names[n++] = ext;
		}
		for (size_t i = 0; r == VK_SUCCESS && i < ARRAY_SIZE(swapchain_optional_extensions); i++) {
			const char *ext = swapchain_optional_extensions[i];
			if (extension_in(have, n_have, ext) && !name_in(names, n, ext))
				names[n++] = ext;
		}
		dev->sync_fd_import = name_in(names, n, VK_KHR_EXTERNAL_SEMAPHORE_FD_EXTENSION_NAME) &&
				      name_in(names, n, VK_KHR_EXTERNAL_FENCE_FD_EXTENSION_NAME);
		free(have);
	}
	if (r == VK_SUCCESS) {
		VkDeviceCreateInfo copy = *info;
		copy.enabledExtensionCount = n;
		copy.ppEnabledExtensionNames = names;
		r = hv_drv.CreateDevice(physical_device, &copy, allocator, device);
	}
	free(names);
	if (r != VK_SUCCESS) {
		free(dev);
		return r;
	}

	dev->handle = *device;
	dev->physical_device = physical_device;
	pthread_mutex_init(&dev->queue_lock, NULL);
	load_device_functions(dev);
	/* A queue for the acquire fallback; one with flags (protected) needs vkGetDeviceQueue2 */
	if (info->queueCreateInfoCount && !info->pQueueCreateInfos[0].flags && dev->GetDeviceQueue)
		dev->GetDeviceQueue(dev->handle, info->pQueueCreateInfos[0].queueFamilyIndex, 0, &dev->queue);

	pthread_mutex_lock(&devices_lock);
	dev->next = devices;
	devices = dev;
	pthread_mutex_unlock(&devices_lock);
	return VK_SUCCESS;
}

static VKAPI_ATTR void VKAPI_CALL destroy_device(VkDevice device, const VkAllocationCallbacks *allocator)
{
	if (!device)
		return;
	pthread_mutex_lock(&devices_lock);
	struct hv_device **p = &devices;
	while (*p && (*p)->handle != device)
		p = &(*p)->next;
	struct hv_device *dev = *p;
	if (dev)
		*p = dev->next;
	pthread_mutex_unlock(&devices_lock);

	if (dev) {
		dev->DestroyDevice(device, allocator);
		pthread_mutex_destroy(&dev->queue_lock);
		free(dev);
	} else {
		/* Not created through this ICD: cannot happen with the loader, but the driver knows */
		PFN_vkDestroyDevice destroy = (PFN_vkDestroyDevice)hv_drv.GetDeviceProcAddr(device, "vkDestroyDevice");
		if (destroy)
			destroy(device, allocator);
	}
}

static VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL get_device_proc_addr(VkDevice device, const char *name);

/* Device functions this ICD answers for itself */
static PFN_vkVoidFunction own_device_function(VkDevice device, const char *name)
{
	if (!strcmp(name, "vkGetDeviceProcAddr"))
		return (PFN_vkVoidFunction)get_device_proc_addr;
	if (!strcmp(name, "vkDestroyDevice"))
		return (PFN_vkVoidFunction)destroy_device;
	PFN_vkVoidFunction wsi = hv_wsi_device_function(name);
	if (!wsi)
		return NULL;
	/* A device without VK_KHR_swapchain has none of its functions; with no device (asked
	 * through vkGetInstanceProcAddr) the loader decides */
	if (device) {
		struct hv_device *dev = hv_device_find(device);
		if (!dev || !dev->swapchain)
			return NULL;
	}
	return wsi;
}

static VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL get_device_proc_addr(VkDevice device, const char *name)
{
	if (!name)
		return NULL;
	PFN_vkVoidFunction own = own_device_function(device, name);
	if (own)
		return own;
	if (hv_wsi_device_function(name))
		return NULL;
	return hv_drv.GetDeviceProcAddr(device, name);
}

/* Loader interface */

HV_EXPORT VKAPI_ATTR VkResult VKAPI_CALL vk_icdNegotiateLoaderICDInterfaceVersion(uint32_t *version);
HV_EXPORT VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vk_icdGetInstanceProcAddr(VkInstance instance, const char *name);

/* The first call of the loader: without a driver, this ICD is not there */
HV_EXPORT VKAPI_ATTR VkResult VKAPI_CALL vk_icdNegotiateLoaderICDInterfaceVersion(uint32_t *version)
{
	pthread_once(&hal_once, open_hal);
	if (!hal)
		return VK_ERROR_INCOMPATIBLE_DRIVER;
	/* Older loaders expect a different handling of surfaces and the API version */
	if (*version < ICD_INTERFACE_VERSION)
		return VK_ERROR_INCOMPATIBLE_DRIVER;
	*version = ICD_INTERFACE_VERSION;
	return VK_SUCCESS;
}

/* Everything the loader asks for comes from the driver, except the functions this ICD replaces.
 * No vk_icdGetPhysicalDeviceProcAddr: it must answer only for physical-device functions, which
 * the driver's vkGetInstanceProcAddr cannot tell apart, and the loader knows every function of
 * this driver's Vulkan version by itself. */
HV_EXPORT VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vk_icdGetInstanceProcAddr(VkInstance instance, const char *name)
{
	pthread_once(&hal_once, open_hal);
	if (!hal || !name)
		return NULL;

	if (!strcmp(name, "vkGetInstanceProcAddr"))
		return (PFN_vkVoidFunction)vk_icdGetInstanceProcAddr;
	if (!instance) {
		/* The HAL hands these out directly; the driver's vkGetInstanceProcAddr(NULL, ...) is
		 * not required to know them */
		if (!strcmp(name, "vkCreateInstance"))
			return (PFN_vkVoidFunction)create_instance;
		if (!strcmp(name, "vkEnumerateInstanceExtensionProperties"))
			return (PFN_vkVoidFunction)enumerate_instance_extensions;
		return hal->GetInstanceProcAddr(NULL, name);
	}
	if (!strcmp(name, "vkEnumerateDeviceExtensionProperties"))
		return (PFN_vkVoidFunction)enumerate_device_extensions;
	if (!strcmp(name, "vkCreateDevice"))
		return (PFN_vkVoidFunction)create_device;
	PFN_vkVoidFunction own = hv_wsi_instance_function(name);
	if (!own)
		own = own_device_function(NULL, name);
	return own ? own : hal->GetInstanceProcAddr(instance, name);
}
