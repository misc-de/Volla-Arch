# camera-hal-bridge: turning the sensor image upright (plane rotation), YUV images from the
# image reader to NV21 (strides, planar and semi-planar layouts, short planes), the rotation of
# photos and of the video transform for how the phone is held, what is read from the camera
# characteristics, and the JPEG written for TakePicture. The NDK is never loaded: its function
# pointers point to fakes in the harness; libhybris is a stand-in (fakes/hybris/).

import functools

import pytest

from dhw_harness import PKG, build_harness, kv, run, sandboxed_source

SRC = PKG / "camera-hal-bridge" / "camera-hal-bridge.c"

HARNESS = r"""
#define _GNU_SOURCE
#include <glib.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define main real_main
#include "camera-hal-bridge.c"
#undef main
#define WIDTH 1280 /* the default stream size */
#define HEIGHT 720
#define FRAME_SIZE NV21_SIZE(WIDTH, HEIGHT)

/* An image of the reader: three planes as the HAL lays them out */
struct AImage {
	int32_t width, height;
	uint8_t *data[3];
	int size[3];
	int32_t row_stride[3], pixel_stride[3];
};
static int img_width(const AImage *i, int32_t *v) { *v = i->width; return 0; }
static int img_height(const AImage *i, int32_t *v) { *v = i->height; return 0; }
static int img_data(const AImage *i, int p, uint8_t **d, int *n) { *d = i->data[p]; *n = i->size[p]; return 0; }
static int img_row(const AImage *i, int p, int32_t *v) { *v = i->row_stride[p]; return 0; }
static int img_pixel(const AImage *i, int p, int32_t *v) { *v = i->pixel_stride[p]; return 0; }

/* Characteristics: a few entries */
static uint8_t facing_value;
static int32_t orientation_value, ae_range[2] = {-12, 12}, configs[32];
static int n_configs = 16;
static uint8_t meta_scene_modes[] = {0, 1, 5, 40};
static ACameraMetadata_rational ae_step = {1, 6};
static uint8_t mfb[2] = {0, 1};
static int meta_entry(const ACameraMetadata *m, uint32_t tag, ACameraMetadata_const_entry *e)
{
	memset(e, 0, sizeof *e);
	switch (tag) {
	case TAG_LENS_FACING: e->count = 1; e->data.u8 = &facing_value; return 0;
	case TAG_SENSOR_ORIENTATION: e->count = 1; e->data.i32 = &orientation_value; return 0;
	case TAG_CONTROL_AVAILABLE_SCENE_MODES: e->count = 4; e->data.u8 = meta_scene_modes; return 0;
	case TAG_MTK_MFB_AVAILABLE: e->count = 2; e->data.u8 = mfb; return 0;
	case TAG_CONTROL_AE_COMPENSATION_RANGE: e->count = 2; e->data.i32 = ae_range; return 0;
	case TAG_CONTROL_AE_COMPENSATION_STEP: e->count = 1; e->data.r = &ae_step; return 0;
	case TAG_SCALER_AVAILABLE_STREAM_CONFIGURATIONS: e->count = n_configs; e->data.i32 = configs; return 0;
	}
	return -1;
}

static struct bridge b;

/* A 1280x720 test picture: Y = x + y, U = 2x + y, V = x + 3y (mod 256), in the given layout */
static AImage *make_image(const char *layout, int y_stride, int c_stride, int shorten)
{
	AImage *im = calloc(1, sizeof *im);
	im->width = WIDTH;
	im->height = HEIGHT;
	im->row_stride[0] = y_stride;
	im->pixel_stride[0] = 1;
	im->size[0] = y_stride * (HEIGHT - 1) + WIDTH - shorten;
	im->data[0] = calloc(1, y_stride * HEIGHT);
	for (int y = 0; y < HEIGHT; y++)
		for (int x = 0; x < WIDTH; x++)
			im->data[0][y * y_stride + x] = x + y;
	int semi = !strcmp(layout, "nv21");
	int ps = semi ? 2 : 1;
	im->row_stride[1] = im->row_stride[2] = c_stride;
	im->pixel_stride[1] = im->pixel_stride[2] = ps;
	int csize = c_stride * (HEIGHT / 2 - 1) + (WIDTH / 2 - 1) * ps + 1;
	if (semi) {
		/* One buffer V U V U ...: v at offset 0, u at offset 1 */
		uint8_t *vu = calloc(1, c_stride * HEIGHT / 2 + 1);
		for (int y = 0; y < HEIGHT / 2; y++)
			for (int x = 0; x < WIDTH / 2; x++) {
				vu[y * c_stride + 2 * x] = x + 3 * y;
				vu[y * c_stride + 2 * x + 1] = 2 * x + y;
			}
		im->data[2] = vu;
		im->data[1] = vu + 1;
	} else {
		im->data[1] = calloc(1, c_stride * HEIGHT / 2);
		im->data[2] = calloc(1, c_stride * HEIGHT / 2);
		for (int y = 0; y < HEIGHT / 2; y++)
			for (int x = 0; x < WIDTH / 2; x++) {
				im->data[1][y * c_stride + x] = 2 * x + y;
				im->data[2][y * c_stride + x] = x + 3 * y;
			}
	}
	im->size[1] = im->size[2] = csize;
	return im;
}

int main(int argc, char **argv)
{
	AImage_getWidth = img_width;
	AImage_getHeight = img_height;
	AImage_getPlaneData = img_data;
	AImage_getPlaneRowStride = img_row;
	AImage_getPlanePixelStride = img_pixel;
	ACameraMetadata_getConstEntry = meta_entry;
	struct camera c = {.bridge = &b, .sensor_width = WIDTH, .sensor_height = HEIGHT};
	snprintf(c.name, sizeof c.name, "test");
	for (int i = 1; i < argc; i++) {
		g_auto(GStrv) a = g_strsplit(argv[i], ":", -1);
		if (!strcmp(a[0], "rotate")) {
			/* rotate:<w>:<h>:<degrees>: a numbered plane, printed row by row */
			int w = atoi(a[1]), h = atoi(a[2]), deg = atoi(a[3]);
			uint8_t *src = malloc(w * h), *dst = malloc(w * h);
			uint16_t *src16 = malloc(2 * w * h), *dst16 = malloc(2 * w * h);
			for (int k = 0; k < w * h; k++)
				src[k] = k, src16[k] = 1000 + k;
			rotate_plane8(src, dst, w, h, deg);
			rotate_plane16(src16, dst16, w, h, deg);
			printf("rotated=");
			for (int k = 0; k < w * h; k++)
				printf("%d,", dst[k]);
			printf("\nrotated16=");
			for (int k = 0; k < w * h; k++)
				printf("%d,", dst16[k] - 1000);
			printf("\n");
			free(src); free(dst); free(src16); free(dst16);
		} else if (!strcmp(a[0], "copy")) {
			/* copy:<nv21|i420>:<y stride>:<chroma stride>:<bytes missing from Y> */
			AImage *im = make_image(a[1], atoi(a[2]), atoi(a[3]), atoi(a[4]));
			uint8_t *dst = malloc(FRAME_SIZE);
			bool ok = copy_image(&c, im, dst);
			int bad = 0;
			for (int y = 0; ok && y < HEIGHT; y++)
				for (int x = 0; x < WIDTH; x++)
					bad += dst[y * WIDTH + x] != (uint8_t)(x + y);
			for (int y = 0; ok && y < HEIGHT / 2; y++)
				for (int x = 0; x < WIDTH / 2; x++) {
					bad += dst[WIDTH * HEIGHT + y * WIDTH + 2 * x] != (uint8_t)(x + 3 * y);     /* V */
					bad += dst[WIDTH * HEIGHT + y * WIDTH + 2 * x + 1] != (uint8_t)(2 * x + y); /* U */
				}
			printf("copy=%d wrong=%d\n", ok, bad);
			free(dst);
		} else if (!strcmp(a[0], "rotation")) {
			/* rotation:<facing front 0/1>:<sensor orientation>:<device rotation> */
			c.facing = atoi(a[1]) ? FACING_FRONT : FACING_BACK;
			c.orientation = atoi(a[2]);
			atomic_store(&b.device_rotation, atoi(a[3]));
			printf("picture=%d transform=%d\n", picture_rotation(&c), frame_transform(&c));
		} else if (!strcmp(a[0], "characteristics")) {
			facing_value = atoi(a[1]);
			orientation_value = atoi(a[2]);
			/* with ":sizes", YUV stream sizes too: 720p and 4K, 1080p only as an input */
			int32_t list[32] = {STREAM_FORMAT_BLOB, 4000, 3000, 0, STREAM_FORMAT_BLOB, 8000, 6000, 1,
					    0x23, 9000, 9000, 0, STREAM_FORMAT_BLOB, 4624, 3472, 0,
					    0x23, 3840, 2160, 0, 0x23, 1920, 1080, 1, 0x23, 1280, 720, 0,
					    0x22, 1920, 1080, 0};
			memcpy(configs, list, sizeof list);
			n_configs = a[3] && !strcmp(a[3], "sizes") ? 32 : 16;
			read_characteristics(&c, NULL);
			printf("chars=facing %d orientation %d scenes %#x mfb %d ae %d..%d step %.4f picture %dx%d sizes",
			       c.facing, c.orientation, c.scene_modes, c.has_mfb, c.ae_min, c.ae_max, c.ae_step,
			       c.picture_width, c.picture_height);
			for (int k = 0; k < c.n_sizes; k++)
				printf(" %dx%d", c.sizes[k].width, c.sizes[k].height);
			printf("\n");
		} else if (!strcmp(a[0], "jpeg")) {
			static const uint8_t data[] = {0xff, 0xd8, 1, 2, 3, 0xff, 0xd9};
			char *slash = strrchr(a[1], '/');
			*slash = '\0';
			int dir = open(a[1], O_RDONLY | O_DIRECTORY);
			printf("jpeg=%d\n", dir < 0 ? -errno : write_jpeg(dir, slash + 1, data, sizeof data));
			if (dir >= 0)
				close(dir);
		} else if (!strcmp(a[0], "port")) {
			int fd = open(FRONT_PORT_STATUS, O_RDONLY);
			printf("port=%d\n", front_port_active(fd));
			if (fd >= 0)
				close(fd);
		}
	}
	return 0;
}
"""


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("camera")
    sandboxed_source(SRC, d, [(
        '"/sys/devices/platform/soc/1a00e000.seninf-top/1a00e000.seninf-top:seninf-csi-port-1/"',
        f'"{d}/front-port/"'), ('#define PROC_DIR "/proc/"', f'#define PROC_DIR "{d}/proc/"')])
    exe = build_harness(d, "camera", HARNESS, flags=[f"-I{SRC.parent}"], pkgs=("libpipewire-0.3", "libsystemd", "glib-2.0"))
    return d, functools.partial(run, exe)


@pytest.fixture
def cam(built):
    return built[1]


def rotate_ref(w, h, deg):
    """Clockwise rotation of a w x h plane numbered row by row (reference)"""
    src = [[y * w + x for x in range(w)] for y in range(h)]
    if deg == 0:
        return [v for row in src for v in row]
    if deg == 90:      # new w = h: dst[y'][x'] = src[h - 1 - x'][y']
        return [src[h - 1 - xp][yp] for yp in range(w) for xp in range(h)]
    if deg == 180:
        return [src[h - 1 - y][w - 1 - x] for y in range(h) for x in range(w)]
    if deg == 270:     # dst[y'][x'] = src[x'][w - 1 - y']
        return [src[xp][w - 1 - yp] for yp in range(w) for xp in range(h)]


@pytest.mark.parametrize("w,h", [(4, 3), (40, 35), (1, 5)])
@pytest.mark.parametrize("deg", [0, 90, 180, 270])
def test_rotate_planes(cam, w, h, deg):
    out = kv(cam(f"rotate:{w}:{h}:{deg}").stdout)
    expected = rotate_ref(w, h, deg)
    assert [int(v) for v in out["rotated"].rstrip(",").split(",")] == [v % 256 for v in expected]
    assert [int(v) for v in out["rotated16"].rstrip(",").split(",")] == expected


@pytest.mark.parametrize("layout,y_stride,c_stride", [
    ("nv21", 1280, 1280),    # tight, the fast path
    ("nv21", 1344, 1344),    # padded rows
    ("i420", 1280, 640),     # planar chroma
    ("i420", 1408, 768),
])
def test_copy_image_to_nv21(cam, layout, y_stride, c_stride):
    assert kv(cam(f"copy:{layout}:{y_stride}:{c_stride}:0").stdout)["copy"] == "1 wrong=0"


def test_copy_image_refuses_short_planes(cam):
    # A plane smaller than its strides say: refused, nothing read beyond it (ASan)
    assert kv(cam("copy:nv21:1344:1344:1").stdout)["copy"].startswith("0 ")


@pytest.mark.parametrize("front,sensor,device,picture,transform", [
    # JPEG orientation as Android computes it: sensor + device for the back camera, sensor -
    # device for the (mirrored) front camera; device: clockwise from portrait (left edge up: 90)
    (0, 90, 0, 90, 0),       # back camera, portrait
    (0, 90, 90, 180, 1),     # left edge up: one quarter more, viewfinder turned 90
    (0, 90, 180, 270, 2),
    (0, 90, 270, 0, 3),
    (1, 270, 0, 270, 0),     # front camera, portrait
    (1, 270, 90, 180, 3),    # the other way round
    (1, 270, 270, 0, 1),
])
def test_picture_rotation_and_transform(cam, front, sensor, device, picture, transform):
    # transform: SPA_META_TRANSFORMATION_None 0, _90 1, _180 2, _270 3
    out = kv(cam(f"rotation:{front}:{sensor}:{device}").stdout)
    assert out["picture"] == f"{picture} transform={transform}"


def test_characteristics(cam):
    out = kv(cam("characteristics:0:270").stdout)
    # Scene mode 40 does not fit the mask and is left out; the largest output JPEG (8000x6000 is
    # an input configuration, 9000x9000 is not a JPEG); no YUV stream size listed: the default
    assert out["chars"] == ("facing 1 orientation 270 scenes 0x23 mfb 1 ae -12..12 step 0.1667 "
                            "picture 4624x3472 sizes 1280x720")
    assert kv(cam("characteristics:1:90").stdout)["chars"].startswith("facing 0 orientation 90 ")


def test_the_stream_sizes_are_the_yuv_outputs_the_hal_lists(cam):
    # 720p and 4K as YUV outputs; 1080p only as an input and as another format; 9000x9000 is no
    # stream size of ours. From the smallest, which is the default
    out = kv(cam("characteristics:0:90:sizes").stdout)
    assert out["chars"].endswith("picture 4624x3472 sizes 1280x720 3840x2160")


def test_write_jpeg_atomically(built):
    d, cam = built
    target = d / "photo.jpg"
    target.unlink(missing_ok=True)
    assert kv(cam(f"jpeg:{target}").stdout)["jpeg"] == "0"
    assert target.read_bytes() == bytes([0xff, 0xd8, 1, 2, 3, 0xff, 0xd9])
    assert not (d / "photo.jpg.part").exists()
    # Never onto a file that is there, nor through a symlink as the name or as the part
    target.write_bytes(b"old")
    assert kv(cam(f"jpeg:{target}").stdout)["jpeg"] == "-17"   # -EEXIST
    assert target.read_bytes() == b"old" and not (d / "photo.jpg.part").exists()
    outside = d / "outside"
    outside.write_bytes(b"keys")
    link, part = d / "link.jpg", d / "part.jpg.part"
    for p in (link, part):
        p.unlink(missing_ok=True)
        p.symlink_to(outside)
    assert kv(cam(f"jpeg:{link}").stdout)["jpeg"] == "-17"
    assert kv(cam(f"jpeg:{d}/part.jpg").stdout)["jpeg"] == "-17"
    assert outside.read_bytes() == b"keys" and link.is_symlink() and part.is_symlink()
    assert not (d / "part.jpg").exists() and not (d / "link.jpg.part").exists()


def test_front_port(built):
    d, cam = built
    p = d / "front-port/power"
    p.mkdir(parents=True, exist_ok=True)
    (p / "runtime_status").write_text("active\n")
    assert kv(cam("port").stdout)["port"] == "1"
    (p / "runtime_status").write_text("suspended\n")
    assert kv(cam("port").stdout)["port"] == "0"
    (p / "runtime_status").unlink()
    assert kv(cam("port").stdout)["port"] == "0"
