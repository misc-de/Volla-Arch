// SPDX-License-Identifier: MIT
// plinius-faced: face recognition service of the Plinius on the system bus (org.plinius.Face).
//
// Each user manages only their own face: enroll it, delete it, switch face unlock on or off,
// choose how strict it is, and try it ("Verify"). The caller is the user the bus reports for the
// sender, never a user named in a call. The templates (embeddings, no pictures) live in
// /var/lib/plinius-face, readable by root only.
//
// Enrolling a face, switching face unlock on and making it less strict ("balanced") need the
// user's own PIN (polkit org.plinius.face.enroll, auth_self_keep): else anyone holding the
// unlocked phone could add their face, or loosen the check until a photo passes. Switching it
// off, making it strict and deleting the face take rights away and need nothing.
//
// Unlocking: for a user who switched face unlock on, the service watches their graphical
// session (logind LockedHint) and the panel (DRM dpms). When the screen comes on while the
// session is locked, it looks for up to UNLOCK_TIMEOUT_S; UNLOCK_MATCHES frames in a row above
// the threshold unlock the session through logind (UnlockSession). Never right after locking with
// the screen still on, which would undo the lock button. The face only unlocks once the service
// has seen an unlock without it (PIN, pattern or fingerprint) since it started at boot, and no
// longer after UNLOCK_MAX_FAILURES tries in which a face was seen but did not match, until the next
// such unlock. The liveness checks against photos and videos are not done yet (misc-de, 2026-10-31:
// unlocking first, security after), so a good photo may unlock (docs/HARDWARE.md).
//
// Camera and NPU are only used during a request, in a worker thread, one request at a time. With
// face unlock on for anyone, the service runs from boot; else it ends after IDLE_EXIT_S without
// requests (it is started through D-Bus activation). The models take about 180 MB, so they are
// only in memory while a locked session may use them: loaded when it locks, so that the screen
// coming on does not wait for them, and dropped RELEASE_MODELS_S after the last unlock. The vendor's
// NPU libraries keep about 70 MB after closing the models and 18 MB more on every further round
// (measured 2026-11-05), so the service drops them by executing itself again, which takes 1.6 MB;
// what it knows about the sessions waits in STATE_FILE. It ends through pltfl_exit(), because
// those libraries crash in their exit handlers.
#define _GNU_SOURCE /* close_range */
#include "face-camera.h"
#include "face-pipeline.h"

#include <errno.h>
#include <glob.h>
#include <stdio.h>
#include <gio/gio.h>
#include <glib-unix.h>
#include <glib/gstdio.h>
#include <limits.h>
#include <math.h>
#include <string.h>
#include <unistd.h>

#define BUS_NAME "org.plinius.Face"
#define OBJECT_PATH "/org/plinius/Face"
#define INTERFACE "org.plinius.Face1"
#define ERROR_PREFIX "org.plinius.Face.Error."
#define STATE_DIR "/var/lib/plinius-face"
#define MODEL_DIR "/usr/share/plinius-face"
#define CACHE_DIR "/var/cache/plinius-face"
/* Survives the re-exec, not a reboot: root-only, on a tmpfs, from RuntimeDirectory= */
#define STATE_FILE "/run/plinius-face/sessions"

#define IDLE_EXIT_S 120
#define RELEASE_MODELS_S 30
/* Models that failed to load (missing files, the NPU refusing) are tried again after this long,
 * not at once after the failed try */
#define MODELS_RETRY_S 60
#define TEMPLATE_MAGIC "PLFACE1"
#define MAX_TEMPLATES 16

/* Enrollment: ENROLL_FRAMES good frames within ENROLL_TIMEOUT_S; the mean and every
 * ENROLL_KEEP_EVERY-th of them become the templates */
#define ENROLL_FRAMES 30
#define ENROLL_KEEP_EVERY 3
#define ENROLL_TIMEOUT_S 20
/* Verify: up to VERIFY_FRAMES good frames within VERIFY_TIMEOUT_S */
#define VERIFY_FRAMES 5
#define VERIFY_TIMEOUT_S 4
/* Unlock: so many matching frames in a row within UNLOCK_TIMEOUT_S */
#define UNLOCK_MATCHES 3
#define UNLOCK_TIMEOUT_S 4
#define UNLOCK_MAX_FAILURES 5
/* How often the panel's power state is read while a watched session is locked */
#define SCREEN_POLL_MS 250
#define DPMS_GLOB "/sys/class/drm/card*-DSI-*/dpms"
/* Created by phosh-plinius while the always-on display is shown */
#define AOD_FLAG "/run/user/*/plinius-aod"

/* A good frame: a clear face, big enough and about level */
#define GOOD_SCORE 0.9f
#define GOOD_PRESENCE 0.9f
#define GOOD_MIN_EYE_DISTANCE 40.0f /* pixels in the upright 720×1280 frame */
#define GOOD_MAX_TILT 0.35f	    /* radians, about 20° */

/* Similarity a face needs to count as the user's. Preliminary, from the first measurements
 * (2026-10-31: the user 0.62–0.80, a photo of them on a screen 0.45–0.57); the liveness checks,
 * not these values, are what will keep photos out. */
#define THRESHOLD_STRICT 0.60f
#define THRESHOLD_BALANCED 0.50f

static const char introspection[] =
	"<node>"
	" <interface name='" INTERFACE "'>"
	"  <method name='GetUser'>"
	"   <arg direction='out' type='b' name='enrolled'/>"
	"   <arg direction='out' type='b' name='enabled'/>"
	"   <arg direction='out' type='s' name='strictness'/>"
	"  </method>"
	"  <method name='SetEnabled'><arg direction='in' type='b' name='enabled'/></method>"
	"  <method name='SetStrictness'><arg direction='in' type='s' name='strictness'/></method>"
	"  <method name='Enroll'/>"
	"  <method name='Cancel'/>"
	"  <method name='Delete'/>"
	"  <method name='Verify'>"
	"   <arg direction='out' type='b' name='recognized'/>"
	"   <arg direction='out' type='d' name='similarity'/>"
	"  </method>"
	/* Per caller uid, so that a settings page can tell its own enrollment from another's */
	"  <signal name='EnrollProgress'>"
	"   <arg type='u' name='uid'/><arg type='u' name='percent'/><arg type='s' name='hint'/>"
	"  </signal>"
	"  <signal name='EnrollFinished'>"
	"   <arg type='u' name='uid'/><arg type='b' name='success'/><arg type='s' name='message'/>"
	"  </signal>"
	" </interface>"
	"</node>";

typedef enum { JOB_ENROLL, JOB_VERIFY, JOB_UNLOCK, JOB_PRELOAD } JobKind;

typedef struct {
	JobKind kind;
	uid_t uid;
	GDBusMethodInvocation *invocation; /* Verify: answered when the job ends */
	char *session; /* Unlock: logind session id */
	gboolean face_seen; /* Unlock: a face was seen (a miss counts as a failed try) */
	/* Results, written by the worker */
	gboolean success;
	char *message;
	double similarity;
} Job;

static struct {
	GMainLoop *loop;
	GDBusConnection *bus;
	GMutex lock;
	Job *job;	  /* the running request, NULL when idle */
	gboolean cancel;  /* asks the worker to stop */
	FacePipeline *pipeline; /* written by the worker thread; the main thread reads it without a job */
	guint idle_source;
	guint release_source;   /* drops the models when it runs out */
	guint models_retry;     /* the models failed to load: the next try is due then */
	gboolean unlock_pending; /* the screen came on while the models were loading */
	GHashTable *sessions;   /* logind object path → Watch */
	guint screen_source;
	gboolean screen_on;
	char *dpms_path;
} self;

/* A graphical session watched for face unlock */
typedef struct {
	char *id;
	char *path;
	uid_t uid;
	gboolean locked;
	gboolean pin_seen;   /* unlocked without the face since the service started */
	gboolean by_face;    /* the next unlock is ours, not the user's PIN */
	int failures;
} Watch;

typedef struct {
	gboolean enabled;
	gboolean strict;
} Settings;

/* ---- Files ---- */

static char *
user_file (uid_t uid, const char *suffix)
{
	return g_strdup_printf (STATE_DIR "/%u.%s", (unsigned) uid, suffix);
}

static Settings
load_settings (uid_t uid)
{
	Settings settings = { FALSE, TRUE };
	g_autofree char *path = user_file (uid, "conf");
	g_autoptr (GKeyFile) file = g_key_file_new ();

	if (g_key_file_load_from_file (file, path, G_KEY_FILE_NONE, NULL)) {
		g_autofree char *strictness = g_key_file_get_string (file, "face", "strictness", NULL);

		settings.enabled = g_key_file_get_boolean (file, "face", "enabled", NULL);
		settings.strict = g_strcmp0 (strictness, "balanced") != 0;
	}
	return settings;
}

static gboolean
save_settings (uid_t uid, const Settings *settings, GError **error)
{
	g_autofree char *path = user_file (uid, "conf");
	g_autoptr (GKeyFile) file = g_key_file_new ();
	g_autofree char *data = NULL;

	g_key_file_set_boolean (file, "face", "enabled", settings->enabled);
	g_key_file_set_string (file, "face", "strictness", settings->strict ? "strict" : "balanced");
	data = g_key_file_to_data (file, NULL, NULL);
	return g_file_set_contents_full (path, data, -1, G_FILE_SET_CONTENTS_CONSISTENT, 0600, error);
}

/* Templates: magic, count, then count embeddings */
static int
load_templates (uid_t uid, float templates[][FACE_EMBEDDING_SIZE])
{
	g_autofree char *path = user_file (uid, "emb");
	g_autofree char *data = NULL;
	gsize length = 0;
	guint32 count;
	gsize header = sizeof TEMPLATE_MAGIC + sizeof count;

	if (!g_file_get_contents (path, &data, &length, NULL) || length < header ||
	    memcmp (data, TEMPLATE_MAGIC, sizeof TEMPLATE_MAGIC) != 0)
		return 0;
	memcpy (&count, data + sizeof TEMPLATE_MAGIC, sizeof count);
	if (count == 0 || count > MAX_TEMPLATES ||
	    length != header + count * sizeof (float) * FACE_EMBEDDING_SIZE)
		return 0;
	memcpy (templates, data + header, count * sizeof (float) * FACE_EMBEDDING_SIZE);
	return (int) count;
}

static gboolean
save_templates (uid_t uid, float templates[][FACE_EMBEDDING_SIZE], int count, GError **error)
{
	g_autofree char *path = user_file (uid, "emb");
	gsize size = sizeof TEMPLATE_MAGIC + sizeof (guint32) + count * sizeof (float) * FACE_EMBEDDING_SIZE;
	g_autofree char *data = g_malloc (size);
	guint32 n = count;

	memcpy (data, TEMPLATE_MAGIC, sizeof TEMPLATE_MAGIC);
	memcpy (data + sizeof TEMPLATE_MAGIC, &n, sizeof n);
	memcpy (data + sizeof TEMPLATE_MAGIC + sizeof n, templates, count * sizeof (float) * FACE_EMBEDDING_SIZE);
	return g_file_set_contents_full (path, data, size, G_FILE_SET_CONTENTS_CONSISTENT, 0600, error);
}

/* ---- Worker ---- */

static gboolean
cancelled (void)
{
	gboolean value;

	g_mutex_lock (&self.lock);
	value = self.cancel;
	g_mutex_unlock (&self.lock);
	return value;
}

typedef struct {
	uid_t uid;
	guint percent;
	char *hint;
} Progress;

static gboolean
emit_progress (gpointer data)
{
	Progress *progress = data;

	g_dbus_connection_emit_signal (self.bus, NULL, OBJECT_PATH, INTERFACE, "EnrollProgress",
				       g_variant_new ("(uus)", (guint32) progress->uid,
						      progress->percent, progress->hint),
				       NULL);
	g_free (progress->hint);
	g_free (progress);
	return G_SOURCE_REMOVE;
}

static void
report_progress (uid_t uid, guint percent, const char *hint)
{
	Progress *progress = g_new (Progress, 1);

	progress->uid = uid;
	progress->percent = percent;
	progress->hint = g_strdup (hint);
	g_idle_add (emit_progress, progress);
}

/* Whether a frame is good enough to enroll or compare; else a hint why not */
static const char *
check_frame (const FaceResult *result, int found)
{
	float dx, dy;

	if (found <= 0)
		return "no-face";
	dx = result->keypoints[1].x - result->keypoints[0].x;
	dy = result->keypoints[1].y - result->keypoints[0].y;
	if (result->score < GOOD_SCORE || result->presence < GOOD_PRESENCE)
		return "unclear";
	if (sqrtf (dx * dx + dy * dy) < GOOD_MIN_EYE_DISTANCE)
		return "too-far";
	if (fabsf (atan2f (dy, dx)) > GOOD_MAX_TILT)
		return "tilted";
	return NULL;
}

/* Whether the kernel started the APU, the NPU's own processor: then remoteproc lists it as
 * apusys-rv. Without it MediaTek's NNAPI library still offers mtk-neuron, falls back to ArmNN on
 * the GPU and aborts the whole process when that fails (09.11.2026, kernel 6.1.192-7). */
static gboolean
apu_running (void)
{
	g_autoptr (GDir) dir = g_dir_open ("/sys/class/remoteproc", 0, NULL);
	const char *entry;

	while (dir && (entry = g_dir_read_name (dir))) {
		g_autofree char *path = g_build_filename ("/sys/class/remoteproc", entry, "name",
							  NULL);
		g_autofree char *name = NULL;

		if (g_file_get_contents (path, &name, NULL, NULL) &&
		    g_strcmp0 (g_strstrip (name), "apusys-rv") == 0)
			return TRUE;
	}
	return FALSE;
}

static gboolean
ensure_pipeline (char **error)
{
	PlTflOptions options;

	if (self.pipeline)
		return TRUE;
	pltfl_options_init (&options);
	if (apu_running ())
		options.accelerator = "mtk-neuron";
	else
		g_message ("the APU is not running, models on the CPU");
	options.allow_fp16 = 1;
	options.cache_dir = CACHE_DIR;
	self.pipeline = face_pipeline_new (MODEL_DIR, &options, error);
	if (self.pipeline)
		g_message ("models loaded: %s", face_pipeline_describe (self.pipeline));
	return self.pipeline != NULL;
}

/* Runs frames through the chain until done() says enough or the time is up. Returns FALSE with
 * job->message set when the camera or the chain failed. */
typedef gboolean (*FrameCallback) (Job *job, const FaceResult *result, const char *hint,
				   gpointer data);

static gboolean
run_camera (Job *job, int timeout_s, FrameCallback on_frame, gpointer data)
{
	g_autofree char *error = NULL;
	g_autofree uint8_t *buffer = NULL;
	gint64 until = g_get_monotonic_time () + (gint64) timeout_s * G_USEC_PER_SEC;
	FaceCamera *camera;
	FaceFrame frame;
	size_t bytes;
	gboolean ok = TRUE;

	if (!ensure_pipeline (&error)) {
		job->message = g_strdup_printf ("models: %s", error);
		return FALSE;
	}
	camera = face_camera_open (&error);
	if (!camera) {
		job->message = g_strdup_printf ("camera: %s", error);
		return FALSE;
	}
	face_camera_format (camera, &frame.width, &frame.height, &frame.orientation, &bytes);
	buffer = g_malloc (bytes);
	frame.nv21 = buffer;

	while (!cancelled () && g_get_monotonic_time () < until) {
		FaceResult result;
		int got = face_camera_next_frame (camera, buffer, 1000, NULL), found;

		if (got < 0) {
			job->message = g_strdup ("camera error");
			ok = FALSE;
			break;
		}
		if (got == 0)
			continue;
		found = face_pipeline_run (self.pipeline, &frame, &result);
		if (found < 0) {
			job->message = g_strdup ("recognition failed");
			ok = FALSE;
			break;
		}
		if (on_frame (job, &result, check_frame (&result, found), data))
			break;
	}
	face_camera_close (camera);
	return ok;
}

typedef struct {
	float samples[ENROLL_FRAMES][FACE_EMBEDDING_SIZE];
	int count;
	gint64 last_hint;
} Enrollment;

static gboolean
on_enroll_frame (Job *job, const FaceResult *result, const char *hint, gpointer data)
{
	Enrollment *enrollment = data;
	gint64 now = g_get_monotonic_time ();

	if (hint) {
		/* Hints at most twice a second, so a settings page can show them without flicker */
		if (now - enrollment->last_hint > G_USEC_PER_SEC / 2) {
			report_progress (job->uid, enrollment->count * 100 / ENROLL_FRAMES, hint);
			enrollment->last_hint = now;
		}
		return FALSE;
	}
	memcpy (enrollment->samples[enrollment->count++], result->embedding, sizeof result->embedding);
	report_progress (job->uid, enrollment->count * 100 / ENROLL_FRAMES, "");
	return enrollment->count == ENROLL_FRAMES;
}

static void
enroll (Job *job)
{
	g_autofree Enrollment *enrollment = g_new0 (Enrollment, 1);
	float templates[MAX_TEMPLATES][FACE_EMBEDDING_SIZE] = { { 0 } };
	g_autoptr (GError) error = NULL;
	int count = 1;
	float norm = 0;

	if (!run_camera (job, ENROLL_TIMEOUT_S, on_enroll_frame, enrollment))
		return;
	if (cancelled ()) {
		job->message = g_strdup ("cancelled");
		return;
	}
	if (enrollment->count < ENROLL_FRAMES) {
		job->message = g_strdup ("not enough clear pictures of the face");
		return;
	}
	/* Template 0 is the mean, then a spread of single frames */
	for (int n = 0; n < enrollment->count; n++)
		for (int v = 0; v < FACE_EMBEDDING_SIZE; v++)
			templates[0][v] += enrollment->samples[n][v];
	for (int v = 0; v < FACE_EMBEDDING_SIZE; v++)
		norm += templates[0][v] * templates[0][v];
	norm = sqrtf (norm);
	for (int v = 0; v < FACE_EMBEDDING_SIZE; v++)
		templates[0][v] /= norm;
	for (int n = 0; n < enrollment->count && count < MAX_TEMPLATES; n += ENROLL_KEEP_EVERY)
		memcpy (templates[count++], enrollment->samples[n], sizeof templates[0]);

	if (!save_templates (job->uid, templates, count, &error)) {
		job->message = g_strdup_printf ("cannot store the face: %s", error->message);
		return;
	}
	g_message ("uid %u: face enrolled (%d templates)", (unsigned) job->uid, count);
	job->success = TRUE;
}

typedef struct {
	float templates[MAX_TEMPLATES][FACE_EMBEDDING_SIZE];
	int count;
	float best[VERIFY_FRAMES];
	int frames;
} Verification;

static gboolean
on_verify_frame (Job *job, const FaceResult *result, const char *hint, gpointer data)
{
	Verification *verification = data;
	float best = -1;

	if (hint)
		return FALSE;
	for (int t = 0; t < verification->count; t++) {
		float similarity = face_similarity (result->embedding, verification->templates[t]);
		if (similarity > best)
			best = similarity;
	}
	verification->best[verification->frames++] = best;
	return verification->frames == VERIFY_FRAMES;
}

static int
compare_floats (const void *a, const void *b)
{
	float x = *(const float *) a, y = *(const float *) b;

	return (x > y) - (x < y);
}

static void
verify (Job *job)
{
	g_autofree Verification *verification = g_new0 (Verification, 1);
	Settings settings = load_settings (job->uid);
	float threshold = settings.strict ? THRESHOLD_STRICT : THRESHOLD_BALANCED;

	verification->count = load_templates (job->uid, verification->templates);
	if (verification->count == 0) {
		job->message = g_strdup ("no face enrolled");
		return;
	}
	if (!run_camera (job, VERIFY_TIMEOUT_S, on_verify_frame, verification))
		return;
	if (verification->frames == 0) {
		job->message = g_strdup ("no clear picture of a face");
		return;
	}
	/* The median of the frames, so one lucky frame does not decide; of an even number the
	 * lower of the two middle values (with two frames the worse one) */
	qsort (verification->best, verification->frames, sizeof (float), compare_floats);
	job->similarity = verification->best[(verification->frames - 1) / 2];
	job->success = job->similarity >= threshold;
	g_message ("uid %u: verify %.3f over %d frames (threshold %.2f)", (unsigned) job->uid,
		   job->similarity, verification->frames, threshold);
}

typedef struct {
	float templates[MAX_TEMPLATES][FACE_EMBEDDING_SIZE];
	int count;
	float threshold;
	int in_a_row;
} Unlocking;

static gboolean
on_unlock_frame (Job *job, const FaceResult *result, const char *hint, gpointer data)
{
	Unlocking *unlocking = data;
	float best = -1;

	if (hint) {
		/* No usable face in this frame: the series starts again */
		unlocking->in_a_row = 0;
		return FALSE;
	}
	job->face_seen = TRUE;
	for (int t = 0; t < unlocking->count; t++) {
		float similarity = face_similarity (result->embedding, unlocking->templates[t]);
		if (similarity > best)
			best = similarity;
	}
	unlocking->in_a_row = best >= unlocking->threshold ? unlocking->in_a_row + 1 : 0;
	if (best > job->similarity)
		job->similarity = best;
	return unlocking->in_a_row >= UNLOCK_MATCHES;
}

static void
unlock_attempt (Job *job)
{
	g_autofree Unlocking *unlocking = g_new0 (Unlocking, 1);
	Settings settings = load_settings (job->uid);

	unlocking->threshold = settings.strict ? THRESHOLD_STRICT : THRESHOLD_BALANCED;
	unlocking->count = load_templates (job->uid, unlocking->templates);
	if (!settings.enabled || unlocking->count == 0)
		return;
	job->similarity = -1;
	if (run_camera (job, UNLOCK_TIMEOUT_S, on_unlock_frame, unlocking))
		job->success = unlocking->in_a_row >= UNLOCK_MATCHES;
}

static gboolean finish_job (gpointer data);

static gpointer
worker (gpointer data)
{
	Job *job = data;
	g_autofree char *error = NULL;

	switch (job->kind) {
	case JOB_ENROLL: enroll (job); break;
	case JOB_VERIFY: verify (job); break;
	case JOB_UNLOCK: unlock_attempt (job); break;
	case JOB_PRELOAD:
		if (!ensure_pipeline (&error))
			g_warning ("models: %s", error);
		break;
	}
	g_idle_add (finish_job, job);
	return NULL;
}

/* ---- Unlocking: sessions and screen ---- */

#define LOGIND "org.freedesktop.login1"
#define LOGIND_PATH "/org/freedesktop/login1"

static gboolean start_job (JobKind kind, uid_t uid, GDBusMethodInvocation *invocation,
			   const char *session);
static void sync_models (void);
static void restore_watch (Watch *watch);

/* Whether any user switched face unlock on (and has a face): then the service stays */
static gboolean
any_enabled (void)
{
	g_autoptr (GDir) dir = g_dir_open (STATE_DIR, 0, NULL);
	const char *name;

	while (dir && (name = g_dir_read_name (dir))) {
		float templates[MAX_TEMPLATES][FACE_EMBEDDING_SIZE];
		unsigned uid;
		char end;

		if (sscanf (name, "%u.con%c", &uid, &end) == 2 && end == 'f' &&
		    load_settings (uid).enabled && load_templates (uid, templates) > 0)
			return TRUE;
	}
	return FALSE;
}

static gboolean
read_screen_on (void)
{
	g_autofree char *state = NULL;
	glob_t aod;
	gboolean dozing;

	/* The always-on display counts as off: the panel dozes, and dpms goes Off and On with its
	 * CRTC for each new picture, which must not look like the screen coming on. Waking up
	 * removes the flag, so the try starts then. */
	dozing = glob (AOD_FLAG, GLOB_NOSORT, NULL, &aod) == 0 && aod.gl_pathc > 0;
	globfree (&aod);
	if (dozing)
		return FALSE;

	if (!self.dpms_path || !g_file_get_contents (self.dpms_path, &state, NULL, NULL))
		return FALSE;
	return g_str_has_prefix (state, "On");
}

static void
watch_free (gpointer data)
{
	Watch *watch = data;

	g_free (watch->id);
	g_free (watch->path);
	g_free (watch);
}

static gboolean
session_locked (const char *path)
{
	g_autoptr (GVariant) reply = g_dbus_connection_call_sync (
		self.bus, LOGIND, path, "org.freedesktop.DBus.Properties", "Get",
		g_variant_new ("(ss)", "org.freedesktop.login1.Session", "LockedHint"),
		G_VARIANT_TYPE ("(v)"), G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL);
	g_autoptr (GVariant) value = NULL;

	if (!reply)
		return FALSE;
	g_variant_get (reply, "(v)", &value);
	return g_variant_get_boolean (value);
}

/* Takes user sessions on a seat into the watch list. Not by their type: greetd opens the
 * session as "tty" and phosh-session switches it to "wayland" only later (SetType), after
 * SessionNew at boot. Only a lock screen sets LockedHint, so a text console never tries. */
static void
add_session (const char *id, uid_t uid, const char *seat, const char *path)
{
	g_autoptr (GVariant) reply = NULL;
	g_autoptr (GVariant) class = NULL;
	Watch *watch;

	if (!seat || !*seat || g_hash_table_contains (self.sessions, path))
		return;
	reply = g_dbus_connection_call_sync (self.bus, LOGIND, path, "org.freedesktop.DBus.Properties",
					     "Get", g_variant_new ("(ss)", "org.freedesktop.login1.Session", "Class"),
					     G_VARIANT_TYPE ("(v)"), G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL);
	if (!reply)
		return;
	g_variant_get (reply, "(v)", &class);
	if (g_strcmp0 (g_variant_get_string (class, NULL), "user") != 0)
		return;
	watch = g_new0 (Watch, 1);
	watch->id = g_strdup (id);
	watch->path = g_strdup (path);
	watch->uid = uid;
	watch->locked = session_locked (path);
	restore_watch (watch);
	g_hash_table_insert (self.sessions, watch->path, watch);
	g_message ("watching session %s of uid %u (%s)", id, (unsigned) uid,
		   watch->locked ? "locked" : "unlocked");
}

static void
on_unlocked (GObject *source, GAsyncResult *res, gpointer data)
{
	g_autoptr (GError) error = NULL;
	g_autoptr (GVariant) reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), res, &error);

	if (!reply)
		g_warning ("UnlockSession: %s", error->message);
}

/* The screen came on, or a session got locked: try where it makes sense */
static void
try_unlock (void)
{
	GHashTableIter iter;
	gpointer value;

	if (!self.screen_on)
		return;
	g_hash_table_iter_init (&iter, self.sessions);
	while (g_hash_table_iter_next (&iter, NULL, &value)) {
		Watch *watch = value;

		if (!watch->locked || !watch->pin_seen || watch->failures >= UNLOCK_MAX_FAILURES ||
		    !load_settings (watch->uid).enabled)
			continue;
		if (start_job (JOB_UNLOCK, watch->uid, NULL, watch->id))
			return;
		/* Busy, most likely with the models: finish_job tries again */
		self.unlock_pending = TRUE;
		return;
	}
}

/* Whether this session could be unlocked by the face right now (see try_unlock) */
static gboolean
watch_wants_models (const Watch *watch)
{
	return watch->locked && watch->pin_seen && watch->failures < UNLOCK_MAX_FAILURES &&
	       load_settings (watch->uid).enabled;
}

/* Writes what only this process knows about the sessions, for restore_watch after the re-exec */
static void
save_sessions (void)
{
	g_autoptr (GString) state = g_string_new (NULL);
	g_autoptr (GError) error = NULL;
	GHashTableIter iter;
	gpointer value;

	g_hash_table_iter_init (&iter, self.sessions);
	while (g_hash_table_iter_next (&iter, NULL, &value)) {
		Watch *watch = value;

		g_string_append_printf (state, "%s %d %d\n", watch->id, watch->pin_seen, watch->failures);
	}
	if (!g_file_set_contents_full (STATE_FILE, state->str, state->len, G_FILE_SET_CONTENTS_CONSISTENT,
				       0600, &error))
		g_warning ("%s", error->message);
}

/* Takes over what the process before the re-exec knew about this session. A session it did not
 * know stays as new: the face only unlocks after the next PIN, the safe side. */
static void
restore_watch (Watch *watch)
{
	g_autofree char *state = NULL;
	g_auto (GStrv) lines = NULL;

	if (!g_file_get_contents (STATE_FILE, &state, NULL, NULL))
		return;
	lines = g_strsplit (state, "\n", -1);
	for (char **line = lines; *line; line++) {
		char id[64];
		int pin_seen, failures;

		if (sscanf (*line, "%63s %d %d", id, &pin_seen, &failures) == 3 &&
		    g_strcmp0 (id, watch->id) == 0) {
			watch->pin_seen = pin_seen;
			watch->failures = failures;
		}
	}
}

/* The delay after the last unlock is over: drop the models, unless a session wants them again by
 * now. The new process lists the sessions afresh, so it also sees a lock from in between. */
static gboolean
on_release_due (gpointer data)
{
	g_autofree char *self_path = NULL;
	GHashTableIter iter;
	gpointer value;

	self.release_source = 0;
	if (self.job || !self.pipeline)
		return G_SOURCE_REMOVE;
	g_hash_table_iter_init (&iter, self.sessions);
	while (g_hash_table_iter_next (&iter, NULL, &value))
		if (watch_wants_models (value))
			return G_SOURCE_REMOVE;
	self_path = g_file_read_link ("/proc/self/exe", NULL);
	if (!self_path)
		return G_SOURCE_REMOVE;
	save_sessions ();
	g_message ("dropping the models");
	/* Not all of the vendor's descriptors are close-on-exec; the bus connection goes too */
	close_range (3, UINT_MAX, 0);
	execl (self_path, self_path, NULL);
	/* Without the bus connection this process is of no use any more: systemd starts it again */
	_exit (1);
}

/* Loads or drops the models to match the sessions. Runs after every change of the sessions and
 * after every job, so a job in progress only delays it. */
static void
sync_models (void)
{
	GHashTableIter iter;
	gpointer value;
	gboolean wanted = FALSE;

	if (!self.sessions || self.job)
		return;
	g_hash_table_iter_init (&iter, self.sessions);
	while (!wanted && g_hash_table_iter_next (&iter, NULL, &value))
		wanted = watch_wants_models (value);
	if (wanted) {
		g_clear_handle_id (&self.release_source, g_source_remove);
		if (!self.pipeline && !self.models_retry)
			start_job (JOB_PRELOAD, 0, NULL, NULL);
	} else if (self.pipeline && !self.release_source) {
		self.release_source = g_timeout_add_seconds (RELEASE_MODELS_S, on_release_due, NULL);
	}
}

static gboolean
on_models_retry (gpointer data)
{
	self.models_retry = 0;
	sync_models ();
	return G_SOURCE_REMOVE;
}

static gboolean
on_screen_poll (gpointer data)
{
	gboolean on = read_screen_on ();
	gboolean was_on = self.screen_on;

	self.screen_on = on;
	if (on && !was_on)
		try_unlock ();
	if (!on) {
		self.unlock_pending = FALSE;
		/* Screen off: a running try stops */
		g_mutex_lock (&self.lock);
		if (self.job && self.job->kind == JOB_UNLOCK)
			self.cancel = TRUE;
		g_mutex_unlock (&self.lock);
	}
	return G_SOURCE_CONTINUE;
}

static void
on_session_changed (GDBusConnection *bus, const char *sender, const char *path, const char *iface,
		    const char *signal, GVariant *parameters, gpointer data)
{
	Watch *watch = g_hash_table_lookup (self.sessions, path);
	g_autoptr (GVariant) changed = NULL;
	const char *interface;
	gboolean locked;

	if (!watch)
		return;
	g_variant_get (parameters, "(&s@a{sv}@as)", &interface, &changed, NULL);
	if (g_strcmp0 (interface, "org.freedesktop.login1.Session") != 0 ||
	    !g_variant_lookup (changed, "LockedHint", "b", &locked) || locked == watch->locked)
		return;
	watch->locked = locked;
	if (!locked) {
		if (watch->by_face) {
			watch->by_face = FALSE;
		} else {
			/* PIN, pattern or fingerprint: the face may unlock again */
			watch->pin_seen = TRUE;
			watch->failures = 0;
		}
		g_mutex_lock (&self.lock);
		if (self.job && self.job->kind == JOB_UNLOCK)
			self.cancel = TRUE;
		g_mutex_unlock (&self.lock);
	}
	g_message ("session %s %s", watch->id, locked ? "locked" : "unlocked");
	if (!locked)
		self.unlock_pending = FALSE;
	/* Locking with the screen still on does not try: that would undo the lock button */
	sync_models ();
}

static void
on_session_new (GDBusConnection *bus, const char *sender, const char *path, const char *iface,
		const char *signal, GVariant *parameters, gpointer data)
{
	g_autoptr (GVariant) reply = NULL;
	const char *id, *object;
	guint32 uid;
	const char *seat;

	g_variant_get (parameters, "(&s&o)", &id, &object);
	reply = g_dbus_connection_call_sync (bus, LOGIND, object, "org.freedesktop.DBus.Properties", "GetAll",
					     g_variant_new ("(s)", "org.freedesktop.login1.Session"),
					     G_VARIANT_TYPE ("(a{sv})"), G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL);
	if (reply) {
		g_autoptr (GVariant) props = g_variant_get_child_value (reply, 0);
		g_autoptr (GVariant) user = g_variant_lookup_value (props, "User", G_VARIANT_TYPE ("(uo)"));
		g_autoptr (GVariant) seat_value = g_variant_lookup_value (props, "Seat", G_VARIANT_TYPE ("(so)"));

		if (!user || !seat_value)
			return;
		g_variant_get (user, "(u&o)", &uid, NULL);
		g_variant_get (seat_value, "(&s&o)", &seat, NULL);
		add_session (id, uid, seat, object);
	}
}

static void
on_session_removed (GDBusConnection *bus, const char *sender, const char *path, const char *iface,
		    const char *signal, GVariant *parameters, gpointer data)
{
	const char *id, *object;

	g_variant_get (parameters, "(&s&o)", &id, &object);
	g_hash_table_remove (self.sessions, object);
}

static void
start_watching (GDBusConnection *bus)
{
	g_autoptr (GVariant) reply = NULL;
	g_autoptr (GVariantIter) iter = NULL;
	const char *id, *seat, *object;
	g_auto (GStrv) dpms = NULL;
	guint32 uid;
	glob_t found;

	self.sessions = g_hash_table_new_full (g_str_hash, g_str_equal, NULL, watch_free);
	g_dbus_connection_signal_subscribe (bus, LOGIND, "org.freedesktop.DBus.Properties",
					    "PropertiesChanged", NULL, "org.freedesktop.login1.Session",
					    G_DBUS_SIGNAL_FLAGS_NONE, on_session_changed, NULL, NULL);
	g_dbus_connection_signal_subscribe (bus, LOGIND, "org.freedesktop.login1.Manager", "SessionNew",
					    LOGIND_PATH, NULL, G_DBUS_SIGNAL_FLAGS_NONE, on_session_new,
					    NULL, NULL);
	g_dbus_connection_signal_subscribe (bus, LOGIND, "org.freedesktop.login1.Manager",
					    "SessionRemoved", LOGIND_PATH, NULL, G_DBUS_SIGNAL_FLAGS_NONE,
					    on_session_removed, NULL, NULL);
	reply = g_dbus_connection_call_sync (bus, LOGIND, LOGIND_PATH, "org.freedesktop.login1.Manager",
					     "ListSessions", NULL, G_VARIANT_TYPE ("(a(susso))"),
					     G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL);
	if (reply) {
		g_variant_get (reply, "(a(susso))", &iter);
		while (g_variant_iter_next (iter, "(&su&s&s&o)", &id, &uid, NULL, &seat, &object))
			add_session (id, uid, seat, object);
	}
	/* The panel: DSI on this phone */
	if (glob (DPMS_GLOB, 0, NULL, &found) == 0 && found.gl_pathc > 0)
		self.dpms_path = g_strdup (found.gl_pathv[0]);
	globfree (&found);
	self.screen_on = read_screen_on ();
	self.screen_source = g_timeout_add (SCREEN_POLL_MS, on_screen_poll, NULL);
}

/* ---- Bus ---- */

static gboolean
on_idle_exit (gpointer data)
{
	self.idle_source = 0;
	/* With face unlock on, the service watches the lock screen and stays */
	if (any_enabled ())
		return G_SOURCE_REMOVE;
	g_message ("idle, exiting");
	pltfl_exit (0);
}

static void
schedule_idle_exit (void)
{
	if (self.idle_source)
		g_source_remove (self.idle_source);
	self.idle_source = g_timeout_add_seconds (IDLE_EXIT_S, on_idle_exit, NULL);
}

static void
finish_unlock (Job *job)
{
	GHashTableIter iter;
	gpointer value;

	g_hash_table_iter_init (&iter, self.sessions);
	while (g_hash_table_iter_next (&iter, NULL, &value)) {
		Watch *watch = value;

		if (g_strcmp0 (watch->id, job->session) != 0)
			continue;
		if (job->success && watch->locked) {
			g_message ("uid %u: face recognized (%.3f), unlocking session %s",
				   (unsigned) job->uid, job->similarity, watch->id);
			watch->by_face = TRUE;
			g_dbus_connection_call (self.bus, LOGIND, LOGIND_PATH, "org.freedesktop.login1.Manager",
						"UnlockSession", g_variant_new ("(s)", watch->id), NULL,
						G_DBUS_CALL_FLAGS_NONE, -1, NULL, on_unlocked, NULL);
		} else if (job->face_seen && !job->success && watch->locked) {
			watch->failures++;
			g_message ("uid %u: face not recognized (best %.3f), try %d of %d",
				   (unsigned) job->uid, job->similarity, watch->failures, UNLOCK_MAX_FAILURES);
		}
	}
}

static gboolean
finish_job (gpointer data)
{
	Job *job = data;

	switch (job->kind) {
	case JOB_ENROLL:
		g_dbus_connection_emit_signal (self.bus, NULL, OBJECT_PATH, INTERFACE, "EnrollFinished",
					       g_variant_new ("(ubs)", (guint32) job->uid, job->success,
							      job->message ? job->message : ""),
					       NULL);
		break;
	case JOB_VERIFY:
		if (job->message)
			g_dbus_method_invocation_return_dbus_error (job->invocation, ERROR_PREFIX "Failed",
								    job->message);
		else
			g_dbus_method_invocation_return_value (
				job->invocation, g_variant_new ("(bd)", job->success, job->similarity));
		break;
	case JOB_UNLOCK:
		finish_unlock (job);
		break;
	case JOB_PRELOAD:
		break;
	}
	/* Loading the models failed: not again at once from sync_models () below */
	if (self.pipeline)
		g_clear_handle_id (&self.models_retry, g_source_remove);
	else if (job->kind == JOB_PRELOAD && !self.models_retry)
		self.models_retry = g_timeout_add_seconds (MODELS_RETRY_S, on_models_retry, NULL);
	g_mutex_lock (&self.lock);
	self.job = NULL;
	self.cancel = FALSE;
	g_mutex_unlock (&self.lock);
	g_free (job->message);
	g_free (job->session);
	g_free (job);
	schedule_idle_exit ();
	if (self.unlock_pending) {
		self.unlock_pending = FALSE;
		try_unlock ();
	}
	sync_models ();
	return G_SOURCE_REMOVE;
}

/* The user behind the sender, as the bus daemon knows it */
static gboolean
caller_uid (GDBusConnection *bus, const char *sender, uid_t *uid)
{
	g_autoptr (GVariant) reply = g_dbus_connection_call_sync (
		bus, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
		"GetConnectionUnixUser", g_variant_new ("(s)", sender), G_VARIANT_TYPE ("(u)"),
		G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL);
	guint32 value;

	if (!reply)
		return FALSE;
	g_variant_get (reply, "(u)", &value);
	*uid = value;
	return TRUE;
}

static gboolean
start_job (JobKind kind, uid_t uid, GDBusMethodInvocation *invocation, const char *session)
{
	Job *job;

	g_mutex_lock (&self.lock);
	if (self.job) {
		g_mutex_unlock (&self.lock);
		return FALSE;
	}
	job = g_new0 (Job, 1);
	job->kind = kind;
	job->uid = uid;
	job->invocation = invocation;
	job->session = g_strdup (session);
	self.job = job;
	self.cancel = FALSE;
	g_mutex_unlock (&self.lock);
	if (self.idle_source) {
		g_source_remove (self.idle_source);
		self.idle_source = 0;
	}
	g_thread_unref (g_thread_new ("face", worker, job));
	return TRUE;
}

static void handle_method (GDBusMethodInvocation *invocation, uid_t uid);

/* ---- polkit ---- */

#define POLKIT_ACTION "org.plinius.face.enroll"

typedef struct {
	GDBusMethodInvocation *invocation;
	uid_t uid;
} PendingAuth;

static void
on_authorization (GObject *source, GAsyncResult *res, gpointer data)
{
	PendingAuth *pending = data;
	g_autoptr (GError) error = NULL;
	g_autoptr (GVariant) reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), res, &error);
	gboolean authorized = FALSE, challenge = FALSE;

	if (reply) {
		g_autoptr (GVariant) details = NULL;

		g_variant_get (reply, "((bb@a{ss}))", &authorized, &challenge, &details);
	} else {
		g_warning ("polkit: %s", error->message);
	}
	if (authorized)
		handle_method (pending->invocation, pending->uid);
	else
		g_dbus_method_invocation_return_dbus_error (pending->invocation, ERROR_PREFIX "Denied",
							    "not authorized");
	g_free (pending);
}

/* Asks polkit whether the sender may enroll, letting the user's agent ask for the PIN */
static void
check_authorization (GDBusConnection *bus, const char *sender, GDBusMethodInvocation *invocation,
		     uid_t uid)
{
	PendingAuth *pending = g_new (PendingAuth, 1);
	GVariantBuilder subject;

	pending->invocation = invocation;
	pending->uid = uid;
	g_variant_builder_init (&subject, G_VARIANT_TYPE ("a{sv}"));
	g_variant_builder_add (&subject, "{sv}", "name", g_variant_new_string (sender));
	g_dbus_connection_call (bus, "org.freedesktop.PolicyKit1", "/org/freedesktop/PolicyKit1/Authority",
				"org.freedesktop.PolicyKit1.Authority", "CheckAuthorization",
				g_variant_new ("((sa{sv})sa{ss}us)", "system-bus-name", &subject,
					       POLKIT_ACTION, NULL, 1 /* AllowUserInteraction */, ""),
				G_VARIANT_TYPE ("((bba{ss}))"), G_DBUS_CALL_FLAGS_NONE,
				G_MAXINT /* the user may take a while for the PIN */, NULL,
				on_authorization, pending);
}

static void
on_method_call (GDBusConnection *bus, const char *sender, const char *path, const char *iface,
		const char *method, GVariant *parameters, GDBusMethodInvocation *invocation,
		gpointer data)
{
	gboolean enable = FALSE, loosen = FALSE;
	uid_t uid;

	if (!caller_uid (bus, sender, &uid)) {
		g_dbus_method_invocation_return_dbus_error (invocation, ERROR_PREFIX "Denied",
							    "unknown caller");
		return;
	}
	schedule_idle_exit ();
	if (g_strcmp0 (method, "SetEnabled") == 0)
		g_variant_get (parameters, "(b)", &enable);
	/* Anything but "strict" is refused in handle_method; only the way to "balanced" is asked */
	if (g_strcmp0 (method, "SetStrictness") == 0 &&
	    g_variant_is_of_type (parameters, G_VARIANT_TYPE ("(s)"))) {
		const char *value;

		g_variant_get (parameters, "(&s)", &value);
		loosen = g_strcmp0 (value, "balanced") == 0;
	}
	if (g_strcmp0 (method, "Enroll") == 0 || enable || loosen)
		check_authorization (bus, sender, invocation, uid);
	else
		handle_method (invocation, uid);
}

static void
handle_method (GDBusMethodInvocation *invocation, uid_t uid)
{
	const char *method = g_dbus_method_invocation_get_method_name (invocation);
	GVariant *parameters = g_dbus_method_invocation_get_parameters (invocation);
	g_autoptr (GError) error = NULL;
	Settings settings = load_settings (uid);

	if (g_strcmp0 (method, "GetUser") == 0) {
		float templates[MAX_TEMPLATES][FACE_EMBEDDING_SIZE];

		g_dbus_method_invocation_return_value (
			invocation, g_variant_new ("(bbs)", load_templates (uid, templates) > 0,
						   settings.enabled,
						   settings.strict ? "strict" : "balanced"));
	} else if (g_strcmp0 (method, "SetEnabled") == 0 ||
		   g_strcmp0 (method, "SetStrictness") == 0) {
		if (g_strcmp0 (method, "SetEnabled") == 0) {
			float templates[MAX_TEMPLATES][FACE_EMBEDDING_SIZE];

			g_variant_get (parameters, "(b)", &settings.enabled);
			if (settings.enabled && load_templates (uid, templates) == 0) {
				g_dbus_method_invocation_return_dbus_error (
					invocation, ERROR_PREFIX "NotEnrolled", "no face enrolled");
				return;
			}
		} else {
			const char *value;

			g_variant_get (parameters, "(&s)", &value);
			if (g_strcmp0 (value, "strict") != 0 && g_strcmp0 (value, "balanced") != 0) {
				g_dbus_method_invocation_return_dbus_error (
					invocation, ERROR_PREFIX "Invalid", "strictness is strict or balanced");
				return;
			}
			settings.strict = g_strcmp0 (value, "strict") == 0;
		}
		if (!save_settings (uid, &settings, &error)) {
			g_dbus_method_invocation_return_dbus_error (invocation, ERROR_PREFIX "Failed",
								    error->message);
			return;
		}
		/* Switching on just asked for the PIN (polkit): as good as an unlock with it */
		if (g_strcmp0 (method, "SetEnabled") == 0 && settings.enabled) {
			GHashTableIter iter;
			gpointer value;

			g_hash_table_iter_init (&iter, self.sessions);
			while (g_hash_table_iter_next (&iter, NULL, &value))
				if (((Watch *) value)->uid == uid) {
					((Watch *) value)->pin_seen = TRUE;
					((Watch *) value)->failures = 0;
				}
		}
		g_dbus_method_invocation_return_value (invocation, NULL);
	} else if (g_strcmp0 (method, "Enroll") == 0) {
		if (!start_job (JOB_ENROLL, uid, NULL, NULL))
			g_dbus_method_invocation_return_dbus_error (invocation, ERROR_PREFIX "Busy",
								    "the camera is in use for another request");
		else
			g_dbus_method_invocation_return_value (invocation, NULL);
	} else if (g_strcmp0 (method, "Verify") == 0) {
		if (!start_job (JOB_VERIFY, uid, invocation, NULL))
			g_dbus_method_invocation_return_dbus_error (invocation, ERROR_PREFIX "Busy",
								    "the camera is in use for another request");
		/* else answered when the job ends */
	} else if (g_strcmp0 (method, "Cancel") == 0) {
		g_mutex_lock (&self.lock);
		/* Only one's own request */
		if (self.job && self.job->uid == uid)
			self.cancel = TRUE;
		g_mutex_unlock (&self.lock);
		g_dbus_method_invocation_return_value (invocation, NULL);
	} else if (g_strcmp0 (method, "Delete") == 0) {
		g_autofree char *templates = user_file (uid, "emb");

		if (g_unlink (templates) != 0 && errno != ENOENT) {
			g_dbus_method_invocation_return_dbus_error (invocation, ERROR_PREFIX "Failed",
								    g_strerror (errno));
			return;
		}
		/* Without a face there is nothing to unlock with */
		settings.enabled = FALSE;
		save_settings (uid, &settings, NULL);
		g_message ("uid %u: face deleted", (unsigned) uid);
		g_dbus_method_invocation_return_value (invocation, NULL);
	} else {
		g_dbus_method_invocation_return_dbus_error (invocation, ERROR_PREFIX "Unknown", method);
	}
}

static const GDBusInterfaceVTable vtable = { on_method_call, NULL, NULL, { 0 } };

static void
on_bus_acquired (GDBusConnection *bus, const char *name, gpointer data)
{
	g_autoptr (GDBusNodeInfo) info = g_dbus_node_info_new_for_xml (introspection, NULL);

	self.bus = bus;
	g_dbus_connection_register_object (bus, OBJECT_PATH, info->interfaces[0], &vtable, NULL, NULL,
					   NULL);
	start_watching (bus);
	/* Only for this re-exec: after a crash the face waits for a PIN again */
	g_unlink (STATE_FILE);
	sync_models ();
}

static void
on_name_lost (GDBusConnection *bus, const char *name, gpointer data)
{
	g_warning ("lost the bus name %s", name);
	pltfl_exit (1);
}

static gboolean
on_signal (gpointer data)
{
	pltfl_exit (0);
}

int
main (void)
{
	g_mutex_init (&self.lock);
	self.loop = g_main_loop_new (NULL, FALSE);
	g_unix_signal_add (SIGTERM, on_signal, NULL);
	g_unix_signal_add (SIGINT, on_signal, NULL);
	g_bus_own_name (G_BUS_TYPE_SYSTEM, BUS_NAME, G_BUS_NAME_OWNER_FLAGS_NONE, on_bus_acquired,
			NULL, on_name_lost, NULL, NULL);
	schedule_idle_exit ();
	g_main_loop_run (self.loop);
	pltfl_exit (0);
}
