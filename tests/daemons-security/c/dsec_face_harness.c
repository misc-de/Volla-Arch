/*
 * Harness for plinius-faced.c (its main() renamed): the decisions of the face service without
 * camera and NPU. The camera and the recognition chain are stubs fed with scripted frames
 * ("frames" command); face_similarity() is the dot product as in face-pipeline.c. The state of
 * /var/lib/plinius-face and /run/plinius-face lives in DSEC_ROOT (any other path is refused,
 * /sys reads as empty), the bus is dsec_fakebus.h, the monotonic clock jumps when the scripted
 * frames run out so a timeout costs no time.
 *
 * Commands on stdin; each prints one line:
 *   frames SPEC...      frames for the next job: s:SIM (a clear face with similarity SIM to
 *                       the templates), n (no face), u (unclear), f (too far), t (tilted),
 *                       e (recognition error), c (camera error)
 *   check SPEC          check_frame(): the hint or "good"
 *   settings UID ENABLED STRICT / templates UID N / load UID
 *   verify UID / enroll UID / unlock UID SESSION   run that job to its end
 *   watch ID UID LOCKED PIN_SEEN FAILURES          a watched session (object path /s/ID)
 *   show ID             "locked pin_seen by_face failures"
 *   lockhint ID 0|1     logind says LockedHint changed
 *   screen 0|1          the panel is on or off     try   try_unlock() and its job
 *   wants ID            watch_wants_models()       restore ID   a watch restored from STATE_FILE
 *   save                save_sessions()
 *   method NAME [ARG]   on_method_call() as the bus caller: the answer ("pending" while polkit
 *                       is asked)
 *   calls               the asynchronous bus calls since the last "calls" (UnlockSession, polkit)
 *   bus KEY VALUE       see dsec_fakebus.h
 */
#define _GNU_SOURCE
#include <errno.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <gio/gio.h>
#include <glib/gstdio.h>

#include "dsec_fakebus.h"
#include "face-camera.h"
#include "face-pipeline.h"

/* ---- Paths ---- */

static char *
redirect (const char *path)
{
    static const struct {
        const char *real, *sub;
    } maps[] = { { "/var/lib/plinius-face", "/state" }, { "/run/plinius-face", "/run" } };
    const char *root = getenv ("DSEC_ROOT");

    if (!root || !*root)
        abort ();
    for (size_t i = 0; i < G_N_ELEMENTS (maps); i++)
        if (g_str_has_prefix (path, maps[i].real))
            return g_strconcat (root, maps[i].sub, path + strlen (maps[i].real), NULL);
    if (g_str_has_prefix (path, "/sys/"))
        return NULL;
    fprintf (stderr, "harness: refused path outside the test: %s\n", path);
    abort ();
}

static gboolean
fake_get_contents (const char *path, char **contents, gsize *length, GError **error)
{
    g_autofree char *p = redirect (path);

    if (!p) {
        g_set_error (error, G_FILE_ERROR, G_FILE_ERROR_NOENT, "not in the test");
        return FALSE;
    }
    return g_file_get_contents (p, contents, length, error);
}

static gboolean
fake_set_contents_full (const char *path, const char *contents, gssize length,
                        GFileSetContentsFlags flags, int mode, GError **error)
{
    g_autofree char *p = redirect (path);

    if (!p) {
        g_set_error (error, G_FILE_ERROR, G_FILE_ERROR_PERM, "not in the test");
        return FALSE;
    }
    return g_file_set_contents_full (p, contents, length, flags, mode, error);
}

static gboolean
fake_key_file_load (GKeyFile *file, const char *path, GKeyFileFlags flags, GError **error)
{
    g_autofree char *p = redirect (path);

    if (!p) {
        g_set_error (error, G_FILE_ERROR, G_FILE_ERROR_NOENT, "not in the test");
        return FALSE;
    }
    return g_key_file_load_from_file (file, p, flags, error);
}

static GDir *
fake_dir_open (const char *path, guint flags, GError **error)
{
    g_autofree char *p = redirect (path);

    if (!p) {
        g_set_error (error, G_FILE_ERROR, G_FILE_ERROR_NOENT, "not in the test");
        return NULL;
    }
    return g_dir_open (p, flags, error);
}

static int
fake_unlink (const char *path)
{
    g_autofree char *p = redirect (path);

    if (!p) {
        errno = EPERM;
        return -1;
    }
    return g_unlink (p);
}

/* ---- Clock: a timeout passes at once when the frames run out ---- */

static GMutex frames_lock;
static gint64 clock_offset; /* under frames_lock: the worker moves it, both threads read it */

static gint64
fake_monotonic (void)
{
    gint64 offset;

    g_mutex_lock (&frames_lock);
    offset = clock_offset;
    g_mutex_unlock (&frames_lock);
    return g_get_monotonic_time () + offset;
}

/* ---- Camera and recognition chain ---- */

static GQueue frames = G_QUEUE_INIT; /* char* specs */

struct FaceCamera {
    int unused;
};

struct FacePipeline {
    int unused;
};

FaceCamera *
face_camera_open (char **error)
{
    if (getenv ("DSEC_NO_CAMERA")) {
        *error = strdup ("no camera here");
        return NULL;
    }
    return g_new0 (FaceCamera, 1);
}

void
face_camera_close (FaceCamera *camera)
{
    g_free (camera);
}

void
face_camera_format (FaceCamera *camera, int *width, int *height, int *orientation, size_t *bytes)
{
    *width = 1280;
    *height = 720;
    *orientation = 270;
    *bytes = 16;
}

static char *current_frame;

int
face_camera_next_frame (FaceCamera *camera, uint8_t *dst, int timeout_ms, int64_t *timestamp_ns)
{
    g_mutex_lock (&frames_lock);
    g_free (current_frame);
    current_frame = g_queue_pop_head (&frames);
    g_mutex_unlock (&frames_lock);
    if (!current_frame) {
        /* Nothing more to see: the job's time is up */
        g_mutex_lock (&frames_lock);
        clock_offset += (gint64) 3600 * G_USEC_PER_SEC;
        g_mutex_unlock (&frames_lock);
        return 0;
    }
    if (!strcmp (current_frame, "c"))
        return -1;
    return 1;
}

FacePipeline *
face_pipeline_new (const char *model_dir, const PlTflOptions *options, char **error)
{
    return g_new0 (FacePipeline, 1);
}

void
face_pipeline_free (FacePipeline *pipeline)
{
    g_free (pipeline);
}

const char *
face_pipeline_describe (FacePipeline *pipeline)
{
    return "stub";
}

float
face_similarity (const float *a, const float *b)
{
    float dot = 0;

    for (int v = 0; v < FACE_EMBEDDING_SIZE; v++)
        dot += a[v] * b[v];
    return dot;
}

/* A frame of @spec; returns what face_pipeline_run() returns */
static int
make_frame (const char *spec, FaceResult *r)
{
    float sim = 0;

    memset (r, 0, sizeof *r);
    if (!strcmp (spec, "n"))
        return 0;
    if (!strcmp (spec, "e"))
        return -1;
    r->score = 0.95f;
    r->presence = 0.95f;
    r->keypoints[0] = (FacePoint) { 300, 500 };
    r->keypoints[1] = (FacePoint) { 400, 500 };
    if (!strcmp (spec, "u"))
        r->score = 0.5f;
    else if (!strcmp (spec, "f"))
        r->keypoints[1] = (FacePoint) { 330, 500 };
    else if (!strcmp (spec, "t"))
        r->keypoints[1] = (FacePoint) { 380, 560 };
    else if (sscanf (spec, "s:%f", &sim) == 1) {
        /* Unit vector at similarity @sim to the template direction e0 */
        r->embedding[0] = sim;
        r->embedding[1] = sqrtf (fmaxf (0, 1 - sim * sim));
    } else if (sscanf (spec, "raw:%f:%f:%f:%f", &r->score, &r->presence, &r->keypoints[1].x,
                       &r->keypoints[1].y) == 4) {
        r->embedding[0] = 1;
    }
    return 1;
}

int
face_pipeline_run (FacePipeline *pipeline, const FaceFrame *frame, FaceResult *result)
{
    return make_frame (current_frame ? current_frame : "n", result);
}

#define g_file_get_contents fake_get_contents
#define g_file_set_contents_full fake_set_contents_full
#define g_key_file_load_from_file fake_key_file_load
#define g_dir_open fake_dir_open
#define g_unlink fake_unlink
#define g_get_monotonic_time fake_monotonic
#define main faced_main
#include "plinius-face/plinius-faced.c"
#undef main
#undef g_get_monotonic_time

/* Runs the main loop until the job has finished (finish_job ran) */
static void
wait_job (void)
{
    while (TRUE) {
        gboolean busy;

        g_mutex_lock (&self.lock);
        busy = self.job != NULL;
        g_mutex_unlock (&self.lock);
        if (!busy)
            break;
        g_main_context_iteration (NULL, TRUE);
    }
    /* Signals and progress queued with the job */
    while (g_main_context_iteration (NULL, FALSE))
        ;
}

static Watch *
watch_by_id (const char *id)
{
    GHashTableIter iter;
    gpointer value;

    g_hash_table_iter_init (&iter, self.sessions);
    while (g_hash_table_iter_next (&iter, NULL, &value))
        if (!strcmp (((Watch *) value)->id, id))
            return value;
    return NULL;
}

static void
print_calls (void)
{
    printf ("calls %s\n", fb.async_calls && fb.async_calls->len ? fb.async_calls->str : "-");
    if (fb.async_calls)
        g_string_truncate (fb.async_calls, 0);
}

int
main (void)
{
    static char line[8192];

    setvbuf (stdout, NULL, _IOLBF, 0);
    g_mutex_init (&self.lock);
    self.sessions = g_hash_table_new_full (g_str_hash, g_str_equal, NULL, watch_free);
    while (fgets (line, sizeof line, stdin)) {
        char a[1024] = "", b[1024] = "";
        long n1, n2, n3, n4;

        line[strcspn (line, "\n")] = '\0';
        if (fb_command (line))
            continue;
        if (!strncmp (line, "frames", 6)) {
            g_auto (GStrv) parts = g_strsplit (line + 6, " ", -1);

            g_mutex_lock (&frames_lock);
            g_queue_clear_full (&frames, g_free);
            for (char **p = parts; *p; p++)
                if (**p)
                    g_queue_push_tail (&frames, g_strdup (*p));
            g_mutex_unlock (&frames_lock);
            puts ("ok");
        } else if (sscanf (line, "check %1023s", a) == 1) {
            FaceResult r;
            int found = make_frame (a, &r);
            const char *hint = check_frame (&r, found);

            puts (hint ? hint : "good");
        } else if (sscanf (line, "settings %ld %ld %ld", &n1, &n2, &n3) == 3) {
            Settings s = { n2 != 0, n3 != 0 };

            puts (save_settings ((uid_t) n1, &s, NULL) ? "ok" : "error");
        } else if (sscanf (line, "templates %ld %ld", &n1, &n2) == 2) {
            static float t[MAX_TEMPLATES][FACE_EMBEDDING_SIZE];

            memset (t, 0, sizeof t);
            for (int i = 0; i < n2 && i < MAX_TEMPLATES; i++)
                t[i][0] = 1;
            puts (save_templates ((uid_t) n1, t, (int) n2, NULL) ? "ok" : "error");
        } else if (sscanf (line, "load %ld", &n1) == 1) {
            static float t[MAX_TEMPLATES][FACE_EMBEDDING_SIZE];
            Settings s = load_settings ((uid_t) n1);
            int count = load_templates ((uid_t) n1, t);
            float norm = 0;

            for (int v = 0; count > 0 && v < FACE_EMBEDDING_SIZE; v++)
                norm += t[0][v] * t[0][v];
            printf ("templates %d enabled %d strict %d norm %.3f\n", count, s.enabled, s.strict, norm);
        } else if (sscanf (line, "verify %ld", &n1) == 1) {
            FakeInv inv = { 0 };

            fake_inv_reset (&inv, "Verify", NULL);
            if (!start_job (JOB_VERIFY, (uid_t) n1, (GDBusMethodInvocation *) &inv, NULL))
                puts ("busy");
            else {
                wait_job ();
                fake_inv_print (&inv);
            }
            fake_inv_reset (&inv, NULL, NULL);
        } else if (sscanf (line, "enroll %ld", &n1) == 1) {
            int before = fb.signals;

            start_job (JOB_ENROLL, (uid_t) n1, NULL, NULL);
            wait_job ();
            printf ("signals %d\n", fb.signals - before);
        } else if (sscanf (line, "unlock %ld %1023s", &n1, a) == 2) {
            start_job (JOB_UNLOCK, (uid_t) n1, NULL, a);
            wait_job ();
            print_calls ();
        } else if (sscanf (line, "watch %1023s %ld %ld %ld %ld", a, &n1, &n2, &n3, &n4) == 5) {
            Watch *w = g_new0 (Watch, 1);

            w->id = g_strdup (a);
            w->path = g_strdup_printf ("/s/%s", a);
            w->uid = (uid_t) n1;
            w->locked = n2 != 0;
            w->pin_seen = n3 != 0;
            w->failures = (int) n4;
            g_hash_table_insert (self.sessions, w->path, w);
            puts ("ok");
        } else if (sscanf (line, "show %1023s", a) == 1) {
            Watch *w = watch_by_id (a);

            if (w)
                printf ("%d %d %d %d\n", w->locked, w->pin_seen, w->by_face, w->failures);
            else
                puts ("none");
        } else if (sscanf (line, "lockhint %1023s %ld", a, &n1) == 2) {
            g_autofree char *path = g_strdup_printf ("/s/%s", a);
            GVariantBuilder changed;
            g_autoptr (GVariant) params = NULL;

            g_variant_builder_init (&changed, G_VARIANT_TYPE ("a{sv}"));
            g_variant_builder_add (&changed, "{sv}", "LockedHint", g_variant_new_boolean (n1 != 0));
            params = g_variant_ref_sink (g_variant_new ("(sa{sv}as)", "org.freedesktop.login1.Session",
                                                        &changed, NULL));
            on_session_changed (NULL, LOGIND, path, "org.freedesktop.DBus.Properties",
                                "PropertiesChanged", params, NULL);
            wait_job ();
            puts ("ok");
        } else if (sscanf (line, "screen %ld", &n1) == 1) {
            /* Also the panel the screen poll reads when a lock starts it */
            g_free (self.dpms_path);
            self.dpms_path = g_strdup ("/run/plinius-face/panel");
            fake_set_contents_full (self.dpms_path, n1 ? "On\n" : "Off\n", -1, 0, 0600, NULL);
            self.screen_on = n1 != 0;
            puts ("ok");
        } else if (!strcmp (line, "try")) {
            try_unlock ();
            wait_job ();
            print_calls ();
        } else if (sscanf (line, "wants %1023s", a) == 1) {
            Watch *w = watch_by_id (a);

            puts (w && watch_wants_models (w) ? "yes" : "no");
        } else if (sscanf (line, "restore %1023s", a) == 1) {
            Watch w = { .id = a };

            restore_watch (&w);
            printf ("%d %d\n", w.pin_seen, w.failures);
        } else if (!strcmp (line, "save")) {
            save_sessions ();
            puts ("ok");
        } else if (sscanf (line, "method %1023s %1023s", a, b) >= 1 && !strncmp (line, "method", 6)) {
            FakeInv inv = { 0 };
            GVariant *params = NULL;

            if (!strcmp (a, "SetEnabled"))
                params = g_variant_new ("(b)", !strcmp (b, "true"));
            else if (!strcmp (a, "SetStrictness"))
                params = g_variant_new ("(s)", b);
            fake_inv_reset (&inv, a, params);
            on_method_call (NULL, ":1.5", OBJECT_PATH, INTERFACE, a, inv.params,
                            (GDBusMethodInvocation *) &inv, NULL);
            wait_job ();
            fake_inv_print (&inv);
            fake_inv_reset (&inv, NULL, NULL);
        } else if (!strcmp (line, "calls")) {
            print_calls ();
        } else {
            printf ("<unknown command: %.60s>\n", line);
        }
    }
    return 0;
}
