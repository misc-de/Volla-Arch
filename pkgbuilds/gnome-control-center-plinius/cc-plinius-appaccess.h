/* SPDX-License-Identifier: MIT */
#pragma once

#include <gio/gio.h>
#include <gtk/gtk.h>

#include "shell/cc-panel.h"

G_BEGIN_DECLS

void cc_plinius_appaccess_update_app_page (CcPanel *panel, GAppInfo *info, GtkWidget *group,
                                           GtkWidget *gnome_camera, GtkWidget *gnome_microphone);

G_END_DECLS
