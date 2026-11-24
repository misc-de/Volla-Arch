# plinius-usb: what it offers a computer over USB. The gadget it builds in configfs (functions,
# configuration links, product IDs, UDC binding), File Sharing (umtprd's configuration, FunctionFS),
# the gateway (iptables rules, networkd drop-in, dnsmasq), the webcam (the UVC function, its video
# node, closing it before the gadget goes), plug-in and unplugging, and its D-Bus methods and
# properties. configfs, sysfs, /proc and /run are directories in tmp (dhw_usb_phone.py); the
# programs it runs are stubs. Power role and pogo pins: test_dhw_usb_power.py; main() on a private
# bus: test_dhw_usb_daemon.py.
#
# Not run, as no input reaches them: plinius-usb.c:886 (webcam_set_device() with the value it has:
# its only callers pass a found node after the teardown emptied it, or "" for a non-empty one) and
# :1403 (power_swap_free() with the invocation still set: on_power_swapped() always takes it first,
# and GLib runs a task's callback before it frees the task's data).

import hashlib
import os
import pwd

import pytest

from dhw_usb_phone import Phone, build

ME = pwd.getpwuid(os.getuid())
SERIAL = hashlib.sha256(b"plinius-usb-serial:0123456789abcdef0123456789abcdef").hexdigest()[:12]
FN_MTP, FN_NCM, FN_GATEWAY, FN_WEBCAM = 1, 2, 4, 8

needs_a_user = pytest.mark.skipif(os.getuid() == 0, reason="File Sharing refuses root's home")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("usb")
    return d, build(d)


@pytest.fixture
def phone(built):
    return Phone(*built).reset()


def attached(phone, *commands):
    """A computer attached (as plug-in found it, without the preset), then the commands"""
    phone.computer()
    return phone.run("set network_on_connect 0", "set_connected 1", "set network_on_connect 1",
                     *commands)


def links(phone):
    cfg = phone.gadget("configs/c.1")
    return {p.name: os.readlink(p) for p in cfg.iterdir() if p.is_symlink()} if cfg.is_dir() else {}


def device_ids(phone):
    return tuple(phone.get(f"cfg/usb_gadget/plinius/{a}")
                 for a in ("idProduct", "bDeviceClass", "bDeviceSubClass", "bDeviceProtocol"))


# ---- What can be offered ----

@pytest.mark.parametrize("release,uvc", [
    ("6.1.192-plinius14", 1),
    ("6.1.192-plinius15", 1),
    ("6.1.193-plinius1", 1),
    ("6.2.0-plinius1", 1),
    ("7.0.1-plinius1", 1),
    ("6.1.192-plinius13", 0),
    ("6.1.191-plinius20", 0),
    ("6.0.200-plinius99", 0),
    ("5.15.0-plinius99", 0),
    # The GKI kernel and any other: no fixed f_uvc
    ("6.1.192-android14-11", 0),
    ("6.17.8-arch1-1", 0),
    ("fail", 0),
])
def test_webcam_needs_the_fixed_f_uvc(phone, release, uvc):
    assert phone.run(f"kernel {release}", "kernel_has_uvc").of("kernel_has_uvc") == [f"uvc {uvc}"]


@pytest.mark.parametrize("missing,available", [
    (None, "mtp, ncm, gateway, webcam"),
    ("bin/umtprd", "ncm, gateway, webcam"),
    ("bin/plinius-usb-webcam", "mtp, ncm, gateway"),
    ("lib/libgstuvcgadget.so", "mtp, ncm, gateway"),
])
def test_available_functions(phone, missing, available):
    if missing:
        (phone.d / missing).unlink()
    r = phone.run("available", "get AvailableFunctions")
    assert r.of("available") == [f"available {available}"]
    names = ", ".join(f"'{n}'" for n in available.split(", "))
    assert r.of("get AvailableFunctions") == [f"prop AvailableFunctions [{names}]"]


def test_no_webcam_on_an_old_kernel(phone):
    r = phone.run("kernel 6.1.192-plinius13", "available", "set connected 1", "call SetFunctions webcam")
    assert r.of("available") == ["available mtp, ncm, gateway"]
    assert r.of("call SetFunctions webcam") == [
        "reply SetFunctions error g-dbus-error-quark:7: Function not installed"]


# ---- Is a computer attached? ----

@pytest.mark.parametrize("usb_type,online,connected", [
    ("SDP", "1", 1),
    ("CDP", "1", 1),
    # A wall charger, or detection not done yet
    ("DCP", "1", 0),
    ("Unknown", "1", 0),
    ("SDP", "0", 0),
])
def test_charger_detection_tells_a_computer(phone, usb_type, online, connected):
    phone.computer(usb_type, online)
    assert phone.run("read_connected").of("read_connected") == [f"read_connected {connected}"]


def test_a_supply_that_is_not_usb_or_has_no_state(phone):
    phone.computer()
    phone.put("power_supply/usb/type", "Mains\n")
    phone.put("power_supply/usb2/type", "USB\n")   # no online attribute
    phone.put("power_supply/usb3/type", "USB\n")
    phone.put("power_supply/usb3/online", "1\n")   # no usb_type attribute
    assert phone.run("read_connected").of("read_connected") == ["read_connected 0"]


def test_a_configured_controller_is_a_computer(phone):
    # The phone powers the computer: charger detection sees no input, the gadget is configured
    phone.put("udc/11201000.usb/state", "configured\n")
    assert phone.run("read_connected").of("read_connected") == ["read_connected 1"]


@pytest.mark.parametrize("data,connected", [("device", 1), ("host", 0)])
def test_a_host_the_phone_powers_is_a_computer(phone, data, connected):
    phone.typec(role="source", data=data)
    r = phone.run(f"set port {phone.d}/typec/port0", "read_connected")
    assert r.of("read_connected") == [f"read_connected {connected}"]


# ---- Plug-in and unplugging ----

def test_plug_in_brings_the_network_preset(phone):
    phone.computer()
    r = phone.run("uevent power_supply USB", "timers", "advance 999", "state", "advance 1", "state")
    assert r.of("timers") == ["timers settle:1000"]
    assert r.of("advance 999") == []
    assert r.of("state")[0].startswith("state connected=0 functions=charging only")
    assert r.of("advance 1") == ["changed Connected true", "changed Error ''", "changed Functions ['ncm']"]
    assert r.of("state", 1)[0].startswith("state connected=1 functions=ncm user=0 error= ")
    g = phone.gadget()
    assert phone.get("cfg/usb_gadget/plinius/idVendor") == "0x0e8d\n"
    assert device_ids(phone) == ("0x2004\n", "0x00\n", "0x00\n", "0x00\n")
    assert (g / "strings/0x409/serialnumber").read_text() == SERIAL
    assert (g / "strings/0x409/manufacturer").read_text() == "Volla"
    assert (g / "strings/0x409/product").read_text() == "Plinius (Arch)"
    assert (g / "configs/c.1/strings/0x409/configuration").read_text() == "plinius"
    assert (g / "functions/ncm.usb0").is_dir()
    assert links(phone) == {"ncm.usb0": str(g / "functions/ncm.usb0")}
    # Bound to the real controller, not to dummy_udc
    assert (g / "UDC").read_text() == "11201000.usb"


def test_the_serial_number_without_a_machine_id(phone):
    (phone.d / "etc/machine-id").unlink()
    attached(phone, "call SetFunctions ncm")
    want = hashlib.sha256(b"plinius-usb-serial:").hexdigest()[:12]
    assert phone.gadget("strings/0x409/serialnumber").read_text() == want


def test_the_gadget_is_made_once(phone):
    r = attached(phone, "apply 0 0", "apply 2 0")
    assert r.of("apply 2 0") == ["changed Functions ['ncm']", "apply 1"]
    # Strings and IDs stay: an existing gadget is not written again
    phone.gadget("idVendor").unlink()
    phone.run("apply 0 0", "apply 2 0")
    assert not phone.gadget("idVendor").exists()


def test_only_a_usb_supply_counts(phone):
    r = phone.run("uevent power_supply Battery", "timers", "uevent extcon", "timers")
    assert r.of("timers") == ["timers"]
    assert r.of("timers", 1) == ["timers"]


def test_the_type_settles_before_it_counts(phone):
    # A second event restarts the wait: the type may change once
    phone.computer()
    r = phone.run("uevent power_supply USB", "advance 600", "uevent power_supply USB", "advance 600",
                  "timers", "advance 400")
    assert r.of("advance 600", 1) == []
    assert r.of("timers") == ["timers settle:400"]
    assert "changed Connected true" in r.of("advance 400")


def test_without_the_preset_plug_in_offers_nothing(phone):
    phone.computer()
    r = phone.run("set network_on_connect 0", "uevent power_supply USB", "advance 1000", "state")
    assert r.of("advance 1000") == ["changed Connected true", "changed Error ''"]
    assert not phone.gadget("UDC").exists()


def test_unplugging_resets_everything(phone):
    r = attached(phone, "call SetFunctions ncm,gateway")
    assert r.of("call SetFunctions ncm,gateway")[-1] == "reply SetFunctions ok"
    phone.computer(online="0")
    r = phone.run("set connected 1", f"set functions {FN_NCM | FN_GATEWAY}", "uevent power_supply USB",
                  "advance 1000", "state")
    assert r.of("advance 1000") == ["changed Connected false", "changed Error ''", "changed Functions []"]
    g = phone.gadget()
    assert (g / "UDC").read_text() == "\n"
    assert links(phone) == {}
    assert not (g / "functions/ncm.usb0").exists()
    assert not (phone.d / "run/network/40-plinius-usb.network.d/50-plinius-gateway.conf").exists()
    assert "USB functions: charging only" in r.stderr


def test_an_unbound_gadget_is_not_written_again(phone):
    # Nothing bound (UDC empty): teardown leaves it alone
    phone.put("cfg/usb_gadget/plinius/UDC", "")
    phone.run("teardown")
    assert phone.get("cfg/usb_gadget/plinius/UDC") == ""


# ---- SetFunctions ----

def test_set_functions_needs_a_computer(phone):
    assert phone.run("call SetFunctions ncm").of("call SetFunctions ncm") == [
        "reply SetFunctions error g-dbus-error-quark:0: No computer attached"]


def test_unknown_function(phone):
    r = phone.run("set connected 1", "call SetFunctions ncm,printer")
    assert r.of("call SetFunctions ncm,printer") == [
        "reply SetFunctions error g-dbus-error-quark:16: Unknown function printer"]


@pytest.mark.parametrize("bus,message", [
    ("bus locked 1", "Unlock the phone first"),
    ("bus caller 4321", "Only the user in front of the phone can do this"),
    ("bus session -", "Nobody is signed in on the phone"),
    # logind's "no session" and a session it cannot tell about
    ("bus session /", "Nobody is signed in on the phone"),
    ("bus suid -1", "Nobody is signed in on the phone"),
    ("bus caller -1", "fake bus: no such sender"),
])
def test_switching_on_needs_the_unlocked_user(phone, bus, message):
    r = attached(phone, bus, "call SetFunctions ncm")
    assert r.of("call SetFunctions ncm") == [
        f"reply SetFunctions error g-dbus-error-quark:9: {message}"]
    assert not phone.gadget("UDC").exists()


def test_root_switches_on_without_a_session(phone):
    r = attached(phone, "bus caller 0", "bus session -", "call SetFunctions ncm", "state")
    assert r.of("call SetFunctions ncm")[-1] == "reply SetFunctions ok"
    assert r.of("state")[0].startswith("state connected=1 functions=ncm user=0 ")


def test_switching_off_needs_nobody(phone):
    r = attached(phone, "call SetFunctions ncm", "bus locked 1", "bus caller 4321", "call SetFunctions -")
    assert r.of("call SetFunctions -") == ["changed Functions []", "reply SetFunctions ok"]
    assert phone.gadget("UDC").read_text() == "\n"


def test_the_same_functions_change_nothing(phone):
    r = attached(phone, "call SetFunctions ncm")
    phone.gadget("UDC").write_text("kept")
    r = phone.run("set connected 1", f"set functions {FN_NCM}", "bus locked 1", "call SetFunctions ncm")
    assert r.of("call SetFunctions ncm") == ["reply SetFunctions ok"]
    assert phone.gadget("UDC").read_text() == "kept"


def test_a_failed_change_answers_why(phone):
    r = attached(phone, "fail mkdir /functions/ncm.usb0", "call SetFunctions ncm", "state")
    assert r.of("call SetFunctions ncm") == [
        "changed Error 'The USB network could not start'",
        "reply SetFunctions error g-dbus-error-quark:0: The USB network could not start"]
    assert r.of("state")[0].startswith("state connected=1 functions=charging only")
    # The next change that works clears the error
    r = phone.run("set connected 1", "call SetFunctions ncm")
    assert r.of("call SetFunctions ncm")[:2] == ["changed Error ''", "changed Functions ['ncm']"]


def test_a_gadget_that_cannot_be_made_says_so(phone):
    r = attached(phone, "fail mkdirp /strings/0x409", "call SetFunctions ncm", "get Error")
    assert "Cannot create the USB gadget" in r.stderr
    reply = r.of("call SetFunctions ncm")[-1]
    assert reply == "reply SetFunctions error g-dbus-error-quark:0: The USB functions could not start"
    assert r.of("get Error") == ["prop Error 'The USB functions could not start'"]


def test_a_gadget_that_cannot_be_made(phone):
    # Charging only
    r = attached(phone, "fail mkdirp /strings/0x409", "call SetFunctions ncm", "state")
    assert "Cannot create the USB gadget" in r.stderr
    assert r.of("call SetFunctions ncm")[-1].startswith("reply SetFunctions error")
    assert r.of("state")[0].startswith("state connected=1 functions=charging only")


def test_a_link_that_fails_is_reported(phone):
    r = attached(phone, "fail symlink /configs/c.1/ncm.usb0", "call SetFunctions ncm")
    assert "Cannot add ncm.usb0 to the gadget" in r.stderr
    # Bound anyway: the controller decides then
    assert phone.gadget("UDC").read_text() == "11201000.usb"


def test_no_controller(phone):
    for udc in (phone.d / "udc").iterdir():
        (udc / "state").unlink()
        udc.rmdir()
    phone.put("udc/dummy_udc.0/state", "not attached\n")
    r = attached(phone, "call SetFunctions ncm")
    assert r.of("call SetFunctions ncm")[-1] == (
        "reply SetFunctions error g-dbus-error-quark:0: No USB controller found")


def test_a_busy_controller(phone):
    r = attached(phone, "fail write /UDC", "call SetFunctions ncm")
    assert r.of("call SetFunctions ncm")[-1] == (
        "reply SetFunctions error g-dbus-error-quark:0: The USB controller is busy")


# ---- File Sharing ----

@needs_a_user
def test_file_sharing_offers_the_home_without_hidden_files(phone):
    ffs = phone.d / "run/plinius-usb/ffs-mtp"
    r = attached(phone, "call SetFunctions mtp,ncm", "state")
    assert r.of("call SetFunctions mtp,ncm")[-2:] == ["changed Functions ['mtp', 'ncm']",
                                                    "reply SetFunctions ok"]
    assert f"mount mtp {ffs} functionfs" in r.of("call SetFunctions mtp,ncm")
    assert phone.get("run/plinius-usb/umtprd.conf") == (
        "loop_on_disconnect 1\n"
        "show_hidden_files 0\n"
        f'storage "{ME.pw_dir}" "Phone" "rw,uid={ME.pw_uid},gid={ME.pw_gid}"\n'
        'manufacturer "Volla"\n'
        'product "Plinius"\n'
        f'serial "{SERIAL}"\n'
        'firmware_version "1"\n'
        'interface "MTP"\n'
        "usb_functionfs_mode 0x1\n"
        f'usb_dev_path "{ffs}/ep0"\n'
        f'usb_epin_path "{ffs}/ep1"\n'
        f'usb_epout_path "{ffs}/ep2"\n'
        f'usb_epint_path "{ffs}/ep3"\n'
        "usb_max_packet_size 0x200\n")
    assert phone.bin.calls("umtprd") == [["-conf", str(phone.d / "run/plinius-usb/umtprd.conf")]]
    assert r.of("state")[0].startswith(f"state connected=1 functions=mtp, ncm user={ME.pw_uid} ")
    assert "umtprd=1" in r.of("state")[0]
    g = phone.gadget()
    assert links(phone) == {"ncm.usb0": str(g / "functions/ncm.usb0"),
                            "ffs.mtp": str(g / "functions/ffs.mtp")}
    # Two functions: interface association descriptors; an ID of its own for hosts to remember
    assert device_ids(phone) == ("0x200a\n", "0xef\n", "0x02\n", "0x01\n")


@needs_a_user
def test_file_sharing_alone(phone):
    attached(phone, "call SetFunctions mtp")
    assert device_ids(phone) == ("0x2008\n", "0x00\n", "0x00\n", "0x00\n")


@needs_a_user
def test_file_sharing_goes_with_umtprd_and_functionfs(phone):
    ffs = phone.d / "run/plinius-usb/ffs-mtp"
    r = attached(phone, "call SetFunctions mtp,ncm", "call SetFunctions ncm", "state")
    out = r.of("call SetFunctions ncm")
    assert f"umount {ffs}" in out
    assert out[-2:] == ["changed Functions ['ncm']", "reply SetFunctions ok"]
    assert "umtprd=0" in r.of("state")[0]
    assert not phone.gadget("functions/ffs.mtp").exists()
    assert links(phone) == {"ncm.usb0": str(phone.gadget("functions/ncm.usb0"))}
    assert "user=0 " in r.of("state")[0]


@needs_a_user
def test_file_sharing_keeps_the_user_it_offers(phone):
    # Root switches the network on later: the home stays the one of the user who chose it
    r = attached(phone, "call SetFunctions mtp", "bus caller 0", "call SetFunctions mtp,ncm", "state")
    assert f"user={ME.pw_uid} " in r.of("state")[0]


def test_file_sharing_needs_a_user_signed_in(phone):
    r = attached(phone, "bus caller 0", "bus session -", "call SetFunctions mtp")
    assert r.of("call SetFunctions mtp")[-1] == (
        "reply SetFunctions error g-dbus-error-quark:0: File sharing needs a user signed in on the phone")
    assert phone.bin.calls("umtprd") == []


@needs_a_user
@pytest.mark.parametrize("fail,warning", [
    ("fail mkdir /functions/ffs.mtp", "Cannot create the MTP function"),
    ("mount-errno 1", "Cannot mount FunctionFS: Operation not permitted"),
])
def test_file_sharing_that_cannot_start(phone, fail, warning):
    r = attached(phone, fail, "call SetFunctions mtp,ncm", "state")
    assert r.of("call SetFunctions mtp,ncm")[-1] == (
        "reply SetFunctions error g-dbus-error-quark:0: File sharing could not start")
    assert warning in r.stderr
    assert r.of("state")[0].startswith("state connected=1 functions=charging only")
    assert phone.bin.calls("umtprd") == []


@needs_a_user
def test_functionfs_mounted_already(phone):
    r = attached(phone, "mount-errno 16", "call SetFunctions mtp")
    assert r.of("call SetFunctions mtp")[-1] == "reply SetFunctions ok"


@needs_a_user
def test_umtprd_that_cannot_run(phone):
    phone.bin.stub("umtprd").write_text("#!/nonexistent/interpreter\n")
    r = attached(phone, "call SetFunctions mtp")
    assert r.of("call SetFunctions mtp")[-1] == (
        "reply SetFunctions error g-dbus-error-quark:0: File sharing could not start")
    assert f"Cannot start {phone.d}/bin/umtprd" in r.stderr


@needs_a_user
def test_umtprd_that_never_sets_up_functionfs(phone):
    phone.bin.stub("umtprd", "exec sleep 30 </dev/null >/dev/null 2>&1")
    r = attached(phone, "call SetFunctions mtp", "state")
    assert r.of("call SetFunctions mtp")[-1] == (
        "reply SetFunctions error g-dbus-error-quark:0: File sharing could not start")
    assert "umtprd did not set up FunctionFS" in r.stderr
    # Stopped again with the rest
    assert "umtprd=0" in r.of("state")[0]


@needs_a_user
def test_umtprd_that_ends_takes_file_sharing_with_it(phone):
    phone.bin.stub("umtprd", f'touch "{phone.d}/run/plinius-usb/ffs-mtp/ep1"\nsleep 0.3\nexit 1')
    r = attached(phone, "call SetFunctions mtp,ncm", "pump umtprd", "state")
    out = r.of("pump umtprd")
    # The reason once, before the functions that are left
    changes = [line for line in out if line.startswith("changed ")]
    assert changes == ["changed Error 'File sharing stopped unexpectedly'", "changed Functions ['ncm']"]
    assert out[-1] == "pumped umtprd yes"
    assert r.of("state")[0].startswith("state connected=1 functions=ncm ")


@needs_a_user
def test_why_file_sharing_went_stays_readable(phone):
    phone.bin.stub("umtprd", f'touch "{phone.d}/run/plinius-usb/ffs-mtp/ep1"\nsleep 0.3\nexit 1')
    r = attached(phone, "call SetFunctions mtp,ncm", "pump umtprd", "get Error")
    assert r.of("get Error") == ["prop Error 'File sharing stopped unexpectedly'"]


@needs_a_user
def test_umtprd_that_ends_with_file_sharing_alone(phone):
    phone.bin.stub("umtprd", f'touch "{phone.d}/run/plinius-usb/ffs-mtp/ep1"\nsleep 0.3\nexit 1')
    r = attached(phone, "call SetFunctions mtp", "pump umtprd", "get Error", "state")
    # Nothing left to rebuild: the reason stays
    assert r.of("get Error") == ["prop Error 'File sharing stopped unexpectedly'"]
    assert "functions=charging only " in r.of("state")[0]


def test_a_failed_unmount_is_reported(phone):
    r = phone.run("umount-errno 16", "teardown")
    assert "Cannot unmount FunctionFS: Device or resource busy" in r.stderr


# ---- Internet for the computer ----

NAT = [
    ["-w", "-t", "nat", "-A", "POSTROUTING", "-s", "10.15.19.0/24", "!", "-o", "usb0", "-j", "MASQUERADE"],
    ["-w", "-I", "FORWARD", "1", "-i", "usb0", "-j", "ACCEPT"],
    ["-w", "-I", "FORWARD", "1", "-o", "usb0", "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j",
     "ACCEPT"],
]
NAT_OFF = [
    ["-w", "-t", "nat", "-D", "POSTROUTING", "-s", "10.15.19.0/24", "!", "-o", "usb0", "-j", "MASQUERADE"],
    ["-w", "-D", "FORWARD", "-i", "usb0", "-j", "ACCEPT"],
    ["-w", "-D", "FORWARD", "-o", "usb0", "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j",
     "ACCEPT"],
]
DROPIN = "run/network/40-plinius-usb.network.d/50-plinius-gateway.conf"


def test_gateway(phone):
    # dnsmasq runs side by side with the daemon: the run waits until its stand-in has logged,
    # or the end of the run could stop it before that
    r = attached(phone, "call SetFunctions gateway", f"pump file:{phone.bin.path / 'dnsmasq.log'}",
                 "state")
    # The gateway brings the network it runs over
    assert r.of("call SetFunctions gateway")[-2:] == ["changed Functions ['ncm', 'gateway']",
                                                     "reply SetFunctions ok"]
    calls = phone.bin.calls("iptables-legacy")
    # Leftovers first (the teardown before every change), then the rules
    assert calls[-3:] == NAT, "\n".join(r.lines)
    assert all(c in NAT_OFF for c in calls[:-3])
    assert phone.get("ip_forward") == "1\n"
    assert phone.get(DROPIN) == ("# plinius-usb: Internet for the computer\n[DHCPServer]\nEmitRouter=yes\n"
                                 "EmitDNS=yes\nDNS=_server_address\n")
    assert phone.bin.calls("networkctl") == [["reload"]]
    assert phone.bin.calls("dnsmasq") == [[
        "--keep-in-foreground", "--conf-file=/dev/null", "--pid-file", "--no-hosts", "--port=53",
        "--interface=usb0", "--bind-dynamic", "--no-dhcp-interface=usb0", "--log-facility=-"]]
    assert "dnsmasq=1" in r.of("state")[0]
    assert device_ids(phone)[0] == "0x2004\n"


def test_gateway_off_keeps_the_network(phone):
    r = attached(phone, "call SetFunctions gateway", "call SetFunctions ncm", "state")
    assert r.of("call SetFunctions ncm")[-2:] == ["changed Functions ['ncm']", "reply SetFunctions ok"]
    assert "dnsmasq=0" in r.of("state")[0]
    assert not (phone.d / DROPIN).exists()
    assert phone.bin.calls("networkctl") == [["reload"], ["reload"]]


def test_leftover_rules_are_removed(phone):
    # A crashed instance left the masquerading rule twice
    count = phone.d / "left"
    count.write_text("2")
    phone.bin.stub("iptables-legacy", f'case " $* " in *" -D POSTROUTING "*)\n'
                   f'  n=$(cat "{count}"); [ "$n" -gt 0 ] || exit 1; echo $((n - 1)) > "{count}"; exit 0 ;;\n'
                   '*" -D "*) exit 1 ;; esac\nexit 0')
    phone.run("teardown")
    calls = phone.bin.calls("iptables-legacy")
    assert calls.count(NAT_OFF[0]) == 3
    assert calls.count(NAT_OFF[1]) == 1


@pytest.mark.parametrize("break_it,warning", [
    ("networkctl", "networkctl failed: no networkd"),
    ("iptables", "iptables-legacy failed: no NAT"),
    ("dnsmasq", "Cannot start"),
    ("ip_forward", "Cannot write"),
    ("dropin", "Cannot write"),
])
def test_gateway_that_cannot_start(phone, break_it, warning):
    commands = []
    if break_it == "networkctl":
        phone.bin.stub("networkctl", "echo 'no networkd' >&2\nexit 1")
    elif break_it == "iptables":
        phone.bin.stub("iptables-legacy", 'case " $* " in *" -D "*) exit 1 ;; esac\necho "no NAT" >&2\nexit 2')
    elif break_it == "dnsmasq":
        (phone.d / "bin/dnsmasq").unlink()
    elif break_it == "ip_forward":
        commands.append("fail write /ip_forward")
    else:
        commands.append("fail write /50-plinius-gateway.conf")
    r = attached(phone, *commands, "call SetFunctions gateway", "state")
    assert r.of("call SetFunctions gateway")[-1] == (
        "reply SetFunctions error g-dbus-error-quark:0: Internet for the computer could not start")
    assert warning in r.stderr
    assert r.of("state")[0].startswith("state connected=1 functions=charging only")
    assert not phone.gadget("UDC").exists() or phone.gadget("UDC").read_text() in ("", "\n")


def test_a_program_that_is_not_there(phone):
    (phone.d / "bin/networkctl").unlink()
    r = attached(phone, "call SetFunctions gateway")
    assert "Cannot run networkctl" in r.stderr


def test_dnsmasq_that_ends_takes_the_gateway_with_it(phone):
    phone.bin.stub("dnsmasq", "sleep 0.3\nexit 1")
    r = attached(phone, "call SetFunctions gateway", "pump dnsmasq", "state")
    out = r.of("pump dnsmasq")
    changes = [line for line in out if line.startswith("changed ")]
    assert changes == ["changed Error 'Internet for the computer stopped unexpectedly'", "changed Functions ['ncm']"]
    assert out[-1] == "pumped dnsmasq yes"
    assert r.of("state")[0].startswith("state connected=1 functions=ncm ")
    assert not (phone.d / DROPIN).exists()


def test_why_the_gateway_went_stays_readable(phone):
    phone.bin.stub("dnsmasq", "sleep 0.3\nexit 1")
    r = attached(phone, "call SetFunctions gateway", "pump dnsmasq", "get Error")
    assert r.of("get Error") == ["prop Error 'Internet for the computer stopped unexpectedly'"]


def test_programs_stopped_on_purpose_are_no_error(phone):
    r = attached(phone, "call SetFunctions gateway", "call SetFunctions ncm", "pump dnsmasq", "advance 10",
                 "state")
    assert "error= " in r.of("state")[0]
    assert r.of("advance 10") == []


# ---- Webcam ----

def webcam_on(phone, *more):
    node = phone.webcam_node()
    return node, attached(phone, "call SetFunctions webcam,ncm", *more)


def test_webcam(phone):
    node, r = webcam_on(phone, "timers", "state")
    assert r.of("call SetFunctions webcam,ncm")[-3:] == [
        f"changed WebcamDevice '{node}'", "changed Functions ['ncm', 'webcam']", "reply SetFunctions ok"]
    uvc = phone.gadget("functions/uvc.usb0")
    for name, w, h in (("720p", 1280, 720), ("480p", 640, 480)):
        frame = uvc / "streaming/mjpeg/m" / name
        assert (frame / "wWidth").read_text() == f"{w}\n"
        assert (frame / "wHeight").read_text() == f"{h}\n"
        assert (frame / "dwDefaultFrameInterval").read_text() == "333333\n"
        assert (frame / "dwFrameInterval").read_text() == "333333\n"
        assert (frame / "dwMaxVideoFrameBufferSize").read_text() == f"{w * h * 2}\n"
        assert (frame / "dwMinBitRate").read_text() == f"{w * h * 16 * 30}\n"
        assert (frame / "dwMaxBitRate").read_text() == f"{w * h * 16 * 30}\n"
    assert os.readlink(uvc / "streaming/header/h/m") == str(uvc / "streaming/mjpeg/m")
    for c in ("fs", "hs", "ss"):
        assert os.readlink(uvc / f"streaming/class/{c}/h") == str(uvc / "streaming/header/h")
    for c in ("fs", "ss"):
        assert os.readlink(uvc / f"control/class/{c}/h") == str(uvc / "control/header/h")
    assert (uvc / "streaming_maxpacket").read_text() == "1024\n"
    assert links(phone)["uvc.usb0"] == str(uvc)
    # UVC always comes with an interface association; webcam 0x2010, +2 for NCM
    assert device_ids(phone) == ("0x2012\n", "0xef\n", "0x02\n", "0x01\n")
    # The helper has five seconds to open the node
    assert r.of("timers") == ["timers webcam:5000"]
    assert f"webcam={node} " in r.of("state")[0]
    assert f"user={ME.pw_uid} " in r.of("state")[0]


def test_webcam_alone(phone):
    webcam_on(phone, "call SetFunctions webcam")
    assert device_ids(phone) == ("0x2010\n", "0xef\n", "0x02\n", "0x01\n")


@needs_a_user
@pytest.mark.parametrize("before,asked,stays", [
    # Switched on last wins: the controller has no room for both
    (FN_MTP, "mtp,webcam", "webcam"),
    (FN_WEBCAM, "mtp,webcam", "mtp"),
    (0, "mtp,webcam", "mtp"),
])
def test_file_sharing_or_the_webcam(phone, before, asked, stays):
    phone.webcam_node()
    phone.computer()
    r = phone.run("set network_on_connect 0", "set_connected 1", f"set functions {before}",
                  f"call SetFunctions {asked}", "state")
    assert f"functions={stays} " in r.of("state")[0]
    names = {"webcam": "the webcam", "mtp": "file sharing"}
    assert f"File sharing and the webcam do not fit together: {names[stays]} stays" in r.stderr


def test_webcam_needs_a_user_signed_in(phone):
    phone.webcam_node()
    r = attached(phone, "bus caller 0", "bus session -", "call SetFunctions webcam")
    assert r.of("call SetFunctions webcam")[-1] == (
        "reply SetFunctions error g-dbus-error-quark:0: The webcam needs a user signed in on the phone")


@pytest.mark.parametrize("fail,warning", [
    ("fail mkdir /functions/uvc.usb0", "Cannot create the UVC function"),
    ("fail mkdirp /streaming/mjpeg/m/720p", "Cannot create the UVC frame"),
    ("fail mkdirp /control/header/h", "Cannot create the UVC headers"),
    ("fail symlink /streaming/header/h/m", "Cannot link"),
    ("fail symlink /streaming/class/hs/h", "Cannot link"),
    ("fail symlink /control/class/ss/h", "Cannot link"),
])
def test_webcam_that_cannot_be_set_up(phone, fail, warning):
    phone.webcam_node()
    r = attached(phone, fail, "call SetFunctions webcam,ncm", "state")
    assert r.of("call SetFunctions webcam,ncm")[-1] == (
        "reply SetFunctions error g-dbus-error-quark:0: The webcam could not start")
    assert warning in r.stderr
    assert r.of("state")[0].startswith("state connected=1 functions=charging only")


def test_webcam_without_its_video_node(phone):
    r = attached(phone, "call SetFunctions webcam")
    assert r.of("call SetFunctions webcam")[-1] == (
        "reply SetFunctions error g-dbus-error-quark:0: The webcam could not start")


def test_a_video_node_of_a_camera_is_not_the_webcam(phone):
    (phone.d / "v4l/video0").symlink_to("../../devices/platform/soc/15040000.seninf/video4linux/video0")
    r = attached(phone, "call SetFunctions webcam")
    assert r.of("call SetFunctions webcam")[-1].endswith("The webcam could not start")


def test_webcam_off_removes_the_function(phone):
    node, r = webcam_on(phone, "call SetFunctions ncm", "timers")
    out = r.of("call SetFunctions ncm")
    # The helper is told first and the bus flushed, then the gadget is unbound
    assert out[:2] == ["changed WebcamDevice ''", "flush"]
    assert out[-2:] == ["changed Functions ['ncm']", "reply SetFunctions ok"]
    assert not phone.gadget("functions/uvc.usb0").exists()
    assert "uvc.usb0" not in links(phone)
    assert r.of("timers") == ["timers"]
    assert "Cannot remove the UVC function" not in r.stderr


def test_a_uvc_function_still_linked_stays(phone):
    # A link the daemon does not know keeps the function (configfs refuses the rmdir)
    node, r = webcam_on(phone)
    os.symlink("x", phone.gadget("functions/uvc.usb0/control/class/fs/extra"))
    r = phone.run("teardown")
    assert "Cannot remove the UVC function: Directory not empty" in r.stderr


@pytest.mark.parametrize("udc_state,stays", [("configured", True), ("suspended", True),
                                             ("not attached", False), (None, False)])
def test_webcam_the_helper_did_not_open(phone, udc_state, stays):
    node, r = webcam_on(phone)
    if udc_state is None:
        (phone.d / "udc/11201000.usb/state").unlink()
    else:
        phone.put("udc/11201000.usb/state", udc_state + "\n")
    r = phone.run("set connected 1", f"set functions {FN_NCM | FN_WEBCAM}", f"set webcam_device {node}",
                  "advance 5000", "webcam_timeout", "state")
    if stays:
        assert r.of("webcam_timeout") == []
        assert "functions=ncm, webcam " in r.of("state")[0]
    else:
        changes = [line for line in r.of("webcam_timeout") if line.startswith("changed Error")
                   or line.startswith("changed Functions")]
        assert changes == ["changed Error 'The webcam could not start'", "changed Functions ['ncm']"]
        # The network over USB does not stay away for it
        assert "functions=ncm " in r.of("state")[0]


def test_webcam_timeout_without_the_webcam(phone):
    phone.put("cfg/usb_gadget/plinius/UDC", "11201000.usb\n")
    r = phone.run(f"set functions {FN_NCM}", "webcam_timeout")
    assert r.of("webcam_timeout") == []
    phone.put("cfg/usb_gadget/plinius/UDC", "")
    r = phone.run(f"set functions {FN_NCM | FN_WEBCAM}", "webcam_timeout")
    assert r.of("webcam_timeout") == []


def test_webcam_timeout_fires(phone):
    # The timer itself: the node not opened in five seconds
    node, r = webcam_on(phone, "advance 4999", "advance 1", "state")
    assert r.of("advance 4999") == []
    changes = [line for line in r.of("advance 1") if line.startswith("changed ")]
    assert changes == ["changed WebcamDevice ''", "changed Error 'The webcam could not start'",
                       "changed Functions ['ncm']"]
    assert "functions=ncm " in r.of("state")[0]


def test_why_the_webcam_went_stays_readable(phone):
    node, r = webcam_on(phone, "advance 5000", "get Error")
    assert r.of("get Error") == ["prop Error 'The webcam could not start'"]


def test_why_the_webcam_alone_went_stays_readable(phone):
    phone.webcam_node()
    r = attached(phone, "call SetFunctions webcam", "advance 5000", "get Error", "state")
    assert r.of("get Error") == ["prop Error 'The webcam could not start'"]
    assert "functions=charging only " in r.of("state")[0]


# ---- Closing the webcam node before the gadget goes ----

def release(phone, *more):
    node = phone.webcam_node()
    return phone.run(f"set webcam_device {node}", "webcam_release", *more)


def test_release_without_holders(phone):
    r = release(phone, "state")
    assert r.of("webcam_release") == ["changed WebcamDevice ''", "flush"]


def test_release_waits_for_the_helper(phone):
    phone.holder(4242, closes=True)
    r = release(phone)
    assert r.of("webcam_release") == ["changed WebcamDevice ''", "flush", "closed by 4242"]
    assert "killed" not in r.stderr


def test_release_kills_who_keeps_it(phone):
    phone.holder(4242)
    phone.holder(4243, comm="firefox")
    phone.put("proc/4244/comm", "nothing open\n")
    (phone.d / "proc/4244/fd").mkdir()
    phone.put("proc/self/comm", "not a pid\n")
    r = release(phone)
    out = r.of("webcam_release")
    assert out[:2] == ["changed WebcamDevice ''", "flush"]
    assert sorted(out[2:]) == ["kill 4242 9", "kill 4243 9"]
    assert "plinius-usb-web (4242) still holds the webcam node: killed" in r.stderr
    assert "firefox (4243) still holds the webcam node: killed" in r.stderr
    assert "still open" not in r.stderr


def test_the_daemon_itself_is_no_holder(phone):
    node = phone.webcam_node()
    r = phone.run("self_proc", f"set webcam_device {node}", "webcam_release")
    assert r.of("webcam_release") == ["changed WebcamDevice ''", "flush"]


def test_release_of_a_holder_that_does_not_go(phone):
    phone.holder(4242, stubborn=True)
    (phone.d / "proc/4242/comm").unlink()
    r = release(phone)
    assert r.of("webcam_release")[-1] == "kill 4242 9"
    assert "? (4242) still holds the webcam node" in r.stderr
    assert "The webcam node is still open: removing the gadget may wait for it" in r.stderr


def test_release_of_a_node_that_is_gone(phone):
    phone.holder(4242)
    r = phone.run(f"set webcam_device {phone.d}/dev/video9", "webcam_release")
    assert r.of("webcam_release") == ["changed WebcamDevice ''", "flush"]
    r = phone.run("set webcam_device -", "webcam_release")
    assert r.of("webcam_release") == []


# ---- Properties ----

def test_properties(phone):
    r = phone.run(*[f"get {p}" for p in ("Connected", "Functions", "NetworkOnConnect", "WebcamDevice",
                                         "WebcamCamera", "Error", "ActivePort", "PortQuestion", "PowerRole",
                                         "PowerRoleSwappable", "SupplyPowerOnConnect", "Volume")])
    assert [line for line in r.lines if not line.startswith(">")] == [
        "prop Connected false", "prop Functions []", "prop NetworkOnConnect true",
        "prop WebcamDevice ''", "prop WebcamCamera 'hal_camera_1'", "prop Error ''",
        "prop ActivePort 'none'", "prop PortQuestion ''", "prop PowerRole ''",
        "prop PowerRoleSwappable false", "prop SupplyPowerOnConnect false",
        "prop Volume error Unknown property Volume"]


def test_webcam_camera_is_saved(phone):
    r = phone.run("setprop WebcamCamera hal_camera_0", "get WebcamCamera")
    assert r.of("setprop WebcamCamera hal_camera_0") == ["changed WebcamCamera 'hal_camera_0'", "setprop ok"]
    assert phone.get("var/settings") == ("[usb]\nnetwork-on-connect=true\nsupply-power-on-connect=false\n"
                                         "webcam-camera=hal_camera_0\n")


@pytest.mark.parametrize("camera", ["-", "../x", "a;b", "Hal_camera_0", "x" * 65, "a:b"])
def test_webcam_camera_must_be_a_node_name(phone, camera):
    out = phone.run(f"setprop WebcamCamera {camera}").of(f"setprop WebcamCamera {camera}")
    assert out[0].startswith("setprop error Invalid camera")
    assert phone.get("var/settings") is None


@pytest.mark.parametrize("bus", ["bus locked 1", "bus caller 4321"])
def test_webcam_camera_only_by_the_user(phone, bus):
    r = phone.run(bus, "setprop WebcamCamera hal_camera_0", "setprop WebcamCamera hal_camera_1")
    assert r.of("setprop WebcamCamera hal_camera_0")[0].startswith("setprop error ")
    # Unchanged needs nobody
    assert r.of("setprop WebcamCamera hal_camera_1") == ["setprop ok"]


@pytest.mark.parametrize("prop,key", [("SupplyPowerOnConnect", "supply-power-on-connect"),
                                      ("NetworkOnConnect", "network-on-connect")])
def test_presets_need_the_user_to_switch_on(phone, prop, key):
    r = phone.run(f"setprop {prop} false", "bus locked 1", f"setprop {prop} true", "bus locked 0",
                  f"setprop {prop} true", "bus locked 1", f"setprop {prop} true", f"setprop {prop} false")
    if prop == "NetworkOnConnect":
        assert r.of(f"setprop {prop} false") == [f"changed {prop} false", "setprop ok"]
    else:
        assert r.of(f"setprop {prop} false") == ["setprop ok"]
    assert r.of(f"setprop {prop} true") == ["setprop error Unlock the phone first"]
    assert r.of(f"setprop {prop} true", 1) == [f"changed {prop} true", "setprop ok"]
    # The value it has needs nobody; switching off needs nobody
    assert r.of(f"setprop {prop} true", 2) == ["setprop ok"]
    assert r.of(f"setprop {prop} false", 1) == [f"changed {prop} false", "setprop ok"]
    assert f"{key}=false" in phone.get("var/settings")


@pytest.mark.parametrize("prop", ["Connected", "Functions", "AvailableFunctions", "WebcamDevice", "Error",
                                  "ActivePort", "PortQuestion", "PowerRole", "PowerRoleSwappable"])
def test_read_only_properties(phone, prop):
    assert phone.run(f"setprop {prop} true").of(f"setprop {prop} true") == [
        f"setprop error Property {prop} is read-only"]


def test_settings_are_loaded(phone):
    phone.put("var/settings", "[usb]\nnetwork-on-connect=false\nsupply-power-on-connect=true\n"
                              "webcam-camera=hal_camera_0\n")
    r = phone.run("load_settings", "get NetworkOnConnect", "get SupplyPowerOnConnect", "get WebcamCamera")
    assert [line for line in r.lines if line.startswith("prop")] == [
        "prop NetworkOnConnect false", "prop SupplyPowerOnConnect true", "prop WebcamCamera 'hal_camera_0'"]


@pytest.mark.parametrize("text", ["[usb]\nnetwork-on-connect=maybe\nsupply-power-on-connect=7\n"
                                  "webcam-camera=\n", "[other]\nx=1\n", "not a key file\x00"])
def test_settings_that_say_nothing_keep_the_defaults(phone, text):
    phone.put("var/settings", text)
    r = phone.run("load_settings", "get NetworkOnConnect", "get SupplyPowerOnConnect", "get WebcamCamera")
    assert [line for line in r.lines if line.startswith("prop")] == [
        "prop NetworkOnConnect true", "prop SupplyPowerOnConnect false", "prop WebcamCamera 'hal_camera_1'"]


def test_settings_that_cannot_be_saved(phone):
    (phone.d / "var").rmdir()
    r = phone.run("setprop WebcamCamera hal_camera_0", "get WebcamCamera")
    # The choice holds until the restart
    assert r.of("get WebcamCamera") == ["prop WebcamCamera 'hal_camera_0'"]
    assert "Cannot save the settings" in r.stderr
