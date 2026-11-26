# plinius-mobile-data (device-volla-ansuz): the first access point of a SIM from oFono's context,
# so mobile data works on a new phone without setting it up. nmcli, mmcli and busctl are stubs
# that answer from files in tmp_path; the state directory lies there too. The script is loaded as
# a module (PATH only the stubs, sleep and the clock replaced) and once run as a program.

import importlib.machinery
import importlib.util
import json
import subprocess
import sys

import pytest

from conftest import PKG

SRC = PKG / "device-volla-ansuz" / "plinius-mobile-data"
ICCID = "8949000000000000001"
SIM_PATH = "/org/freedesktop/ModemManager1/SIM/0"


def load():
    loader = importlib.machinery.SourceFileLoader("plinius_mobile_data", str(SRC))
    spec = importlib.util.spec_from_loader("plinius_mobile_data", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def context(path, type_, apn, **extra):
    props = {"Name": "Mobile Broadband", "Type": type_, "AccessPointName": apn,
             "Protocol": "dual", "Username": "", "Password": "", "AuthenticationMethod": "none"}
    props.update(extra)
    return [path, {k: {"type": "s", "data": v} for k, v in props.items()}]


class Phone:
    def __init__(self, tmp_path, fake_bin):
        self.d = tmp_path / "phone"
        self.d.mkdir()
        self.bin = fake_bin
        self.state = tmp_path / "state"
        self.devices("/ril_0:gsm:disconnected\n/ril_1:gsm:unavailable\n")
        self.connections("")
        (self.d / "modems").write_text("    /org/freedesktop/ModemManager1/Modem/0 [ofono] binder\n"
                                       "    /org/freedesktop/ModemManager1/Modem/1 [ofono] binder\n")
        self.modem(0, "/ril_0", SIM_PATH)
        self.modem(1, "/ril_1", "--")
        self.sim(SIM_PATH, ICCID)
        self.contexts("/ril_0", [context("/ril_0/context1", "internet", "web.example.net"),
                                 context("/ril_0/context2", "mms", "mms")])
        d = self.d
        fake_bin.stub("nmcli", f'''
case "$*" in
"-t -f DEVICE,TYPE,STATE device") cat "{d}/devices" ;;
"-t -f UUID,TYPE connection show") [ -e "{d}/no-nm" ] && exit 8; cat "{d}/connections" ;;
"-g gsm.sim-id connection show "*) f="{d}/sim-id-$5"; [ -e "$f" ] || exit 10; cat "$f" ;;
"connection add "*) exit ${{ADD_RC:-0}} ;;
*) exit 2 ;;
esac''')
        fake_bin.stub("mmcli", f'''
case "$1" in
-L) cat "{d}/modems" ;;
-m) f="{d}/modem-${{2##*/}}"; [ -e "$f" ] && cat "$f" ;;
-i) f="{d}/sim-${{2##*/}}"; [ -e "$f" ] && cat "$f" ;;
esac''')
        # busctl --json=short call org.ofono DEVICE org.ofono.ConnectionManager GetContexts
        fake_bin.stub("busctl", f'f="{d}/contexts-${{4##*/}}"; [ -e "$f" ] || exit 1; cat "$f"')

    def devices(self, text):
        (self.d / "devices").write_text(text)

    def connections(self, text):
        (self.d / "connections").write_text(text)

    def connection(self, uuid, sim_id):
        (self.d / f"sim-id-{uuid}").write_text(sim_id + "\n")

    def modem(self, n, device, sim):
        (self.d / f"modem-{n}").write_text(
            f"modem.generic.device                            : {device}\n"
            f"modem.generic.sim                               : {sim}\n")

    def sim(self, path, iccid):
        (self.d / f"sim-{path.rsplit('/', 1)[1]}").write_text(
            f"sim.properties.iccid                       : {iccid}\n"
            "sim.properties.operator-code               : 00101\n")

    def contexts(self, device, contexts):
        (self.d / f"contexts-{device.rsplit('/', 1)[1]}").write_text(
            json.dumps({"type": "a(oa{sv})", "data": [contexts]}))

    def added(self):
        return [c[2:] for c in self.bin.calls("nmcli") if c[:2] == ["connection", "add"]]

    def stamps(self):
        return sorted(p.name for p in self.state.iterdir()) if self.state.exists() else []


@pytest.fixture
def phone(tmp_path, fake_bin):
    return Phone(tmp_path, fake_bin)


@pytest.fixture
def md(phone, monkeypatch):
    mod = load()
    monkeypatch.setenv("PATH", phone.bin.env(isolated=True)["PATH"])
    monkeypatch.setattr(mod, "STATE", str(phone.state))
    clock = {"t": 0.0, "sleeps": 0}

    def sleep(s):
        clock["t"] += s
        clock["sleeps"] += 1
        if clock.get("on_sleep"):
            clock["on_sleep"](clock["sleeps"])

    monkeypatch.setattr(mod.time, "sleep", sleep)
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])
    mod.clock = clock
    return mod


def args(cmd):
    """{key: value} of the settings of an nmcli connection add"""
    rest = cmd[cmd.index("con-name") + 2:]
    return dict(zip(rest[::2], rest[1::2]))


def test_a_new_sim_gets_its_access_point_from_ofono(md, phone, capsys):
    assert md.main() == 0
    (add,) = phone.added()
    assert add[:4] == ["type", "gsm", "con-name", "Mobile Broadband"]
    a = args(add)
    assert a == {"connection.autoconnect": "yes", "gsm.apn": "web.example.net", "gsm.sim-id": ICCID,
                 "gsm.home-only": "yes",
                 "ipv4.dns-priority": "120", "ipv4.route-metric": "1050",
                 "ipv6.dns-priority": "120", "ipv6.route-metric": "1050"}
    # A stamp for the SIM, not its ICCID
    assert len(phone.stamps()) == 1 and ICCID not in phone.stamps()[0]
    assert "/ril_0: mobile data with access point web.example.net (Mobile Broadband)" in capsys.readouterr().out
    # The empty slot is given up quietly a minute after the SIM was set up, not after ten
    assert md.EMPTY_S <= md.clock["t"] < md.EMPTY_S + 2 * md.POLL_S


def test_a_second_sim_coming_up_late_is_still_set_up(md, phone):
    """The wait for an empty slot starts anew when it shows a SIM after all."""
    second = "8949000000000000222"

    def later(n):
        if n == 5:
            phone.devices("/ril_0:gsm:disconnected\n/ril_1:gsm:disconnected\n")
            phone.modem(1, "/ril_1", "/org/freedesktop/ModemManager1/SIM/1")
            phone.sim("/org/freedesktop/ModemManager1/SIM/1", second)
            phone.contexts("/ril_1", [context("/ril_1/context1", "internet", "second.example")])

    md.clock["on_sleep"] = later
    assert md.main() == 0
    assert [args(a)["gsm.apn"] for a in phone.added()] == ["web.example.net", "second.example"]
    assert len(phone.stamps()) == 2 and md.clock["t"] < md.EMPTY_S


def test_a_sim_once_set_up_is_left_alone(md, phone):
    assert md.main() == 0
    # The user switched data off and deleted the access point: it stays that way
    phone.bin.calls("nmcli")
    assert md.main() == 0
    assert len(phone.added()) == 1


def test_a_sim_with_an_access_point_of_its_own_gets_none(md, phone):
    phone.connections("1111:802-3-ethernet\n2222:gsm\n")
    phone.connection("2222", ICCID)
    phone.devices("/ril_0:gsm:disconnected\n")
    assert md.main() == 0
    assert phone.added() == []
    # Remembered: deleting it later does not bring one back
    assert len(phone.stamps()) == 1
    assert md.clock["sleeps"] == 0


def test_an_access_point_for_every_sim_counts(md, phone):
    phone.connections("2222:gsm\n")
    phone.connection("2222", "")
    phone.devices("/ril_0:gsm:connected\n")
    assert md.main() == 0
    assert phone.added() == []


def test_another_sims_access_point_does_not_count(md, phone):
    phone.connections("2222:gsm\n")
    phone.connection("2222", "8949000000000000999")
    phone.devices("/ril_0:gsm:disconnected\n")
    assert md.main() == 0
    assert len(phone.added()) == 1


def test_the_sim_and_ofonos_context_may_come_late(md, phone):
    phone.modem(0, "/ril_0", "--")
    (phone.d / "contexts-ril_0").unlink()
    phone.devices("/ril_0:gsm:unavailable\n")

    def later(n):
        if n == 2:
            phone.devices("/ril_0:gsm:disconnected\n")
        if n == 4:
            phone.modem(0, "/ril_0", SIM_PATH)
        if n == 6:
            phone.contexts("/ril_0", [context("/ril_0/context1", "internet", "")])
        if n == 8:
            phone.contexts("/ril_0", [context("/ril_0/context1", "internet", "late.example")])

    md.clock["on_sleep"] = later
    assert md.main() == 0
    (add,) = phone.added()
    assert args(add)["gsm.apn"] == "late.example"
    assert md.clock["sleeps"] == 8


def test_networkmanager_not_answering_is_tried_again(md, phone):
    (phone.d / "no-nm").touch()
    phone.devices("/ril_0:gsm:disconnected\n")
    md.clock["on_sleep"] = lambda n: n == 3 and (phone.d / "no-nm").unlink()
    assert md.main() == 0
    assert len(phone.added()) == 1 and md.clock["sleeps"] == 3


def test_an_unreadable_sim_id_is_tried_again(md, phone):
    phone.connections("2222:gsm\n")
    phone.devices("/ril_0:gsm:disconnected\n")
    md.clock["on_sleep"] = lambda n: n == 2 and phone.connection("2222", ICCID)
    assert md.main() == 0
    assert phone.added() == [] and md.clock["sleeps"] == 2


def test_a_failed_save_is_tried_again_and_reported(md, phone, monkeypatch, capsys):
    monkeypatch.setenv("ADD_RC", "1")
    phone.devices("/ril_0:gsm:disconnected\n")
    md.clock["on_sleep"] = lambda n: n == 2 and monkeypatch.setenv("ADD_RC", "0")
    assert md.main() == 0
    # Failed at the first look and the second, saved at the third
    assert len(phone.added()) == 3
    assert "/ril_0: could not save the access point web.example.net" in capsys.readouterr().out


def test_a_phone_without_any_sim_waits_and_ends(md, phone):
    phone.devices("/ril_0:gsm:unavailable\n/ril_1:gsm:unavailable\n")
    assert md.main() == 0
    assert phone.added() == [] and phone.stamps() == []
    assert md.clock["t"] >= md.WAIT_S


def test_no_modem_at_all(md, phone):
    phone.devices("")
    assert md.main() == 0
    assert phone.added() == []


@pytest.mark.parametrize("protocol, disabled", [("ip", "ipv6"), ("ipv6", "ipv4"), ("dual", None)])
def test_the_protocol_of_the_context(md, phone, protocol, disabled):
    phone.devices("/ril_0:gsm:disconnected\n")
    phone.contexts("/ril_0", [context("/ril_0/context1", "internet", "a.example", Protocol=protocol)])
    md.main()
    a = args(phone.added()[0])
    for ip in ("ipv4", "ipv6"):
        if ip == disabled:
            assert a[f"{ip}.method"] == "disabled" and f"{ip}.route-metric" not in a
        else:
            assert a[f"{ip}.route-metric"] == "1050" and f"{ip}.method" not in a


def test_user_password_and_a_context_without_name(md, phone):
    phone.devices("/ril_0:gsm:disconnected\n")
    phone.contexts("/ril_0", [context("/ril_0/context1", "internet", "b.example", Name="",
                                      Username="user", Password="pass word")])
    md.main()
    (add,) = phone.added()
    assert add[3] == "Mobile Broadband"
    a = args(add)
    assert (a["gsm.username"], a["gsm.password"], a["gsm.password-flags"]) == ("user", "pass word", "0")


@pytest.mark.parametrize("text", ["not json", '{"data": []}', '{"data": [[]]}', "null"])
def test_odd_answers_of_ofono(md, phone, text):
    phone.devices("/ril_0:gsm:disconnected\n")
    (phone.d / "contexts-ril_0").write_text(text)
    assert md.main() == 0
    assert phone.added() == [] and phone.stamps() == []


def test_a_modem_without_a_sim_iccid(md, phone):
    phone.sim(SIM_PATH, "--")
    phone.devices("/ril_0:gsm:disconnected\n")
    assert md.main() == 0
    assert phone.added() == []


def test_a_device_without_its_modem_in_modemmanager(md, phone):
    phone.devices("/ril_7:gsm:disconnected\n")
    assert md.main() == 0
    assert phone.added() == []


def test_nmcli_fields_with_escapes(md):
    assert md.nm_split(r"/a\:b:gsm:x\\y") == ["/a:b", "gsm", "x\\y"]
    assert md.nm_split("a\\") == ["a\\"]
    assert md.keyvalue(None) == {}


def test_runs_as_a_program(phone, tmp_path):
    # As the unit starts it: the state directory from systemd; the SIM has an access point
    phone.devices("/ril_0:gsm:connected\n")
    phone.connections("2222:gsm\n")
    phone.connection("2222", ICCID)
    env = phone.bin.env(isolated=True, STATE_DIRECTORY=str(tmp_path / "s"))
    r = subprocess.run([sys.executable, "-I", str(SRC)], capture_output=True, text=True, env=env,
                       timeout=30)
    assert r.returncode == 0, r.stderr
    assert len(list((tmp_path / "s").iterdir())) == 1


def test_the_unit():
    unit = (PKG / "device-volla-ansuz" / "plinius-mobile-data.service").read_text().splitlines()
    assert "ExecStart=/usr/lib/plinius/plinius-mobile-data" in unit
    assert "StateDirectory=plinius-mobile-data" in unit
    # Waiting for the SIMs must not hold up the boot
    assert "Type=exec" in unit
    assert "After=NetworkManager.service ModemManager.service ofono.service" in unit
