# plinius-tflite-bench: the options reach plinius-tflite, the tensors are listed with type, shape
# and quantization, the inputs are filled with the same random numbers every time (srand(1):
# floats in 0..1, other types byte by byte), the first run is timed apart from the others, and
# every failure ends through pltfl_exit (the NPU's vendor libraries crash in their exit
# handlers). plinius-tflite itself (TensorFlow Lite, the NPU) is a stand-in here, which reads a
# small description of the "model" from the model file.

import ctypes
import re
import struct

import pytest

from dhw_harness import PKG, build_harness, run

DIR = PKG / "plinius-tflite"

FAKE = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "plinius-tflite.h"

/* The model file: lines "nodes NPU TOTAL", "input|output TYPE DIMS BYTES SCALE ZERO NAME" (DIMS
 * comma-separated, NAME "-" for none), "delays MS,MS,..." (per invoke), "fail_invoke N" (the
 * N-th invoke fails, from 1), "fail_open MESSAGE" */
struct PlTfl {
	int npu, total, n_in, n_out, invokes, fail_invoke, n_delays;
	PlTflTensorInfo in[8], out[8];
	unsigned char *data[8];
	char names[16][32];
	double delays[16];
};

void pltfl_options_init(PlTflOptions *o)
{
	memset(o, 0, sizeof(*o));
	o->threads = -7; /* a value of its own: the bench must keep it unless -t is given */
}

static void parse_tensor(PlTfl *m, char *rest, PlTflTensorInfo *info, int slot)
{
	char dims[64], name[32];
	int type;
	sscanf(rest, "%d %63s %zu %f %d %31s", &type, dims, &info->bytes, &info->scale, &info->zero_point, name);
	info->type = type;
	info->ndims = 0;
	if (strcmp(dims, "-"))
		for (char *s = strtok(dims, ","); s; s = strtok(NULL, ","))
			info->dims[info->ndims++] = atoi(s);
	if (strcmp(name, "-")) {
		snprintf(m->names[slot], sizeof(m->names[slot]), "%s", name);
		info->name = m->names[slot];
	}
}

PlTfl *pltfl_open(const char *path, const PlTflOptions *o, char **error)
{
	fprintf(stderr, "OPEN %s accelerator=%s threads=%d cache=%s fp16=%d\n", path,
		o->accelerator ? o->accelerator : "(none)", o->threads, o->cache_dir ? o->cache_dir : "(none)",
		o->allow_fp16);
	FILE *f = fopen(path, "r");
	if (!f) {
		*error = strdup("cannot read the model");
		return NULL;
	}
	PlTfl *m = calloc(1, sizeof(*m));
	char line[256];
	while (fgets(line, sizeof(line), f)) {
		line[strcspn(line, "\n")] = 0;
		if (!strncmp(line, "fail_open ", 10)) {
			*error = strdup(line + 10);
			fclose(f);
			free(m);
			return NULL;
		} else if (!strncmp(line, "nodes ", 6)) {
			sscanf(line + 6, "%d %d", &m->npu, &m->total);
		} else if (!strncmp(line, "input ", 6)) {
			parse_tensor(m, line + 6, &m->in[m->n_in], m->n_in);
			m->data[m->n_in] = calloc(1, m->in[m->n_in].bytes);
			m->n_in++;
		} else if (!strncmp(line, "output ", 7)) {
			parse_tensor(m, line + 7, &m->out[m->n_out], 8 + m->n_out);
			m->n_out++;
		} else if (!strncmp(line, "fail_invoke ", 12)) {
			m->fail_invoke = atoi(line + 12);
		} else if (!strncmp(line, "delays ", 7)) {
			for (char *s = strtok(line + 7, ","); s && m->n_delays < 16; s = strtok(NULL, ","))
				m->delays[m->n_delays++] = atof(s);
		}
	}
	fclose(f);
	return m;
}

void pltfl_close(PlTfl *m)
{
	fprintf(stderr, "CLOSE\n");
}

int pltfl_input_count(PlTfl *m) { return m->n_in; }
int pltfl_output_count(PlTfl *m) { return m->n_out; }
int pltfl_npu_nodes(PlTfl *m) { return m->npu; }
int pltfl_total_nodes(PlTfl *m) { return m->total; }

void *pltfl_input(PlTfl *m, int i, PlTflTensorInfo *info)
{
	*info = m->in[i];
	return m->data[i];
}

const void *pltfl_output(PlTfl *m, int i, PlTflTensorInfo *info)
{
	*info = m->out[i];
	return NULL;
}

int pltfl_invoke(PlTfl *m)
{
	m->invokes++;
	if (m->invokes == 1)
		/* What the model got as input */
		for (int i = 0; i < m->n_in; i++) {
			fprintf(stderr, "INPUT %d ", i);
			for (size_t j = 0; j < m->in[i].bytes; j++)
				fprintf(stderr, "%02x", m->data[i][j]);
			fprintf(stderr, "\n");
		}
	if (m->invokes <= m->n_delays) {
		double ms = m->delays[m->invokes - 1];
		struct timespec ts = {(time_t)(ms / 1000), (long)((ms - (time_t)(ms / 1000) * 1000) * 1e6)};
		nanosleep(&ts, NULL);
	}
	return m->invokes == m->fail_invoke ? -1 : 0;
}

/* The real one ends with _exit; exit here, so that gcov writes its counters */
void pltfl_exit(int status)
{
	fprintf(stderr, "EXIT %d after %s\n", status, "pltfl_exit");
	exit(status);
}
"""


@pytest.fixture(scope="module")
def bench(tmp_path_factory):
    d = tmp_path_factory.mktemp("tflite")
    (d / "fake_pltfl.c").write_text(FAKE)
    harness = f'#include "{DIR / "plinius-tflite-bench.c"}"\n#include "fake_pltfl.c"\n'
    return build_harness(d, "bench", harness, pkgs=(), flags=[f"-I{DIR}"])


def model(tmp_path, *lines):
    f = tmp_path / "model.tflite"
    f.write_text("\n".join(lines) + "\n")
    return f


def libc_rand_sequence(n):
    """glibc's rand() after srand(1), as the bench draws it"""
    libc = ctypes.CDLL(None)
    libc.srand(1)
    return [libc.rand() for _ in range(n)]


RAND_MAX = 2147483647


def test_tensors_and_random_inputs(bench, tmp_path):
    m = model(tmp_path, "nodes 3 12",
              "input 1 1,2,2 16 0 0 image",          # float32: 4 floats in 0..1
              "input 3 6 6 0.0078125 128 -",         # uint8, quantized, no name
              "input 4 1 8 0 0 ids",                 # int64: filled byte by byte
              "output 9 1,10 10 0.1 -3 scores",
              "output 2 - 4 0 0 count",
              "output 10 2 4 0 0 half",
              "output 7 3 6 0 0 pcm",
              "output 42 1 1 0 0 odd")
    r = run(bench, "-n", "3", m)
    out = r.stdout.splitlines()
    assert re.fullmatch(r"opened in \d+\.\d ms, 3 of 12 operations on the NPU", out[0])
    assert out[1:9] == [
        "  input 0: image float32 [1,2,2]",
        "  input 1: ? uint8 [6] scale 0.0078125 zero 128",
        "  input 2: ids int64 [1]",
        "  output 0: scores int8 [1,10] scale 0.1 zero -3",
        "  output 1: count int32 []",
        "  output 2: half float16 [2]",
        "  output 3: pcm int16 [3]",
        "  output 4: odd other [1]",
    ]
    assert re.fullmatch(r"first run \d+\.\d\d ms, then 3 runs: mean \d+\.\d\d ms, best \d+\.\d\d ms", out[9])
    rand = libc_rand_sequence(4 + 6 + 8)
    floats = b"".join(struct.pack("<f", ctypes.c_float(v / RAND_MAX).value) for v in rand[:4])
    inputs = dict(line.split(" ", 2)[1:] for line in r.stderr.splitlines() if line.startswith("INPUT"))
    assert inputs["0"] == floats.hex()
    assert inputs["1"] == bytes(v & 0xff for v in rand[4:10]).hex()
    assert inputs["2"] == bytes(v & 0xff for v in rand[10:18]).hex()
    # Every value of the floats in 0..1
    assert all(0 <= v <= 1 for v in struct.unpack("<4f", floats))
    assert r.stderr.splitlines()[-2:] == ["CLOSE", "EXIT 0 after pltfl_exit"]


def test_options_reach_the_library(bench, tmp_path):
    m = model(tmp_path, "nodes 0 1")
    r = run(bench, "-a", "mtk-neuron", "-t", "4", "-c", tmp_path / "cache", "-f", "-n", "0", m)
    assert r.stderr.splitlines()[0] == \
        f"OPEN {m} accelerator=mtk-neuron threads=4 cache={tmp_path / 'cache'} fp16=1"
    assert r.stdout.splitlines()[0].endswith(" 0 of 1 operations on the NPU mtk-neuron")
    # No runs after the first: nothing to average
    assert r.stdout.splitlines()[-1].endswith("then 0 runs: mean 0.00 ms, best 0.00 ms")


def test_defaults(bench, tmp_path):
    m = model(tmp_path, "nodes 0 5", "delays 0," + ",".join(["0"] * 50))
    r = run(bench, m)
    # The library's own defaults stay: CPU, its thread count, no cache
    assert r.stderr.splitlines()[0] == f"OPEN {m} accelerator=(none) threads=-7 cache=(none) fp16=0"
    assert r.stdout.splitlines()[0].endswith(" 0 of 5 operations on the NPU")
    assert "then 50 runs" in r.stdout


def test_first_run_is_timed_apart(bench, tmp_path):
    m = model(tmp_path, "nodes 1 1", "delays 60,10,30")
    out = run(bench, "-n", "2", m).stdout.splitlines()[-1]
    first, mean, best = map(float, re.fullmatch(
        r"first run (\S+) ms, then 2 runs: mean (\S+) ms, best (\S+) ms", out).groups())
    assert first >= 60
    assert 20 <= mean < first and 10 <= best < 30
    assert mean > best


@pytest.mark.parametrize("args,message", [
    (["-x"], "usage: "),
    ([], "no model given"),
])
def test_usage(bench, args, message):
    r = run(bench, *args, check=False)
    assert r.returncode == 2 and message in r.stderr


def test_model_that_does_not_open(bench, tmp_path):
    r = run(bench, model(tmp_path, "fail_open Not a TensorFlow Lite model"), check=False)
    assert r.returncode == 1
    assert "Not a TensorFlow Lite model\n" in r.stderr and "EXIT 1 after pltfl_exit" in r.stderr
    assert r.stdout == ""


@pytest.mark.parametrize("fail,message", [(1, "invoke failed"), (3, "invoke 1 failed")])
def test_failing_runs(bench, tmp_path, fail, message):
    r = run(bench, "-n", "5", model(tmp_path, "nodes 1 1", f"fail_invoke {fail}"), check=False)
    assert r.returncode == 1
    assert f"{message}\nEXIT 1 after pltfl_exit" in r.stderr
    assert "CLOSE" not in r.stderr and "first run" not in r.stdout
