# plinius-usb: who may switch USB functions on (file sharing of the home, network, webcam), pick
# the active port or the camera a computer sees, and what the methods and properties accept
# (plinius-usb.c through c/dsec_usb_harness.c, against a scripted bus). Writing files and running
# programs are refused by the harness; the tests stop before anything would be switched.

import pytest

from conftest import PKG
from dsec_helpers import CDIR, build, run_harness

USB = PKG / "plinius-usb"
NCM, GATEWAY = 2, 4
DENIED = "error g-dbus-error-quark:9: "


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    return build(tmp_path_factory.mktemp("usb"), "usb-harness", [CDIR / "dsec_usb_harness.c"],
                 flags=[f"-I{USB}", "-Wno-unused-parameter"],
                 pkgs=["gio-unix-2.0", "gudev-1.0"])


def run(harness, *cmds):
    """Output lines of the last command (setup commands answer "ok")."""
    out = run_harness(harness, "\n".join(cmds) + "\n")
    setup = sum(1 for c in cmds[:-1] if c.split()[0] in ("bus", "connected", "functions",
                                                          "question"))
    assert out[:setup] == ["ok"] * setup, out
    return out[setup:]


# ---- check_caller ----

@pytest.mark.parametrize("bus,result", [
    ([], "allowed 1001"),
    (["bus locked 1"], "denied Unlock the phone first"),
    (["bus caller 1002"], "denied Only the user in front of the phone can do this"),
    (["bus caller 1002", "bus locked 1"], "denied Only the user in front of the phone can do this"),
    # Root (SSH) acts for the user in front: File Sharing offers that user's home
    (["bus caller 0"], "allowed 1001"),
    (["bus caller 0", "bus locked 1"], "allowed 1001"),
    (["bus caller 0", "bus session /"], "allowed 0"),
    (["bus caller 0", "bus session -"], "allowed 0"),
    (["bus session /"], "denied Nobody is signed in on the phone"),
    (["bus session -"], "denied Nobody is signed in on the phone"),
    (["bus session badtype"], "denied Nobody is signed in on the phone"),
    (["bus suid -1"], "denied Nobody is signed in on the phone"),
    (["bus suid -2"], "denied Nobody is signed in on the phone"),
    (["bus locked -1"], "denied Nobody is signed in on the phone"),
    (["bus locked -2"], "denied Nobody is signed in on the phone"),
])
def test_check_caller(harness, bus, result):
    assert run(harness, *bus, "caller") == [result]


def test_unknown_sender_is_denied(harness):
    out, = run(harness, "bus caller -1", "caller")
    assert out.startswith("denied ")


# ---- SetFunctions ----

def test_unknown_function(harness):
    assert run(harness, "connected 1", "setfn ncm,evil") == [
        "error g-dbus-error-quark:16: Unknown function evil"]


def test_nothing_without_a_computer(harness):
    assert run(harness, "setfn ncm") == ["error g-dbus-error-quark:0: No computer attached"]


def test_webcam_needs_the_fixed_kernel(harness):
    # The host's kernel is no plinius one: the function is not offered
    assert run(harness, "connected 1", "setfn webcam") == [
        "error g-dbus-error-quark:7: Function not installed"]


@pytest.mark.parametrize("bus,message", [
    (["bus locked 1"], "Unlock the phone first"),
    (["bus caller 1002"], "Only the user in front of the phone can do this"),
    (["bus session /"], "Nobody is signed in on the phone"),
])
def test_switching_on_needs_the_unlocked_user(harness, bus, message):
    out = run(harness, *bus, "connected 1", "setfn ncm")
    # Refused before anything is written or run
    assert out == [DENIED + message]


def test_gateway_brings_the_network_and_needs_consent(harness):
    assert run(harness, "bus caller 1002", "connected 1", f"functions {NCM}", "setfn gateway") == [
        DENIED + "Only the user in front of the phone can do this"]
    # What runs already needs nobody's consent again (gateway includes ncm)
    assert run(harness, "bus caller 1002", "connected 1", f"functions {NCM | GATEWAY}",
               "setfn gateway") == ["ok"]


# ---- ChoosePort / SetPowerRole ----

def test_choose_port(harness):
    assert run(harness, "choose hdmi") == ["error g-dbus-error-quark:16: Unknown port hdmi"]
    assert run(harness, "choose pogo") == ["error g-dbus-error-quark:0: There is no question open"]
    assert run(harness, "question pogo", "bus caller 1002", "choose usb-c") == [
        DENIED + "Only the user in front of the phone can do this"]
    assert run(harness, "question pogo", "bus locked 1", "choose usb-c") == [
        DENIED + "Unlock the phone first"]


def test_power_role(harness):
    assert run(harness, "role evil") == ["error g-dbus-error-quark:16: Unknown power role evil"]
    # The host has no Type-C port known to the daemon
    assert run(harness, "role source") == [
        "error g-dbus-error-quark:7: This USB port cannot change the direction of power"]


# ---- Properties ----

def test_webcam_camera_by_the_user(harness):
    assert run(harness, "setprop WebcamCamera hal_camera_0") == ["SAVE", "ok 'hal_camera_0'"]


@pytest.mark.parametrize("camera", ["../x", "a;b", "Hal_camera_0", "x" * 65, "-", "a/b",
                                    "a'b", "cam\\x00"])
def test_webcam_camera_must_be_a_node_name(harness, camera):
    out, = run(harness, f"setprop WebcamCamera {camera}")
    # Only a PipeWire node name of the bridge reaches the helper's pipeline
    assert out.startswith("error Invalid camera") and out.endswith("| 'hal_camera_1'")


def test_webcam_camera_by_someone_else(harness):
    out = run(harness, "bus caller 1002", "setprop WebcamCamera hal_camera_0")
    assert out == ["error Only the user in front of the phone can do this | 'hal_camera_1'"]


def test_webcam_camera_unchanged_needs_nobody(harness):
    assert run(harness, "bus locked 1", "setprop WebcamCamera hal_camera_1") == [
        "ok 'hal_camera_1'"]


@pytest.mark.parametrize("prop,default", [("SupplyPowerOnConnect", "false"),
                                          ("NetworkOnConnect", "true")])
def test_presets_need_the_user_to_switch_on_only(harness, prop, default):
    other = "false" if default == "true" else "true"
    on_first = default == "false"
    if on_first:
        # Powering devices without unlocking: only the unlocked user switches it on
        assert run(harness, "bus locked 1", f"setprop {prop} true") == [
            "error Unlock the phone first | false"]
        assert run(harness, f"setprop {prop} true") == ["SAVE", "ok true"]
    else:
        # Switching off takes a right away: anyone may
        assert run(harness, "bus locked 1", f"setprop {prop} false") == ["SAVE", "ok false"]
        assert run(harness, "bus caller 1002", f"setprop {prop} {other}") == ["SAVE", "ok false"]
    # Setting the value it has changes nothing and needs nobody
    assert run(harness, "bus caller 1002", f"setprop {prop} {default}") == [f"ok {default}"]


def test_network_on_connect_switched_on_again(harness):
    assert run(harness, "bus locked 1", "setprop NetworkOnConnect false",
               "setprop NetworkOnConnect true") == [
        "SAVE", "ok false", "error Unlock the phone first | false"]


@pytest.mark.parametrize("prop", ["Connected", "Functions", "ActivePort", "Error", "PowerRole"])
def test_read_only_properties(harness, prop):
    out, = run(harness, f"setprop {prop} true")
    assert out.startswith(f"error Property {prop} is read-only")
