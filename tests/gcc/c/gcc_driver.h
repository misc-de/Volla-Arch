/* The test program of a GNOME Control Center panel file (tests/gcc, see gcc_harness.py).
 *
 * gcc_driver.c is the program: a window like GNOME Settings' (a navigation view holding the panel
 * and its subpages), the shell's stand-ins (CcWindow, CcApplication, CcShellModel), and a script
 * of actions read from JSON (find a widget, click, type, toggle, wait) with dumps of what is on
 * screen. A test adds a few lines of glue in C that create the panel through the file's own
 * entry point: gcc_test_setup () is required, the others are optional. */
#pragma once

#include <adwaita.h>
#include <json-glib/json-glib.h>

#include "cc-window.h"
#include "shell/cc-panel.h"

G_BEGIN_DECLS

typedef struct {
    CcWindow *window;
    AdwNavigationView *nav;
    CcPanel *panel;
    /* The default panel's page (NULL when gcc_test_create_panel made another panel) */
    AdwPreferencesPage *page;
    /* "params" of the script: strings for the glue */
    JsonObject *params;
} GccTest;

/* Required: create the panel's content through the file's own function */
void gcc_test_setup (GccTest *t);

/* Optional (weak default: a panel with a preferences page, t->page) */
CcPanel *gcc_test_create_panel (GccTest *t);

/* Optional: the script's {"op": "call", "name": ..., "args": [...]} for functions of the file that
 * are not reached through widgets; returns FALSE for an unknown name. The result goes into the
 * builder as one JSON value. */
gboolean gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result);

/* Helpers for the glue */
const char *gcc_test_param (GccTest *t, const char *name);
void gcc_test_add (GccTest *t, GtkWidget *widget);

G_END_DECLS
