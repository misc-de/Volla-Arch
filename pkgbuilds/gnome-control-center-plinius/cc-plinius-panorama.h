/*
 * cc-plinius-panorama.h
 *
 * Plinius: 360° photos and videos on the Background page (see cc-plinius-panorama.c).
 *
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include <adwaita.h>

G_BEGIN_DECLS

typedef enum {
    CC_PLINIUS_FILES_PANORAMA,
    CC_PLINIUS_FILES_VIDEO,
} CcPliniusFilesKind;

/* Called when a file of the section is switched on or off, from here or from elsewhere */
typedef void (*CcPliniusPanoramaChanged) (gboolean active, gpointer user_data);
typedef void (*CcPliniusRemoveConfirmed) (gpointer data);

gboolean cc_plinius_panorama_fill (AdwExpanderRow *expander, CcPliniusFilesKind kind,
                                   CcPliniusPanoramaChanged changed, gpointer user_data);
GtkWidget *cc_plinius_files_empty_row_new (void);
const char *cc_plinius_panorama_heading (const char *which);
gboolean cc_plinius_panorama_is_photo (const char *path);

gboolean cc_plinius_panorama_is_active (void);
gboolean cc_plinius_video_is_active (void);
gboolean cc_plinius_background_replaced (void);
void cc_plinius_background_picture_chosen (void);
void cc_plinius_background_reset_picture (void);
gboolean cc_plinius_background_picture_off (void);
void cc_plinius_background_clear_picture (void);
void cc_plinius_background_confirm_remove (GtkWidget *parent, const char *path, CcPliniusRemoveConfirmed confirmed,
                                           gpointer data, GDestroyNotify destroy);

char *cc_plinius_files_current (CcPliniusFilesKind kind);
void cc_plinius_files_thumbnail (CcPliniusFilesKind kind, const char *path, int width, int height,
                                 GtkPicture *picture, GCancellable *cancellable);
void cc_plinius_files_drop_placeholder (GtkWidget *overlay);
void cc_plinius_files_add_placeholder_style (GtkWidget *widget);

gboolean cc_plinius_files_supported (CcPliniusFilesKind kind);
void cc_plinius_files_add_types (GtkFileFilter *filter);
gboolean cc_plinius_files_sort (GFile *file, CcPliniusFilesKind *kind);
void cc_plinius_files_add (CcPliniusFilesKind kind, GFile *file);

G_END_DECLS
