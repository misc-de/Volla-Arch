# plinius-face-probe (the measuring tool next to the face service): its options, the saved frame
# or the front camera as the source, what it prints per frame, the enrolled mean embedding, the
# comparison with a stored one, the saved frames and results, and its exit codes. The recognition
# chain and the camera are stand-ins in the harness that log what they get to stderr and return
# the results a script (FACE_PROBE_SCRIPT) gives per frame; the probe is built without the camera
# (as on x86_64) and with it (as on the phone).

import math
import os
import struct

import pytest

from dhw_harness import PKG, build_harness, run

FACE = PKG / "plinius-face"
TFLITE = PKG / "plinius-tflite"

HARNESS = r"""
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <plinius-tflite.h>
#include "face-pipeline.h"
#include "face-camera.h"

void
pltfl_options_init (PlTflOptions *options)
{
	memset (options, 0, sizeof *options);
	options->threads = 4;
}

void
pltfl_exit (int status)
{
	fflush (stdout);
	fprintf (stderr, "stub: exit %d\n", status);
	exit (status);
}

/* ---- Recognition chain: one script line per frame ---- */

struct FacePipeline {
	FILE *script;
};

FacePipeline *
face_pipeline_new (const char *dir, const PlTflOptions *options, char **error)
{
	const char *script = getenv ("FACE_PROBE_SCRIPT");
	FacePipeline *p;

	fprintf (stderr, "stub: new %s accel=%s fp16=%d cache=%s threads=%d\n", dir,
		 options->accelerator ? options->accelerator : "-", options->allow_fp16,
		 options->cache_dir ? options->cache_dir : "-", options->threads);
	if (getenv ("FACE_PROBE_NO_MODELS")) {
		*error = strdup ("no models here");
		return NULL;
	}
	p = calloc (1, sizeof *p);
	p->script = script ? fopen (script, "r") : NULL;
	return p;
}

void
face_pipeline_free (FacePipeline *p)
{
	fprintf (stderr, "stub: free\n");
	if (p->script)
		fclose (p->script);
	free (p);
}

const char *
face_pipeline_describe (FacePipeline *p)
{
	return "detect 1/2, mesh 3/4, embed 5/6 operations on the NPU";
}

/* "-1", "0" or "1 SCORE E0 E1" (the rest of the embedding 0); ms_* fixed */
int
face_pipeline_run (FacePipeline *p, const FaceFrame *frame, FaceResult *r)
{
	char line[256] = "0";
	int found;
	float score = 0, e0 = 0, e1 = 0;

	memset (r, 0, sizeof *r);
	if (p->script && !fgets (line, sizeof line, p->script))
		strcpy (line, "0");
	fprintf (stderr, "stub: run %dx%d o%d byte %d\n", frame->width, frame->height,
		 frame->orientation, frame->nv21[0]);
	sscanf (line, "%d %f %f %f", &found, &score, &e0, &e1);
	r->ms_detect = 2;
	if (found <= 0)
		return found;
	r->score = score;
	r->box[0] = 10;
	r->box[1] = 20;
	r->box[2] = 110;
	r->box[3] = 140;
	r->presence = 0.75f;
	for (int k = 0; k < 5; k++)
		r->keypoints[k] = (FacePoint) { 30 + k, 40 + k };
	for (int k = 0; k < FACE_MESH_POINTS; k++) {
		r->mesh[k] = (FacePoint) { k, 2 * k };
		r->mesh_z[k] = -0.5f;
	}
	r->embedding[0] = e0;
	r->embedding[1] = e1;
	r->ms_mesh = 4;
	r->ms_embed = 6;
	return 1;
}

float
face_similarity (const float *a, const float *b)
{
	float dot = 0;

	for (int v = 0; v < FACE_EMBEDDING_SIZE; v++)
		dot += a[v] * b[v];
	return dot;
}

/* ---- Front camera: FACE_PROBE_CAMERA lists the results of next_frame ("1,1,0") ---- */

struct FaceCamera {
	int calls;
};

FaceCamera *
face_camera_open (char **error)
{
	if (getenv ("FACE_PROBE_NO_CAMERA")) {
		*error = strdup ("no front camera");
		return NULL;
	}
	fprintf (stderr, "stub: camera open\n");
	return calloc (1, sizeof (FaceCamera));
}

void
face_camera_close (FaceCamera *c)
{
	fprintf (stderr, "stub: camera close %s\n", c ? "camera" : "nothing");
	free (c);
}

void
face_camera_format (FaceCamera *c, int *w, int *h, int *o, size_t *bytes)
{
	*w = 4;
	*h = 2;
	*o = 90;
	*bytes = 12;
}

int
face_camera_next_frame (FaceCamera *c, uint8_t *dst, int timeout_ms, int64_t *ts)
{
	const char *results = getenv ("FACE_PROBE_CAMERA");
	int n = c->calls++, rc;

	fprintf (stderr, "stub: next timeout %d\n", timeout_ms);
	if (!results || (int) strlen (results) <= 2 * n)
		return 0;
	rc = results[2 * n] == '-' ? -1 : results[2 * n] - '0';
	if (rc == 1) {
		memset (dst, 100 + n, 12);
		*ts = 1000000000LL * (n + 1);
	}
	return rc;
}

#include "plinius-face-probe.c"
"""


@pytest.fixture(scope="module")
def probes(tmp_path_factory):
    d = tmp_path_factory.mktemp("face-probe")
    flags = [f"-I{FACE}", f"-I{TFLITE}"]
    plain = build_harness(d, "probe", HARNESS, pkgs=(), flags=flags)
    camera = build_harness(d, "probe-camera", HARNESS, pkgs=(), flags=[*flags, "-DFACE_WITH_CAMERA"])
    return {"plain": plain, "camera": camera}


def probe(binary, *args, script=None, tmp_path=None, **env):
    e = {}
    if script is not None:
        f = tmp_path / "frames.script"
        f.write_text("\n".join(script) + "\n")
        e["FACE_PROBE_SCRIPT"] = str(f)
    e.update(env)
    full = dict(os.environ)
    full.pop("FACE_PROBE_SCRIPT", None)
    full.update(e)
    return run(binary, *args, env=full, check=False)


def stub_lines(r):
    return [line[6:] for line in r.stderr.splitlines() if line.startswith("stub: ")]


def nv21(tmp_path, w, h, first=7):
    f = tmp_path / "frame.nv21"
    f.write_bytes(bytes([first]) + bytes(w * h * 3 // 2 - 1))
    return f


def embedding_file(tmp_path, values, name="ref.emb"):
    f = tmp_path / name
    f.write_bytes(struct.pack("<128f", *(values + [0] * (128 - len(values)))))
    return f


# ---- Options and errors ----

def test_unknown_option_shows_the_usage(probes):
    r = probe(probes["plain"], "-x")
    assert r.returncode == 2
    assert "usage:" in r.stderr and "[-m match.emb]" in r.stderr


@pytest.mark.parametrize("content", [None, b"\0" * 511])
def test_match_needs_a_whole_embedding(probes, tmp_path, content):
    ref = tmp_path / "ref.emb"
    if content is not None:
        ref.write_bytes(content)
    r = probe(probes["plain"], "-m", ref, "-i", nv21(tmp_path, 4, 2), "-W", 4, "-H", 2)
    assert r.returncode == 1
    assert f"cannot read the embedding {ref}" in r.stderr
    # Checked before the models load
    assert not any(s.startswith("new") for s in stub_lines(r))


def test_without_models(probes, tmp_path):
    r = probe(probes["plain"], "-d", tmp_path, FACE_PROBE_NO_MODELS="1")
    assert r.returncode == 1
    assert "no models here" in r.stderr.splitlines()


def test_options_reach_the_chain(probes, tmp_path):
    r = probe(probes["plain"], "-d", "/models", "-c", "/cache", "-a", "mtk-mdla", "-n", 0,
              "-i", nv21(tmp_path, 4, 2), "-W", 4, "-H", 2)
    assert stub_lines(r)[0] == "new /models accel=mtk-mdla fp16=1 cache=/cache threads=4"
    # No frame, no face
    assert r.returncode == 3
    assert r.stdout.splitlines() == ["detect 1/2, mesh 3/4, embed 5/6 operations on the NPU"]


def test_defaults_and_cpu(probes, tmp_path):
    frame = nv21(tmp_path, 4, 2)
    r = probe(probes["plain"], "-n", 0, "-i", frame, "-W", 4, "-H", 2)
    # Only handed to the (stand-in) loader, nothing is read there
    assert stub_lines(r)[0] == "new /usr/share/plinius-face accel=mtk-neuron fp16=1 cache=- threads=4"
    r = probe(probes["plain"], "-a", "cpu", "-n", 0, "-i", frame, "-W", 4, "-H", 2)
    assert stub_lines(r)[0].startswith("new /usr/share/plinius-face accel=- ")


@pytest.mark.parametrize("size", [[], ["-W", "4"], ["-H", "2"], ["-W", "0", "-H", "2"],
                                  ["-W", "4", "-H", "-2"]])
def test_saved_frame_needs_its_size(probes, tmp_path, size):
    r = probe(probes["plain"], "-i", nv21(tmp_path, 4, 2), *size)
    assert r.returncode == 2
    assert "a saved frame needs -W and -H" in r.stderr


def test_saved_frame_must_be_complete(probes, tmp_path):
    frame = nv21(tmp_path, 4, 2)
    r = probe(probes["plain"], "-i", frame, "-W", 4, "-H", 4)
    assert r.returncode == 1
    assert f"cannot read {frame}" in r.stderr
    r = probe(probes["plain"], "-i", tmp_path / "missing.nv21", "-W", 4, "-H", 2)
    assert r.returncode == 1


def test_without_camera_a_saved_frame_is_needed(probes):
    r = probe(probes["plain"])
    assert r.returncode == 2
    assert "built without the camera, use -i" in r.stderr


# ---- Frames ----

def test_frames_from_a_saved_file(probes, tmp_path):
    ref = embedding_file(tmp_path, [0.6, 0.8])
    out = tmp_path / "enrolled.emb"
    r = probe(probes["plain"], "-i", nv21(tmp_path, 4, 2, first=9), "-W", 4, "-H", 2, "-o", 270,
              "-n", 4, "-m", ref, "-e", out,
              script=["1 0.96 1 0", "0", "-1", "1 0.91 0 1"], tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [
        "detect 1/2, mesh 3/4, embed 5/6 operations on the NPU",
        "frame 0: face 0.96 box 10,20-110,140 presence 0.75 | detect 2.0 mesh 4.0 embed 6.0 ms"
        " | similarity 0.600",
        "frame 1: no face (2.0 ms)",
        "frame 2: error",
        "frame 3: face 0.91 box 10,20-110,140 presence 0.75 | detect 2.0 mesh 4.0 embed 6.0 ms"
        " | similarity 0.800",
        "2 of 4 frames with a face; mean detect 2.0 mesh 4.0 embed 6.0 ms",
        f"embedding of 2 frames written to {out}",
    ]
    assert [s for s in stub_lines(r) if s.startswith("run")] == ["run 4x2 o270 byte 9"] * 4
    # The mean of the faces' embeddings, normalized: (1, 1)/√2
    emb = struct.unpack("<128f", out.read_bytes())
    assert emb[:2] == pytest.approx((1 / math.sqrt(2), 1 / math.sqrt(2)), abs=1e-6)
    assert not any(emb[2:])
    assert stub_lines(r)[-2:] == ["free", "exit 0"]


def test_no_face_ends_with_three_and_writes_no_embedding(probes, tmp_path):
    out = tmp_path / "enrolled.emb"
    r = probe(probes["plain"], "-i", nv21(tmp_path, 4, 2), "-W", 4, "-H", 2, "-n", 2, "-e", out,
              script=["0", "-1"], tmp_path=tmp_path)
    assert r.returncode == 3
    assert not out.exists()
    assert "frames with a face" not in r.stdout


def test_enroll_file_that_cannot_be_written(probes, tmp_path):
    out = tmp_path / "no-such-dir" / "enrolled.emb"
    r = probe(probes["plain"], "-i", nv21(tmp_path, 4, 2), "-W", 4, "-H", 2, "-n", 1, "-e", out,
              script=["1 0.95 1 0"], tmp_path=tmp_path)
    assert r.returncode == 1
    assert f"cannot write {out}" in r.stderr


def test_saved_frames_and_results(probes, tmp_path):
    frame = nv21(tmp_path, 4, 2, first=3)
    save = tmp_path / "saved"
    save.mkdir()
    r = probe(probes["plain"], "-i", frame, "-W", 4, "-H", 2, "-o", 90, "-n", 2, "-s", save,
              script=["1 0.95 0.6 0.8", "0"], tmp_path=tmp_path)
    assert r.returncode == 0
    assert (save / "frame-000.nv21").read_bytes() == frame.read_bytes()
    assert (save / "frame-001.nv21").read_bytes() == frame.read_bytes()
    face = (save / "frame-000.txt").read_text().splitlines()
    assert face[:5] == ["size 4 2 orientation 90", "time_ns 0", "found 1", "score 0.9500",
                        "box 10.0 20.0 110.0 140.0"]
    assert face[5] == "presence 0.7500"
    assert face[6:11] == [f"keypoint {k} {30 + k}.0 {40 + k}.0" for k in range(5)]
    assert face[11] == "mesh 0 0.00 0.00 -0.50"
    assert face[11 + 477] == "mesh 477 477.00 954.00 -0.50"
    emb = face[-1].split()
    assert emb[0] == "embedding" and len(emb) == 129
    assert emb[1:4] == ["0.600000", "0.800000", "0.000000"]
    assert len(face) == 11 + 478 + 1
    assert (save / "frame-001.txt").read_text() == "size 4 2 orientation 90\ntime_ns 0\nfound 0\n"


def test_saving_into_a_place_that_cannot_take_it(probes, tmp_path):
    # No directory at all: the frames are measured all the same
    r = probe(probes["plain"], "-i", nv21(tmp_path, 4, 2), "-W", 4, "-H", 2, "-n", 1,
              "-s", tmp_path / "missing", script=["1 0.95 1 0"], tmp_path=tmp_path)
    assert r.returncode == 0
    assert "1 of 1 frames with a face" in r.stdout
    # The frame can be written, its result not (a directory in its place)
    save = tmp_path / "saved"
    (save / "frame-000.txt").mkdir(parents=True)
    r = probe(probes["plain"], "-i", nv21(tmp_path, 4, 2), "-W", 4, "-H", 2, "-n", 1, "-s", save,
              script=["1 0.95 1 0"], tmp_path=tmp_path)
    assert r.returncode == 0
    assert (save / "frame-000.nv21").stat().st_size == 12


# ---- Front camera (the phone's build) ----

def test_frames_from_the_camera(probes, tmp_path):
    save = tmp_path / "saved"
    save.mkdir()
    r = probe(probes["camera"], "-n", 3, "-s", save, script=["1 0.95 1 0", "0", "1 0.9 0 1"],
              tmp_path=tmp_path, FACE_PROBE_CAMERA="1,1,1")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[1] == "front camera 4x2, orientation 90"
    runs = [s for s in stub_lines(r) if s.startswith(("run", "next"))]
    assert runs == ["next timeout 2000", "run 4x2 o90 byte 100", "next timeout 2000",
                    "run 4x2 o90 byte 101", "next timeout 2000", "run 4x2 o90 byte 102"]
    # The camera's arrival time goes with the saved frame
    assert (save / "frame-001.txt").read_text().splitlines()[1] == "time_ns 2000000000"
    assert stub_lines(r)[-3:] == ["camera close camera", "free", "exit 0"]


@pytest.mark.parametrize("results", ["1,0", "1,-"])
def test_camera_without_a_frame_stops(probes, tmp_path, results):
    r = probe(probes["camera"], "-n", 5, script=["1 0.95 1 0"] * 5, tmp_path=tmp_path,
              FACE_PROBE_CAMERA=results)
    assert "no frame from the camera" in r.stderr
    assert "1 of 5 frames with a face" in r.stdout
    assert r.returncode == 0


def test_camera_missing(probes, tmp_path):
    r = probe(probes["camera"], FACE_PROBE_NO_CAMERA="1")
    assert r.returncode == 1
    assert "no front camera" in r.stderr.splitlines()


def test_saved_frame_with_the_camera_build(probes, tmp_path):
    r = probe(probes["camera"], "-i", nv21(tmp_path, 4, 2, first=5), "-W", 4, "-H", 2, "-n", 1,
              script=["1 0.95 1 0"], tmp_path=tmp_path)
    assert r.returncode == 0
    assert "stub: camera open" not in r.stderr
    assert [s for s in stub_lines(r) if s.startswith(("run", "next", "camera"))] == [
        "run 4x2 o0 byte 5", "camera close nothing"]
