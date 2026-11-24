/* SPDX-License-Identifier: MIT */
/*
 * The vcodec decoder of the Dimensity 7300 always writes MediaTek's compressed HyFBC format.
 * Android turns it into linear YUV with the MDP (libdpframework's DpBlitStream); this does the
 * same from Linux, in plinius-vdec-mdpd only (see mdp-proto.h): the plugin asks the daemon.
 * libhybris is opened at run time, and libdpframework goes into a linker namespace of its own: its dependencies live in
 * /vendor/lib64/mt6878 and the VNDK apex, which the session's HYBRIS_LD_LIBRARY_PATH does not
 * name, and the default namespace refuses vendor libraries.
 *
 * Values found on the device (2026-11-10): DP_COLOR_NV12_HYFBC 0x402C0C4C, DP_COLOR_NV12
 * 0x004C084C; the source Y pitch is width * 12 / 8 and the MDP checks each plane against
 * pitch * height although the RDMA computes the HyFBC layout by itself.
 */
#define _GNU_SOURCE
#include "mdp.h"
#include "mdp-proto.h"

#include <dlfcn.h>
#include <gio/gio.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/time.h>
#include <sys/types.h>

#define DP_COLOR_NV12       0x004C084Cu
#define DP_COLOR_NV12_HYFBC 0x402C0C4Cu
#define DP_PROFILE_BT601    0
#define DP_INTERLACE_NONE   0
#define DP_SECURE_NONE      0

/* Android's codec2 allocates 0xb88 bytes for a shared_ptr<DpBlitStream>. */
#define DP_BLIT_STREAM_SIZE 8192

#define ANDROID_NAMESPACE_TYPE_SHARED 2
#define ANDROID_DLEXT_USE_NAMESPACE   0x200

typedef struct {
	uint64_t flags;
	void *reserved_addr;
	size_t reserved_size;
	int relro_fd;
	int library_fd;
	off64_t library_fd_offset;
	void *library_namespace;
} AndroidDlextinfo;

static const char NS_SEARCH_PATH[] =
	"/vendor/lib64/mt6878:/vendor/lib64:/system/lib64:/apex/com.android.vndk.v34/lib64";

static struct {
	void (*ctor)(void *self);
	void (*dtor)(void *self);
	int (*set_src_buffer)(void *self, int fd, unsigned *sizes, unsigned planes);
	int (*set_dst_buffer)(void *self, int fd, unsigned *sizes, unsigned planes);
	int (*set_src_config)(void *self, int w, int h, int ypitch, int uvpitch, unsigned color,
		int profile, int interlace, void *roi, int secure, int flush);
	int (*set_dst_config)(void *self, int w, int h, int ypitch, int uvpitch, unsigned color,
		int profile, int interlace, void *roi, int secure, int flush);
	int (*invalidate)(void *self, struct timeval *end);
	char *error;
} api;

struct PvMdp {
	void *stream;
};

static gpointer load_api(gpointer unused)
{
	void *hybris = dlopen("libhybris-common.so.1", RTLD_NOW | RTLD_GLOBAL);
	if (!hybris) {
		api.error = g_strdup_printf("libhybris: %s", dlerror());
		return NULL;
	}
	void *(*create_ns)(const char *, const char *, const char *, uint64_t, const char *, void *) =
		dlsym(hybris, "android_create_namespace");
	void *(*dlopen_ext)(const char *, int, const AndroidDlextinfo *) =
		dlsym(hybris, "android_dlopen_ext");
	void *(*hsym)(void *, const char *) = dlsym(hybris, "hybris_dlsym");
	const char *(*herror)(void) = dlsym(hybris, "hybris_dlerror");
	if (!create_ns || !dlopen_ext || !hsym || !herror) {
		api.error = g_strdup("libhybris lacks the namespace functions");
		return NULL;
	}
	void *ns = create_ns("plinius-vdec", NS_SEARCH_PATH, "/vendor/lib64:/system/lib64",
		ANDROID_NAMESPACE_TYPE_SHARED, NULL, NULL);
	AndroidDlextinfo ext = { .flags = ANDROID_DLEXT_USE_NAMESPACE, .library_namespace = ns };
	void *lib = ns ? dlopen_ext("libdpframework.so", RTLD_NOW, &ext) : NULL;
	if (!lib) {
		api.error = g_strdup_printf("libdpframework: %s", herror() ? herror() : "not loadable");
		return NULL;
	}
	static const struct { const char *name; size_t offset; } syms[] = {
		{ "_ZN12DpBlitStreamC1Ev", G_STRUCT_OFFSET(typeof(api), ctor) },
		{ "_ZN12DpBlitStreamD1Ev", G_STRUCT_OFFSET(typeof(api), dtor) },
		{ "_ZN12DpBlitStream12setSrcBufferEiPjj", G_STRUCT_OFFSET(typeof(api), set_src_buffer) },
		{ "_ZN12DpBlitStream12setDstBufferEiPjj", G_STRUCT_OFFSET(typeof(api), set_dst_buffer) },
		{ "_ZN12DpBlitStream12setSrcConfigEiiii13DP_COLOR_ENUM15DP_PROFILE_ENUM"
		  "17DpInterlaceFormatP6DpRect8DpSecureb", G_STRUCT_OFFSET(typeof(api), set_src_config) },
		{ "_ZN12DpBlitStream12setDstConfigEiiii13DP_COLOR_ENUM15DP_PROFILE_ENUM"
		  "17DpInterlaceFormatP6DpRect8DpSecureb", G_STRUCT_OFFSET(typeof(api), set_dst_config) },
		{ "_ZN12DpBlitStream10invalidateEP7timeval", G_STRUCT_OFFSET(typeof(api), invalidate) },
	};
	for (guint i = 0; i < G_N_ELEMENTS(syms); i++) {
		void *p = hsym(lib, syms[i].name);
		if (!p) {
			api.error = g_strdup_printf("libdpframework lacks %s", syms[i].name);
			return NULL;
		}
		G_STRUCT_MEMBER(void *, &api, syms[i].offset) = p;
	}
	return GINT_TO_POINTER(1);
}

PvMdp *pv_mdp_new(GError **error)
{
	static GOnce once = G_ONCE_INIT;
	if (!g_once(&once, load_api, NULL)) {
		g_set_error_literal(error, G_IO_ERROR, G_IO_ERROR_NOT_SUPPORTED, api.error);
		return NULL;
	}
	PvMdp *mdp = g_new0(PvMdp, 1);
	mdp->stream = g_malloc0(DP_BLIT_STREAM_SIZE);
	api.ctor(mdp->stream);
	return mdp;
}

void pv_mdp_free(PvMdp *mdp)
{
	if (!mdp)
		return;
	api.dtor(mdp->stream);
	g_free(mdp->stream);
	g_free(mdp);
}

static guint src_ypitch(guint coded_width)
{
	return coded_width * 12 / 8;
}

guint pv_mdp_hyfbc_src_size(guint coded_width, guint height)
{
	return (guint)pv_mdp_src_bytes(coded_width, height);
}

gboolean pv_mdp_hyfbc_to_nv12(PvMdp *mdp, int src_fd, guint src_size, guint coded_width,
	guint width, guint height, int dst_fd, guint dst_pitch, GError **error)
{
	guint ypitch = src_ypitch(coded_width);
	unsigned src_sizes[2] = { ypitch * height, ypitch / 2 * (height / 2) };
	unsigned dst_sizes[2] = { dst_pitch * height, dst_pitch * (height / 2) };
	int r;

	/* The RDMA lays the HyFBC blocks out by the width it is given: given the visible width of a
	 * picture whose lines are wider (720 in 768), it reads the blocks of each line from the wrong
	 * place and the picture comes out sheared, with green steps (measured 2026-11-21). So the MDP
	 * converts whole decoded lines, and the destination's video meta shows the visible part. */
	if (dst_pitch < coded_width) {
		g_set_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT,
			"pitch %u is narrower than the decoded lines (%u)", dst_pitch, coded_width);
		return FALSE;
	}
	if (src_size < src_sizes[0] + src_sizes[1]) {
		g_set_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT,
			"HyFBC buffer of %u bytes is too small for %ux%u", src_size, width, height);
		return FALSE;
	}
	if ((r = api.set_src_buffer(mdp->stream, src_fd, src_sizes, 2)) ||
	    (r = api.set_src_config(mdp->stream, coded_width, height, ypitch, ypitch / 2,
		DP_COLOR_NV12_HYFBC, DP_PROFILE_BT601, DP_INTERLACE_NONE, NULL, DP_SECURE_NONE, 1)) ||
	    (r = api.set_dst_buffer(mdp->stream, dst_fd, dst_sizes, 2)) ||
	    (r = api.set_dst_config(mdp->stream, coded_width, height, dst_pitch, dst_pitch,
		DP_COLOR_NV12, DP_PROFILE_BT601, DP_INTERLACE_NONE, NULL, DP_SECURE_NONE, 1)) ||
	    (r = api.invalidate(mdp->stream, NULL))) {
		g_set_error(error, G_IO_ERROR, G_IO_ERROR_FAILED, "MDP conversion failed (%d)", r);
		return FALSE;
	}
	return TRUE;
}
