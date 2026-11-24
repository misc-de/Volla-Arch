# ofono2mm as built (pinned commit + our patches, from the source cache): when a modem counts as
# one with a SIM (patch 0024). Since ofono-binder-plugin 1.1.42 the eSIM chip without a profile
# is "Present" in oFono, without an ICCID; reported as a SIM, NetworkManager put the mobile data
# connection on that modem and timed out there, while the modem with the real SIM stayed unused.
# mm_sim.py runs against a stand-in for dbus-next (not on the build host); mm_modem.py, which
# needs GObject introspection besides, is checked for using the same rule.

import ast
import shutil
import sys
import textwrap

import pytest

from conftest import load_script
from prepare_helper import SRCDEST, Unavailable, run_prepare

pytestmark = pytest.mark.skipif(not SRCDEST.is_dir(), reason="no source cache")

DBUS_NEXT = {
    "__init__.py": """\
        class Variant:
            def __init__(self, signature, value):
                self.signature, self.value = signature, value
        class DBusError(Exception):
            def __init__(self, name, text=''):
                super().__init__(name, text)
        BusType = Message = MessageType = object
        """,
    "service.py": """\
        class ServiceInterface:
            def __init__(self, name):
                self.name, self.emitted = name, []
            def emit_properties_changed(self, changed):
                self.emitted.append(changed)
        def _decorator(*args, **kwargs):
            return lambda f: f
        method = dbus_property = signal = _decorator
        """,
    "constants.py": """\
        class PropertyAccess:
            READ, WRITE, READWRITE = 'read', 'write', 'readwrite'
        """,
}


@pytest.fixture(scope="module")
def tree(tmp_path_factory):
    work = tmp_path_factory.mktemp("ofono2mm-sim")
    try:
        r = run_prepare("ofono2mm", work)
    except Unavailable as e:
        shutil.rmtree(work, ignore_errors=True)
        pytest.skip(str(e))
    assert r.returncode == 0, r.stdout + r.stderr
    yield work / "src" / "oFono2MM"
    shutil.rmtree(work, ignore_errors=True)


@pytest.fixture(scope="module")
def mm_sim(tree, tmp_path_factory):
    fake = tmp_path_factory.mktemp("fake-dbus-next")
    (fake / "dbus_next").mkdir()
    for name, text in DBUS_NEXT.items():
        (fake / "dbus_next" / name).write_text(textwrap.dedent(text))
    sys.path.insert(0, str(fake))
    try:
        yield load_script(tree / "ofono2mm" / "mm_sim.py", "ofono2mm_mm_sim")
    finally:
        sys.path.remove(str(fake))
        for mod in [m for m in sys.modules if m == "dbus_next" or m.startswith("dbus_next.")]:
            del sys.modules[mod]


def props(mm_sim, **values):
    return {k: mm_sim.Variant("v", v) for k, v in values.items()}


REAL_SIM = dict(Present=True, CardIdentifier="8900000000000000001", SubscriberIdentity="001010000000001",
                PinRequired="none", MobileCountryCode="001", MobileNetworkCode="01")


@pytest.mark.parametrize("values,usable", [
    (REAL_SIM, True),
    # Locked by a PIN: the ICCID is there before the PIN, the SIM counts (and is reported locked)
    (dict(Present=True, CardIdentifier="8900000000000000001", PinRequired="pin"), True),
    # The eSIM chip without a profile: present since ofono-binder-plugin 1.1.42, no ICCID
    (dict(Present=True, PinRequired="none"), False),
    (dict(Present=True, CardIdentifier=""), False),
    (dict(Present=False, CardIdentifier="8900000000000000001"), False),
    ({}, False),
])
def test_a_sim_needs_an_iccid(mm_sim, values, usable):
    assert mm_sim.sim_usable(props(mm_sim, **values)) is usable


def sim_interface(mm_sim, sim_manager):
    iface_props = {"org.ofono.SimManager": sim_manager}
    client = {"ofono_modem": {"/ril_1": object()}}
    sim = mm_sim.MMSimInterface(1, None, client, "/ril_1", None, {}, {"org.ofono.SimManager": None},
                                iface_props)
    return sim, iface_props


def test_the_sim_object_follows_the_rule(mm_sim):
    chip, _ = sim_interface(mm_sim, props(mm_sim, Present=True, PinRequired="none"))
    chip.set_props()
    assert chip.props["Active"].value is False
    assert chip.present is False

    sim, iface_props = sim_interface(mm_sim, props(mm_sim, **REAL_SIM))
    sim.set_props()
    assert sim.props["Active"].value is True and sim.present is True
    assert sim.props["SimIdentifier"].value == REAL_SIM["CardIdentifier"]

    # The ICCID arriving after "Present" (at boot) turns the card into a SIM then
    iface_props["org.ofono.SimManager"] = props(mm_sim, Present=True)
    sim.set_props()
    assert sim.present is False
    iface_props["org.ofono.SimManager"] = props(mm_sim, **REAL_SIM)
    sim.set_props()
    assert sim.present is True


def test_without_sim_manager_there_is_no_sim(mm_sim):
    sim, _ = sim_interface(mm_sim, props(mm_sim, **REAL_SIM))
    sim.ofono_interfaces.clear()
    assert sim.present is False


def test_the_modem_state_uses_the_same_rule(tree):
    """set_modem_state() reports a missing SIM through sim_usable(), not through "Present"."""
    source = (tree / "ofono2mm" / "mm_modem.py").read_text()
    module = ast.parse(source)
    func = next(n for n in ast.walk(module)
                if isinstance(n, ast.FunctionDef) and n.name == "set_modem_state")
    calls = [n.func.id for n in ast.walk(func) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "sim_usable" in calls
    assert "'Present'" not in ast.get_source_segment(source, func)
    imports = [a.name for n in module.body if isinstance(n, ast.ImportFrom) and n.module == "ofono2mm.mm_sim"
               for a in n.names]
    assert "sim_usable" in imports
