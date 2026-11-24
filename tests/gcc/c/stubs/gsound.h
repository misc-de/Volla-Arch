/* Stand-in for GSound (tests/gcc): the host has no libgsound. Its implementation is
 * c/gsound_stub.c, which a test compiles into its program. Nothing is played: each request is
 * appended as a JSON line to $PLINIUS_TEST_GSOUND_LOG ({"op": "play", "id": n, "attrs": {...}},
 * later {"op": "end", "id": n, "how": "done" | "failed" | "cancelled"}).
 *
 * - A sound "plays" for $PLINIUS_TEST_GSOUND_MS milliseconds (default 100), then ends; with
 *   $PLINIUS_TEST_GSOUND_FAIL set it ends with that message as a G_IO_ERROR_FAILED error.
 * - Cancelling ends it at once with G_IO_ERROR_CANCELLED (in an idle, as libcanberra's answer
 *   comes from its thread).
 * - With $PLINIUS_TEST_GSOUND_NEW_FAIL set, gsound_context_new fails with that message. */
#pragma once

#include <gio/gio.h>

G_BEGIN_DECLS

#define GSOUND_TYPE_CONTEXT (gsound_context_get_type ())
G_DECLARE_FINAL_TYPE (GSoundContext, gsound_context, GSOUND, CONTEXT, GObject)

#define GSOUND_ATTR_MEDIA_FILENAME "media.filename"
#define GSOUND_ATTR_MEDIA_ROLE "media.role"
#define GSOUND_ATTR_EVENT_ID "event.id"
#define GSOUND_ATTR_APPLICATION_ID "application.id"
#define GSOUND_ATTR_CANBERRA_XDG_THEME_NAME "canberra.xdg-theme.name"

GSoundContext *gsound_context_new (GCancellable *cancellable, GError **error);
void gsound_context_play_full (GSoundContext *context, GCancellable *cancellable, GAsyncReadyCallback callback,
                               gpointer user_data, ...) G_GNUC_NULL_TERMINATED;
gboolean gsound_context_play_full_finish (GSoundContext *context, GAsyncResult *result, GError **error);

G_END_DECLS
