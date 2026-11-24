# tools/kernel-cves/plinius-kernel-cves: which CNA records count for a phone kernel (version
# ranges, built files, fixes already in the tree, fixes not taken) and the CVSS score. The CNA
# repository is a synthetic directory; git is replaced for the one rev-parse it runs.

import json
import subprocess
import sys
import types

import pytest

from conftest import TOOLS, load_script

SCRIPT = TOOLS / "kernel-cves" / "plinius-kernel-cves"
K = (6, 1, 145)


@pytest.fixture(scope="module")
def kc():
    return load_script(SCRIPT, name="plinius_kernel_cves")


@pytest.mark.parametrize("text,version", [
    ("6.1.145", (6, 1, 145)), ("6.2", (6, 2, None)), ("6.2-rc3", (6, 2, None)), ("0", None),
    ("", None), (None, None), ("6.1.145-gabc123", (6, 1, 145)),
])
def test_parse_version(kc, text, version):
    assert kc.parse_version(text) == version


@pytest.mark.parametrize("pairs,hit", [
    # fixed later in the kernel's own branch
    ([("6.1.100", "6.1.150")], ("6.1.150", True)),
    ([("6.1.100", "6.1.140")], None),
    ([("6.1.150", "6.1.160")], None),  # introduced after this kernel
    ([("0", "6.1.150")], ("6.1.150", True)),
    ([("5.10", "6.1.150")], ("6.1.150", True)),
    # two separate ranges in the same branch
    ([("6.1.10", "6.1.20"), ("6.1.100", "6.1.150")], ("6.1.150", True)),
    # introduced in this branch and never fixed here
    ([("6.1.100", "0")], (None, False)),
    # no pair for the branch: the bug predates it, the fix came with a later mainline release
    ([("4.19", "6.5")], ("6.5", False)),
    ([("4.19", "6.0")], None),  # fixed before the branch was made
    ([("6.2", "6.5")], None),  # introduced after the branch
    ([("4.19", "0")], (None, False)),  # not fixed anywhere
    # fixes only in newer stable branches: the oldest one is named, not as a fix of this branch
    ([("4.19", "6.6.30"), ("4.19", "6.8")], ("6.6.30", False)),
    # a fix in an older stable branch says nothing about this one
    ([("4.19", "5.15.100"), ("4.19", "6.2")], ("6.2", False)),
    # a backport into another branch introduced it there only
    ([("5.15.80", "5.15.90")], None),
    # introduced in this branch, fixed only in mainline
    ([("6.1.100", "6.2")], ("6.2", False)),
])
def test_affected(kc, pairs, hit):
    assert kc.affected(pairs, K) == hit


@pytest.mark.parametrize("vector,score", [
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8),
    ("CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H", 7.8),
    ("CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:N/I:N/A:H", 5.5),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H", 10.0),
    ("CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:C/C:L/I:L/A:N", 6.4),
    ("CVSS:3.0/AV:P/AC:H/PR:H/UI:R/S:U/C:L/I:N/A:N", 1.6),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N", 0.0),
    ("CVSS:2.0/AV:N", None), ("CVSS:3.1/AV:N/AC:L", None), ("CVSS:3.1/AV:X/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", None),
])
def test_cvss_score(kc, vector, score):
    assert kc.cvss_score(vector) == score


def test_roundup_follows_the_specification(kc):
    assert kc.roundup(4.02) == 4.1
    assert kc.roundup(4.0) == 4.0
    assert kc.roundup(4.000001) == 4.0  # within floating point noise


def test_in_built_code(kc):
    built = {"drivers/net/a.c", "fs/ext4/inode.c"}
    m = (built, {"drivers/net", "fs/ext4"})
    assert kc.in_built_code([], m) is True
    assert kc.in_built_code(["drivers/net/a.c", "include/linux/x.h"], m) is True
    assert kc.in_built_code(["drivers/s390/ism.c", "include/linux/ism.h"], m) is False
    assert kc.in_built_code(["include/linux/skbuff.h"], m) is True
    assert kc.in_built_code(["arch/arm64/include/asm/x.h"], m) is True
    assert kc.in_built_code(["fs/ext4/ext4.h"], m) is True
    assert kc.in_built_code(["fs/f2fs/f2fs.h"], m) is False


# --- generate: the records against the manifests -------------------------------------------------

def record(d, cve, dyad, title="", files=(), cvss=None):
    year = d / "cve/published" / cve.split("-")[1]
    year.mkdir(parents=True, exist_ok=True)
    (year / f"{cve}.dyad").write_text("# dyad\n" + "\n".join(dyad) + "\n")
    (year / f"{cve}.json").write_text(json.dumps({"containers": {"cna": {
        "title": title, "affected": [{"programFiles": list(files)}]}}}))
    if cvss:
        (year / f"{cve}.cvss").write_text(f"CNA {cvss}\n")


FIX = {n: f"{n}" * 40 for n in "abcdef"}


@pytest.fixture
def vulns(tmp_path, kc, monkeypatch):
    repo, manifests, out = tmp_path / "vulns", tmp_path / "manifests", tmp_path / "out"
    manifests.mkdir()
    record(repo, "CVE-2025-0001", [f"6.1.100:{'1' * 40}:6.1.150:{FIX['a']}"], "net: fix a",
           ["drivers/net/a.c"], "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")
    record(repo, "CVE-2025-0002", [f"6.1.100:{'1' * 40}:6.1.150:{FIX['b']}"], "s390: not built",
           ["drivers/s390/x.c"])
    record(repo, "CVE-2025-0003", [f"6.1.100:{'1' * 40}:6.1.150:{FIX['c']}"], "fixed in the tree",
           ["drivers/net/a.c"])
    record(repo, "CVE-2025-0004", [f"6.1.10:{'1' * 40}:6.1.120:{FIX['d']}"], "fixed by version, "
           "but not taken", ["drivers/net/a.c"])
    record(repo, "CVE-2025-0005", [f"6.1.10:{'1' * 40}:6.1.120:{FIX['e']}"], "fixed", ["fs/x.c"])
    (repo / "cve/published/README").parent.mkdir(parents=True, exist_ok=True)
    (repo / "cve/published/README").write_text("not a year")
    (repo / "cve/published/2025/CVE-2025-0006.json").write_text("{}")  # no dyad: skipped
    (manifests / "6.1.145-plinius9.files").write_text("# header\ndrivers/net/a.c\n")
    (manifests / "6.1.145-plinius9.commits").write_text(FIX["c"] + "\n" + FIX["d"] + "\nnot-an-id\n")
    (manifests / "6.1.145-plinius9.missing").write_text(FIX["d"] + "\n")
    (manifests / "6.1.130-ut.files").write_text("drivers/net/a.c\n")
    (manifests / "android.files").write_text("x\n")  # no version in the name
    monkeypatch.setattr(kc, "subprocess", types.SimpleNamespace(
        run=lambda *a, **k: types.SimpleNamespace(stdout="f" * 40 + "\n")))
    return repo, manifests, out


def test_generate(kc, vulns, capsys):
    repo, manifests, out = vulns
    assert kc.generate(str(repo), str(manifests), str(out)) == 0
    data = json.loads((out / "6.1.145-plinius9.json").read_text())
    assert data["kernel"] == "6.1.145" and data["release"] == "6.1.145-plinius9"
    assert data["commit"] == "f" * 40
    assert data["counts"] == {"affected": 3, "built": 2, "fixed_in_tree": 1}
    assert data["fixed_in_tree"] == ["CVE-2025-0003"]
    issues = {i["cve"]: i for i in data["issues"]}
    assert set(issues) == {"CVE-2025-0001", "CVE-2025-0004"}
    assert issues["CVE-2025-0001"] == {
        "cve": "CVE-2025-0001", "title": "net: fix a", "fixed": "6.1.150", "branch_fixed": True,
        "cvss": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", "score": 9.8}
    # Fixed by version, but the fixing commit's change was left out
    assert issues["CVE-2025-0004"]["fixed"] == "6.1.120 (not taken)"
    # Without a commit list: no fixed_in_tree, the version alone decides
    ut = json.loads((out / "6.1.130-ut.json").read_text())
    assert ut["counts"] == {"affected": 3, "built": 2}
    assert "fixed_in_tree" not in ut
    assert sorted(i["cve"] for i in ut["issues"]) == ["CVE-2025-0001", "CVE-2025-0003"]
    assert not (out / "android.json").exists()
    assert "android.files: no kernel version" in capsys.readouterr().err
    assert (out / "6.1.145-plinius9.json").stat().st_mode & 0o777 == 0o644
    assert not [p for p in out.iterdir() if p.name.startswith(".kernel-")]


def test_generate_without_manifests(kc, vulns, tmp_path):
    repo, _, out = vulns
    empty = tmp_path / "empty"
    empty.mkdir()
    assert kc.generate(str(repo), str(empty), str(out)) == 1


def test_read_record_without_json_or_cvss(kc, tmp_path):
    base = tmp_path / "CVE-2025-9"
    (tmp_path / "CVE-2025-9.dyad").write_text(f"0:0:6.1.150:{FIX['a']}\nbroken line\n6.2:x:0:0\n")
    pairs, vector, title, files, fixes, versions = kc.read_record(str(base))
    assert pairs == [("0", "6.1.150"), ("6.2", "0")]
    assert (vector, title, files) == (None, "", [])
    assert fixes == {FIX["a"]} and versions == {FIX["a"]: "6.1.150"}
    assert kc.read_record(str(tmp_path / "missing")) is None


def test_generate_skips_an_unreadable_record(kc, vulns):
    repo, manifests, out = vulns
    # A record whose .dyad cannot be read (a dangling link): skipped, the rest still counts
    (repo / "cve/published/2025/CVE-2025-0009.dyad").symlink_to("nowhere")
    assert kc.generate(str(repo), str(manifests), str(out)) == 0
    data = json.loads((out / "6.1.145-plinius9.json").read_text())
    assert data["counts"] == {"affected": 3, "built": 2, "fixed_in_tree": 1}
    assert "CVE-2025-0009" not in json.dumps(data)


def cli(fake_bin, *args):
    fake_bin.stub("git", '[ "$3" = rev-parse ] && echo ' + "e" * 40 + "\nexit 0")
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True,
                          text=True, timeout=60, env=fake_bin.env(isolated=True))


def test_command_line_clones_then_fetches_and_writes_the_lists(tmp_path, fake_bin):
    repo, manifests, out = tmp_path / "vulns", tmp_path / "manifests", tmp_path / "out"
    record(repo, "CVE-2025-0001", [f"6.1.100:{'1' * 40}:6.1.150:{FIX['a']}"], "net: fix a",
           ["drivers/net/a.c"])
    manifests.mkdir()
    (manifests / "6.1.145-x.files").write_text("drivers/net/a.c\n")
    # No clone yet: one shallow clone
    r = cli(fake_bin, "--fetch", repo, manifests, out)
    assert r.returncode == 0, r.stderr
    assert fake_bin.calls("git") == [
        ["clone", "-q", "--depth", "1", "https://git.kernel.org/pub/scm/linux/security/vulns.git",
         str(repo)],
        ["-C", str(repo), "rev-parse", "HEAD"]]
    data = json.loads((out / "6.1.145-x.json").read_text())
    assert data["commit"] == "e" * 40 and [i["cve"] for i in data["issues"]] == ["CVE-2025-0001"]
    assert "6.1.145-x: 1 CVEs for 6.1.145, 1 in built code" in r.stdout
    # A clone: fetched to the newest commit, the old trees dropped
    (repo / ".git").mkdir()
    (fake_bin.path / "git.log").unlink()
    r = cli(fake_bin, "--fetch", repo, manifests, out)
    assert r.returncode == 0, r.stderr
    assert fake_bin.calls("git") == [
        ["-C", str(repo), "fetch", "-q", "--depth", "1", "origin", "master"],
        ["-C", str(repo), "reset", "-q", "--hard", "FETCH_HEAD"],
        ["-C", str(repo), "reflog", "expire", "--expire=now", "--all"],
        ["-C", str(repo), "gc", "-q", "--prune=now"],
        ["-C", str(repo), "rev-parse", "HEAD"]]
    # Without --fetch the clone is used as it is; without manifests the exit status says so
    (fake_bin.path / "git.log").unlink()
    empty = tmp_path / "empty"
    empty.mkdir()
    r = cli(fake_bin, repo, empty, out)
    assert r.returncode == 1 and "no manifests" in r.stderr
    assert fake_bin.calls("git") == [["-C", str(repo), "rev-parse", "HEAD"]]
