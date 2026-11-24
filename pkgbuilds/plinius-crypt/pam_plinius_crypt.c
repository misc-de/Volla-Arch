/*
 * pam_plinius_crypt.so: a new login password (on the phone the PIN of the screen lock) must
 * differ from the password of the encrypted root. The PIN is short and typed in view of others
 * many times a day; were it also the encryption password, a stolen phone would be open.
 * plinius-encrypt checks the other way round (login-password-check).
 *
 * For the password stack of /etc/pam.d/passwd, before system-auth:
 *   password  requisite  pam_plinius_crypt.so
 * In the update phase it reads the new password itself (pam_get_authtok, with the retype
 * prompt), so the modules after it take that one. Then it tries the password on the LUKS
 * header of the open mapping plinius-root, or plinius-data on the phone's Halium system, where the
 * whole data partition is encrypted (only a check, nothing is activated). A match is refused with
 * "BAD PASSWORD: ..." (passwd and GNOME Settings know that form); without such a mapping the module
 * does nothing.
 *
 * Option name=MAPPING checks another mapping (for tests). Runs as root (passwd is setuid).
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <errno.h>
#include <string.h>
#include <syslog.h>

#include <libcryptsetup.h>
#include <security/pam_ext.h>
#include <security/pam_modules.h>

/* The mappings of plinius-crypt: the root of a system with its own initramfs, the data
 * partition of the Halium system (halium-unlock) */
static const char *const default_names[] = { "plinius-root", "plinius-data", NULL };

/* 1 if @password opens the LUKS header behind mapping @name, 0 if not, -1 if there is no
 * such mapping or it cannot be checked */
static int
is_luks_password (pam_handle_t *pamh, const char *name, const char *password)
{
    struct crypt_device *cd = NULL;
    int r;

    if (crypt_init_by_name (&cd, name) < 0)
        return -1;

    if (crypt_get_type (cd) == NULL || strncmp (crypt_get_type (cd), "LUKS", 4) != 0) {
        crypt_free (cd);
        return -1;
    }

    /* No name: only verify the passphrase, activate nothing */
    r = crypt_activate_by_passphrase (cd, NULL, CRYPT_ANY_SLOT, password, strlen (password), 0);
    crypt_free (cd);

    if (r >= 0)
        return 1;
    if (r == -EPERM)
        return 0;

    pam_syslog (pamh, LOG_WARNING, "cannot check the password on %s: %s", name, strerror (-r));
    return -1;
}

PAM_EXTERN int
pam_sm_chauthtok (pam_handle_t *pamh, int flags, int argc, const char **argv)
{
    const char *name = NULL;
    const char *password = NULL;
    crypt_status_info status;
    int r;

    for (int i = 0; i < argc; i++) {
        if (strncmp (argv[i], "name=", 5) == 0)
            name = argv[i] + 5;
        else
            pam_syslog (pamh, LOG_ERR, "unknown option: %s", argv[i]);
    }

    /* Nothing to check before the new password is asked for */
    if (flags & PAM_PRELIM_CHECK)
        return PAM_SUCCESS;

    /* The first open mapping; none: nothing to compare with, and no prompt from us */
    if (name == NULL) {
        for (int i = 0; default_names[i] != NULL && name == NULL; i++) {
            status = crypt_status (NULL, default_names[i]);
            if (status == CRYPT_ACTIVE || status == CRYPT_BUSY)
                name = default_names[i];
        }
        if (name == NULL)
            return PAM_IGNORE;
    } else {
        status = crypt_status (NULL, name);
        if (status != CRYPT_ACTIVE && status != CRYPT_BUSY)
            return PAM_IGNORE;
    }

    r = pam_get_authtok (pamh, PAM_AUTHTOK, &password, NULL);
    if (r != PAM_SUCCESS)
        return r;
    if (password == NULL || *password == '\0')
        return PAM_IGNORE;

    if (is_luks_password (pamh, name, password) == 1) {
        /* Forget it, so that a retry asks again */
        pam_set_item (pamh, PAM_AUTHTOK, NULL);
        pam_error (pamh, "BAD PASSWORD: it is the encryption password, choose another one");
        return PAM_AUTHTOK_ERR;
    }

    return PAM_SUCCESS;
}

PAM_EXTERN int
pam_sm_authenticate (pam_handle_t *pamh, int flags, int argc, const char **argv)
{
    return PAM_IGNORE;
}

PAM_EXTERN int
pam_sm_setcred (pam_handle_t *pamh, int flags, int argc, const char **argv)
{
    return PAM_IGNORE;
}
