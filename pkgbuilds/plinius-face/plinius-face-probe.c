// SPDX-License-Identifier: MIT
// plinius-face-probe: runs the recognition chain of plinius-face on frames of the front camera
// (or on a saved NV21 frame) and shows per frame what it found and how long each step took, on
// the NPU or the CPU. It can also store an embedding and compare later frames with it, and save
// the frames for data sets. A measuring tool, not the unlock service.
//
// Usage: plinius-face-probe [options]
//   -a accelerator  NNAPI device: mtk-neuron (default), mtk-mdla, or cpu
//   -d dir          model directory (default /usr/share/plinius-face)
//   -c dir          NNAPI compilation cache
//   -n frames       how many frames (default 30)
//   -i file -W w -H h -o degrees   a saved NV21 frame instead of the camera
//   -e file         enroll: write the mean embedding of all frames with a face
//   -m file         match: similarity of each frame to this stored embedding
//   -s dir          save every frame (.nv21) and its result (.txt) with its arrival time
#include "face-pipeline.h"
#ifdef FACE_WITH_CAMERA
#include "face-camera.h"
#endif

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define DEFAULT_MODEL_DIR "/usr/share/plinius-face"

static int
read_file (const char *path, uint8_t *buffer, size_t bytes)
{
	FILE *file = fopen (path, "rb");
	size_t got;

	if (!file)
		return -1;
	got = fread (buffer, 1, bytes, file);
	fclose (file);
	return got == bytes ? 0 : -1;
}

static void
save_result (const char *dir, int index, const uint8_t *frame, size_t bytes, int64_t time_ns,
	     const FaceFrame *f, int found, const FaceResult *r)
{
	char path[4096];
	FILE *file;

	snprintf (path, sizeof path, "%s/frame-%03d.nv21", dir, index);
	if ((file = fopen (path, "wb"))) {
		fwrite (frame, 1, bytes, file);
		fclose (file);
	}
	snprintf (path, sizeof path, "%s/frame-%03d.txt", dir, index);
	if (!(file = fopen (path, "w")))
		return;
	/* Arrival of the frame, CLOCK_MONOTONIC, to line it up with what the screen showed */
	fprintf (file, "size %d %d orientation %d\ntime_ns %lld\nfound %d\n", f->width, f->height,
		 f->orientation, (long long) time_ns, found);
	if (found > 0) {
		fprintf (file, "score %.4f\nbox %.1f %.1f %.1f %.1f\npresence %.4f\n", r->score,
			 r->box[0], r->box[1], r->box[2], r->box[3], r->presence);
		for (int p = 0; p < 5; p++)
			fprintf (file, "keypoint %d %.1f %.1f\n", p, r->keypoints[p].x, r->keypoints[p].y);
		for (int p = 0; p < FACE_MESH_POINTS; p++)
			fprintf (file, "mesh %d %.2f %.2f %.2f\n", p, r->mesh[p].x, r->mesh[p].y,
				 r->mesh_z[p]);
		fprintf (file, "embedding");
		for (int v = 0; v < FACE_EMBEDDING_SIZE; v++)
			fprintf (file, " %.6f", r->embedding[v]);
		fprintf (file, "\n");
	}
	fclose (file);
}

int
main (int argc, char **argv)
{
	const char *accelerator = "mtk-neuron", *model_dir = DEFAULT_MODEL_DIR, *input = NULL;
	const char *enroll = NULL, *match = NULL, *save = NULL, *cache = NULL;
	int frames = 30, width = 0, height = 0, orientation = 0, faces = 0, opt;
	float reference[FACE_EMBEDDING_SIZE], sum[FACE_EMBEDDING_SIZE] = { 0 };
	double total[3] = { 0 };
	PlTflOptions options;
	FacePipeline *pipeline;
	char *error = NULL;
	uint8_t *buffer;
	size_t bytes;
#ifdef FACE_WITH_CAMERA
	FaceCamera *camera = NULL;
#endif

	setvbuf (stdout, NULL, _IOLBF, 0);
	while ((opt = getopt (argc, argv, "a:d:c:n:i:W:H:o:e:m:s:")) != -1) {
		switch (opt) {
		case 'a': accelerator = optarg; break;
		case 'd': model_dir = optarg; break;
		case 'c': cache = optarg; break;
		case 'n': frames = atoi (optarg); break;
		case 'i': input = optarg; break;
		case 'W': width = atoi (optarg); break;
		case 'H': height = atoi (optarg); break;
		case 'o': orientation = atoi (optarg); break;
		case 'e': enroll = optarg; break;
		case 'm': match = optarg; break;
		case 's': save = optarg; break;
		default:
			fprintf (stderr, "usage: %s [-a accelerator|cpu] [-d models] [-c cache] [-n frames] "
				 "[-i file.nv21 -W width -H height -o degrees] [-e enroll.emb] "
				 "[-m match.emb] [-s dir]\n", argv[0]);
			return 2;
		}
	}
	if (match && read_file (match, (uint8_t *) reference, sizeof reference)) {
		fprintf (stderr, "cannot read the embedding %s\n", match);
		return 1;
	}

	pltfl_options_init (&options);
	options.accelerator = strcmp (accelerator, "cpu") == 0 ? NULL : accelerator;
	options.allow_fp16 = 1;
	options.cache_dir = cache;
	pipeline = face_pipeline_new (model_dir, &options, &error);
	if (!pipeline) {
		fprintf (stderr, "%s\n", error);
		pltfl_exit (1);
	}
	printf ("%s\n", face_pipeline_describe (pipeline));

	if (input) {
		if (width <= 0 || height <= 0) {
			fprintf (stderr, "a saved frame needs -W and -H\n");
			pltfl_exit (2);
		}
		bytes = (size_t) width * height * 3 / 2;
	} else {
#ifdef FACE_WITH_CAMERA
		camera = face_camera_open (&error);
		if (!camera) {
			fprintf (stderr, "%s\n", error);
			pltfl_exit (1);
		}
		face_camera_format (camera, &width, &height, &orientation, &bytes);
		printf ("front camera %dx%d, orientation %d\n", width, height, orientation);
#else
		fprintf (stderr, "built without the camera, use -i\n");
		pltfl_exit (2);
#endif
	}
	buffer = malloc (bytes);
	if (!buffer || (input && read_file (input, buffer, bytes))) {
		fprintf (stderr, "cannot read %s\n", input ? input : "the frame");
		pltfl_exit (1);
	}

	for (int n = 0; n < frames; n++) {
		FaceFrame frame = { buffer, width, height, orientation };
		FaceResult result;
		int64_t time_ns = 0;
		int found;

#ifdef FACE_WITH_CAMERA
		if (camera && face_camera_next_frame (camera, buffer, 2000, &time_ns) != 1) {
			fprintf (stderr, "no frame from the camera\n");
			break;
		}
#endif
		found = face_pipeline_run (pipeline, &frame, &result);
		if (save)
			save_result (save, n, buffer, bytes, time_ns, &frame, found, &result);
		if (found < 0) {
			printf ("frame %d: error\n", n);
			continue;
		}
		if (found == 0) {
			printf ("frame %d: no face (%.1f ms)\n", n, result.ms_detect);
			continue;
		}
		faces++;
		total[0] += result.ms_detect;
		total[1] += result.ms_mesh;
		total[2] += result.ms_embed;
		for (int v = 0; v < FACE_EMBEDDING_SIZE; v++)
			sum[v] += result.embedding[v];
		printf ("frame %d: face %.2f box %.0f,%.0f-%.0f,%.0f presence %.2f | detect %.1f mesh %.1f "
			"embed %.1f ms", n, result.score, result.box[0], result.box[1], result.box[2],
			result.box[3], result.presence, result.ms_detect, result.ms_mesh,
			result.ms_embed);
		if (match)
			printf (" | similarity %.3f", face_similarity (result.embedding, reference));
		printf ("\n");
	}
	if (faces)
		printf ("%d of %d frames with a face; mean detect %.1f mesh %.1f embed %.1f ms\n", faces,
			frames, total[0] / faces, total[1] / faces, total[2] / faces);

	if (enroll && faces) {
		float norm = 0;
		FILE *file;

		for (int v = 0; v < FACE_EMBEDDING_SIZE; v++)
			norm += sum[v] * sum[v];
		norm = sqrtf (norm);
		for (int v = 0; v < FACE_EMBEDDING_SIZE; v++)
			sum[v] /= norm;
		if (!(file = fopen (enroll, "wb")) || fwrite (sum, sizeof sum, 1, file) != 1) {
			fprintf (stderr, "cannot write %s\n", enroll);
			pltfl_exit (1);
		}
		fclose (file);
		printf ("embedding of %d frames written to %s\n", faces, enroll);
	}

#ifdef FACE_WITH_CAMERA
	face_camera_close (camera);
#endif
	face_pipeline_free (pipeline);
	free (buffer);
	/* The vendor's NPU libraries crash in their exit handlers */
	pltfl_exit (faces ? 0 : 3);
}
