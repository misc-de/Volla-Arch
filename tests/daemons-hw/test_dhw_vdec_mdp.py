# plinius-vdec, the MDP itself (mdp.c, in plinius-vdec-mdpd): libdpframework is loaded once
# through libhybris into a linker namespace of its own, and each frame becomes the vendor's
# DpBlitStream calls with the plane sizes and pitches the MDP checks. libhybris is a stand-in
# (libhybris-common.so.1 in tmp, found through LD_LIBRARY_PATH) that logs every call with its
# arguments and hands out a fake DpBlitStream; each failure on the way gives its own message.

import os
import subprocess

import pytest

from conftest import compile_c
from dhw_harness import PKG, build_harness, run

VDEC = PKG / "plinius-vdec"

FAKE_HYBRIS = r"""
#define _GNU_SOURCE
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <unistd.h>

typedef struct {
	uint64_t flags;
	void *reserved_addr;
	size_t reserved_size;
	int relro_fd;
	int library_fd;
	off64_t library_fd_offset;
	void *library_namespace;
} AndroidDlextinfo;

static int ns_token, lib_token;

static void say(const char *fmt, ...)
{
	FILE *f = fopen(getenv("FAKE_LOG"), "a");
	va_list args;
	va_start(args, fmt);
	vfprintf(f, fmt, args);
	va_end(args);
	fputc('\n', f);
	fclose(f);
}

/* FAKE_FAIL=<step>:<code>: that DpBlitStream call returns code */
static int result(const char *step)
{
	const char *f = getenv("FAKE_FAIL");
	size_t n = strlen(step);
	if (f && !strncmp(f, step, n) && f[n] == ':')
		return atoi(f + n + 1);
	return 0;
}

static const char *fd_name(int fd)
{
	static char target[4][256];
	static int k;
	char path[64];
	char *t = target[k++ % 4];
	snprintf(path, sizeof path, "/proc/self/fd/%d", fd);
	ssize_t n = readlink(path, t, 255);
	t[n < 0 ? 0 : n] = 0;
	return strrchr(t, '/') ? strrchr(t, '/') + 1 : t;
}

static void ctor(void *self)
{
	*(int *)self = 0x5eed;
	say("ctor");
}

static void dtor(void *self)
{
	say("dtor %s", *(int *)self == 0x5eed ? "constructed" : "garbage");
}

static int set_src_buffer(void *self, int fd, unsigned *sizes, unsigned planes)
{
	say("setSrcBuffer %s %u %u planes %u", fd_name(fd), sizes[0], sizes[1], planes);
	return result("setSrcBuffer");
}

static int set_dst_buffer(void *self, int fd, unsigned *sizes, unsigned planes)
{
	say("setDstBuffer %s %u %u planes %u", fd_name(fd), sizes[0], sizes[1], planes);
	return result("setDstBuffer");
}

static int set_src_config(void *self, int w, int h, int ypitch, int uvpitch, unsigned color, int profile,
			  int interlace, void *roi, int secure, int flush)
{
	say("setSrcConfig %dx%d pitch %d/%d color 0x%08X profile %d interlace %d roi %s secure %d flush %d", w, h,
	    ypitch, uvpitch, color, profile, interlace, roi ? "set" : "none", secure, flush);
	return result("setSrcConfig");
}

static int set_dst_config(void *self, int w, int h, int ypitch, int uvpitch, unsigned color, int profile,
			  int interlace, void *roi, int secure, int flush)
{
	say("setDstConfig %dx%d pitch %d/%d color 0x%08X profile %d interlace %d roi %s secure %d flush %d", w, h,
	    ypitch, uvpitch, color, profile, interlace, roi ? "set" : "none", secure, flush);
	return result("setDstConfig");
}

static int invalidate(void *self, struct timeval *end)
{
	say("invalidate %s", end ? "timed" : "untimed");
	return result("invalidate");
}

#ifndef NO_NAMESPACES
void *android_create_namespace(const char *name, const char *ld_path, const char *default_path, uint64_t type,
			       const char *permitted, void *parent)
{
	say("namespace %s search %s default %s type %llu permitted %s parent %s", name, ld_path, default_path,
	    (unsigned long long)type, permitted ? permitted : "none", parent ? "set" : "none");
	return getenv("FAKE_NS_FAIL") ? NULL : &ns_token;
}
#endif

void *android_dlopen_ext(const char *name, int flags, const AndroidDlextinfo *ext)
{
	say("dlopen %s flags 0x%x ext 0x%llx namespace %s", name, flags, (unsigned long long)ext->flags,
	    ext->library_namespace == &ns_token ? "ours" : "other");
	return getenv("FAKE_LIB_FAIL") ? NULL : &lib_token;
}

void *hybris_dlsym(void *handle, const char *name)
{
	static const struct { const char *name; void *fn; } syms[] = {
		{"_ZN12DpBlitStreamC1Ev", ctor},
		{"_ZN12DpBlitStreamD1Ev", dtor},
		{"_ZN12DpBlitStream12setSrcBufferEiPjj", set_src_buffer},
		{"_ZN12DpBlitStream12setDstBufferEiPjj", set_dst_buffer},
		{"_ZN12DpBlitStream12setSrcConfigEiiii13DP_COLOR_ENUM15DP_PROFILE_ENUM17DpInterlaceFormatP6DpRect8DpSecureb",
		 set_src_config},
		{"_ZN12DpBlitStream12setDstConfigEiiii13DP_COLOR_ENUM15DP_PROFILE_ENUM17DpInterlaceFormatP6DpRect8DpSecureb",
		 set_dst_config},
		{"_ZN12DpBlitStream10invalidateEP7timeval", invalidate},
	};
	if (handle != &lib_token)
		return NULL;
	if (getenv("FAKE_MISSING") && !strcmp(name, getenv("FAKE_MISSING")))
		return NULL;
	for (size_t i = 0; i < sizeof syms / sizeof syms[0]; i++)
		if (!strcmp(name, syms[i].name))
			return syms[i].fn;
	return NULL;
}

const char *hybris_dlerror(void)
{
	return getenv("FAKE_DLERROR");
}
"""

HARNESS = r"""
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <gio/gio.h>
#include "mdp.h"

/* SRC SRC_SIZE CODED W H DST PITCH [FRAMES]: opens the MDP twice (loaded once), converts */
int main(int argc, char **argv)
{
	g_autoptr(GError) error = NULL;
	int src = open(argv[1], O_RDWR), dst = open(argv[6], O_RDWR);
	unsigned coded = atoi(argv[3]), height = atoi(argv[5]);
	printf("src_size=%u\n", pv_mdp_hyfbc_src_size(coded, height));
	pv_mdp_free(NULL);
	for (int round = 0; round < 2; round++) {
		PvMdp *mdp = pv_mdp_new(&error);
		if (!mdp) {
			printf("error %s (%s)\n", error->message,
			       error->code == G_IO_ERROR_NOT_SUPPORTED ? "not supported" : "other");
			g_clear_error(&error);
			continue;
		}
		int frames = argc > 8 ? atoi(argv[8]) : 1;
		for (int i = 0; i < frames; i++) {
			if (pv_mdp_hyfbc_to_nv12(mdp, src, strtoul(argv[2], NULL, 10), coded, atoi(argv[4]), height, dst,
						 atoi(argv[7]), &error))
				puts("ok");
			else {
				printf("error %s\n", error->message);
				g_clear_error(&error);
			}
		}
		pv_mdp_free(mdp);
	}
	return 0;
}
"""

MDP_CALLS = {
    "setSrcBuffer": "_ZN12DpBlitStream12setSrcBufferEiPjj",
    "invalidate": "_ZN12DpBlitStream10invalidateEP7timeval",
}


def src_bytes(coded_width, height):
    ypitch = coded_width * 12 // 8
    return ypitch * height + ypitch // 2 * (height // 2)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("mdp")
    libs = {}
    for variant, flags in (("full", []), ("old", ["-DNO_NAMESPACES"])):
        (d / variant).mkdir()
        c = d / f"fake-hybris-{variant}.c"
        c.write_text(FAKE_HYBRIS)
        libs[variant] = compile_c(d / variant / "libhybris-common.so.1", [c],
                                  flags=["-shared", "-fPIC", *flags], counted=False).parent
    (d / "none").mkdir()
    libs["none"] = d / "none"
    exe = build_harness(d, "mdp", HARNESS, flags=[f"-I{VDEC}", str(VDEC / "mdp.c"), "-ldl",
                                                  "-Wno-unused-parameter"], pkgs=("gio-2.0",))
    return exe, libs


def mdp(built, tmp_path, *args, lib="full", **fake):
    exe, libs = built
    log = tmp_path / "hybris.log"
    if log.exists():
        log.unlink()
    env = dict(os.environ, LD_LIBRARY_PATH=str(libs[lib]), FAKE_LOG=str(log),
               **{k: str(v) for k, v in fake.items()})
    out = run(exe, *args, env=env).stdout.splitlines()
    return out, log.read_text().splitlines() if log.exists() else []


def buffers(tmp_path, coded=1280, h=720, pitch=1280):
    src, dst = tmp_path / "hyfbc", tmp_path / "nv12"
    src.write_bytes(b"\0" * src_bytes(coded, h))
    dst.write_bytes(b"\0" * (pitch * h * 3 // 2))
    return [src, str(src_bytes(coded, h)), str(coded)], [dst, str(pitch)]


def test_loaded_once_into_a_namespace_and_converted(built, tmp_path):
    s, d = buffers(tmp_path, coded=1296, h=736, pitch=1296)
    out, log = mdp(built, tmp_path, *s, 1280, 720, *d, 2)
    assert out == [f"src_size={src_bytes(1296, 720)}", "ok", "ok", "ok", "ok"]
    # The vendor's dependencies live in mt6878 and the VNDK apex: a shared namespace of its own
    assert log[0] == ("namespace plinius-vdec search /vendor/lib64/mt6878:/vendor/lib64:/system/lib64:"
                      "/apex/com.android.vndk.v34/lib64 default /vendor/lib64:/system/lib64 type 2 "
                      "permitted none parent none")
    assert log[1] == "dlopen libdpframework.so flags 0x2 ext 0x200 namespace ours"
    ypitch = 1296 * 12 // 8
    one_frame = [
        f"setSrcBuffer hyfbc {ypitch * 720} {ypitch // 2 * 360} planes 2",
        f"setSrcConfig 1296x720 pitch {ypitch}/{ypitch // 2} color 0x402C0C4C profile 0 interlace 0 roi none "
        "secure 0 flush 1",
        f"setDstBuffer nv12 {1296 * 720} {1296 * 360} planes 2",
        "setDstConfig 1296x720 pitch 1296/1296 color 0x004C084C profile 0 interlace 0 roi none secure 0 flush 1",
        "invalidate untimed",
    ]
    # One stream per pv_mdp_new, constructed before and destroyed after its frames; the library
    # is not loaded again for the second
    stream = ["ctor", *one_frame, *one_frame, "dtor constructed"]
    assert log[2:] == stream + stream


def test_a_source_too_small_never_reaches_the_mdp(built, tmp_path):
    s, d = buffers(tmp_path)
    s[1] = str(src_bytes(1280, 720) - 1)
    out, log = mdp(built, tmp_path, *s, 1280, 720, *d)
    assert out[1] == f"error HyFBC buffer of {src_bytes(1280, 720) - 1} bytes is too small for 1280x720"
    assert not [line for line in log if line.startswith("set")]


def test_whole_decoded_lines_or_nothing(built, tmp_path):
    # 2026-11-21: given the visible width, the RDMA read a 720 wide picture with 768 wide lines
    # sheared; whole lines are converted now, so a destination with shorter lines is refused
    s, d = buffers(tmp_path, coded=768, h=1280, pitch=720)
    out, log = mdp(built, tmp_path, *s, 720, 1280, *d)
    assert out[1] == "error pitch 720 is narrower than the decoded lines (768)"
    assert not [line for line in log if line.startswith("set")]


@pytest.mark.parametrize("step", ["setSrcBuffer", "setSrcConfig", "setDstBuffer", "setDstConfig", "invalidate"])
def test_each_failing_call_stops_the_frame(built, tmp_path, step):
    s, d = buffers(tmp_path)
    out, log = mdp(built, tmp_path, *s, 1280, 720, *d, FAKE_FAIL=f"{step}:-7")
    assert out[1:] == ["error MDP conversion failed (-7)"] * 2
    calls = [line.split()[0] for line in log if line.split()[0] in
             ("setSrcBuffer", "setSrcConfig", "setDstBuffer", "setDstConfig", "invalidate")]
    order = ["setSrcBuffer", "setSrcConfig", "setDstBuffer", "setDstConfig", "invalidate"]
    assert calls == order[:order.index(step) + 1] * 2


def no_real_libhybris():
    r = subprocess.run(["sh", "-c", "ldconfig -p 2>/dev/null | grep -q libhybris-common.so.1"])
    return r.returncode != 0


@pytest.mark.parametrize("lib,fake,message", [
    pytest.param("none", {}, "libhybris: libhybris-common.so.1: cannot open shared object file",
                 marks=pytest.mark.skipif(not no_real_libhybris(), reason="the host has libhybris")),
    ("old", {}, "libhybris lacks the namespace functions"),
    ("full", {"FAKE_NS_FAIL": 1}, "libdpframework: not loadable"),
    ("full", {"FAKE_LIB_FAIL": 1, "FAKE_DLERROR": "dlopen failed: library \"libion.so\" not found"},
     "libdpframework: dlopen failed: library \"libion.so\" not found"),
    ("full", {"FAKE_MISSING": MDP_CALLS["invalidate"]}, f"libdpframework lacks {MDP_CALLS['invalidate']}"),
    ("full", {"FAKE_MISSING": MDP_CALLS["setSrcBuffer"]}, f"libdpframework lacks {MDP_CALLS['setSrcBuffer']}"),
])
def test_why_the_mdp_cannot_be_used(built, tmp_path, lib, fake, message):
    s, d = buffers(tmp_path)
    out, log = mdp(built, tmp_path, *s, 1280, 720, *d, lib=lib, **fake)
    # Both attempts get the reason of the one load
    assert len(out) == 3
    for line in out[1:]:
        assert line.startswith("error " + message) and line.endswith("(not supported)")
    assert "ctor" not in log
    assert len([line for line in log if line.startswith("namespace")]) <= 1
