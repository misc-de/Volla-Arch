# hybris-vulkan, the Wayland WSI (wsi_wayland.c): surface queries, swapchain creation on
# imported dma-bufs with an explicit LINEAR layout, wl_buffers through linux-dmabuf, acquire and
# present against the compositor's releases and frame callbacks, the CPU wait for the GPU before
# a commit, and what happens when the driver, the heap or the compositor fail. The compositor is
# a real libwayland-server process (c/hv_fake_compositor.c) on a socket pair; the driver's device
# functions and the DMA heap are fakes in the harness.

import os
import re

import pytest

from conftest import compile_c
from dhw_harness import HERE, run
from hybris_vulkan_harness import build, generate_protocol, needs_vulkan

pytestmark = needs_vulkan

HARNESS = r"""
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>
#include <linux/dma-heap.h>

static int heap_fail, calloc_fail_at, callocs, malloc_fail;
/* The DMA heap: a memfd of the requested size */
static int test_ioctl(int fd, unsigned long req, void *arg)
{
	if (req != DMA_HEAP_IOCTL_ALLOC || heap_fail) {
		errno = ENOMEM;
		return -1;
	}
	struct dma_heap_allocation_data *d = arg;
	int m = memfd_create("heap", MFD_CLOEXEC);
	if (m < 0 || ftruncate(m, d->len) < 0)
		return -1;
	d->fd = m;
	return 0;
}
static void *test_calloc(size_t n, size_t s)
{
	return ++callocs == calloc_fail_at ? NULL : calloc(n, s);
}
static void *test_malloc(size_t s)
{
	return malloc_fail ? NULL : malloc(s);
}
#define ioctl test_ioctl
#define calloc test_calloc
#define malloc test_malloc
#include "hybris_vulkan.c"
#include "wsi_wayland.c"
#undef ioctl
#undef calloc
#undef malloc

/* The HAL is not used here: the device is built by hand below */
int hw_get_module(const char *id, const struct hw_module_t **m)
{
	return -ENOENT;
}

/* Scripted driver */
static int fail_create_image_at, n_create_images, destroyed_images, extra_pitch, layout_offset;
static int fdprops_fail, no_types, alloc_fail, bind_fail, fence_fail, submit_fail, wait_timeout;
static int import_sem_fail, import_fence_fail, freed_memory;
static uint32_t last_width;
static uint64_t next_handle = 0x100;

static VkResult VKAPI_CALL drv_create_image(VkDevice d, const VkImageCreateInfo *ci, const VkAllocationCallbacks *a, VkImage *out)
{
	n_create_images++;
	if (fail_create_image_at && n_create_images == fail_create_image_at)
		return VK_ERROR_OUT_OF_DEVICE_MEMORY;
	const VkBaseInStructure *p = ci->pNext;
	const char *kind = "none";
	uint32_t pitch = 0;
	for (; p; p = p->pNext) {
		if (p->sType == VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_LIST_CREATE_INFO_EXT)
			kind = "list";
		if (p->sType == VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_EXPLICIT_CREATE_INFO_EXT) {
			kind = "explicit";
			pitch = (uint32_t)((const VkImageDrmFormatModifierExplicitCreateInfoEXT *)p)->pPlaneLayouts[0].rowPitch;
		}
	}
	printf("image %s pitch=%u %ux%u format=%d tiling=%d usage=0x%x\n", kind, pitch, ci->extent.width, ci->extent.height,
	       ci->format, ci->tiling, ci->usage);
	last_width = ci->extent.width;
	*out = (VkImage)next_handle++;
	return VK_SUCCESS;
}
static void VKAPI_CALL drv_destroy_image(VkDevice d, VkImage i, const VkAllocationCallbacks *a)
{
	destroyed_images++;
}
static void VKAPI_CALL drv_layout(VkDevice d, VkImage i, const VkImageSubresource *s, VkSubresourceLayout *l)
{
	printf("layout aspect=0x%x\n", s->aspectMask);
	l->rowPitch = ((last_width * 4 + 63) & ~63u) + extra_pitch;
	l->offset = layout_offset;
}
static void VKAPI_CALL drv_memreq(VkDevice d, VkImage i, VkMemoryRequirements *r)
{
	r->size = 64;
	r->alignment = 64;
	r->memoryTypeBits = 0x3;
}
static VkResult VKAPI_CALL drv_fdprops(VkDevice d, VkExternalMemoryHandleTypeFlagBits t, int fd, VkMemoryFdPropertiesKHR *p)
{
	if (fdprops_fail)
		return VK_ERROR_INVALID_EXTERNAL_HANDLE;
	p->memoryTypeBits = no_types ? 0x4 : 0x2;
	return VK_SUCCESS;
}
static VkResult VKAPI_CALL drv_alloc(VkDevice d, const VkMemoryAllocateInfo *ai, const VkAllocationCallbacks *a, VkDeviceMemory *m)
{
	const VkImportMemoryFdInfoKHR *imp = ai->pNext;
	const VkMemoryDedicatedAllocateInfo *ded = imp->pNext;
	if (alloc_fail)
		return VK_ERROR_OUT_OF_DEVICE_MEMORY;
	printf("alloc size=%llu type=%u dedicated=%d fd-valid=%d\n", (unsigned long long)ai->allocationSize,
	       ai->memoryTypeIndex, ded->image != 0, fcntl(imp->fd, F_GETFD) >= 0);
	close(imp->fd); /* the driver owns it */
	*m = (VkDeviceMemory)next_handle++;
	return VK_SUCCESS;
}
static void VKAPI_CALL drv_free(VkDevice d, VkDeviceMemory m, const VkAllocationCallbacks *a)
{
	freed_memory++;
}
static VkResult VKAPI_CALL drv_bind(VkDevice d, VkImage i, VkDeviceMemory m, VkDeviceSize o)
{
	return bind_fail ? VK_ERROR_OUT_OF_DEVICE_MEMORY : VK_SUCCESS;
}
static VkResult VKAPI_CALL drv_create_fence(VkDevice d, const VkFenceCreateInfo *ci, const VkAllocationCallbacks *a, VkFence *f)
{
	if (fence_fail)
		return VK_ERROR_OUT_OF_HOST_MEMORY;
	*f = (VkFence)next_handle++;
	return VK_SUCCESS;
}
static void VKAPI_CALL drv_destroy_fence(VkDevice d, VkFence f, const VkAllocationCallbacks *a) {}
static VkResult VKAPI_CALL drv_wait(VkDevice d, uint32_t n, const VkFence *f, VkBool32 all, uint64_t t)
{
	printf("wait-fence timeout=%llu\n", (unsigned long long)t);
	return wait_timeout ? VK_TIMEOUT : VK_SUCCESS;
}
static VkResult VKAPI_CALL drv_reset(VkDevice d, uint32_t n, const VkFence *f)
{
	return VK_SUCCESS;
}
static VkResult VKAPI_CALL drv_submit(VkQueue q, uint32_t n, const VkSubmitInfo *s, VkFence f)
{
	printf("submit queue=%p waits=%u stage=0x%x signals=%u fence=%d\n", (void *)q, s->waitSemaphoreCount,
	       s->waitSemaphoreCount ? s->pWaitDstStageMask[0] : 0, s->signalSemaphoreCount, f != VK_NULL_HANDLE);
	return submit_fail ? VK_ERROR_DEVICE_LOST : VK_SUCCESS;
}
static VkResult VKAPI_CALL drv_import_sem(VkDevice d, const VkImportSemaphoreFdInfoKHR *i)
{
	printf("import-semaphore fd=%d temporary=%d sync-fd=%d\n", i->fd, !!(i->flags & VK_SEMAPHORE_IMPORT_TEMPORARY_BIT),
	       i->handleType == VK_EXTERNAL_SEMAPHORE_HANDLE_TYPE_SYNC_FD_BIT);
	return import_sem_fail ? VK_ERROR_INVALID_EXTERNAL_HANDLE : VK_SUCCESS;
}
static VkResult VKAPI_CALL drv_import_fence(VkDevice d, const VkImportFenceFdInfoKHR *i)
{
	printf("import-fence fd=%d temporary=%d\n", i->fd, !!(i->flags & VK_FENCE_IMPORT_TEMPORARY_BIT));
	return import_fence_fail ? VK_ERROR_INVALID_EXTERNAL_HANDLE : VK_SUCCESS;
}

static struct hv_device fake_dev;

/* Wayland: the application's side */
static struct wl_display *display;
static struct wl_compositor *compositor;
static uint32_t compositor_version = 4;
static void reg_global(void *data, struct wl_registry *r, uint32_t name, const char *iface, uint32_t version)
{
	if (!strcmp(iface, "wl_compositor"))
		compositor = wl_registry_bind(r, name, &wl_compositor_interface, version < compositor_version ? version : compositor_version);
}
static void reg_remove(void *data, struct wl_registry *r, uint32_t name) {}
static const struct wl_registry_listener reg_listener = {reg_global, reg_remove};

static pid_t server;
static void start_server(const char *path, char **opts, int nopts)
{
	int sv[2];
	socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, sv);
	fflush(stdout);
	server = fork();
	if (server == 0) {
		char fd[16];
		snprintf(fd, sizeof fd, "%d", sv[1]);
		fcntl(sv[1], F_SETFD, 0);
		char *argv[16] = {(char *)path, fd};
		for (int i = 0; i < nopts && i < 13; i++)
			argv[2 + i] = opts[i];
		execv(path, argv);
		_exit(127);
	}
	close(sv[1]);
	display = wl_display_connect_to_fd(sv[0]);
	struct wl_registry *r = wl_display_get_registry(display);
	wl_registry_add_listener(r, &reg_listener, NULL);
	wl_display_roundtrip(display);
}

static VkSurfaceKHR make_surface(void)
{
	VkWaylandSurfaceCreateInfoKHR ci = {.sType = VK_STRUCTURE_TYPE_WAYLAND_SURFACE_CREATE_INFO_KHR,
					    .display = display, .surface = wl_compositor_create_surface(compositor)};
	VkSurfaceKHR s = VK_NULL_HANDLE;
	VkResult r = create_wayland_surface(VK_NULL_HANDLE, &ci, NULL, &s);
	if (r != VK_SUCCESS)
		printf("surface result=%d\n", r);
	return s;
}

static int count_fds(void)
{
	int n = 0;
	for (int fd = 0; fd < 1024; fd++)
		n += fcntl(fd, F_GETFD) >= 0;
	return n;
}

static const char *opt(int argc, char **argv, const char *name, const char *def)
{
	size_t l = strlen(name);
	for (int i = 1; i < argc; i++)
		if (!strncmp(argv[i], name, l) && argv[i][l] == '=')
			return argv[i] + l + 1;
	return def;
}

int main(int argc, char **argv)
{
	setvbuf(stdout, NULL, _IOLBF, 0);
	/* argv[1]: the compositor binary; server options after "--" up to "++" */
	char *sopts[16];
	int nsopts = 0, i = 2;
	if (i < argc && !strcmp(argv[i], "--"))
		for (i++; i < argc && strcmp(argv[i], "++"); i++)
			sopts[nsopts++] = argv[i];
	compositor_version = atoi(opt(argc, argv, "client-compositor", "4"));

	fake_dev = (struct hv_device){.handle = (VkDevice)(uintptr_t)0x2000, .swapchain = true, .sync_fd_import = true,
				      .queue = (VkQueue)(uintptr_t)0x3000, .QueueSubmit = drv_submit,
				      .CreateImage = drv_create_image, .DestroyImage = drv_destroy_image,
				      .GetImageSubresourceLayout = drv_layout, .GetImageMemoryRequirements = drv_memreq,
				      .AllocateMemory = drv_alloc, .FreeMemory = drv_free, .BindImageMemory = drv_bind,
				      .GetMemoryFdPropertiesKHR = drv_fdprops, .CreateFence = drv_create_fence,
				      .DestroyFence = drv_destroy_fence, .WaitForFences = drv_wait, .ResetFences = drv_reset,
				      .ImportSemaphoreFdKHR = drv_import_sem, .ImportFenceFdKHR = drv_import_fence};
	pthread_mutex_init(&fake_dev.queue_lock, NULL);
	devices = &fake_dev;
	VkDevice dev = fake_dev.handle;
	VkQueue queue = (VkQueue)(uintptr_t)0x3001;

	start_server(argv[1], sopts, nsopts);
	VkSurfaceKHR surface = make_surface(), surface2 = VK_NULL_HANDLE;
	VkSwapchainKHR sc = VK_NULL_HANDLE, old = VK_NULL_HANDLE, sc2 = VK_NULL_HANDLE;
	VkSemaphore sem = (VkSemaphore)(uintptr_t)0x777;
	VkFence fence = (VkFence)(uintptr_t)0x778;
	int fds_before = count_fds();

	for (i++; i < argc; i++) {
		const char *a = argv[i];
		if (!strncmp(a, "create:", 7)) {
			/* create:WxH,count,mode,format,alpha */
			unsigned w, h, count, mode, format, alpha;
			sscanf(a + 7, "%ux%u,%u,%u,%u,%u", &w, &h, &count, &mode, &format, &alpha);
			VkSwapchainCreateInfoKHR ci = {.sType = VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR,
						       .surface = surface, .minImageCount = count, .imageFormat = format,
						       .imageColorSpace = VK_COLOR_SPACE_SRGB_NONLINEAR_KHR,
						       .imageExtent = {w, h}, .imageArrayLayers = atoi(opt(argc, argv, "layers", "1")),
						       .imageUsage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT,
						       .compositeAlpha = alpha ? VK_COMPOSITE_ALPHA_PRE_MULTIPLIED_BIT_KHR : VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR,
						       .presentMode = mode, .oldSwapchain = old};
			VkSwapchainKHR out = VK_NULL_HANDLE;
			VkResult r = create_swapchain(dev, &ci, NULL, &out);
			printf("create result=%d\n", r);
			if (r == VK_SUCCESS) {
				struct swapchain *s = (struct swapchain *)(uintptr_t)out;
				printf("swapchain count=%u stride=%u format=%.4s mode=%d\n", s->count, s->stride, (char *)&s->drm_format, s->mode);
				sc = out;
			}
		} else if (!strcmp(a, "retire")) {
			old = sc;
		} else if (!strcmp(a, "second-surface")) {
			surface2 = make_surface();
			VkSwapchainCreateInfoKHR ci = {.sType = VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR, .surface = surface2,
						       .minImageCount = 2, .imageFormat = VK_FORMAT_B8G8R8A8_UNORM, .imageExtent = {8, 8},
						       .imageArrayLayers = 1, .presentMode = VK_PRESENT_MODE_MAILBOX_KHR};
			printf("second result=%d\n", create_swapchain(dev, &ci, NULL, &sc2));
		} else if (!strcmp(a, "images")) {
			uint32_t n = 0;
			get_swapchain_images(dev, sc, &n, NULL);
			VkImage imgs[8];
			uint32_t room = 1;
			VkResult r = get_swapchain_images(dev, sc, &room, imgs);
			printf("images count=%u partial-result=%d partial=%u\n", n, r, room);
			room = 8;
			r = get_swapchain_images(dev, sc, &room, imgs);
			printf("images full-result=%d first=0x%llx\n", r, (unsigned long long)(uintptr_t)imgs[0]);
		} else if (!strncmp(a, "acquire:", 8)) {
			uint32_t idx = 99;
			uint64_t t = !strcmp(a + 8, "inf") ? UINT64_MAX : strtoull(a + 8, NULL, 0);
			VkResult r = acquire_next_image(dev, sc, t, sem, fence, &idx);
			printf("acquire result=%d index=%u\n", r, idx);
		} else if (!strncmp(a, "acquire-old:", 12)) {
			uint32_t idx = 99;
			printf("acquire-old result=%d\n", acquire_next_image(dev, old, 0, sem, VK_NULL_HANDLE, &idx));
		} else if (!strcmp(a, "acquire2")) {
			uint32_t idx = 99;
			VkAcquireNextImageInfoKHR ai = {.sType = VK_STRUCTURE_TYPE_ACQUIRE_NEXT_IMAGE_INFO_KHR, .swapchain = sc,
							.timeout = 0, .semaphore = VK_NULL_HANDLE, .fence = fence, .deviceMask = 1};
			VkResult r = acquire_next_image2(dev, &ai, &idx);
			printf("acquire2 result=%d index=%u\n", r, idx);
		} else if (!strncmp(a, "present:", 8)) {
			uint32_t idx = atoi(a + 8);
			VkResult res = 123;
			VkPresentInfoKHR pi = {.sType = VK_STRUCTURE_TYPE_PRESENT_INFO_KHR, .waitSemaphoreCount = 1,
					       .pWaitSemaphores = &sem, .swapchainCount = 1, .pSwapchains = &sc,
					       .pImageIndices = &idx, .pResults = &res};
			VkResult r = queue_present(queue, &pi);
			printf("present result=%d per-swapchain=%d\n", r, res);
		} else if (!strncmp(a, "present-both:", 13)) {
			uint32_t idx[2];
			sscanf(a + 13, "%u,%u", &idx[0], &idx[1]);
			VkSwapchainKHR both[2] = {sc, sc2};
			VkResult res[2] = {123, 123};
			VkPresentInfoKHR pi = {.sType = VK_STRUCTURE_TYPE_PRESENT_INFO_KHR, .swapchainCount = 2,
					       .pSwapchains = both, .pImageIndices = idx, .pResults = res};
			VkResult r = queue_present(queue, &pi);
			printf("present-both result=%d results=%d,%d\n", r, res[0], res[1]);
		} else if (!strcmp(a, "present-none")) {
			VkPresentInfoKHR pi = {.sType = VK_STRUCTURE_TYPE_PRESENT_INFO_KHR};
			printf("present-none result=%d\n", queue_present(queue, &pi));
		} else if (!strncmp(a, "loop:", 5)) {
			int n = atoi(a + 5), ok = 0;
			struct timespec t0, t1;
			clock_gettime(CLOCK_MONOTONIC, &t0);
			for (int k = 0; k < n; k++) {
				uint32_t idx;
				if (acquire_next_image(dev, sc, 2000000000ull, sem, VK_NULL_HANDLE, &idx) != VK_SUCCESS)
					break;
				VkPresentInfoKHR pi = {.sType = VK_STRUCTURE_TYPE_PRESENT_INFO_KHR, .swapchainCount = 1,
						       .pSwapchains = &sc, .pImageIndices = &idx};
				if (queue_present(queue, &pi) != VK_SUCCESS)
					break;
				ok++;
			}
			clock_gettime(CLOCK_MONOTONIC, &t1);
			printf("loop done=%d ms=%ld\n", ok, (t1.tv_sec - t0.tv_sec) * 1000 + (t1.tv_nsec - t0.tv_nsec) / 1000000);
		} else if (!strcmp(a, "roundtrip")) {
			wl_display_roundtrip(display);
		} else if (!strcmp(a, "destroy")) {
			destroy_swapchain(dev, sc, NULL);
			destroy_swapchain(dev, VK_NULL_HANDLE, NULL);
			wl_display_roundtrip(display);
			printf("destroyed images=%d memory=%d fds-leaked=%d\n", destroyed_images, freed_memory, count_fds() - fds_before);
			sc = VK_NULL_HANDLE;
		} else if (!strcmp(a, "destroy-surface")) {
			destroy_surface(VK_NULL_HANDLE, surface, NULL);
			surface = VK_NULL_HANDLE;
		} else if (!strcmp(a, "wait-server")) {
			int st;
			waitpid(server, &st, 0);
		} else if (!strcmp(a, "no-sync-fd")) {
			fake_dev.sync_fd_import = false;
		} else if (!strcmp(a, "no-queue")) {
			fake_dev.queue = VK_NULL_HANDLE;
		} else if (!strcmp(a, "not-swapchain-device")) {
			fake_dev.swapchain = false;
		} else if (!strcmp(a, "unknown-device")) {
			dev = (VkDevice)(uintptr_t)0x9999;
		} else if (!strcmp(a, "foreign-surface")) {
			static VkIcdSurfaceBase xcb = {.platform = VK_ICD_WSI_PLATFORM_XCB};
			surface = (VkSurfaceKHR)(uintptr_t)&xcb;
		} else if (!strcmp(a, "queries")) {
			VkBool32 sup = 9;
			surface_support(NULL, 0, surface, &sup);
			VkSurfaceCapabilitiesKHR caps = {0};
			VkResult rc = surface_capabilities(NULL, surface, &caps);
			uint32_t nf = 0, nm = 0;
			VkResult rf = surface_formats(NULL, surface, &nf, NULL);
			VkSurfaceFormatKHR fmts[8];
			uint32_t room = 2;
			VkResult rf2 = surface_formats(NULL, surface, &room, fmts);
			VkResult rm = surface_present_modes(NULL, surface, &nm, NULL);
			VkPresentModeKHR modes[4];
			uint32_t mroom = 4;
			surface_present_modes(NULL, surface, &mroom, modes);
			VkDeviceGroupPresentModeFlagsKHR gm = 0;
			VkResult rg = device_group_surface_present_modes(dev, surface, &gm);
			printf("support=%u caps=%d min=%u max=%u extent=%u usage=0x%x alpha=0x%x\n", sup, rc, caps.minImageCount,
			       caps.maxImageCount, caps.currentExtent.width, caps.supportedUsageFlags, caps.supportedCompositeAlpha);
			printf("formats=%d count=%u partial=%d/%u first=%d modes=%d count=%u first=%d group=%d/0x%x\n", rf, nf, rf2,
			       room, fmts[0].format, rm, nm, modes[0], rg, gm);
			VkDeviceGroupPresentCapabilitiesKHR gc = {0};
			device_group_present_capabilities(dev, &gc);
			printf("wayland-support=%u group-mask=%u group-modes=0x%x\n",
			       wayland_presentation_support(NULL, 0, display), gc.presentMask[0], gc.modes);
		} else if (!strncmp(a, "heap-fail", 9)) {
			heap_fail = 1;
		} else if (!strncmp(a, "fail-image:", 11)) {
			fail_create_image_at = n_create_images + atoi(a + 11);
		} else if (!strncmp(a, "extra-pitch:", 12)) {
			extra_pitch = atoi(a + 12);
		} else if (!strncmp(a, "layout-offset:", 14)) {
			layout_offset = atoi(a + 14);
		} else if (!strcmp(a, "fdprops-fail")) {
			fdprops_fail = 1;
		} else if (!strcmp(a, "no-types")) {
			no_types = 1;
		} else if (!strcmp(a, "alloc-fail")) {
			alloc_fail = 1;
		} else if (!strcmp(a, "bind-fail")) {
			bind_fail = 1;
		} else if (!strcmp(a, "fence-fail")) {
			fence_fail = 1;
		} else if (!strcmp(a, "submit-fail")) {
			submit_fail = 1;
		} else if (!strcmp(a, "wait-timeout")) {
			wait_timeout = 1;
		} else if (!strcmp(a, "submit-ok")) {
			submit_fail = 0;
			wait_timeout = 0;
		} else if (!strcmp(a, "import-sem-fail")) {
			import_sem_fail = 1;
		} else if (!strcmp(a, "import-fence-fail")) {
			import_fence_fail = 1;
		} else if (!strncmp(a, "calloc-fail:", 12)) {
			calloc_fail_at = callocs + atoi(a + 12);
		} else if (!strcmp(a, "malloc-fail")) {
			malloc_fail = 1;
		}
	}
	if (sc)
		destroy_swapchain(dev, sc, NULL);
	wl_display_disconnect(display);
	int st;
	waitpid(server, &st, 0);
	printf("end\n");
	return 0;
}
"""

UNORM = 44  # VK_FORMAT_B8G8R8A8_UNORM
SRGB = 50
RGBA = 37
FIFO, MAILBOX, IMMEDIATE = 2, 1, 0


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    d = tmp_path_factory.mktemp("hybris_vulkan_wsi")
    for name in ("heap_mtk_mm_uncached", "heap_mtk_mm", "heap_system"):
        (d / name).write_text("")
    exe = build(d, "hybris_vulkan_wsi", HARNESS,
                replacements_wsi=[("/dev/dma_heap/mtk_mm-uncached", d / "heap_mtk_mm_uncached"),
                                  ("/dev/dma_heap/mtk_mm\"", f"{d / 'heap_mtk_mm'}\""),
                                  ("/dev/dma_heap/system", d / "heap_system")])
    generate_protocol(d, server=True)
    server = compile_c(d / "hv_fake_compositor", [HERE / "c" / "hv_fake_compositor.c",
                                                   d / "linux-dmabuf-v1-protocol.c"],
                       flags=[f"-I{d}"], pkgs=("wayland-server",), counted=False)
    return exe, server, d


def call(harness, *args, server_opts=(), env=None):
    exe, server, d = harness
    argv = [server, "--", *server_opts, "++", *args]
    e = dict(os.environ)
    e.pop("HYBRIS_VULKAN_DMA_HEAP", None)
    e.update(env or {})
    return run(exe, *argv, env=e).stdout


def loop_ms(out):
    return int(re.search(r"loop done=\d+ ms=(\d+)", out).group(1))


def lines(out, prefix):
    return [l for l in out.splitlines() if l.startswith(prefix)]


# Surfaces

def test_surface_queries(harness):
    out = call(harness, "queries", "destroy-surface")
    assert "support=1 caps=0 min=2 max=8 extent=4294967295 usage=0x97 alpha=0x3" in out
    assert "formats=0 count=4 partial=5/2 first=50 modes=0 count=2 first=2 group=0/0x1" in out
    assert "wayland-support=1 group-mask=1 group-modes=0x1" in out


def test_foreign_surface_is_not_ours(harness):
    out = call(harness, "foreign-surface", "queries", "create:64x64,2,2,44,0")
    assert "support=0 caps=-1000000000 " in out  # VK_ERROR_SURFACE_LOST_KHR
    assert "formats=-1000000000 count=0 partial=-1000000000/2 first=" in out
    assert "modes=-1000000000" in out and "group=-1000000000/0x0" in out
    assert "create result=-1000000000" in out


def test_surface_out_of_memory(harness):
    out = call(harness, "calloc-fail:1", "second-surface")
    assert "surface result=-1" in out


# Creating a swapchain

def test_swapchain_images_are_imported_dma_bufs(harness):
    out = call(harness, "create:476x1011,3,2,44,0", "images", "destroy")
    imgs = lines(out, "image ")
    # One image the driver lays out itself for its pitch, then three with that layout spelled out
    assert imgs[0] == "image list pitch=0 476x1011 format=44 tiling=1000158000 usage=0x10"
    assert imgs[1:] == ["image explicit pitch=1920 476x1011 format=44 tiling=1000158000 usage=0x10"] * 3
    assert "layout aspect=0x80" in out  # VK_IMAGE_ASPECT_MEMORY_PLANE_0_BIT_EXT
    # Memory: the dma-buf's size (pitch x height, page-aligned), the type both sides allow, dedicated
    size = (1920 * 1011 + 4095) // 4096 * 4096
    assert lines(out, "alloc ") == [f"alloc size={size} type=1 dedicated=1 fd-valid=1"] * 3
    # The compositor gets each buffer with that pitch and the LINEAR modifier, never create_immed
    assert lines(out, "server: add") == ["server: add plane=0 offset=0 stride=1920 modifier=0:0"] * 3
    assert lines(out, "server: create ") == ["server: create 476x1011 format=XR24 flags=0"] * 3
    assert "server: bind linux-dmabuf version 3" in out
    assert "swapchain count=3 stride=1920 format=XR24 mode=2" in out
    assert "images count=3 partial-result=5 partial=1" in out
    # Handles: the fence first, then the probe image, then image, memory, ...
    assert "images full-result=0 first=0x102" in out
    # Images and memory go back to the driver, no fd is left open
    assert "destroyed images=4 memory=3 fds-leaked=0" in out


@pytest.mark.parametrize("fmt,alpha,fourcc", [(UNORM, 1, "AR24"), (SRGB, 0, "XR24"), (RGBA, 0, "XB24"),
                                               (RGBA, 1, "AB24")])
def test_formats_and_alpha(harness, fmt, alpha, fourcc):
    out = call(harness, f"create:64x64,2,2,{fmt},{alpha}")
    assert f"format={fourcc}" in lines(out, "swapchain")[0]


@pytest.mark.parametrize("asked,got", [(1, 2), (3, 3), (20, 8)])
def test_image_count_within_limits(harness, asked, got):
    assert f"swapchain count={got} " in call(harness, f"create:64x64,{asked},2,44,0")


@pytest.mark.parametrize("mode,got", [(FIFO, FIFO), (MAILBOX, MAILBOX), (IMMEDIATE, MAILBOX), (3, FIFO)])
def test_present_modes_map_to_fifo_or_mailbox(harness, mode, got):
    assert f"mode={got}" in lines(call(harness, f"create:64x64,2,{mode},44,0"), "swapchain")[0]


@pytest.mark.parametrize("args", [["create:64x64,2,2,99,0"], ["layers=2", "create:64x64,2,2,44,0"],
                                  ["create:0x64,2,2,44,0"], ["create:64x0,2,2,44,0"],
                                  ["create:16385x64,2,2,44,0"], ["create:64x16385,2,2,44,0"],
                                  ["unknown-device", "create:64x64,2,2,44,0"],
                                  ["not-swapchain-device", "create:64x64,2,2,44,0"]])
def test_invalid_swapchains_are_refused(harness, args):
    out = call(harness, *args)
    assert "create result=-3" in out  # VK_ERROR_INITIALIZATION_FAILED
    assert "server: create" not in out


def test_compositor_without_linux_dmabuf(harness):
    out = call(harness, "create:64x64,2,2,44,0", server_opts=["no-dmabuf"])
    assert "create result=-3" in out
    assert "image " not in out


def test_compositor_refusing_the_buffer(harness):
    out = call(harness, "create:64x64,2,2,44,0", server_opts=["fail-params"], env={"HYBRIS_VULKAN_DEBUG": "1"})
    assert "create result=-3" in out
    assert "server: create 64x64" in out


def test_compositor_gone_while_creating(harness):
    out = call(harness, "create:64x64,2,2,44,0", server_opts=["die-at-create"])
    assert "create result=-1000000000" in out  # VK_ERROR_SURFACE_LOST_KHR


@pytest.mark.parametrize("args,result", [
    (["heap-fail"], -2),                     # no heap has memory: VK_ERROR_OUT_OF_DEVICE_MEMORY
    (["fail-image:1"], -2),                  # the layout probe
    (["fail-image:2"], -2),                  # the first real image
    (["extra-pitch:-4"], -3),                # a pitch shorter than a row
    (["layout-offset:64"], -3),              # plane 0 not at the start
    (["fdprops-fail"], -1000072003),         # VK_ERROR_INVALID_EXTERNAL_HANDLE
    (["no-types"], -2),                      # no memory type for both the image and the dma-buf
    (["alloc-fail"], -2),
    (["bind-fail"], -2),
    (["fence-fail"], -1),
    (["calloc-fail:1"], -1),
])
def test_driver_and_heap_failures(harness, args, result):
    out = call(harness, *args, "create:64x64,2,2,44,0", "roundtrip")
    assert f"create result={result}" in out
    # Whatever was made is gone again; the fds too (checked by the leak count in the end)
    assert "swapchain count" not in out


def test_heap_order_and_override(harness, tmp_path):
    exe, server, d = harness
    # Without the two MediaTek heaps the generic one is taken
    for name in ("heap_mtk_mm_uncached", "heap_mtk_mm"):
        (d / name).unlink()
    try:
        assert "create result=0" in call(harness, "create:64x64,2,2,44,0")
        (d / "heap_system").unlink()
        out = call(harness, "create:64x64,2,2,44,0", env={"HYBRIS_VULKAN_DEBUG": "1"})
        assert "create result=-2" in out
        # HYBRIS_VULKAN_DMA_HEAP is tried first
        custom = tmp_path / "custom_heap"
        custom.write_text("")
        assert "create result=0" in call(harness, "create:64x64,2,2,44,0",
                                         env={"HYBRIS_VULKAN_DMA_HEAP": str(custom)})
        assert "create result=-2" in call(harness, "create:64x64,2,2,44,0", env={"HYBRIS_VULKAN_DMA_HEAP": ""})
    finally:
        for name in ("heap_mtk_mm_uncached", "heap_mtk_mm", "heap_system"):
            (d / name).write_text("")


# Acquire and present

def test_acquire_present_cycle_follows_the_releases(harness):
    out = call(harness, "create:64x64,3,2,44,0", "loop:12", env={"HYBRIS_VULKAN_DEBUG": "1"})
    assert "loop done=12 " in out
    assert len(lines(out, "server: commit")) == 12
    # Every present waits for the GPU first: an empty submission with a fence on the present queue
    assert lines(out, "submit ").count("submit queue=0x3001 waits=0 stage=0x0 signals=0 fence=1") == 12
    assert lines(out, "wait-fence") == ["wait-fence timeout=5000000000"] * 12
    # The semaphore of each acquire: a signalled sync file, no submission
    assert lines(out, "import-semaphore") == ["import-semaphore fd=-1 temporary=1 sync-fd=1"] * 12
    # FIFO: a frame callback per commit, damage over the whole buffer
    assert len(lines(out, "server: frame")) == 12
    assert "server: damage_buffer 0 0 2147483647 2147483647" in out


def test_present_waits_for_the_applications_semaphores(harness):
    out = call(harness, "create:64x64,2,2,44,0", "acquire:0", "present:0")
    assert "submit queue=0x3001 waits=1 stage=0x10000 signals=0 fence=1" in out  # ALL_COMMANDS
    assert "present result=0 per-swapchain=0" in out
    assert "server: commit 1 buffer=yes" in out


def test_acquire_without_free_image(harness):
    out = call(harness, "create:64x64,2,2,44,0", "acquire:0", "acquire:0", "acquire:0", "acquire:20000000")
    assert lines(out, "acquire result") == ["acquire result=0 index=0", "acquire result=0 index=1",
                                            "acquire result=1 index=99",  # VK_NOT_READY
                                            "acquire result=2 index=99"]  # VK_TIMEOUT


def test_acquire_waits_for_a_release(harness):
    # Two images, both presented: the second commit releases the first
    out = call(harness, "create:64x64,2,2,44,0", "acquire:0", "present:0", "acquire:0", "present:1",
               "acquire:inf")
    assert lines(out, "acquire result")[-1] == "acquire result=0 index=0"


def test_no_release_means_timeout(harness):
    out = call(harness, "create:64x64,2,2,44,0", "acquire:0", "present:0", "acquire:0", "present:1",
               "acquire:30000000", server_opts=["no-release"])
    assert lines(out, "acquire result")[-1] == "acquire result=2 index=99"


def test_acquire_fence_and_acquire2(harness):
    out = call(harness, "create:64x64,2,2,44,0", "acquire2")
    assert "acquire2 result=0 index=0" in out
    assert "import-fence fd=-1 temporary=1" in out
    assert "import-semaphore" not in out


@pytest.mark.parametrize("args,submit", [
    (["no-sync-fd"], "submit queue=0x3000 waits=0 stage=0x0 signals=1 fence=1"),
    (["import-sem-fail"], "submit queue=0x3000 waits=0 stage=0x0 signals=1 fence=0"),
    (["import-fence-fail"], "submit queue=0x3000 waits=0 stage=0x0 signals=0 fence=1"),
])
def test_acquire_falls_back_to_an_empty_submission(harness, args, submit):
    out = call(harness, *args, "create:64x64,2,2,44,0", "acquire:0")
    assert "acquire result=0 index=0" in out
    assert submit in out


def test_fallback_without_queue_and_failing_submission(harness):
    out = call(harness, "no-sync-fd", "no-queue", "create:64x64,2,2,44,0", "acquire:0")
    assert "acquire result=-4 index=99" in out  # VK_ERROR_DEVICE_LOST
    out = call(harness, "no-sync-fd", "submit-fail", "create:64x64,2,2,44,0", "acquire:0", "acquire:0")
    assert lines(out, "acquire result") == ["acquire result=-4 index=99"] * 2


def test_gpu_not_done_is_device_lost(harness):
    out = call(harness, "create:64x64,2,2,44,0", "acquire:0", "wait-timeout", "present:0", "submit-fail",
               "present:0", "submit-ok", "present:0")
    assert lines(out, "present result") == ["present result=-4 per-swapchain=-4"] * 2 + \
        ["present result=0 per-swapchain=0"]
    # Nothing reached the compositor until the GPU was done
    assert lines(out, "server: commit") == ["server: commit 1 buffer=yes"]


def test_present_of_an_image_not_acquired(harness):
    out = call(harness, "create:64x64,2,2,44,0", "present:0", "present:7")
    assert lines(out, "present result") == ["present result=-1000001004 per-swapchain=-1000001004"] * 2


def test_present_without_swapchains_and_out_of_memory(harness):
    out = call(harness, "present-none", "create:64x64,2,2,44,0", "acquire:0", "malloc-fail", "present:0")
    assert "present-none result=0" in out
    assert "present result=-1 per-swapchain=123" in out


def test_two_swapchains_in_one_present(harness):
    out = call(harness, "create:64x64,2,2,44,0", "second-surface", "acquire:0", "present-both:0,0",
               "present-both:0,0")
    assert "second result=0" in out
    # The second swapchain's image was never acquired: its result says so, the first still shows
    assert "present-both result=-1000001004 results=0,-1000001004" in out
    assert "present-both result=-1000001004 results=-1000001004,-1000001004" in out


def test_fifo_waits_for_the_frame_callback_but_not_forever(harness):
    out = call(harness, "create:64x64,3,2,44,0", "loop:4", server_opts=["no-frames"])
    assert "loop done=4 " in out
    # Three of the four presents wait out FRAME_WAIT_NS (100 ms) for a callback that never comes
    assert 290 <= loop_ms(out) < 1500


def test_mailbox_does_not_wait(harness):
    out = call(harness, "create:64x64,3,1,44,0", "loop:4", server_opts=["no-frames"])
    assert "server: frame" not in out
    assert loop_ms(out) < 250


def test_old_compositor_gets_surface_damage(harness):
    out = call(harness, "client-compositor=3", "create:64x64,2,2,44,0", "acquire:0", "present:0",
               server_opts=["compositor:3"])
    assert "server: damage 0 0 2147483647 2147483647" in out
    assert "damage_buffer" not in out


def test_retired_swapchain_is_out_of_date(harness):
    out = call(harness, "create:64x64,2,2,44,0", "retire", "create:64x64,2,2,44,0", "acquire-old:0")
    assert out.count("create result=0") == 2
    assert "acquire-old result=-1000001004" in out


def test_lost_compositor(harness):
    # The compositor exits after the first commit: the free image is still handed out, waiting
    # for another one finds the connection gone, and from then on everything says so
    out = call(harness, "create:64x64,2,2,44,0", "acquire:0", "present:0", "wait-server", "acquire:inf",
               "acquire:inf", "present:1", "acquire:0", server_opts=["die-after:1"])
    assert lines(out, "acquire result") == ["acquire result=0 index=0", "acquire result=0 index=1",
                                            "acquire result=-1000000000 index=99",
                                            "acquire result=-1000000000 index=99"]
    assert "present result=-1000000000 per-swapchain=-1000000000" in out


def test_debug_log_goes_to_stderr_only(harness):
    exe, server, d = harness
    r = run(exe, server, "--", "++", "create:64x64,2,2,44,0", "acquire:0", "present:0",
            env={**os.environ, "HYBRIS_VULKAN_DEBUG": "1"})
    assert "hybris-vulkan: swapchain" in r.stderr
    assert "hybris-vulkan: acquired 0" in r.stderr and "hybris-vulkan: presented 0" in r.stderr
    assert "hybris-vulkan:" not in r.stdout
    quiet = run(exe, server, "--", "++", "create:64x64,2,2,44,0", env={**os.environ, "HYBRIS_VULKAN_DEBUG": "0"})
    assert "hybris-vulkan:" not in quiet.stderr
