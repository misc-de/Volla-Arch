# The release tooling around the signed repo [plinius]: tools/lib.sh (helpers), publish.sh
# (local repo -> beta), release.sh (beta -> stable), upload-repo.sh (to the server), stamp.sh
# (freshness stamps) and candidate.sh (argument handling and the result step).
#
# Every script runs as a copy in tmp_path/volla/tools, so VOLLA_DIR and with it build/ lie in
# tmp_path; the real build/ and the signing key are never touched. gpg is a stub whose
# "signature" is "sig:" + the file's sha256 (verify checks exactly that), rsync, ssh, curl,
# repo-add and unshare are stubs as well. bsdtar is the real one (it only reads).

import hashlib
import json
import os
import shutil
import subprocess
import time
import types

import pytest

from conftest import PKG, TOOLS, load_script
from release_sim import write_db, write_tar

FPR = "0123456789ABCDEF0123456789ABCDEF01234567"
SCRIPTS = ("lib.sh", "publish.sh", "release.sh", "upload-repo.sh", "stamp.sh", "candidate.sh")

GPG = r'''
last=${@: -1}
case " $* " in
*" --list-secret-keys "*)
	[[ -e $GNUPGHOME/secret ]] || exit 0  # like gpg: no keys, no output
	echo "sec:u:255:22:89ABCDEF01234567:1700000000::::::scESC:::+:::ed25519:::0:"
	echo "fpr:::::::::$(cat "$GNUPGHOME/secret"):" ;;
*" --verify "*)
	sig=${@: -2:1}
	[[ -f $sig && $(cat "$sig") == "sig:$(sha256sum < "$last" | cut -c1-64)" ]] ;;
*" --detach-sign "*)
	out=
	while (($#)); do [[ $1 == -o ]] && out=$2; shift; done
	[[ -e $GNUPGHOME/secret ]] || exit 2
	echo "sig:$(sha256sum < "$last" | cut -c1-64)" > "$out" ;;
*) exit 2 ;;
esac
'''

REPO_ADD = r'''
db= files=()
while (($#)); do
	case $1 in
	-q | --sign | --verify) ;;
	--key) shift ;;
	*) if [[ -z $db ]]; then db=$1; else files+=("$1"); fi ;;
	esac
	shift
done
for f in "${files[@]}"; do [[ -e $f ]] || { echo "repo-add: $f missing" >&2; exit 1; }; done
cp "$FAKE_DB" "$db"
fdb=${db%/*}/plinius.files.tar.gz
cp "$FAKE_DB" "$fdb"
for f in "$db" "$fdb"; do echo "sig:$(sha256sum < "$f" | cut -c1-64)" > "$f.sig"; done
'''


def sig(path):
    return "sig:" + hashlib.sha256(path.read_bytes()).hexdigest() + "\n"


class Repo:
    def __init__(self, tmp_path, fake_bin):
        self.tmp = tmp_path
        self.volla = tmp_path / "volla"
        self.tools = self.volla / "tools"
        self.tools.mkdir(parents=True)
        for s in SCRIPTS:
            shutil.copy(TOOLS / s, self.tools / s)
        self.build = self.volla / "build"
        self.local = self.build / "repo/local"
        self.pub = self.build / "repo/plinius"
        self.beta = self.pub / "beta/aarch64"
        self.stable = self.pub / "aarch64"
        self.gnupg = tmp_path / "gnupg"
        self.gnupg.mkdir()
        (self.gnupg / "secret").write_text(FPR)
        self.fake_bin = fake_bin
        fake_bin.stub("gpg", GPG)
        fake_bin.stub("repo-add", REPO_ADD)
        fake_bin.stub("rsync")
        fake_bin.stub("ssh")
        fake_bin.stub("curl", 'cat "$FAKE_REMOTE"')
        fake_bin.stub("unshare")
        fake_bin.stub("uname", '[[ $1 == -m ]] && echo aarch64')
        bsdtar = shutil.which("bsdtar")
        if not bsdtar:
            pytest.skip("bsdtar missing on the host")
        fake_bin.stub("bsdtar", f'exec {bsdtar} "$@"')
        fake_bin.stub("rmdir", f'exec {shutil.which("rmdir")} "$@"')
        self.freigabe(0)
        self.gate(0)

    def freigabe(self, status):
        f = self.tools / "freigabe.py"
        f.write_text(f"#!/bin/bash\n[[ $1 == --check ]] || exit 9\nexit {status}\n")
        f.chmod(0o755)

    def gate(self, status):
        """tools/test-gate.py as a stand-in: logs its arguments, answers check with status."""
        f = self.tools / "test-gate.py"
        f.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{self.tmp}/gate.log"\n'
                     f'[[ $1 == check ]] || exit 9\nexit {status}\n')
        f.chmod(0o755)

    def gate_calls(self):
        log = self.tmp / "gate.log"
        return log.read_text().splitlines() if log.exists() else []

    def local_repo(self, packages, keyring_fpr=FPR):
        """packages: {name: version}; plinius-keyring is added with keyring_fpr (None: left out)."""
        self.local.mkdir(parents=True, exist_ok=True)
        entries = {}
        for name, version in packages.items():
            fn = f"{name}-{version}-aarch64.pkg.tar.zst"
            (self.local / fn).write_bytes(f"package {name} {version}".encode())
            entries[f"{name}-{version}"] = {"FILENAME": fn, "NAME": name, "VERSION": version}
        if keyring_fpr is not None:
            fn = "plinius-keyring-1-1-any.pkg.tar.zst"
            write_tar(self.local / fn, {".PKGINFO": "pkgname = plinius-keyring\n",
                                        "usr/share/pacman/keyrings/plinius-trusted":
                                            f"{keyring_fpr}:4:\n"})
            entries["plinius-keyring-1-1"] = {"FILENAME": fn, "NAME": "plinius-keyring",
                                              "VERSION": "1-1"}
        write_db(self.local / "plinius-local.db.tar.gz", entries)
        return entries

    def env(self, **extra):
        values = dict(HOME=self.tmp, SIGN_GNUPG=self.gnupg,
                      FAKE_DB=self.local / "plinius-local.db.tar.gz")
        values.update(extra)
        return self.fake_bin.env(isolated=True, **values)

    def run(self, script, *args, **extra):
        return subprocess.run(["bash", str(self.tools / script), *args], env=self.env(**extra),
                              capture_output=True, text=True, timeout=60, cwd=self.tmp)

    def lib(self, code, **extra):
        return subprocess.run(["bash", "-c", f'set -euo pipefail\n. "{self.tools / "lib.sh"}"\n{code}'],
                              env=self.env(**extra), capture_output=True, text=True, timeout=60)

    def signed(self):
        return [c[c.index("-o") + 1] for c in self.fake_bin.calls("gpg") if "--detach-sign" in c]


@pytest.fixture
def repo(tmp_path, fake_bin):
    return Repo(tmp_path, fake_bin)


def ok(r):
    assert r.returncode == 0, f"exit {r.returncode}\n{r.stdout}\n{r.stderr}"
    return r


def fails(r, text=None):
    assert r.returncode != 0, f"unexpected success\n{r.stdout}\n{r.stderr}"
    if text:
        assert text in r.stderr, r.stderr
    return r


# --- lib.sh ------------------------------------------------------------------------------------

def test_lib_paths_follow_the_checkout(repo):
    r = ok(repo.lib('echo "$VOLLA_DIR $PUBREPO $LOCALREPO $SIGN_GNUPG"'))
    assert r.stdout.split() == [str(repo.volla), str(repo.pub), str(repo.local), str(repo.gnupg)]


def test_channel_dir(repo):
    r = ok(repo.lib('channel_dir stable /srv/repo; channel_dir beta /srv/repo'))
    assert r.stdout.split() == ["/srv/repo/aarch64", "/srv/repo/beta/aarch64"]
    r = ok(repo.lib('channel_dir beta "u@h:/r"', ARCH="x86_64"))
    assert r.stdout.strip() == "u@h:/r/beta/x86_64"
    fails(repo.lib("channel_dir nightly /srv/repo"), "unknown channel: nightly")


def test_unsupported_arch_stops(repo):
    fails(repo.lib("true", ARCH="riscv64"), "ARCH=riscv64 not supported")


def test_mirrorlist(repo):
    r = ok(repo.lib('mirrorlist; echo "first $MIRROR"', MIRRORS="https://a http://b"))
    assert r.stdout.splitlines() == ["Server = https://a/$arch/$repo", "Server = http://b/$arch/$repo",
                                     "first https://a"]
    r = ok(repo.lib("mirrorlist | head -1", ARCH="x86_64"))
    assert r.stdout.strip() == "Server = https://geo.mirror.pkgbuild.com/$repo/os/$arch"


def test_db_versions_sorted_by_name(repo, tmp_path):
    db = write_db(tmp_path / "x.db.tar.gz", {
        "zlib-1.3-1": {"NAME": "zlib", "VERSION": "1.3-1"},
        "avahi-1:0.9-2": {"NAME": "avahi", "VERSION": "1:0.9-2"},
        "Mesa-1-1": {"NAME": "Mesa", "VERSION": "1-1"}})
    r = ok(repo.lib(f'db_versions "{db}"'))
    assert r.stdout.splitlines() == ["Mesa 1-1", "avahi 1:0.9-2", "zlib 1.3-1"]


def test_sign_key(repo):
    assert ok(repo.lib("sign_key")).stdout.strip() == FPR
    assert ok(repo.lib("sign_key", SIGN_GNUPG=repo.tmp / "none")).stdout == ""


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def channel_with_database(tmp_path):
    """A channel directory as publish.sh leaves it: plinius.db(.sig) link to the tar.gz files."""
    out = tmp_path / "chan"
    out.mkdir()
    (out / "plinius.db.tar.gz").write_bytes(b"the database")
    (out / "plinius.db.tar.gz.sig").write_bytes(b"its signature")
    os.symlink("plinius.db.tar.gz", out / "plinius.db")
    os.symlink("plinius.db.tar.gz.sig", out / "plinius.db.sig")
    return out


def test_write_stamp(repo, tmp_path):
    out = channel_with_database(tmp_path)
    before = int(time.time())
    ok(repo.lib(f'write_stamp beta "{out}" {FPR}', ))
    fields = dict(line.split("=", 1) for line in (out / "plinius.stamp").read_text().splitlines())
    assert list(fields) == ["channel", "time", "db", "db_sha256", "sig_sha256"]
    assert fields["channel"] == "beta" and before <= int(fields["time"]) <= time.time()
    # It names the file plinius.db links to, what pacman downloads as plinius.db
    assert fields["db"] == "plinius.db.tar.gz"
    assert fields["db_sha256"] == sha256(out / "plinius.db.tar.gz")
    assert fields["sig_sha256"] == sha256(out / "plinius.db.tar.gz.sig")
    assert (out / "plinius.stamp.sig").read_text() == sig(out / "plinius.stamp")
    assert sorted(p.name for p in out.iterdir()) == ["plinius.db", "plinius.db.sig", "plinius.db.tar.gz",
                                                    "plinius.db.tar.gz.sig", "plinius.stamp",
                                                    "plinius.stamp.sig"]


@pytest.mark.parametrize("missing, error", [("plinius.db.tar.gz", "write_stamp: no database"),
                                            ("plinius.db", "write_stamp: no database"),
                                            ("plinius.db.tar.gz.sig", "write_stamp: no signature"),
                                            ("plinius.db.sig", "write_stamp: no signature")])
def test_write_stamp_needs_the_database_and_its_signature(repo, tmp_path, missing, error):
    # A stamp without its database would warn on every device; a dangling link counts as missing
    out = channel_with_database(tmp_path)
    (out / missing).unlink()
    fails(repo.lib(f'write_stamp stable "{out}" {FPR}'), error)
    assert not (out / "plinius.stamp").exists() and not (out / "plinius.stamp.sig").exists()
    assert repo.signed() == []


def test_stamp_of_write_stamp_passes_the_device_check(repo, tmp_path, monkeypatch):
    # What tools/lib.sh writes is what plinius-repo-stamp (plinius-software) reads: the database
    # pacman fetched as sync/plinius.db matches, another one does not. The fake gpg signs nothing
    # real, so only the signature check is left out.
    out = channel_with_database(tmp_path)
    ok(repo.lib(f'write_stamp stable "{out}" {FPR}'))
    stamp = load_script(PKG / "plinius-software" / "plinius-repo-stamp", "repo_stamp_for_tools")
    conf = tmp_path / "pacman.conf"
    conf.write_text("[plinius]\nServer = https://plinius.example/repo/$arch\n")
    files = {"plinius.stamp": (out / "plinius.stamp").read_bytes(), "plinius.stamp.sig": b"sig"}
    monkeypatch.setattr(stamp, "PACMAN_CONF", str(conf))
    monkeypatch.setattr(stamp, "SYSTEM_STATE", str(tmp_path / "state" / "repo-stamp"))
    monkeypatch.setattr(stamp, "signed", lambda data, signature: data == files["plinius.stamp"])
    monkeypatch.setattr(stamp, "fetch", lambda server: [files["plinius.stamp"], files["plinius.stamp.sig"]])
    monkeypatch.setattr(stamp, "time", types.SimpleNamespace(time=time.time, sleep=lambda s: None))
    monkeypatch.setattr(stamp.os, "geteuid", lambda: 1000)
    sync = tmp_path / "sync"
    sync.mkdir()
    shutil.copy(out / "plinius.db.tar.gz", sync / "plinius.db")
    shutil.copy(out / "plinius.db.tar.gz.sig", sync / "plinius.db.sig")
    assert stamp.check(str(sync / "plinius.db"))["result"] == "ok"
    (sync / "plinius.db").write_bytes(b"an older database")
    assert stamp.check(str(sync / "plinius.db"))["result"] == "mismatch"
    assert json.dumps(stamp.check(str(sync / "plinius.db")))  # the verdict is JSON for plinius-software


@pytest.mark.parametrize("hour,battery,jobs", [("21", True, 6), ("05", True, 6), ("12", True, 8),
                                               ("21", False, 8), ("08", True, 8)])
def test_build_jobs(repo, fake_bin, tmp_path, hour, battery, jobs):
    # The battery test reads /sys/class/power_supply: a copy of the function with that path moved
    lib = (repo.tools / "lib.sh").read_text()
    assert "/sys/class/power_supply/BAT*" in lib
    ps = tmp_path / "power_supply"
    ps.mkdir()
    if battery:
        (ps / "BAT0").mkdir()
    (repo.tools / "lib.sh").write_text(lib.replace("/sys/class/power_supply/BAT*", f"{ps}/BAT*"))
    fake_bin.stub("nproc", "echo 8")
    fake_bin.stub("date", f'[[ $1 == +%H ]] && echo {hour}')
    assert ok(repo.lib("build_jobs")).stdout.strip() == str(jobs)
    fake_bin.stub("nproc", "echo 1")
    assert ok(repo.lib("build_jobs")).stdout.strip() == "1"


# --- publish.sh ---------------------------------------------------------------------------------

def test_publish_usage(repo):
    fails(repo.run("publish.sh", "--beta"), "usage: tools/publish.sh")


def test_publish_needs_the_key(repo):
    (repo.gnupg / "secret").unlink()
    repo.local_repo({"foo": "1-1"})
    fails(repo.run("publish.sh"), "no signing key")
    assert not repo.pub.exists()


def test_publish_needs_a_local_repo(repo):
    fails(repo.run("publish.sh"), "local repo empty")


def test_publish_needs_the_keyring_package_with_this_key(repo):
    repo.local_repo({"foo": "1-1"}, keyring_fpr=None)
    fails(repo.run("publish.sh"), "plinius-keyring missing")
    repo.local_repo({"foo": "1-1"}, keyring_fpr="F" * 40)
    fails(repo.run("publish.sh"), "does not contain the key")
    assert not repo.pub.exists()


def test_publish_refuses_when_the_approval_does_not_match(repo):
    repo.local_repo({"foo": "1-1"})
    repo.freigabe(1)
    fails(repo.run("publish.sh"), "approval does not match")
    assert not repo.pub.exists()
    r = ok(repo.run("publish.sh", "--force"))
    assert "publishing anyway" in r.stderr
    assert (repo.beta / "plinius.db").exists()


def test_publish_asks_the_test_gate_about_beta(repo):
    repo.local_repo({"foo": "1-1"})
    ok(repo.run("publish.sh"))
    assert repo.gate_calls() == [f"check {repo.local / 'plinius-local.db.tar.gz'} {repo.beta}"]


def test_publish_refuses_what_has_not_passed_the_test_gate(repo):
    repo.local_repo({"foo": "1-1"})
    repo.gate(1)
    fails(repo.run("publish.sh"), "have not passed the test gate, or use --untested")
    assert not repo.pub.exists()
    fails(repo.run("publish.sh", "--stable"), "have not passed the test gate")
    assert not repo.pub.exists()
    # The approval is checked first: a mismatch there stops before the gate is asked
    repo.freigabe(1)
    (repo.tmp / "gate.log").unlink()
    fails(repo.run("publish.sh"), "approval does not match")
    assert repo.gate_calls() == []


def test_publish_untested_on_purpose(repo):
    repo.local_repo({"foo": "1-1"})
    repo.gate(1)
    r = ok(repo.run("publish.sh", "--untested"))
    assert "have not passed the test gate (--untested)" in r.stderr
    assert (repo.beta / "plinius.db").exists()
    # --force covers the approval only, not the gate
    fails(repo.run("publish.sh", "--force"), "have not passed the test gate")


def test_publish_to_beta(repo):
    repo.local_repo({"foo": "1-1", "bar": "2-1"})
    repo.beta.mkdir(parents=True)
    (repo.beta / "old-0-1-aarch64.pkg.tar.zst").write_text("old")
    (repo.beta / "old-0-1-aarch64.pkg.tar.zst.sig").write_text("old sig")
    r = ok(repo.run("publish.sh"))
    pkgs = ["bar-2-1-aarch64.pkg.tar.zst", "foo-1-1-aarch64.pkg.tar.zst",
            "plinius-keyring-1-1-any.pkg.tar.zst"]
    for p in pkgs:
        assert (repo.beta / p).read_bytes() == (repo.local / p).read_bytes()
        assert (repo.beta / (p + ".sig")).read_text() == sig(repo.beta / p)
    assert not (repo.beta / "old-0-1-aarch64.pkg.tar.zst").exists()
    assert not (repo.beta / "old-0-1-aarch64.pkg.tar.zst.sig").exists()
    for f in ("plinius.db.tar.gz", "plinius.files.tar.gz"):
        assert (repo.beta / (f + ".sig")).read_text() == sig(repo.beta / f)
    assert os.readlink(repo.beta / "plinius.db") == "plinius.db.tar.gz"
    assert os.readlink(repo.beta / "plinius.files.sig") == "plinius.files.tar.gz.sig"
    assert (repo.beta / "plinius.stamp").read_text().startswith("channel=beta\n")
    assert not [p for p in repo.beta.iterdir() if p.name.startswith(".db.") or p.name.endswith(".part")]
    assert sorted(os.path.basename(s) for s in repo.signed()) == sorted(
        [p + ".sig" for p in pkgs] + ["plinius.stamp.sig.part"])
    assert not repo.stable.exists()
    assert "Published to beta: 3 packages" in r.stderr
    # A second run signs no package again
    repo.fake_bin.path.joinpath("gpg.log").unlink()
    ok(repo.run("publish.sh"))
    assert [os.path.basename(s) for s in repo.signed()] == ["plinius.stamp.sig.part"]


def test_publish_resigns_a_changed_package(repo):
    repo.local_repo({"foo": "1-1"})
    ok(repo.run("publish.sh"))
    (repo.local / "foo-1-1-aarch64.pkg.tar.zst").write_bytes(b"rebuilt with the same version")
    repo.fake_bin.path.joinpath("gpg.log").unlink()
    ok(repo.run("publish.sh"))
    assert (repo.beta / "foo-1-1-aarch64.pkg.tar.zst").read_bytes() == b"rebuilt with the same version"
    assert (repo.beta / "foo-1-1-aarch64.pkg.tar.zst.sig").read_text() == \
        sig(repo.beta / "foo-1-1-aarch64.pkg.tar.zst")
    assert "foo-1-1-aarch64.pkg.tar.zst.sig" in [os.path.basename(s) for s in repo.signed()]


def test_publish_links_a_package_stable_already_has(repo):
    repo.local_repo({"foo": "1-1"})
    ok(repo.run("publish.sh", "--stable"))
    # beta and stable share the file; a fresh beta takes it from stable without signing
    shutil.rmtree(repo.beta)
    repo.fake_bin.path.joinpath("gpg.log").unlink()
    ok(repo.run("publish.sh"))
    p = "foo-1-1-aarch64.pkg.tar.zst"
    assert (repo.beta / p).samefile(repo.stable / p)
    assert (repo.beta / (p + ".sig")).samefile(repo.stable / (p + ".sig"))
    assert [os.path.basename(s) for s in repo.signed()] == ["plinius.stamp.sig.part"]


def test_publish_stable_releases_right_away(repo):
    repo.local_repo({"foo": "1-1"})
    r = ok(repo.run("publish.sh", "--stable"))
    assert "Released to stable: 2 packages" in r.stderr
    assert (repo.stable / "plinius.stamp").read_text().startswith("channel=stable\n")


# --- release.sh ---------------------------------------------------------------------------------

def published(repo, packages):
    repo.local_repo(packages)
    ok(repo.run("publish.sh"))


def test_release_usage(repo):
    fails(repo.run("release.sh", "--now"), "usage: tools/release.sh")


def test_release_needs_beta(repo):
    fails(repo.run("release.sh"), "nothing published to beta")


def test_release_takes_beta_over_to_stable(repo):
    published(repo, {"foo": "1-1"})
    ok(repo.run("release.sh"))
    published(repo, {"foo": "2-1"})
    r = ok(repo.run("release.sh", "--dry-run"))
    assert "    foo: 1-1 → 2-1" in r.stderr
    assert (repo.stable / "foo-1-1-aarch64.pkg.tar.zst").exists()  # dry run changed nothing
    assert not (repo.stable / "foo-2-1-aarch64.pkg.tar.zst").exists()
    (repo.stable / "plinius.stamp").read_text()  # the stamp exists before the release
    r = ok(repo.run("release.sh"))
    p = "foo-2-1-aarch64.pkg.tar.zst"
    assert (repo.stable / p).samefile(repo.beta / p)
    assert (repo.stable / (p + ".sig")).samefile(repo.beta / (p + ".sig"))
    assert not (repo.stable / "foo-1-1-aarch64.pkg.tar.zst").exists()
    # The database is a copy, not a link: publish.sh rebuilds beta's next time
    db = repo.stable / "plinius.db.tar.gz"
    assert db.read_bytes() == (repo.beta / "plinius.db.tar.gz").read_bytes()
    assert not db.samefile(repo.beta / "plinius.db.tar.gz")
    assert os.readlink(repo.stable / "plinius.db") == "plinius.db.tar.gz"
    new_stamp = (repo.stable / "plinius.stamp").read_text()
    assert new_stamp.startswith("channel=stable\n")
    assert (repo.stable / "plinius.stamp.sig").read_text() == sig(repo.stable / "plinius.stamp")
    assert not [x for x in repo.stable.iterdir() if x.name.startswith(".")]
    r = ok(repo.run("release.sh", "--dry-run"))
    assert "(no changes)" in r.stderr


def test_release_first_time(repo):
    published(repo, {"foo": "1-1"})
    r = ok(repo.run("release.sh", "--dry-run"))
    assert "stable is new: 2 packages" in r.stderr
    assert not repo.stable.exists()


def test_release_refuses_unsigned_beta(repo):
    published(repo, {"foo": "1-1"})
    (repo.beta / "foo-1-1-aarch64.pkg.tar.zst.sig").write_text("broken")
    fails(repo.run("release.sh"), "foo-1-1-aarch64.pkg.tar.zst in beta without a valid signature")
    (repo.beta / "plinius.db.tar.gz.sig").unlink()
    fails(repo.run("release.sh"), "beta database without a valid signature")
    assert not repo.stable.exists()


def test_release_compares_with_the_server(repo, tmp_path):
    published(repo, {"foo": "1-1"})
    other = tmp_path / "other.db"
    other.write_bytes(b"what the server has")
    fails(repo.run("release.sh", REPO_URL="https://h/repo/", FAKE_REMOTE=other),
          "differs from")
    assert not repo.stable.exists()
    ok(repo.run("release.sh", REPO_URL="https://h/repo/", FAKE_REMOTE=repo.beta / "plinius.db.tar.gz"))
    curl = repo.fake_bin.calls("curl")[-1]
    assert curl[-1] == "https://h/repo/beta/aarch64/plinius.db.tar.gz"
    assert curl[curl.index("-A") + 1] == "Plinius Arch Linux"
    assert (repo.stable / "plinius.db").exists()


def test_release_needs_the_key_for_the_stamp(repo):
    published(repo, {"foo": "1-1"})
    (repo.gnupg / "secret").unlink()
    fails(repo.run("release.sh"), "no signing key")


# --- upload-repo.sh -----------------------------------------------------------------------------

def test_upload_needs_a_target(repo):
    r = fails(repo.run("upload-repo.sh"))
    assert "REPO_TARGET" in r.stderr
    assert repo.fake_bin.calls("rsync") == []


def test_upload_both_channels_packages_first_database_last(repo):
    published(repo, {"foo": "1-1"})
    ok(repo.run("release.sh"))
    ok(repo.run("upload-repo.sh", REPO_TARGET="u@h:/srv/repo", REPO_SSH_PORT="2222"))
    calls = repo.fake_bin.calls("rsync")
    assert len(calls) == 6
    for c in calls:
        assert c[:7] == ["-rltz", "--mkpath", "--no-owner", "--no-group", "--chmod=D755,F644",
                         "--partial-dir=.upload", "-e"]
        assert c[7] == "ssh -p 2222 -o BatchMode=yes"
    beta, stable = calls[:3], calls[3:]
    assert beta[0][8:] == ["--link-dest=../../aarch64", "--exclude=plinius.db*",
                           "--exclude=plinius.files*", "--exclude=plinius.stamp*",
                           f"{repo.beta}/", "u@h:/srv/repo/beta/aarch64/"]
    # Database, its signature and the stamp naming it switch over together (--delay-updates)
    assert beta[1][8:] == ["--delay-updates", f"{repo.beta}/", "u@h:/srv/repo/beta/aarch64/"]
    assert beta[2][8:] == ["--delete-after", f"{repo.beta}/", "u@h:/srv/repo/beta/aarch64/"]
    assert stable[0][8] == "--link-dest=../beta/aarch64"
    assert stable[0][-2:] == [f"{repo.stable}/", "u@h:/srv/repo/aarch64/"]
    assert stable[1][8:] == ["--delay-updates", f"{repo.stable}/", "u@h:/srv/repo/aarch64/"]
    assert stable[2][8] == "--delete-after"


def test_upload_skips_an_unpublished_channel(repo):
    published(repo, {"foo": "1-1"})
    r = ok(repo.run("upload-repo.sh", REPO_TARGET="u@h:/r"))
    assert "stable: nothing published" in r.stderr
    assert all(c[-1] == "u@h:/r/beta/aarch64/" for c in repo.fake_bin.calls("rsync"))
    assert repo.fake_bin.calls("rsync")[0][7] == "ssh -p 22 -o BatchMode=yes"


def test_upload_with_nothing_published_stops(repo):
    fails(repo.run("upload-repo.sh", REPO_TARGET="u@h:/r"), "nothing published")
    assert repo.fake_bin.calls("rsync") == []


# --- stamp.sh -----------------------------------------------------------------------------------

def test_stamp_renews_every_published_channel(repo):
    published(repo, {"foo": "1-1"})
    ok(repo.run("release.sh"))
    for d in (repo.beta, repo.stable):
        (d / "plinius.stamp").write_text("channel=x\ntime=1\n")
    r = ok(repo.run("stamp.sh"))
    assert (repo.beta / "plinius.stamp").read_text().startswith("channel=beta\ntime=")
    assert (repo.stable / "plinius.stamp").read_text().startswith("channel=stable\ntime=")
    assert (repo.stable / "plinius.stamp.sig").read_text() == sig(repo.stable / "plinius.stamp")
    # Each stamp names the database of its own channel
    for d in (repo.beta, repo.stable):
        assert f"db_sha256={sha256(d / 'plinius.db.tar.gz')}\n" in (d / "plinius.stamp").read_text()
    assert "Next: tools/upload-repo.sh" in r.stderr


def test_stamp_only_where_something_is_published(repo):
    published(repo, {"foo": "1-1"})
    ok(repo.run("stamp.sh"))
    assert not repo.stable.exists()


def test_stamp_needs_the_key_and_a_published_channel(repo):
    fails(repo.run("stamp.sh"), "nothing published")
    (repo.gnupg / "secret").unlink()
    fails(repo.run("stamp.sh"), "no signing key")


# --- candidate.sh: arguments and the result ------------------------------------------------------

def candidate_world(repo):
    (repo.build / "chroot/root/usr").mkdir(parents=True)
    repo.local_repo({"foo": "1.2-3"})
    (repo.local / "plinius-local.db").symlink_to("plinius-local.db.tar.gz")
    (repo.volla / "image").mkdir()
    (repo.volla / "image/mkimage.sh").write_text("packages=(\n\tbase phosh\n\tmodemmanager\n)\n")
    (repo.volla / "pkgbuilds/plinius-apps").mkdir(parents=True)
    (repo.volla / "pkgbuilds/plinius-apps/recommended.list").write_text("# apps\nsnapshot\nfirefox\n")
    # freigabe as a module (the file conflicts step imports it) and a fixed conflict report
    (repo.tools / "freigabe.py").write_text(f'''
import os
ROOT = {str(repo.volla)!r}
CACHE = os.path.join(ROOT, "build/cache")
REPOS = ("core",)
def fetch(repo, kind, max_age):
    pass
def our_packages():
    return {{"foo": (os.path.join(ROOT, "build/repo/local/foo-1.2-3-aarch64.pkg.tar.zst"), "aarch64", "foo")}}
''')
    (repo.tools / "check-conflicts.py").write_text(
        'print("foo-1.2-3-aarch64.pkg.tar.zst: usr/bin/x  <->  extra/x-1-1")\nprint("1 conflict(s)")\n')


def test_candidate_needs_the_chroot_and_the_local_repo(repo):
    fails(repo.run("candidate.sh"), "build chroot missing")
    (repo.build / "chroot/root/usr").mkdir(parents=True)
    fails(repo.run("candidate.sh"), "no local repo")


def test_candidate_result_new_findings_then_accepted(repo):
    candidate_world(repo)
    r = repo.run("candidate.sh")
    assert r.returncode == 1, r.stderr
    assert "NEW (fix, or accept" in r.stderr
    assert "conflict foo: usr/bin/x" in r.stderr  # without the version
    # The chroot steps ran in a namespace (stubbed here) with our packages and the image selection
    ns = [c for c in repo.fake_bin.calls("unshare") if "--fork" in c]
    assert len(ns) == 1
    args = ns[0][ns[0].index("ns_run") + 1:]
    assert args[0] == str(repo.build / "chroot/root") and args[2] == str(repo.local)
    assert args[6:] == ["foo", "plinius-keyring", "--", "base", "phosh", "modemmanager", "firefox"]
    report = sorted((repo.build / "candidate").glob("*-aarch64.txt"))[-1].read_text()
    assert "== 2. File conflicts" in report and "== Result" in report
    # Accepting makes the same findings known: the next run is green
    r = ok(repo.run("candidate.sh", "--accept"))
    assert (repo.tools / "candidate-known-aarch64.txt").read_text() == "conflict foo: usr/bin/x\n"
    r = ok(repo.run("candidate.sh"))
    assert "green: nothing new" in r.stderr


def test_candidate_accept_without_a_run(repo):
    (repo.build / "chroot/root/usr").mkdir(parents=True)
    repo.local_repo({"foo": "1-1"})
    (repo.local / "plinius-local.db").symlink_to("plinius-local.db.tar.gz")
    fails(repo.run("candidate.sh", "--accept"), "no run to accept yet")


def test_in_chroot_gives_pacman_our_user_agent(repo):
    """pacman in the build chroots (build.sh, chroot-init.sh, mkimage.sh) says who asks: in_chroot
    clears the environment but passes HTTP_USER_AGENT, which libalpm sends as User-Agent."""
    # unshare and chroot without namespaces: the script runs here, chroot drops its root path
    repo.fake_bin.stub("unshare", 'while [[ $1 != -- ]]; do shift; done; shift; exec "$@"')
    repo.fake_bin.stub("chroot", 'shift; exec "$@"')
    r = ok(repo.lib("ns_run 'in_chroot /r printenv HTTP_USER_AGENT HOME; in_chroot /r printenv SECRET || echo gone'",
                    SECRET="from the host"))
    assert r.stdout.splitlines() == ["Plinius Arch Linux", "/root", "gone"]
