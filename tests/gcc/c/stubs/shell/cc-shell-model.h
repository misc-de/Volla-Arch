/* Stand-in for shell/cc-shell-model.h (tests/gcc): only what our panels call; the test program
 * records each panel's visibility and reports it with the program's state. */
#pragma once

#ifdef GCC_REAL_SHELL_MODEL
/* A test that compiles the tree's shell/cc-shell-model.c (with panels/common/cc-util.c) and
 * builds with -DGCC_REAL_SHELL_MODEL gets GNOME's model, a GtkListStore with the panel list's
 * columns; the test program then records no visibility of its own (test_gcc_hubs.py) */
#include_next <shell/cc-shell-model.h>
#else

#include <adwaita.h>

#include "shell/cc-panel.h"

G_BEGIN_DECLS

#define CC_TYPE_SHELL_MODEL (cc_shell_model_get_type ())
G_DECLARE_FINAL_TYPE (CcShellModel, cc_shell_model, CC, SHELL_MODEL, GObject)

void cc_shell_model_set_panel_visibility (CcShellModel *self, const gchar *id, CcPanelVisibility visible);

G_END_DECLS

#endif
