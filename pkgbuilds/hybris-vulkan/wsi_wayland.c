/*
 * hybris-vulkan: VK_KHR_surface, VK_KHR_wayland_surface and VK_KHR_swapchain for the Android
 * Vulkan driver, which has no window system integration of its own.
 *
 * Swapchain images are dma-bufs that the driver imports (it cannot export them: LINEAR images
 * are import-only, measured 2026-11-20 on the Mali-G615). They come from a DMA heap (MediaTek's
 * mtk_mm, where gralloc allocates too), are bound to VkImages with an explicit LINEAR layout,
 * and reach the compositor as wl_buffers through zwp_linux_dmabuf_v1.
 *
 * Synchronisation: Mali attaches no implicit fences to its dma-bufs, so a compositor would read
 * half-rendered images. vkQueuePresentKHR therefore waits on the CPU until the queue has
 * finished everything submitted before (an empty submission with a fence) and only then
 * commits the buffer. Acquiring an image waits for the compositor's wl_buffer.release and
 * signals the application's semaphore and fence by importing an already signalled sync file
 * (fd -1), or with an empty submission where the driver lacks sync file import.
 *
 * Present modes: FIFO waits for the previous frame's wl_surface.frame callback before the next
 * commit (at most FRAME_WAIT_NS: a hidden surface gets no callbacks); MAILBOX commits at once.
 *
 * All Wayland traffic of a swapchain runs on its own event queue, so it never dispatches the
 * application's events and is safe next to the application's own event loop.
 */
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

#include <linux/dma-heap.h>
#include <wayland-client.h>

#include "hybris_vulkan.h"
#include "linux-dmabuf-v1-client-protocol.h"

#define ARRAY_SIZE(a) (sizeof(a) / sizeof(*(a)))

#define FOURCC(a, b, c, d) ((uint32_t)(a) | ((uint32_t)(b) << 8) | ((uint32_t)(c) << 16) | ((uint32_t)(d) << 24))
#define DRM_FORMAT_ARGB8888 FOURCC('A', 'R', '2', '4')
#define DRM_FORMAT_XRGB8888 FOURCC('X', 'R', '2', '4')
#define DRM_FORMAT_ABGR8888 FOURCC('A', 'B', '2', '4')
#define DRM_FORMAT_XBGR8888 FOURCC('X', 'B', '2', '4')
#define DRM_FORMAT_MOD_LINEAR 0ull

/* Where swapchain buffers come from, in this order: MediaTek's uncached heap, which gralloc
 * uses for the buffers phoc composites (all of them, seen 2026-11-20), the cached one, the
 * generic heap of other kernels. HYBRIS_VULKAN_DMA_HEAP names one to try first. */
static const char *const dma_heaps[] = {
	"/dev/dma_heap/mtk_mm-uncached",
	"/dev/dma_heap/mtk_mm",
	"/dev/dma_heap/system",
};

/* How long a FIFO present waits for the previous frame's callback. The compositor sends none
 * while the surface is hidden; without a limit the application would hang there. */
#define FRAME_WAIT_NS (100 * 1000000LL)
/* How long a present waits for the GPU to finish the image */
#define RENDER_WAIT_NS (5 * 1000000000ULL)

#define MIN_IMAGES 2u
#define MAX_IMAGES 8u
/* The largest image the driver takes (16384 measured); more than any output here */
#define MAX_EXTENT 16384u

const VkExtensionProperties hv_wsi_instance_extensions[] = {
	{VK_KHR_SURFACE_EXTENSION_NAME, VK_KHR_SURFACE_SPEC_VERSION},
	{VK_KHR_WAYLAND_SURFACE_EXTENSION_NAME, VK_KHR_WAYLAND_SURFACE_SPEC_VERSION},
};
const uint32_t hv_wsi_instance_extension_count = ARRAY_SIZE(hv_wsi_instance_extensions);

const VkExtensionProperties hv_wsi_device_extensions[] = {
	{VK_KHR_SWAPCHAIN_EXTENSION_NAME, VK_KHR_SWAPCHAIN_SPEC_VERSION},
};
const uint32_t hv_wsi_device_extension_count = ARRAY_SIZE(hv_wsi_device_extensions);

/* Swapchain formats and their DRM fourcc codes (little-endian: B8G8R8A8 is ARGB8888), without
 * and with alpha for the compositor */
static const struct {
	VkFormat format;
	uint32_t opaque, alpha;
} formats[] = {
	{VK_FORMAT_B8G8R8A8_SRGB, DRM_FORMAT_XRGB8888, DRM_FORMAT_ARGB8888},
	{VK_FORMAT_B8G8R8A8_UNORM, DRM_FORMAT_XRGB8888, DRM_FORMAT_ARGB8888},
	{VK_FORMAT_R8G8B8A8_SRGB, DRM_FORMAT_XBGR8888, DRM_FORMAT_ABGR8888},
	{VK_FORMAT_R8G8B8A8_UNORM, DRM_FORMAT_XBGR8888, DRM_FORMAT_ABGR8888},
};

static const VkPresentModeKHR present_modes[] = {
	VK_PRESENT_MODE_FIFO_KHR,
	VK_PRESENT_MODE_MAILBOX_KHR,
};

/* HYBRIS_VULKAN_DEBUG=1: what the swapchains do, on stderr */
static int debug = -1;

static void hv_log(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void hv_log(const char *fmt, ...)
{
	if (debug < 0) {
		const char *env = getenv("HYBRIS_VULKAN_DEBUG");
		debug = env && *env && strcmp(env, "0");
	}
	if (!debug)
		return;
	va_list ap;
	va_start(ap, fmt);
	fputs("hybris-vulkan: ", stderr);
	vfprintf(stderr, fmt, ap);
	fputc('\n', stderr);
	va_end(ap);
}

/* Copy a list out with Vulkan's two-call idiom */
static VkResult answer(void *out, const void *list, size_t size, uint32_t n, uint32_t *count)
{
	if (!out) {
		*count = n;
		return VK_SUCCESS;
	}
	uint32_t k = *count < n ? *count : n;
	memcpy(out, list, k * size);
	*count = k;
	return k < n ? VK_INCOMPLETE : VK_SUCCESS;
}

/* A surface of this ICD (create_wayland_surface), or the loader's own VkIcdSurface where an older
 * loader passes that; both begin with VkIcdSurfaceBase */
static VkIcdSurfaceWayland *wayland_surface(VkSurfaceKHR surface)
{
	VkIcdSurfaceBase *base = (VkIcdSurfaceBase *)(uintptr_t)surface;
	return base && base->platform == VK_ICD_WSI_PLATFORM_WAYLAND ? (VkIcdSurfaceWayland *)base : NULL;
}

/* Surfaces: with ICD interface 3 and later the loader asks the ICD for a surface of its own and
 * refuses every surface query with VK_ERROR_EXTENSION_NOT_PRESENT when it cannot have one
 * (loader wsi_unwrap_icd_surface, seen with vkcube 2026-11-21) */
static VKAPI_ATTR VkResult VKAPI_CALL create_wayland_surface(VkInstance instance,
							    const VkWaylandSurfaceCreateInfoKHR *info,
							    const VkAllocationCallbacks *allocator, VkSurfaceKHR *out)
{
	VkIcdSurfaceWayland *surface = calloc(1, sizeof *surface);
	if (!surface)
		return VK_ERROR_OUT_OF_HOST_MEMORY;
	surface->base.platform = VK_ICD_WSI_PLATFORM_WAYLAND;
	surface->display = info->display;
	surface->surface = info->surface;
	*out = (VkSurfaceKHR)(uintptr_t)surface;
	return VK_SUCCESS;
}

static VKAPI_ATTR void VKAPI_CALL destroy_surface(VkInstance instance, VkSurfaceKHR surface,
						 const VkAllocationCallbacks *allocator)
{
	free((void *)(uintptr_t)surface);
}

/* Surface queries */

static VKAPI_ATTR VkResult VKAPI_CALL surface_support(VkPhysicalDevice physical_device, uint32_t queue_family,
						     VkSurfaceKHR surface, VkBool32 *supported)
{
	*supported = wayland_surface(surface) ? VK_TRUE : VK_FALSE;
	return VK_SUCCESS;
}

static VKAPI_ATTR VkResult VKAPI_CALL surface_capabilities(VkPhysicalDevice physical_device, VkSurfaceKHR surface,
							  VkSurfaceCapabilitiesKHR *caps)
{
	if (!wayland_surface(surface))
		return VK_ERROR_SURFACE_LOST_KHR;
	*caps = (VkSurfaceCapabilitiesKHR){
		.minImageCount = MIN_IMAGES,
		.maxImageCount = MAX_IMAGES,
		/* On Wayland the swapchain decides the size of the surface */
		.currentExtent = {UINT32_MAX, UINT32_MAX},
		.minImageExtent = {1, 1},
		.maxImageExtent = {MAX_EXTENT, MAX_EXTENT},
		.maxImageArrayLayers = 1,
		.supportedTransforms = VK_SURFACE_TRANSFORM_IDENTITY_BIT_KHR,
		.currentTransform = VK_SURFACE_TRANSFORM_IDENTITY_BIT_KHR,
		.supportedCompositeAlpha = VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR | VK_COMPOSITE_ALPHA_PRE_MULTIPLIED_BIT_KHR,
		.supportedUsageFlags = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT |
				       VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_SAMPLED_BIT |
				       VK_IMAGE_USAGE_INPUT_ATTACHMENT_BIT,
	};
	return VK_SUCCESS;
}

static VKAPI_ATTR VkResult VKAPI_CALL surface_formats(VkPhysicalDevice physical_device, VkSurfaceKHR surface,
						     uint32_t *count, VkSurfaceFormatKHR *out)
{
	if (!wayland_surface(surface))
		return VK_ERROR_SURFACE_LOST_KHR;
	VkSurfaceFormatKHR list[ARRAY_SIZE(formats)];
	for (size_t i = 0; i < ARRAY_SIZE(formats); i++)
		list[i] = (VkSurfaceFormatKHR){formats[i].format, VK_COLOR_SPACE_SRGB_NONLINEAR_KHR};
	return answer(out, list, sizeof *list, ARRAY_SIZE(list), count);
}

static VKAPI_ATTR VkResult VKAPI_CALL surface_present_modes(VkPhysicalDevice physical_device, VkSurfaceKHR surface,
							   uint32_t *count, VkPresentModeKHR *out)
{
	if (!wayland_surface(surface))
		return VK_ERROR_SURFACE_LOST_KHR;
	return answer(out, present_modes, sizeof *present_modes, ARRAY_SIZE(present_modes), count);
}

static VKAPI_ATTR VkBool32 VKAPI_CALL wayland_presentation_support(VkPhysicalDevice physical_device,
								  uint32_t queue_family, struct wl_display *display)
{
	return VK_TRUE;
}

/* Vulkan 1.1 with VK_KHR_swapchain: one device, local presentation only */
static VKAPI_ATTR VkResult VKAPI_CALL device_group_present_capabilities(VkDevice device,
								       VkDeviceGroupPresentCapabilitiesKHR *caps)
{
	memset(caps->presentMask, 0, sizeof caps->presentMask);
	caps->presentMask[0] = 1;
	caps->modes = VK_DEVICE_GROUP_PRESENT_MODE_LOCAL_BIT_KHR;
	return VK_SUCCESS;
}

static VKAPI_ATTR VkResult VKAPI_CALL device_group_surface_present_modes(VkDevice device, VkSurfaceKHR surface,
									VkDeviceGroupPresentModeFlagsKHR *modes)
{
	if (!wayland_surface(surface))
		return VK_ERROR_SURFACE_LOST_KHR;
	*modes = VK_DEVICE_GROUP_PRESENT_MODE_LOCAL_BIT_KHR;
	return VK_SUCCESS;
}

/* Swapchain */

enum image_state {
	IMAGE_FREE,	 /* the application may acquire it */
	IMAGE_ACQUIRED,	 /* the application has it */
	IMAGE_PRESENTED, /* the compositor has it until wl_buffer.release */
};

struct sc_image {
	VkImage image;
	VkDeviceMemory memory;
	struct wl_buffer *buffer;
	enum image_state state;
};

struct swapchain {
	struct hv_device *dev;
	struct wl_display *display;
	struct wl_event_queue *queue;
	struct wl_display *display_wrapper;
	struct wl_registry *registry;
	struct zwp_linux_dmabuf_v1 *dmabuf;
	/* The application's surface, wrapped onto this swapchain's queue */
	struct wl_surface *surface;
	struct wl_callback *frame;
	VkPresentModeKHR mode;
	VkFence fence;
	/* VK_SUCCESS until the connection to the compositor is lost */
	VkResult status;
	bool retired;
	uint32_t width, height, drm_format, stride;
	uint32_t count;
	struct sc_image images[];
};

static int64_t now_ns(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (int64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}

static VkResult lost(struct swapchain *sc)
{
	hv_log("connection to the compositor lost: %s (protocol error %d)", strerror(errno),
	       wl_display_get_error(sc->display));
	sc->status = VK_ERROR_SURFACE_LOST_KHR;
	return sc->status;
}

/* Dispatch the swapchain's queue once, until the deadline (CLOCK_MONOTONIC ns, INT64_MAX for
 * none): number of events dispatched (0: none arrived in time, or only some for other queues),
 * -1 when the connection is gone */
static int dispatch_until(struct swapchain *sc, int64_t deadline)
{
	if (deadline == INT64_MAX)
		return wl_display_dispatch_queue(sc->display, sc->queue);
	int64_t left = deadline - now_ns();
	if (left <= 0)
		return 0;
	struct timespec ts = {.tv_sec = left / 1000000000, .tv_nsec = left % 1000000000};
	return wl_display_dispatch_queue_timeout(sc->display, sc->queue, &ts);
}

static void registry_global(void *data, struct wl_registry *registry, uint32_t name, const char *interface,
			    uint32_t version)
{
	struct swapchain *sc = data;
	/* Version 3 has everything used here (params with created/failed, modifiers in add) */
	if (!sc->dmabuf && !strcmp(interface, zwp_linux_dmabuf_v1_interface.name))
		sc->dmabuf = wl_registry_bind(registry, name, &zwp_linux_dmabuf_v1_interface, version < 3 ? version : 3);
}

static void registry_global_remove(void *data, struct wl_registry *registry, uint32_t name)
{
}

static const struct wl_registry_listener registry_listener = {
	.global = registry_global,
	.global_remove = registry_global_remove,
};

struct params_result {
	struct wl_buffer *buffer;
	bool done;
};

static void params_created(void *data, struct zwp_linux_buffer_params_v1 *params, struct wl_buffer *buffer)
{
	struct params_result *r = data;
	r->buffer = buffer;
	r->done = true;
}

static void params_failed(void *data, struct zwp_linux_buffer_params_v1 *params)
{
	struct params_result *r = data;
	hv_log("compositor refused a dma-buf");
	r->done = true;
}

static const struct zwp_linux_buffer_params_v1_listener params_listener = {
	.created = params_created,
	.failed = params_failed,
};

static void buffer_release(void *data, struct wl_buffer *buffer)
{
	struct sc_image *img = data;
	hv_log("release of buffer %p (state %d)", (void *)buffer, img->state);
	if (img->state == IMAGE_PRESENTED)
		img->state = IMAGE_FREE;
}

static const struct wl_buffer_listener buffer_listener = {
	.release = buffer_release,
};

static void frame_done(void *data, struct wl_callback *callback, uint32_t time)
{
	struct swapchain *sc = data;
	hv_log("frame callback");
	if (sc->frame == callback)
		sc->frame = NULL;
	wl_callback_destroy(callback);
}

static const struct wl_callback_listener frame_listener = {
	.done = frame_done,
};

/* A dma-buf of size bytes from the first heap that has one, or -1 */
static int alloc_dma_buf(size_t size)
{
	const char *first = getenv("HYBRIS_VULKAN_DMA_HEAP");
	for (size_t i = 0; i <= ARRAY_SIZE(dma_heaps); i++) {
		const char *path = i == 0 ? first : dma_heaps[i - 1];
		if (!path || !*path)
			continue;
		int heap = open(path, O_RDONLY | O_CLOEXEC);
		if (heap < 0)
			continue;
		struct dma_heap_allocation_data data = {.len = size, .fd_flags = O_RDWR | O_CLOEXEC};
		int r = ioctl(heap, DMA_HEAP_IOCTL_ALLOC, &data);
		close(heap);
		if (r == 0)
			return (int)data.fd;
		hv_log("%s: no %zu bytes", path, size);
	}
	return -1;
}

/* The wl_buffer for one image's dma-buf; waits for the compositor's answer, so a buffer it
 * cannot take fails the swapchain instead of the connection (create_immed would) */
static struct wl_buffer *create_buffer(struct swapchain *sc, struct sc_image *img, int fd)
{
	struct params_result res = {0};
	struct zwp_linux_buffer_params_v1 *params = zwp_linux_dmabuf_v1_create_params(sc->dmabuf);
	zwp_linux_buffer_params_v1_add_listener(params, &params_listener, &res);
	zwp_linux_buffer_params_v1_add(params, fd, 0, 0, sc->stride, DRM_FORMAT_MOD_LINEAR >> 32,
				       DRM_FORMAT_MOD_LINEAR & 0xffffffff);
	zwp_linux_buffer_params_v1_create(params, sc->width, sc->height, sc->drm_format, 0);
	while (!res.done) {
		if (wl_display_dispatch_queue(sc->display, sc->queue) < 0) {
			lost(sc);
			break;
		}
	}
	zwp_linux_buffer_params_v1_destroy(params);
	hv_log("wl_buffer %p for fd %d, %ux%u stride %u format %.4s", (void *)res.buffer, fd, sc->width, sc->height,
	       sc->stride, (const char *)&sc->drm_format);
	if (res.buffer)
		wl_buffer_add_listener(res.buffer, &buffer_listener, img);
	return res.buffer;
}

static VkResult create_image(struct swapchain *sc, const VkImageCreateInfo *info, struct sc_image *img)
{
	struct hv_device *dev = sc->dev;
	VkResult r = dev->CreateImage(dev->handle, info, NULL, &img->image);
	if (r != VK_SUCCESS)
		return r;
	VkMemoryRequirements req;
	dev->GetImageMemoryRequirements(dev->handle, img->image, &req);
	size_t page = (size_t)sysconf(_SC_PAGESIZE);
	size_t size = (size_t)sc->stride * sc->height;
	if (req.size > size)
		size = req.size;
	size = (size + page - 1) / page * page;

	int fd = alloc_dma_buf(size);
	if (fd < 0)
		return VK_ERROR_OUT_OF_DEVICE_MEMORY;
	VkMemoryFdPropertiesKHR props = {.sType = VK_STRUCTURE_TYPE_MEMORY_FD_PROPERTIES_KHR};
	r = dev->GetMemoryFdPropertiesKHR(dev->handle, VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT, fd, &props);
	uint32_t types = props.memoryTypeBits & req.memoryTypeBits;
	if (r == VK_SUCCESS && !types)
		r = VK_ERROR_OUT_OF_DEVICE_MEMORY;
	if (r == VK_SUCCESS) {
		/* The driver owns the fd it imports, on success only */
		int import = fcntl(fd, F_DUPFD_CLOEXEC, 0);
		VkMemoryDedicatedAllocateInfo dedicated = {
			.sType = VK_STRUCTURE_TYPE_MEMORY_DEDICATED_ALLOCATE_INFO,
			.image = img->image,
		};
		VkImportMemoryFdInfoKHR import_info = {
			.sType = VK_STRUCTURE_TYPE_IMPORT_MEMORY_FD_INFO_KHR,
			.pNext = &dedicated,
			.handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT,
			.fd = import,
		};
		VkMemoryAllocateInfo alloc = {
			.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
			.pNext = &import_info,
			.allocationSize = size,
			.memoryTypeIndex = (uint32_t)__builtin_ctz(types),
		};
		r = import < 0 ? VK_ERROR_OUT_OF_HOST_MEMORY : dev->AllocateMemory(dev->handle, &alloc, NULL, &img->memory);
		if (r != VK_SUCCESS && import >= 0)
			close(import);
	}
	if (r == VK_SUCCESS)
		r = dev->BindImageMemory(dev->handle, img->image, img->memory, 0);
	if (r == VK_SUCCESS) {
		img->buffer = create_buffer(sc, img, fd);
		if (!img->buffer)
			r = sc->status != VK_SUCCESS ? sc->status : VK_ERROR_INITIALIZATION_FAILED;
	}
	close(fd);
	img->state = IMAGE_FREE;
	return r;
}

static VkResult create_images(struct swapchain *sc, const VkSwapchainCreateInfoKHR *info)
{
	struct hv_device *dev = sc->dev;
	uint64_t linear = DRM_FORMAT_MOD_LINEAR;
	VkImageDrmFormatModifierListCreateInfoEXT list = {
		.sType = VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_LIST_CREATE_INFO_EXT,
		.drmFormatModifierCount = 1,
		.pDrmFormatModifiers = &linear,
	};
	VkExternalMemoryImageCreateInfo external = {
		.sType = VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO,
		.pNext = &list,
		.handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT,
	};
	VkImageCreateInfo ci = {
		.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,
		.pNext = &external,
		.imageType = VK_IMAGE_TYPE_2D,
		.format = info->imageFormat,
		.extent = {sc->width, sc->height, 1},
		.mipLevels = 1,
		.arrayLayers = 1,
		.samples = VK_SAMPLE_COUNT_1_BIT,
		.tiling = VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT,
		.usage = info->imageUsage,
		.sharingMode = info->imageSharingMode,
		.queueFamilyIndexCount = info->queueFamilyIndexCount,
		.pQueueFamilyIndices = info->pQueueFamilyIndices,
		.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED,
	};

	/* The driver's row pitch for this size: from an image it lays out itself */
	VkImage probe;
	VkResult r = dev->CreateImage(dev->handle, &ci, NULL, &probe);
	if (r != VK_SUCCESS)
		return r;
	VkImageSubresource plane0 = {.aspectMask = VK_IMAGE_ASPECT_MEMORY_PLANE_0_BIT_EXT};
	VkSubresourceLayout layout = {0};
	dev->GetImageSubresourceLayout(dev->handle, probe, &plane0, &layout);
	dev->DestroyImage(dev->handle, probe, NULL);
	if (layout.rowPitch < (uint64_t)sc->width * 4 || layout.rowPitch > UINT32_MAX || layout.offset)
		return VK_ERROR_INITIALIZATION_FAILED;
	sc->stride = (uint32_t)layout.rowPitch;

	/* The images themselves with that layout spelled out: memory imported into them must be
	 * laid out as the compositor is told */
	VkSubresourceLayout plane = {.rowPitch = sc->stride};
	VkImageDrmFormatModifierExplicitCreateInfoEXT explicit_layout = {
		.sType = VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_EXPLICIT_CREATE_INFO_EXT,
		.drmFormatModifier = DRM_FORMAT_MOD_LINEAR,
		.drmFormatModifierPlaneCount = 1,
		.pPlaneLayouts = &plane,
	};
	external.pNext = &explicit_layout;
	for (uint32_t i = 0; r == VK_SUCCESS && i < sc->count; i++)
		r = create_image(sc, &ci, &sc->images[i]);
	return r;
}

/* Every Wayland object on the swapchain's queue, then the queue */
static void swapchain_free(struct swapchain *sc)
{
	struct hv_device *dev = sc->dev;
	for (uint32_t i = 0; i < sc->count; i++) {
		struct sc_image *img = &sc->images[i];
		if (img->buffer)
			wl_buffer_destroy(img->buffer);
		if (img->image)
			dev->DestroyImage(dev->handle, img->image, NULL);
		if (img->memory)
			dev->FreeMemory(dev->handle, img->memory, NULL);
	}
	if (sc->fence)
		dev->DestroyFence(dev->handle, sc->fence, NULL);
	if (sc->frame)
		wl_callback_destroy(sc->frame);
	if (sc->dmabuf)
		zwp_linux_dmabuf_v1_destroy(sc->dmabuf);
	if (sc->registry)
		wl_registry_destroy(sc->registry);
	if (sc->surface)
		wl_proxy_wrapper_destroy(sc->surface);
	if (sc->display_wrapper)
		wl_proxy_wrapper_destroy(sc->display_wrapper);
	if (sc->queue) {
		wl_display_flush(sc->display);
		wl_event_queue_destroy(sc->queue);
	}
	free(sc);
}

static VkResult connect_compositor(struct swapchain *sc, struct wl_surface *surface)
{
	sc->queue = wl_display_create_queue(sc->display);
	sc->display_wrapper = wl_proxy_create_wrapper(sc->display);
	sc->surface = wl_proxy_create_wrapper(surface);
	if (!sc->queue || !sc->display_wrapper || !sc->surface)
		return VK_ERROR_OUT_OF_HOST_MEMORY;
	wl_proxy_set_queue((struct wl_proxy *)sc->display_wrapper, sc->queue);
	wl_proxy_set_queue((struct wl_proxy *)sc->surface, sc->queue);
	sc->registry = wl_display_get_registry(sc->display_wrapper);
	wl_registry_add_listener(sc->registry, &registry_listener, sc);
	if (wl_display_roundtrip_queue(sc->display, sc->queue) < 0)
		return lost(sc);
	/* A compositor without linux-dmabuf cannot show these images */
	return sc->dmabuf ? VK_SUCCESS : VK_ERROR_INITIALIZATION_FAILED;
}

static uint32_t drm_format(VkFormat format, bool alpha)
{
	for (size_t i = 0; i < ARRAY_SIZE(formats); i++)
		if (formats[i].format == format)
			return alpha ? formats[i].alpha : formats[i].opaque;
	return 0;
}

static VKAPI_ATTR VkResult VKAPI_CALL create_swapchain(VkDevice device, const VkSwapchainCreateInfoKHR *info,
						      const VkAllocationCallbacks *allocator, VkSwapchainKHR *out)
{
	struct hv_device *dev = hv_device_find(device);
	VkIcdSurfaceWayland *surface = wayland_surface(info->surface);
	if (!dev || !dev->swapchain)
		return VK_ERROR_INITIALIZATION_FAILED;
	if (!surface)
		return VK_ERROR_SURFACE_LOST_KHR;
	uint32_t format = drm_format(info->imageFormat, info->compositeAlpha == VK_COMPOSITE_ALPHA_PRE_MULTIPLIED_BIT_KHR);
	if (!format || info->imageArrayLayers != 1 || !info->imageExtent.width || !info->imageExtent.height ||
	    info->imageExtent.width > MAX_EXTENT || info->imageExtent.height > MAX_EXTENT)
		return VK_ERROR_INITIALIZATION_FAILED;
	uint32_t count = info->minImageCount < MIN_IMAGES   ? MIN_IMAGES
			 : info->minImageCount > MAX_IMAGES ? MAX_IMAGES
							    : info->minImageCount;
	struct swapchain *sc = calloc(1, sizeof *sc + count * sizeof *sc->images);
	if (!sc)
		return VK_ERROR_OUT_OF_HOST_MEMORY;
	sc->dev = dev;
	sc->display = surface->display;
	/* IMMEDIATE and FIFO_RELAXED are not offered; what comes closest */
	sc->mode = info->presentMode == VK_PRESENT_MODE_MAILBOX_KHR || info->presentMode == VK_PRESENT_MODE_IMMEDIATE_KHR
			   ? VK_PRESENT_MODE_MAILBOX_KHR
			   : VK_PRESENT_MODE_FIFO_KHR;
	sc->width = info->imageExtent.width;
	sc->height = info->imageExtent.height;
	sc->drm_format = format;
	sc->count = count;

	VkResult r = connect_compositor(sc, surface->surface);
	if (r == VK_SUCCESS) {
		VkFenceCreateInfo fci = {.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
		r = dev->CreateFence(dev->handle, &fci, NULL, &sc->fence);
	}
	if (r == VK_SUCCESS)
		r = create_images(sc, info);
	if (r != VK_SUCCESS) {
		hv_log("no swapchain: %d", r);
		swapchain_free(sc);
		return r;
	}
	if (info->oldSwapchain)
		((struct swapchain *)(uintptr_t)info->oldSwapchain)->retired = true;
	hv_log("swapchain %p: %u images %ux%u, %s", (void *)sc, sc->count, sc->width, sc->height,
	       sc->mode == VK_PRESENT_MODE_FIFO_KHR ? "FIFO" : "MAILBOX");
	*out = (VkSwapchainKHR)(uintptr_t)sc;
	return VK_SUCCESS;
}

static VKAPI_ATTR void VKAPI_CALL destroy_swapchain(VkDevice device, VkSwapchainKHR swapchain,
						   const VkAllocationCallbacks *allocator)
{
	if (swapchain)
		swapchain_free((struct swapchain *)(uintptr_t)swapchain);
}

static VKAPI_ATTR VkResult VKAPI_CALL get_swapchain_images(VkDevice device, VkSwapchainKHR swapchain,
							  uint32_t *count, VkImage *out)
{
	struct swapchain *sc = (struct swapchain *)(uintptr_t)swapchain;
	VkImage list[MAX_IMAGES];
	for (uint32_t i = 0; i < sc->count; i++)
		list[i] = sc->images[i].image;
	return answer(out, list, sizeof *list, sc->count, count);
}

/* An acquired image's semaphore and fence, signalled at once: the image is free already */
static VkResult signal_acquired(struct hv_device *dev, VkSemaphore semaphore, VkFence fence)
{
	if (dev->sync_fd_import) {
		/* fd -1: a sync file that has already signalled */
		if (semaphore) {
			VkImportSemaphoreFdInfoKHR info = {
				.sType = VK_STRUCTURE_TYPE_IMPORT_SEMAPHORE_FD_INFO_KHR,
				.semaphore = semaphore,
				.flags = VK_SEMAPHORE_IMPORT_TEMPORARY_BIT,
				.handleType = VK_EXTERNAL_SEMAPHORE_HANDLE_TYPE_SYNC_FD_BIT,
				.fd = -1,
			};
			if (dev->ImportSemaphoreFdKHR(dev->handle, &info) == VK_SUCCESS)
				semaphore = VK_NULL_HANDLE;
		}
		if (fence) {
			VkImportFenceFdInfoKHR info = {
				.sType = VK_STRUCTURE_TYPE_IMPORT_FENCE_FD_INFO_KHR,
				.fence = fence,
				.flags = VK_FENCE_IMPORT_TEMPORARY_BIT,
				.handleType = VK_EXTERNAL_FENCE_HANDLE_TYPE_SYNC_FD_BIT,
				.fd = -1,
			};
			if (dev->ImportFenceFdKHR(dev->handle, &info) == VK_SUCCESS)
				fence = VK_NULL_HANDLE;
		}
	}
	if (!semaphore && !fence)
		return VK_SUCCESS;
	/* What is left: an empty submission signals it */
	if (!dev->queue)
		return VK_ERROR_DEVICE_LOST;
	VkSubmitInfo submit = {
		.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO,
		.signalSemaphoreCount = semaphore ? 1 : 0,
		.pSignalSemaphores = &semaphore,
	};
	pthread_mutex_lock(&dev->queue_lock);
	VkResult r = dev->QueueSubmit(dev->queue, 1, &submit, fence);
	pthread_mutex_unlock(&dev->queue_lock);
	return r;
}

static VkResult acquire(struct swapchain *sc, uint64_t timeout, VkSemaphore semaphore, VkFence fence, uint32_t *index)
{
	if (sc->retired)
		return VK_ERROR_OUT_OF_DATE_KHR;
	if (sc->status != VK_SUCCESS)
		return sc->status;
	int64_t now = now_ns();
	int64_t deadline = timeout >= (uint64_t)(INT64_MAX - now) ? INT64_MAX : now + (int64_t)timeout;
	for (;;) {
		if (wl_display_dispatch_queue_pending(sc->display, sc->queue) < 0)
			return lost(sc);
		for (uint32_t i = 0; i < sc->count; i++) {
			if (sc->images[i].state != IMAGE_FREE)
				continue;
			VkResult r = signal_acquired(sc->dev, semaphore, fence);
			if (r != VK_SUCCESS)
				return r;
			sc->images[i].state = IMAGE_ACQUIRED;
			*index = i;
			hv_log("acquired %u", i);
			return VK_SUCCESS;
		}
		if (timeout == 0)
			return VK_NOT_READY;
		hv_log("acquire waits for a release");
		int r = dispatch_until(sc, deadline);
		if (r < 0)
			return lost(sc);
		if (r == 0 && now_ns() >= deadline)
			return VK_TIMEOUT;
	}
}

static VKAPI_ATTR VkResult VKAPI_CALL acquire_next_image(VkDevice device, VkSwapchainKHR swapchain, uint64_t timeout,
							VkSemaphore semaphore, VkFence fence, uint32_t *index)
{
	return acquire((struct swapchain *)(uintptr_t)swapchain, timeout, semaphore, fence, index);
}

static VKAPI_ATTR VkResult VKAPI_CALL acquire_next_image2(VkDevice device, const VkAcquireNextImageInfoKHR *info,
							 uint32_t *index)
{
	return acquire((struct swapchain *)(uintptr_t)info->swapchain, info->timeout, info->semaphore, info->fence,
		       index);
}

/* Hand one finished image to the compositor */
static VkResult present_image(struct swapchain *sc, uint32_t index)
{
	if (sc->status != VK_SUCCESS)
		return sc->status;
	if (index >= sc->count || sc->images[index].state != IMAGE_ACQUIRED)
		return VK_ERROR_OUT_OF_DATE_KHR;
	struct sc_image *img = &sc->images[index];

	if (sc->mode == VK_PRESENT_MODE_FIFO_KHR) {
		int64_t deadline = now_ns() + FRAME_WAIT_NS;
		while (sc->frame) {
			int r = dispatch_until(sc, deadline);
			if (r < 0)
				return lost(sc);
			if (r == 0 && now_ns() >= deadline)
				break;
		}
		/* No callback in time (hidden surface): this frame goes without */
		if (sc->frame)
			wl_callback_destroy(sc->frame);
		sc->frame = wl_surface_frame(sc->surface);
		wl_callback_add_listener(sc->frame, &frame_listener, sc);
	}
	wl_surface_attach(sc->surface, img->buffer, 0, 0);
	if (wl_proxy_get_version((struct wl_proxy *)sc->surface) >= WL_SURFACE_DAMAGE_BUFFER_SINCE_VERSION)
		wl_surface_damage_buffer(sc->surface, 0, 0, INT32_MAX, INT32_MAX);
	else
		wl_surface_damage(sc->surface, 0, 0, INT32_MAX, INT32_MAX);
	wl_surface_commit(sc->surface);
	img->state = IMAGE_PRESENTED;
	hv_log("presented %u", index);
	if (wl_display_flush(sc->display) < 0 && errno != EAGAIN)
		return lost(sc);
	return VK_SUCCESS;
}

static VKAPI_ATTR VkResult VKAPI_CALL queue_present(VkQueue queue, const VkPresentInfoKHR *info)
{
	if (!info->swapchainCount)
		return VK_SUCCESS;
	struct swapchain *first = (struct swapchain *)(uintptr_t)info->pSwapchains[0];
	struct hv_device *dev = first->dev;

	/* The GPU has to be done before the compositor reads: everything submitted to this queue
	 * so far (an empty submission signals its fence only after all of it) and the
	 * application's semaphores */
	VkPipelineStageFlags *stages = NULL;
	if (info->waitSemaphoreCount) {
		stages = malloc(info->waitSemaphoreCount * sizeof *stages);
		if (!stages)
			return VK_ERROR_OUT_OF_HOST_MEMORY;
		for (uint32_t i = 0; i < info->waitSemaphoreCount; i++)
			stages[i] = VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;
	}
	VkSubmitInfo submit = {
		.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO,
		.waitSemaphoreCount = info->waitSemaphoreCount,
		.pWaitSemaphores = info->pWaitSemaphores,
		.pWaitDstStageMask = stages,
	};
	VkResult r = dev->QueueSubmit(queue, 1, &submit, first->fence);
	free(stages);
	if (r == VK_SUCCESS) {
		r = dev->WaitForFences(dev->handle, 1, &first->fence, VK_TRUE, RENDER_WAIT_NS);
		if (r == VK_TIMEOUT)
			r = VK_ERROR_DEVICE_LOST;
		dev->ResetFences(dev->handle, 1, &first->fence);
	}

	VkResult result = r;
	for (uint32_t i = 0; i < info->swapchainCount; i++) {
		struct swapchain *sc = (struct swapchain *)(uintptr_t)info->pSwapchains[i];
		VkResult ri = r == VK_SUCCESS ? present_image(sc, info->pImageIndices[i]) : r;
		if (info->pResults)
			info->pResults[i] = ri;
		if (result == VK_SUCCESS)
			result = ri;
	}
	return result;
}

/* Function tables */

struct named_function {
	const char *name;
	PFN_vkVoidFunction function;
};

static const struct named_function instance_functions[] = {
	{"vkCreateWaylandSurfaceKHR", (PFN_vkVoidFunction)create_wayland_surface},
	{"vkDestroySurfaceKHR", (PFN_vkVoidFunction)destroy_surface},
	{"vkGetPhysicalDeviceSurfaceSupportKHR", (PFN_vkVoidFunction)surface_support},
	{"vkGetPhysicalDeviceSurfaceCapabilitiesKHR", (PFN_vkVoidFunction)surface_capabilities},
	{"vkGetPhysicalDeviceSurfaceFormatsKHR", (PFN_vkVoidFunction)surface_formats},
	{"vkGetPhysicalDeviceSurfacePresentModesKHR", (PFN_vkVoidFunction)surface_present_modes},
	{"vkGetPhysicalDeviceWaylandPresentationSupportKHR", (PFN_vkVoidFunction)wayland_presentation_support},
};

static const struct named_function device_functions[] = {
	{"vkCreateSwapchainKHR", (PFN_vkVoidFunction)create_swapchain},
	{"vkDestroySwapchainKHR", (PFN_vkVoidFunction)destroy_swapchain},
	{"vkGetSwapchainImagesKHR", (PFN_vkVoidFunction)get_swapchain_images},
	{"vkAcquireNextImageKHR", (PFN_vkVoidFunction)acquire_next_image},
	{"vkAcquireNextImage2KHR", (PFN_vkVoidFunction)acquire_next_image2},
	{"vkQueuePresentKHR", (PFN_vkVoidFunction)queue_present},
	{"vkGetDeviceGroupPresentCapabilitiesKHR", (PFN_vkVoidFunction)device_group_present_capabilities},
	{"vkGetDeviceGroupSurfacePresentModesKHR", (PFN_vkVoidFunction)device_group_surface_present_modes},
};

static PFN_vkVoidFunction lookup(const struct named_function *table, size_t n, const char *name)
{
	for (size_t i = 0; i < n; i++)
		if (!strcmp(table[i].name, name))
			return table[i].function;
	return NULL;
}

PFN_vkVoidFunction hv_wsi_instance_function(const char *name)
{
	return lookup(instance_functions, ARRAY_SIZE(instance_functions), name);
}

PFN_vkVoidFunction hv_wsi_device_function(const char *name)
{
	return lookup(device_functions, ARRAY_SIZE(device_functions), name);
}
