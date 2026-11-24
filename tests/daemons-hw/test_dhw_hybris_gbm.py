# hybris-gbm: GBM to Android pixel formats, which fd of a gralloc handle is the dma-buf (from
# fdinfo: exp_name and size), stride in bytes, finding a buffer again by its dma-buf, and the
# double-buffered surface. libhybris' gralloc functions are fakes in the harness; fdinfo is read
# from files in tmp instead of /proc/self.

import functools

import pytest

from dhw_harness import PKG, build_harness, kv, run, sandboxed_source

SRC = PKG / "hybris-gbm" / "hybris_gbm.c"

HARNESS = r"""
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <EGL/egl.h>
#include <sys/stat.h>
#include <unistd.h>
/* Scripted failures: the n-th calloc (out of memory), fstat of a dup'd dma-buf; dup is
 * recorded, so a test sees that the fd it made is closed again */
static int calloc_fail_at, callocs, fstat_fails, last_dup = -1;
static void *test_calloc(size_t n, size_t size)
{
	return ++callocs == calloc_fail_at ? NULL : calloc(n, size);
}
static int test_fstat(int fd, struct stat *st)
{
	return fstat_fails && fd == last_dup ? -1 : fstat(fd, st);
}
static int test_dup(int fd)
{
	return last_dup = dup(fd);
}
#define calloc test_calloc
#define fstat test_fstat
#define dup test_dup
#include "hybris_gbm.c"
#undef calloc
#undef fstat
#undef dup

/* gralloc: three fds per handle, as on the Plinius; the dma-buf is fake_fds[1] */
static int fake_fds[3];
static int created, released, fail_info;
static EGLint fake_stride = 1088;
void hybris_gralloc_initialize(int framebuffer) {}
EGLBoolean eglplatformcommon_eglHybrisCreateNativeBuffer(EGLint w, EGLint h, EGLint usage, EGLint format,
							 EGLint *stride, EGLClientBuffer *buffer)
{
	printf("gralloc %dx%d usage=0x%x format=%d\n", w, h, usage, format);
	*stride = fake_stride;
	*buffer = (EGLClientBuffer)(intptr_t)(0x1000 + ++created);
	return EGL_TRUE;
}
EGLBoolean eglplatformcommon_eglHybrisReleaseNativeBuffer(EGLClientBuffer b)
{
	released++;
	return EGL_TRUE;
}
EGLBoolean eglplatformcommon_eglHybrisLockNativeBuffer(EGLClientBuffer b, EGLint usage, EGLint l, EGLint t,
						       EGLint w, EGLint h, void **vaddr)
{
	return EGL_FALSE;
}
EGLBoolean eglplatformcommon_eglHybrisUnlockNativeBuffer(EGLClientBuffer b)
{
	return EGL_TRUE;
}
EGLBoolean eglplatformcommon_eglHybrisGetNativeBufferInfo(EGLClientBuffer b, int *num_ints, int *num_fds)
{
	*num_ints = 20;
	*num_fds = fail_info ? 9 : 3;
	return EGL_TRUE;
}
EGLBoolean eglplatformcommon_eglHybrisSerializeNativeBuffer(EGLClientBuffer b, int *ints, int *fds)
{
	memcpy(fds, fake_fds, sizeof fake_fds);
	return EGL_TRUE;
}

static void fdinfo(int fd, const char *text)
{
	char path[512];
	snprintf(path, sizeof path, "%s/fdinfo/%d", BASE, fd);
	FILE *f = fopen(path, "w");
	fputs(text, f);
	fclose(f);
}

int main(int argc, char **argv)
{
	char path[512];
	for (int i = 0; i < 3; i++) {
		snprintf(path, sizeof path, "%s/buf%d", BASE, i);
		fake_fds[i] = open(path, O_RDWR | O_CREAT, 0600);
	}
	/* fd 0: no dma-buf; fd 1: the buffer's dma-buf; fd 2: a small dma-buf (metadata) */
	fdinfo(fake_fds[0], "pos:\t0\nflags:\t02\nmnt_id:\t15\n");
	fdinfo(fake_fds[2], "pos:\t0\nflags:\t02\nsize:\t4096\nexp_name:\tion\n");
	for (int i = 1; i < argc; i++) {
		char *a = argv[i];
		if (!strncmp(a, "format:", 7)) {
			uint32_t f = strtoul(a + 7, NULL, 0);
			printf("format %s=%d\n", a + 7, hal_format(f));
		} else if (!strncmp(a, "dmabuf-size:", 12)) {
			char text[128];
			snprintf(text, sizeof text, "pos:\t0\nflags:\t02\nsize:\t%s\nexp_name:\tion\n", a + 12);
			fdinfo(fake_fds[1], text);
		} else if (!strncmp(a, "stride:", 7)) {
			fake_stride = atoi(a + 7);
		} else if (!strcmp(a, "bad-info")) {
			fail_info = 1;
		} else if (!strncmp(a, "alloc:", 6)) {
			unsigned w, h, f;
			sscanf(a + 6, "%u,%u,%x", &w, &h, &f);
			struct hybris_bo *bo = alloc_bo(NULL, w, h, f);
			if (!bo) {
				printf("alloc=failed created=%d released=%d\n", created, released);
				continue;
			}
			struct stat st1, st2;
			fstat(bo->fd, &st1);
			fstat(fake_fds[1], &st2);
			printf("alloc=stride %u dmabuf %d handle-is-fd %d\n", bo->base.v0.stride,
			       st1.st_ino == st2.st_ino, bo->base.v0.handle.s32 == bo->fd);
			/* A dup of the dma-buf (another process would get another fd) finds the buffer */
			int other = dup(fake_fds[1]);
			printf("lookup=%d\n", hybris_gbm_native_buffer(other) == bo->buffer);
			free_bo(bo);
			printf("lookup-after-free=%d released=%d\n", hybris_gbm_native_buffer(other) != NULL, released);
			close(other);
		} else if (!strncmp(a, "oom:", 4)) {
			calloc_fail_at = atoi(a + 4);
		} else if (!strcmp(a, "fstat-fails")) {
			fstat_fails = 1;
		} else if (!strcmp(a, "dup-closed")) {
			printf("dup-closed=%d\n", last_dup >= 0 && fcntl(last_dup, F_GETFD) < 0);
		} else if (!strcmp(a, "device")) {
			struct gbm_device *dev = gbmint_get_backend(NULL)->v0.create_device(7, 1);
			printf("device=%s\n", dev ? dev->v0.name : "none");
			if (dev)
				dev->v0.destroy(dev);
		} else if (!strcmp(a, "surface-unused")) {
			struct gbm_device dev = {0};
			struct gbm_surface *s = surface_create(&dev, 64, 64, GBM_FORMAT_XRGB8888, 0, NULL, 0);
			if (!s) {
				printf("surface=none created=%d released=%d\n", created, released);
				continue;
			}
			surface_destroy(s);
			printf("surface-destroyed released=%d\n", released);
		} else if (!strcmp(a, "surface")) {
			struct gbm_device dev = {0};
			struct gbm_surface *s = surface_create(&dev, 1080, 2400, GBM_FORMAT_XRGB8888, 0, NULL, 0);
			struct gbm_bo *f1 = surface_lock_front_buffer(s);
			struct gbm_bo *f2 = surface_lock_front_buffer(s);
			struct gbm_bo *f3 = surface_lock_front_buffer(s);
			printf("surface=created %d distinct %d alternates %d\n", created, f1 != f2, f1 == f3);
			surface_destroy(s);
			printf("surface-destroyed released=%d\n", released);
		}
	}
	return 0;
}
"""


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("gbm")
    (d / "fdinfo").mkdir()
    sandboxed_source(SRC, d, [
        ('"/proc/self/fdinfo/%d"', f'"{d}/fdinfo/%d"'),
        ("char path[64], line[128];", "char path[512], line[128];"),
    ])
    exe = build_harness(d, "gbm", HARNESS, flags=[f'-DBASE="{d}"'],
                        pkgs=("gbm", "egl"))
    return functools.partial(run, exe)


@pytest.mark.parametrize("gbm,hal", [
    ("0x34325241", 5),   # ARGB8888 (B, G, R, A in memory) -> HAL_PIXEL_FORMAT_BGRA_8888
    ("0x34325258", 5),   # XRGB8888
    ("0x34324241", 1),   # ABGR8888 -> RGBA_8888
    ("0x34324258", 2),   # XBGR8888 -> RGBX_8888
    ("0x36314752", 4),   # RGB565
    ("0x3231564e", -1),  # NV12: not offered
])
def test_hal_format(built, gbm, hal):
    assert kv(built(f"format:{gbm}").stdout)[f"format {gbm}"] == str(hal)


def test_alloc_takes_the_large_dmabuf(built):
    # 1088 px stride (gralloc pads 1080) x 2400 x 4 bytes
    out = built(f"dmabuf-size:{1088 * 2400 * 4}", "alloc:1080,2400,34325258").stdout
    k = kv(out)
    assert k["alloc"] == f"stride {1088 * 4} dmabuf 1 handle-is-fd 1"
    assert k["lookup"] == "1"
    assert k["lookup-after-free"] == "0 released=1"
    # Linear, CPU-accessible usage, so that gralloc chooses no compressed layout
    assert "gralloc 1080x2400 usage=0xb22 format=5" in out


def test_rgb565_stride(built):
    out = built(f"dmabuf-size:{1088 * 100 * 2}", "alloc:1080,100,36314752").stdout
    assert kv(out)["alloc"].startswith(f"stride {1088 * 2} ")


def test_alloc_without_large_enough_dmabuf_fails_cleanly(built):
    out = built(f"dmabuf-size:{1088 * 2400 * 4 - 1}", "alloc:1080,2400,34325258").stdout
    assert kv(out)["alloc"] == "failed created=1 released=1"


def test_alloc_with_too_many_fds_fails_cleanly(built):
    out = built(f"dmabuf-size:{10**9}", "bad-info", "alloc:16,16,34325258").stdout
    assert kv(out)["alloc"] == "failed created=1 released=1"


def test_unsupported_format_allocates_nothing(built):
    assert kv(built("alloc:16,16,3231564e").stdout)["alloc"] == "failed created=0 released=0"


def test_surface_double_buffer(built):
    out = built(f"dmabuf-size:{10**9}", "surface").stdout
    k = kv(out)
    assert k["surface"] == "created 2 distinct 1 alternates 1"
    assert k["surface-destroyed released"] == "2"


# ---- As Mesa's libgbm loads it ----
# The backend built as the PKGBUILD builds it (hidden symbols, hybris_gbm.so), loaded by the
# host's libgbm through GBM_BACKENDS_PATH/GBM_BACKEND=hybris, as wlroots on the phone does. The
# program around it stands in for libhybris' gralloc functions (exported to the backend).

CLIENT = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <gbm.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include <EGL/egl.h>

/* gralloc: three fds per handle (no fdinfo, a small dma-buf, the buffer's dma-buf) */
static int fds[3], created, released, locked, unlocked, fail_create, lock_ok;
static char pixels[64];
void hybris_gralloc_initialize(int framebuffer) { printf("gralloc-init %d\n", framebuffer); }
EGLBoolean eglplatformcommon_eglHybrisCreateNativeBuffer(EGLint w, EGLint h, EGLint usage, EGLint format,
							 EGLint *stride, EGLClientBuffer *buffer)
{
	if (fail_create)
		return EGL_FALSE;
	*stride = (w + 63) / 64 * 64;
	*buffer = (EGLClientBuffer)(intptr_t)(0x1000 + ++created);
	return EGL_TRUE;
}
EGLBoolean eglplatformcommon_eglHybrisReleaseNativeBuffer(EGLClientBuffer b) { released++; return EGL_TRUE; }
EGLBoolean eglplatformcommon_eglHybrisLockNativeBuffer(EGLClientBuffer b, EGLint usage, EGLint l, EGLint t,
						       EGLint w, EGLint h, void **vaddr)
{
	printf("lock usage=0x%x %d,%d %dx%d\n", usage, l, t, w, h);
	locked++;
	*vaddr = pixels;
	return lock_ok;
}
EGLBoolean eglplatformcommon_eglHybrisUnlockNativeBuffer(EGLClientBuffer b) { unlocked++; return EGL_TRUE; }
EGLBoolean eglplatformcommon_eglHybrisGetNativeBufferInfo(EGLClientBuffer b, int *num_ints, int *num_fds)
{
	*num_ints = 4;
	*num_fds = 3;
	return EGL_TRUE;
}
EGLBoolean eglplatformcommon_eglHybrisSerializeNativeBuffer(EGLClientBuffer b, int *ints, int *out)
{
	memcpy(out, fds, sizeof fds);
	return EGL_TRUE;
}

static void fdinfo(int fd, const char *text)
{
	char path[512];
	snprintf(path, sizeof path, "%s/fdinfo/%d", BASE, fd);
	FILE *f = fopen(path, "w");
	fputs(text, f);
	fclose(f);
}

static ino_t ino(int fd)
{
	struct stat st;
	return fstat(fd, &st) < 0 ? 0 : st.st_ino;
}

int main(int argc, char **argv)
{
	char path[512];
	for (int i = 0; i < 3; i++) {
		snprintf(path, sizeof path, "%s/buf%d", BASE, i);
		fds[i] = open(path, O_RDWR | O_CREAT, 0600);
	}
	snprintf(path, sizeof path, "%s/fdinfo/%d", BASE, fds[0]);
	unlink(path);
	fdinfo(fds[1], "pos:\t0\nsize:\t4096\nexp_name:\tion\n");
	fdinfo(fds[2], "pos:\t0\nsize:\t1000000000\nexp_name:\tion\n");

	/* Any character device: the backend only keeps the fd */
	int drm = open("/dev/null", O_RDONLY);
	struct gbm_device *gbm = gbm_create_device(drm);
	if (!gbm) {
		printf("device=none\n");
		return 1;
	}
	printf("device=%s fd-kept=%d\n", gbm_device_get_backend_name(gbm), gbm_device_get_fd(gbm) == drm);
	void *backend = dlopen(BASE "/backends/hybris_gbm.so", RTLD_NOW | RTLD_NOLOAD);
	EGLClientBuffer (*native)(int) = backend ? dlsym(backend, "hybris_gbm_native_buffer") : NULL;
	printf("native-exported=%d\n", native != NULL);

	for (int i = 1; i < argc; i++) {
		char *a = argv[i];
		if (!strcmp(a, "formats")) {
			printf("xrgb=%d abgr=%d nv12=%d planes=%d\n",
			       gbm_device_is_format_supported(gbm, GBM_FORMAT_XRGB8888, GBM_BO_USE_RENDERING),
			       gbm_device_is_format_supported(gbm, GBM_FORMAT_ABGR8888, GBM_BO_USE_SCANOUT),
			       gbm_device_is_format_supported(gbm, GBM_FORMAT_NV12, GBM_BO_USE_RENDERING),
			       gbm_device_get_format_modifier_plane_count(gbm, GBM_FORMAT_XRGB8888, 0));
		} else if (!strcmp(a, "lock-ok")) {
			lock_ok = 1;
		} else if (!strcmp(a, "gralloc-fails")) {
			fail_create = 1;
		} else if (!strcmp(a, "bo") || !strcmp(a, "bo-modifiers")) {
			const uint64_t mods[] = {0x0800000000000001ull};   /* an AFBC-style modifier: ignored */
			struct gbm_bo *bo = !strcmp(a, "bo")
				? gbm_bo_create(gbm, 1080, 2400, GBM_FORMAT_ARGB8888, GBM_BO_USE_SCANOUT | GBM_BO_USE_RENDERING)
				: gbm_bo_create_with_modifiers(gbm, 1080, 2400, GBM_FORMAT_ARGB8888, mods, 1);
			if (!bo) {
				printf("bo=none created=%d released=%d\n", created, released);
				continue;
			}
			int fd = gbm_bo_get_fd(bo), pfd = gbm_bo_get_fd_for_plane(bo, 0);
			printf("bo=%ux%u stride=%u format=%#x planes=%d offset=%u modifier=%#llx\n", gbm_bo_get_width(bo),
			       gbm_bo_get_height(bo), gbm_bo_get_stride(bo), gbm_bo_get_format(bo), gbm_bo_get_plane_count(bo),
			       gbm_bo_get_offset(bo, 0), (unsigned long long)gbm_bo_get_modifier(bo));
			printf("plane-stride=%u handle=%d plane-handle=%d\n", gbm_bo_get_stride_for_plane(bo, 0),
			       ino(gbm_bo_get_handle(bo).s32) == ino(fds[2]),
			       gbm_bo_get_handle_for_plane(bo, 0).s32 == gbm_bo_get_handle(bo).s32);
			printf("fd-is-dmabuf=%d plane-fd-is-dmabuf=%d own-fds=%d\n", ino(fd) == ino(fds[2]),
			       ino(pfd) == ino(fds[2]), fd != fds[2] && pfd != fd);
			/* libhybris' EGL finds the buffer behind a dma-buf it is given */
			printf("native=%#lx unknown=%d closed=%d\n", (unsigned long)(uintptr_t)native(fd), native(fds[1]) == NULL,
			       native(-1) == NULL);
			uint32_t stride = 0;
			void *data = NULL;
			void *map = gbm_bo_map(bo, 8, 16, 32, 4, GBM_BO_TRANSFER_READ_WRITE, &stride, &data);
			printf("map=%d stride=%u\n", map == pixels, stride);
			if (map)
				gbm_bo_unmap(bo, data);
			printf("write=%d\n", gbm_bo_write(bo, pixels, sizeof pixels));
			struct gbm_import_fd_data imp = {.fd = fd, .width = 1080, .height = 2400, .stride = 4352,
							 .format = GBM_FORMAT_ARGB8888};
			printf("import=%d\n", gbm_bo_import(gbm, GBM_BO_IMPORT_FD, &imp, GBM_BO_USE_RENDERING) != NULL);
			close(fd);
			close(pfd);
			int again = dup(fds[2]);
			gbm_bo_destroy(bo);
			printf("destroyed released=%d native-after=%d locked=%d unlocked=%d\n", released,
			       native(again) != NULL, locked, unlocked);
			close(again);
		} else if (!strcmp(a, "surface")) {
			struct gbm_surface *s = gbm_surface_create(gbm, 720, 1600, GBM_FORMAT_XRGB8888,
								   GBM_BO_USE_SCANOUT | GBM_BO_USE_RENDERING);
			if (!s) {
				printf("surface=none created=%d\n", created);
				continue;
			}
			struct gbm_bo *f1 = gbm_surface_lock_front_buffer(s);
			printf("free=%d\n", gbm_surface_has_free_buffers(s));
			gbm_surface_release_buffer(s, f1);
			struct gbm_bo *f2 = gbm_surface_lock_front_buffer(s);
			gbm_surface_release_buffer(s, f2);
			struct gbm_bo *f3 = gbm_surface_lock_front_buffer(s);
			printf("surface=%ux%u created=%d swapped=%d back-again=%d\n", gbm_bo_get_width(f1),
			       gbm_bo_get_height(f1), created, f1 != f2, f1 == f3);
			gbm_surface_destroy(s);
			printf("surface-destroyed released=%d\n", released);
		}
	}
	gbm_device_destroy(gbm);
	printf("device-destroyed\n");
	return 0;
}
"""


@pytest.fixture(scope="module")
def mesa(tmp_path_factory):
    import subprocess
    from conftest import compile_c
    d = tmp_path_factory.mktemp("gbm-mesa")
    (d / "fdinfo").mkdir()
    (d / "backends").mkdir()
    copy = sandboxed_source(SRC, d, [
        ('"/proc/self/fdinfo/%d"', f'"{d}/fdinfo/%d"'),
        ("char path[64], line[128];", "char path[512], line[128];"),
    ])
    # As the PKGBUILD builds it; gralloc's functions come from the program that loads it
    so = compile_c(d / "backends" / "hybris_gbm.so", [copy],
                   flags=["-fPIC", "-shared", "-fvisibility=hidden", "-lpthread"], pkgs=("gbm", "egl"))
    (d / "client.c").write_text(CLIENT)
    exe = compile_c(d / "client", [d / "client.c"], flags=["-rdynamic", f'-DBASE="{d}"', "-ldl"],
                    pkgs=("gbm", "egl"))
    env = {"PATH": "/usr/bin:/bin", "GBM_BACKENDS_PATH": str(d / "backends"), "GBM_BACKEND": "hybris",
           "LC_ALL": "C"}
    exports = subprocess.run(["nm", "-D", "--defined-only", str(so)], capture_output=True, text=True).stdout
    return functools.partial(run, exe, env=env), exports


def test_mesa_loads_the_backend(mesa):
    client, exports = mesa
    out = kv(client("formats").stdout)
    assert out["device"] == "hybris fd-kept=1"
    assert out["xrgb"] == "1 abgr=1 nv12=0 planes=1"
    assert out["native-exported"] == "1"


def test_only_the_entry_points_are_exported(mesa):
    client, exports = mesa
    names = {line.split()[-1] for line in exports.splitlines() if " T " in line}
    names = {n for n in names if not n.startswith(("_", "gcov", "mangle_path"))}
    assert names == {"gbmint_get_backend", "hybris_gbm_native_buffer"}


@pytest.mark.parametrize("kind", ["bo", "bo-modifiers"])
def test_mesa_buffer(mesa, kind):
    client, exports = mesa
    out = client("lock-ok", kind).stdout
    k = kv(out)
    # Linear (modifier 0) whatever the compositor asks for; the stride in bytes
    assert k["bo"] == f"1080x2400 stride={1088 * 4} format=0x34325241 planes=1 offset=0 modifier=0"
    assert k["plane-stride"] == f"{1088 * 4} handle=1 plane-handle=1"
    # Every fd handed out is a dup of the large dma-buf, the caller's to close
    assert k["fd-is-dmabuf"] == "1 plane-fd-is-dmabuf=1 own-fds=1"
    assert k["native"] == "0x1001 unknown=1 closed=1"
    # Mapped for the CPU through gralloc's lock, at the region asked for
    assert "lock usage=0x22 8,16 32x4" in out
    assert k["map"] == f"1 stride={1088 * 4}"
    assert k["write"] == "-1" and k["import"] == "0"
    assert k["destroyed released"] == "1 native-after=0 locked=1 unlocked=1"
    assert out.splitlines()[-1] == "device-destroyed"
    # gralloc is set up once, for the first buffer
    assert out.count("gralloc-init 0") == 1


def test_mesa_buffer_that_cannot_be_mapped(mesa):
    client, exports = mesa
    k = kv(client("bo").stdout)
    assert k["map"] == "0 stride=0"
    assert k["destroyed released"] == "1 native-after=0 locked=1 unlocked=0"


def test_mesa_surface(mesa):
    client, exports = mesa
    k = kv(client("surface").stdout)
    assert k["free"] == "1"
    # Two buffers, the front one swapped with the back one at each lock
    assert k["surface"] == "720x1600 created=2 swapped=1 back-again=1"
    assert k["surface-destroyed released"] == "2"


def test_mesa_gralloc_failing(mesa):
    client, exports = mesa
    k = kv(client("gralloc-fails", "bo", "surface").stdout)
    assert k["bo"] == "none created=0 released=0"
    assert k["surface"] == "none created=0"


# ---- Out of memory and other failures that leave nothing behind ----

def test_out_of_memory_for_a_buffer(built):
    out = kv(built(f"dmabuf-size:{10**9}", "oom:1", "alloc:16,16,34325258").stdout)
    # Nothing allocated from gralloc before the bo itself exists
    assert out["alloc"] == "failed created=0 released=0"


def test_out_of_memory_for_a_surface(built):
    out = kv(built(f"dmabuf-size:{10**9}", "oom:1", "surface-unused").stdout)
    assert out["surface"] == "none created=0 released=0"


def test_surface_without_a_buffer(built):
    # Its first buffer cannot be had: no surface, and no leak (the sanitizer would say so)
    out = kv(built(f"dmabuf-size:{10**9}", "oom:2", "surface-unused").stdout)
    assert out["surface"] == "none created=0 released=0"


def test_surface_never_shown(built):
    # Destroyed before a frame: only its back buffer existed
    out = kv(built(f"dmabuf-size:{10**9}", "surface-unused").stdout)
    assert out["surface-destroyed released"] == "1"


def test_out_of_memory_for_the_device(built):
    assert kv(built("oom:1", "device").stdout)["device"] == "none"
    assert kv(built("device").stdout)["device"] == "hybris"


def test_dmabuf_that_cannot_be_examined(built):
    out = kv(built(f"dmabuf-size:{10**9}", "fstat-fails", "alloc:16,16,34325258", "dup-closed").stdout)
    # The gralloc buffer is released and the fd taken for it closed again
    assert out["alloc"] == "failed created=1 released=1"
    assert out["dup-closed"] == "1"
