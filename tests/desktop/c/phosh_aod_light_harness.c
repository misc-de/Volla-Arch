/*
 * Harness for the light sensor part of aod.c of phosh-plinius 0116 (taken from the patch, see
 * test_desktop_phosh_aod_light.py): the always-on display with half the light in the dark. A
 * PhoshAod here is a plain GObject with the fields that part uses; its content box only records
 * its opacity. On the system bus of the test (a private dbus-daemon with a fake sensor proxy):
 *
 *   stdin "new"      a new always-on display, as aod_show () makes one (claim_light ())
 *   stdin "destroy"  as aod_hide () destroys it (dispose: release_light ())
 *   stdin "quit"     ends it
 *
 * stdout: "ready", "opacity <value>" for each set, "exit"; GLib's debug messages as well.
 */
#include <gio/gio.h>
#include <stdio.h>
#include <string.h>

typedef struct { double opacity; } GtkWidget;

static void
gtk_widget_set_opacity (GtkWidget *widget, double opacity)
{
  widget->opacity = opacity;
  printf ("opacity %.2f\n", opacity);
}

#define PHOSH_TYPE_AOD (phosh_aod_get_type ())
G_DECLARE_FINAL_TYPE (PhoshAod, phosh_aod, PHOSH, AOD, GObject)

struct _PhoshAod {
  GObject          parent;

  GtkWidget       *box;

  GCancellable    *light_cancel;
  GDBusConnection *light_bus;
  guint            light_signal_id;
};

G_DEFINE_TYPE (PhoshAod, phosh_aod, G_TYPE_OBJECT)

#include AOD_LIGHT_DEFINES
#include AOD_LIGHT_SOURCE

static void
phosh_aod_dispose (GObject *object)
{
  PhoshAod *self = PHOSH_AOD (object);

  release_light (self);
  G_OBJECT_CLASS (phosh_aod_parent_class)->dispose (object);
}

static void
phosh_aod_finalize (GObject *object)
{
  g_free (PHOSH_AOD (object)->box);
  G_OBJECT_CLASS (phosh_aod_parent_class)->finalize (object);
}

static void
phosh_aod_class_init (PhoshAodClass *klass)
{
  G_OBJECT_CLASS (klass)->dispose = phosh_aod_dispose;
  G_OBJECT_CLASS (klass)->finalize = phosh_aod_finalize;
}

static void
phosh_aod_init (PhoshAod *self)
{
  self->box = g_new0 (GtkWidget, 1);
  self->box->opacity = 1.0;
}

static PhoshAod *aod;

static gboolean
on_stdin (GIOChannel *channel, GIOCondition condition, gpointer loop)
{
  g_autofree char *line = NULL;

  if (g_io_channel_read_line (channel, &line, NULL, NULL, NULL) != G_IO_STATUS_NORMAL ||
      g_str_equal (g_strstrip (line), "quit")) {
    g_main_loop_quit (loop);
    return G_SOURCE_REMOVE;
  }
  if (g_str_equal (line, "new")) {
    g_assert (aod == NULL);
    aod = g_object_new (PHOSH_TYPE_AOD, NULL);
    claim_light (aod);
  } else if (g_str_equal (line, "destroy")) {
    /* gtk_widget_destroy () disposes, whoever still holds a reference */
    g_object_run_dispose (G_OBJECT (aod));
    g_clear_object (&aod);
  }
  printf ("done %s\n", line);
  return G_SOURCE_CONTINUE;
}

int
main (void)
{
  g_autoptr (GMainLoop) loop = g_main_loop_new (NULL, FALSE);
  GIOChannel *channel = g_io_channel_unix_new (0);

  setvbuf (stdout, NULL, _IOLBF, 0);
  g_io_add_watch (channel, G_IO_IN | G_IO_HUP, on_stdin, loop);
  puts ("ready");
  g_main_loop_run (loop);
  if (aod)
    g_object_run_dispose (G_OBJECT (aod));
  g_clear_object (&aod);
  /* Let the callbacks of calls still under way give back their references */
  for (int i = 0; i < 50 && g_main_context_iteration (NULL, FALSE); i++)
    ;
  g_io_channel_unref (channel);
  puts ("exit");
  return 0;
}
