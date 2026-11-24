# Throw-away OpenPGP keys for the signature tests of tests/apps (not a test module itself).
#
# Every key lives in a GNUPGHOME below pytest's temporary directory; the user's keyring is
# never read or written. The gpg-agent started for that home is stopped again at the end.

import os
import shutil
import subprocess

import pytest


class TestKeys:
    """A private GNUPGHOME with helpers to make keys, export them and sign data."""

    __test__ = False  # not a test class for pytest

    def __init__(self, home):
        self.home = home
        home.mkdir(mode=0o700)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("GPG_", "GNUPG"))}
        self.env["GNUPGHOME"] = str(home)
        self.env["LC_ALL"] = "C"

    def gpg(self, *args, data=None):
        r = subprocess.run(["gpg", "--batch", "--no-tty", "--pinentry-mode", "loopback",
                            "--passphrase", "", *args],
                           input=data, env=self.env, capture_output=True, timeout=60)
        assert r.returncode == 0, r.stderr.decode(errors="replace")
        return r.stdout

    def generate(self, name):
        """A new signing key; returns its fingerprint."""
        self.gpg("--quick-gen-key", f"{name} <{name}@example.invalid>", "ed25519", "sign", "never")
        out = self.gpg("--with-colons", "--list-keys", f"{name}@example.invalid").decode()
        return next(line.split(":")[9] for line in out.splitlines() if line.startswith("fpr:"))

    def export(self, *fprs, armor=True):
        return self.gpg(*(["--armor"] if armor else []), "--export", *fprs)

    def sign(self, fpr, data):
        """Detached binary signature of data by fpr."""
        return self.gpg("--local-user", fpr, "--detach-sign", "--output", "-", data=data)

    def stop(self):
        # Stop the agent and drop its socket directory (/run/user/UID/gnupg/d.*)
        subprocess.run(["gpgconf", "--kill", "all"], env=self.env, capture_output=True)
        subprocess.run(["gpgconf", "--remove-socketdir"], env=self.env, capture_output=True)


@pytest.fixture(scope="session")
def gpg_keys(tmp_path_factory):
    """(keys, trusted fingerprint, attacker fingerprint)."""
    if not (shutil.which("gpg") and shutil.which("gpgv") and shutil.which("gpgconf")):
        pytest.skip("gpg/gpgv not installed")
    keys = TestKeys(tmp_path_factory.mktemp("gpg") / "home")
    try:
        trusted = keys.generate("repo")
        attacker = keys.generate("attacker")
        yield keys, trusted, attacker
    finally:
        keys.stop()
