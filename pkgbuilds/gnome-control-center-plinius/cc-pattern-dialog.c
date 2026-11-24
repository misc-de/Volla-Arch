/*
 * cc-pattern-dialog.c
 *
 * Plinius: set or remove the unlock pattern for the lock screen (plinius-pattern). Either PIN or
 * pattern: with a pattern, pam_plinius_pattern takes only the pattern on the lock screen (or a
 * fingerprint), not the PIN. First the current one is asked: the pattern if one is set, else the
 * PIN (also the way back when the pattern is forgotten, the PIN stays the account password).
 * Both are counted like tries on the lock screen: the PIN is checked by plinius-pinguard-check,
 * the pattern by plinius-pattern, which counts it with plinius-pinguard; while a wait runs after
 * too many wrong tries, nothing is checked. Without plinius-pinguard the PIN is checked with
 * unix_chkpwd, PAM's helper for exactly that, uncounted. Then the new pattern is drawn twice
 * on a grid of 4x4 to 6x6 points; plinius-pattern stores grid size and hash. The pattern pad is a
 * private GTK 4 twin of Phosh's PhoshPatternPad.
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-pattern-dialog"

#include "config.h"

#include <math.h>
#include <string.h>

#include <adwaita.h>
#include <glib.h>
#include <gtk/gtk.h>

#include "cc-pattern-dialog.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

#define PATTERN_TOOL       "plinius-pattern"
#define UNIX_CHKPWD        "/usr/bin/unix_chkpwd"
/* Exit status 0 right, 1 wrong, 2 a wait runs, 3 plinius-pinguard not there */
#define PINGUARD_CHECK     "/usr/lib/plinius-pinguard/plinius-pinguard-check"
/* plinius-pattern check: not checked, a wait runs */
#define PATTERN_WAIT       3
#define PATTERN_FILE       ".config/plinius/unlock-pattern"
/* As plinius-pattern.h: setting a pattern needs at least this many points */
#define PATTERN_MIN_POINTS 6
/* A wrong PIN or pattern blocks the next try for this long, as PAM does on the lock screen */
#define WRONG_PIN_DELAY_MS 2000

/* ----- The pattern pad ----- */

#define MIN_GRID      3
#define MAX_GRID      6
/* New patterns: 3x3 is too easy to guess and to watch over a shoulder. The pad still draws 3x3
 * for a pattern set with it before, to check it before it is changed. */
#define MIN_NEW_GRID  4
#define MAX_POINTS    (MAX_GRID * MAX_GRID)
/* As Phosh's lock screen (phosh-plinius 0012): the pad takes the screen's width less 24 px each
 * side, at most 55 % of its height, from 270 to 480 px; so it has the same size here as there.
 * Without a monitor yet, the pad's own sizes. */
#define PAD_SIZE      270
#define PAD_SIZE_LARGE 320
#define PAD_SIZE_MAX  480
#define PAD_SIDE_MARGIN 24
#define PAD_HEIGHT_SHARE 0.55
#define DOT_RADIUS    0.07
#define PICKED_RADIUS 0.15
#define HIT_RADIUS    0.32
#define LINE_WIDTH    0.045

#define CC_TYPE_PATTERN_PAD (cc_pattern_pad_get_type ())
G_DECLARE_FINAL_TYPE (CcPatternPad, cc_pattern_pad, CC, PATTERN_PAD, GtkWidget)

struct _CcPatternPad {
    GtkWidget parent_instance;

    double    start_x, start_y, finger_x, finger_y;
    gboolean  drawing;
    gboolean  error;
    /* Points per side, and the points picked so far (row by row from 0), in order */
    int       grid;
    int       points[MAX_POINTS];
    int       n_points;
};

G_DEFINE_FINAL_TYPE (CcPatternPad, cc_pattern_pad, GTK_TYPE_WIDGET)

static guint pad_entered_signal;

/* Size of a cell and the grid's top left corner, the grid centered in the widget */
static double
pad_geometry (CcPatternPad *self, double *x0, double *y0)
{
    int width = gtk_widget_get_width (GTK_WIDGET (self));
    int height = gtk_widget_get_height (GTK_WIDGET (self));
    double cell = MIN (width, height) / (double) self->grid;

    *x0 = (width - self->grid * cell) / 2;
    *y0 = (height - self->grid * cell) / 2;
    return cell;
}

static void
pad_point_center (CcPatternPad *self, int point, double *x, double *y)
{
    double x0, y0, cell = pad_geometry (self, &x0, &y0);

    *x = x0 + (point % self->grid + 0.5) * cell;
    *y = y0 + (point / self->grid + 0.5) * cell;
}

static gboolean
pad_is_picked (CcPatternPad *self, int point)
{
    for (int i = 0; i < self->n_points; i++) {
        if (self->points[i] == point)
            return TRUE;
    }
    return FALSE;
}

static void
pad_pick (CcPatternPad *self, int point)
{
    if (self->n_points < MAX_POINTS && !pad_is_picked (self, point))
        self->points[self->n_points++] = point;
}

static int
gcd (int a, int b)
{
    while (b) {
        int t = a % b;

        a = b;
        b = t;
    }
    return a;
}

/* The point under the finger, and the unused grid points the line jumps over */
static void
pad_pick_at (CcPatternPad *self, double x, double y)
{
    double x0, y0, cell = pad_geometry (self, &x0, &y0);

    for (int point = 0; point < self->grid * self->grid; point++) {
        double cx, cy;

        pad_point_center (self, point, &cx, &cy);
        if (hypot (x - cx, y - cy) > HIT_RADIUS * cell || pad_is_picked (self, point))
            continue;

        if (self->n_points > 0) {
            int last = self->points[self->n_points - 1];
            int dcol = point % self->grid - last % self->grid;
            int drow = point / self->grid - last / self->grid;
            int steps = gcd (abs (dcol), abs (drow));

            /* Grid points exactly on the line from the last point, in order */
            for (int i = 1; i < steps; i++)
                pad_pick (self, last + (drow / steps) * i * self->grid + (dcol / steps) * i);
        }
        pad_pick (self, point);
        return;
    }
}

static void
pad_clear (CcPatternPad *self)
{
    self->n_points = 0;
    self->error = FALSE;
    gtk_widget_queue_draw (GTK_WIDGET (self));
}

static void
pad_drag_begin (CcPatternPad *self, double x, double y, GtkGestureDrag *gesture)
{
    pad_clear (self);
    gtk_gesture_set_state (GTK_GESTURE (gesture), GTK_EVENT_SEQUENCE_CLAIMED);
    self->start_x = self->finger_x = x;
    self->start_y = self->finger_y = y;
    self->drawing = TRUE;
    pad_pick_at (self, x, y);
    gtk_widget_queue_draw (GTK_WIDGET (self));
}

static void
pad_drag_update (CcPatternPad *self, double offset_x, double offset_y, GtkGestureDrag *gesture)
{
    self->finger_x = self->start_x + offset_x;
    self->finger_y = self->start_y + offset_y;
    pad_pick_at (self, self->finger_x, self->finger_y);
    gtk_widget_queue_draw (GTK_WIDGET (self));
}

static void
pad_drag_end (CcPatternPad *self, double offset_x, double offset_y, GtkGestureDrag *gesture)
{
    g_autoptr(GString) pattern = g_string_new (NULL);

    self->drawing = FALSE;
    gtk_widget_queue_draw (GTK_WIDGET (self));

    if (self->n_points == 0)
        return;

    /* "grid:points", as plinius-pattern wants it */
    g_string_printf (pattern, "%d:", self->grid);
    for (int i = 0; i < self->n_points; i++)
        g_string_append_printf (pattern, i ? ".%d" : "%d", self->points[i]);
    g_signal_emit (self, pad_entered_signal, 0, pattern->str);
}

static void
cc_pattern_pad_snapshot (GtkWidget *widget, GtkSnapshot *snapshot)
{
    CcPatternPad *self = CC_PATTERN_PAD (widget);
    int width = gtk_widget_get_width (widget), height = gtk_widget_get_height (widget);
    double x0, y0, cell = pad_geometry (self, &x0, &y0);
    cairo_t *cr;
    GdkRGBA color;

    gtk_widget_get_color (widget, &color);
    if (self->error)
        gdk_rgba_parse (&color, "#e01b24");

    cr = gtk_snapshot_append_cairo (snapshot, &GRAPHENE_RECT_INIT (0, 0, width, height));

    /* The line through the picked points, and on to the finger while drawing */
    if (self->n_points > 0) {
        double x, y;

        cairo_set_source_rgba (cr, color.red, color.green, color.blue, color.alpha * 0.6);
        cairo_set_line_width (cr, LINE_WIDTH * cell);
        cairo_set_line_cap (cr, CAIRO_LINE_CAP_ROUND);
        cairo_set_line_join (cr, CAIRO_LINE_JOIN_ROUND);
        for (int i = 0; i < self->n_points; i++) {
            pad_point_center (self, self->points[i], &x, &y);
            if (i == 0)
                cairo_move_to (cr, x, y);
            else
                cairo_line_to (cr, x, y);
        }
        if (self->drawing)
            cairo_line_to (cr, self->finger_x, self->finger_y);
        cairo_stroke (cr);
    }

    for (int point = 0; point < self->grid * self->grid; point++) {
        double x, y;

        pad_point_center (self, point, &x, &y);
        cairo_set_source_rgba (cr, color.red, color.green, color.blue, color.alpha);
        cairo_arc (cr, x, y, DOT_RADIUS * cell, 0, 2 * G_PI);
        cairo_fill (cr);
        if (pad_is_picked (self, point)) {
            cairo_set_line_width (cr, LINE_WIDTH * cell * 0.6);
            cairo_arc (cr, x, y, PICKED_RADIUS * cell, 0, 2 * G_PI);
            cairo_stroke (cr);
        }
    }
    cairo_destroy (cr);
}

static int
pad_size (CcPatternPad *self)
{
    GtkNative *native = gtk_widget_get_native (GTK_WIDGET (self));
    GdkSurface *surface = native ? gtk_native_get_surface (native) : NULL;
    GdkMonitor *monitor = surface ? gdk_display_get_monitor_at_surface (gdk_surface_get_display (surface),
                                                                         surface) : NULL;
    GdkRectangle geometry;

    if (monitor == NULL)
        return self->grid > 4 ? PAD_SIZE_LARGE : PAD_SIZE;
    gdk_monitor_get_geometry (monitor, &geometry);
    return CLAMP (MIN (geometry.width - 2 * PAD_SIDE_MARGIN, (int) (geometry.height * PAD_HEIGHT_SHARE)),
                  PAD_SIZE, PAD_SIZE_MAX);
}

static void
cc_pattern_pad_measure (GtkWidget *widget, GtkOrientation orientation, int for_size,
                        int *minimum, int *natural, int *minimum_baseline, int *natural_baseline)
{
    *minimum = *natural = pad_size (CC_PATTERN_PAD (widget));
}

static void
cc_pattern_pad_class_init (CcPatternPadClass *klass)
{
    GtkWidgetClass *widget_class = GTK_WIDGET_CLASS (klass);

    widget_class->snapshot = cc_pattern_pad_snapshot;
    widget_class->measure = cc_pattern_pad_measure;

    /* The points in order as digits, e.g. "1235789", when the finger lifts */
    pad_entered_signal = g_signal_new ("entered", G_TYPE_FROM_CLASS (klass), G_SIGNAL_RUN_LAST,
                                       0, NULL, NULL, NULL, G_TYPE_NONE, 1, G_TYPE_STRING);
}

static void
cc_pattern_pad_init (CcPatternPad *self)
{
    GtkGesture *drag = gtk_gesture_drag_new ();

    self->grid = MIN_GRID;
    gtk_widget_set_halign (GTK_WIDGET (self), GTK_ALIGN_CENTER);
    gtk_widget_set_valign (GTK_WIDGET (self), GTK_ALIGN_CENTER);
    /* The monitor is known once the pad has a surface */
    g_signal_connect (self, "realize", G_CALLBACK (gtk_widget_queue_resize), NULL);
    g_signal_connect_swapped (drag, "drag-begin", G_CALLBACK (pad_drag_begin), self);
    g_signal_connect_swapped (drag, "drag-update", G_CALLBACK (pad_drag_update), self);
    g_signal_connect_swapped (drag, "drag-end", G_CALLBACK (pad_drag_end), self);
    gtk_widget_add_controller (GTK_WIDGET (self), GTK_EVENT_CONTROLLER (drag));
}

/* ----- The dialog ----- */

typedef enum {
    STEP_PIN,
    STEP_CURRENT,
    STEP_FIRST,
    STEP_CONFIRM,
} Step;

struct _CcPatternDialog {
    AdwDialog       parent_instance;

    ActUser        *user;
    Step            step;
    char           *first_pattern;
    GCancellable   *cancellable;

    GtkStack       *stack;
    GtkLabel       *pin_label;
    AdwEntryRow    *pin_row;
    GtkButton      *continue_button;
    GtkLabel       *draw_label;
    GtkWidget      *grid_group;
    CcPatternPad   *pad;
    GtkButton      *remove_button;
    GtkButton      *use_pin_button;
    guint           wrong_pin_id;
    guint           wrong_pattern_id;
    /* The PIN is being checked by PINGUARD_CHECK, not UNIX_CHKPWD */
    gboolean        pin_pinguard;
};

G_DEFINE_FINAL_TYPE (CcPatternDialog, cc_pattern_dialog, ADW_TYPE_DIALOG)

static void
set_draw_label (CcPatternDialog *self, const char *text, gboolean error)
{
    gtk_label_set_text (self->draw_label, text);
    if (error)
        gtk_widget_add_css_class (GTK_WIDGET (self->draw_label), "error");
    else
        gtk_widget_remove_css_class (GTK_WIDGET (self->draw_label), "error");
}

static void
go_to_first_step (CcPatternDialog *self)
{
    self->step = STEP_FIRST;
    g_clear_pointer (&self->first_pattern, g_free);
    pad_clear (self->pad);
    set_draw_label (self, P_("Draw an unlock pattern. Connect at least 6 points."), FALSE);
    /* A new pattern on the chosen grid, also after checking one on a grid no longer offered */
    self->pad->grid = adw_toggle_group_get_active_name (ADW_TOGGLE_GROUP (self->grid_group))[0] - '0';
    gtk_widget_queue_resize (GTK_WIDGET (self->pad));
    gtk_widget_set_visible (self->grid_group, TRUE);
    gtk_widget_set_visible (GTK_WIDGET (self->remove_button), cc_pattern_dialog_has_pattern ());
    gtk_widget_set_visible (GTK_WIDGET (self->use_pin_button), FALSE);
    gtk_stack_set_visible_child_name (self->stack, "draw");
}

/* With a pattern set: draw it first, on its own grid, before it can be changed or removed */
static void
go_to_current_step (CcPatternDialog *self)
{
    self->step = STEP_CURRENT;
    pad_clear (self->pad);
    set_draw_label (self, P_("Draw your current pattern."), FALSE);
    gtk_widget_set_visible (self->grid_group, FALSE);
    gtk_widget_set_visible (GTK_WIDGET (self->remove_button), FALSE);
    gtk_widget_set_visible (GTK_WIDGET (self->use_pin_button), TRUE);
    gtk_stack_set_visible_child_name (self->stack, "draw");
}

/* Forgot the pattern: the PIN (the account password) proves who it is as well */
static void
on_use_pin_clicked (CcPatternDialog *self)
{
    self->step = STEP_PIN;
    gtk_stack_set_visible_child_name (self->stack, "pin");
    adw_dialog_set_focus (ADW_DIALOG (self), GTK_WIDGET (self->pin_row));
}

static gboolean
on_wrong_pattern_timeout (gpointer user_data)
{
    CcPatternDialog *self = user_data;

    self->wrong_pattern_id = 0;
    pad_clear (self->pad);
    gtk_widget_set_sensitive (GTK_WIDGET (self->pad), TRUE);
    return G_SOURCE_REMOVE;
}

/* plinius-pattern with one command, the pattern (if any) on stdin; TRUE on success */
static gboolean
run_pattern_tool (const char *command, const char *pattern)
{
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;
    g_autofree char *input = pattern ? g_strconcat (pattern, "\n", NULL) : NULL;

    process = g_subprocess_new (G_SUBPROCESS_FLAGS_STDIN_PIPE, &error, PATTERN_TOOL, command, NULL);
    if (process == NULL || !g_subprocess_communicate_utf8 (process, input, NULL, NULL, NULL, &error) ||
        !g_subprocess_get_successful (process)) {
        g_warning ("%s %s failed: %s", PATTERN_TOOL, command,
                   error ? error->message : "exit status");
        return FALSE;
    }
    return TRUE;
}

static int
count_points (const char *pattern)
{
    int n = 1;

    for (const char *p = pattern; *p; p++)
        n += *p == '.';
    return n;
}

/* Another grid size starts over */
static void
on_grid_changed (CcPatternDialog *self, GParamSpec *pspec, AdwToggleGroup *group)
{
    const char *name = adw_toggle_group_get_active_name (group);

    self->pad->grid = CLAMP (name ? name[0] - '0' : MIN_NEW_GRID, MIN_NEW_GRID, MAX_GRID);
    gtk_widget_queue_resize (GTK_WIDGET (self->pad));
    go_to_first_step (self);
}

static void
on_pattern_checked (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(CcPatternDialog) self = user_data;
    g_autoptr(GError) error = NULL;
    GSubprocess *process = G_SUBPROCESS (source);
    int status = -1;

    if (!g_subprocess_communicate_utf8_finish (process, result, NULL, NULL, &error)) {
        if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
            return;
        g_warning ("%s check failed: %s", PATTERN_TOOL, error->message);
    } else if (g_subprocess_get_if_exited (process)) {
        status = g_subprocess_get_exit_status (process);
    }

    if (status == 0) {
        gtk_widget_set_sensitive (GTK_WIDGET (self->pad), TRUE);
        go_to_first_step (self);
        return;
    }

    self->pad->error = TRUE;
    gtk_widget_queue_draw (GTK_WIDGET (self->pad));
    set_draw_label (self, status == PATTERN_WAIT ? P_("Too many wrong tries. Try again later.")
                                                 : P_("Wrong pattern. Try again."), TRUE);
    self->wrong_pattern_id = g_timeout_add (WRONG_PIN_DELAY_MS, on_wrong_pattern_timeout, self);
}

/* The current pattern, without blocking: plinius-pattern hashes it and asks plinius-pinguard */
static void
check_pattern (CcPatternDialog *self, const char *pattern)
{
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;
    g_autofree char *input = g_strconcat (pattern, "\n", NULL);

    gtk_widget_set_sensitive (GTK_WIDGET (self->pad), FALSE);
    process = g_subprocess_new (G_SUBPROCESS_FLAGS_STDIN_PIPE | G_SUBPROCESS_FLAGS_STDOUT_SILENCE,
                                &error, PATTERN_TOOL, "check", NULL);
    if (process == NULL) {
        g_warning ("Running %s failed: %s", PATTERN_TOOL, error->message);
        gtk_widget_set_sensitive (GTK_WIDGET (self->pad), TRUE);
        pad_clear (self->pad);
        return;
    }
    g_subprocess_communicate_utf8_async (process, input, self->cancellable, on_pattern_checked,
                                         g_object_ref (self));
}

static void
on_pattern_entered (CcPatternDialog *self, const char *pattern, CcPatternPad *pad)
{
    if (self->step == STEP_CURRENT) {
        /* A single point is a tap, not a pattern */
        if (strchr (pattern, '.') == NULL) {
            pad_clear (pad);
            return;
        }
        check_pattern (self, pattern);
        return;
    }

    /* Points are separated by dots */
    if (count_points (pattern) < PATTERN_MIN_POINTS) {
        pad->error = TRUE;
        gtk_widget_queue_draw (GTK_WIDGET (pad));
        set_draw_label (self, P_("Connect at least 6 points."), TRUE);
        return;
    }

    if (self->step == STEP_FIRST) {
        self->first_pattern = g_strdup (pattern);
        self->step = STEP_CONFIRM;
        pad_clear (pad);
        set_draw_label (self, P_("Draw the pattern again to confirm."), FALSE);
        return;
    }

    if (g_strcmp0 (pattern, self->first_pattern) != 0) {
        go_to_first_step (self);
        set_draw_label (self, P_("The patterns do not match. Draw a new one."), TRUE);
        return;
    }

    if (!run_pattern_tool ("set", pattern)) {
        set_draw_label (self, P_("The pattern could not be saved."), TRUE);
        return;
    }
    adw_dialog_close (ADW_DIALOG (self));
}

static void
on_remove_clicked (CcPatternDialog *self)
{
    if (run_pattern_tool ("remove", NULL))
        adw_dialog_close (ADW_DIALOG (self));
    else
        set_draw_label (self, P_("The pattern could not be removed."), TRUE);
}

static gboolean
on_wrong_pin_timeout (gpointer user_data)
{
    CcPatternDialog *self = user_data;

    self->wrong_pin_id = 0;
    gtk_widget_set_sensitive (GTK_WIDGET (self->pin_row), TRUE);
    gtk_widget_set_sensitive (GTK_WIDGET (self->continue_button), TRUE);
    gtk_widget_grab_focus (GTK_WIDGET (self->pin_row));
    return G_SOURCE_REMOVE;
}

static void check_pin (CcPatternDialog *self, gboolean pinguard);

static void
on_pin_checked (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(CcPatternDialog) self = user_data;
    g_autoptr(GError) error = NULL;
    GSubprocess *process = G_SUBPROCESS (source);
    gboolean pinguard = self->pin_pinguard;
    int status = -1;

    if (!g_subprocess_communicate_finish (process, result, NULL, NULL, &error)) {
        if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
            return;
        g_warning ("Checking the PIN failed: %s", error->message);
    } else if (g_subprocess_get_if_exited (process)) {
        status = g_subprocess_get_exit_status (process);
    }

    if (status == 0) {
        gtk_editable_set_text (GTK_EDITABLE (self->pin_row), "");
        go_to_first_step (self);
        return;
    }

    /* plinius-pinguard is not running: as before, uncounted */
    if (pinguard && status != 1 && status != 2) {
        check_pin (self, FALSE);
        return;
    }

    gtk_label_set_text (self->pin_label, pinguard && status == 2 ? P_("Too many wrong tries. Try again later.")
                                                                 : P_("Wrong PIN. Try again."));
    gtk_widget_add_css_class (GTK_WIDGET (self->pin_label), "error");
    gtk_editable_set_text (GTK_EDITABLE (self->pin_row), "");
    self->wrong_pin_id = g_timeout_add (WRONG_PIN_DELAY_MS, on_wrong_pin_timeout, self);
}

/*
 * Both checkers check the caller's own password, read from stdin up to a NUL byte, as pam_unix
 * sends it to unix_chkpwd: without one, unix_chkpwd reports "no password supplied" and every PIN
 * would be wrong. Exit status 0 if it is right.
 */
static void
check_pin (CcPatternDialog *self, gboolean pinguard)
{
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;
    g_autoptr(GBytes) input = NULL;
    const char *pin = gtk_editable_get_text (GTK_EDITABLE (self->pin_row));

    self->pin_pinguard = pinguard;
    if (pinguard)
        process = g_subprocess_new (G_SUBPROCESS_FLAGS_STDIN_PIPE | G_SUBPROCESS_FLAGS_STDOUT_SILENCE |
                                    G_SUBPROCESS_FLAGS_STDERR_SILENCE, &error, PINGUARD_CHECK, NULL);
    else
        process = g_subprocess_new (G_SUBPROCESS_FLAGS_STDIN_PIPE | G_SUBPROCESS_FLAGS_STDERR_SILENCE,
                                    &error, UNIX_CHKPWD, act_user_get_user_name (self->user), "nullok", NULL);
    if (process == NULL) {
        if (pinguard) {
            check_pin (self, FALSE);
            return;
        }
        g_warning ("Running %s failed: %s", UNIX_CHKPWD, error->message);
        on_wrong_pin_timeout (self);
        return;
    }
    /* The PIN with its terminating NUL */
    input = g_bytes_new (pin, strlen (pin) + 1);
    g_subprocess_communicate_async (process, input, self->cancellable, on_pin_checked,
                                    g_object_ref (self));
}

static void
on_continue_clicked (CcPatternDialog *self)
{
    const char *pin = gtk_editable_get_text (GTK_EDITABLE (self->pin_row));

    if (pin[0] == '\0' || self->wrong_pin_id != 0)
        return;

    gtk_widget_set_sensitive (GTK_WIDGET (self->pin_row), FALSE);
    gtk_widget_set_sensitive (GTK_WIDGET (self->continue_button), FALSE);
    check_pin (self, g_file_test (PINGUARD_CHECK, G_FILE_TEST_IS_EXECUTABLE));
}

static void
cc_pattern_dialog_dispose (GObject *object)
{
    CcPatternDialog *self = CC_PATTERN_DIALOG (object);

    g_cancellable_cancel (self->cancellable);
    g_clear_object (&self->cancellable);
    g_clear_handle_id (&self->wrong_pin_id, g_source_remove);
    g_clear_handle_id (&self->wrong_pattern_id, g_source_remove);
    g_clear_object (&self->user);
    g_clear_pointer (&self->first_pattern, g_free);

    G_OBJECT_CLASS (cc_pattern_dialog_parent_class)->dispose (object);
}

static void
cc_pattern_dialog_class_init (CcPatternDialogClass *klass)
{
    G_OBJECT_CLASS (klass)->dispose = cc_pattern_dialog_dispose;
}

static GtkWidget *
new_page_box (void)
{
    GtkWidget *box = gtk_box_new (GTK_ORIENTATION_VERTICAL, 18);

    gtk_widget_set_margin_top (box, 12);
    gtk_widget_set_margin_bottom (box, 24);
    gtk_widget_set_margin_start (box, 24);
    gtk_widget_set_margin_end (box, 24);
    return box;
}

static void
cc_pattern_dialog_init (CcPatternDialog *self)
{
    GtkWidget *toolbar = adw_toolbar_view_new ();
    GtkWidget *box, *list, *label, *grid_group, *page, *bottom;

    self->cancellable = g_cancellable_new ();
    self->step = STEP_PIN;

    adw_dialog_set_title (ADW_DIALOG (self), P_("Unlock Pattern"));
    adw_dialog_set_content_width (ADW_DIALOG (self), 360);
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    self->stack = GTK_STACK (gtk_stack_new ());
    gtk_stack_set_transition_type (self->stack, GTK_STACK_TRANSITION_TYPE_SLIDE_LEFT);
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), GTK_WIDGET (self->stack));
    adw_dialog_set_child (ADW_DIALOG (self), toolbar);

    /* Step 1: the PIN (or the current pattern, below), so that nobody with an unlocked phone in
     * hand sets a pattern of their own */
    box = new_page_box ();
    label = gtk_label_new (P_("With an unlock pattern, the lock screen takes the pattern or a "
                              "fingerprint instead of the PIN. Enter your PIN to continue."));
    gtk_label_set_wrap (GTK_LABEL (label), TRUE);
    gtk_label_set_justify (GTK_LABEL (label), GTK_JUSTIFY_CENTER);
    gtk_box_append (GTK_BOX (box), label);
    self->pin_label = GTK_LABEL (gtk_label_new (""));
    gtk_label_set_wrap (self->pin_label, TRUE);
    gtk_box_append (GTK_BOX (box), GTK_WIDGET (self->pin_label));
    list = gtk_list_box_new ();
    gtk_list_box_set_selection_mode (GTK_LIST_BOX (list), GTK_SELECTION_NONE);
    gtk_widget_add_css_class (list, "boxed-list");
    self->pin_row = ADW_ENTRY_ROW (adw_password_entry_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->pin_row), P_("PIN"));
    gtk_list_box_append (GTK_LIST_BOX (list), GTK_WIDGET (self->pin_row));
    gtk_box_append (GTK_BOX (box), list);
    self->continue_button = GTK_BUTTON (gtk_button_new_with_label (P_("Continue")));
    gtk_widget_set_halign (GTK_WIDGET (self->continue_button), GTK_ALIGN_CENTER);
    gtk_widget_add_css_class (GTK_WIDGET (self->continue_button), "pill");
    gtk_widget_add_css_class (GTK_WIDGET (self->continue_button), "suggested-action");
    gtk_box_append (GTK_BOX (box), GTK_WIDGET (self->continue_button));
    g_signal_connect_swapped (self->continue_button, "clicked", G_CALLBACK (on_continue_clicked), self);
    g_signal_connect_swapped (self->pin_row, "entry-activated", G_CALLBACK (on_continue_clicked), self);
    gtk_stack_add_named (self->stack, box, "pin");

    /*
     * Steps 2 and 3: draw, draw again; with a pattern set, drawing the current one comes first.
     * The pad sits in the middle of the page, text and grid size above, buttons below: on the
     * phone that is where the lock screen shows it (its unlock page is centered on the screen),
     * so the hand draws the pattern at the same place.
     */
    page = gtk_center_box_new ();
    gtk_orientable_set_orientation (GTK_ORIENTABLE (page), GTK_ORIENTATION_VERTICAL);
    gtk_widget_set_vexpand (page, TRUE);
    gtk_widget_set_margin_top (page, 12);
    gtk_widget_set_margin_bottom (page, 24);
    gtk_widget_set_margin_start (page, 24);
    gtk_widget_set_margin_end (page, 24);
    box = gtk_box_new (GTK_ORIENTATION_VERTICAL, 18);
    gtk_widget_set_margin_bottom (box, 18);
    gtk_center_box_set_start_widget (GTK_CENTER_BOX (page), box);
    self->draw_label = GTK_LABEL (gtk_label_new (""));
    gtk_label_set_wrap (self->draw_label, TRUE);
    gtk_label_set_justify (self->draw_label, GTK_JUSTIFY_CENTER);
    gtk_box_append (GTK_BOX (box), GTK_WIDGET (self->draw_label));
    /* Grid size: more points make a pattern harder to guess and to watch over a shoulder */
    grid_group = self->grid_group = adw_toggle_group_new ();
    gtk_widget_set_halign (grid_group, GTK_ALIGN_CENTER);
    for (int grid = MIN_NEW_GRID; grid <= MAX_GRID; grid++) {
        AdwToggle *toggle = adw_toggle_new ();
        g_autofree char *name = g_strdup_printf ("%d", grid);
        g_autofree char *label = g_strdup_printf ("%d×%d", grid, grid);

        adw_toggle_set_name (toggle, name);
        adw_toggle_set_label (toggle, label);
        adw_toggle_group_add (ADW_TOGGLE_GROUP (grid_group), toggle);
    }
    adw_toggle_group_set_active_name (ADW_TOGGLE_GROUP (grid_group), "4");
    gtk_box_append (GTK_BOX (box), grid_group);
    self->pad = g_object_new (CC_TYPE_PATTERN_PAD, NULL);
    self->pad->grid = MIN_NEW_GRID;
    /* The grid of a pattern set before (its file starts with the grid size): the pad draws it to
     * check the current pattern, the toggle preselects it for a new one if still offered */
    {
        g_autofree char *path = g_build_filename (g_get_home_dir (), PATTERN_FILE, NULL);
        g_autofree char *contents = NULL;

        if (g_file_get_contents (path, &contents, NULL, NULL) &&
            contents[0] >= '0' + MIN_GRID && contents[0] <= '0' + MAX_GRID && contents[1] == '\n') {
            char name[2] = { contents[0], '\0' };

            if (contents[0] >= '0' + MIN_NEW_GRID)
                adw_toggle_group_set_active_name (ADW_TOGGLE_GROUP (grid_group), name);
            self->pad->grid = contents[0] - '0';
        }
    }
    gtk_center_box_set_center_widget (GTK_CENTER_BOX (page), GTK_WIDGET (self->pad));
    g_signal_connect_swapped (self->pad, "entered", G_CALLBACK (on_pattern_entered), self);
    g_signal_connect_object (grid_group, "notify::active-name", G_CALLBACK (on_grid_changed), self,
                             G_CONNECT_SWAPPED);
    self->remove_button = GTK_BUTTON (gtk_button_new_with_label (P_("Remove Pattern")));
    gtk_widget_set_halign (GTK_WIDGET (self->remove_button), GTK_ALIGN_CENTER);
    gtk_widget_add_css_class (GTK_WIDGET (self->remove_button), "pill");
    gtk_widget_add_css_class (GTK_WIDGET (self->remove_button), "destructive-action");
    bottom = gtk_box_new (GTK_ORIENTATION_VERTICAL, 18);
    gtk_widget_set_margin_top (bottom, 18);
    gtk_widget_set_valign (bottom, GTK_ALIGN_END);
    gtk_center_box_set_end_widget (GTK_CENTER_BOX (page), bottom);
    gtk_box_append (GTK_BOX (bottom), GTK_WIDGET (self->remove_button));
    g_signal_connect_swapped (self->remove_button, "clicked", G_CALLBACK (on_remove_clicked), self);
    self->use_pin_button = GTK_BUTTON (gtk_button_new_with_label (P_("Use PIN Instead")));
    gtk_widget_set_halign (GTK_WIDGET (self->use_pin_button), GTK_ALIGN_CENTER);
    gtk_widget_add_css_class (GTK_WIDGET (self->use_pin_button), "flat");
    gtk_box_append (GTK_BOX (bottom), GTK_WIDGET (self->use_pin_button));
    g_signal_connect_swapped (self->use_pin_button, "clicked", G_CALLBACK (on_use_pin_clicked), self);
    gtk_stack_add_named (self->stack, page, "draw");

    if (cc_pattern_dialog_has_pattern ()) {
        go_to_current_step (self);
    } else {
        gtk_stack_set_visible_child_name (self->stack, "pin");
        adw_dialog_set_focus (ADW_DIALOG (self), GTK_WIDGET (self->pin_row));
    }
}

CcPatternDialog *
cc_pattern_dialog_new (ActUser *user)
{
    CcPatternDialog *self = g_object_new (CC_TYPE_PATTERN_DIALOG, NULL);

    self->user = g_object_ref (user);
    return self;
}

/* plinius-pattern is installed (and with it the PAM module the lock screen uses) */
gboolean
cc_pattern_dialog_is_available (void)
{
    g_autofree char *path = g_find_program_in_path (PATTERN_TOOL);

    return path != NULL;
}

/* The current user has set a pattern */
gboolean
cc_pattern_dialog_has_pattern (void)
{
    g_autofree char *path = g_build_filename (g_get_home_dir (), PATTERN_FILE, NULL);

    return g_file_test (path, G_FILE_TEST_IS_REGULAR);
}
