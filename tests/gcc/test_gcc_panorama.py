# The 360° photos and the videos of the Background page (cc-plinius-panorama.c): the two
# sections (AdwExpanderRows as cc-background-chooser.c has them) with their tiles from the
# folders under ~/.local/share/backgrounds and the Pictures folder, plinius-ambient's keys a tap
# sets, removing a copy after a question, sorting and copying added files, and the thumbnails
# (gdk-pixbuf for photos and GIFs, GStreamer for videos, both for real on small files made here).
# plinius-ambient's schema comes in three versions (none, 0.4 without videos, 0.5): each test of
# another version has a schema directory of its own (the file looks the schema up once per run).

import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from conftest import PKG
from gcc_harness import (build_panel, call, click, dump, gcc_session, gcc_world,  # noqa: F401
                         set_active, settle, wait, wait_for)

AMBIENT = "org.plinius.ambient"
BG = "org.gnome.desktop.background"
SAVER = "org.gnome.desktop.screensaver"
GPANO = "http://ns.google.com/photos/1.0/panorama/"

GLUE = r'''
#include <stdlib.h>
#include <gdk-pixbuf/gdk-pixbuf.h>
#include "gcc_driver.h"
#include "cc-plinius-panorama.h"

/* The two sections of the Background page, as cc-background-chooser.c makes them */
static GtkWidget *expanders[2];
static gboolean filled[2];
static JsonBuilder *changes;
static JsonBuilder *confirmations;

static void
on_changed (gboolean active, gpointer user_data)
{
    json_builder_begin_object (changes);
    json_builder_set_member_name (changes, "kind");
    json_builder_add_int_value (changes, GPOINTER_TO_INT (user_data));
    json_builder_set_member_name (changes, "active");
    json_builder_add_boolean_value (changes, active);
    json_builder_end_object (changes);
}

void
gcc_test_setup (GccTest *t)
{
    GtkWidget *group = adw_preferences_group_new ();
    const char *titles[] = { cc_plinius_panorama_heading ("panorama"), cc_plinius_panorama_heading ("video") };

    changes = json_builder_new ();
    json_builder_begin_array (changes);
    confirmations = json_builder_new ();
    json_builder_begin_array (confirmations);
    for (int k = 0; k < 2; k++) {
        expanders[k] = adw_expander_row_new ();
        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (expanders[k]), titles[k]);
        filled[k] = cc_plinius_panorama_fill (ADW_EXPANDER_ROW (expanders[k]), k, on_changed, GINT_TO_POINTER (k));
        gtk_widget_set_visible (expanders[k], filled[k]);
        adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), expanders[k]);
    }
    gcc_test_add (t, group);
    /* A second fill of a section without a callback (the chooser of another window) */
    if (gcc_test_param (t, "silent")) {
        GtkWidget *extra = adw_expander_row_new ();

        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (extra), "Silent");
        cc_plinius_panorama_fill (ADW_EXPANDER_ROW (extra), CC_PLINIUS_FILES_PANORAMA, NULL, NULL);
        adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), extra);
    }
}

static GtkFlowBox *
find_flowbox (GtkWidget *w)
{
    if (GTK_IS_FLOW_BOX (w))
        return GTK_FLOW_BOX (w);
    for (GtkWidget *c = gtk_widget_get_first_child (w); c; c = gtk_widget_get_next_sibling (c)) {
        GtkFlowBox *f = find_flowbox (c);

        if (f)
            return f;
    }
    return NULL;
}

static GtkWidget *
find_child_of_type (GtkWidget *w, GType type)
{
    for (GtkWidget *c = gtk_widget_get_first_child (w); c; c = gtk_widget_get_next_sibling (c))
        if (g_type_is_a (G_OBJECT_TYPE (c), type))
            return c;
    return NULL;
}

static GtkWidget *
find_image (GtkWidget *overlay, const char *icon)
{
    for (GtkWidget *c = gtk_widget_get_first_child (overlay); c; c = gtk_widget_get_next_sibling (c))
        if (GTK_IS_IMAGE (c) && !g_strcmp0 (gtk_image_get_icon_name (GTK_IMAGE (c)), icon))
            return c;
    return NULL;
}

static void
add_classes (JsonBuilder *b, const char *name, GtkWidget *w)
{
    g_auto(GStrv) classes = w ? gtk_widget_get_css_classes (w) : NULL;

    json_builder_set_member_name (b, name);
    json_builder_begin_array (b);
    for (int i = 0; classes && classes[i]; i++)
        json_builder_add_string_value (b, classes[i]);
    json_builder_end_array (b);
}

static void
tiles (int kind, JsonBuilder *b)
{
    GtkFlowBox *flowbox = find_flowbox (expanders[kind]);
    GtkFlowBoxChild *child;
    int i = 0;

    json_builder_begin_array (b);
    while (flowbox && (child = gtk_flow_box_get_child_at_index (flowbox, i++))) {
        GtkWidget *overlay = gtk_flow_box_child_get_child (child);
        GtkWidget *picture = gtk_overlay_get_child (GTK_OVERLAY (overlay));
        GdkPaintable *paintable = gtk_picture_get_paintable (GTK_PICTURE (picture));
        GtkWidget *button = find_child_of_type (overlay, GTK_TYPE_BUTTON);

        json_builder_begin_object (b);
        json_builder_set_member_name (b, "path");
        json_builder_add_string_value (b, g_object_get_data (G_OBJECT (child), "path"));
        json_builder_set_member_name (b, "active");
        json_builder_add_boolean_value (b, gtk_widget_has_css_class (GTK_WIDGET (child), "active-item"));
        json_builder_set_member_name (b, "placeholder");
        json_builder_add_boolean_value (b, g_object_get_data (G_OBJECT (overlay), "plinius-placeholder") != NULL);
        add_classes (b, "overlay_classes", overlay);
        add_classes (b, "mark_classes", find_image (overlay, "media-playback-start-symbolic"));
        json_builder_set_member_name (b, "check");
        json_builder_add_boolean_value (b, find_image (overlay, "background-selected-symbolic") != NULL);
        json_builder_set_member_name (b, "size");
        json_builder_begin_array (b);
        if (paintable) {
            json_builder_add_int_value (b, gdk_paintable_get_intrinsic_width (paintable));
            json_builder_add_int_value (b, gdk_paintable_get_intrinsic_height (paintable));
        }
        json_builder_end_array (b);
        json_builder_set_member_name (b, "remove");
        if (button)
            json_builder_add_string_value (b, gtk_widget_get_tooltip_text (button));
        else
            json_builder_add_null_value (b);
        json_builder_end_object (b);
    }
    json_builder_end_array (b);
}

static GtkFlowBoxChild *
tile (JsonArray *args)
{
    GtkFlowBox *flowbox = find_flowbox (expanders[json_array_get_int_element (args, 0)]);

    return flowbox ? gtk_flow_box_get_child_at_index (flowbox, json_array_get_int_element (args, 1)) : NULL;
}

static void
confirmed (gpointer data)
{
    json_builder_add_string_value (confirmations, data);
}

static void
add_node (JsonBuilder *result, JsonBuilder *from)
{
    g_autoptr(JsonNode) node = NULL;

    json_builder_end_array (from);
    node = json_builder_get_root (from);
    json_builder_add_value (result, json_node_copy (node));
    json_builder_reset (from);
    json_builder_begin_array (from);
    {
        JsonArray *a = json_node_get_array (node);

        for (guint i = 0; i < json_array_get_length (a); i++)
            json_builder_add_value (from, json_node_copy (json_array_get_element (a, i)));
    }
}

/* A thumbnail outside a section: into a picture of an overlay carrying a placeholder */
static GtkWidget *loose_overlay, *loose_picture;

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    const char *arg0 = json_array_get_length (args) > 0 && JSON_NODE_HOLDS_VALUE (json_array_get_element (args, 0)) &&
                       json_node_get_value_type (json_array_get_element (args, 0)) == G_TYPE_STRING
                           ? json_array_get_string_element (args, 0) : NULL;

    if (g_str_equal (name, "filled")) {
        json_builder_begin_array (result);
        json_builder_add_boolean_value (result, filled[0]);
        json_builder_add_boolean_value (result, filled[1]);
        json_builder_end_array (result);
    } else if (g_str_equal (name, "tiles")) {
        /* kind [, pictures, ms]: after waiting up to ms for that many tiles with a picture */
        if (json_array_get_length (args) > 2) {
            gint64 end = g_get_monotonic_time () + json_array_get_int_element (args, 2) * 1000;
            int want = json_array_get_int_element (args, 1);

            while (g_get_monotonic_time () < end) {
                GtkFlowBox *flowbox = find_flowbox (expanders[json_array_get_int_element (args, 0)]);
                GtkFlowBoxChild *child;
                int i = 0, have = 0;

                while (flowbox && (child = gtk_flow_box_get_child_at_index (flowbox, i++)))
                    have += gtk_picture_get_paintable (GTK_PICTURE (gtk_overlay_get_child (
                                GTK_OVERLAY (gtk_flow_box_child_get_child (child))))) != NULL;
                if (have >= want)
                    break;
                if (!g_main_context_iteration (NULL, FALSE))
                    g_usleep (1000);
            }
        }
        tiles (json_array_get_int_element (args, 0), result);
    } else if (g_str_equal (name, "activate")) {
        GtkFlowBoxChild *child = tile (args);

        g_assert (child != NULL);
        g_signal_emit_by_name (gtk_widget_get_parent (GTK_WIDGET (child)), "child-activated", child);
        json_builder_add_null_value (result);
    } else if (g_str_equal (name, "remove")) {
        GtkFlowBoxChild *child = tile (args);
        GtkWidget *button = find_child_of_type (gtk_flow_box_child_get_child (child), GTK_TYPE_BUTTON);

        g_assert (button != NULL);
        g_signal_emit_by_name (button, "clicked");
        json_builder_add_null_value (result);
    } else if (g_str_equal (name, "changes")) {
        add_node (result, changes);
    } else if (g_str_equal (name, "confirmations")) {
        add_node (result, confirmations);
    } else if (g_str_equal (name, "heading")) {
        json_builder_add_string_value (result, cc_plinius_panorama_heading (arg0));
    } else if (g_str_equal (name, "is_photo")) {
        json_builder_add_boolean_value (result, cc_plinius_panorama_is_photo (arg0));
    } else if (g_str_equal (name, "sort")) {
        g_autoptr(GFile) file = g_file_new_for_path (arg0);
        CcPliniusFilesKind kind = 7;
        gboolean sorted = cc_plinius_files_sort (file, &kind);

        json_builder_add_int_value (result, sorted ? (gint64) kind : -1);
    } else if (g_str_equal (name, "sort_uri")) {
        g_autoptr(GFile) file = g_file_new_for_uri (arg0);
        CcPliniusFilesKind kind = 7;

        json_builder_add_boolean_value (result, cc_plinius_files_sort (file, &kind));
    } else if (g_str_equal (name, "add")) {
        g_autoptr(GFile) file = g_file_new_for_path (json_array_get_string_element (args, 1));

        cc_plinius_files_add (json_array_get_int_element (args, 0), file);
        json_builder_add_null_value (result);
    } else if (g_str_equal (name, "supported")) {
        json_builder_begin_array (result);
        json_builder_add_boolean_value (result, cc_plinius_files_supported (CC_PLINIUS_FILES_PANORAMA));
        json_builder_add_boolean_value (result, cc_plinius_files_supported (CC_PLINIUS_FILES_VIDEO));
        json_builder_end_array (result);
    } else if (g_str_equal (name, "state")) {
        g_autofree char *panorama = cc_plinius_files_current (CC_PLINIUS_FILES_PANORAMA);
        g_autofree char *video = cc_plinius_files_current (CC_PLINIUS_FILES_VIDEO);

        json_builder_begin_object (result);
        json_builder_set_member_name (result, "panorama");
        json_builder_add_string_value (result, panorama);
        json_builder_set_member_name (result, "video");
        json_builder_add_string_value (result, video);
        json_builder_set_member_name (result, "panorama_active");
        json_builder_add_boolean_value (result, cc_plinius_panorama_is_active ());
        json_builder_set_member_name (result, "video_active");
        json_builder_add_boolean_value (result, cc_plinius_video_is_active ());
        json_builder_set_member_name (result, "replaced");
        json_builder_add_boolean_value (result, cc_plinius_background_replaced ());
        json_builder_set_member_name (result, "picture_off");
        json_builder_add_boolean_value (result, cc_plinius_background_picture_off ());
        json_builder_end_object (result);
    } else if (g_str_equal (name, "picture_chosen")) {
        cc_plinius_background_picture_chosen ();
        json_builder_add_null_value (result);
    } else if (g_str_equal (name, "reset_picture")) {
        cc_plinius_background_reset_picture ();
        json_builder_add_null_value (result);
    } else if (g_str_equal (name, "clear_picture")) {
        cc_plinius_background_clear_picture ();
        json_builder_add_null_value (result);
    } else if (g_str_equal (name, "confirm_remove")) {
        cc_plinius_background_confirm_remove (GTK_WIDGET (t->page), arg0, confirmed, g_strdup (arg0), g_free);
        json_builder_add_null_value (result);
    } else if (g_str_equal (name, "confirm_remove_no_destroy")) {
        cc_plinius_background_confirm_remove (GTK_WIDGET (t->page), arg0, confirmed, (gpointer) "static", NULL);
        json_builder_add_null_value (result);
    } else if (g_str_equal (name, "add_types")) {
        g_autoptr(GtkFileFilter) filter = gtk_file_filter_new ();
        g_autoptr(GVariant) v = NULL;
        g_autofree char *text = NULL;

        gtk_file_filter_add_mime_type (filter, "image/*");
        cc_plinius_files_add_types (filter);
        v = gtk_file_filter_to_gvariant (filter);
        text = g_variant_print (v, FALSE);
        json_builder_add_string_value (result, text);
    } else if (g_str_equal (name, "empty_row")) {
        GtkWidget *row = cc_plinius_files_empty_row_new ();
        GtkWidget *group = adw_preferences_group_new ();

        adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (group), "Loose");
        adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), row);
        gcc_test_add (t, group);
        json_builder_add_boolean_value (result, gtk_list_box_row_get_activatable (GTK_LIST_BOX_ROW (row)));
    } else if (g_str_equal (name, "thumbnail")) {
        /* kind, path, width, height, placeholder? */
        GtkWidget *label = gtk_label_new ("placeholder");

        loose_overlay = gtk_overlay_new ();
        loose_picture = gtk_picture_new ();
        gtk_overlay_set_child (GTK_OVERLAY (loose_overlay), loose_picture);
        if (json_array_get_boolean_element (args, 4)) {
            gtk_overlay_add_overlay (GTK_OVERLAY (loose_overlay), label);
            g_object_set_data (G_OBJECT (loose_overlay), "plinius-placeholder", label);
            cc_plinius_files_add_placeholder_style (loose_overlay);
        }
        gcc_test_add (t, loose_overlay);
        cc_plinius_files_thumbnail (json_array_get_int_element (args, 0), json_array_get_string_element (args, 1),
                                    json_array_get_int_element (args, 2), json_array_get_int_element (args, 3),
                                    GTK_PICTURE (loose_picture), NULL);
        json_builder_add_null_value (result);
    } else if (g_str_equal (name, "loose")) {
        /* Waiting up to the ms given for the picture to come */
        gint64 end = g_get_monotonic_time () +
                     (json_array_get_length (args) > 0 ? json_array_get_int_element (args, 0) : 0) * 1000;
        GdkPaintable *paintable;

        while (!gtk_picture_get_paintable (GTK_PICTURE (loose_picture)) && g_get_monotonic_time () < end)
            if (!g_main_context_iteration (NULL, FALSE))
                g_usleep (1000);
        paintable = gtk_picture_get_paintable (GTK_PICTURE (loose_picture));

        json_builder_begin_object (result);
        json_builder_set_member_name (result, "size");
        json_builder_begin_array (result);
        if (paintable) {
            json_builder_add_int_value (result, gdk_paintable_get_intrinsic_width (paintable));
            json_builder_add_int_value (result, gdk_paintable_get_intrinsic_height (paintable));
        }
        json_builder_end_array (result);
        json_builder_set_member_name (result, "placeholder");
        json_builder_add_boolean_value (result, g_object_get_data (G_OBJECT (loose_overlay), "plinius-placeholder") != NULL);
        add_classes (result, "classes", loose_overlay);
        json_builder_end_object (result);
    } else if (g_str_equal (name, "wait_tiles")) {
        /* Until the section shows n tiles (for none: its empty row), the scan being a thread's */
        int kind = json_array_get_int_element (args, 0), n = json_array_get_int_element (args, 1);
        gint64 end = g_get_monotonic_time () + json_array_get_int_element (args, 2) * 1000;

        for (;;) {
            GtkFlowBox *flowbox = find_flowbox (expanders[kind]);
            int count = 0;
            gboolean empty_shown = FALSE;

            while (flowbox && gtk_flow_box_get_child_at_index (flowbox, count))
                count++;
            {
                /* The empty row is the expander's row after the tiles' row */
                GtkWidget *tiles_row = flowbox ? gtk_widget_get_parent (GTK_WIDGET (flowbox)) : NULL;
                GtkWidget *empty = tiles_row ? gtk_widget_get_next_sibling (tiles_row) : NULL;

                empty_shown = empty && gtk_widget_get_visible (empty);
            }
            if (count == n && (n > 0 || empty_shown))
                break;
            if (g_get_monotonic_time () > end) {
                g_printerr ("glue: %d tiles, not %d, in section %d\n", count, n, kind);
                exit (5);
            }
            if (!g_main_context_iteration (NULL, FALSE))
                g_usleep (1000);
        }
        json_builder_add_int_value (result, n);
    } else if (g_str_equal (name, "drop_placeholder_null")) {
        cc_plinius_files_drop_placeholder (NULL);
        json_builder_add_null_value (result);
    } else {
        return FALSE;
    }
    return TRUE;
}
'''


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("panorama"), "panorama",
                       ["cc-plinius-panorama.c"], GLUE,
                       pkgs=("gsettings-desktop-schemas", "gstreamer-1.0", "gdk-pixbuf-2.0"))


# ----- Media made here -----

def jpeg(path, size=(64, 32), color=(200, 30, 30), xmp=None):
    """A JPEG; xmp: text of an XMP packet put into an APP1 segment as cameras write it."""
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    data = buf.getvalue()
    if xmp is not None:
        payload = b"http://ns.adobe.com/xap/1.0/\0" + xmp.encode()
        data = data[:2] + b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload + data[2:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def gpano(projection="equirectangular", ns=GPANO, close=True):
    text = (f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description '
            f'xmlns:GPano="{ns}" GPano:ProjectionType="{projection}"/></rdf:RDF>')
    return text + ("</x:xmpmeta>" if close else "")


def photo360(path, **kw):
    return jpeg(path, size=(80, 40), xmp=gpano(), **kw)


def png(path, size=(40, 30)):
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", size, (0, 0, 255, 255)).save(path, "PNG")
    return path


def gif(path, size=(60, 30)):
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = [Image.new("RGB", size, c) for c in ((255, 0, 0), (0, 255, 0))]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=100, loop=0)
    return path


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    """Videos made once with GStreamer: 2 s and 0.4 s of VP8 in WebM."""
    if not shutil.which("gst-launch-1.0"):
        pytest.skip("no gst-launch-1.0")
    d = tmp_path_factory.mktemp("media")
    out = {}
    for name, frames in (("long", 50), ("short", 10)):
        f = d / f"{name}.webm"
        subprocess.run(["gst-launch-1.0", "-q", "videotestsrc", f"num-buffers={frames}", "!",
                        "video/x-raw,width=64,height=48,framerate=25/1", "!", "vp8enc", "!",
                        "webmmux", "!", "filesink", f"location={f}"],
                       check=True, capture_output=True, timeout=60)
        out[name] = f
    # A GStreamer registry for every run of the module, made once
    out["registry"] = d / "registry.bin"
    return out


# ----- Schemas: plinius-ambient as 0.5 (the session's), 0.4 or not there -----

def schema_dir(gcc_session, base, variant):
    d = base / f"schemas-{variant}"
    if (d / "gschemas.compiled").exists():
        return d
    d.mkdir(parents=True, exist_ok=True)
    for f in gcc_session.schemas.glob("*.xml"):
        if f.name != f"{AMBIENT}.gschema.xml":
            shutil.copy(f, d / f.name)
    # GNOME's enums, which the background schemas need (the session's directory lacks them)
    for f in Path("/usr/share/glib-2.0/schemas").glob("org.gnome.desktop.enums.xml"):
        shutil.copy(f, d / f.name)
    text = (PKG / "plinius-ambient" / f"{AMBIENT}.gschema.xml").read_text()
    if variant == "0.5":
        (d / f"{AMBIENT}.gschema.xml").write_text(text)
    elif variant == "0.4":
        for key in ("video-enabled", "video"):
            start = text.index(f'<key name="{key}"')
            text = text[:start] + text[text.index("</key>", start) + len("</key>"):]
        (d / f"{AMBIENT}.gschema.xml").write_text(text)
    elif variant == "0.3":
        start = text.index('<key name="panorama-enabled"')
        text = text[:start] + text[text.index("</key>", start) + len("</key>"):]
        (d / f"{AMBIENT}.gschema.xml").write_text(text)
    subprocess.run(["glib-compile-schemas", str(d)], check=True, capture_output=True)
    return d


class Env:
    def __init__(self, world, media, gcc_session, schema_base):
        self.world = world
        self.media = media
        self.home = world.home
        self.share = self.home / ".local/share"
        self.bg = self.share / "backgrounds"
        self.pano = self.bg / "360"
        self.video = self.bg / "video"
        self.pictures = self.home / "Pictures"
        # The MIME database, for the types guessed from names (read only)
        (world.data_dir / "mime").symlink_to("/usr/share/mime")
        # gdk-pixbuf's loaders of GIFs and more (glycin) find their configuration there too
        if Path("/usr/share/glycin-loaders").is_dir():
            (world.data_dir / "glycin-loaders").symlink_to("/usr/share/glycin-loaders")
            # which run each loader in bubblewrap, from PATH
            if shutil.which("bwrap"):
                (world.fake_bin.path / "bwrap").symlink_to(shutil.which("bwrap"))
        world.extra_env["GST_REGISTRY"] = str(media["registry"])
        self.gcc_session = gcc_session
        self.schema_base = schema_base
        self.schemas("0.5")

    def schemas(self, variant):
        self.world.extra_env["GSETTINGS_SCHEMA_DIR"] = str(
            schema_dir(self.gcc_session, self.schema_base, variant))

    def pictures_dir(self):
        (self.home / ".config/user-dirs.dirs").write_text('XDG_PICTURES_DIR="$HOME/Pictures"\n')

    def run(self, steps, before=(), **kw):
        return self.world.run(self.program, [*before, {"op": "setup"}, *steps], **kw)


@pytest.fixture(scope="module")
def schema_base(tmp_path_factory):
    return tmp_path_factory.mktemp("panorama-schemas")


@pytest.fixture
def env(program, gcc_world, media, gcc_session, schema_base):
    e = Env(gcc_world, media, gcc_session, schema_base)
    e.program = program
    return e


def gset(schema, key, value):
    return {"op": "gsettings_set", "schema": schema, "key": key, "value": value}


def gget(schema, key, label):
    return {"op": "gsettings_get", "schema": schema, "key": key, "label": label}


def expand(title="360° Photos"):
    return set_active(True, type="AdwExpanderRow", text=title)


def tiles(kind, label):
    return call("tiles", kind, label=label)


def wait_tiles(kind, n, ms=8000):
    """Until the section shows n tiles (the scan runs on a thread, a rescan after a delay)."""
    return call("wait_tiles", kind, n, ms, label=f"wait-{kind}-{n}")


def names(tile_list):
    return [Path(t["path"]).name for t in tile_list]


def wait_thumbs(kind, label, n, ms=15000):
    """The tiles once n of them have their picture (decoded on threads)."""
    return [call("tiles", kind, n, ms, label=label)]


PANO, VIDEO = 0, 1


# ----- Which sections show -----

def test_with_ambient_05_both_sections_show(env):
    r = env.run([call("filled"), call("supported"), call("heading", "add", label="add"),
                 call("heading", "video", label="video"), call("heading", "panorama", label="pano"),
                 call("heading", "recent", label="recent"), call("heading", "other", label="other"),
                 call("add_types"), dump("page")])
    assert r.value("filled") == [True, True]
    assert r.value("supported") == [True, True]
    assert [r.value(k) for k in ("add", "video", "pano", "recent", "other")] == [
        "Add Pictures and Videos", "Video", "360° Photos", "Own Pictures", "Default Pictures"]
    types = r.value("add_types")
    assert "video/*" in types and "image/gif" in types
    screen = r.dump("page")
    assert [n["text"] for n in screen.find(type="AdwExpanderRow")] == ["360° Photos", "Video"]
    # Nothing read before a section opens
    assert not env.bg.exists()
    assert not r.warnings()


def test_with_ambient_04_only_the_photos(env):
    env.schemas("0.4")
    r = env.run([call("filled"), call("supported"), call("heading", "add", label="add"),
                 call("add_types"), call("sort", str(gif(env.world.tmp / "a.gif")), label="gif"),
                 dump("page")])
    assert r.value("filled") == [True, False]
    assert r.value("supported") == [True, False]
    assert r.value("add") == "Add Pictures"
    assert "video" not in r.value("add_types")
    # A GIF is no video without 0.5: the chooser takes it as a picture
    assert r.value("gif") == -1
    assert [n["text"] for n in r.dump("page").find(type="AdwExpanderRow")] == ["360° Photos"]


@pytest.mark.parametrize("variant", ["none", "0.3"])
def test_without_ambient_nothing_shows(env, variant):
    env.schemas(variant)
    p = photo360(env.world.tmp / "p.jpg")
    r = env.run([call("filled"), call("supported"), call("state"),
                 call("sort", str(p), label="sort"), call("picture_chosen"),
                 call("add_types"), dump("page")])
    assert r.value("filled") == [False, False]
    assert r.value("supported") == [False, False]
    assert r.value("state") == {"panorama": None, "video": None, "panorama_active": False,
                                "video_active": False, "replaced": False, "picture_off": False}
    assert r.value("sort") == -1
    assert r.value("add_types") == r.value("add_types").replace("video", "")
    assert not r.dump("page").find(type="AdwExpanderRow")


# ----- The photos' section -----

def test_photos_from_all_places_sorted_and_deduplicated(env):
    env.pictures_dir()
    photo360(env.pano / "b-lake.jpg")
    # Trusted folder: anything that is a picture by its name
    png(env.pano / "a-flat.png")
    # Not a picture by its name
    (env.pano / "notes.txt").write_text("x")
    (env.pano / "sub.jpg").mkdir()
    # GNOME's own pictures folder: only equirectangular photos, by their XMP
    photo360(env.bg / "2026-01-02-03-04-05-c-sky.jpg")
    jpeg(env.bg / "plain.jpg")
    jpeg(env.bg / "cubemap.jpg", xmp=gpano(projection="cubemap"))
    # The same photo (name without GNOME's time stamp, same size) once
    photo360(env.bg / "2026-01-02-03-04-05-b-lake.jpg")
    # The Pictures folder's 360
    photo360(env.pictures / "360" / "d-city.jpg")
    jpeg(env.pictures / "360" / "e-noxmp.jpg")
    r = env.run([expand(), wait_tiles(PANO, 4), *wait_thumbs(PANO, "tiles", 4), dump("page")])
    t = r.value("tiles")
    assert names(t) == ["a-flat.png", "b-lake.jpg", "2026-01-02-03-04-05-c-sky.jpg", "d-city.jpg"]
    # Copies under ~/.local/share/backgrounds can be removed, the Pictures folder's not
    assert [x["remove"] for x in t] == ["Remove", "Remove", "Remove", None]
    assert all(not x["active"] and x["check"] and not x["placeholder"] for x in t)
    # Thumbnails: the middle of the photo cut to 4:3 at 288x216
    assert all(x["size"] == [288, 216] for x in t)
    screen = r.dump("page")
    assert not screen.has(type="AdwActionRow", text="Nothing added yet")
    assert not r.warnings()


def test_empty_section_says_so_and_makes_its_folders(env):
    r = env.run([expand(), wait_tiles(PANO, 0), dump("page"), tiles(PANO, "tiles")])
    assert r.value("tiles") == []
    row = r.dump("page").one(type="AdwActionRow", text="Nothing added yet")
    assert row.get("classes") == ["dim-label"]
    # GNOME makes ~/.local/share/backgrounds itself, the 360 folder is made to be watched
    assert env.pano.is_dir()
    # Opening again reads nothing again
    r2 = env.run([expand(), wait_tiles(PANO, 0), set_active(False, type="AdwExpanderRow",
                                                            text="360° Photos"),
                  expand(), settle(), call("empty_row")])
    assert r2.value("empty_row") is False


def test_current_photo_first_wherever_it_is_and_marked(env):
    elsewhere = photo360(env.world.tmp / "away" / "z-far.jpg")
    photo360(env.pano / "a.jpg")
    r = env.run([expand(), wait_tiles(PANO, 2), settle(), tiles(PANO, "tiles"), call("changes"),
                 call("state")],
                before=[gset(AMBIENT, "panorama", json.dumps(str(elsewhere))),
                        gset(AMBIENT, "panorama-enabled", "true")])
    t = r.value("tiles")
    assert names(t) == ["a.jpg", "z-far.jpg"]
    assert [x["active"] for x in t] == [False, True]
    # Not a copy for backgrounds: no cross
    assert t[1]["remove"] is None
    # Active from the start: no change to report
    assert r.value("changes") == []
    assert r.value("state")["panorama_active"] and r.value("state")["replaced"]


def test_tap_shows_the_photo_and_switches_the_video_off(env):
    p = photo360(env.pano / "a.jpg")
    r = env.run([expand(), wait_tiles(PANO, 1), call("activate", PANO, 0), settle(),
                 tiles(PANO, "tiles"), call("changes"), call("state"),
                 gget(AMBIENT, "panorama", "path"), gget(AMBIENT, "video-enabled", "video")],
                before=[gset(AMBIENT, "video", '"/x.webm"'), gset(AMBIENT, "video-enabled", "true")])
    assert r.value("tiles")[0]["active"]
    assert r.value("path") == repr(str(p))
    assert r.value("video") == "false"
    # The video goes off before the photo comes
    assert r.value("changes") == [{"kind": 1, "active": False}, {"kind": 0, "active": True}]
    assert r.value("state")["panorama"] == str(p)


def test_tap_on_the_photo_shown_switches_it_off_and_the_picture_too(env):
    p = photo360(env.pano / "a.jpg")
    r = env.run([expand(), wait_tiles(PANO, 1), call("activate", PANO, 0), settle(),
                 tiles(PANO, "tiles"), call("changes"), call("state"),
                 gget(BG, "picture-options", "bg"), gget(SAVER, "picture-options", "saver")],
                before=[gset(AMBIENT, "panorama", json.dumps(str(p))),
                        gset(AMBIENT, "panorama-enabled", "true"),
                        # Already none on the lock screen: left as it is
                        gset(SAVER, "picture-options", "'none'")])
    assert not r.value("tiles")[0]["active"]
    assert r.value("changes") == [{"kind": 0, "active": False}]
    assert r.value("bg") == "'none'" and r.value("saver") == "'none'"
    assert r.value("state")["picture_off"] and not r.value("state")["replaced"]


def test_keys_changed_elsewhere_move_the_mark(env):
    a = photo360(env.pano / "a.jpg")
    b = photo360(env.pano / "b.jpg", color=(1, 2, 3))
    r = env.run([expand(), wait_tiles(PANO, 2), settle(),
                 gset(AMBIENT, "panorama", json.dumps(str(b))),
                 gset(AMBIENT, "panorama-enabled", "true"), settle(), tiles(PANO, "on"),
                 # Another key of the schema changes nothing
                 gset(AMBIENT, "density", "'high'"), settle(),
                 call("picture_chosen"), settle(), tiles(PANO, "off"), call("changes")],
                params={"silent": "yes"})
    assert [x["active"] for x in r.value("on")] == [False, True]
    assert [x["active"] for x in r.value("off")] == [False, False]
    assert r.value("changes") == [{"kind": 0, "active": True}, {"kind": 0, "active": False}]
    assert a.exists()


def test_folder_changes_show_after_the_delay(env):
    photo360(env.pano / "a.jpg")
    r = env.run([expand(), wait_tiles(PANO, 1),
                 {"op": "write_file", "path": str(env.pano / "c.png"), "text": "not really a png"},
                 wait_tiles(PANO, 2), tiles(PANO, "added"),
                 {"op": "remove_file", "path": str(env.pano / "c.png")},
                 {"op": "remove_file", "path": str(env.pano / "a.jpg")},
                 wait_tiles(PANO, 0), tiles(PANO, "removed"), dump("page")])
    assert names(r.value("added")) == ["a.jpg", "c.png"]
    assert r.value("removed") == []
    assert r.dump("page").has(type="AdwActionRow", text="Nothing added yet")
    # The broken picture got no thumbnail, and said so
    assert any("No thumbnail" in m["message"] for m in r.warnings())


def test_a_rescan_with_the_same_files_keeps_the_tiles(env):
    photo360(env.pano / "a.jpg")
    r = env.run([expand(), wait_tiles(PANO, 1), *wait_thumbs(PANO, "before", 1),
                 # A file that does not count: the list stays the same
                 {"op": "write_file", "path": str(env.pano / "x.txt"), "text": "x"},
                 wait(1200), settle(), tiles(PANO, "after")])
    assert r.value("before") == r.value("after")
    assert r.value("after")[0]["size"] == [288, 216]


# ----- Removing -----

def test_remove_asks_and_deletes_the_copy_shown_and_brings_the_picture_back(env):
    p = photo360(env.pano / "2026-01-02-03-04-05-lake.jpg")
    gone = env.world.tmp / "gone.jpg"
    r = env.run([expand(), wait_tiles(PANO, 1), call("remove", PANO, 0),
                 wait_for(type="AdwAlertDialog"), dump("question"),
                 click(type="GtkButton", text="Remove", within={"type": "AdwAlertDialog"}),
                 wait_tiles(PANO, 0), call("state"), gget(AMBIENT, "panorama", "path"),
                 gget(BG, "picture-uri", "uri"), gget(BG, "picture-options", "options")],
                before=[gset(AMBIENT, "panorama", json.dumps(str(p))),
                        gset(AMBIENT, "panorama-enabled", "true"),
                        gset(BG, "picture-uri", json.dumps(gone.as_uri())),
                        gset(BG, "picture-options", "'stretched'")])
    q = r.dump("question").one(type="AdwAlertDialog")
    assert q["text"] == "Remove “lake.jpg”?"
    assert q["subtitle"] == "Only the copy made for backgrounds is deleted."
    assert not p.exists()
    assert r.value("state")["panorama"] is None
    assert r.value("path") == "''"
    # The picture under it was removed meanwhile: GNOME's default comes back
    assert r.value("uri") != repr(gone.as_uri()) and r.value("options") == "'zoom'"


def test_remove_of_the_photo_set_but_off_keeps_the_picture(env):
    p = photo360(env.pano / "a.jpg")
    gone = env.world.tmp / "gone.jpg"
    r = env.run([expand(), wait_tiles(PANO, 1), call("remove", PANO, 0),
                 wait_for(type="AdwAlertDialog"),
                 click(type="GtkButton", text="Remove", within={"type": "AdwAlertDialog"}),
                 wait_tiles(PANO, 0), gget(AMBIENT, "panorama", "path"),
                 gget(BG, "picture-uri", "uri")],
                before=[gset(AMBIENT, "panorama", json.dumps(str(p))),
                        gset(BG, "picture-uri", json.dumps(gone.as_uri()))])
    assert r.value("path") == "''"
    assert r.value("uri") == repr(gone.as_uri())


@pytest.mark.parametrize("uri, options, kept", [
    ("http://example.org/a.jpg", "'zoom'", True),      # not a file: nothing to check
    (None, "'none'", True),                             # no picture shows: the color stays
    ("exists", "'zoom'", True),                         # the picture is still there
])
def test_picture_after_removing_the_photo_shown(env, uri, options, kept):
    p = photo360(env.pano / "a.jpg")
    pic = png(env.world.tmp / "pic.png")
    if uri == "exists":
        uri = pic.as_uri()
    elif uri is None:
        uri = (env.world.tmp / "gone.jpg").as_uri()
    r = env.run([expand(), wait_tiles(PANO, 1), call("remove", PANO, 0),
                 wait_for(type="AdwAlertDialog"),
                 click(type="GtkButton", text="Remove", within={"type": "AdwAlertDialog"}),
                 wait_tiles(PANO, 0), gget(BG, "picture-uri", "uri"),
                 gget(BG, "picture-options", "options")],
                before=[gset(AMBIENT, "panorama", json.dumps(str(p))),
                        gset(AMBIENT, "panorama-enabled", "true"),
                        gset(BG, "picture-uri", json.dumps(uri)),
                        gset(BG, "picture-options", options)])
    assert r.value("uri") == repr(uri)
    assert r.value("options") == options


def test_cancel_keeps_the_file(env):
    p = photo360(env.pano / "a.jpg")
    r = env.run([expand(), wait_tiles(PANO, 1), call("remove", PANO, 0),
                 wait_for(type="AdwAlertDialog"),
                 click(type="GtkButton", text="Cancel", within={"type": "AdwAlertDialog"}),
                 wait_for(type="AdwAlertDialog", gone=True), wait(700), tiles(PANO, "tiles")])
    assert p.exists() and len(r.value("tiles")) == 1


def test_remove_of_another_file_than_the_one_set(env):
    a = photo360(env.pano / "a.jpg")
    b = photo360(env.pano / "b.jpg", color=(1, 1, 1))
    r = env.run([expand(), wait_tiles(PANO, 2), call("remove", PANO, 1),
                 wait_for(type="AdwAlertDialog"),
                 click(type="GtkButton", text="Remove", within={"type": "AdwAlertDialog"}),
                 wait_tiles(PANO, 1), tiles(PANO, "tiles"), gget(AMBIENT, "panorama", "path")],
                before=[gset(AMBIENT, "panorama", json.dumps(str(a))),
                        gset(AMBIENT, "panorama-enabled", "true")])
    assert not b.exists() and a.exists()
    assert names(r.value("tiles")) == ["a.jpg"] and r.value("tiles")[0]["active"]
    assert r.value("path") == repr(str(a))


def test_remove_of_a_file_already_gone_warns(env):
    p = photo360(env.pano / "a.jpg")
    r = env.run([expand(), wait_tiles(PANO, 1), call("remove", PANO, 0),
                 wait_for(type="AdwAlertDialog"),
                 {"op": "remove_file", "path": str(p)},
                 click(type="GtkButton", text="Remove", within={"type": "AdwAlertDialog"}),
                 wait_tiles(PANO, 0)])
    assert [m["message"] for m in r.warnings()
            if m["message"].startswith("Could not remove")] == [
        f"Could not remove {p}: Error removing file {p}: No such file or directory"]


def test_question_of_the_chooser(env):
    r = env.run([call("confirm_remove", "/x/2026-01-02-03-04-05-pic.png"),
                 wait_for(type="AdwAlertDialog"), dump("q"),
                 click(type="GtkButton", text="Remove", within={"type": "AdwAlertDialog"}),
                 settle(), call("confirmations", label="yes"),
                 call("confirm_remove_no_destroy", "/x/pic.png"),
                 wait_for(type="AdwAlertDialog"),
                 {"op": "close_dialog"}, wait_for(type="AdwAlertDialog", gone=True),
                 call("confirmations", label="no")])
    assert r.dump("q").one(type="AdwAlertDialog")["text"] == "Remove “pic.png”?"
    assert r.value("yes") == ["/x/2026-01-02-03-04-05-pic.png"]
    assert r.value("no") == ["/x/2026-01-02-03-04-05-pic.png"]


# ----- Background picture keys -----

def test_reset_and_clear_picture(env):
    r = env.run([call("reset_picture"), gget(BG, "picture-uri", "uri"),
                 gget(BG, "picture-options", "bg"), gget(SAVER, "picture-options", "saver"),
                 gget(BG, "primary-color", "color"), call("state", label="before"),
                 call("clear_picture"), gget(BG, "picture-options", "bg2"),
                 gget(SAVER, "picture-options", "saver2"), call("state", label="after")],
                before=[gset(BG, "picture-uri", '"file:///x.jpg"'),
                        gset(BG, "picture-options", "'centered'"),
                        gset(SAVER, "picture-options", "'centered'"),
                        gset(BG, "primary-color", "'#123456'")])
    assert r.value("uri") != "'file:///x.jpg'"
    assert r.value("bg") == "'zoom'" and r.value("saver") == "'zoom'"
    # The color is the Style page's
    assert r.value("color") == "'#123456'"
    assert not r.value("before")["picture_off"]
    assert r.value("bg2") == "'none'" and r.value("saver2") == "'none'"
    assert r.value("after")["picture_off"]


def test_current_needs_the_switch_and_a_path(env):
    r = env.run([call("state", label="empty")],
                before=[gset(AMBIENT, "video-enabled", "true"), gset(AMBIENT, "video", "''"),
                        gset(AMBIENT, "panorama", "'/p.jpg'")])
    assert r.value("empty") == {"panorama": None, "video": None, "panorama_active": False,
                                "video_active": False, "replaced": False, "picture_off": False}


# ----- Recognising and sorting files -----

def test_is_photo_by_the_xmp(env):
    t = env.world.tmp
    big = t / "far.jpg"
    # XMP beyond the first 512 KiB is not looked for
    big.write_bytes(b"\0" * (600 * 1024) + gpano().encode())
    cases = {
        "pano": photo360(t / "p.jpg"),
        "plain": jpeg(t / "plain.jpg"),
        "cubemap": jpeg(t / "c.jpg", xmp=gpano(projection="cubemap")),
        "no_ns": jpeg(t / "n.jpg", xmp=gpano(ns="http://example.org/")),
        "unclosed": jpeg(t / "u.jpg", xmp=gpano(close=False)),
        "far": big,
        "tiny": t / "tiny.jpg",
        "missing": t / "missing.jpg",
    }
    (t / "tiny.jpg").write_bytes(b"<x")
    steps = [call("is_photo", str(p), label=k) for k, p in cases.items()]
    steps.append(call("is_photo", "", label="empty_name"))
    r = env.run(steps)
    assert {k: r.value(k) for k in cases} == {
        "pano": True, "plain": False, "cubemap": False, "no_ns": False, "unclosed": False,
        "far": False, "tiny": False, "missing": False}


def test_sort_added_files(env):
    t = env.world.tmp
    noname = t / "upload"
    shutil.copy(photo360(t / "p0.jpg"), noname)
    vid = t / "clip"
    shutil.copy(env.media["long"], vid)
    r = env.run([call("sort", str(photo360(t / "p.jpg")), label="pano"),
                 call("sort", str(jpeg(t / "plain.jpg")), label="plain"),
                 call("sort", str(gif(t / "a.gif")), label="gif"),
                 call("sort", str(env.media["long"]), label="video"),
                 # The content says what the name does not
                 call("sort", str(noname), label="noname"),
                 call("sort", str(vid), label="clip"),
                 # Not there: the type from the name
                 call("sort", str(t / "missing.webm"), label="missing"),
                 call("sort", str(t / "missing.txt"), label="text"),
                 # No path at all
                 call("sort_uri", "http://example.org/p.jpg", label="uri")])
    assert {k: r.value(k) for k in ("pano", "plain", "gif", "video", "noname", "clip", "missing",
                                    "text", "uri")} == {
        "pano": PANO, "plain": -1, "gif": VIDEO, "video": VIDEO, "noname": PANO, "clip": VIDEO,
        "missing": VIDEO, "text": -1, "uri": False}


def test_add_copies_under_a_free_name(env):
    t = env.world.tmp
    src = photo360(t / "src" / "lake.jpg")
    noext = t / "src" / "lake"
    shutil.copy(src, noext)
    dot = t / "src" / ".hidden"
    shutil.copy(src, dot)
    photo360(env.pano / "lake.jpg", color=(9, 9, 9))
    r = env.run([call("add", PANO, str(src), label="a1"), wait(300),
                 call("add", PANO, str(src), label="a2"), wait(300),
                 call("add", PANO, str(noext), label="a3"), wait(300),
                 call("add", PANO, str(noext), label="a4"), wait(300),
                 call("add", PANO, str(dot), label="a5"), wait(300),
                 call("add", PANO, str(dot), label="a6"), wait(300),
                 # Already in its folder: nothing
                 call("add", PANO, str(env.pano / "lake.jpg"), label="a7"), wait(300),
                 # The video folder is made on the first video
                 call("add", VIDEO, str(env.media["short"]), label="v"), wait(300),
                 # What cannot be read: a warning
                 call("add", VIDEO, str(t / "missing.webm"), label="m"), wait(300), settle()])
    assert sorted(p.name for p in env.pano.iterdir()) == [
        ".hidden", ".hidden-2", "lake", "lake-2", "lake-2.jpg", "lake-3.jpg", "lake.jpg"]
    assert (env.pano / "lake-2.jpg").read_bytes() == src.read_bytes()
    assert [p.name for p in env.video.iterdir()] == ["short.webm"]
    assert [m["message"] for m in r.warnings() if "copy" in m["message"]] == [
        "Could not copy a background: Error when getting information for file "
        f"“{t / 'missing.webm'}”: No such file or directory"]


def test_add_where_the_folder_cannot_be_made(env):
    env.share.mkdir(parents=True, exist_ok=True)
    (env.share / "backgrounds").write_text("a file, not a folder")
    r = env.run([call("add", PANO, str(photo360(env.world.tmp / "p.jpg")))])
    assert [m["message"] for m in r.warnings()] == [
        f"Could not make {env.pano}: Not a directory"]


def test_add_when_every_number_is_taken(env):
    src = photo360(env.world.tmp / "src" / "p.jpg")
    env.pano.mkdir(parents=True)
    (env.pano / "p.jpg").write_text("x")
    for n in range(2, 1000):
        (env.pano / f"p-{n}.jpg").write_text("x")
    r = env.run([call("add", PANO, str(src)), wait(300), settle()])
    # Every name up to -999 is taken: the copy onto the last one fails, the old file stays
    assert (env.pano / "p-999.jpg").read_text() == "x"
    assert any("Could not copy a background" in m["message"] for m in r.warnings())


# ----- Videos -----

def test_video_tiles_with_frames_and_placeholders(env):
    env.video.mkdir(parents=True)
    shutil.copy(env.media["long"], env.video / "long.webm")
    shutil.copy(env.media["short"], env.video / "short.webm")
    gif(env.video / "anim.gif")
    (env.video / "broken.webm").write_bytes(b"\x1a\x45\xdf\xa3 not a video")
    (env.video / "pic.png").write_bytes(b"")
    r = env.run([expand("Video"), wait_tiles(VIDEO, 4), *wait_thumbs(VIDEO, "tiles", 3),
                 wait(1000), settle(), dump("page")])
    t = {Path(x["path"]).name: x for x in r.value("tiles")}
    assert list(t) == ["anim.gif", "broken.webm", "long.webm", "short.webm"]
    for name in ("anim.gif", "long.webm", "short.webm"):
        assert t[name]["size"] == [288, 216], name
        assert not t[name]["placeholder"]
        assert "plinius-file-placeholder" not in t[name]["overlay_classes"]
        assert "slideshow-icon" in t[name]["mark_classes"]
        assert "dim-label" not in t[name]["mark_classes"]
    # Nothing decodes it: the icon and the name stay
    b = t["broken.webm"]
    assert b["size"] == [] and b["placeholder"]
    assert "plinius-file-placeholder" in b["overlay_classes"]
    assert "dim-label" in b["mark_classes"]
    assert r.dump("page").has(type="GtkLabel", text="broken.webm")
    assert any(m["message"].startswith("No thumbnail") for m in r.warnings())
    # Frames of videos are kept (GIFs are not)
    cache = env.home / ".cache/plinius-control-center/video-thumbnails"
    assert len(list(cache.glob("*.png"))) == 2


def test_video_thumbnail_from_the_cache(env):
    env.video.mkdir(parents=True)
    shutil.copy(env.media["long"], env.video / "long.webm")
    env.run([expand("Video"), wait_tiles(VIDEO, 1), *wait_thumbs(VIDEO, "t", 1), settle()])
    cache = env.home / ".cache/plinius-control-center/video-thumbnails"
    [kept] = cache.glob("*.png")
    png(kept, size=(10, 10))
    r = env.run([expand("Video"), wait_tiles(VIDEO, 1), *wait_thumbs(VIDEO, "tiles", 1)])
    # The kept frame, not a new one
    assert r.value("tiles")[0]["size"] == [10, 10]


def test_video_cache_cannot_be_written(env):
    env.video.mkdir(parents=True)
    shutil.copy(env.media["short"], env.video / "short.webm")
    (env.home / ".cache/plinius-control-center").write_text("a file")
    r = env.run([expand("Video"), wait_tiles(VIDEO, 1), *wait_thumbs(VIDEO, "tiles", 1)])
    assert r.value("tiles")[0]["size"] == [288, 216]
    assert not r.warnings()


def test_video_tap_and_the_current_one(env):
    env.video.mkdir(parents=True)
    v = env.video / "short.webm"
    shutil.copy(env.media["short"], v)
    r = env.run([expand("Video"), wait_tiles(VIDEO, 1), call("activate", VIDEO, 0), settle(),
                 tiles(VIDEO, "tiles"), call("changes"), call("state"),
                 gget(AMBIENT, "panorama-enabled", "pano")],
                before=[gset(AMBIENT, "panorama", "'/p.jpg'"),
                        gset(AMBIENT, "panorama-enabled", "true")])
    assert r.value("tiles")[0]["active"]
    assert r.value("state")["video"] == str(v)
    assert r.value("pano") == "false"
    assert r.value("changes") == [{"kind": 0, "active": False}, {"kind": 1, "active": True}]


def test_video_folder_made_later_is_not_watched(env):
    # The video section watches a folder that is not there yet: shown once the section opens
    r = env.run([expand("Video"), wait_tiles(VIDEO, 0), dump("page")])
    assert r.dump("page").has(type="AdwActionRow", text="Nothing added yet")
    assert not env.video.exists()


def test_watch_fails_on_a_folder_that_is_a_file(env):
    env.bg.mkdir(parents=True)
    env.video.write_text("a file")
    r = env.run([expand("Video"), wait_tiles(VIDEO, 0), dump("page")])
    # Nothing to read there: the section says it is empty, and nothing is touched
    assert r.dump("page").has(type="AdwActionRow", text="Nothing added yet")
    assert env.video.read_text() == "a file"


# ----- Thumbnails outside a section -----

def test_thumbnail_drops_the_placeholder(env):
    p = gif(env.world.tmp / "a.gif", size=(40, 40))
    r = env.run([call("thumbnail", VIDEO, str(p), 0, 0, True), call("loose", 8000, label="loose"), call("drop_placeholder_null")])
    loose = r.value("loose")
    # Sizes below one pixel are one pixel
    assert loose["size"] == [1, 1]
    assert not loose["placeholder"] and "plinius-file-placeholder" not in loose["classes"]


def test_thumbnail_of_a_tall_picture(env):
    p = png(env.world.tmp / "tall.png", size=(20, 200))
    r = env.run([call("thumbnail", VIDEO, str(p), 40, 30, False), call("loose", 8000, label="loose")])
    assert r.value("loose") == {"size": [40, 30], "placeholder": False, "classes": []}


def test_video_thumbnail_of_a_missing_file(env):
    r = env.run([call("thumbnail", VIDEO, str(env.world.tmp / "gone.webm"), 40, 30, True),
                 wait(1500), settle(), call("loose", label="loose")])
    assert r.value("loose")["placeholder"]
    assert any(m["message"].startswith("No thumbnail") for m in r.warnings())
