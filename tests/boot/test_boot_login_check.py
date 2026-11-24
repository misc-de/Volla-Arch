# The two checks that keep the encryption password and the login password (the PIN of the
# screen lock) apart: login-password-check (plinius-encrypt asks it) and pam_plinius_crypt.so
# (passwd asks it). Test accounts and hashes are made in tmp_path; /etc/shadow is never read.

import ctypes
import io
import shutil
import subprocess
import sys

import pytest

from conftest import PKG, compile_c, load_script

CHECK = PKG / "plinius-crypt" / "login-password-check"
PAM = PKG / "plinius-crypt" / "pam_plinius_crypt.c"


def libcrypt():
    try:
        lib = ctypes.CDLL("libcrypt.so.2")
    except OSError:
        pytest.skip("no libcrypt.so.2 on the host")
    lib.crypt.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    lib.crypt.restype = ctypes.c_char_p
    lib.crypt_gensalt.argtypes = [ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p, ctypes.c_int]
    lib.crypt_gensalt.restype = ctypes.c_char_p
    return lib


def hashed(password, prefix=b"$6$"):
    lib = libcrypt()
    salt = lib.crypt_gensalt(prefix, 0, None, 0)
    assert salt, prefix
    return lib.crypt(password.encode(), salt)


@pytest.fixture
def check(monkeypatch, tmp_path):
    """login-password-check's main with its files in tmp_path and a fixed login.defs."""
    mod = load_script(CHECK)
    defs = tmp_path / "login.defs"
    defs.write_text("")
    real_open = open

    def fake_open(path, *a, **kw):
        # The one fixed path the script reads besides its arguments
        if path == "/etc/login.defs":
            path = defs
        assert str(path).startswith(str(tmp_path)), path
        return real_open(path, *a, **kw)

    monkeypatch.setattr(mod, "open", fake_open, raising=False)

    def run(password, users, login_defs=""):
        defs.write_text(login_defs)
        passwd = tmp_path / "passwd"
        shadow = tmp_path / "shadow"
        passwd.write_bytes(b"".join(b"%s:x:%d:%d::/home/x:/bin/bash\n" % (n.encode(), uid, uid)
                                    for n, uid, _ in users))
        shadow.write_bytes(b"".join(b"%s:%s:19000:0:99999:7:::\n" % (n.encode(), h)
                                    for n, _, h in users))
        monkeypatch.setattr(sys, "argv", ["login-password-check", str(passwd), str(shadow)])
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(password)))
        return mod.main()
    run.module = mod
    return run


def test_pin_of_a_user_matches(check):
    users = [("root", 0, hashed("1234")), ("phablet", 1000, hashed("2468")),
             ("misc", 1001, hashed("1357", b"$y$"))]
    assert check(b"2468\n", users) == 0
    assert check(b"1357\n", users) == 0          # yescrypt as well as sha512crypt
    assert check(b"1357", users) == 0            # without the newline
    assert check(b"9999\n", users) == 1
    assert check(b"2468 \n", users) == 1         # no trimming besides the newline


def test_only_login_users_count(check):
    users = [("root", 0, hashed("1234")), ("daemon", 999, hashed("5555")),
             ("nobody", 65534, hashed("7777"))]
    assert check(b"1234\n", users) == 1
    assert check(b"5555\n", users) == 1
    assert check(b"7777\n", users) == 1


def test_uid_range_from_login_defs(check):
    users = [("old", 500, hashed("1111")), ("big", 70000, hashed("2222"))]
    defs = "# comment\nUID_MIN   500\nUID_MAX\t70000\nUID_MIN_X 1\nGID_MIN 1000\n"
    assert check(b"1111\n", users, defs) == 0
    assert check(b"2222\n", users, defs) == 0
    assert check(b"1111\n", users, "UID_MIN nonsense\n") == 1


def test_locked_and_empty_accounts_are_skipped(check):
    h = hashed("1234")
    users = [("locked", 1000, b"!" + h), ("star", 1001, b"*"), ("empty", 1002, b"")]
    assert check(b"1234\n", users) == 1
    assert check(b"\n", users) == 1
    # An empty password never matches anything
    assert check(b"", [("u", 1000, hashed(""))]) == 1


def test_unreadable_files(check, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"1234\n")))
    monkeypatch.setattr(sys, "argv", ["x", str(tmp_path / "missing"), str(tmp_path / "missing2")])
    assert check.module.main() == 2


# --- pam_plinius_crypt.so ------------------------------------------------------------------------

HARNESS = r"""
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <libcryptsetup.h>
#include <security/pam_ext.h>
#include <security/pam_modules.h>

/* Stand-ins for libcryptsetup and libpam, steered by the command line */
static char active[256], checked[64];
static const char *type = "LUKS2", *luks_pass = "secret", *authtok = "1234";
static int activate_err, authtok_rc = PAM_SUCCESS, activations, cleared, shown, logged;
static const char *activated_name = "unset";

int crypt_init_by_name(struct crypt_device **cd, const char *name)
{ snprintf(checked, sizeof checked, "%s", name); *cd = (struct crypt_device *) 1; return 0; }
const char *crypt_get_type(struct crypt_device *cd) { return *type ? type : NULL; }
int crypt_activate_by_passphrase(struct crypt_device *cd, const char *name, int keyslot,
                                 const char *pass, size_t size, uint32_t flags)
{
    activations++;
    activated_name = name ? name : "NULL";
    if (activate_err)
        return activate_err;
    return size == strlen(luks_pass) && memcmp(pass, luks_pass, size) == 0 ? 0 : -EPERM;
}
void crypt_free(struct crypt_device *cd) {}
crypt_status_info crypt_status(struct crypt_device *cd, const char *name)
{
    char want[80];
    snprintf(want, sizeof want, ",%s,", name);
    return strstr(active, want) ? CRYPT_ACTIVE : CRYPT_INACTIVE;
}
int pam_get_authtok(pam_handle_t *pamh, int item, const char **tok, const char *prompt)
{ *tok = *authtok ? authtok : NULL; return authtok_rc; }
int pam_set_item(pam_handle_t *pamh, int item, const void *value)
{ if (item == PAM_AUTHTOK && value == NULL) cleared++; return PAM_SUCCESS; }
void pam_syslog(const pam_handle_t *pamh, int priority, const char *fmt, ...) { logged++; }
/* pam_error is a macro for pam_prompt with PAM_ERROR_MSG */
int pam_prompt(pam_handle_t *pamh, int style, char **response, const char *fmt, ...)
{ if (style == PAM_ERROR_MSG) shown++; return PAM_SUCCESS; }

/* harness ACTIVE TYPE LUKS_PASS ACTIVATE_ERR AUTHTOK AUTHTOK_RC FLAGS [MODULE_ARG...] */
int main(int argc, char **argv)
{
    snprintf(active, sizeof active, ",%s,", argv[1]);
    type = argv[2];
    luks_pass = argv[3];
    activate_err = atoi(argv[4]);
    authtok = argv[5];
    authtok_rc = atoi(argv[6]);
    int flags = strcmp(argv[7], "prelim") == 0 ? PAM_PRELIM_CHECK : PAM_UPDATE_AUTHTOK;
    /* Never looked into by the stand-ins, but declared non-null */
    static int handle;
    int rc = pam_sm_chauthtok((pam_handle_t *) &handle, flags, argc - 8, (const char **) argv + 8);
    printf("rc=%d checked=%s name=%s activations=%d cleared=%d shown=%d logged=%d\n", rc,
           *checked ? checked : "-", activated_name, activations, cleared, shown, logged);
    printf("auth=%d setcred=%d\n", pam_sm_authenticate(NULL, 0, 0, NULL),
           pam_sm_setcred(NULL, 0, 0, NULL));
    return 0;
}
"""


@pytest.fixture(scope="module")
def pam(tmp_path_factory):
    d = tmp_path_factory.mktemp("pam")
    cc = shutil.which("cc") or shutil.which("gcc")
    if cc is None:
        pytest.skip("no C compiler")
    probe = subprocess.run([cc, "-E", "-x", "c", "-"], input="#include <libcryptsetup.h>\n"
                           "#include <security/pam_ext.h>\n", capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip("no libcryptsetup or PAM headers on the host")
    (d / "harness.c").write_text(HARNESS)
    exe = compile_c(d / "harness", [d / "harness.c", PAM], flags=["-Wno-unused-variable"])

    def run(active="plinius-root", type="LUKS2", luks_pass="secret", err=0, authtok="1234",
            authtok_rc=0, flags="update", *args):
        out = subprocess.run([str(exe), active, type, luks_pass, str(err), authtok,
                              str(authtok_rc), flags, *args], capture_output=True, text=True,
                             check=True).stdout
        return dict(kv.split("=", 1) for kv in out.split())
    return run


PAM_SUCCESS, PAM_AUTHTOK_ERR, PAM_IGNORE = 0, 20, 25


def test_pam_different_password_passes(pam):
    r = pam()
    assert int(r["rc"]) == PAM_SUCCESS
    # Only checked, nothing activated (no mapping name)
    assert r["checked"] == "plinius-root" and r["name"] == "NULL" and r["activations"] == "1"


def test_pam_encryption_password_is_refused(pam):
    r = pam(authtok="secret")
    assert int(r["rc"]) == PAM_AUTHTOK_ERR
    assert r["cleared"] == "1" and r["shown"] == "1"


def test_pam_halium_mapping(pam):
    r = pam(active="plinius-data", authtok="secret")
    assert int(r["rc"]) == PAM_AUTHTOK_ERR and r["checked"] == "plinius-data"
    # The root mapping comes first when both are open
    r = pam(active="plinius-data,plinius-root", authtok="secret")
    assert r["checked"] == "plinius-root"


def test_pam_nothing_to_compare_with(pam):
    assert int(pam(active="other")["rc"]) == PAM_IGNORE
    assert int(pam(authtok="")["rc"]) == PAM_IGNORE
    r = pam(type="", authtok="secret")  # not a LUKS device: cannot tell
    assert int(r["rc"]) == PAM_SUCCESS and r["activations"] == "0"


def test_pam_prelim_phase_asks_nothing(pam):
    r = pam(flags="prelim", authtok="secret")
    assert int(r["rc"]) == PAM_SUCCESS and r["checked"] == "-"


def test_pam_authtok_error_is_passed_on(pam):
    assert int(pam(authtok_rc=PAM_AUTHTOK_ERR)["rc"]) == PAM_AUTHTOK_ERR


def test_pam_check_error_is_logged(pam):
    r = pam(err=-5, authtok="secret")  # -EIO
    assert int(r["rc"]) == PAM_SUCCESS and r["logged"] == "1"


def test_pam_name_option(pam):
    r = pam("test-map", "LUKS2", "secret", 0, "secret", 0, "update", "name=test-map")
    assert int(r["rc"]) == PAM_AUTHTOK_ERR and r["checked"] == "test-map"
    r = pam("plinius-root", "LUKS2", "secret", 0, "secret", 0, "update", "name=test-map")
    assert int(r["rc"]) == PAM_IGNORE
    r = pam("plinius-root", "LUKS2", "secret", 0, "x", 0, "update", "bogus")
    assert r["logged"] == "1"


def test_pam_other_phases_ignore(pam):
    r = pam()
    assert int(r["auth"]) == PAM_IGNORE and int(r["setcred"]) == PAM_IGNORE


def test_without_login_defs_the_shadow_defaults_apply(check, monkeypatch):
    def no_defs(path, *a, **kw):
        raise FileNotFoundError(path)
    monkeypatch.setattr(check.module, "open", no_defs, raising=False)
    assert check.module.login_uid_range() == (1000, 60000)


def test_without_libcrypt_it_is_an_error(check, monkeypatch, capsys):
    import types

    def no_lib(name):
        raise OSError(f"{name}: cannot open shared object file")
    monkeypatch.setattr(check.module, "ctypes", types.SimpleNamespace(CDLL=no_lib))
    assert check(b"2468\n", [("phablet", 1000, hashed("2468"))]) == 2
    assert "libcrypt.so.2: cannot open shared object file" in capsys.readouterr().err


def test_as_a_program(tmp_path):
    # The real /etc/login.defs is read (only read); the accounts are the test's own files
    passwd, shadow = tmp_path / "passwd", tmp_path / "shadow"
    passwd.write_text("phablet:x:1000:1000::/home/phablet:/bin/bash\n")
    shadow.write_bytes(b"phablet:%s:19000:0:99999:7:::\n" % hashed("2468"))
    for pin, code in ((b"2468\n", 0), (b"1111\n", 1)):
        r = subprocess.run([sys.executable, "-I", str(CHECK), str(passwd), str(shadow)],
                           input=pin, capture_output=True)
        assert r.returncode == code, r.stderr
        assert r.stdout == b"" and r.stderr == b""
