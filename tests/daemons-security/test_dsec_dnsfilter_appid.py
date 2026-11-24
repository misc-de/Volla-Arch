# plinius-dnsfilter: which app asked (appid.c through c/dsec_appid_harness.c). The socket of a
# question is found through sock_diag: once against the kernel (the cgroup id it reports for a
# socket of the harness must be the inode of the harness's own cgroup), otherwise against
# scripted dump answers, with a cgroup tree in tmp_path whose directory inodes are the ids.

import os
from pathlib import Path

import pytest

from conftest import PKG, REPO, load_script
from dsec_helpers import CDIR, build, run_harness

DNSFILTER = PKG / "plinius-dnsfilter"
dhw = load_script(REPO / "tests" / "daemons-hw" / "dhw_harness.py", "dhw_harness")

APP = "user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome-org.gnome.Maps-4242.scope"


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    out = tmp_path_factory.mktemp("appid")
    dhw.sandboxed_source(DNSFILTER / "appid.c", out, [('"/sys/fs/cgroup"', "appid_root")])
    return build(out, "appid-harness", [CDIR / "dsec_appid_harness.c"], flags=[f"-I{out}", f"-I{DNSFILTER}"],
                 pkgs=["glib-2.0"])


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "cgroup"
    for rel in (APP, "system.slice/NetworkManager.service", "lxc.payload.android/uid_10123/pid_77",
                # Deeper than the scan goes (8 levels)
                "deep/1/2/3/4/5/6/7/8/9"):
        (root / rel).mkdir(parents=True)
    # Files and links in a cgroup directory are no cgroups
    (root / "cgroup.procs").write_text("1\n")
    os.symlink(root / "system.slice", root / "link.slice")
    return root


def run(harness, root, *cmds):
    out = run_harness(harness, "\n".join(cmds) + "\n", APPID_ROOT=root)
    assert len(out) == len(cmds), out
    return out


def ino(path):
    return os.stat(path).st_ino


def own_cgroup():
    line = Path("/proc/self/cgroup").read_text().splitlines()[0]
    own = Path("/sys/fs/cgroup" + line[3:]) if line.startswith("0::") else None
    if own is None or not own.is_dir():
        pytest.skip("no cgroup2 hierarchy")
    return own


@pytest.mark.parametrize("sock,family,proto", [("udp4", 4, "udp"), ("tcp6", 6, "tcp")])
def test_the_kernel_reports_the_cgroup_of_the_socket(harness, tmp_path, sock, family, proto):
    # The request as the kernel takes it: the harness (in our cgroup) finds its own socket
    own = own_cgroup()
    out = run(harness, tmp_path, "real", f"bind {sock}", "peerbound", f"find {family} {proto}")
    if out[3].startswith("error"):
        pytest.skip(f"no sock_diag here: {out[3]}")
    assert out[3] == f"id {own.stat().st_ino}"


def test_a_question_of_an_app_names_the_app(harness, tree):
    cg = ino(tree / APP)
    out = run(harness, tree, "fake", "peer 4 127.0.0.1 5353",
              f"msg 9999 127.0.0.1 {ino(tree)}", f"msg 5353 127.0.0.1 {cg}", f"msg 5353 127.0.0.1 1 ",
              "msg 0 0.0.0.0 0 done", "batch", "lookup udp", "request")
    assert out[7] == "app/org.gnome.Maps"
    # The kernel filters by the port (bytecode S_EQ), dumps all states of UDP sockets of IPv4
    # (INET_DIAG_BC_S_EQ: "no" jumps past the end, which rejects)
    assert out[8] == ("type 20 flags 0x301 family 2 proto 17 states 0xffffffff attr 1/12 op 11/8/12 "
                      "port 0/5353")


def test_a_socket_bound_to_any_address_counts(harness, tree):
    cg = ino(tree / "system.slice" / "NetworkManager.service")
    out = run(harness, tree, "fake", "peer 6 ::1 853", f"msg 853 2001:db8::1 {ino(tree / APP)}",
              f"msg 853 :: {cg}", "msg 0 :: 0 done", "batch", "lookup tcp")
    # Another address of the same port is someone else's; "::" (sending without connect) is it
    assert out[-1] == "system/NetworkManager.service"


def test_a_dual_stack_client_is_found_by_its_mapped_address(harness, tree):
    cg = ino(tree / "lxc.payload.android" / "uid_10123" / "pid_77")
    out = run(harness, tree, "fake", "peer 4 127.0.0.1 4000", "msg 0 0.0.0.0 0 done", "batch",
              f"msg 4000 ::ffff:127.0.0.1 {cg}", "msg 0 :: 0 done", "batch", "lookup udp", "request")
    assert out[-2] == "android/uid_10123"
    assert " family 10 proto 17 " in out[-1]


@pytest.mark.parametrize("msgs,result", [
    (["msg 5353 127.0.0.1 {cg} badseq", "msg 0 0.0.0.0 0 done", "batch"], "-"),   # an old dump's answer
    (["msg 5353 127.0.0.1 {cg} noid", "msg 0 0.0.0.0 0 done", "batch"], "-"),     # no cgroup in it
    (["msg 5353 127.0.0.1 {cg} short", "msg 0 0.0.0.0 0 done", "batch"], "-"),
    (["msg 0 0.0.0.0 0 error", "batch"], "-"),
    # The dump in two parts, the second never comes: what was found counts
    (["msg 5353 127.0.0.1 {cg}", "batch"], "app/org.gnome.Maps"),
    ([], "-"),
])
def test_dump_answers_that_name_nobody(harness, tree, msgs, result):
    cg = ino(tree / APP)
    out = run(harness, tree, "fake", "peer 4 127.0.0.1 5353", *[m.format(cg=cg) for m in msgs], "lookup udp")
    assert out[-1] == result


def test_unknown_cgroups_are_looked_for_once_a_second(harness, tree):
    fresh = tree / "system.slice" / "new.service"
    answer = [f"msgdir 5353 127.0.0.1 {fresh}", "msg 0 0.0.0.0 0 done", "batch"]
    out = run(harness, tree, "fake", "peer 4 127.0.0.1 5353", f"msg 5353 127.0.0.1 {ino(tree / APP)}",
              "msg 0 0.0.0.0 0 done", "batch", "lookup udp", f"mkdir {fresh}", *answer, "lookup udp",
              "clock 1000001", *answer, "lookup udp", "shutdown", "peer x - 0", "lookup udp")
    assert out[5] == "app/org.gnome.Maps"
    # An app started since the scan: not looked for again within a second, then found
    assert out[10] == "-"
    assert out[15] == "system/new.service"
    # Not an IP socket
    assert out[-1] == "-"


def test_without_a_netlink_answer_nothing_is_known(harness, tree):
    out = run(harness, tree, "fake", "peer 4 127.0.0.1 1", "lookup udp", "lookup tcp", "request")
    assert out[2:4] == ["-", "-"]


def test_without_sock_diag_nothing_is_known(harness, tree):
    assert run(harness, tree, "none", "peer 4 127.0.0.1 53", "lookup udp")[-1] == "-"


def test_a_known_cgroup_needs_no_scan_and_deep_ones_are_not_scanned(harness, tree):
    cg = ino(tree / APP)
    deep = tree / "deep/1/2/3/4/5/6/7/8/9"
    answer = [f"msg 5353 127.0.0.1 {cg}", "msg 0 0.0.0.0 0 done", "batch"]
    out = run(harness, tree, "fake", "peer 4 127.0.0.1 5353", *answer, "lookup udp",
              *answer, "lookup udp", "clock 2000000",
              f"msgdir 5353 127.0.0.1 {deep}", "msg 0 0.0.0.0 0 done", "batch", "lookup udp")
    assert out[5] == out[9] == "app/org.gnome.Maps"
    assert out[-1] == "-"


@pytest.mark.parametrize("rel,key", [
    ("user.slice/user-1000.slice/user@1000.service/init.scope", "user/systemd"),
    # A scope without the "-<pid>" of its starter is no app's
    ("user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome-x.scope", "user/app-gnome-x.scope"),
    ("system.slice/foo.service/cgroup-only-dir", "system/foo.service"),
    ("-", "kernel"),
])
def test_cgroup_keys(harness, tmp_path, rel, key):
    assert run(harness, tmp_path, f"key {rel}") == [key]
