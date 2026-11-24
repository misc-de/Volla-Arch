/*
 * cc-plinius-dialog-pages.c
 *
 * Plinius: the form dialogs of Settings open as pages of the window's navigation instead of
 * sheets over the window. A sheet on a phone covers the page it belongs to, leaves little room
 * for the on-screen keyboard and cannot be left with the back swipe.
 *
 * This file replaces adw_dialog_present(), adw_dialog_close() and adw_dialog_force_close() for
 * the whole program: the executable defines them, so its own calls (panels are linked in
 * statically) and GtkBuilder callbacks such as "clicked => $adw_dialog_force_close(template)"
 * end up here. libadwaita's functions are reached through dlsym (RTLD_NEXT).
 *
 * A dialog presented for a widget inside the window's navigation view becomes a page:
 * - The dialog keeps existing, unparented, while its child (usually an AdwToolbarView with a
 *   header bar) moves into a new AdwNavigationPage, which is pushed. An AdwDialog itself cannot
 *   go into a page: libadwaita aborts when it is rooted anywhere but in a dialog host or its
 *   own window. Signals and template children stay as they are, they hang off the child.
 * - The page takes the dialog's title, its can-close as can-pop, and on showing the dialog's
 *   default and focus widget. A "window.close" action on the page closes the dialog, not
 *   Settings (CcInputChooser's Cancel button uses that action).
 * - A navigation of the dialog's own (eSIM) gives up its back swipe on its first page, so that
 *   the back swipe closes the dialog's page there.
 * - adw_dialog_close() and adw_dialog_force_close() pop the page; back button and back swipe pop
 *   it as well. Once popped the dialog emits "closed", as it would after a sheet, and gets its
 *   child back when the page is gone, so the dialog is freed as before.
 * - A header bar that brings its own start buttons (mostly "Cancel") shows no back button.
 *
 * cc_plinius_gtk_dialog_present_as_page() does the same for a GtkDialog (libnma's Wi-Fi dialogs,
 * the avatar crop): its content goes into a page, its confirming button to the page's header
 * bar; back and the back swipe answer it with "Cancel".
 *
 * Stay dialogs: libadwaita's own (alerts, about, preferences, shortcuts), dialogs without a
 * child, and dialogs presented for a widget outside the navigation view (e.g. the panel list).
 * The fingerprint dialog is a page as well; it keeps the room for the sensor under the display
 * at its bottom itself.
 * A dialog presented for one of our page dialogs (an alert asking "Discard changes?") is
 * presented for its page instead, since the dialog itself is in no window.
 * PLINIUS_DIALOG_PAGES=0 in the environment turns all of this off.
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-dialog-pages"

#include <dlfcn.h>

#include <adwaita.h>

#include "cc-window.h"

typedef struct {
    AdwDialog *dialog;             /* owned until the page is gone */
    GtkWidget *child;              /* the dialog's child, owned while the page holds it */
    AdwNavigationView *navigation; /* weak */
    AdwNavigationPage *page;       /* weak */
    gulong popped_id;
    gboolean closed;
} DialogPage;

G_DEFINE_QUARK (cc-plinius-dialog-page, dialog_page)

static void (*real_present) (AdwDialog *dialog, GtkWidget *parent);
static gboolean (*real_close) (AdwDialog *dialog);
static void (*real_force_close) (AdwDialog *dialog);
static gboolean enabled;

static void
resolve_real_functions (void)
{
    const char *env;

    if (real_present)
        return;

    real_present = dlsym (RTLD_NEXT, "adw_dialog_present");
    real_close = dlsym (RTLD_NEXT, "adw_dialog_close");
    real_force_close = dlsym (RTLD_NEXT, "adw_dialog_force_close");
    /* Without libadwaita's functions there is nothing to fall back on */
    if (!real_present || !real_close || !real_force_close)
        g_error ("libadwaita's dialog functions not found: %s", dlerror ());

    env = g_getenv ("PLINIUS_DIALOG_PAGES");
    enabled = env == NULL || g_strcmp0 (env, "0") != 0;
}

static DialogPage *
get_dialog_page (AdwDialog *dialog)
{
    return g_object_get_qdata (G_OBJECT (dialog), dialog_page_quark ());
}

/* Dialogs that should stay dialogs */
static gboolean
wants_page (AdwDialog *dialog)
{
    const char *name = G_OBJECT_TYPE_NAME (dialog);

    /* libadwaita's own: AdwAlertDialog, AdwAboutDialog, AdwPreferencesDialog, ...; a plain
     * AdwDialog from adw_dialog_new() (the encryption password) is a form like ours */
    if (g_str_has_prefix (name, "Adw") && !g_str_equal (name, "AdwDialog"))
        return FALSE;

    return adw_dialog_get_child (dialog) != NULL;
}

/* The window's navigation view, if @parent is a widget shown inside it */
static AdwNavigationView *
find_navigation (GtkWidget *parent)
{
    AdwNavigationView *navigation;
    GtkRoot *root;

    if (!parent)
        return NULL;

    root = gtk_widget_get_root (parent);
    if (!CC_IS_WINDOW (root))
        return NULL;

    navigation = cc_window_get_navigation_view (CC_WINDOW (root));
    if (!navigation)
        return NULL;
    /* Presented for the window (the modem details, the app shortcut dialog): a page as well */
    if (parent != GTK_WIDGET (root) && !gtk_widget_is_ancestor (parent, GTK_WIDGET (navigation)))
        return NULL;

    return navigation;
}

/* A page dialog is in no window: what is presented for it is presented for its page */
static GtkWidget *
substitute_parent (GtkWidget *parent)
{
    DialogPage *dp;

    if (!ADW_IS_DIALOG (parent))
        return parent;

    dp = get_dialog_page (ADW_DIALOG (parent));
    if (dp && dp->page)
        return GTK_WIDGET (dp->page);

    return parent;
}

/* The first header bar in @widget's tree, depth first */
static AdwHeaderBar *
find_header_bar (GtkWidget *widget)
{
    GtkWidget *child;

    if (ADW_IS_HEADER_BAR (widget))
        return ADW_HEADER_BAR (widget);

    for (child = gtk_widget_get_first_child (widget); child; child = gtk_widget_get_next_sibling (child)) {
        AdwHeaderBar *bar = find_header_bar (child);

        if (bar)
            return bar;
    }

    return NULL;
}

/* The box of the header bar's start widgets (libadwaita gives it the style class "start") */
static GtkWidget *
find_start_box (GtkWidget *widget)
{
    GtkWidget *child;

    if (GTK_IS_BOX (widget) && gtk_widget_has_css_class (widget, "start"))
        return widget;

    for (child = gtk_widget_get_first_child (widget); child; child = gtk_widget_get_next_sibling (child)) {
        GtkWidget *box = find_start_box (child);

        if (box)
            return box;
    }

    return NULL;
}

/* Whether the header bar shows start widgets of its own, besides back button and window controls */
static gboolean
has_own_start_widgets (AdwHeaderBar *bar)
{
    GtkWidget *box = find_start_box (GTK_WIDGET (bar));
    GtkWidget *child;

    if (!box)
        return FALSE;

    for (child = gtk_widget_get_first_child (box); child; child = gtk_widget_get_next_sibling (child)) {
        if (!gtk_widget_get_visible (child))
            continue;
        if (GTK_IS_WINDOW_CONTROLS (child) || g_str_equal (G_OBJECT_TYPE_NAME (child), "AdwBackButton"))
            continue;
        return TRUE;
    }

    return FALSE;
}

static void
page_showing_cb (AdwNavigationPage *page, DialogPage *dp)
{
    AdwHeaderBar *bar = find_header_bar (dp->child);

    /* "Cancel" already leads back; a back button next to it would say the same */
    if (bar && has_own_start_widgets (bar))
        adw_header_bar_set_show_back_button (bar, FALSE);
}

static void
page_shown_cb (AdwNavigationPage *page, DialogPage *dp)
{
    GtkRoot *root = gtk_widget_get_root (GTK_WIDGET (page));
    GtkWidget *focus = adw_dialog_get_focus (dp->dialog);

    /* Enter in an entry activates the dialog's default widget, as in the dialog */
    if (GTK_IS_WINDOW (root))
        gtk_window_set_default_widget (GTK_WINDOW (root), adw_dialog_get_default_widget (dp->dialog));
    /* Not a text field: the on-screen keyboard is for fields the user tapped (see
     * cc-plinius-text-focus.c, which does not see this call) */
    if (focus && !GTK_IS_EDITABLE (focus))
        gtk_widget_grab_focus (focus);
}

static void
page_hiding_cb (AdwNavigationPage *page, DialogPage *dp)
{
    GtkRoot *root = gtk_widget_get_root (GTK_WIDGET (page));

    if (GTK_IS_WINDOW (root) &&
        gtk_window_get_default_widget (GTK_WINDOW (root)) == adw_dialog_get_default_widget (dp->dialog))
        gtk_window_set_default_widget (GTK_WINDOW (root), NULL);
}

/* The page is off the stack: the dialog counts as closed */
static void
finish (DialogPage *dp)
{
    if (dp->closed)
        return;

    dp->closed = TRUE;
    if (dp->navigation && dp->popped_id)
        g_signal_handler_disconnect (dp->navigation, dp->popped_id);
    dp->popped_id = 0;

    /* The window goes away with the page in it: a dialog host says nothing then either.
     * gtk_widget_in_destruction () holds only while GTK emits "destroy", not in the navigation
     * view's own dispose, where it lets go of its pages: by then it is out of the window */
    if (!dp->navigation || gtk_widget_in_destruction (GTK_WIDGET (dp->navigation)) ||
        !gtk_widget_get_root (GTK_WIDGET (dp->navigation)))
        return;

    g_signal_emit_by_name (dp->dialog, "closed");
}

static void
navigation_popped_cb (AdwNavigationView *navigation, AdwNavigationPage *page, DialogPage *dp)
{
    if (page == dp->page)
        finish (dp);
}

/* The page is gone (popped, or dropped when another panel replaced the stack) */
static void
page_gone_cb (gpointer data, GObject *where_the_page_was)
{
    DialogPage *dp = data;

    dp->page = NULL;
    finish (dp);

    /* The page let go of the child in its dispose; give it back to the dialog to free it */
    if (gtk_widget_get_parent (dp->child) == NULL)
        adw_dialog_set_child (dp->dialog, dp->child);
    g_clear_object (&dp->child);

    g_clear_weak_pointer (&dp->navigation);
    g_object_set_qdata (G_OBJECT (dp->dialog), dialog_page_quark (), NULL);
    g_clear_object (&dp->dialog);
    g_free (dp);
}

/* Takes the page off the stack, with any page shown above it */
static void
remove_page (DialogPage *dp)
{
    AdwNavigationPage *previous;

    if (dp->closed || !dp->page || !dp->navigation)
        return;

    /* Closing is decided here; the page must not refuse to go */
    adw_navigation_page_set_can_pop (dp->page, TRUE);

    previous = adw_navigation_view_get_previous_page (dp->navigation, dp->page);
    if (adw_navigation_view_get_visible_page (dp->navigation) == dp->page)
        adw_navigation_view_pop (dp->navigation);
    else if (previous)
        adw_navigation_view_pop_to_page (dp->navigation, previous);
    else
        finish (dp);
}

static void
close_action_cb (GSimpleAction *action, GVariant *parameter, gpointer data)
{
    adw_dialog_close (ADW_DIALOG (data));
}

/* A navigation of the dialog's own inside its child (the eSIM dialog: profile and add pages) */
static AdwNavigationView *
find_inner_navigation (GtkWidget *widget)
{
    if (ADW_IS_NAVIGATION_VIEW (widget))
        return ADW_NAVIGATION_VIEW (widget);

    for (GtkWidget *child = gtk_widget_get_first_child (widget); child; child = gtk_widget_get_next_sibling (child)) {
        AdwNavigationView *found = find_inner_navigation (child);

        if (found)
            return found;
    }
    return NULL;
}

/*
 * The back swipe of a nested navigation took the gesture even on its first page, where it has
 * nothing to go back to, so the page of the dialog could not be swiped away. On its first page
 * its swipe gestures are out of the event flow (as libadwaita does for a disabled swipe tracker)
 * and the window's navigation closes the dialog's page; on its own subpages they are back.
 *
 * Off only once the first page is shown: the visible page changes while a back swipe is still
 * running, and taking the gestures away then left the navigation half way, a black screen.
 */
static gboolean
inner_navigation_on_first (AdwNavigationView *inner)
{
    AdwNavigationPage *visible = adw_navigation_view_get_visible_page (inner);

    return !visible || !adw_navigation_view_get_previous_page (inner, visible);
}

static void
inner_navigation_swipe (AdwNavigationView *inner, gboolean on)
{
    g_autoptr(GListModel) controllers = gtk_widget_observe_controllers (GTK_WIDGET (inner));

    for (guint i = 0; i < g_list_model_get_n_items (controllers); i++) {
        g_autoptr(GtkEventController) controller = g_list_model_get_item (controllers, i);
        int phase;

        if (!GTK_IS_GESTURE (controller))
            continue;

        /* The phase libadwaita gave it, kept the first time (+1, so that 0 means unknown) */
        phase = GPOINTER_TO_INT (g_object_get_data (G_OBJECT (controller), "plinius-phase"));
        if (!phase) {
            phase = gtk_event_controller_get_propagation_phase (controller) + 1;
            g_object_set_data (G_OBJECT (controller), "plinius-phase", GINT_TO_POINTER (phase));
        }
        gtk_event_controller_set_propagation_phase (controller, on ? phase - 1 : GTK_PHASE_NONE);
    }
}

/* To a subpage: the navigation's own swipe at once */
static void
inner_navigation_changed_cb (AdwNavigationView *inner, GParamSpec *pspec, gpointer data)
{
    if (!inner_navigation_on_first (inner))
        inner_navigation_swipe (inner, TRUE);
}

/* Back on the first page, the transition done: the window's swipe */
static void
inner_first_page_shown_cb (AdwNavigationPage *page, AdwNavigationView *inner)
{
    if (inner_navigation_on_first (inner))
        inner_navigation_swipe (inner, FALSE);
}

static void
present_as_page (AdwDialog *dialog, AdwNavigationView *navigation)
{
    AdwNavigationView *inner;
    g_autoptr(GSimpleActionGroup) actions = g_simple_action_group_new ();
    g_autoptr(GSimpleAction) close_action = g_simple_action_new ("close", NULL);
    DialogPage *dp = g_new0 (DialogPage, 1);

    dp->dialog = g_object_ref_sink (dialog);
    dp->child = g_object_ref (adw_dialog_get_child (dialog));
    adw_dialog_set_child (dialog, NULL);
    g_set_weak_pointer (&dp->navigation, navigation);

    dp->page = adw_navigation_page_new (dp->child, adw_dialog_get_title (dialog));
    g_object_weak_ref (G_OBJECT (dp->page), page_gone_cb, dp);
    g_object_bind_property (dialog, "title", dp->page, "title", G_BINDING_DEFAULT);
    g_object_bind_property (dialog, "can-close", dp->page, "can-pop", G_BINDING_SYNC_CREATE);

    g_signal_connect_object (close_action, "activate", G_CALLBACK (close_action_cb), dialog, 0);
    g_action_map_add_action (G_ACTION_MAP (actions), G_ACTION (close_action));
    gtk_widget_insert_action_group (GTK_WIDGET (dp->page), "window", G_ACTION_GROUP (actions));

    g_signal_connect (dp->page, "showing", G_CALLBACK (page_showing_cb), dp);
    g_signal_connect (dp->page, "shown", G_CALLBACK (page_shown_cb), dp);
    g_signal_connect (dp->page, "hiding", G_CALLBACK (page_hiding_cb), dp);
    dp->popped_id = g_signal_connect (navigation, "popped", G_CALLBACK (navigation_popped_cb), dp);

    inner = find_inner_navigation (dp->child);
    if (inner) {
        g_autoptr(AdwNavigationPage) first = NULL;
        g_autoptr(GListModel) stack = adw_navigation_view_get_navigation_stack (inner);

        if (g_list_model_get_n_items (stack) > 0)
            first = g_list_model_get_item (stack, 0);
        g_signal_connect_object (inner, "notify::visible-page", G_CALLBACK (inner_navigation_changed_cb), NULL, 0);
        if (first)
            g_signal_connect_object (first, "shown", G_CALLBACK (inner_first_page_shown_cb), inner, 0);
        inner_navigation_swipe (inner, !inner_navigation_on_first (inner));
    }

    g_object_set_qdata (G_OBJECT (dialog), dialog_page_quark (), dp);
    g_debug ("%s as a page", G_OBJECT_TYPE_NAME (dialog));
    adw_navigation_view_push (navigation, dp->page);
}

void
adw_dialog_present (AdwDialog *dialog, GtkWidget *parent)
{
    AdwNavigationView *navigation;
    DialogPage *dp;

    resolve_real_functions ();
    g_return_if_fail (ADW_IS_DIALOG (dialog));

    if (!enabled) {
        real_present (dialog, parent);
        return;
    }

    parent = substitute_parent (parent);

    /* Presented again while it is a page: nothing to do */
    dp = get_dialog_page (dialog);
    if (dp) {
        if (!dp->closed)
            return;
        g_critical ("Cannot present %s %p again while its page is still going away",
                    G_OBJECT_TYPE_NAME (dialog), dialog);
        return;
    }

    navigation = find_navigation (parent);
    if (!navigation || !wants_page (dialog)) {
        real_present (dialog, parent);
        return;
    }

    present_as_page (dialog, navigation);
}

gboolean
adw_dialog_close (AdwDialog *dialog)
{
    DialogPage *dp;

    resolve_real_functions ();
    g_return_val_if_fail (ADW_IS_DIALOG (dialog), FALSE);

    dp = get_dialog_page (dialog);
    if (!dp)
        return real_close (dialog);

    if (dp->closed)
        return FALSE;

    /* As in the dialog: a dialog that may not close only hears about the attempt */
    if (!adw_dialog_get_can_close (dialog)) {
        g_signal_emit_by_name (dialog, "close-attempt");
        return FALSE;
    }

    remove_page (dp);
    return TRUE;
}

void
adw_dialog_force_close (AdwDialog *dialog)
{
    DialogPage *dp;

    resolve_real_functions ();
    g_return_if_fail (ADW_IS_DIALOG (dialog));

    dp = get_dialog_page (dialog);
    if (!dp) {
        real_force_close (dialog);
        return;
    }

    remove_page (dp);
}

/* ---- GtkDialog as a page ---- */

typedef struct {
    GtkDialog *dialog;             /* weak */
    AdwNavigationView *navigation; /* weak */
    AdwNavigationPage *page;       /* weak */
    gulong popped_id;
    gulong destroy_id;
    gulong response_id;
    gboolean answered;
} GtkDialogPage;

static void
gtk_dialog_page_free (GtkDialogPage *gp)
{
    if (gp->navigation)
        g_clear_signal_handler (&gp->popped_id, gp->navigation);
    if (gp->dialog) {
        g_clear_signal_handler (&gp->destroy_id, gp->dialog);
        g_clear_signal_handler (&gp->response_id, gp->dialog);
    }
    g_clear_weak_pointer (&gp->dialog);
    g_clear_weak_pointer (&gp->navigation);
    g_clear_weak_pointer (&gp->page);
    g_free (gp);
}

/* Takes the page off the stack, with any page shown above it */
static void
gtk_dialog_page_remove (GtkDialogPage *gp)
{
    AdwNavigationPage *previous;

    if (!gp->page || !gp->navigation)
        return;

    previous = adw_navigation_view_get_previous_page (gp->navigation, gp->page);
    if (previous)
        adw_navigation_view_pop_to_page (gp->navigation, previous);
}

/* Back or the back swipe: the dialog hears "Cancel", while the page's widgets are still alive */
static void
gtk_dialog_page_popped_cb (AdwNavigationView *navigation, AdwNavigationPage *page, GtkDialogPage *gp)
{
    g_autoptr(GtkDialog) dialog = NULL;
    gboolean answered = gp->answered;

    if (page != gp->page)
        return;

    dialog = gp->dialog ? g_object_ref (gp->dialog) : NULL;
    gtk_dialog_page_free (gp);
    if (!dialog || answered)
        return;

    G_GNUC_BEGIN_IGNORE_DEPRECATIONS
    gtk_dialog_response (dialog, GTK_RESPONSE_CANCEL);
    G_GNUC_END_IGNORE_DEPRECATIONS
}

/* Answered (the button in the header bar): the dialog is done, its page goes */
static void
gtk_dialog_page_response_cb (GtkDialog *dialog, int response, GtkDialogPage *gp)
{
    gp->answered = TRUE;
    gtk_dialog_page_remove (gp);
}

/* Destroyed by its owner: the page goes too */
static void
gtk_dialog_page_destroy_cb (GtkWidget *dialog, GtkDialogPage *gp)
{
    g_clear_signal_handler (&gp->destroy_id, dialog);
    g_clear_signal_handler (&gp->response_id, dialog);
    g_clear_weak_pointer (&gp->dialog);
    gp->answered = TRUE;
    gtk_dialog_page_remove (gp);
}

typedef struct {
    GtkDialog *dialog; /* weak */
    int response;
} PageButton;

static void
page_button_free (gpointer data, GClosure *closure)
{
    PageButton *pb = data;

    g_clear_weak_pointer (&pb->dialog);
    g_free (pb);
}

static void
page_button_clicked_cb (GtkButton *button, PageButton *pb)
{
    G_GNUC_BEGIN_IGNORE_DEPRECATIONS
    if (pb->dialog)
        gtk_dialog_response (pb->dialog, pb->response);
    G_GNUC_END_IGNORE_DEPRECATIONS
}

/*
 * Shows @dialog, a GtkDialog of Settings or of a library (libnma), as a page of the window's
 * navigation instead of a window of its own. The widgets of its content area move into the page;
 * the dialog itself stays unshown and keeps its signals. Its confirming button (OK, Accept,
 * Apply or Yes) is repeated at the end of the page's header bar and follows the original's
 * sensitivity, "Cancel" goes: back and the back swipe answer GTK_RESPONSE_CANCEL. Once answered
 * or destroyed the page goes.
 *
 * With the object data CC_PLINIUS_CONFIRM_BELOW set on @dialog (a form, like libnma's Wi-Fi
 * dialogs) the confirming button is a pill below the content instead, where the thumb is.
 *
 * Returns FALSE if @parent is not in the window's navigation (or PLINIUS_DIALOG_PAGES=0); the
 * caller shows the dialog as before then.
 */
#define CC_PLINIUS_CONFIRM_BELOW "cc-plinius-confirm-below"

gboolean
cc_plinius_gtk_dialog_present_as_page (GtkDialog *dialog, GtkWidget *parent)
{
    static const int confirming[] = { GTK_RESPONSE_OK, GTK_RESPONSE_ACCEPT, GTK_RESPONSE_APPLY, GTK_RESPONSE_YES };
    AdwNavigationView *navigation;
    GtkWidget *content, *child, *box, *scrolled, *toolbar, *header;
    GtkDialogPage *gp;
    const char *title;
    gboolean below;
    g_autoptr(GPtrArray) buttons = g_ptr_array_new ();

    resolve_real_functions ();
    g_return_val_if_fail (GTK_IS_DIALOG (dialog), FALSE);

    navigation = enabled ? find_navigation (substitute_parent (parent)) : NULL;
    if (!navigation)
        return FALSE;

    header = adw_header_bar_new ();
    below = g_object_get_data (G_OBJECT (dialog), CC_PLINIUS_CONFIRM_BELOW) != NULL;
    G_GNUC_BEGIN_IGNORE_DEPRECATIONS
    for (guint i = 0; i < G_N_ELEMENTS (confirming); i++) {
        GtkWidget *original = gtk_dialog_get_widget_for_response (dialog, confirming[i]);
        GtkWidget *button;
        PageButton *pb;

        if (!GTK_IS_BUTTON (original) || !gtk_button_get_label (GTK_BUTTON (original)))
            continue;

        button = gtk_button_new_with_mnemonic (gtk_button_get_label (GTK_BUTTON (original)));
        gtk_button_set_use_underline (GTK_BUTTON (button), gtk_button_get_use_underline (GTK_BUTTON (original)));
        gtk_widget_add_css_class (button, "suggested-action");
        g_object_bind_property (original, "sensitive", button, "sensitive", G_BINDING_SYNC_CREATE);
        pb = g_new0 (PageButton, 1);
        g_set_weak_pointer (&pb->dialog, dialog);
        pb->response = confirming[i];
        g_signal_connect_data (button, "clicked", G_CALLBACK (page_button_clicked_cb), pb, page_button_free, 0);
        if (below) {
            gtk_widget_add_css_class (button, "pill");
            gtk_widget_set_halign (button, GTK_ALIGN_CENTER);
            gtk_widget_set_margin_top (button, 12);
            g_ptr_array_add (buttons, button);
        } else {
            adw_header_bar_pack_end (ADW_HEADER_BAR (header), button);
        }
    }
    content = gtk_dialog_get_content_area (dialog);
    G_GNUC_END_IGNORE_DEPRECATIONS

    box = gtk_box_new (GTK_ORIENTATION_VERTICAL, 12);
    gtk_widget_set_margin_start (box, 12);
    gtk_widget_set_margin_end (box, 12);
    gtk_widget_set_margin_top (box, 12);
    gtk_widget_set_margin_bottom (box, 12);
    while ((child = gtk_widget_get_first_child (content))) {
        g_object_ref (child);
        gtk_box_remove (GTK_BOX (content), child);
        gtk_box_append (GTK_BOX (box), child);
        g_object_unref (child);
    }
    /* below the form: after everything the dialog brought */
    for (guint i = 0; i < buttons->len; i++)
        gtk_box_append (GTK_BOX (box), g_ptr_array_index (buttons, i));

    scrolled = gtk_scrolled_window_new ();
    gtk_scrolled_window_set_policy (GTK_SCROLLED_WINDOW (scrolled), GTK_POLICY_NEVER, GTK_POLICY_AUTOMATIC);
    gtk_scrolled_window_set_child (GTK_SCROLLED_WINDOW (scrolled), box);
    toolbar = adw_toolbar_view_new ();
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), header);
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), scrolled);

    title = gtk_window_get_title (GTK_WINDOW (dialog));

    gp = g_new0 (GtkDialogPage, 1);
    g_set_weak_pointer (&gp->dialog, dialog);
    g_set_weak_pointer (&gp->navigation, navigation);
    g_set_weak_pointer (&gp->page, adw_navigation_page_new (toolbar, title ? title : ""));
    gp->popped_id = g_signal_connect (navigation, "popped", G_CALLBACK (gtk_dialog_page_popped_cb), gp);
    gp->destroy_id = g_signal_connect (dialog, "destroy", G_CALLBACK (gtk_dialog_page_destroy_cb), gp);
    gp->response_id = g_signal_connect (dialog, "response", G_CALLBACK (gtk_dialog_page_response_cb), gp);

    g_debug ("%s as a page", G_OBJECT_TYPE_NAME (dialog));
    adw_navigation_view_push (navigation, gp->page);
    return TRUE;
}
