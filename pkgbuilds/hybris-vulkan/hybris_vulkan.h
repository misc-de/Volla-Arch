/*
 * hybris-vulkan: what the ICD core (hybris_vulkan.c) and the Wayland WSI (wsi_wayland.c) share.
 */
#ifndef HYBRIS_VULKAN_H
#define HYBRIS_VULKAN_H

#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>

#define VK_USE_PLATFORM_WAYLAND_KHR
#include <vulkan/vk_icd.h>
#include <vulkan/vulkan.h>

#define HV_EXPORT __attribute__((visibility("default")))

/* The driver's instance-level functions, taken from the first instance this ICD creates. The
 * driver hands out the same pointers for every instance. */
struct hv_driver {
	PFN_vkGetInstanceProcAddr GetInstanceProcAddr;
	PFN_vkCreateDevice CreateDevice;
	PFN_vkEnumerateDeviceExtensionProperties EnumerateDeviceExtensionProperties;
	PFN_vkGetDeviceProcAddr GetDeviceProcAddr;
};
extern struct hv_driver hv_drv;

/* One VkDevice of the driver, with the driver functions the WSI calls */
struct hv_device {
	struct hv_device *next;
	VkDevice handle;
	VkPhysicalDevice physical_device;
	/* The application enabled VK_KHR_swapchain */
	bool swapchain;
	/* VK_KHR_external_semaphore_fd and VK_KHR_external_fence_fd are on: an acquired image's
	 * semaphore and fence are signalled by importing an already signalled sync file */
	bool sync_fd_import;
	/* The first queue the application asked for: the fallback to signal an acquire with an
	 * empty submission, under queue_lock */
	VkQueue queue;
	pthread_mutex_t queue_lock;

	PFN_vkDestroyDevice DestroyDevice;
	PFN_vkGetDeviceQueue GetDeviceQueue;
	PFN_vkQueueSubmit QueueSubmit;
	PFN_vkCreateImage CreateImage;
	PFN_vkDestroyImage DestroyImage;
	PFN_vkGetImageSubresourceLayout GetImageSubresourceLayout;
	PFN_vkGetImageMemoryRequirements GetImageMemoryRequirements;
	PFN_vkAllocateMemory AllocateMemory;
	PFN_vkFreeMemory FreeMemory;
	PFN_vkBindImageMemory BindImageMemory;
	PFN_vkGetMemoryFdPropertiesKHR GetMemoryFdPropertiesKHR;
	PFN_vkCreateFence CreateFence;
	PFN_vkDestroyFence DestroyFence;
	PFN_vkWaitForFences WaitForFences;
	PFN_vkResetFences ResetFences;
	PFN_vkImportSemaphoreFdKHR ImportSemaphoreFdKHR;
	PFN_vkImportFenceFdKHR ImportFenceFdKHR;
};

struct hv_device *hv_device_find(VkDevice device);

/* Wayland WSI (wsi_wayland.c): the functions it implements, by name; NULL for any other name */
PFN_vkVoidFunction hv_wsi_instance_function(const char *name);
PFN_vkVoidFunction hv_wsi_device_function(const char *name);

/* The extensions the WSI adds to the driver's lists */
extern const VkExtensionProperties hv_wsi_instance_extensions[];
extern const uint32_t hv_wsi_instance_extension_count;
extern const VkExtensionProperties hv_wsi_device_extensions[];
extern const uint32_t hv_wsi_device_extension_count;

#endif
