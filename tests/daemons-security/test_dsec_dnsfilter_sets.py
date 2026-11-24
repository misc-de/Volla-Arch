# plinius-dnsfilter: the compiled rule sets on disk (filter.c, through c/dsec_dnsmsg_harness.c)
# when the disk says no, the limit of $badfilter lines, listing a set's rules, the record type
# names (dnsmsg.c) and names whose label bytes are not a length.

import os

import pytest

from conftest import PKG
from dsec_helpers import CDIR, build, run_harness

DNSFILTER = PKG / "plinius-dnsfilter"


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    return build(tmp_path_factory.mktemp("dnsf-sets"), "dnsf-sets-harness",
                 [CDIR / "dsec_dnsmsg_harness.c", DNSFILTER / "dnsmsg.c", DNSFILTER / "filter.c"],
                 flags=["-Wno-unused-parameter", "-Wno-missing-field-initializers"], pkgs=["gio-2.0"])


def run(harness, *cmds):
    out = run_harness(harness, "\n".join(cmds) + "\n")
    assert len(out) == len(cmds), out
    return out


@pytest.fixture
def listfile(tmp_path):
    f = tmp_path / "list.txt"
    f.write_text("||a.test^\n@@||b.test^\n|c.test^$important\n")
    return f


def test_saving_where_it_cannot_be_written(harness, tmp_path, listfile):
    (tmp_path / "isdir").mkdir()
    (tmp_path / "isdir.new").write_text("")
    os.symlink(tmp_path / "elsewhere", tmp_path / "link.new")
    out = run(harness, f"build {listfile}", f"save {tmp_path}/missing/set.bin", f"save {tmp_path}/isdir",
              f"save {tmp_path}/link", f"load {tmp_path}/missing.bin")
    assert out[1].startswith(f"error {tmp_path}/missing/set.bin.new: ")
    # Renaming over a directory fails: the temporary file goes
    assert out[2].startswith(f"error {tmp_path}/isdir: ")
    assert not (tmp_path / "isdir.new").exists()
    # A link in the place of the temporary file is not followed
    assert out[3].startswith(f"error {tmp_path}/link.new: ")
    assert not (tmp_path / "elsewhere").exists()
    assert out[4].startswith("error ")


def test_a_full_disk_leaves_neither_a_set_nor_a_temporary_file(harness, tmp_path, listfile):
    # (the limit back to none afterwards, so the counters of a coverage run can be written)
    out = run(harness, f"build {listfile}", "fsize 40", f"save {tmp_path}/set.bin", "fsize 0")
    assert out[2].startswith(f"error {tmp_path}/set.bin: ") and "too large" in out[2]
    assert not (tmp_path / "set.bin").exists() and not (tmp_path / "set.bin.new").exists()


def test_badfilter_lines_count_against_the_limit_too(harness, tmp_path):
    f = tmp_path / "bad.txt"
    f.write_text("".join(f"||b{i}.test^$badfilter\n" for i in range(3)) + "||x.test^\n")
    assert run(harness, f"build {f} 2", f"build {f} 5") == ["rules 1 unsupported 0 toomany 1",
                                                            "rules 1 unsupported 0 toomany 0"]


def test_the_rules_of_a_set(harness, listfile):
    out = run(harness, f"build {listfile}", "rules")
    # RULE_SUBDOMAINS 1, RULE_ALLOW 2, RULE_IMPORTANT 4
    assert sorted(out[1].split()[1:]) == ["a.test/1", "b.test/3", "c.test/4"]


@pytest.mark.parametrize("t,name", [(1, "A"), (28, "AAAA"), (65, "HTTPS"), (64, "SVCB"), (5, "CNAME"),
                                    (99, "TYPE99"), (65535, "TYPE65535")])
def test_record_type_names(harness, t, name):
    assert run(harness, f"typename {t}") == [name]


def test_a_label_with_reserved_length_bits_is_no_name(harness):
    # 0x40 and 0x80 are neither a length nor a pointer (RFC 6891 retired them)
    assert run(harness, "skip 4061626300 0", "skip 8061626300 0") == ["-1", "-1"]


def test_a_name_longer_than_255_bytes_is_no_name(harness):
    label = "3f" + "61" * 63
    assert run(harness, f"skip {label * 4}00 0", f"skip {label * 3}00 0") == ["-1", "193"]
