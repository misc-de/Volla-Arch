# plinius-face recognition chain (face-pipeline.c) without the NPU: plinius-tflite is replaced by
# scripted models that record what the chain writes into their inputs and hand back the outputs
# a test sets. What is checked: which models load from where with which options, how a camera
# frame (NV21, any sensor orientation) is cut into the models' inputs (colour conversion, padding,
# BGR for YuNet, quantization), how YuNet's grids decode into the face's box and points in the
# upright frame, the first and the second (whole frame) search, Face Mesh's landmarks and
# presence, the alignment to SFace's template and the normalized embedding, and every way a
# model or a tensor can fail.

import math

import pytest

from dhw_harness import PKG, build_harness, run

FACE = PKG / "plinius-face"
TFLITE = PKG / "plinius-tflite"

# SFace's template in the 112×112 crop (face-pipeline.c, embed_template)
TEMPLATE = [(38.2946, 51.6963), (73.5318, 51.5014), (56.0252, 71.7366), (41.5493, 92.3655),
            (70.7299, 92.2041)]

HARNESS = r"""
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <plinius-tflite.h>

/* ---- Scripted plinius-tflite ---- */

#define MAX_OUT 12
#define MAX_CELLS 16

typedef struct {
	int pass, level, cell;
	float cls, obj, box[4], kps[10];
} Cell;

struct PlTfl {
	char name[16];
	PlTflTensorInfo in;
	void *in_data;
	int nout;
	PlTflTensorInfo out[MAX_OUT];
	float *out_data[MAX_OUT];
	int invokes;
	int fail_invoke;   /* invoke number that fails (1-based), 0 never */
	int no_input;
	int bad_output;    /* output index +1 that has the wrong type, 0 none */
	int short_output;  /* output index +1 one value short, 0 none */
	int null_output;   /* output index +1 that is missing, 0 none */
};

static struct {
	char fail_open[16];
	PlTflType in_type[3];
	float in_scale[3];
	int in_zero[3];
	int in_bytes_off[3];
	Cell cells[MAX_CELLS];
	int ncells;
	float presence;
	float landmarks[478 * 3];
	float embedding[128];
	int npu[3], total[3];
	PlTfl *models[3];
	int quiet;
} cfg = { .in_type = { PLTFL_FLOAT32, PLTFL_FLOAT32, PLTFL_FLOAT32 } };

static int
model_index (const char *path)
{
	if (strstr (path, "yunet"))
		return 0;
	if (strstr (path, "landmarks"))
		return 1;
	return 2;
}

static size_t
type_size (PlTflType type)
{
	return type == PLTFL_FLOAT32 || type == PLTFL_INT32 ? 4 : 1;
}

void
pltfl_options_init (PlTflOptions *options)
{
	memset (options, 0, sizeof *options);
}

PlTfl *
pltfl_open (const char *path, const PlTflOptions *options, char **error)
{
	int k = model_index (path);
	static const int sides[3] = { 640, 256, 112 };
	PlTfl *m;
	size_t count = (size_t) sides[k] * sides[k] * 3;

	printf ("open %s accel=%s fp16=%d cache=%s\n", path,
		options->accelerator ? options->accelerator : "-", options->allow_fp16,
		options->cache_dir ? options->cache_dir : "-");
	if (cfg.fail_open[0] && strstr (path, cfg.fail_open)) {
		*error = strdup ("no such model");
		return NULL;
	}
	m = calloc (1, sizeof *m);
	snprintf (m->name, sizeof m->name, "%s", k == 0 ? "detect" : k == 1 ? "mesh" : "embed");
	m->in.type = cfg.in_type[k];
	m->in.bytes = count * type_size (cfg.in_type[k]) + cfg.in_bytes_off[k];
	m->in.scale = cfg.in_scale[k];
	m->in.zero_point = cfg.in_zero[k];
	m->in_data = calloc (1, m->in.bytes + 16);
	if (k == 0) {
		static const int strides[3] = { 8, 16, 32 };
		static const int per[4] = { 1, 1, 4, 10 };

		m->nout = 12;
		for (int o = 0; o < 12; o++) {
			int cols = 640 / strides[o % 3];

			m->out[o].type = PLTFL_FLOAT32;
			m->out[o].bytes = (size_t) cols * cols * per[o / 3] * sizeof (float);
		}
	} else if (k == 1) {
		m->nout = 2;
		m->out[0] = (PlTflTensorInfo) { .type = PLTFL_FLOAT32, .bytes = 478 * 3 * sizeof (float) };
		m->out[1] = (PlTflTensorInfo) { .type = PLTFL_FLOAT32, .bytes = sizeof (float) };
	} else {
		m->nout = 1;
		m->out[0] = (PlTflTensorInfo) { .type = PLTFL_FLOAT32, .bytes = 128 * sizeof (float) };
	}
	for (int o = 0; o < m->nout; o++)
		m->out_data[o] = calloc (1, m->out[o].bytes);
	cfg.models[k] = m;
	return m;
}

void
pltfl_close (PlTfl *m)
{
	if (!m)
		return;
	if (!cfg.quiet)
		printf ("close %s\n", m->name);
	for (int k = 0; k < 3; k++)
		if (cfg.models[k] == m)
			cfg.models[k] = NULL;
	for (int o = 0; o < m->nout; o++)
		free (m->out_data[o]);
	free (m->in_data);
	free (m);
}

void *
pltfl_input (PlTfl *m, int index, PlTflTensorInfo *info)
{
	if (m->no_input || index != 0)
		return NULL;
	*info = m->in;
	return m->in_data;
}

const void *
pltfl_output (PlTfl *m, int index, PlTflTensorInfo *info)
{
	if (index >= m->nout || m->null_output == index + 1)
		return NULL;
	*info = m->out[index];
	if (m->bad_output == index + 1)
		info->type = PLTFL_UINT8;
	if (m->short_output == index + 1)
		info->bytes -= sizeof (float);
	return m->out_data[index];
}

int
pltfl_invoke (PlTfl *m)
{
	m->invokes++;
	if (m->fail_invoke == m->invokes)
		return 1;
	if (!strcmp (m->name, "detect")) {
		for (int o = 0; o < 12; o++)
			memset (m->out_data[o], 0, m->out[o].bytes);
		for (int c = 0; c < cfg.ncells; c++) {
			Cell *cell = &cfg.cells[c];

			if (cell->pass != m->invokes)
				continue;
			m->out_data[cell->level][cell->cell] = cell->cls;
			m->out_data[3 + cell->level][cell->cell] = cell->obj;
			memcpy (m->out_data[6 + cell->level] + 4 * cell->cell, cell->box, sizeof cell->box);
			memcpy (m->out_data[9 + cell->level] + 10 * cell->cell, cell->kps, sizeof cell->kps);
		}
	} else if (!strcmp (m->name, "mesh")) {
		memcpy (m->out_data[0], cfg.landmarks, sizeof cfg.landmarks);
		m->out_data[1][0] = cfg.presence;
	} else {
		memcpy (m->out_data[0], cfg.embedding, sizeof cfg.embedding);
	}
	return 0;
}

int
pltfl_npu_nodes (PlTfl *m)
{
	return cfg.npu[model_index (m->name[0] == 'd' ? "yunet" : m->name[0] == 'm' ? "landmarks" : "x")];
}

int
pltfl_total_nodes (PlTfl *m)
{
	return cfg.total[model_index (m->name[0] == 'd' ? "yunet" : m->name[0] == 'm' ? "landmarks" : "x")];
}

void
pltfl_exit (int status)
{
	fflush (stdout);
	exit (status);
}

/* Out of memory for the pipeline itself */
static int fail_calloc;

static void *
pipeline_calloc (size_t n, size_t size)
{
	return fail_calloc ? NULL : calloc (n, size);
}

#define calloc pipeline_calloc
#include "face-pipeline.c"
#undef calloc

/* ---- Frames ---- */

static uint8_t *frame_data;
static FaceFrame frame;

/* Luma: "flat:Y", "grad" (x + 2y, mod 256 per byte) or "xy" (x*7 + y*3 mod 251) */
static void
make_frame (int w, int h, int orientation, const char *luma, int v, int u)
{
	free (frame_data);
	frame_data = malloc ((size_t) w * h * 3 / 2);
	for (int y = 0; y < h; y++)
		for (int x = 0; x < w; x++) {
			int value;

			if (!strncmp (luma, "flat:", 5))
				value = atoi (luma + 5);
			else if (!strcmp (luma, "grad"))
				value = (x + 2 * y) & 255;
			else
				value = (x * 7 + y * 3) % 251;
			frame_data[(size_t) y * w + x] = (uint8_t) value;
		}
	memset (frame_data + (size_t) w * h, 128, (size_t) w * h / 2);
	for (int cy = 0; cy < h / 2; cy++)
		for (int cx = 0; cx < w / 2; cx++) {
			uint8_t *vu = frame_data + (size_t) w * h + ((size_t) cy * (w / 2) + cx) * 2;

			/* v < 0: V = −v only in the last chroma column and row */
			vu[0] = (uint8_t) (v >= 0 ? v : cx == w / 2 - 1 || cy == h / 2 - 1 ? -v : 128);
			vu[1] = (uint8_t) u;
		}
	frame = (FaceFrame) { frame_data, w, h, orientation };
}

static double
input_value (PlTfl *m, size_t k)
{
	switch (m->in.type) {
	case PLTFL_FLOAT32: return ((float *) m->in_data)[k];
	case PLTFL_UINT8: return ((uint8_t *) m->in_data)[k];
	case PLTFL_INT8: return ((int8_t *) m->in_data)[k];
	default: return NAN;
	}
}

static PlTfl *
model_by_name (const char *name)
{
	return cfg.models[!strcmp (name, "detect") ? 0 : !strcmp (name, "mesh") ? 1 : 2];
}

int
main (void)
{
	static char line[4096];
	FacePipeline *pipeline = NULL;
	static FaceResult result;

	setvbuf (stdout, NULL, _IOLBF, 0);
	while (fgets (line, sizeof line, stdin)) {
		char a[512], b[64];
		int n1, n2, n3, n4, n5;
		float f[16];

		line[strcspn (line, "\n")] = '\0';
		if (sscanf (line, "failopen %15s", a) == 1) {
			snprintf (cfg.fail_open, sizeof cfg.fail_open, "%s", a);
		} else if (sscanf (line, "nodes %d %d %d %d %d %d", &cfg.npu[0], &cfg.total[0], &cfg.npu[1],
				   &cfg.total[1], &cfg.npu[2], &cfg.total[2]) == 6) {
		} else if (sscanf (line, "intype %d %d %f %d %d", &n1, &n2, &f[0], &n3, &n4) == 5) {
			/* model, PlTflType, scale, zero point, bytes off */
			cfg.in_type[n1] = n2;
			cfg.in_scale[n1] = f[0];
			cfg.in_zero[n1] = n3;
			cfg.in_bytes_off[n1] = n4;
		} else if (sscanf (line, "new %511s", a) == 1) {
			PlTflOptions options;
			char *error = NULL;

			pltfl_options_init (&options);
			options.accelerator = "mtk-neuron";
			options.allow_fp16 = 1;
			options.cache_dir = "cachedir";
			pipeline = face_pipeline_new (a, &options, &error);
			if (pipeline)
				printf ("describe %s\n", face_pipeline_describe (pipeline));
			else
				printf ("error %s\n", error ? error : "(no message)");
			free (error);
		} else if (!strcmp (line, "free")) {
			face_pipeline_free (pipeline);
			pipeline = NULL;
		} else if (!strcmp (line, "oom")) {
			fail_calloc = 1;
		} else if (!strcmp (line, "freenull")) {
			face_pipeline_free (NULL);
			puts ("ok");
		} else if (sscanf (line, "frame %d %d %d %63s %d %d", &n1, &n2, &n3, b, &n4, &n5) == 6) {
			make_frame (n1, n2, n3, b, n4, n5);
		} else if (sscanf (line, "cell %d %d %d %f %f %f %f %f %f %f %f %f %f %f %f %f %f %f %f",
				   &n1, &n2, &n3, &f[0], &f[1], &f[2], &f[3], &f[4], &f[5], &f[6], &f[7],
				   &f[8], &f[9], &f[10], &f[11], &f[12], &f[13], &f[14], &f[15]) == 19) {
			/* pass level cell cls obj box[4] kps[10] */
			Cell *c = &cfg.cells[cfg.ncells++];

			c->pass = n1;
			c->level = n2;
			c->cell = n3;
			c->cls = f[0];
			c->obj = f[1];
			memcpy (c->box, f + 2, sizeof c->box);
			memcpy (c->kps, f + 6, sizeof c->kps);
		} else if (sscanf (line, "presence %f", &f[0]) == 1) {
			cfg.presence = f[0];
		} else if (sscanf (line, "landmark %d %f %f %f", &n1, &f[0], &f[1], &f[2]) == 4) {
			cfg.landmarks[3 * n1] = f[0];
			cfg.landmarks[3 * n1 + 1] = f[1];
			cfg.landmarks[3 * n1 + 2] = f[2];
		} else if (sscanf (line, "embedding %d %f", &n1, &f[0]) == 2) {
			cfg.embedding[n1] = f[0];
		} else if (sscanf (line, "break %63s %511s %d", b, a, &n1) == 3) {
			PlTfl *m = model_by_name (b);

			if (!strcmp (a, "invoke"))
				m->fail_invoke = n1;
			else if (!strcmp (a, "input"))
				m->no_input = n1;
			else if (!strcmp (a, "type"))
				m->bad_output = n1;
			else if (!strcmp (a, "short"))
				m->short_output = n1;
			else if (!strcmp (a, "null"))
				m->null_output = n1;
		} else if (!strcmp (line, "run")) {
			int found = face_pipeline_run (pipeline, &frame, &result);

			printf ("found %d invokes %d %d %d\n", found, cfg.models[0]->invokes,
				cfg.models[1]->invokes, cfg.models[2]->invokes);
			if (found > 0) {
				printf ("score %.4f\n", result.score);
				printf ("box %.3f %.3f %.3f %.3f\n", result.box[0], result.box[1], result.box[2],
					result.box[3]);
				for (int p = 0; p < 5; p++)
					printf ("kp %d %.3f %.3f\n", p, result.keypoints[p].x, result.keypoints[p].y);
				printf ("presence %.6g\n", result.presence);
				printf ("mesh0 %.3f %.3f %.4f\n", result.mesh[0].x, result.mesh[0].y,
					result.mesh_z[0]);
				printf ("emb %.4f %.4f %.4f\n", result.embedding[0], result.embedding[1],
					result.embedding[2]);
				printf ("times %d\n", result.ms_detect >= 0 && result.ms_mesh >= 0 &&
							      result.ms_embed >= 0);
			}
		} else if (sscanf (line, "input %63s %d %d %d", b, &n1, &n2, &n3) == 4) {
			/* model, i, j, side: the three channels of patch pixel (i, j) */
			PlTfl *m = model_by_name (b);
			size_t k = ((size_t) n2 * n3 + n1) * 3;

			printf ("in %g %g %g\n", input_value (m, k), input_value (m, k + 1),
				input_value (m, k + 2));
		} else if (sscanf (line, "sim %f %f %f %f", &f[0], &f[1], &f[2], &f[3]) == 4) {
			float x[128] = { f[0], f[1] }, y[128] = { f[2], f[3] };

			printf ("sim %.4f\n", face_similarity (x, y));
		} else {
			printf ("<unknown command: %.60s>\n", line);
		}
	}
	cfg.quiet = 1;
	face_pipeline_free (pipeline);
	free (frame_data);
	return 0;
}
"""


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    d = tmp_path_factory.mktemp("face-pipeline")
    return build_harness(d, "face-pipeline", HARNESS, pkgs=(),
                         flags=[f"-I{FACE}", f"-I{TFLITE}"])


def script(harness, tmp_path, *cmds):
    """Runs the commands (on stdin, which run() has no argument for); returns the output lines."""
    f = tmp_path / "cmds"
    f.write_text("\n".join(cmds) + "\n")
    r = run("sh", "-c", f'exec "$0" < "$1"', harness, f)
    assert "<unknown command" not in r.stdout, r.stdout
    return r.stdout.splitlines()


def found_line(lines):
    return next(line for line in lines if line.startswith("found "))


def values(lines, key):
    for line in lines:
        if line.startswith(key + " "):
            return [float(v) for v in line.split()[1:]]
    raise AssertionError(f"{key} not in {lines}")


def cell_cmd(pass_, level, cell, cls, obj, box, kps):
    return "cell " + " ".join(str(v) for v in (pass_, level, cell, cls, obj, *box, *kps))


# A face at stride 8, cell 0 of a 640×640 detector input whose five points are SFace's template:
# with an upright frame of 640×640 the detector's pixels are the frame's, so the alignment for
# SFace is the identity
TEMPLATE_KPS = [v / 8 for p in TEMPLATE for v in p]
TEMPLATE_FACE = cell_cmd(1, 0, 0, 0.95, 0.95, [7, 9, 1.5, 2.0], TEMPLATE_KPS)


# ---- Loading ----

def test_models_load_from_the_directory_with_the_options(harness, tmp_path):
    out = script(harness, tmp_path, "nodes 165 165 0 300 12 40", "new /models", "free")
    assert out == [
        "open /models/face_detection_yunet_2023mar_float32.tflite accel=mtk-neuron fp16=1 "
        "cache=cachedir",
        "open /models/face_landmarks_detector.tflite accel=mtk-neuron fp16=1 cache=cachedir",
        "open /models/face_recognition_sface_2021dec_float32.tflite accel=mtk-neuron fp16=1 "
        "cache=cachedir",
        "describe detect 165/165, mesh 0/300, embed 12/40 operations on the NPU",
        "close detect", "close mesh", "close embed",
    ]


@pytest.mark.parametrize("missing,closed", [
    ("yunet", []), ("landmarks", ["close detect"]), ("sface", ["close detect", "close mesh"])])
def test_a_missing_model_fails_and_closes_the_others(harness, tmp_path, missing, closed):
    out = script(harness, tmp_path, f"failopen {missing}", "new /m", "freenull")
    opened = [line for line in out if line.startswith("open ")]
    assert opened[-1].split()[1].split("/")[-1].startswith(
        {"yunet": "face_detection", "landmarks": "face_landmarks",
         "sface": "face_recognition"}[missing])
    assert [line for line in out if line.startswith("close")] == closed
    assert "error no such model" in out
    assert out[-1] == "ok"


# ---- Finding the face ----

def test_no_face_searches_the_middle_square_then_the_whole_frame(harness, tmp_path):
    # 1280×720 turned by 270°: upright 720×1280, first the 720×720 square in the middle, then
    # 1280×1280 around the frame, with black left and right of it
    out = script(harness, tmp_path, "new /m", "frame 1280 720 270 flat:200 128 128", "run",
                 "input detect 0 320 640", "input detect 639 320 640")
    assert out[-3] == "found 0 invokes 2 0 0"
    # The second pass: 280 px of padding on both sides of 720 px in 1280 → the edges are black
    assert values(out, "in") == [0, 0, 0]
    assert out[-1] == "in 0 0 0"


def test_first_pass_covers_the_middle_square_without_padding(harness, tmp_path):
    out = script(harness, tmp_path, "new /m", "frame 1280 720 270 flat:200 128 128",
                 cell_cmd(1, 0, 0, 0.95, 0.95, [0.5, 0.5, 2, 2], [0.5] * 10), "run",
                 "input detect 0 0 640", "input detect 639 639 640")
    assert found_line(out).startswith("found 1 invokes 1 1 1")
    assert out[-2:] == ["in 200 200 200", "in 200 200 200"]


def test_face_in_the_second_pass(harness, tmp_path):
    # Upright 720×1280: side 1280, offset (−280, 0), one detector pixel is 2 frame pixels
    out = script(harness, tmp_path, "new /m", "frame 1280 720 270 flat:100 128 128",
                 cell_cmd(2, 2, 3 * 20 + 10, 0.95, 0.95, [0.5, 0.25, 0, 0], [0] * 10), "run")
    assert found_line(out).startswith("found 1 invokes 2 1 1")
    k = 2.0
    ox = -280 + 0.5 * k - 0.5
    oy = 0.5 * k - 0.5
    cx, cy, w = (10 + 0.5) * 32, (3 + 0.25) * 32, 32
    assert values(out, "box") == pytest.approx(
        [ox + (cx - w / 2) * k, oy + (cy - w / 2) * k, ox + (cx + w / 2) * k, oy + (cy + w / 2) * k],
        abs=1e-3)
    assert values(out, "kp")[1:] == pytest.approx([ox + 10 * 32 * k, oy + 3 * 32 * k], abs=1e-3)
    assert values(out, "score") == pytest.approx([0.95], abs=1e-4)


def test_best_cell_over_all_strides_wins(harness, tmp_path):
    out = script(harness, tmp_path, "new /m", "frame 640 640 0 flat:100 128 128",
                 cell_cmd(1, 0, 5, 0.92, 0.92, [0, 0, 0, 0], [0] * 10),
                 cell_cmd(1, 1, 7, 0.97, 0.97, [0, 0, 0, 0], [0] * 10),
                 cell_cmd(1, 2, 1, 0.94, 0.94, [0, 0, 0, 0], [0] * 10),
                 # Below the detector's 0.9: never, however big
                 cell_cmd(1, 2, 2, 0.89, 0.89, [0, 0, 0, 0], [0] * 10),
                 "run")
    assert values(out, "score") == pytest.approx([0.97], abs=1e-4)
    # Cell 7 of the 40×40 grid at stride 16: column 7, row 0; box of exp(0)·16 around its corner
    assert values(out, "box") == pytest.approx([7 * 16 - 8, -8, 7 * 16 + 8, 8], abs=1e-3)


@pytest.mark.parametrize("cls,obj,found", [
    (0.9, 0.9, 1),       # the limit itself
    (0.89, 0.9, 0),
    # Scores outside 0..1 are clamped: 1 · 0.81 → 0.9
    (5.0, 0.81, 1),
    (-1.0, 1.0, 0),
])
def test_detector_score(harness, tmp_path, cls, obj, found):
    out = script(harness, tmp_path, "new /m", "frame 640 640 0 flat:100 128 128",
                 cell_cmd(1, 0, 0, cls, obj, [0, 0, 0, 0], [0] * 10),
                 cell_cmd(2, 0, 0, cls, obj, [0, 0, 0, 0], [0] * 10), "run")
    assert found_line(out).startswith(f"found {found} ")


# ---- What the models get ----

def test_colour_conversion_bgr_for_yunet_rgb_for_the_others(harness, tmp_path):
    # V (Cr) 178: R = Y + 1.402·50, G = Y − 0.714136·50, B = Y
    out = script(harness, tmp_path, "new /m", "frame 640 640 0 flat:100 178 128", TEMPLATE_FACE,
                 "presence 0", "run", "input detect 320 320 640", "input mesh 128 128 256",
                 "input embed 56 56 112")
    r, g, b = 100 + 1.402 * 50, 100 - 0.714136 * 50, 100
    assert [float(v) for v in out[-3].split()[1:]] == [round(b), round(g), round(r)]
    assert [float(v) for v in out[-2].split()[1:]] == pytest.approx(
        [round(r) / 255, round(g) / 255, round(b) / 255], abs=1e-6)
    assert [float(v) for v in out[-1].split()[1:]] == [round(r), round(g), round(b)]


@pytest.mark.parametrize("luma,v,u,rgb", [
    (250, 178, 128, (255, 214, 250)),     # red clips at 255
    (10, 178, 128, (80, 0, 10)),          # green clips at 0
    (128, 128, 228, (128, 94, 255)),      # U (Cb) 228: B = Y + 1.772·100 clips, G = Y − 34.4
])
def test_colour_values_are_clamped(harness, tmp_path, luma, v, u, rgb):
    out = script(harness, tmp_path, "new /m", f"frame 640 640 0 flat:{luma} {v} {u}",
                 TEMPLATE_FACE, "run", "input embed 56 56 112")
    assert [float(x) for x in out[-1].split()[1:]] == list(rgb)


@pytest.mark.parametrize("orientation", [0, 90, 180, 270])
def test_alignment_to_the_template_and_sensor_orientation(harness, tmp_path, orientation):
    # The five points on SFace's template: the 112×112 crop is the upright frame's corner
    # pixel by pixel. Upright (u, v) comes from the sensor at the rotation's inverse.
    pts = [(10, 5), (100, 20), (37, 90), (0, 0), (111, 111)]
    cmds = ["new /m", f"frame 640 640 {orientation} xy 128 128", TEMPLATE_FACE, "run"]
    cmds += [f"input embed {i} {j} 112" for i, j in pts]
    out = script(harness, tmp_path, *cmds)
    w1 = h1 = 639

    def sensor(u, v):
        return {0: (u, v), 90: (v, h1 - u), 180: (w1 - u, h1 - v), 270: (w1 - v, u)}[orientation]

    for (i, j), line in zip(pts, out[-len(pts):]):
        x, y = sensor(i, j)
        expected = (x * 7 + y * 3) % 251
        assert [float(c) for c in line.split()[1:]] == pytest.approx([expected] * 3, abs=1.01), (i, j)


def test_luma_is_interpolated_between_pixels(harness, tmp_path):
    # YuNet's first pass over a 1280×1280 upright frame: one detector pixel is two frame pixels,
    # its centre lies between them: the mean of the gradient's neighbours
    out = script(harness, tmp_path, "new /m", "frame 1280 1280 0 grad 128 128", "run",
                 "input detect 10 0 640")
    # u = 2·10 + 0.5, v = 0.5: (x + 2y) at (20.5, 0.5) = 21.5 → rounds to 22
    assert out[-1] == "in 22 22 22"


@pytest.mark.parametrize("model,args,expected", [
    # uint8 with scale 0.5, zero point 10: 100/0.5 + 10 = 210; 170.1/0.5 + 10 clips at 255
    (0, "3 0.5 10 0", ["in 210 138 255"]),
    # int8 with scale 1, zero point −100: 100 → 0, 64 → −36, 170 → 70
    (0, "9 1 -100 0", ["in 0 -36 70"]),
    # int8 clipping: 170 + 100 → 127; zero point −250 → −128
    (2, "9 1 100 0", ["in 127 127 127"]),
])
def test_quantized_inputs(harness, tmp_path, model, args, expected):
    out = script(harness, tmp_path, f"intype {model} {args}", "new /m",
                 "frame 640 640 0 flat:100 178 128", TEMPLATE_FACE, "run",
                 "input detect 320 320 640" if model == 0 else "input embed 56 56 112")
    assert out[-1:] == expected


def test_int8_clips_low(harness, tmp_path):
    out = script(harness, tmp_path, "intype 2 9 1 -250 0", "new /m",
                 "frame 640 640 0 flat:100 128 128", TEMPLATE_FACE, "run", "input embed 56 56 112")
    assert out[-1] == "in -128 -128 -128"


def test_uint8_clips_low(harness, tmp_path):
    out = script(harness, tmp_path, "intype 2 3 1 -150 0", "new /m",
                 "frame 640 640 0 flat:100 128 128", TEMPLATE_FACE, "run", "input embed 56 56 112")
    assert out[-1] == "in 0 0 0"


@pytest.mark.parametrize("model,args", [
    (0, "1 0 0 4"),     # float32 of the wrong size
    (0, "3 0.5 0 1"),   # uint8 of the wrong size
    (0, "3 0 0 0"),     # uint8 without a scale
    (1, "2 0 0 0"),     # int32: no such input for these models
    (2, "9 0 0 0"),
])
def test_unusable_input_is_an_error(harness, tmp_path, model, args):
    out = script(harness, tmp_path, f"intype {model} {args}", "new /m",
                 "frame 640 640 0 flat:100 128 128", TEMPLATE_FACE, "run")
    # The chain stops at the model whose input does not fit, before running it
    invokes = [1] * model + [0] * (3 - model)
    assert found_line(out) == "found -1 invokes " + " ".join(map(str, invokes))


# ---- Landmarks and embedding ----

def test_landmarks_presence_and_embedding(harness, tmp_path):
    # Level eyes (points 0 and 1 of the template are 0.19 px apart in y: nearly level)
    out = script(harness, tmp_path, "new /m", "frame 640 640 0 flat:100 128 128", TEMPLATE_FACE,
                 "presence 2", "landmark 0 128 128 10", "embedding 0 3", "embedding 1 -4", "run")
    # Box of exp(1.5)·8 × exp(2)·8 around (7·8, 9·8)
    bw, bh = math.exp(1.5) * 8, math.exp(2.0) * 8
    box = [56 - bw / 2, 72 - bh / 2, 56 + bw / 2, 72 + bh / 2]
    assert values(out, "box") == pytest.approx(box, abs=1e-3)
    assert values(out, "kp")[1:] == pytest.approx(list(TEMPLATE[0]), abs=1e-3)
    assert values(out, "presence") == pytest.approx([1 / (1 + math.exp(-2))], rel=1e-5)
    # The patch's centre (128, 128) is the box's centre; depth in frame pixels
    k = max(bw, bh) * 1.5 / 256
    assert values(out, "mesh0") == pytest.approx([56, 72, 10 * k], abs=0.05)
    assert values(out, "emb") == pytest.approx([0.6, -0.8, 0], abs=1e-5)
    assert out[-1] == "times 1"


def test_turned_face_is_levelled_for_face_mesh(harness, tmp_path):
    # Eyes at 45°: the patch is turned with them, so a landmark to the right of the patch's
    # centre lies down-right of the box centre in the frame
    kps = [10, 10, 20, 20, 15, 20, 10, 25, 20, 25]
    out = script(harness, tmp_path, "new /m", "frame 640 640 0 flat:100 128 128",
                 cell_cmd(1, 0, 0, 0.95, 0.95, [10, 10, 0, 0], [v / 8 for v in kps]),
                 "landmark 0 228 128 0", "run")
    k = 8 * 1.5 / 256
    c = 10 * 8
    assert values(out, "mesh0")[:2] == pytest.approx(
        [c + 100 * k * math.cos(math.pi / 4), c + 100 * k * math.sin(math.pi / 4)], abs=0.05)


@pytest.mark.parametrize("logit,presence", [(1000, 1.0), (-1000, 0.0), (-5, 1 / (1 + math.exp(5)))])
def test_presence_of_extreme_logits(harness, tmp_path, logit, presence):
    out = script(harness, tmp_path, "new /m", "frame 640 640 0 flat:100 128 128", TEMPLATE_FACE,
                 f"presence {logit}", "run")
    assert values(out, "presence") == pytest.approx([presence], rel=1e-4, abs=1e-30)


def test_zero_embedding_stays_zero(harness, tmp_path):
    out = script(harness, tmp_path, "new /m", "frame 640 640 0 flat:100 128 128", TEMPLATE_FACE,
                 "run")
    assert values(out, "emb") == [0, 0, 0]


def test_all_points_in_one_place_do_not_break_the_alignment(harness, tmp_path):
    out = script(harness, tmp_path, "new /m", "frame 640 640 0 xy 128 128",
                 cell_cmd(1, 0, 0, 0.95, 0.95, [0, 0, 0, 0], [5] * 10), "embedding 2 1", "run",
                 "input embed 0 0 112")
    assert values(out, "emb") == [0, 0, 1]
    # No rotation or scale: the crop is the frame shifted by the points' mean to the template's
    in_ = values(out, "in")
    assert all(0 <= v <= 255 for v in in_)


# ---- Failures ----

@pytest.mark.parametrize("model,how,n,invokes", [
    ("detect", "invoke", 1, "1 0 0"),
    ("detect", "invoke", 2, "2 0 0"),      # the second search
    ("detect", "input", 1, "0 0 0"),
    ("detect", "type", 1, "1 0 0"),        # cls at stride 8 not float
    ("detect", "short", 4, "1 0 0"),       # obj one value short
    ("detect", "null", 7, "1 0 0"),        # no bbox
    ("detect", "null", 12, "1 0 0"),       # no keypoints at stride 32
    ("mesh", "invoke", 1, "1 1 0"),
    ("mesh", "input", 1, "1 0 0"),
    ("mesh", "short", 1, "1 1 0"),
    ("mesh", "type", 2, "1 1 0"),
    ("embed", "invoke", 1, "1 1 1"),
    ("embed", "input", 1, "1 1 0"),
    ("embed", "null", 1, "1 1 1"),
])
def test_model_failures(harness, tmp_path, model, how, n, invokes):
    # The detector finds a face in the first pass, unless it fails there; for the second pass's
    # failure there is none in the first
    face = TEMPLATE_FACE if (model, n) != ("detect", 2) else "nodes 0 0 0 0 0 0"
    out = script(harness, tmp_path, "new /m", "frame 640 640 0 flat:100 128 128", face,
                 f"break {model} {how} {n}", "run")
    assert out[-1] == f"found -1 invokes {invokes}"


def test_similarity_is_the_dot_product(harness, tmp_path):
    out = script(harness, tmp_path, "sim 0.6 0.8 0.6 0.8", "sim 1 0 0 1", "sim 0.6 0.8 -0.6 -0.8")
    assert out == ["sim 1.0000", "sim 0.0000", "sim -1.0000"]


def detected_at(frame_w, frame_h, points):
    """A detector cell (first pass, stride 8, cell 0) whose five points land on the given upright
    frame points: undoes the map from detector input to frame for a frame of this size."""
    side = min(frame_w, frame_h)
    k = side / 640
    tx = (frame_w - side) / 2 + 0.5 * k - 0.5
    ty = (frame_h - side) / 2 + 0.5 * k - 0.5
    kps = []
    for x, y in points:
        kps += [(x - tx) / k / 8, (y - ty) / k / 8]
    return cell_cmd(1, 0, 0, 0.95, 0.95, [0, 0, 0, 0], kps)


@pytest.mark.parametrize("i,j,red", [
    (50, 50, 100),
    (110, 50, 170),   # the frame's last pixel column: odd width, the chroma column before it
    (50, 110, 170),   # the same for the last row
])
def test_odd_frame_sizes_take_the_last_chroma_sample(harness, tmp_path, i, j, red):
    # 111×111: the last luma column has no chroma column of its own. V is 178 only in the last
    # chroma column and row; reading past them would give 128 (or the next row's start).
    face = detected_at(111, 111, [(x - 0.25, y - 0.25) for x, y in TEMPLATE])
    out = script(harness, tmp_path, "new /m", "frame 111 111 0 flat:100 -178 128", face, "run",
                 f"input embed {i} {j} 112")
    assert found_line(out).startswith("found 1 ")
    assert values(out, "in")[0] == red


def test_points_unrelated_to_the_template_give_a_usable_crop(harness, tmp_path):
    # Five points around (300, 300) whose spread is orthogonal to the template's in both the dot
    # and the cross product: no similarity fits (c and s vanish up to rounding, so the scale is
    # huge); the crop must still be plain pixel values, no NaN for SFace. (c = s = 0 exactly,
    # the n2 <= 0 guard in similarity_inverse(), does not come out of float arithmetic.)
    dx = [x - sum(p[0] for p in TEMPLATE) / 5 for x, _ in TEMPLATE]
    dy = [y - sum(p[1] for p in TEMPLATE) / 5 for _, y in TEMPLATE]
    basis = [dx + dy, dy + [-v for v in dx], [1] * 5 + [0] * 5, [0] * 5 + [1] * 5]
    vec = [0.0] * 10
    vec[0], vec[6] = 1.0, 1.0
    ortho = []
    for b in basis:
        for o in ortho:
            f = sum(x * y for x, y in zip(b, o)) / sum(y * y for y in o)
            b = [x - f * y for x, y in zip(b, o)]
        ortho.append(b)
    for o in ortho:
        f = sum(x * y for x, y in zip(vec, o)) / sum(y * y for y in o)
        vec = [x - f * y for x, y in zip(vec, o)]
    pts = [(300 + 20 * vec[n], 300 + 20 * vec[5 + n]) for n in range(5)]
    out = script(harness, tmp_path, "new /m", "frame 640 640 0 xy 128 128",
                 detected_at(640, 640, pts), "run", "input embed 0 0 112",
                 "input embed 111 111 112", "input embed 50 7 112")
    assert found_line(out).startswith("found 1 ")
    for line in out[-3:]:
        assert all(0 <= float(v) <= 255 for v in line.split()[1:]), line


def test_out_of_memory_opens_no_model(harness, tmp_path):
    out = script(harness, tmp_path, "oom", "new /m")
    assert not any(line.startswith("open ") for line in out)
    assert out[-1] == "error out of memory"
