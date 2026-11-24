# tools/freigabe.py, the release gate: which packages get coupled (linked libraries, kern.list),
# at which version, and when --check or the approval refuses.
#
# The mirror is a directory of synthetic databases (urlretrieve is replaced), the local repo and
# PKGBUILD lie in tmp_path, readelf is a stub that reads "NEEDED" lines from the fake ELF files.

import os
import shutil
import time

import pytest

from conftest import TOOLS, load_script
from release_sim import write_db, write_tar

PKGBUILD = """# Maintainer: misc-de
pkgname=plinius-freigabe
pkgver=20260101.0000
pkgrel=1
conflicts_aarch64=(
{aarch64})
conflicts_x86_64=(
\t"glibc>2.40-1"  # core list
)
"""


def elf(*libs):
    return "\x7fELF\n" + "".join(f"NEEDED {l}\n" for l in libs)


def buildinfo(*installed):
    return "format = 2\npkgname = x\n" + "".join(f"installed = {i}\n" for i in installed)


class World:
    def __init__(self, tmp_path, monkeypatch, fake_bin):
        monkeypatch.delenv("ARCH", raising=False)
        self.fg = load_script(TOOLS / "freigabe.py")
        self.tmp = tmp_path
        self.repo = tmp_path / "repo"
        self.mirror = tmp_path / "mirror"
        self.mirror.mkdir()
        self.pkgbuild = tmp_path / "PKGBUILD"
        self.kern = tmp_path / "kern.list"
        self.downloads = []
        for name, value in (("REPO", str(self.repo)), ("PKGBUILD", str(self.pkgbuild)),
                            ("KERN", str(self.kern)), ("CACHE", str(tmp_path / "cache"))):
            monkeypatch.setattr(self.fg, name, value)

        def urlretrieve(url, path):
            self.downloads.append(url)
            shutil.copy(self.mirror / url.rsplit("/", 1)[1], path)
        monkeypatch.setattr(self.fg.urllib.request, "urlretrieve", urlretrieve)
        fake_bin.stub("readelf", '''[[ $1 == -d ]] || exit 1
grep -a '^NEEDED ' "$2" | while read -r _ lib; do
	echo " 0x0000000000000001 (NEEDED)             Shared library: [$lib]"
done''')
        monkeypatch.setenv("PATH", f"{fake_bin.path}:{os.environ['PATH']}")
        if not shutil.which("bsdtar"):
            pytest.skip("bsdtar missing on the host")
        self.ours = {}
        self.kern.write_text("# core packages\nglibc\n\nsystemd  # init\n")

    def package(self, name, version, arch="aarch64", files=None, built=(), base=None):
        filename = f"{name}-{version}-{arch}.pkg.tar.zst"
        members = {".PKGINFO": f"pkgname = {name}\n", ".BUILDINFO": buildinfo(*built)}
        members.update(files or {})
        write_tar(self.repo / filename, members)
        fields = {"FILENAME": filename, "NAME": name, "VERSION": version, "ARCH": arch}
        if base:
            fields["BASE"] = base
        self.ours[f"{name}-{version}"] = fields

    def mirror_state(self, versions, files):
        """versions: {name: version} over core/extra/alarm; files: {repo: {entry: [paths]}}."""
        names = list(versions)
        for i, repo in enumerate(("core", "extra", "alarm")):
            part = {f"{n}-{versions[n]}": {"NAME": n, "VERSION": versions[n]}
                    for n in names[i::3]}
            if repo == "extra":
                part["findnewest-0-0"] = {}  # ALARM's broken entry without fields
            write_db(self.mirror / f"{repo}.db", part)
            write_db(self.mirror / f"{repo}.files.tar.gz",
                     {e: {"FILES": f} for e, f in files.get(repo, {}).items()}, member="files")

    def approval(self, lines):
        self.pkgbuild.write_text(PKGBUILD.format(aarch64="".join(f"\t{l}\n" for l in lines)))

    def run(self, monkeypatch, *args):
        write_db(self.repo / "plinius-local.db", self.ours)
        monkeypatch.setattr(self.fg.sys, "argv", ["freigabe.py", *args])
        return self.fg.main()


@pytest.fixture
def world(tmp_path, monkeypatch, fake_bin):
    w = World(tmp_path, monkeypatch, fake_bin)
    w.mirror_state(
        {"glibc": "2.42-1", "alsa-lib": "1.2.16.1-1", "systemd": "259.1-1", "avahi": "1:0.9rc5-2",
         "libfoo-utils": "3.0-1", "cef": "140-1"},
        {"core": {"glibc-2.42-1": ["usr/lib/", "usr/lib/libc.so.6", "usr/lib/libm.so.6"],
                  "systemd-259.1-1": ["usr/lib/libsystemd.so.0"]},
         "extra": {"alsa-lib-1.2.16.1-1": ["usr/lib/libasound.so.2", "usr/lib/libasound.so.2.0.0"],
                   "avahi-1:0.9rc5-2": ["usr/lib/libavahi-client.so.3"],
                   "cef-140-1": ["usr/lib/cef/libcef.so"]},
         "alarm": {"libfoo-utils-3.0-1": ["usr/lib/libfoo.so.1"]}})
    return w


GOOD = ['"alsa-lib>1.2.16.1-1"  # linked by audio-hal-bridge',
        '"avahi>1:0.9rc5-2"  # linked by cups-plinius',
        '"glibc>2.42-1"  # linked by audio-hal-bridge',
        '"systemd>259.1-1"  # core list']


def standard_packages(w, alsa_built="1.2.16.1-1"):
    w.package("audio-hal-bridge", "0.5-1",
              files={"usr/bin/audio-hal-bridge": elf("libasound.so.2", "libc.so.6"),
                     "usr/share/doc/README": "NEEDED libnot.so.1\n"},
              built=(f"alsa-lib-{alsa_built}-aarch64", "glibc-2.42-1-aarch64"))
    w.package("cups-plinius", "2.5-2",
              files={"usr/lib/cups/backend/x": elf("libavahi-client.so.3", "libplinius.so.1")},
              built=("avahi-1:0.9rc5-2-aarch64", "lib32-foo-bar-baz-1.0-1-any"))
    # Our own library: what links it is not coupled to ALARM through it
    w.package("libplinius", "1.0-1", files={"usr/lib/libplinius.so.1": elf("libc.so.6")},
              built=("glibc-2.42-1-aarch64",))
    # Not ELF-linked through the mirror: arch any and debug packages are skipped
    w.package("plinius-theme", "1-1", arch="any", files={"usr/bin/x": elf("libfoo.so.1")},
              built=("libfoo-utils-3.0-1-aarch64",))
    w.package("audio-hal-bridge-debug", "0.5-1", files={"usr/lib/debug/x": elf("libfoo.so.1")},
              built=("libfoo-utils-3.0-1-aarch64",))
    # Linked against a library that was not installed at build time (built in the tree): skipped;
    # and a library in a subdirectory of /usr/lib, which the linker does not search
    w.package("webview", "1-1", files={"usr/bin/webview": elf("libfoo.so.1", "libcef.so")},
              built=("cef-140-1-aarch64",))


# --- the pieces -------------------------------------------------------------------------------

def test_built_against_splits_names_with_hyphens_and_epochs(world, tmp_path):
    p = write_tar(tmp_path / "p.pkg.tar.zst", {".BUILDINFO": buildinfo(
        "lib32-foo-bar-1.2-3-x86_64", "avahi-1:0.9rc5-2-aarch64", "glibc-2.42-1-aarch64")})
    assert world.fg.built_against(str(p)) == {
        "lib32-foo-bar": "1.2-3", "avahi": "1:0.9rc5-2", "glibc": "2.42-1"}


def test_needed_reads_only_elf_files(world, tmp_path):
    p = write_tar(tmp_path / "p.pkg.tar.zst", {
        "usr/bin/a": elf("liba.so.1"), "usr/lib/b.so": elf("libb.so.2", "liba.so.1"),
        "usr/share/text": "NEEDED libnot.so.1\n"})
    assert world.fg.needed(str(p)) == {"liba.so.1", "libb.so.2"}


def test_library_owners_only_directly_in_usr_lib(world):
    owners = world.fg.library_owners()
    assert owners["libc.so.6"] == "glibc"
    assert owners["libasound.so.2"] == "alsa-lib"
    assert owners["libavahi-client.so.3"] == "avahi"  # entry name with an epoch
    assert "libcef.so" not in owners


def test_versions_skip_broken_entries(world):
    v = world.fg.versions()
    assert v["avahi"] == "1:0.9rc5-2" and v["libfoo-utils"] == "3.0-1"
    assert "findnewest" not in v and "" not in v


def test_files_databases_are_cached_for_a_day_and_package_databases_never(world):
    world.fg.library_owners()
    world.fg.versions()
    first = list(world.downloads)
    world.fg.library_owners()
    world.fg.versions()
    again = world.downloads[len(first):]
    assert sorted(again) == sorted(u for u in first if u.endswith(".db"))
    # An old cache is fetched again
    old = time.time() - 2 * 86400
    for f in (world.tmp / "cache").iterdir():
        os.utime(f, (old, old))
    n = len(world.downloads)
    world.fg.library_owners()
    assert len(world.downloads) == n + 3
    assert all(u.startswith("https://de3.mirror.archlinuxarm.org/aarch64/") for u in world.downloads)


# --- main: --check and writing the approval -----------------------------------------------------

def test_check_passes_when_the_approval_matches(world, monkeypatch):
    standard_packages(world)
    world.approval(GOOD)
    assert world.run(monkeypatch, "--check") is None


def test_check_compares_limits_not_comments(world, monkeypatch):
    standard_packages(world)
    world.approval([l.split("#")[0].rstrip() + "  # old comment" for l in reversed(GOOD)])
    assert world.run(monkeypatch, "--check") is None


def test_check_names_missing_and_outdated_limits(world, monkeypatch, capsys):
    standard_packages(world)
    world.approval([GOOD[0], '"glibc>2.41-1"  # linked by audio-hal-bridge', GOOD[3],
                    '"dropped>1-1"  # core list'])
    with pytest.raises(SystemExit) as e:
        world.run(monkeypatch, "--check")
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert 'approval aarch64: missing "avahi>1:0.9rc5-2"' in err
    assert 'approval aarch64: missing "glibc>2.42-1"' in err
    assert 'approval aarch64: outdated "glibc>2.41-1"' in err
    assert 'approval aarch64: outdated "dropped>1-1"' in err


def test_check_fails_when_a_package_was_built_against_another_state(world, monkeypatch, capsys):
    # The approval matches the mirror, but audio-hal-bridge was built against an older alsa-lib
    standard_packages(world, alsa_built="1.2.15-1")
    world.approval(GOOD)
    with pytest.raises(SystemExit) as e:
        world.run(monkeypatch, "--check")
    assert e.value.code == 1
    assert ("audio-hal-bridge was built against a different state (alsa-lib 1.2.15-1 → 1.2.16.1-1)"
            in capsys.readouterr().err)


def test_writing_the_approval(world, monkeypatch, capsys):
    standard_packages(world)
    world.approval(['"old>1-1"  # core list'])
    world.run(monkeypatch)
    text = world.pkgbuild.read_text()
    block = text.split("conflicts_aarch64=(\n", 1)[1].split("\n)\n", 1)[0]
    assert block.splitlines() == ["\t" + l for l in GOOD]
    # The other architecture's block and everything else stays
    assert 'conflicts_x86_64=(\n\t"glibc>2.40-1"  # core list\n)\n' in text
    assert "pkgname=plinius-freigabe\n" in text
    pkgver = [l for l in text.splitlines() if l.startswith("pkgver=")]
    assert len(pkgver) == 1 and pkgver[0] != "pkgver=20260101.0000"
    assert pkgver[0][len("pkgver="):].replace(".", "").isdigit()
    out = capsys.readouterr()
    assert "4 coupled packages approved for aarch64" in out.out
    assert "skipped, not in aarch64" not in out.err
    # What was written passes the check
    assert world.run(monkeypatch, "--check") is None


def test_stale_build_writes_no_approval_without_force(world, monkeypatch, capsys):
    standard_packages(world, alsa_built="1.2.15-1")
    world.approval(['"old>1-1"  # core list'])
    before = world.pkgbuild.read_text()
    with pytest.raises(SystemExit) as e:
        world.run(monkeypatch)
    assert "No approval written" in str(e.value.code)
    assert world.pkgbuild.read_text() == before
    assert "tools/build.sh audio-hal-bridge" in capsys.readouterr().err
    world.run(monkeypatch, "--force")
    assert '"alsa-lib>1.2.16.1-1"' in world.pkgbuild.read_text()


def test_stale_build_names_the_pkgbase(world, monkeypatch, capsys):
    world.package("pipewire-audio-plinius", "1.4-1", base="pipewire-plinius",
                  files={"usr/lib/x.so": elf("libasound.so.2")},
                  built=("alsa-lib-1.2.15-1-aarch64",))
    world.approval([])
    with pytest.raises(SystemExit):
        world.run(monkeypatch)
    assert "tools/build.sh pipewire-plinius" in capsys.readouterr().err


def test_core_list_package_missing_on_the_mirror_is_skipped(world, monkeypatch, capsys):
    standard_packages(world)
    world.kern.write_text("glibc\nsystemd\nnot-in-alarm\n")
    world.approval(GOOD)
    assert world.run(monkeypatch, "--check") is None
    assert "skipped, not in aarch64: not-in-alarm" in capsys.readouterr().err


def test_unsupported_arch_stops(monkeypatch):
    monkeypatch.setenv("ARCH", "riscv64")
    with pytest.raises(SystemExit) as e:
        load_script(TOOLS / "freigabe.py", name="freigabe_riscv")
    assert "not supported" in str(e.value.code)


def test_x86_64_uses_its_own_repo_and_mirror(monkeypatch):
    monkeypatch.setenv("ARCH", "x86_64")
    fg = load_script(TOOLS / "freigabe.py", name="freigabe_x86")
    assert fg.REPO.endswith("/build/repo/local-x86_64")
    assert fg.REPOS == ("core", "extra")
    assert fg.MIRROR.format(repo="core") == "https://geo.mirror.pkgbuild.com/core/os/x86_64/core"
