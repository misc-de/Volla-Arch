/* GSound stand-in of the GNOME Control Center panel tests (tests/gcc), see stubs/gsound.h. */

#include <json-glib/json-glib.h>
#include <stdio.h>
#include <stdlib.h>

#include "gsound.h"

struct _GSoundContext {
    GObject parent_instance;
};

G_DEFINE_FINAL_TYPE (GSoundContext, gsound_context, G_TYPE_OBJECT)

static void
gsound_context_class_init (GSoundContextClass *klass)
{
}

static void
gsound_context_init (GSoundContext *self)
{
}

static guint next_id = 1;

static void
log_line (JsonBuilder *b)
{
    const char *path = g_getenv ("PLINIUS_TEST_GSOUND_LOG");
    g_autoptr(JsonNode) root = json_builder_get_root (b);
    g_autofree char *text = json_to_string (root, FALSE);
    FILE *f;

    if (!path)
        return;
    f = fopen (path, "a");
    if (!f)
        return;
    fprintf (f, "%s\n", text);
    fclose (f);
}

GSoundContext *
gsound_context_new (GCancellable *cancellable, GError **error)
{
    const char *fail = g_getenv ("PLINIUS_TEST_GSOUND_NEW_FAIL");

    if (fail) {
        g_set_error_literal (error, G_IO_ERROR, G_IO_ERROR_FAILED, fail);
        return NULL;
    }
    return g_object_new (GSOUND_TYPE_CONTEXT, NULL);
}

typedef struct {
    GTask *task;
    GCancellable *cancellable;
    guint id;
    guint timeout_id;
    guint idle_id;
    gulong handler;
    gboolean done;
} Play;

static void
play_finish (Play *play, const char *how)
{
    g_autoptr(JsonBuilder) b = json_builder_new ();
    const char *fail = g_getenv ("PLINIUS_TEST_GSOUND_FAIL");

    if (play->done)
        return;
    play->done = TRUE;
    g_clear_handle_id (&play->timeout_id, g_source_remove);
    g_clear_handle_id (&play->idle_id, g_source_remove);
    if (play->cancellable && play->handler)
        g_cancellable_disconnect (play->cancellable, play->handler);
    if (g_str_equal (how, "done") && fail)
        how = "failed";
    json_builder_begin_object (b);
    json_builder_set_member_name (b, "op");
    json_builder_add_string_value (b, "end");
    json_builder_set_member_name (b, "id");
    json_builder_add_int_value (b, play->id);
    json_builder_set_member_name (b, "how");
    json_builder_add_string_value (b, how);
    json_builder_end_object (b);
    log_line (b);
    if (g_str_equal (how, "cancelled"))
        g_task_return_new_error (play->task, G_IO_ERROR, G_IO_ERROR_CANCELLED, "Operation was cancelled");
    else if (g_str_equal (how, "failed"))
        g_task_return_new_error (play->task, G_IO_ERROR, G_IO_ERROR_FAILED, "%s", fail);
    else
        g_task_return_boolean (play->task, TRUE);
    g_clear_object (&play->task);
    g_clear_object (&play->cancellable);
    g_free (play);
}

static gboolean
on_played (gpointer data)
{
    Play *play = data;

    play->timeout_id = 0;
    play_finish (play, "done");
    return G_SOURCE_REMOVE;
}

static gboolean
on_cancelled_idle (gpointer data)
{
    Play *play = data;

    play->idle_id = 0;
    play_finish (play, "cancelled");
    return G_SOURCE_REMOVE;
}

static void
on_cancelled (GCancellable *cancellable, Play *play)
{
    /* Not from inside g_cancellable_cancel: libcanberra answers from its thread */
    if (!play->idle_id)
        play->idle_id = g_idle_add (on_cancelled_idle, play);
}

void
gsound_context_play_full (GSoundContext *context, GCancellable *cancellable, GAsyncReadyCallback callback,
                          gpointer user_data, ...)
{
    g_autoptr(JsonBuilder) b = json_builder_new ();
    const char *ms = g_getenv ("PLINIUS_TEST_GSOUND_MS");
    Play *play = g_new0 (Play, 1);
    const char *key;
    va_list ap;

    play->id = next_id++;
    play->task = g_task_new (context, cancellable, callback, user_data);
    /* The error of a cancelled play is the stand-in's own, as libcanberra's */
    g_task_set_check_cancellable (play->task, FALSE);
    json_builder_begin_object (b);
    json_builder_set_member_name (b, "op");
    json_builder_add_string_value (b, "play");
    json_builder_set_member_name (b, "id");
    json_builder_add_int_value (b, play->id);
    json_builder_set_member_name (b, "attrs");
    json_builder_begin_object (b);
    va_start (ap, user_data);
    while ((key = va_arg (ap, const char *))) {
        const char *value = va_arg (ap, const char *);

        json_builder_set_member_name (b, key);
        if (value)
            json_builder_add_string_value (b, value);
        else
            json_builder_add_null_value (b);
    }
    va_end (ap);
    json_builder_end_object (b);
    json_builder_end_object (b);
    log_line (b);

    play->timeout_id = g_timeout_add (ms ? atoi (ms) : 100, on_played, play);
    if (cancellable) {
        play->cancellable = g_object_ref (cancellable);
        play->handler = g_cancellable_connect (cancellable, G_CALLBACK (on_cancelled), play, NULL);
        /* Cancelled already: the handler ran inside g_cancellable_connect */
    }
}

gboolean
gsound_context_play_full_finish (GSoundContext *context, GAsyncResult *result, GError **error)
{
    return g_task_propagate_boolean (G_TASK (result), error);
}
