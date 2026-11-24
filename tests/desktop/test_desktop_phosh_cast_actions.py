# phosh-plinius 0113: a TV's row in the cast quick setting gets a button for each thing Schwupp
# offers for it (its Actions property: the phone's screen, a YouTube link, a video from the phone,
# the desktop); speakers get none, the active row says what the TV gets. cast-quick-setting.c is
# rebuilt from the patches that touch it (0019 makes it) and its get_actions (),
# on_action_clicked (), add_action_buttons (), streaming_subtitle () and create_row () run as they
# are, compiled against GLib with widgets that only record what is done to them. 0114: every
# string of the quick setting is translated in all languages.

import re
import subprocess

import pytest

from conftest import PKG, compile_c

PHOSH = PKG / "phosh-plinius"
SOURCE = "plugins/cast-quick-setting/cast-quick-setting.c"
PO_PATCH = next(PHOSH.glob("0114-po-The-cast-quick-setting.patch"))
LANGUAGES = ("de", "es", "fr", "it", "nl", "pl", "pt", "pt_BR", "ru")


def patched(path, tmp_path):
    """A file of the plugin as all patches leave it (git apply, only that path)."""
    work = tmp_path / "tree"
    work.mkdir(exist_ok=True)
    for patch in sorted(PHOSH.glob("0*.patch")):
        if f"b/{path}" in patch.read_text(errors="replace"):
            subprocess.run(["git", "apply", f"--include={path}", str(patch)], cwd=work,
                           check=True, capture_output=True)
    return (work / path).read_text()


HARNESS = r"""
#include <glib.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define _(s) (s)
#define N_(s) (s)
typedef void (*GCallback) (void);
#define G_CALLBACK(f) ((GCallback) (f))
#define G_CONNECT_SWAPPED 2
typedef enum { G_DBUS_CALL_FLAGS_NONE = 0, G_DBUS_CALL_FLAGS_NO_AUTO_START = 1 } GDBusCallFlags;
enum { GTK_ICON_SIZE_BUTTON = 4, GTK_ALIGN_CENTER = 3 };

typedef struct _GtkWidget GtkWidget;
struct _GtkWidget {
  const char *kind, *icon, *title, *subtitle, *tooltip, *accessible, *css, *id, *method;
  int activatable, valign, spinning, shown;
  GtkWidget *prefix, *children[8];
  int n_children;
  GCallback clicked;
  gpointer clicked_data;
};
typedef GtkWidget GtkButton;
typedef GtkWidget GtkListBoxRow;
typedef struct { GVariant *values; } GDBusProxy;
typedef struct { GDBusProxy *proxy; } PhoshCastQuickSetting;
typedef struct { int unused; } PhoshShell;
typedef struct { const char *id; } GDesktopAppInfo;

#define HDY_ACTION_ROW(w) ((GtkWidget *) (w))
#define HDY_PREFERENCES_ROW(w) ((GtkWidget *) (w))
#define GTK_LIST_BOX_ROW(w) ((GtkWidget *) (w))
#define GTK_CONTAINER(w) ((GtkWidget *) (w))
#define GTK_SPINNER(w) ((GtkWidget *) (w))
#define G_OBJECT(w) ((GtkWidget *) (w))

static PhoshShell the_shell;

static GtkWidget *
widget (const char *kind, const char *icon)
{
  GtkWidget *w = g_new0 (GtkWidget, 1);

  w->kind = kind;
  w->icon = icon;
  return w;
}

static GtkWidget *hdy_action_row_new (void) { return widget ("row", NULL); }
static GtkWidget *gtk_image_new_from_icon_name (const char *icon, int size)
{ return size == GTK_ICON_SIZE_BUTTON ? widget ("image", icon) : NULL; }
static GtkWidget *gtk_button_new_from_icon_name (const char *icon, int size)
{ return size == GTK_ICON_SIZE_BUTTON ? widget ("button", icon) : NULL; }
static GtkWidget *gtk_spinner_new (void) { return widget ("spinner", NULL); }
static void hdy_preferences_row_set_title (GtkWidget *w, const char *t) { w->title = t; }
static void hdy_action_row_set_subtitle (GtkWidget *w, const char *t) { w->subtitle = t; }
static void gtk_list_box_row_set_activatable (GtkWidget *w, int on) { w->activatable = on; }
static void hdy_action_row_add_prefix (GtkWidget *w, GtkWidget *p) { w->prefix = p; }
static void gtk_container_add (GtkWidget *w, GtkWidget *c) { w->children[w->n_children++] = c; }
static void gtk_spinner_start (GtkWidget *w) { w->spinning = 1; }
static void gtk_widget_show_all (GtkWidget *w) { w->shown = 1; }
static void gtk_widget_set_valign (GtkWidget *w, int a) { w->valign = a; }
static GtkWidget *gtk_widget_get_style_context (GtkWidget *w) { return w; }
static void gtk_style_context_add_class (GtkWidget *w, const char *c) { w->css = c; }
static void gtk_widget_set_tooltip_text (GtkWidget *w, const char *t) { w->tooltip = t; }
static GtkWidget *gtk_widget_get_accessible (GtkWidget *w) { return w; }
static void atk_object_set_name (GtkWidget *w, const char *t) { w->accessible = t; }
static PhoshShell *phosh_shell_get_default (void) { return &the_shell; }
static void phosh_shell_fold_top_panel (PhoshShell *shell)
{ printf ("fold %s\n", shell == &the_shell ? "shell" : "?"); }
static void write_consent_stamp (void) { printf ("stamp\n"); }
static int remote_installed = 1;
static GDesktopAppInfo *g_desktop_app_info_new (const char *id)
{
  GDesktopAppInfo *info;

  if (!remote_installed)
    return NULL;
  info = g_new0 (GDesktopAppInfo, 1);
  info->id = id;
  return info;
}
static void g_desktop_app_info_launch_action (GDesktopAppInfo *info, const char *action, void *ctx)
{ printf ("launch %s %s %s\n", info->id, action, ctx ? "ctx" : "-"); }
G_DEFINE_AUTOPTR_CLEANUP_FUNC (GDesktopAppInfo, g_free)

static void
g_object_set_data_full (GtkWidget *w, const char *key, char *value, GDestroyNotify destroy)
{
  if (strcmp (key, "cast-id") != 0 || destroy != g_free)
    g_error ("unexpected data %s", key);
  w->id = value;
}

static void
g_object_set_data (GtkWidget *w, const char *key, gpointer value)
{
  if (strcmp (key, "cast-method") != 0)
    g_error ("unexpected data %s", key);
  w->method = value;
}

static const char *
g_object_get_data (GtkWidget *w, const char *key)
{
  if (strcmp (key, "cast-id") == 0)
    return w->id;
  return strcmp (key, "cast-method") == 0 ? w->method : NULL;
}

static void
g_signal_connect_object (GtkWidget *w, const char *signal, GCallback cb, gpointer data, int flags)
{
  if (strcmp (signal, "clicked") != 0 || flags != G_CONNECT_SWAPPED)
    g_error ("unexpected signal %s", signal);
  w->clicked = cb;
  w->clicked_data = data;
}

static GVariant *
g_dbus_proxy_get_cached_property (GDBusProxy *proxy, const char *name)
{
  return g_variant_lookup_value (proxy->values, name, NULL);
}

static void
call (PhoshCastQuickSetting *self, const char *method, GVariant *params, GDBusCallFlags flags)
{
  g_autoptr (GVariant) owned = params ? g_variant_ref_sink (params) : NULL;
  g_autofree char *text = owned ? g_variant_print (owned, FALSE) : g_strdup ("NULL");

  printf ("call %s %s %d\n", method, text, flags);
}

#include CAST_FUNCTIONS

static void
print_widget (const char *prefix, GtkWidget *w)
{
  printf ("%s kind=%s icon=%s title=%s subtitle=%s id=%s method=%s activatable=%d shown=%d"
          " valign=%d css=%s tooltip=%s accessible=%s spinning=%d prefix=%s children=%d\n",
          prefix, w->kind, w->icon ?: "-", w->title ?: "-", w->subtitle ?: "-", w->id ?: "-",
          w->method ?: "-", w->activatable, w->shown, w->valign, w->css ?: "-",
          w->tooltip ?: "-", w->accessible ?: "-", w->spinning,
          w->prefix ? w->prefix->icon : "-", w->n_children);
  for (int i = 0; i < w->n_children; i++)
    print_widget ("child", w->children[i]);
}

int
main (int argc, char **argv)
{
  PhoshCastQuickSetting self = { NULL };
  GDBusProxy proxy;

  /* CMD ... PROPERTIES (a{sv} as text, "" = no Schwupp on the bus) */
  if (*argv[argc - 1]) {
    proxy.values = g_variant_parse (G_VARIANT_TYPE_VARDICT, argv[argc - 1], NULL, NULL, NULL);
    self.proxy = &proxy;
  }

  if (strcmp (argv[1], "row") == 0) {
    /* row ID NAME DESCRIPTION ICON ACTIVE STATE SOURCE [CLICK-INDEX] PROPERTIES */
    GtkWidget *row = create_row (&self, argv[2], argv[3], argv[4], argv[5], atoi (argv[6]),
                                 argv[7], argv[8]);

    print_widget ("row", row);
    if (argc > 10) {
      GtkWidget *button = row->children[atoi (argv[9])];

      ((void (*) (PhoshCastQuickSetting *, GtkButton *)) button->clicked) (button->clicked_data,
                                                                         button);
      if (button->clicked_data != &self)
        return 2;
    }
    return 0;
  }
  if (strcmp (argv[1], "desktop-shown") == 0) {
    /* desktop-shown INSTALLED PROPERTIES */
    remote_installed = atoi (argv[2]);
    on_desktop_shown (&self);
    printf ("done\n");
    return 0;
  }
  if (strcmp (argv[1], "click") == 0) {
    /* click ID METHOD PROPERTIES: a button without id or method does nothing */
    GtkWidget *button = widget ("button", NULL);

    button->id = *argv[2] ? argv[2] : NULL;
    button->method = *argv[3] ? argv[3] : NULL;
    on_action_clicked (&self, button);
    printf ("done\n");
    return 0;
  }
  /* actions ID PROPERTIES */
  {
    g_auto (GStrv) actions = get_actions (&self, argv[2]);
    g_autofree char *joined = actions ? g_strjoinv (",", actions) : g_strdup ("(none)");

    printf ("actions %s\n", joined);
  }
  return 0;
}
"""


@pytest.fixture(scope="module")
def cast_source(tmp_path_factory):
    return patched(SOURCE, tmp_path_factory.mktemp("cast-source"))


@pytest.fixture(scope="module")
def harness(tmp_path_factory, cast_source):
    d = tmp_path_factory.mktemp("cast-actions")
    start = cast_source.index("static gboolean\nget_discovering")
    end = cast_source.index("\n\nstatic void\nupdate (")
    # The speaker icon and the table of buttons as the plugin defines them
    defines = "".join(line + "\n" for line in cast_source.splitlines()
                      if re.match(r"#define (SPEAKER_ICON|REMOTE_APP|REMOTE_ACTION) ", line))
    table = re.search(r"static const struct \{\n.*?\n\} cast_actions\[\] = \{\n.*?\n\};\n",
                      cast_source, re.S).group(0)
    (d / "functions.c").write_text(defines + table + cast_source[start:end] + "\n")
    (d / "harness.c").write_text(HARNESS)
    return compile_c(d / "harness", [d / "harness.c"],
                     flags=[f'-DCAST_FUNCTIONS="{d / "functions.c"}"'], pkgs=["glib-2.0"])


def run(harness, *args):
    r = subprocess.run([str(harness), *args], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


def fields(line):
    kind, rest = line.split(" ", 1)
    return kind, dict(re.findall(r"(\w+)=(.*?)(?= \w+=|$)", rest))


TV = ["cast:tv", "Living Room", "Chromecast · TV", "video-display-symbolic"]
SPEAKER = ["raop:box", "Kitchen", "AirPlay · speaker", "audio-speakers-symbolic"]
ALL = "{'Actions': <{'cast:tv': ['mirror', 'youtube', 'video', 'desktop'], 'raop:box': []}>}"
BUTTONS = [
    ("phone-symbolic", "Connect", "Mirror the phone's screen on this TV"),
    ("media-playback-start-symbolic", "ChooseLink", "Play a YouTube link on this TV"),
    ("folder-videos-symbolic", "ChooseFile", "Play a video from the phone on this TV"),
    ("computer-symbolic", "ConnectDesktop", "Show the desktop on this TV"),
]


def test_a_tv_gets_a_button_for_everything_schwupp_offers(harness):
    (_, row), *buttons = map(fields, run(harness, "row", *TV, "0", "idle", "", ALL))
    assert (row["title"], row["subtitle"], row["id"], row["prefix"]) == (
        "Living Room", "Chromecast · TV", "cast:tv", "video-display-symbolic")
    # Only the buttons take taps, not the row beside them
    assert row["activatable"] == "0" and row["shown"] == "1" and row["children"] == "4"
    for (_, button), (icon, method, label) in zip(buttons, BUTTONS, strict=True):
        assert (button["kind"], button["icon"], button["method"]) == ("button", icon, method)
        assert button["id"] == "cast:tv" and button["css"] == "flat" and button["valign"] == "3"
        # Seen and read out: the button says what it does
        assert button["tooltip"] == button["accessible"] == label


def test_only_what_the_tv_can_in_the_fixed_order(harness):
    props = "{'Actions': <{'cast:tv': ['desktop', 'mirror']}>}"
    _, *buttons = map(fields, run(harness, "row", *TV, "0", "idle", "", props))
    assert [b["method"] for _, b in buttons] == ["Connect", "ConnectDesktop"]


@pytest.mark.parametrize("device, props", [
    (SPEAKER, ALL),                                          # speakers: sound only
    (TV, ""),                                                # Schwupp not on the bus
    (TV, "{'Discovering': <true>}"),                         # an old Schwupp without Actions
    (TV, "{'Actions': <'mirror'>}"),                         # of another type
    (TV, "{'Actions': <{'cast:other': ['mirror']}>}"),       # not for this device
])
def test_no_buttons(harness, device, props):
    (kind, row), = map(fields, run(harness, "row", *device, "0", "idle", "", props))
    assert row["children"] == "0" and row["subtitle"] == device[2]
    assert row["activatable"] == "1"                         # without buttons a tap starts


@pytest.mark.parametrize("index, expected", [
    # The phone's screen: the tap is the consent, as a tap on the row
    (0, ["stamp", "call Connect ('cast:tv',) 0"]),
    # Schwupp asks in a window: the quick settings fold away first
    (1, ["fold shell", "call ChooseLink ('cast:tv',) 0"]),
    (2, ["fold shell", "call ChooseFile ('cast:tv',) 0"]),
    # No stamp: Schwupp writes the desktop's screen into it
    (3, ["call ConnectDesktop ('cast:tv',) 0"]),
])
def test_each_button_asks_schwupp(harness, index, expected):
    lines = run(harness, "row", *TV, "0", "idle", "", str(index), ALL)
    assert lines[-len(expected):] == expected
    assert not any(line.startswith(("stamp", "fold", "call")) for line in lines[:-len(expected)])


@pytest.mark.parametrize("args", [["", "Connect"], ["cast:tv", ""]])
def test_a_button_without_id_or_method_does_nothing(harness, args):
    assert run(harness, "click", *args, ALL) == ["done"]


@pytest.mark.parametrize("source, subtitle, marked", [
    ("desktop", "Desktop – tap to stop", "ConnectDesktop"),
    ("screen", "Mirroring – tap to stop", "Connect"),
    ("video", "Playing a video – tap to stop", "ChooseFile"),
    ("link", "Playing a link – tap to stop", "ChooseLink"),
])
def test_a_tv_in_use_keeps_its_buttons_with_the_one_in_use_marked(harness, source, subtitle,
                                                                   marked):
    (_, row), *buttons = map(fields, run(harness, "row", *TV, "1", "streaming", source, ALL))
    assert row["subtitle"] == subtitle and row["children"] == "4"
    assert row["activatable"] == "0"
    # Switch over with another button; the marked one stops
    for (_, b), (icon, method, label) in zip(buttons, BUTTONS, strict=True):
        if method == marked:
            assert (b["css"], b["method"], b["icon"]) == ("suggested-action", "Disconnect", icon)
            assert b["tooltip"] == b["accessible"] == "Stop casting to this TV"
        else:
            assert (b["css"], b["method"], b["tooltip"]) == ("flat", method, label)


def test_a_tap_on_the_marked_button_stops(harness):
    lines = run(harness, "row", *TV, "1", "streaming", "desktop", "3", ALL)
    assert lines[-1] == "call Disconnect NULL 0"
    assert not any(line.startswith(("stamp", "fold")) for line in lines)


def test_a_speaker_in_use_shows_a_check(harness):
    (_, row), (_, inner) = map(fields, run(harness, "row", *SPEAKER, "1", "streaming", "screen",
                                           ALL))
    assert row["subtitle"] == "Playing the sound – tap to stop" and row["children"] == "1"
    assert inner["kind"] == "image" and inner["icon"] == "object-select-symbolic"


@pytest.mark.parametrize("device, state, subtitle", [
    (TV, "pin", "Enter the code shown on the TV"),
    (TV, "connecting", "Connecting…"),
])
def test_a_row_being_connected_shows_a_spinner(harness, device, state, subtitle):
    (_, row), (_, inner) = map(fields, run(harness, "row", *device, "1", state, "video", ALL))
    assert row["subtitle"] == subtitle and row["children"] == "1"
    assert inner["kind"] == "spinner" and inner["spinning"] == "1"


@pytest.mark.parametrize("installed, expected", [
    ("1", ["fold shell", "launch org.plinius.Remote.desktop desktop -", "done"]),
    # Without plinius-remote the quick settings still fold away
    ("0", ["fold shell", "done"]),
])
def test_the_desktop_on_the_tv_opens_the_remote(harness, installed, expected):
    assert run(harness, "desktop-shown", installed, ALL) == expected


@pytest.mark.parametrize("dev, props, value", [
    ("cast:tv", ALL, "mirror,youtube,video,desktop"),
    ("raop:box", ALL, ""),
    ("cast:tv", "", "(none)"),
    # Not a{sas}: a newer Schwupp that changed the property is ignored, not trusted
    ("cast:tv", "{'Actions': <@a{sv} {'cast:tv': <'mirror'>}>}", "(none)"),
])
def test_get_actions(harness, dev, props, value):
    assert run(harness, "actions", dev, props) == [f"actions {value}"]


def test_update_reads_the_source(cast_source):
    body = cast_source[cast_source.index("\nupdate (PhoshCastQuickSetting *self)"):]
    body = body[:body.index("\n}\n")]
    assert 'get_string (self, "Source")' in body
    assert "state, source), -1);" in body
    # The remote opens once per desktop, when the TV starts showing it
    assert "if (desktop_shown && !self->desktop_shown)\n    on_desktop_shown (self);" in body
    # The spinner runs from the moment the list is open, not only once Schwupp has started
    assert "if (discovering || self->searching)\n    gtk_spinner_start (self->spinner);" in body


# ----- 0114: the quick setting in all languages -----

def _strings(tmp_path):
    source = patched(SOURCE, tmp_path)
    ids = re.findall(r'\bN?_\("((?:[^"\\]|\\.)*)"\)', source)
    ui = patched("plugins/cast-quick-setting/qs.ui", tmp_path)
    ids += re.findall(r'translatable="yes"[^>]*>([^<]+)<', ui)
    desktop = patched("plugins/cast-quick-setting/cast-quick-setting.desktop.in.in", tmp_path)
    ids += re.findall(r"^(?:Name|Comment)=(.+)$", desktop, flags=re.M)
    return {i.replace("\\'", "'") for i in ids}


def _translations():
    text = PO_PATCH.read_text()
    out = {}
    for part in text.split("diff --git a/po/")[1:]:
        lang = part.split(".po", 1)[0]
        added = "\n".join(line[1:] for line in part.splitlines() if line.startswith("+")
                          and not line.startswith("+++"))
        out[lang] = dict(re.findall(r'^msgid "(.*)"\nmsgstr "(.*)"$', added, flags=re.M))
    return out


def test_every_string_of_the_quick_setting_is_translated(tmp_path):
    strings = _strings(tmp_path)
    assert {"Show the desktop on this TV", "Play a YouTube link on this TV",
            "Playing a video – tap to stop", "Cast"} <= strings
    translations = _translations()
    assert set(translations) == set(LANGUAGES)
    for lang, table in translations.items():
        assert set(table) == strings, lang
        assert all(table.values()), lang
    # pt_BR as pt, as in 0110
    assert translations["pt_BR"] == translations["pt"]


def test_the_translations_compile(tmp_path):
    for lang, table in _translations().items():
        po = tmp_path / f"{lang}.po"
        po.write_text('msgid ""\nmsgstr "Content-Type: text/plain; charset=UTF-8\\n"\n\n'
                      + "".join(f'msgid "{k}"\nmsgstr "{v}"\n\n' for k, v in table.items()))
        subprocess.run(["msgfmt", "--check", "-o", str(tmp_path / f"{lang}.mo"), str(po)],
                       check=True, capture_output=True)
