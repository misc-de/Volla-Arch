# plinius-ssh-set: which networks the SSH server answers on (firewall chain + sshd Match blocks),
# and the install script of plinius-ssh. The iptables tools, sshd and systemctl are stubs; the
# configuration files live in tmp_path.

import pytest

from conftest import PKG
from system_helpers import call_scriptlet, rewritten_copy, run, scriptlet

SRC = PKG / "plinius-ssh" / "plinius-ssh-set"
SHIPPED_CONF = (PKG / "plinius-ssh" / "plinius-ssh.conf").read_text()


class Ssh:
    def __init__(self, tmp_path, fake_bin):
        self.tmp = tmp_path
        self.bin = fake_bin
        self.conf = tmp_path / "etc" / "plinius-ssh.conf"
        self.sshd_dir = tmp_path / "etc" / "ssh"
        self.sshd_conf = self.sshd_dir / "sshd_config.d" / "40-plinius-ssh.conf"
        self.sshd_conf.parent.mkdir(parents=True)
        self.conf.write_text(SHIPPED_CONF)
        self.script = rewritten_copy(SRC, tmp_path / "plinius-ssh-set", {
            "/etc/plinius-ssh.conf": self.conf,
            "/etc/ssh/sshd_config.d/40-plinius-ssh.conf": self.sshd_conf,
            "/etc/ssh/ssh_host_*_key": f"{self.sshd_dir}/ssh_host_*_key",
        }, check_left=("/etc/",))
        for tool in ("iptables-legacy", "ip6tables-legacy"):
            # The rules on stdin are kept per family; -C fails until -I added the jump
            fake_bin.stub(tool + "-restore", f'cat > "{tmp_path}/{tool}.rules"')
            fake_bin.stub(tool, 'port=; prev=; for a; do [ "$prev" = --dport ] && port=$a; '
                          'prev=$a; done\n'
                          f'case "$*" in *" -C "*) [ -e "{tmp_path}/{tool}.jump.$port" ] ;; '
                          f'*" -I "*) touch "{tmp_path}/{tool}.jump.$port" ;; esac')
        fake_bin.stub("sshd", '[ "$1" = -T ] && printf "port 22\\nlistenaddress 0.0.0.0\\n"; '
                      'exit 0')
        fake_bin.stub("systemctl", 'case "$*" in *is-active*) exit "${SSHD_ACTIVE:-0}" ;; esac')
        self.env = fake_bin.env(isolated=True, TMPDIR=tmp_path)

    def run(self, *args, **extra):
        env = dict(self.env, **{k: str(v) for k, v in extra.items()})
        return run([self.script, *args], env)

    def rules(self, family=4):
        tool = "iptables-legacy" if family == 4 else "ip6tables-legacy"
        return (self.tmp / f"{tool}.rules").read_text().splitlines()


@pytest.fixture
def ssh(tmp_path, fake_bin):
    return Ssh(tmp_path, fake_bin)


@pytest.mark.parametrize("args", [[], ["allow"], ["allow", "wan"], ["deny", "../x"],
                                  ["allow", "usb; reboot"], ["allow", "USB"], ["open", "usb"],
                                  ["allow", ""]])
def test_bad_arguments_change_nothing(ssh, args):
    r = ssh.run(*args)
    assert r.returncode == 2 and "Usage" in r.stderr
    assert ssh.conf.read_text() == SHIPPED_CONF
    assert ssh.bin.calls("iptables-legacy-restore") == []
    assert not ssh.sshd_conf.exists()


def test_status_of_the_shipped_file(ssh):
    r = ssh.run("status")
    assert r.stdout == "usb=yes\nlocal=no\nall=no\n"


def test_status_defaults_without_file_and_with_garbage(ssh):
    ssh.conf.unlink()
    assert ssh.run("status").stdout == "usb=yes\nlocal=no\nall=no\n"
    # Anything but yes/no is the default, so a typo never opens a network
    ssh.conf.write_text("usb=maybe\nlocal=YES\nall=yesplease\n")
    assert ssh.run("status").stdout == "usb=yes\nlocal=no\nall=no\n"


def test_default_rules_only_usb(ssh):
    r = ssh.run("apply")
    assert r.returncode == 0, r.stderr
    assert ssh.rules(4) == ["*filter", ":plinius-ssh - [0:0]", "-A plinius-ssh -i lo -j RETURN",
                            "-A plinius-ssh -i usb0 -j RETURN", "-A plinius-ssh -j DROP",
                            "COMMIT"]
    assert ssh.rules(6) == ssh.rules(4)
    # Applied in one step, without flushing the other chains
    assert ssh.bin.calls("iptables-legacy-restore") == [["-w", "--noflush"]]
    sshd = ssh.sshd_conf.read_text()
    assert "Match LocalAddress 10.15.19.82\n\tPubkeyAuthentication yes\n" in sshd
    assert "Match Address" not in sshd and "Match all" not in sshd


def test_deny_usb_drops_everything_but_loopback(ssh):
    r = ssh.run("deny", "usb")
    assert r.returncode == 0, r.stderr
    assert "usb=no" in ssh.conf.read_text().splitlines()
    rules = ssh.rules(4)
    assert "-A plinius-ssh -i usb0 -j RETURN" not in rules
    assert rules[-2:] == ["-A plinius-ssh -j DROP", "COMMIT"]
    # The USB block still comes first and says no, so no later block can allow it
    assert ssh.sshd_conf.read_text().split("\n")[2:4] == ["Match LocalAddress 10.15.19.82",
                                                        "\tPubkeyAuthentication no"]


def test_allow_local_only_from_local_addresses(ssh):
    assert ssh.run("allow", "local").returncode == 0
    v4, v6 = ssh.rules(4), ssh.rules(6)
    assert "-A plinius-ssh -i wlan+ -s 192.168.0.0/16 -j RETURN" in v4
    assert "-A plinius-ssh -i enx+ -s 10.0.0.0/8 -j RETURN" in v4
    assert "-A plinius-ssh -i wlan+ -s fe80::/10 -j RETURN" in v6
    assert not any("fe80" in r for r in v4) and not any("192.168" in r for r in v6)
    # Never the mobile data interfaces
    assert not any("ccmni" in r or "rmnet" in r for r in v4 + v6)
    # Every RETURN rule names an interface (except loopback handling), the end is DROP
    returns = [r for r in v4 if r.endswith("RETURN")]
    assert all(" -i " in r for r in returns)
    assert v4[-2] == "-A plinius-ssh -j DROP"
    sshd = ssh.sshd_conf.read_text()
    assert ("Match Address 10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.0.0/16,"
            "fe80::/10,fc00::/7\n") in sshd


def test_allow_all_returns_for_everything(ssh):
    assert ssh.run("allow", "all").returncode == 0
    assert ssh.rules(4) == ["*filter", ":plinius-ssh - [0:0]", "-A plinius-ssh -i lo -j RETURN",
                            "-A plinius-ssh -j RETURN", "COMMIT"]
    assert "Match all\n\tPubkeyAuthentication yes\n" in ssh.sshd_conf.read_text()


def test_put_keeps_comments_and_other_keys(ssh):
    assert ssh.run("allow", "local").returncode == 0
    lines = ssh.conf.read_text().splitlines()
    assert lines.count("local=yes") == 1 and "local=no" not in lines
    assert "usb=yes" in lines and "all=no" in lines
    assert lines[0].startswith("# Networks")
    assert oct(ssh.conf.stat().st_mode & 0o777) == "0o644"


def test_put_adds_a_missing_key(ssh):
    ssh.conf.write_text("# only a comment\n")
    assert ssh.run("allow", "all").returncode == 0
    assert ssh.conf.read_text() == "# only a comment\nall=yes\n"


def test_jump_added_once_per_port(ssh, fake_bin):
    fake_bin.stub("sshd", '[ "$1" = -T ] && printf "port 22\\nport 2222\\n"; exit 0')
    assert ssh.run("apply").returncode == 0
    assert ssh.run("apply").returncode == 0
    # The second run finds both jumps with -C and adds none
    inserts = [c for c in fake_bin.calls("iptables-legacy") if "-I" in c]
    assert ["-w", "-I", "INPUT", "1", "-p", "tcp", "--dport", "22", "-j", "plinius-ssh"] in inserts
    assert ["-w", "-I", "INPUT", "1", "-p", "tcp", "--dport", "2222", "-j",
            "plinius-ssh"] in inserts
    assert len(inserts) == 2


def test_sshd_rejecting_the_file_restores_the_old_one(ssh, fake_bin):
    (ssh.sshd_dir / "ssh_host_ed25519_key").write_text("key")
    ssh.sshd_conf.write_text("# old file\n")
    fake_bin.stub("sshd", 'case $1 in -t) exit 1 ;; -T) echo "port 22" ;; esac')
    r = ssh.run("allow", "all")
    assert r.returncode == 1 and "kept the old one" in r.stderr
    assert ssh.sshd_conf.read_text() == "# old file\n"
    # No temporary file left next to it
    assert sorted(p.name for p in ssh.sshd_conf.parent.iterdir()) == ["40-plinius-ssh.conf"]
    assert not [c for c in fake_bin.calls("systemctl") if "kill" in c]


def test_sshd_rejecting_a_first_file_removes_it(ssh, fake_bin):
    (ssh.sshd_dir / "ssh_host_ed25519_key").write_text("key")
    fake_bin.stub("sshd", 'case $1 in -t) exit 1 ;; -T) echo "port 22" ;; esac')
    assert ssh.run("apply").returncode == 1
    assert not ssh.sshd_conf.exists()


def test_without_host_keys_sshd_is_not_asked(ssh, fake_bin):
    fake_bin.stub("sshd", 'case $1 in -t) exit 1 ;; -T) echo "port 22" ;; esac')
    assert ssh.run("apply").returncode == 0
    assert ["-t"] not in fake_bin.calls("sshd")
    assert ssh.sshd_conf.exists()


def test_running_sshd_gets_hup(ssh, fake_bin):
    assert ssh.run("apply").returncode == 0
    assert ["kill", "--kill-whom=main", "-s", "HUP", "sshd.service"] in fake_bin.calls("systemctl")


def test_stopped_sshd_is_not_signalled(ssh, fake_bin):
    assert ssh.run("apply", SSHD_ACTIVE=3).returncode == 0
    assert not [c for c in fake_bin.calls("systemctl") if "kill" in c]


def test_firewall_failure_stops_before_sshd(ssh, fake_bin):
    fake_bin.stub("iptables-legacy-restore", "cat >/dev/null; exit 1")
    r = ssh.run("allow", "all")
    assert r.returncode != 0
    assert not ssh.sshd_conf.exists()


# --- install script ---

def test_pre_remove_deletes_every_jump_and_the_chain(tmp_path, fake_bin):
    sshd_conf = tmp_path / "40-plinius-ssh.conf"
    sshd_conf.write_text("x")
    script = scriptlet("plinius-ssh", tmp_path, {
        "/etc/ssh/sshd_config.d/40-plinius-ssh.conf": sshd_conf,
        "/usr/lib/plinius-ssh/plinius-ssh-set": fake_bin.stub("plinius-ssh-set"),
    }, check_left=("rm -f /etc/",))
    for tool in ("iptables-legacy", "ip6tables-legacy"):
        # Two jumps (ports 22 and 2222) and an unrelated rule; -D removes one per call
        state = tmp_path / f"{tool}.S"
        state.write_text("-P INPUT ACCEPT\n-A INPUT -p tcp --dport 22 -j plinius-ssh\n"
                         "-A INPUT -p tcp --dport 80 -j plinius-ssh-other\n"
                         "-A INPUT -p tcp --dport 2222 -j plinius-ssh\n")
        fake_bin.stub(tool, f'case $2 in -S) cat "{state}" ;; -D) shift 2; '
                      f'grep -vxF -- "-A $*" "{state}" > "{state}.n"; mv "{state}.n" "{state}" ;; '
                      'esac')
    fake_bin.stub("systemctl", "exit 3")
    r = call_scriptlet(script, "pre_remove", fake_bin.env(isolated=True))
    assert r.returncode == 0, r.stderr
    assert not sshd_conf.exists()
    for tool in ("iptables-legacy", "ip6tables-legacy"):
        calls = fake_bin.calls(tool)
        assert ["-w", "-D", "INPUT", "-p", "tcp", "--dport", "22", "-j", "plinius-ssh"] in calls
        assert ["-w", "-D", "INPUT", "-p", "tcp", "--dport", "2222", "-j", "plinius-ssh"] in calls
        assert calls[-2:] == [["-w", "-F", "plinius-ssh"], ["-w", "-X", "plinius-ssh"]]
        # The other chain's jump stays
        assert "plinius-ssh-other" in (tmp_path / f"{tool}.S").read_text()


def test_pre_remove_stops_if_a_jump_cannot_be_deleted(tmp_path, fake_bin):
    script = scriptlet("plinius-ssh", tmp_path, {
        "/etc/ssh/sshd_config.d/40-plinius-ssh.conf": tmp_path / "none.conf",
        "/usr/lib/plinius-ssh/plinius-ssh-set": fake_bin.stub("plinius-ssh-set"),
    })
    for tool in ("iptables-legacy", "ip6tables-legacy"):
        fake_bin.stub(tool, 'case $2 in -S) echo "-A INPUT -p tcp --dport 22 -j plinius-ssh" ;; '
                      '-D) exit 1 ;; esac')
    fake_bin.stub("systemctl", "exit 3")
    # Without the break this would loop forever; the timeout of run() catches that
    r = call_scriptlet(script, "pre_remove", fake_bin.env(isolated=True))
    assert r.returncode == 0


def test_post_install_applies_and_stops_the_local_socket(tmp_path, fake_bin):
    tool = fake_bin.stub("plinius-ssh-set", "exit 1")
    script = scriptlet("plinius-ssh", tmp_path, {
        "/etc/ssh/sshd_config.d/40-plinius-ssh.conf": tmp_path / "none.conf",
        "/usr/lib/plinius-ssh/plinius-ssh-set": tool,
    })
    fake_bin.stub("systemctl")
    r = call_scriptlet(script, "post_upgrade", fake_bin.env(isolated=True))
    assert r.returncode == 0
    assert fake_bin.calls("plinius-ssh-set") == [["apply"]]
    assert ["stop", "sshd-unix-local.socket"] in fake_bin.calls("systemctl")
