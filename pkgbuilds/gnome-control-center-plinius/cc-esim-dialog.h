/*
 * cc-esim-dialog.h
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include <adwaita.h>

G_BEGIN_DECLS

#define CC_TYPE_ESIM_DIALOG (cc_esim_dialog_get_type ())
G_DECLARE_FINAL_TYPE (CcEsimDialog, cc_esim_dialog, CC, ESIM_DIALOG, AdwDialog)

AdwDialog *cc_esim_dialog_new (const char *ofono_modem);

void cc_esim_probe_async (const char          *imei,
                          GCancellable        *cancellable,
                          GAsyncReadyCallback  callback,
                          gpointer             user_data);
char *cc_esim_probe_finish (GAsyncResult  *result,
                            GError       **error);

/* A label of one's own for each SIM card (eSIM profile or card in a slot), by its ICCID */
GSettings *cc_sim_label_settings (void);
char *cc_sim_label_dup (const char *iccid);
void cc_sim_label_set (const char *iccid,
                       const char *label);

G_END_DECLS
