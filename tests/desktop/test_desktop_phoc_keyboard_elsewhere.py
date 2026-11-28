# phoc-plinius 0040: plinius-remote's touchpad and keyboard for the desktop on a TV (or over VNC)
# is an ordinary window on the phone. It comes to the front when tapped, but the keyboard stays
# with the most recently focused window on another screen, so what its virtual keyboard types
# reaches the desktop. The lines 0040 adds to seat.c and server.c run here as they are in the
# patch, compiled against GLib with stand-ins for views, outputs, the seat and GSettings.

import ast
import re
import subprocess

import pytest

from conftest import PKG, compile_c

PATCH = next((PKG / "phoc-plinius").glob("0040-seat-*.patch"))
REMOTE = PKG / "plinius-remote" / "plinius-remote.py"

HARNESS = r"""
#include <glib.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct { int unused; } PhocOutput;
typedef struct {
  const char *name, *app_id, *token;
  int output;                       /* index into outputs, -1: on none */
  gboolean minimized;
} PhocView;
typedef struct { PhocView *view; } PhocSeatView;
typedef struct { GQueue *views; } PhocSeatPrivate;
typedef struct { PhocSeatPrivate priv; } PhocSeat;
typedef struct { int settings; } PhocServer;

#define PHOC_IS_SERVER(self) ((self) != NULL)

static PhocOutput outputs[2];
static PhocServer server;
static char **setting;

static PhocServer *phoc_server_get_default (void) { return &server; }
static PhocSeatPrivate *phoc_seat_get_instance_private (PhocSeat *seat) { return &seat->priv; }
static PhocOutput *phoc_view_get_output (PhocView *v) { return v->output < 0 ? NULL : &outputs[v->output]; }
static gboolean phoc_view_is_minimized (PhocView *v) { return v->minimized; }
static const char *phoc_view_get_app_id (PhocView *v) { return v->app_id; }
static const char *phoc_view_get_activation_token (PhocView *v) { return v->token; }
static void phoc_view_flush_activation_token (PhocView *v) { printf ("flush %s\n", v->token); v->token = NULL; }
static char **g_settings_get_strv (int settings, const char *key)
{
  g_assert (strcmp (key, "keyboard-elsewhere") == 0);
  return g_strdupv (setting);
}

#undef g_debug
#define g_debug(...) do { } while (0)

@SERVER@

@HELPERS@

static PhocView *focused;
static gboolean kept;

/* The part of phoc_seat_set_focus_view() that 0040 adds, between the fullscreen check and the
 * focus change: KEPT when the keyboard stays as it is, else FOCUSED gets it */
static void
focus (PhocSeat *seat, PhocView *view, PhocView *prev_focus)
{
  PhocView *token_view = view;

  kept = TRUE;
@BRANCH@
  kept = FALSE;
  focused = view;
}

/* argv[1]: the setting, comma separated; argv[2]: the view to focus; argv[3]: the focused one
 * or "-"; then the seat's views, most recently focused first: name:app_id:output:minimized */
int
main (int argc, char **argv)
{
  PhocSeat seat = { { g_queue_new () } };
  PhocView *view = NULL, *prev = NULL;

  setting = g_strsplit (argv[1], ",", -1);
  for (int i = 4; i < argc; i++) {
    char **f = g_strsplit (argv[i], ":", -1);
    PhocView *v = g_new0 (PhocView, 1);
    PhocSeatView *sv = g_new0 (PhocSeatView, 1);

    v->name = f[0];
    v->app_id = *f[1] ? f[1] : NULL;
    v->output = atoi (f[2]);
    v->minimized = atoi (f[3]);
    v->token = g_strdup_printf ("token-%s", f[0]);
    sv->view = v;
    g_queue_push_tail (seat.priv.views, sv);
    if (strcmp (f[0], argv[2]) == 0)
      view = v;
    if (strcmp (f[0], argv[3]) == 0)
      prev = v;
  }
  focus (&seat, view, prev);
  if (kept)
    printf ("kept %s\n", prev ? prev->name : "(none)");
  else
    printf ("focus %s\n", focused ? focused->name : "(none)");
  return 0;
}
"""


def added_hunks(path):
    """The lines 0040 adds to PATH, one string per hunk."""
    text = PATCH.read_text()
    part = text.split(f"+++ b/{path}\n", 1)[1].split("\ndiff --git", 1)[0]
    hunks = []
    for hunk in ("\n" + part).split("\n@@")[1:]:
        lines = [l[1:] for l in hunk.splitlines()[1:] if l.startswith("+")]
        hunks.append("\n".join(lines))
    return hunks


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    helpers, branch = added_hunks("src/seat.c")
    (server,) = added_hunks("src/server.c")
    out = tmp_path_factory.mktemp("phoc-keyboard-elsewhere")
    src = out / "harness.c"
    src.write_text(HARNESS.replace("@SERVER@", server).replace("@HELPERS@", helpers)
                   .replace("@BRANCH@", branch))
    exe = compile_c(out / "harness", [src], pkgs=["glib-2.0"])

    def run(view, prev, *views, setting="org.plinius.Remote.Desktop"):
        r = subprocess.run([str(exe), setting, view, prev or "-", *views],
                           capture_output=True, text=True, check=True)
        return r.stdout.splitlines()
    return run


REMOTE_ON_PHONE = "remote:org.plinius.Remote.Desktop:0:0"
TV_FIREFOX = "firefox:org.mozilla.firefox:1:0"
PHONE_CALLS = "calls:org.gnome.Calls:0:0"


def test_a_tap_on_the_remote_leaves_the_keyboard_on_the_tv(run):
    # The TV's window has the keyboard; the tap brings the remote to the front, nothing else
    assert run("remote", "firefox", TV_FIREFOX, REMOTE_ON_PHONE) == [
        "flush token-remote", "kept firefox"]


def test_after_a_phone_app_the_keyboard_goes_back_to_the_tv(run):
    # Calls on the phone had the keyboard; back in the remote, typing reaches the TV again
    assert run("remote", "calls", PHONE_CALLS, TV_FIREFOX, REMOTE_ON_PHONE) == [
        "flush token-remote", "focus firefox"]


def test_a_minimized_window_on_the_tv_gets_no_keyboard(run):
    assert run("remote", "calls", PHONE_CALLS, "files:org.gnome.Nautilus:1:1", TV_FIREFOX,
               REMOTE_ON_PHONE) == ["flush token-remote", "focus firefox"]


def test_another_window_of_the_remote_is_no_target(run):
    # A second window of the app (a dialog) on the other screen does not take the keyboard
    assert run("remote", "calls", PHONE_CALLS, "dialog:org.plinius.Remote.Desktop:1:0",
               REMOTE_ON_PHONE) == ["flush token-remote", "focus (none)"]


def test_nothing_on_the_other_screen_leaves_the_keyboard_without_focus(run):
    assert run("remote", "calls", PHONE_CALLS, REMOTE_ON_PHONE) == [
        "flush token-remote", "focus (none)"]
    assert run("remote", None, REMOTE_ON_PHONE) == ["flush token-remote", "kept (none)"]


def test_a_window_on_no_screen_counts_as_elsewhere(run):
    # phoc_view_get_output() gives NULL for a view outside the layout: not the remote's screen
    assert run("remote", None, "lost:org.gnome.Maps:-1:0", REMOTE_ON_PHONE) == [
        "flush token-remote", "focus lost"]


def test_the_remote_on_the_tv_types_into_the_phone(run):
    # Opened on the TV by mistake, its keyboard reaches the phone's window: the other screen
    assert run("remote", None, "remote:org.plinius.Remote.Desktop:1:0", PHONE_CALLS) == [
        "flush token-remote", "focus calls"]


@pytest.mark.parametrize("setting", ["org.plinius.Remote", ""])
def test_other_apps_and_the_bluetooth_remote_focus_as_before(run, setting):
    assert run("calls", "firefox", TV_FIREFOX, PHONE_CALLS) == ["focus calls"]
    assert run("remote", "firefox", TV_FIREFOX, REMOTE_ON_PHONE, setting=setting) == [
        "focus remote"]


def test_a_window_without_app_id_focuses_as_before(run):
    assert run("x", "firefox", TV_FIREFOX, "x::0:0", setting="") == ["focus x"]


def test_the_branch_sits_between_the_fullscreen_check_and_the_focus_change():
    text = PATCH.read_text()
    part = text.split("+++ b/src/seat.c\n", 1)[1].split("\ndiff --git", 1)[0]
    hunk = part.split("\n@@")[1]                      # the second hunk: phoc_seat_set_focus_view
    # After the loop over the fullscreen outputs (the closing braces), before the focus change
    assert hunk.startswith(" -1358,6 +1390,16 @@ phoc_seat_set_focus_view (")
    assert re.search(r"\n     }\n   }\n \n\+  /\* It is in front now.*\n(?:\+.*\n)+"
                     r"   PhocSeatView \*seat_view = NULL;", hunk)
    assert "phoc_server_keeps_keyboard_elsewhere (PhocServer *self," in text  # server.h


def test_the_setting_names_the_remote_s_desktop_window():
    text = PATCH.read_text()
    schema = "\n".join(added_hunks("data/mobi.phosh.phoc.gschema.xml"))
    default = re.search(r'<key name="keyboard-elsewhere" type="as">\s*<default>(.*)</default>',
                        schema).group(1)
    app_id = "org.plinius.Remote" + re.search(r'^DESKTOP_APP_ID = APP_ID \+ "(.*)"$',
                                              REMOTE.read_text(), re.M).group(1)
    assert ast.literal_eval(default) == [app_id]
    assert "Co-Authored-By" in text
