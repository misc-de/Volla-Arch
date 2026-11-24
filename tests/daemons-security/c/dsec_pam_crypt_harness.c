/*
 * Harness for pam_plinius_crypt.c: pam_sm_chauthtok() with the fake libpam of dsec_fakepam.h
 * (see there for the arguments) and a fake libcryptsetup: the mappings in DSEC_ACTIVE
 * (comma-separated) are open, LUKS2, with the passphrase DSEC_LUKS_PASS; DSEC_INIT_FAILS makes
 * crypt_init_by_name() fail (a mapping gone between the status and the check, or no access).
 * Prints "init NAME" and "activate NAME|NULL" for the calls the module makes.
 */
#include "dsec_fakepam.h"

#include <errno.h>
#include <libcryptsetup.h>

int
pam_set_item (pam_handle_t *pamh, int item, const void *value)
{
    (void) pamh;
    printf ("set_item %d %s\n", item, value ? "value" : "NULL");
    return PAM_SUCCESS;
}

crypt_status_info
crypt_status (struct crypt_device *cd, const char *name)
{
    char list[256], want[80];

    (void) cd;
    snprintf (list, sizeof list, ",%s,", getenv ("DSEC_ACTIVE") ? getenv ("DSEC_ACTIVE") : "");
    snprintf (want, sizeof want, ",%s,", name);
    return strstr (list, want) ? CRYPT_ACTIVE : CRYPT_INACTIVE;
}

int
crypt_init_by_name (struct crypt_device **cd, const char *name)
{
    printf ("init %s\n", name);
    if (getenv ("DSEC_INIT_FAILS"))
        return -ENODEV;
    *cd = (struct crypt_device *) 1;
    return 0;
}

const char *
crypt_get_type (struct crypt_device *cd)
{
    (void) cd;
    return "LUKS2";
}

int
crypt_activate_by_passphrase (struct crypt_device *cd, const char *name, int keyslot,
                              const char *pass, size_t size, uint32_t flags)
{
    const char *luks = getenv ("DSEC_LUKS_PASS") ? getenv ("DSEC_LUKS_PASS") : "";

    (void) cd;
    (void) keyslot;
    (void) flags;
    printf ("activate %s\n", name ? name : "NULL");
    return size == strlen (luks) && memcmp (pass, luks, size) == 0 ? 0 : -EPERM;
}

void
crypt_free (struct crypt_device *cd)
{
    (void) cd;
}

#include "plinius-crypt/pam_plinius_crypt.c"

int
main (int argc, char **argv)
{
    return dsec_pam_main (argc, argv, pam_sm_chauthtok);
}
