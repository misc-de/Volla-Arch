/*
 * cc-plinius-text-focus.c
 *
 * Plinius: focus moves that nobody asked for stay out of text fields. On a phone a text field
 * with the focus means the on-screen keyboard, which should only open for a field the user
 * tapped.
 *
 * Settings moves the focus on its own when a page opens: libadwaita onto the page's first row,
 * then CcWindow one step further for screen readers. With a spin row second that step ends in
 * the row's text field. The compositor keeps the keyboard closed unless a press came just
 * before, at the height of the field, and the tap that opened the page can be at that height.
 *
 * All those moves end in the "focus" virtual function of GtkText (gtk_widget_child_focus()), so
 * this file replaces it for the whole program, with no change to any page:
 * - A tap is no such move: it calls gtk_widget_grab_focus(), as do the rows around a text field.
 * - Tab and the arrow keys of a real keyboard are. They emit "move-focus", which lets the
 *   focus in again until the emission is over.
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-text-focus"

#include "cc-plinius-text-focus.h"

static gboolean (*real_text_focus) (GtkWidget *widget, GtkDirectionType direction);
static gboolean key_move;

static gboolean
key_move_over_cb (gpointer user_data)
{
    key_move = FALSE;
    return G_SOURCE_REMOVE;
}

/* Runs before the class handler of "move-focus", which moves the focus right away */
static gboolean
move_focus_hook (GSignalInvocationHint *hint, guint n_values, const GValue *values, gpointer user_data)
{
    if (!key_move) {
        key_move = TRUE;
        g_idle_add_full (G_PRIORITY_HIGH, key_move_over_cb, NULL, NULL);
    }
    return TRUE;
}

static gboolean
text_focus (GtkWidget *widget, GtkDirectionType direction)
{
    return key_move ? real_text_focus (widget, direction) : FALSE;
}

void
cc_plinius_text_focus_init (void)
{
    /* The class stays referenced: its table of functions is what gets changed */
    GtkWidgetClass *klass = g_type_class_ref (GTK_TYPE_TEXT);

    if (real_text_focus)
        return;

    real_text_focus = klass->focus;
    klass->focus = text_focus;
    g_signal_add_emission_hook (g_signal_lookup ("move-focus", GTK_TYPE_WIDGET), 0, move_focus_hook, NULL, NULL);
}
