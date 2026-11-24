# pam_plinius_crypt: a new login PIN must not be the encryption password. pam_sm_chauthtok()
# with a fake libpam (c/dsec_fakepam.h) and a fake libcryptsetup (c/dsec_pam_crypt_harness.c):
# what happens when the LUKS header behind an open mapping cannot be looked at. The rest of the
# module's decisions are in tests/boot/test_boot_login_check.py.

import subprocess

import pytest

from dsec_helpers import CDIR, build, san_env

PAM_UPDATE_AUTHTOK = 0x2000


@pytest.fixture(scope="module")
def module(tmp_path_factory):
    return build(tmp_path_factory.mktemp("pam-crypt"), "pam-crypt-harness",
                 [CDIR / "dsec_pam_crypt_harness.c"], flags=["-Wno-unused-parameter"])


def chauthtok(exe, token, *args, **env):
    r = subprocess.run([str(exe), "alice", token.encode().hex(), str(PAM_UPDATE_AUTHTOK), *args],
                       capture_output=True, text=True, timeout=30,
                       env=san_env(DSEC_ACTIVE="plinius-root", DSEC_LUKS_PASS="secret", **env))
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


def test_encryption_password_is_refused(module):
    out = chauthtok(module, "secret")
    assert out[-1] == "rc AUTHTOK_ERR"
    assert "msg BAD PASSWORD: it is the encryption password, choose another one" in out
    # Forgotten, so that a retry asks again; only checked, nothing activated
    assert "set_item 6 NULL" in out and "activate NULL" in out


def test_header_that_cannot_be_opened_does_not_block_the_change(module):
    # The mapping was there for crypt_status but cannot be opened now: nothing to compare with,
    # the change goes ahead (the module never stands in the way without a match)
    out = chauthtok(module, "secret", DSEC_INIT_FAILS="1")
    assert out == ["init plinius-root", "rc SUCCESS"]


def test_other_password_passes(module):
    out = chauthtok(module, "1234")
    assert out[-1] == "rc SUCCESS" and not any(l.startswith("msg ") for l in out)
