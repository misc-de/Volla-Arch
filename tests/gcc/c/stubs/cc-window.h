/* Stand-in for shell/cc-window.h (tests/gcc): the window of the test program is an
 * AdwApplicationWindow with a navigation view, as GNOME Settings' window is for a panel. */
#pragma once

#include <adwaita.h>

#include "shell/cc-panel.h"

G_BEGIN_DECLS

#define CC_TYPE_WINDOW (cc_window_get_type ())
G_DECLARE_FINAL_TYPE (CcWindow, cc_window, CC, WINDOW, AdwApplicationWindow)

AdwNavigationView *cc_window_get_navigation_view (CcWindow *self);

G_END_DECLS
