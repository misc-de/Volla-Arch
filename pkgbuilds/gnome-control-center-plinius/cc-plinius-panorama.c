/*
 * cc-plinius-panorama.c
 *
 * Plinius: the 360° photos and the videos of the Background page (Appearance > Background), each
 * a section of its own (an AdwExpanderRow of cc-background-chooser.c): the tiles (or a row
 * saying there are none yet) and, on each copy one added, a button to remove it again. Files are
 * added with the one button of the page's header bar (cc_background_chooser_select_file), which
 * sorts each into its section here (cc_plinius_files_sort, cc_plinius_files_add). A tap shows the photo or the
 * video as the background through plinius-ambient (keys panorama-enabled and video-enabled: the
 * photo turns with the phone, the video loops without sound, the animated effect is drawn over
 * both). The two exclude each other; a tap on a picture of the chooser switches both off
 * (cc_plinius_background_picture_chosen, also for the picture that is set already: choosing that
 * one changes no key plinius-ambient could notice).
 *
 * A tap on the photo or video shown (or on the picture shown, see cc-background-chooser.c)
 * switches it off without another one: GNOME's picture-options becomes 'none', so Phosh shows
 * the background color of the Style page (primary-color, cc-plinius-bgcolor.c) and no tile has
 * the check mark (cc_plinius_background_clear_picture). Choosing a picture sets picture-options
 * again; plinius-ambient switches the photo or video off for that, not for 'none' nor for a new
 * background color.
 *
 * 360° photos: everything in ~/.local/share/backgrounds/360, where the add button and Muga's
 * "Set as Background" put their copies; the equirectangular photos (GPano XMP, as 360° cameras
 * and Muga write them) in ~/.local/share/backgrounds itself, where GNOME's "Add Picture…" copied
 * them before they were sorted (GNOME's list of own pictures leaves them out, see
 * cc_plinius_panorama_is_photo), and in the Pictures folder's subfolder 360; and the photo shown
 * right now. The same photo in two of these places (same name without GNOME's time stamp, same
 * size) is listed once.
 *
 * Videos (plinius-ambient 0.5): everything playable in ~/.local/share/backgrounds/video, where
 * the add button copies videos and GIFs to, and the video shown right now. Animated GIFs show their first frame,
 * other videos a frame a second in (or from their middle, if shorter), decoded through GStreamer
 * as plinius-ambient plays them and kept in ~/.cache/plinius-control-center/video-thumbnails;
 * until it is there, or where nothing decodes it, an icon and the name.
 *
 * Only the copies under ~/.local/share/backgrounds can be removed, after a question; removing the
 * photo or video shown right now switches it off. The folders are watched, so what is added or
 * removed shows at once. A section reads nothing before it is opened; folders are read and
 * thumbnails decoded on threads.
 *
 * Nothing of this shows without plinius-ambient 0.4 (key panorama-enabled), the videos not
 * without 0.5 (key video-enabled). Our own strings use the gettext domain plinius-control-center.
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-panorama"

#include "cc-plinius-panorama.h"

#include <errno.h>

#include <gdesktop-enums.h>
#include <gdk-pixbuf/gdk-pixbuf.h>
#include <gst/gst.h>
#include <glib/gstdio.h>
#include <stdio.h>
#include <string.h>

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

#define AMBIENT_SCHEMA "org.plinius.ambient"
#define GPANO_NS "http://ns.google.com/photos/1.0/panorama/"
/* The XMP packet sits near the start of the file (a JPEG segment holds 64 KiB) */
#define XMP_SCAN_BYTES (512 * 1024)

/* After the last change in a watched folder: a copy in progress reports many */
#define RESCAN_DELAY_MS 500

/* As GNOME's own tiles (cc-background-chooser.c), drawn at twice the size for sharpness */
#define THUMBNAIL_WIDTH 144
#define THUMBNAIL_HEIGHT (THUMBNAIL_WIDTH * 3 / 4)

/* A copy whose name is taken gets "-2", "-3"... up to this */
#define MAX_COPY_NUMBER 999

/* Tiles without a picture (videos other than GIFs) */
#define PLACEHOLDER_CLASS "plinius-file-placeholder"

/* The frame of a video shown on its tile: this far in, or from the middle of a shorter one */
#define VIDEO_FRAME_AT (1 * GST_SECOND)
/* How long opening or seeking a video may take before it gets no thumbnail */
#define VIDEO_TIMEOUT (5 * GST_SECOND)

typedef struct {
    const char *path_key;    /* in plinius-ambient's schema */
    const char *enabled_key;
    const char *folder;      /* under ~/.local/share/backgrounds; all of it is of this kind */
} KindInfo;

static const KindInfo kinds[] = {
    [CC_PLINIUS_FILES_PANORAMA] = { "panorama", "panorama-enabled", "360" },
    [CC_PLINIUS_FILES_VIDEO] = { "video", "video-enabled", "video" },
};

typedef struct {
    CcPliniusFilesKind kind;
    AdwExpanderRow *expander; /* owns this */
    GtkWidget *tiles_row;     /* hidden while there are no tiles */
    GtkWidget *empty_row;     /* shown instead once the folders were read */
    GtkFlowBox *flowbox;
    CcPliniusPanoramaChanged changed;
    gpointer user_data;
    GCancellable *cancellable;
    gboolean active;  /* a file of this kind is the background */
    gboolean started; /* the section was opened once */
    GPtrArray *monitors; /* GFileMonitor of the folders */
    guint rescan_id;     /* pending rescan after a change in them */
    GPtrArray *shown;    /* paths of the tiles, in order */
} Section;

/* ---- plinius-ambient's settings ---- */

static gboolean video_supported;

static GSettings *
ambient_settings (void)
{
    static GSettings *settings;
    static gboolean looked;

    if (!looked) {
        GSettingsSchemaSource *source = g_settings_schema_source_get_default ();
        g_autoptr(GSettingsSchema) schema = NULL;

        looked = TRUE;
        if (source)
            schema = g_settings_schema_source_lookup (source, AMBIENT_SCHEMA, TRUE);
        if (schema && g_settings_schema_has_key (schema, "panorama") &&
            g_settings_schema_has_key (schema, "panorama-enabled")) {
            settings = g_settings_new_full (schema, NULL, NULL);
            video_supported = g_settings_schema_has_key (schema, "video") &&
                              g_settings_schema_has_key (schema, "video-enabled");
        }
    }
    return settings;
}

static gboolean
kind_supported (CcPliniusFilesKind kind)
{
    return ambient_settings () && (kind == CC_PLINIUS_FILES_PANORAMA || video_supported);
}

/* Whether the section of @kind shows (plinius-ambient 0.4 for 360° photos, 0.5 for videos) */
gboolean
cc_plinius_files_supported (CcPliniusFilesKind kind)
{
    return kind_supported (kind);
}

/* The photo or video shown in place of the background picture, NULL if there is none */
char *
cc_plinius_files_current (CcPliniusFilesKind kind)
{
    GSettings *settings = ambient_settings ();
    g_autofree char *path = NULL;

    if (!kind_supported (kind) || !g_settings_get_boolean (settings, kinds[kind].enabled_key))
        return NULL;
    path = g_settings_get_string (settings, kinds[kind].path_key);
    return path[0] != '\0' ? g_steal_pointer (&path) : NULL;
}

gboolean
cc_plinius_panorama_is_active (void)
{
    g_autofree char *path = cc_plinius_files_current (CC_PLINIUS_FILES_PANORAMA);

    return path != NULL;
}

gboolean
cc_plinius_video_is_active (void)
{
    g_autofree char *path = cc_plinius_files_current (CC_PLINIUS_FILES_VIDEO);

    return path != NULL;
}

/* No picture of the chooser is the background: a 360° photo or a video is */
gboolean
cc_plinius_background_replaced (void)
{
    return cc_plinius_panorama_is_active () || cc_plinius_video_is_active ();
}

static void
switch_off (CcPliniusFilesKind kind)
{
    GSettings *settings = ambient_settings ();

    if (kind_supported (kind) && g_settings_get_boolean (settings, kinds[kind].enabled_key))
        g_settings_set_boolean (settings, kinds[kind].enabled_key, FALSE);
}

/* A picture instead of the 360° photo or the video; the effect over it stays */
void
cc_plinius_background_picture_chosen (void)
{
    switch_off (CC_PLINIUS_FILES_PANORAMA);
    switch_off (CC_PLINIUS_FILES_VIDEO);
}

static void
activate (CcPliniusFilesKind kind, const char *path)
{
    GSettings *settings = ambient_settings ();

    if (!kind_supported (kind))
        return;
    /* The two exclude each other (plinius-ambient keeps to that as well) */
    switch_off (kind == CC_PLINIUS_FILES_PANORAMA ? CC_PLINIUS_FILES_VIDEO : CC_PLINIUS_FILES_PANORAMA);
    g_settings_set_string (settings, kinds[kind].path_key, path);
    g_settings_set_boolean (settings, kinds[kind].enabled_key, TRUE);
}

/* The section titles and the add button, translated in our domain */
const char *
cc_plinius_panorama_heading (const char *which)
{
    if (g_strcmp0 (which, "video") == 0)
        return P_("Video");
    if (g_strcmp0 (which, "panorama") == 0)
        return P_("360° Photos");
    if (g_strcmp0 (which, "recent") == 0)
        return P_("Own Pictures");
    if (g_strcmp0 (which, "add") == 0)
        return kind_supported (CC_PLINIUS_FILES_VIDEO) ? P_("Add Pictures and Videos") : P_("Add Pictures");
    return P_("Default Pictures");
}

/* The row of a section with nothing in it yet */
GtkWidget *
cc_plinius_files_empty_row_new (void)
{
    GtkWidget *row = adw_action_row_new ();

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Nothing added yet"));
    gtk_widget_add_css_class (row, "dim-label");
    gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), FALSE);
    gtk_widget_set_focusable (row, FALSE);
    return row;
}

/* ---- Picture keys of GNOME ---- */

static const char * const picture_schemas[] = { "org.gnome.desktop.background", "org.gnome.desktop.screensaver" };
/* Not the colors: they are the background color of the Style page, which stays */
static const char * const picture_keys[] = {
    "picture-uri", "picture-uri-dark", "picture-options",
};

static GSettings *
background_settings (void)
{
    static GSettings *settings;

    if (!settings)
        settings = g_settings_new (picture_schemas[0]);
    return settings;
}

/* No picture shows (picture-options 'none'): the background color does, unless a 360° photo or
 * a video covers it */
gboolean
cc_plinius_background_picture_off (void)
{
    return g_settings_get_enum (background_settings (), "picture-options") == G_DESKTOP_BACKGROUND_STYLE_NONE;
}

/* The picture shown goes, no other one comes: the background color shows, on the background and
 * the lock screen (both as set by the panel). The picture stays set, so the panel can show it
 * again with the placement it had (cc-background-panel.c set_background). */
void
cc_plinius_background_clear_picture (void)
{
    for (guint i = 0; i < G_N_ELEMENTS (picture_schemas); i++) {
        g_autoptr(GSettings) settings = g_settings_new (picture_schemas[i]);

        if (g_settings_get_enum (settings, "picture-options") != G_DESKTOP_BACKGROUND_STYLE_NONE)
            g_settings_set_enum (settings, "picture-options", G_DESKTOP_BACKGROUND_STYLE_NONE);
    }
}

/* GNOME's default picture for the background and the lock screen (both as set by the panel) */
void
cc_plinius_background_reset_picture (void)
{
    for (guint i = 0; i < G_N_ELEMENTS (picture_schemas); i++) {
        g_autoptr(GSettings) settings = g_settings_new (picture_schemas[i]);
        g_autoptr(GSettingsSchema) schema = NULL;

        g_object_get (settings, "settings-schema", &schema, NULL);
        for (guint k = 0; k < G_N_ELEMENTS (picture_keys); k++)
            if (g_settings_schema_has_key (schema, picture_keys[k]))
                g_settings_reset (settings, picture_keys[k]);
    }
}

/* After the photo or video was switched off by removing it: the picture shown again must exist
 * (one removed while it was hidden under the photo or video stayed set); with none shown, the
 * background color is what comes back */
static void
ensure_picture (void)
{
    g_autofree char *uri = g_settings_get_string (background_settings (), "picture-uri");
    g_autofree char *path = g_str_has_prefix (uri, "file://") ? g_filename_from_uri (uri, NULL, NULL) : NULL;

    if (path && !cc_plinius_background_picture_off () && !g_file_test (path, G_FILE_TEST_EXISTS))
        cc_plinius_background_reset_picture ();
}

/* ---- Finding the files (thread) ---- */

/* @needle in the first @n bytes of @hay; unlike g_strstr_len it reads past NUL bytes, which a
 * JPEG has long before its XMP packet */
static const char *
find_bytes (const char *hay, gsize n, const char *needle)
{
    gsize m = strlen (needle);
    const char *p;

    if (m == 0 || m > n)
        return NULL;
    for (p = hay; (p = memchr (p, needle[0], n - (gsize) (p - hay) - m + 1)); p++)
        if (memcmp (p, needle, m) == 0)
            return p;
    return NULL;
}

/* Whether the file is an equirectangular 360° photo by its XMP; reads at most 512 KiB */
gboolean
cc_plinius_panorama_is_photo (const char *path)
{
    g_autofree char *head = g_malloc (XMP_SCAN_BYTES);
    FILE *file = fopen (path, "rb");
    size_t n;
    const char *start, *end, *ns;

    if (!file)
        return FALSE;
    n = fread (head, 1, XMP_SCAN_BYTES, file);
    fclose (file);
    start = find_bytes (head, n, "<x:xmpmeta");
    if (!start)
        return FALSE;
    end = find_bytes (start, n - (gsize) (start - head), "</x:xmpmeta>");
    if (!end)
        return FALSE;
    ns = find_bytes (start, (gsize) (end - start), GPANO_NS);
    return ns && find_bytes (start, (gsize) (end - start), "equirectangular") != NULL;
}

/* By the name: a picture, or for videos anything GStreamer plays and animated GIFs */
static gboolean
has_type (CcPliniusFilesKind kind, const char *name)
{
    g_autofree char *type = g_content_type_guess (name, NULL, 0, NULL);

    if (kind == CC_PLINIUS_FILES_VIDEO)
        return g_content_type_is_a (type, "video/*") || g_content_type_is_a (type, "image/gif");
    return g_content_type_is_a (type, "image/*");
}

static gboolean
is_gif (const char *path)
{
    g_autofree char *type = g_content_type_guess (path, NULL, 0, NULL);

    return g_content_type_is_a (type, "image/gif");
}

/* The file name without the time stamp "YYYY-MM-DD-HH-MM-SS-" that GNOME's "Add Picture..." puts
 * in front (bg-recent-source.c) */
static const char *
plain_name (const char *name)
{
    static const char pattern[] = "dddd-dd-dd-dd-dd-dd-";

    for (gsize i = 0; i < sizeof pattern - 1; i++) {
        if (name[i] == '\0')
            return name;
        if (pattern[i] == 'd' ? !g_ascii_isdigit (name[i]) : name[i] != pattern[i])
            return name;
    }
    return name[sizeof pattern - 1] ? name + sizeof pattern - 1 : name;
}

static gint
by_name (gconstpointer a, gconstpointer b)
{
    g_autofree char *name_a = g_path_get_basename (*(const char **) a);
    g_autofree char *name_b = g_path_get_basename (*(const char **) b);

    return g_utf8_collate (plain_name (name_a), plain_name (name_b));
}

/* The same file in two places: same plain name, same size */
static char *
file_key (const char *path)
{
    g_autofree char *name = g_path_get_basename (path);
    GStatBuf st;

    if (g_stat (path, &st) != 0)
        return g_strdup (path);
    return g_strdup_printf ("%s\n%" G_GINT64_FORMAT, plain_name (name), (gint64) st.st_size);
}

typedef struct {
    CcPliniusFilesKind kind;
    char **folders;  /* NULL-terminated */
    guint n_trusted; /* the first ones hold files of this kind only: no look inside the files */
    char *current;   /* the file of the key, or NULL */
} Places;

static void
places_free (gpointer data)
{
    Places *places = data;

    g_strfreev (places->folders);
    g_free (places->current);
    g_free (places);
}

/* Lists @path once, if it counts: by its name in a trusted folder (and as the current file), a
 * 360° photo elsewhere only by its XMP */
static void
take (CcPliniusFilesKind kind, GPtrArray *found, GHashTable *seen, const char *path, gboolean trusted)
{
    g_autofree char *real = g_canonicalize_filename (path, NULL);
    g_autofree char *key = NULL;

    if (g_hash_table_contains (seen, real) || !has_type (kind, real) || !g_file_test (real, G_FILE_TEST_IS_REGULAR))
        return;
    key = file_key (real);
    if (g_hash_table_contains (seen, key))
        return;
    if (!trusted && kind == CC_PLINIUS_FILES_PANORAMA && !cc_plinius_panorama_is_photo (real))
        return;
    g_hash_table_add (seen, g_steal_pointer (&real));
    g_hash_table_add (seen, g_steal_pointer (&key));
    g_ptr_array_add (found, g_strdup (path));
}

static void
find_thread (GTask *task, gpointer source, gpointer task_data, GCancellable *cancellable)
{
    Places *places = task_data;
    GPtrArray *found = g_ptr_array_new_with_free_func (g_free);
    /* canonical paths and file keys of what was taken */
    g_autoptr(GHashTable) seen = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, NULL);

    /* The file shown right now first, wherever it lies: its tile carries the key's path */
    if (places->current)
        take (places->kind, found, seen, places->current, TRUE);
    for (guint i = 0; places->folders[i] && !g_cancellable_is_cancelled (cancellable); i++) {
        g_autoptr(GDir) dir = g_dir_open (places->folders[i], 0, NULL);
        const char *name;

        while (dir && (name = g_dir_read_name (dir))) {
            g_autofree char *path = g_build_filename (places->folders[i], name, NULL);

            take (places->kind, found, seen, path, i < places->n_trusted);
        }
    }
    g_ptr_array_sort (found, by_name);
    g_task_return_pointer (task, found, (GDestroyNotify) g_ptr_array_unref);
}

/* ---- Thumbnails (thread) ---- */

typedef struct {
    CcPliniusFilesKind kind;
    char *path;
    int width; /* of the thumbnail, in pixels */
    int height;
} Thumbnail;

static void
thumbnail_free (gpointer data)
{
    Thumbnail *thumbnail = data;

    g_free (thumbnail->path);
    g_free (thumbnail);
}

/* Where the thumbnail of a video is kept: named after its path, size, time and the thumbnail's
 * size, so a changed video gets a new one */
static char *
video_cache_path (const char *path, int width, int height)
{
    g_autofree char *key = NULL;
    g_autofree char *checksum = NULL;
    g_autofree char *name = NULL;
    GStatBuf info;

    if (g_stat (path, &info) != 0)
        return NULL;
    key = g_strdup_printf ("%s\n%" G_GINT64_FORMAT "\n%" G_GINT64_FORMAT "\n%dx%d", path, (gint64) info.st_size,
                           (gint64) info.st_mtime, width, height);
    checksum = g_compute_checksum_for_string (G_CHECKSUM_SHA1, key, -1);
    name = g_strconcat (checksum, ".png", NULL);
    return g_build_filename (g_get_user_cache_dir (), "plinius-control-center", "video-thumbnails", name, NULL);
}

/* Waits for a state change of @pipeline; %FALSE with the bus's error on failure or timeout */
static gboolean
video_wait (GstElement *pipeline, GError **error)
{
    g_autoptr(GstBus) bus = NULL;
    g_autoptr(GstMessage) message = NULL;

    switch (gst_element_get_state (pipeline, NULL, NULL, VIDEO_TIMEOUT)) {
    case GST_STATE_CHANGE_SUCCESS:
    case GST_STATE_CHANGE_NO_PREROLL:
        return TRUE;
    case GST_STATE_CHANGE_ASYNC:
        g_set_error_literal (error, G_IO_ERROR, G_IO_ERROR_TIMED_OUT, "the video did not open in time");
        return FALSE;
    default:
        break;
    }
    bus = gst_element_get_bus (pipeline);
    message = gst_bus_pop_filtered (bus, GST_MESSAGE_ERROR);
    if (message)
        gst_message_parse_error (message, error, NULL);
    else
        g_set_error_literal (error, G_IO_ERROR, G_IO_ERROR_FAILED, "the video could not be opened");
    return FALSE;
}

/* One frame of a video at VIDEO_FRAME_AT, upright (its orientation tag), through playbin as
 * plinius-ambient decodes it (plinius-vdec on the phone). One video at a time: the hardware
 * decoder is one. */
static GdkPixbuf *
video_frame (const char *path, GError **error)
{
    static GMutex one_at_a_time;
    g_autoptr(GMutexLocker) locker = NULL;
    g_autofree char *uri = NULL;
    g_autoptr(GstElement) pipeline = NULL;
    g_autoptr(GstSample) sample = NULL;
    GstElement *bin, *sink;
    GstStructure *structure;
    GstBuffer *buffer;
    GstMapInfo map;
    GdkPixbuf *pixbuf = NULL;
    gint64 duration = -1;
    int width = 0, height = 0;

    if (!gst_init_check (NULL, NULL, error))
        return NULL;
    uri = g_filename_to_uri (path, NULL, error);
    if (!uri)
        return NULL;
    pipeline = gst_element_factory_make ("playbin", NULL);
    bin = gst_parse_bin_from_description ("videoconvert ! videoflip video-direction=auto ! videoconvert ! "
                                          "appsink name=sink sync=false max-buffers=1 "
                                          "caps=video/x-raw,format=RGBA,pixel-aspect-ratio=1/1",
                                          TRUE, error);
    if (!pipeline || !bin) {
        /* gst_parse_bin_from_description says what it missed; playbin says nothing */
        if (bin || !error || !*error)
            g_set_error_literal (error, G_IO_ERROR, G_IO_ERROR_NOT_SUPPORTED, "no playbin");
        if (bin)
            gst_object_unref (gst_object_ref_sink (bin));
        return NULL;
    }
    gst_object_ref_sink (pipeline);
    sink = gst_bin_get_by_name (GST_BIN (bin), "sink");
    /* flags: video only (GST_PLAY_FLAG_VIDEO), no sound, no subtitles */
    g_object_set (pipeline, "uri", uri, "video-sink", bin, "flags", 1, NULL);

    locker = g_mutex_locker_new (&one_at_a_time);
    gst_element_set_state (pipeline, GST_STATE_PAUSED);
    if (!video_wait (pipeline, error))
        goto out;
    if (gst_element_query_duration (pipeline, GST_FORMAT_TIME, &duration) && duration > 0) {
        gint64 at = MIN (VIDEO_FRAME_AT, duration / 2);

        if (at > 0 && gst_element_seek_simple (pipeline, GST_FORMAT_TIME,
                                               GST_SEEK_FLAG_FLUSH | GST_SEEK_FLAG_KEY_UNIT, at) &&
            !video_wait (pipeline, error))
            goto out;
    }
    g_signal_emit_by_name (sink, "pull-preroll", &sample);
    if (!sample) {
        g_set_error_literal (error, G_IO_ERROR, G_IO_ERROR_FAILED, "no frame");
        goto out;
    }
    structure = gst_caps_get_structure (gst_sample_get_caps (sample), 0);
    buffer = gst_sample_get_buffer (sample);
    if (!gst_structure_get_int (structure, "width", &width) || !gst_structure_get_int (structure, "height", &height) ||
        width <= 0 || height <= 0 || !buffer || !gst_buffer_map (buffer, &map, GST_MAP_READ)) {
        g_set_error_literal (error, G_IO_ERROR, G_IO_ERROR_FAILED, "no frame");
        goto out;
    }
    /* RGBA rows are width * 4 bytes, already a multiple of GStreamer's 4 */
    if (map.size >= (gsize) width * height * 4) {
        pixbuf = gdk_pixbuf_new (GDK_COLORSPACE_RGB, TRUE, 8, width, height);
        for (int y = 0; y < height; y++)
            memcpy (gdk_pixbuf_get_pixels (pixbuf) + (gsize) y * gdk_pixbuf_get_rowstride (pixbuf),
                    map.data + (gsize) y * width * 4, (gsize) width * 4);
    } else
        g_set_error_literal (error, G_IO_ERROR, G_IO_ERROR_FAILED, "frame too small");
    gst_buffer_unmap (buffer, &map);
out:
    gst_element_set_state (pipeline, GST_STATE_NULL);
    gst_clear_object (&sink);
    return pixbuf;
}

/* Of a 360° photo the middle, straight ahead (latitude ±45°), of a GIF its first frame, of other
 * videos one frame (video_frame, kept in the cache), all cut to the thumbnail's shape */
static void
thumbnail_thread (GTask *task, gpointer source, gpointer task_data, GCancellable *cancellable)
{
    Thumbnail *job = task_data;
    g_autoptr(GError) error = NULL;
    g_autoptr(GdkPixbuf) whole = NULL;
    g_autoptr(GdkPixbuf) upright = NULL;
    g_autoptr(GdkPixbuf) middle = NULL;
    g_autoptr(GdkPixbuf) thumbnail = NULL;
    g_autofree char *cached = NULL;
    int width, height, cut_height, cut_width;

    if (job->kind == CC_PLINIUS_FILES_VIDEO && !is_gif (job->path)) {
        cached = video_cache_path (job->path, job->width, job->height);
        if (cached) {
            thumbnail = gdk_pixbuf_new_from_file (cached, NULL);
            if (thumbnail) {
                g_task_return_pointer (task, g_steal_pointer (&thumbnail), g_object_unref);
                return;
            }
        }
        if (g_task_return_error_if_cancelled (task))
            return;
        whole = video_frame (job->path, &error);
    } else if (job->kind == CC_PLINIUS_FILES_PANORAMA)
        whole = gdk_pixbuf_new_from_file_at_scale (job->path, 1024, 512, TRUE, &error);
    else
        whole = gdk_pixbuf_new_from_file (job->path, &error);
    if (!whole) {
        g_task_return_error (task, g_steal_pointer (&error));
        return;
    }
    upright = gdk_pixbuf_apply_embedded_orientation (whole);
    width = gdk_pixbuf_get_width (upright);
    height = gdk_pixbuf_get_height (upright);
    cut_height = job->kind == CC_PLINIUS_FILES_PANORAMA ? MAX (1, height / 2) : height;
    cut_width = MAX (1, MIN (width, cut_height * job->width / job->height));
    cut_height = MAX (1, MIN (cut_height, cut_width * job->height / job->width));
    middle = gdk_pixbuf_new_subpixbuf (upright, (width - cut_width) / 2, (height - cut_height) / 2, cut_width,
                                       cut_height);
    thumbnail = gdk_pixbuf_scale_simple (middle, job->width, job->height, GDK_INTERP_BILINEAR);
    if (cached) {
        g_autofree char *folder = g_path_get_dirname (cached);
        g_autoptr(GError) save_error = NULL;

        if (g_mkdir_with_parents (folder, 0700) != 0 || !gdk_pixbuf_save (thumbnail, cached, "png", &save_error, NULL))
            g_debug ("Video thumbnail not kept: %s", save_error ? save_error->message : g_strerror (errno));
    }
    g_task_return_pointer (task, g_steal_pointer (&thumbnail), g_object_unref);
}

static void
on_thumbnail_done (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(GtkPicture) picture = user_data; /* the reference taken for the task */
    g_autoptr(GError) error = NULL;
    g_autoptr(GdkPixbuf) pixbuf = g_task_propagate_pointer (G_TASK (result), &error);
    g_autoptr(GBytes) bytes = NULL;
    g_autoptr(GdkTexture) texture = NULL;

    if (!pixbuf) {
        if (!g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
            g_warning ("No thumbnail: %s", error->message);
        return;
    }
    bytes = gdk_pixbuf_read_pixel_bytes (pixbuf);
    texture = gdk_memory_texture_new (gdk_pixbuf_get_width (pixbuf), gdk_pixbuf_get_height (pixbuf),
                                      gdk_pixbuf_get_has_alpha (pixbuf) ? GDK_MEMORY_R8G8B8A8 : GDK_MEMORY_R8G8B8,
                                      bytes, gdk_pixbuf_get_rowstride (pixbuf));
    gtk_picture_set_paintable (picture, GDK_PAINTABLE (texture));
    cc_plinius_files_drop_placeholder (gtk_widget_get_parent (GTK_WIDGET (picture)));
}

/**
 * cc_plinius_files_thumbnail:
 * @width: @height: size of the thumbnail in pixels
 *
 * Decodes a thumbnail of @path on a thread and shows it in @picture. Once it is there, a
 * placeholder the picture's parent carries (cc_plinius_files_drop_placeholder) goes.
 */
void
cc_plinius_files_thumbnail (CcPliniusFilesKind kind, const char *path, int width, int height, GtkPicture *picture,
                            GCancellable *cancellable)
{
    g_autoptr(GTask) task = NULL;
    Thumbnail *job;

    job = g_new0 (Thumbnail, 1);
    job->kind = kind;
    job->path = g_strdup (path);
    job->width = MAX (1, width);
    job->height = MAX (1, height);
    task = g_task_new (NULL, cancellable, on_thumbnail_done, g_object_ref (picture));
    g_task_set_task_data (task, job, thumbnail_free);
    g_task_run_in_thread (task, thumbnail_thread);
}

/* The icon and name a tile shows until its thumbnail is there, on @overlay */
#define PLACEHOLDER_DATA "plinius-placeholder"
#define MARK_DATA "plinius-mark"

/* The thumbnail is there: the icon and name go, the mark turns white as on pictures */
void
cc_plinius_files_drop_placeholder (GtkWidget *overlay)
{
    GtkWidget *placeholder = overlay ? g_object_get_data (G_OBJECT (overlay), PLACEHOLDER_DATA) : NULL;
    GtkWidget *mark = overlay ? g_object_get_data (G_OBJECT (overlay), MARK_DATA) : NULL;

    if (!placeholder)
        return;
    gtk_overlay_remove_overlay (GTK_OVERLAY (overlay), placeholder);
    g_object_set_data (G_OBJECT (overlay), PLACEHOLDER_DATA, NULL);
    gtk_widget_remove_css_class (overlay, PLACEHOLDER_CLASS);
    if (mark) {
        gtk_widget_remove_css_class (mark, "dim-label");
        gtk_widget_add_css_class (mark, "slideshow-icon");
        gtk_widget_set_margin_start (mark, 0);
        gtk_widget_set_margin_bottom (mark, 0);
    }
}

/* A faint ground for what has no picture */
void
cc_plinius_files_add_placeholder_style (GtkWidget *widget)
{
    static GtkCssProvider *provider;

    if (!provider) {
        provider = gtk_css_provider_new ();
        gtk_css_provider_load_from_string (provider, "." PLACEHOLDER_CLASS " {\n"
                                                     "  background-color: color-mix(in srgb, currentColor 10%, transparent);\n"
                                                     "}\n");
        gtk_style_context_add_provider_for_display (gdk_display_get_default (), GTK_STYLE_PROVIDER (provider),
                                                    GTK_STYLE_PROVIDER_PRIORITY_APPLICATION);
    }
    gtk_widget_add_css_class (widget, PLACEHOLDER_CLASS);
}

/* ---- Removing ---- */

typedef struct {
    CcPliniusRemoveConfirmed confirmed;
    gpointer data;
    GDestroyNotify destroy;
} Question;

static void
question_free (gpointer data, GClosure *closure)
{
    Question *question = data;

    if (question->destroy)
        question->destroy (question->data);
    g_free (question);
}

static void
on_question_response (AdwAlertDialog *dialog, const char *response, Question *question)
{
    if (g_strcmp0 (response, "remove") == 0)
        question->confirmed (question->data);
}

/**
 * cc_plinius_background_confirm_remove:
 * @parent: the widget the question is for
 * @path: the file to remove (its name is shown)
 *
 * Asks whether to remove @path; @confirmed is called with @data on "Remove" only.
 */
void
cc_plinius_background_confirm_remove (GtkWidget *parent, const char *path, CcPliniusRemoveConfirmed confirmed,
                                      gpointer data, GDestroyNotify destroy)
{
    g_autofree char *name = g_path_get_basename (path);
    g_autofree char *heading = NULL;
    Question *question = g_new0 (Question, 1);
    AdwDialog *dialog;

    question->confirmed = confirmed;
    question->data = data;
    question->destroy = destroy;

    /* Translators: %s is the name of a file */
    heading = g_strdup_printf (P_("Remove “%s”?"), plain_name (name));
    dialog = adw_alert_dialog_new (heading, P_("Only the copy made for backgrounds is deleted."));
    adw_alert_dialog_add_responses (ADW_ALERT_DIALOG (dialog), "cancel", P_("_Cancel"), "remove", P_("_Remove"), NULL);
    adw_alert_dialog_set_response_appearance (ADW_ALERT_DIALOG (dialog), "remove", ADW_RESPONSE_DESTRUCTIVE);
    adw_alert_dialog_set_default_response (ADW_ALERT_DIALOG (dialog), "cancel");
    adw_alert_dialog_set_close_response (ADW_ALERT_DIALOG (dialog), "cancel");
    g_signal_connect_data (dialog, "response", G_CALLBACK (on_question_response), question, question_free, 0);
    adw_dialog_present (dialog, parent);
}

static char *
backgrounds_folder (const char *sub)
{
    return g_build_filename (g_get_user_data_dir (), "backgrounds", sub, NULL);
}

/* Only the copies of ~/.local/share/backgrounds, not the photos of the Pictures folder */
static gboolean
removable (const char *path)
{
    g_autofree char *folder = backgrounds_folder (NULL);
    g_autofree char *root = g_canonicalize_filename (folder, NULL);
    g_autofree char *prefix = g_strconcat (root, G_DIR_SEPARATOR_S, NULL);
    g_autofree char *real = g_canonicalize_filename (path, NULL);

    return g_str_has_prefix (real, prefix);
}

static gboolean
same_file (const char *a, const char *b)
{
    g_autofree char *real_a = g_canonicalize_filename (a, NULL);
    g_autofree char *real_b = g_canonicalize_filename (b, NULL);

    return g_strcmp0 (real_a, real_b) == 0;
}

typedef struct {
    CcPliniusFilesKind kind;
    char *path;
} Removal;

static void
removal_free (gpointer data)
{
    Removal *removal = data;

    g_free (removal->path);
    g_free (removal);
}

static void
remove_confirmed (gpointer data)
{
    Removal *removal = data;
    GSettings *settings = ambient_settings ();
    g_autoptr(GFile) file = g_file_new_for_path (removal->path);
    g_autoptr(GError) error = NULL;
    g_autofree char *set = settings ? g_settings_get_string (settings, kinds[removal->kind].path_key) : NULL;

    /* The file of the key: off and forgotten, plinius-ambient shows the picture again */
    if (set && same_file (set, removal->path)) {
        gboolean was_on = g_settings_get_boolean (settings, kinds[removal->kind].enabled_key);

        switch_off (removal->kind);
        g_settings_reset (settings, kinds[removal->kind].path_key);
        if (was_on)
            ensure_picture ();
    }
    /* The folder's monitor takes the tile away */
    if (!g_file_delete (file, NULL, &error))
        g_warning ("Could not remove %s: %s", removal->path, error->message);
}

static void
on_remove_clicked (GtkButton *button, gpointer user_data)
{
    Removal *removal = g_new0 (Removal, 1);

    removal->kind = GPOINTER_TO_INT (user_data);
    removal->path = g_strdup (g_object_get_data (G_OBJECT (button), "path"));
    cc_plinius_background_confirm_remove (GTK_WIDGET (button), removal->path, remove_confirmed, removal, removal_free);
}

/* ---- Adding ---- */

/* "name.jpg" as "name-2.jpg" */
static char *
numbered_name (const char *name, guint number)
{
    const char *dot = strrchr (name, '.');

    if (!dot || dot == name)
        return g_strdup_printf ("%s-%u", name, number);
    return g_strdup_printf ("%.*s-%u%s", (int) (dot - name), name, number, dot);
}

static void
on_copied (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(GError) error = NULL;

    if (!g_file_copy_finish (G_FILE (source), result, &error) &&
        !g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        g_warning ("Could not copy a background: %s", error->message);
}

/* Under its own name, or numbered if that is taken; the folder's monitor shows the tile */
static void
copy_to (GFile *file, GFile *folder)
{
    g_autofree char *name = g_file_get_basename (file);
    g_autoptr(GFile) destination = NULL;

    if (!name || g_file_has_parent (file, folder))
        return;
    for (guint number = 1; number <= MAX_COPY_NUMBER; number++) {
        g_autofree char *candidate = number == 1 ? g_strdup (name) : numbered_name (name, number);

        g_clear_object (&destination);
        destination = g_file_get_child (folder, candidate);
        if (!g_file_query_exists (destination, NULL))
            break;
    }
    g_file_copy_async (file, destination, G_FILE_COPY_NONE, G_PRIORITY_DEFAULT, NULL, NULL, NULL, on_copied, NULL);
}

/**
 * cc_plinius_files_add_types:
 *
 * Plinius: what the add button offers besides pictures: videos and GIFs, with plinius-ambient 0.5.
 */
void
cc_plinius_files_add_types (GtkFileFilter *filter)
{
    if (!kind_supported (CC_PLINIUS_FILES_VIDEO))
        return;
    gtk_file_filter_add_mime_type (filter, "video/*");
    gtk_file_filter_add_mime_type (filter, "image/gif");
}

/**
 * cc_plinius_files_sort:
 * @kind: (out): the section of @file
 *
 * Where an added file belongs: %TRUE for a video or GIF (with plinius-ambient 0.5) and a 360°
 * photo (equirectangular by its XMP, with 0.4), %FALSE for anything else, which the chooser
 * takes as an own picture if it is one. The type comes from the content where the name says
 * nothing (a file passed on by the portal may have none that fits).
 */
gboolean
cc_plinius_files_sort (GFile *file, CcPliniusFilesKind *kind)
{
    g_autofree char *path = g_file_get_path (file);
    g_autofree char *type = NULL;
    g_autoptr(GFileInfo) info = NULL;

    if (!path)
        return FALSE;
    info = g_file_query_info (file, G_FILE_ATTRIBUTE_STANDARD_CONTENT_TYPE, G_FILE_QUERY_INFO_NONE, NULL, NULL);
    if (info && g_file_info_get_content_type (info))
        type = g_strdup (g_file_info_get_content_type (info));
    else
        type = g_content_type_guess (path, NULL, 0, NULL);

    if (kind_supported (CC_PLINIUS_FILES_VIDEO) &&
        (g_content_type_is_a (type, "video/*") || g_content_type_is_a (type, "image/gif"))) {
        *kind = CC_PLINIUS_FILES_VIDEO;
        return TRUE;
    }
    if (kind_supported (CC_PLINIUS_FILES_PANORAMA) && g_content_type_is_a (type, "image/*") &&
        cc_plinius_panorama_is_photo (path)) {
        *kind = CC_PLINIUS_FILES_PANORAMA;
        return TRUE;
    }
    return FALSE;
}

/**
 * cc_plinius_files_add:
 *
 * Copies @file into the folder of @kind (made if needed); the section's folder monitor shows
 * the tile once it is there.
 */
void
cc_plinius_files_add (CcPliniusFilesKind kind, GFile *file)
{
    g_autofree char *path = backgrounds_folder (kinds[kind].folder);
    g_autoptr(GFile) folder = g_file_new_for_path (path);

    if (g_mkdir_with_parents (path, 0755) != 0) {
        g_warning ("Could not make %s: %s", path, g_strerror (errno));
        return;
    }
    copy_to (file, folder);
}

/* ---- The section ---- */

static void
update_marks (Section *section)
{
    g_autofree char *current = cc_plinius_files_current (section->kind);
    gboolean active = current != NULL;
    GtkFlowBoxChild *child;
    int i = 0;

    while ((child = gtk_flow_box_get_child_at_index (section->flowbox, i++))) {
        const char *path = g_object_get_data (G_OBJECT (child), "path");
        gboolean on = active && g_strcmp0 (path, current) == 0;

        if (on)
            gtk_widget_add_css_class (GTK_WIDGET (child), "active-item");
        else
            gtk_widget_remove_css_class (GTK_WIDGET (child), "active-item");
        gtk_accessible_update_state (GTK_ACCESSIBLE (child), GTK_ACCESSIBLE_STATE_CHECKED, on, -1);
    }
    if (active != section->active) {
        section->active = active;
        if (section->changed)
            section->changed (active, section->user_data);
    }
}

static GtkWidget *
new_tile (Section *section, const char *path)
{
    g_autofree char *name = g_path_get_basename (path);
    GtkWidget *picture = gtk_picture_new ();
    GtkWidget *check = gtk_image_new_from_icon_name ("background-selected-symbolic");
    GtkWidget *overlay = gtk_overlay_new ();
    GtkWidget *child = gtk_flow_box_child_new ();

    gtk_picture_set_content_fit (GTK_PICTURE (picture), GTK_CONTENT_FIT_COVER);
    gtk_widget_set_size_request (picture, THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT);
    gtk_widget_set_halign (check, GTK_ALIGN_END);
    gtk_widget_set_valign (check, GTK_ALIGN_END);
    gtk_widget_add_css_class (check, "selected-check");

    gtk_widget_set_overflow (overlay, GTK_OVERFLOW_HIDDEN);
    gtk_widget_add_css_class (overlay, "background-thumbnail");
    gtk_overlay_set_child (GTK_OVERLAY (overlay), picture);

    if (section->kind == CC_PLINIUS_FILES_VIDEO) {
        /* It moves: the mark GNOME gives slideshows, in the same place */
        GtkWidget *mark = gtk_image_new_from_icon_name ("media-playback-start-symbolic");

        gtk_widget_set_halign (mark, GTK_ALIGN_START);
        gtk_widget_set_valign (mark, GTK_ALIGN_END);
        gtk_widget_add_css_class (mark, "slideshow-icon");

        {
            /* Until the frame is there (or where nothing decodes the video): an icon and the name */
            GtkWidget *box = gtk_box_new (GTK_ORIENTATION_VERTICAL, 6);
            GtkWidget *icon = gtk_image_new_from_icon_name ("video-x-generic-symbolic");
            GtkWidget *label = gtk_label_new (plain_name (name));

            gtk_image_set_pixel_size (GTK_IMAGE (icon), 32);
            gtk_widget_add_css_class (icon, "dim-label");
            gtk_label_set_ellipsize (GTK_LABEL (label), PANGO_ELLIPSIZE_MIDDLE);
            gtk_widget_add_css_class (label, "caption");
            gtk_widget_set_valign (box, GTK_ALIGN_CENTER);
            gtk_widget_set_margin_start (box, 12);
            gtk_widget_set_margin_end (box, 12);
            gtk_box_append (GTK_BOX (box), icon);
            gtk_box_append (GTK_BOX (box), label);
            gtk_overlay_add_overlay (GTK_OVERLAY (overlay), box);
            cc_plinius_files_add_placeholder_style (overlay);
            /* the slideshow mark is white for pictures */
            gtk_widget_remove_css_class (mark, "slideshow-icon");
            gtk_widget_add_css_class (mark, "dim-label");
            gtk_widget_set_margin_start (mark, 8);
            gtk_widget_set_margin_bottom (mark, 8);
            g_object_set_data (G_OBJECT (overlay), PLACEHOLDER_DATA, box);
            g_object_set_data (G_OBJECT (overlay), MARK_DATA, mark);
        }
        gtk_overlay_add_overlay (GTK_OVERLAY (overlay), mark);
        cc_plinius_files_thumbnail (section->kind, path, THUMBNAIL_WIDTH * 2, THUMBNAIL_HEIGHT * 2,
                                    GTK_PICTURE (picture), section->cancellable);
    } else
        cc_plinius_files_thumbnail (section->kind, path, THUMBNAIL_WIDTH * 2, THUMBNAIL_HEIGHT * 2,
                                    GTK_PICTURE (picture), section->cancellable);

    gtk_overlay_add_overlay (GTK_OVERLAY (overlay), check);

    /* As GNOME's own pictures: a cross on what one added */
    if (removable (path)) {
        GtkWidget *button = gtk_button_new_from_icon_name ("cross-small-symbolic");

        gtk_widget_set_halign (button, GTK_ALIGN_END);
        gtk_widget_set_valign (button, GTK_ALIGN_START);
        gtk_widget_add_css_class (button, "osd");
        gtk_widget_add_css_class (button, "circular");
        gtk_widget_add_css_class (button, "remove-button");
        gtk_widget_set_tooltip_text (button, P_("Remove"));
        g_object_set_data_full (G_OBJECT (button), "path", g_strdup (path), g_free);
        g_signal_connect (button, "clicked", G_CALLBACK (on_remove_clicked), GINT_TO_POINTER (section->kind));
        gtk_overlay_add_overlay (GTK_OVERLAY (overlay), button);
    }

    gtk_widget_set_halign (child, GTK_ALIGN_CENTER);
    gtk_widget_set_valign (child, GTK_ALIGN_CENTER);
    gtk_flow_box_child_set_child (GTK_FLOW_BOX_CHILD (child), overlay);
    g_object_set (child, "accessible-role", GTK_ACCESSIBLE_ROLE_TOGGLE_BUTTON, NULL);
    gtk_accessible_update_property (GTK_ACCESSIBLE (child), GTK_ACCESSIBLE_PROPERTY_LABEL, plain_name (name), -1);
    g_object_set_data_full (G_OBJECT (child), "path", g_strdup (path), g_free);
    return child;
}

static void
on_found (GObject *source, GAsyncResult *result, gpointer user_data)
{
    Section *section = user_data;
    g_autoptr(GError) error = NULL;
    g_autoptr(GPtrArray) found = g_task_propagate_pointer (G_TASK (result), &error);
    gboolean same;

    if (!found)
        return; /* cancelled: the section is gone */
    same = section->shown && section->shown->len == found->len;
    for (guint i = 0; same && i < found->len; i++)
        same = g_strcmp0 (g_ptr_array_index (section->shown, i), g_ptr_array_index (found, i)) == 0;
    if (same)
        return;
    gtk_flow_box_remove_all (section->flowbox);
    for (guint i = 0; i < found->len; i++)
        gtk_flow_box_append (section->flowbox, new_tile (section, g_ptr_array_index (found, i)));
    gtk_widget_set_visible (section->tiles_row, found->len > 0);
    gtk_widget_set_visible (section->empty_row, found->len == 0);
    g_clear_pointer (&section->shown, g_ptr_array_unref);
    section->shown = g_ptr_array_ref (found);
    update_marks (section);
}

/* Look through the places again (thread), the tiles follow in on_found */
static void
scan (Section *section)
{
    GSettings *settings = ambient_settings ();
    g_autoptr(GTask) task = NULL;
    g_autofree char *current = g_settings_get_string (settings, kinds[section->kind].path_key);
    GPtrArray *folders = g_ptr_array_new ();
    Places *places = g_new0 (Places, 1);

    places->kind = section->kind;
    g_ptr_array_add (folders, backgrounds_folder (kinds[section->kind].folder));
    places->n_trusted = 1;
    if (section->kind == CC_PLINIUS_FILES_PANORAMA) {
        g_ptr_array_add (folders, backgrounds_folder (NULL));
        if (g_get_user_special_dir (G_USER_DIRECTORY_PICTURES))
            g_ptr_array_add (folders, g_build_filename (g_get_user_special_dir (G_USER_DIRECTORY_PICTURES), "360", NULL));
    }
    g_ptr_array_add (folders, NULL);
    places->folders = (char **) g_ptr_array_free (folders, FALSE);
    if (current[0] != '\0')
        places->current = g_steal_pointer (&current);

    task = g_task_new (NULL, section->cancellable, on_found, section);
    g_task_set_task_data (task, places, places_free);
    g_task_run_in_thread (task, find_thread);
}

static gboolean
on_rescan_due (gpointer user_data)
{
    Section *section = user_data;

    section->rescan_id = 0;
    scan (section);
    return G_SOURCE_REMOVE;
}

/* Every change restarts the delay, so a copy in progress is scanned once it is done */
static void
on_folder_changed (GFileMonitor *monitor, GFile *file, GFile *other, GFileMonitorEvent event, Section *section)
{
    if (event == G_FILE_MONITOR_EVENT_ATTRIBUTE_CHANGED)
        return;
    g_clear_handle_id (&section->rescan_id, g_source_remove);
    section->rescan_id = g_timeout_add (RESCAN_DELAY_MS, on_rescan_due, section);
}

/* @create: GNOME makes ~/.local/share/backgrounds itself, the 360 folder may come with the first
 * photo; the video folder is made when a video is added (a missing folder is watched for) */
static void
watch (Section *section, const char *path, gboolean create)
{
    g_autoptr(GFile) folder = g_file_new_for_path (path);
    g_autoptr(GError) error = NULL;
    GFileMonitor *monitor;

    if (create)
        g_file_make_directory_with_parents (folder, NULL, NULL);
    monitor = g_file_monitor_directory (folder, G_FILE_MONITOR_WATCH_MOVES, NULL, &error);
    if (!monitor) {
        g_warning ("Not watching %s: %s", path, error->message);
        return;
    }
    g_signal_connect (monitor, "changed", G_CALLBACK (on_folder_changed), section);
    g_ptr_array_add (section->monitors, monitor);
}

/* Nothing is read before the section is opened the first time */
static void
on_expanded (AdwExpanderRow *expander, GParamSpec *pspec, Section *section)
{
    g_autofree char *own = backgrounds_folder (kinds[section->kind].folder);

    if (section->started || !adw_expander_row_get_expanded (expander))
        return;
    section->started = TRUE;
    if (section->kind == CC_PLINIUS_FILES_PANORAMA) {
        g_autofree char *backgrounds = backgrounds_folder (NULL);

        watch (section, own, TRUE);
        watch (section, backgrounds, TRUE);
    } else
        watch (section, own, FALSE);
    scan (section);
}

static void
on_tile_activated (GtkFlowBox *flowbox, GtkFlowBoxChild *child, Section *section)
{
    const char *path = g_object_get_data (G_OBJECT (child), "path");
    g_autofree char *current = cc_plinius_files_current (section->kind);

    if (current && g_strcmp0 (path, current) == 0) {
        /* The one shown: off, and no picture under it either, the background color shows. The
         * picture goes first, so that it does not show for a moment. */
        cc_plinius_background_clear_picture ();
        switch_off (section->kind);
    } else
        activate (section->kind, path);
    update_marks (section);
}

static void
on_ambient_changed (GSettings *settings, const char *key, Section *section)
{
    if (g_strcmp0 (key, kinds[section->kind].enabled_key) == 0 || g_strcmp0 (key, kinds[section->kind].path_key) == 0)
        update_marks (section);
}

static void
section_free (gpointer data)
{
    Section *section = data;

    g_cancellable_cancel (section->cancellable);
    g_object_unref (section->cancellable);
    g_clear_handle_id (&section->rescan_id, g_source_remove);
    for (guint i = 0; i < section->monitors->len; i++) {
        GFileMonitor *monitor = g_ptr_array_index (section->monitors, i);

        g_signal_handlers_disconnect_by_data (monitor, section);
        g_file_monitor_cancel (monitor);
    }
    g_ptr_array_unref (section->monitors);
    g_clear_pointer (&section->shown, g_ptr_array_unref);
    if (ambient_settings ())
        g_signal_handlers_disconnect_by_data (ambient_settings (), section);
    g_free (section);
}

/**
 * cc_plinius_panorama_fill:
 * @expander: the section's row of the chooser
 * @changed: called when a file of @kind is switched on or off as the background (the chooser
 *   then takes the check mark off its own pictures, or puts it back)
 *
 * The tiles of the 360° photos or the videos in @expander (a row says when there are none). %FALSE
 * without plinius-ambient (0.5 for the videos): the section should not show.
 */
gboolean
cc_plinius_panorama_fill (AdwExpanderRow *expander, CcPliniusFilesKind kind, CcPliniusPanoramaChanged changed,
                          gpointer user_data)
{
    Section *section;

    if (!kind_supported (kind))
        return FALSE;

    section = g_new0 (Section, 1);
    section->kind = kind;
    section->expander = expander;
    section->changed = changed;
    section->user_data = user_data;
    section->cancellable = g_cancellable_new ();
    section->monitors = g_ptr_array_new_with_free_func (g_object_unref);
    section->active = kind == CC_PLINIUS_FILES_VIDEO ? cc_plinius_video_is_active () : cc_plinius_panorama_is_active ();
    g_object_set_data_full (G_OBJECT (expander), "plinius-files", section, section_free);

    section->flowbox = GTK_FLOW_BOX (gtk_flow_box_new ());
    g_object_set (section->flowbox, "margin-top", 12, "margin-bottom", 12, "margin-start", 12, "margin-end", 12,
                  "column-spacing", 12, "row-spacing", 12, "homogeneous", TRUE, "halign", GTK_ALIGN_CENTER,
                  "min-children-per-line", 1, "max-children-per-line", 8, "activate-on-single-click", TRUE,
                  NULL);
    gtk_flow_box_set_selection_mode (section->flowbox, GTK_SELECTION_NONE);
    gtk_widget_add_css_class (GTK_WIDGET (section->flowbox), "background-flowbox");
    g_signal_connect (section->flowbox, "child-activated", G_CALLBACK (on_tile_activated), section);

    section->tiles_row = gtk_list_box_row_new ();
    gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (section->tiles_row), FALSE);
    gtk_list_box_row_set_selectable (GTK_LIST_BOX_ROW (section->tiles_row), FALSE);
    gtk_widget_set_focusable (section->tiles_row, FALSE);
    gtk_list_box_row_set_child (GTK_LIST_BOX_ROW (section->tiles_row), GTK_WIDGET (section->flowbox));
    gtk_widget_set_visible (section->tiles_row, FALSE);
    adw_expander_row_add_row (expander, section->tiles_row);

    section->empty_row = cc_plinius_files_empty_row_new ();
    gtk_widget_set_visible (section->empty_row, FALSE);
    adw_expander_row_add_row (expander, section->empty_row);

    g_signal_connect (ambient_settings (), "changed", G_CALLBACK (on_ambient_changed), section);
    g_signal_connect (expander, "notify::expanded", G_CALLBACK (on_expanded), section);
    return TRUE;
}
