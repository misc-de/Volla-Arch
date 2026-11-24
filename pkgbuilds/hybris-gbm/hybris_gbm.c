// hybris_gbm: GBM backend for Mesa's libgbm on Halium devices, buffers from Android's gralloc
// through libhybris. For compositors that render with libhybris' EGL and show their buffers
// through real KMS (wlroots on the Plinius, docs/GRAFIK.md):
// - every buffer is linear: gralloc's compressed layout (AFBC on Mali) shows as garbage when KMS
//   or another EGL context reads it, CPU access flags keep gralloc from choosing it;
// - its dma-buf is the fd of the gralloc handle that the kernel exports as a dma-buf (on the
//   Plinius the second of three);
// - hybris_gbm_native_buffer() maps a dma-buf back to its native buffer, so libhybris' EGL can
//   import it as an EGL image (patched libhybris, EGL_LINUX_DMA_BUF_EXT).
// Mesa picks a backend by the DRM driver's name, e.g. /usr/lib/gbm/mediatek_gbm.so, or by
// GBM_BACKEND=hybris.
// Based on libgbm-hybris by D0gg0Man and Bardia Moshiri (FuriLabs).
// SPDX-License-Identifier: BSD-2-Clause
#include <EGL/egl.h>
#include <gbm_backend_abi.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

// libhybris (libgralloc, libhybris-eglplatformcommon). Called directly: through glvnd,
// eglGetProcAddress has no vendor to dispatch these to, as they take no EGLDisplay.
void hybris_gralloc_initialize(int framebuffer);
EGLBoolean eglplatformcommon_eglHybrisCreateNativeBuffer(EGLint width, EGLint height, EGLint usage,
							 EGLint format, EGLint *stride, EGLClientBuffer *buffer);
EGLBoolean eglplatformcommon_eglHybrisReleaseNativeBuffer(EGLClientBuffer buffer);
EGLBoolean eglplatformcommon_eglHybrisLockNativeBuffer(EGLClientBuffer buffer, EGLint usage, EGLint l,
						       EGLint t, EGLint w, EGLint h, void **vaddr);
EGLBoolean eglplatformcommon_eglHybrisUnlockNativeBuffer(EGLClientBuffer buffer);
EGLBoolean eglplatformcommon_eglHybrisGetNativeBufferInfo(EGLClientBuffer buffer, int *num_ints,
							  int *num_fds);
EGLBoolean eglplatformcommon_eglHybrisSerializeNativeBuffer(EGLClientBuffer buffer, int *ints, int *fds);

// Android pixel formats and gralloc usage bits
#define HAL_RGBA_8888 1
#define HAL_RGBX_8888 2
#define HAL_RGB_565 4
#define HAL_BGRA_8888 5
#define USAGE_SW_READ_RARELY 0x2
#define USAGE_SW_WRITE_RARELY 0x20
#define USAGE_HW_TEXTURE 0x100
#define USAGE_HW_RENDER 0x200
#define USAGE_HW_COMPOSER 0x800

#define LOG(...) do { if (getenv("HYBRIS_GBM_DEBUG")) fprintf(stderr, "hybris_gbm: " __VA_ARGS__); } while (0)

struct hybris_bo {
	struct gbm_bo base;
	EGLClientBuffer buffer;
	int fd;         // the dma-buf, owned by this bo
	ino_t ino;      // identifies the dma-buf across dup()s and processes
	uint32_t stride; // bytes
	struct hybris_bo *next;
};

struct hybris_surface {
	struct gbm_surface base;
	struct hybris_bo *front, *back;
};

// All live bos, for hybris_gbm_native_buffer
static pthread_mutex_t bos_lock = PTHREAD_MUTEX_INITIALIZER;
static struct hybris_bo *bos;
static pthread_once_t gralloc_once = PTHREAD_ONCE_INIT;

static void init_gralloc(void)
{
	hybris_gralloc_initialize(0);
}

__attribute__((visibility("default"))) EGLClientBuffer hybris_gbm_native_buffer(int fd)
{
	struct stat st;
	EGLClientBuffer found = NULL;
	if (fstat(fd, &st) < 0)
		return NULL;
	pthread_mutex_lock(&bos_lock);
	for (struct hybris_bo *b = bos; b; b = b->next)
		if (b->ino == st.st_ino) {
			found = b->buffer;
			break;
		}
	pthread_mutex_unlock(&bos_lock);
	return found;
}

static int hal_format(uint32_t format)
{
	switch (format) {
	case GBM_FORMAT_ARGB8888:
	case GBM_FORMAT_XRGB8888:
		return HAL_BGRA_8888;
	case GBM_FORMAT_ABGR8888:
		return HAL_RGBA_8888;
	case GBM_FORMAT_XBGR8888:
		return HAL_RGBX_8888;
	case GBM_FORMAT_RGB565:
		return HAL_RGB_565;
	default:
		return -1;
	}
}

// dmabuf_fd: the fd among a gralloc handle's that the kernel exports as a dma-buf of at least
// min_size bytes (fdinfo has exp_name only for dma-bufs)
static int dmabuf_fd(const int *fds, int count, long long min_size)
{
	for (int i = 0; i < count; i++) {
		char path[64], line[128];
		snprintf(path, sizeof(path), "/proc/self/fdinfo/%d", fds[i]);
		FILE *f = fopen(path, "r");
		if (!f)
			continue;
		int is_dmabuf = 0;
		long long size = -1;
		while (fgets(line, sizeof(line), f)) {
			if (strncmp(line, "exp_name:", 9) == 0)
				is_dmabuf = 1;
			sscanf(line, "size: %lld", &size);
		}
		fclose(f);
		if (is_dmabuf && size >= min_size)
			return fds[i];
	}
	return -1;
}

static struct hybris_bo *alloc_bo(struct gbm_device *gbm, uint32_t width, uint32_t height, uint32_t format)
{
	int hal = hal_format(format);
	if (hal < 0) {
		LOG("format 0x%x not supported\n", format);
		return NULL;
	}
	pthread_once(&gralloc_once, init_gralloc);
	struct hybris_bo *bo = calloc(1, sizeof(*bo));
	if (!bo)
		return NULL;
	bo->fd = -1;
	int bpp = format == GBM_FORMAT_RGB565 ? 2 : 4;
	EGLint stride = 0;
	int usage = USAGE_HW_RENDER | USAGE_HW_TEXTURE | USAGE_HW_COMPOSER | USAGE_SW_READ_RARELY |
		    USAGE_SW_WRITE_RARELY;
	if (!eglplatformcommon_eglHybrisCreateNativeBuffer(width, height, usage, hal, &stride, &bo->buffer)) {
		LOG("gralloc allocation %ux%u failed\n", width, height);
		free(bo);
		return NULL;
	}
	int num_ints = 0, num_fds = 0, ints[128], fds[8];
	if (!eglplatformcommon_eglHybrisGetNativeBufferInfo(bo->buffer, &num_ints, &num_fds) ||
	    num_ints > 128 || num_fds > 8 ||
	    !eglplatformcommon_eglHybrisSerializeNativeBuffer(bo->buffer, ints, fds))
		goto fail;
	int fd = dmabuf_fd(fds, num_fds, (long long)stride * height * bpp);
	struct stat st;
	if (fd < 0 || (bo->fd = dup(fd)) < 0 || fstat(bo->fd, &st) < 0) {
		LOG("no dma-buf in the gralloc handle (%d fds)\n", num_fds);
		goto fail;
	}
	bo->ino = st.st_ino;
	bo->stride = stride * bpp;
	bo->base.gbm = gbm;
	bo->base.v0.width = width;
	bo->base.v0.height = height;
	bo->base.v0.stride = bo->stride;
	bo->base.v0.format = format;
	bo->base.v0.handle.s32 = bo->fd;
	pthread_mutex_lock(&bos_lock);
	bo->next = bos;
	bos = bo;
	pthread_mutex_unlock(&bos_lock);
	LOG("bo %ux%u format 0x%x stride %u fd %d\n", width, height, format, bo->stride, bo->fd);
	return bo;
fail:
	if (bo->fd >= 0)
		close(bo->fd);
	eglplatformcommon_eglHybrisReleaseNativeBuffer(bo->buffer);
	free(bo);
	return NULL;
}

static void free_bo(struct hybris_bo *bo)
{
	if (!bo)
		return;
	pthread_mutex_lock(&bos_lock);
	for (struct hybris_bo **p = &bos; *p; p = &(*p)->next)
		if (*p == bo) {
			*p = bo->next;
			break;
		}
	pthread_mutex_unlock(&bos_lock);
	close(bo->fd);
	eglplatformcommon_eglHybrisReleaseNativeBuffer(bo->buffer);
	free(bo);
}

static void device_destroy(struct gbm_device *gbm)
{
	free(gbm);
}

static int is_format_supported(struct gbm_device *gbm, uint32_t format, uint32_t usage)
{
	return hal_format(format) >= 0;
}

static int get_format_modifier_plane_count(struct gbm_device *gbm, uint32_t format, uint64_t modifier)
{
	return 1;
}

// Modifiers are ignored: every buffer is linear
static struct gbm_bo *bo_create(struct gbm_device *gbm, uint32_t width, uint32_t height, uint32_t format,
				uint32_t usage, const uint64_t *modifiers, const unsigned int count)
{
	struct hybris_bo *bo = alloc_bo(gbm, width, height, format);
	return bo ? &bo->base : NULL;
}

static struct gbm_bo *bo_import(struct gbm_device *gbm, uint32_t type, void *buffer, uint32_t usage)
{
	return NULL;
}

static void *bo_map(struct gbm_bo *gbm_bo, uint32_t x, uint32_t y, uint32_t width, uint32_t height,
		    uint32_t flags, uint32_t *stride, void **map_data)
{
	struct hybris_bo *bo = (struct hybris_bo *)gbm_bo;
	void *addr = NULL;
	if (!eglplatformcommon_eglHybrisLockNativeBuffer(bo->buffer, USAGE_SW_READ_RARELY | USAGE_SW_WRITE_RARELY,
							 x, y, width, height, &addr))
		return NULL;
	*stride = bo->stride;
	*map_data = bo;
	return addr;
}

static void bo_unmap(struct gbm_bo *gbm_bo, void *map_data)
{
	eglplatformcommon_eglHybrisUnlockNativeBuffer(((struct hybris_bo *)gbm_bo)->buffer);
}

static int bo_write(struct gbm_bo *bo, const void *buf, size_t count)
{
	return -1;
}

static int bo_get_fd(struct gbm_bo *bo)
{
	return dup(((struct hybris_bo *)bo)->fd);
}

static int bo_get_planes(struct gbm_bo *bo)
{
	return 1;
}

static union gbm_bo_handle bo_get_handle(struct gbm_bo *bo, int plane)
{
	return bo->v0.handle;
}

static int bo_get_plane_fd(struct gbm_bo *bo, int plane)
{
	return dup(((struct hybris_bo *)bo)->fd);
}

static uint32_t bo_get_stride(struct gbm_bo *bo, int plane)
{
	return ((struct hybris_bo *)bo)->stride;
}

static uint32_t bo_get_offset(struct gbm_bo *bo, int plane)
{
	return 0;
}

static uint64_t bo_get_modifier(struct gbm_bo *bo)
{
	return 0; // DRM_FORMAT_MOD_LINEAR
}

static void bo_destroy(struct gbm_bo *bo)
{
	free_bo((struct hybris_bo *)bo);
}

// gbm surfaces (two buffers, swapped on lock); wlroots does not use them, other clients may
static struct gbm_surface *surface_create(struct gbm_device *gbm, uint32_t width, uint32_t height,
					  uint32_t format, uint32_t flags, const uint64_t *modifiers,
					  const unsigned int count)
{
	struct hybris_surface *surface = calloc(1, sizeof(*surface));
	if (!surface)
		return NULL;
	surface->base.gbm = gbm;
	surface->base.v0.width = width;
	surface->base.v0.height = height;
	surface->base.v0.format = format;
	surface->base.v0.flags = flags;
	surface->back = alloc_bo(gbm, width, height, format);
	if (!surface->back) {
		free(surface);
		return NULL;
	}
	return &surface->base;
}

static struct gbm_bo *surface_lock_front_buffer(struct gbm_surface *gbm_surface)
{
	struct hybris_surface *surface = (struct hybris_surface *)gbm_surface;
	struct hybris_bo *previous = surface->front;
	surface->front = surface->back;
	surface->back = previous;
	if (!surface->back)
		surface->back = alloc_bo(gbm_surface->gbm, gbm_surface->v0.width, gbm_surface->v0.height,
					 gbm_surface->v0.format);
	return surface->front ? &surface->front->base : NULL;
}

static void surface_release_buffer(struct gbm_surface *surface, struct gbm_bo *bo)
{
}

static int surface_has_free_buffers(struct gbm_surface *surface)
{
	return 1;
}

static void surface_destroy(struct gbm_surface *gbm_surface)
{
	struct hybris_surface *surface = (struct hybris_surface *)gbm_surface;
	free_bo(surface->front);
	free_bo(surface->back);
	free(surface);
}

static struct gbm_device *create_device(int fd, uint32_t version)
{
	struct gbm_device *gbm = calloc(1, sizeof(*gbm));
	if (!gbm)
		return NULL;
	gbm->v0.backend_version = version;
	gbm->v0.fd = fd;
	gbm->v0.name = "hybris";
	gbm->v0.destroy = device_destroy;
	gbm->v0.is_format_supported = is_format_supported;
	gbm->v0.get_format_modifier_plane_count = get_format_modifier_plane_count;
	gbm->v0.bo_create = bo_create;
	gbm->v0.bo_import = bo_import;
	gbm->v0.bo_map = bo_map;
	gbm->v0.bo_unmap = bo_unmap;
	gbm->v0.bo_write = bo_write;
	gbm->v0.bo_get_fd = bo_get_fd;
	gbm->v0.bo_get_planes = bo_get_planes;
	gbm->v0.bo_get_handle = bo_get_handle;
	gbm->v0.bo_get_plane_fd = bo_get_plane_fd;
	gbm->v0.bo_get_stride = bo_get_stride;
	gbm->v0.bo_get_offset = bo_get_offset;
	gbm->v0.bo_get_modifier = bo_get_modifier;
	gbm->v0.bo_destroy = bo_destroy;
	gbm->v0.surface_create = surface_create;
	gbm->v0.surface_lock_front_buffer = surface_lock_front_buffer;
	gbm->v0.surface_release_buffer = surface_release_buffer;
	gbm->v0.surface_has_free_buffers = surface_has_free_buffers;
	gbm->v0.surface_destroy = surface_destroy;
	LOG("device on fd %d\n", fd);
	return gbm;
}

static const struct gbm_backend backend = {
	.v0 = {
		.backend_version = GBM_BACKEND_ABI_VERSION,
		.backend_name = "hybris",
		.create_device = create_device,
	},
};

__attribute__((visibility("default"))) const struct gbm_backend *gbmint_get_backend(const struct gbm_core *core)
{
	return &backend;
}
