/*
 * Harness for plinius-usb.c (its main() renamed): who may switch USB functions on, choose the
 * port or the camera a computer sees (check_caller against the scripted bus of dsec_fakebus.h),
 * and what the D-Bus methods and properties accept. Writing files and running programs are
 * refused and only reported ("WRITE PATH", "RUN PROGRAM", "SAVE"), so nothing reaches configfs,
 * sysfs or iptables; the tests stop before anything would be switched.
 *
 * Commands on stdin, one output line each:
 *   connected 0|1 / functions N / question none|usb-c|pogo     the daemon's state
 *   setfn NAME,NAME,...  SetFunctions ("-" for none)            choose PORT   ChoosePort
 *   role ROLE            SetPowerRole
 *   setprop PROP VALUE   Set: "ok" / "error MESSAGE", then the value read back
 *   caller               check_caller(): "allowed UID" / "denied MESSAGE"
 *   bus KEY VALUE        see dsec_fakebus.h
 */
#define _GNU_SOURCE
#include <gio/gio.h>
#include <glib/gstdio.h>

#include "dsec_fakebus.h"

static gboolean
fake_set_contents_full (const char *path, const char *contents, gssize length,
                        GFileSetContentsFlags flags, int mode, GError **error)
{
    (void) contents;
    (void) length;
    (void) flags;
    (void) mode;
    printf ("WRITE %s\n", path);
    g_set_error (error, G_FILE_ERROR, G_FILE_ERROR_PERM, "refused by the test harness");
    return FALSE;
}

static gboolean
fake_spawn_sync (const char *dir, char **argv, char **envp, GSpawnFlags flags,
                 GSpawnChildSetupFunc setup, gpointer data, char **out, char **err, int *status,
                 GError **error)
{
    (void) dir;
    (void) envp;
    (void) flags;
    (void) setup;
    (void) data;
    (void) out;
    (void) err;
    (void) status;
    printf ("RUN %s\n", argv[0]);
    g_set_error (error, G_SPAWN_ERROR, G_SPAWN_ERROR_FAILED, "refused by the test harness");
    return FALSE;
}

static gboolean
fake_key_file_save (GKeyFile *file, const char *path, GError **error)
{
    (void) file;
    (void) path;
    (void) error;
    printf ("SAVE\n");
    return TRUE;
}

#define g_file_set_contents_full fake_set_contents_full
#define g_spawn_sync fake_spawn_sync
#define g_key_file_save_to_file fake_key_file_save
#define main usb_main
#include "plinius-usb/plinius-usb.c"
#undef main

static void
call (const char *method, GVariant *params)
{
    FakeInv inv = { 0 };

    fake_inv_reset (&inv, method, params);
    on_method_call (NULL, ":1.7", OBJECT_PATH, INTERFACE, method, inv.params,
                    (GDBusMethodInvocation *) &inv, NULL);
    fake_inv_print (&inv);
    fake_inv_reset (&inv, NULL, NULL);
}

int
main (void)
{
    static char line[8192];

    setvbuf (stdout, NULL, _IOLBF, 0);
    self.webcam_camera = g_strdup (WEBCAM_CAMERA_DEFAULT);
    while (fgets (line, sizeof line, stdin)) {
        char a[4096] = "", b[4096] = "";
        long n;

        line[strcspn (line, "\n")] = '\0';
        if (fb_command (line))
            continue;
        if (sscanf (line, "connected %ld", &n) == 1) {
            self.connected = n != 0;
            puts ("ok");
        } else if (sscanf (line, "functions %ld", &n) == 1) {
            self.functions = (guint) n;
            puts ("ok");
        } else if (sscanf (line, "question %4095s", a) == 1) {
            self.pogo.question = !strcmp (a, "usb-c") ? POGO_PORT_USBC
                                 : !strcmp (a, "pogo") ? POGO_PORT_POGO : POGO_PORT_NONE;
            puts ("ok");
        } else if (sscanf (line, "setfn %4095s", a) == 1) {
            GVariantBuilder names;
            g_auto (GStrv) parts = g_strsplit (a, ",", -1);

            g_variant_builder_init (&names, G_VARIANT_TYPE ("as"));
            for (char **p = parts; *p; p++)
                if (strcmp (*p, "-"))
                    g_variant_builder_add (&names, "s", *p);
            call ("SetFunctions", g_variant_new ("(as)", &names));
        } else if (sscanf (line, "choose %4095s", a) == 1) {
            call ("ChoosePort", g_variant_new ("(s)", a));
        } else if (sscanf (line, "role %4095s", a) == 1) {
            call ("SetPowerRole", g_variant_new ("(s)", a));
        } else if (sscanf (line, "setprop %4095s %4095s", a, b) == 2) {
            g_autoptr (GError) error = NULL;
            GVariant *value = !strcmp (b, "true")    ? g_variant_new_boolean (TRUE)
                              : !strcmp (b, "false") ? g_variant_new_boolean (FALSE)
                                                     : g_variant_new_string (strcmp (b, "-") ? b : "");
            g_autoptr (GVariant) sunk = g_variant_ref_sink (value);
            gboolean ok = on_set_property (NULL, ":1.7", OBJECT_PATH, INTERFACE, a, sunk, &error, NULL);
            g_autoptr (GError) get_error = NULL;
            g_autoptr (GVariant) now = on_get_property (NULL, ":1.7", OBJECT_PATH, INTERFACE, a,
                                                        &get_error, NULL);
            g_autofree char *text = now ? g_variant_print (g_variant_ref_sink (now), FALSE) : NULL;

            if (ok)
                printf ("ok %s\n", text ? text : "-");
            else
                printf ("error %s | %s\n", error ? error->message : "", text ? text : "-");
        } else if (!strcmp (line, "caller")) {
            g_autoptr (GError) error = NULL;
            uid_t user = 4242;

            if (check_caller (":1.7", &user, &error))
                printf ("allowed %u\n", (unsigned) user);
            else
                printf ("denied %s\n", error ? error->message : "");
        } else {
            printf ("<unknown command: %.60s>\n", line);
        }
    }
    return 0;
}
