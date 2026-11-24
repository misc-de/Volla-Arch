# phosh-plinius 0108: the power menu stands in two columns while its monitor is wider than high.
# set_landscape () and on_monitor_configured () run here as they are in the patch, compiled
# against GLib with a grid that only records where each button is attached.

import re
import subprocess

import pytest

from conftest import PKG, compile_c

PHOSH = PKG / "phosh-plinius"
PATCH = next(PHOSH.glob("0108-power-menu-*.patch"))
ONE_COLUMN = next(PHOSH.glob("0092-power-menu-*.patch"))

BUTTONS = ["stack", "btn_restart", "btn_restart_shell", "btn_lock", "btn_screenshot",
           "btn_emergency_call", "btn_logout"]

HARNESS = r"""
#include <glib.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct { const char *name; int column, row; } GtkWidget;
typedef GtkWidget GtkStack;
typedef struct { struct { gint32 x, y, width, height; } logical; } PhoshMonitor;

typedef struct {
  GtkStack  *stack;
  GtkWidget *btn_logout;
  GtkWidget *grid;
  GtkWidget *btn_restart;
  GtkWidget *btn_restart_shell;
  GtkWidget *btn_lock;
  GtkWidget *btn_screenshot;
  GtkWidget *btn_emergency_call;
  gboolean   landscape;
} PhoshPowerMenu;

#define GTK_WIDGET(w) ((GtkWidget *) (w))
#define GTK_CONTAINER(w) (w)
#define PHOSH_SYSTEM_MODAL_DIALOG(w) (w)

static PhoshPowerMenu menu;
static int moves, style_class = -1, wide = -1;

static void
gtk_container_child_set (GtkWidget *grid, GtkWidget *child, const char *first, ...)
{
  va_list args;

  if (grid != menu.grid)
    abort ();
  va_start (args, first);
  for (const char *name = first; name; name = va_arg (args, const char *)) {
    int value = va_arg (args, int);

    if (strcmp (name, "left-attach") == 0)
      child->column = value;
    else if (strcmp (name, "top-attach") == 0)
      child->row = value;
    else
      abort ();
  }
  va_end (args);
  moves++;
}

static void
phosh_util_toggle_style_class (GtkWidget *widget, const char *name, gboolean on)
{
  if (widget != GTK_WIDGET (&menu) || strcmp (name, "landscape") != 0)
    abort ();
  style_class = on;
}

static void
phosh_system_modal_dialog_set_wide (PhoshPowerMenu *self, gboolean on)
{
  if (self != &menu)
    abort ();
  wide = on;
}

@CODE@

static GtkWidget *
button (const char *name, int row)
{
  GtkWidget *widget = g_new0 (GtkWidget, 1);

  widget->name = name;
  widget->row = row;
  return widget;
}

int
main (int argc, char **argv)
{
  /* As power-menu.ui (0092) attaches them: one column */
  menu.grid = button ("grid", 0);
  menu.stack = button ("stack", 0);
  menu.btn_restart = button ("btn_restart", 1);
  menu.btn_restart_shell = button ("btn_restart_shell", 2);
  menu.btn_lock = button ("btn_lock", 3);
  menu.btn_screenshot = button ("btn_screenshot", 4);
  menu.btn_emergency_call = button ("btn_emergency_call", 5);
  menu.btn_logout = button ("btn_logout", 6);

  /* Each argument: "<width>x<height>", the monitor's logical size as it is configured */
  for (int i = 1; i < argc; i++) {
    PhoshMonitor monitor = { 0 };
    GtkWidget *all[] = { menu.stack, menu.btn_restart, menu.btn_restart_shell, menu.btn_lock,
                         menu.btn_screenshot, menu.btn_emergency_call, menu.btn_logout };

    if (sscanf (argv[i], "%dx%d", &monitor.logical.width, &monitor.logical.height) != 2)
      return 2;
    moves = 0;
    on_monitor_configured (&menu, &monitor);
    printf ("moves=%d landscape=%d class=%d wide=%d", moves, menu.landscape, style_class, wide);
    for (guint j = 0; j < G_N_ELEMENTS (all); j++)
      printf (" %s=%d,%d", all[j]->name, all[j]->column, all[j]->row);
    printf ("\n");
  }
  return 0;
}
"""


def added(path):
    """The lines the patch adds to one file."""
    part = PATCH.read_text().split(f"+++ b/{path}\n", 1)[1].split("\ndiff --git", 1)[0]
    return "\n".join(line[1:] for line in part.splitlines() if line.startswith("+"))


def patch_code():
    """set_landscape () and on_monitor_configured () as 0108 adds them to power-menu.c."""
    code = added("src/power-menu.c")
    start = code.index("/*\n * Plinius: upright the buttons")
    end = code.index("\n}\n", code.index("on_monitor_configured (PhoshPowerMenu")) + 3
    return code[start:end]


@pytest.fixture(scope="module")
def configure(tmp_path_factory):
    build = tmp_path_factory.mktemp("power-menu")
    source = build / "harness.c"
    source.write_text(HARNESS.replace("@CODE@", patch_code()))
    program = compile_c(build / "harness", [source], pkgs=["glib-2.0"])

    def run(*sizes):
        r = subprocess.run([str(program), *sizes], capture_output=True, text=True, check=True)
        states = []
        for line in r.stdout.splitlines():
            fields = dict(f.split("=") for f in line.split())
            states.append({k: (tuple(map(int, v.split(","))) if "," in v else int(v))
                           for k, v in fields.items()})
        return states
    return run


def places(state):
    return {name: state[name] for name in BUTTONS}


PORTRAIT = {name: (0, row) for row, name in enumerate(BUTTONS)}
LANDSCAPE = {
    "stack": (0, 0), "btn_restart": (0, 1), "btn_restart_shell": (0, 2), "btn_logout": (0, 3),
    "btn_lock": (1, 0), "btn_screenshot": (1, 1), "btn_emergency_call": (1, 2),
}


def test_upright_the_menu_stays_as_it_is(configure):
    # The phone upright (360x800 logical) and a square monitor: one column, nothing touched
    for state in configure("360x800", "500x500"):
        assert state["moves"] == 0 and state["landscape"] == 0
        assert state["class"] == -1 and state["wide"] == -1
        assert places(state) == PORTRAIT


def test_turned_the_buttons_stand_in_two_columns(configure):
    state, = configure("800x360")
    assert state["landscape"] == 1 and state["class"] == 1 and state["wide"] == 1
    assert state["moves"] == len(BUTTONS)
    assert places(state) == LANDSCAPE
    # No two buttons in one cell, power on the left, the rest on the right
    assert len(set(LANDSCAPE.values())) == len(LANDSCAPE)
    assert max(row for column, row in LANDSCAPE.values() if column == 1) < \
        max(row for column, row in LANDSCAPE.values() if column == 0)


def test_turning_back_restores_the_column(configure):
    turned, again, back = configure("800x360", "780x360", "360x800")
    assert places(turned) == LANDSCAPE
    # A new size in landscape moves nothing
    assert again["moves"] == 0 and places(again) == LANDSCAPE
    assert back["landscape"] == 0 and back["class"] == 0 and back["wide"] == 0
    assert back["moves"] == len(BUTTONS) and places(back) == PORTRAIT


def test_the_monitor_decides_before_the_menu_has_a_size():
    # Laid out in one column first, the surface would keep that height (seen in the preview)
    code = added("src/power-menu.c")
    constructed = code.split("phosh_power_menu_constructed (GObject *object)\n{", 1)[1]
    constructed = constructed.split("\n}\n", 1)[0]
    assert constructed.index("->constructed (object);") < \
        constructed.index('g_object_get (self, "monitor", &monitor, NULL);') < \
        constructed.index("on_monitor_configured (self, monitor);") < \
        constructed.index('g_signal_connect_object (monitor, "configured"')
    assert "G_CONNECT_SWAPPED" in constructed
    assert "object_class->constructed = phosh_power_menu_constructed;" in code
    assert "configure-event" not in code


def test_the_column_is_the_one_0092_lays_out():
    # 0092's commit message lists the buttons in the order of their rows
    text = ONE_COLUMN.read_text()
    order = ["Power Off", "Restart,", "Restart Phosh", "Lock", "Screenshot", "Emergency",
             "Log Out"]
    message = " ".join(text.split("\n---\n", 1)[0].split())
    assert [message.index(label) for label in order] == \
        sorted(message.index(label) for label in order)
    # and the attach values in the patch's ui hunks are rows 0 to 6 in column 0
    rows = re.findall(r'^\+\s+<property name="top-attach">(\d)</property>', text, re.M)
    assert sorted(map(int, rows)) == [2, 3, 4, 5, 6]


def test_every_button_is_a_template_child():
    # stack and btn_logout were template children before 0108
    code = added("src/power-menu.c")
    for name in ["grid", *BUTTONS[1:-1]]:
        assert f"GtkWidget *{name};" in code
        assert f"bind_template_child (widget_class, PhoshPowerMenu, {name});" in code
    assert '<object class="GtkGrid" id="grid">' in added("src/ui/power-menu.ui")


def test_the_wide_card_fits_the_turned_screen():
    code = added("src/system-modal-dialog.c")
    # Wider card, no margins of its own; upright it gets back the values of system-modal-dialog.ui
    assert "hdy_clamp_set_maximum_size (HDY_CLAMP (priv->clamp), wide ? 640 : 400);" in code
    assert "gtk_widget_set_margin_top (priv->box_dialog, wide ? 0 : 24);" in code
    assert "gtk_widget_set_margin_bottom (priv->box_dialog, wide ? 0 : 24);" in code
    assert "phosh_system_modal_dialog_set_wide (PhoshSystemModalDialog *self, gboolean wide);" \
        in added("src/system-modal-dialog.h")
    css = added("src/stylesheet/common.css")
    assert re.search(r"phosh-power-menu\.landscape \.phosh-system-modal-dialog \{\n\s+margin: 8px 24px;",
                     css)
