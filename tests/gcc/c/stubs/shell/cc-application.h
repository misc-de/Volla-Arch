/* Stand-in for shell/cc-application.h (tests/gcc): an AdwApplication with the shell model and the
 * action "launch-panel", which the test program records. */
#pragma once

#include <adwaita.h>

#include "shell/cc-shell-model.h"

G_BEGIN_DECLS

#define CC_TYPE_APPLICATION (cc_application_get_type ())
G_DECLARE_FINAL_TYPE (CcApplication, cc_application, CC, APPLICATION, AdwApplication)

CcShellModel *cc_application_get_model (CcApplication *self);

G_END_DECLS
