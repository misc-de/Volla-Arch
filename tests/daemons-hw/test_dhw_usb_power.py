# plinius-usb: the power role of the Type-C port (the phone powers the device or takes power: the
# swap asked for by the user or by the preset, refusals, the battery limit, the hold while the
# power changes direction) and the pogo pins of the Plinius Plus (which port carries data, the
# question when both meet, what is written to the driver). The port and the driver's state are
# files in tmp (dhw_usb_phone.py), changed during a run with "put"/"rm"; the driver's answer to a
# swap is scripted ("swap MODE").

import pytest

from dhw_usb_phone import Phone, build


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("usb-power")
    return d, build(d)


@pytest.fixture
def phone(built):
    return Phone(*built).reset()


def swapping(phone, *commands, **typec):
    """A USB PD partner on a dual-role port, as main() finds it, then the commands"""
    phone.typec(**typec)
    return phone.run("init_power", *commands)


SOURCE = r"put typec/port0/power_role [source]\x20sink\n"


# ---- The port ----

def test_no_type_c_port(phone):
    (phone.d / "typec").rmdir()
    r = phone.run("find_port", "init_power", "state", "call SetPowerRole source")
    assert r.of("find_port") == ["port -"]
    assert r.of("init_power") == []
    assert r.of("call SetPowerRole source") == [
        "reply SetPowerRole error g-dbus-error-quark:7: This USB port cannot change the direction of power"]


def test_the_first_port_with_a_power_role(phone):
    phone.put("typec/port0/data_role", "[host] device\n")         # no power role
    phone.put("typec/port1-partner/supports_usb_power_delivery", "yes\n")
    phone.put("typec/port1/power_role", "source [sink]\n")
    phone.put("typec/port1.2/power_role", "source [sink]\n")
    assert phone.run("find_port").of("find_port") == [f"port {phone.d}/typec/port1"]


def test_a_partner_is_found(phone):
    r = swapping(phone, "timers", "state", "get PowerRole", "get PowerRoleSwappable")
    assert r.of("init_power") == ["changed PowerRole 'sink'", "changed PowerRoleSwappable true"]
    # While a partner is there the port is read every few seconds; the role decides what
    # charger detection sees, so the cable is looked at again
    assert r.of("timers") == ["timers power-poll:3000 settle:1000"]
    assert r.of("state")[1] == (f"power port={phone.d}/typec/port0 partner=1 role=sink swappable=1 busy=0 "
                                "hold=0 asked=0 supply=0")
    assert r.of("get PowerRole") == ["prop PowerRole 'sink'"]
    assert r.of("get PowerRoleSwappable") == ["prop PowerRoleSwappable true"]
    assert "USB-C partner attached" in r.stderr


@pytest.mark.parametrize("typec", [
    {"port_type": "sink"},
    {"port_type": "source"},
    {"mode": "default"},
    {"mode": "3.0A"},
    {"pd": "no"},
])
def test_what_cannot_swap(phone, typec):
    r = swapping(phone, "call SetPowerRole source", **typec)
    assert r.of("init_power") == ["changed PowerRole 'sink'"]
    assert r.of("call SetPowerRole source") == [
        "reply SetPowerRole error g-dbus-error-quark:7: The connected device does not support this"]


def test_a_partner_without_the_pd_attribute_may_swap(phone):
    phone.typec()
    (phone.d / "typec/port0-partner/supports_usb_power_delivery").unlink()
    r = phone.run("init_power")
    assert "changed PowerRoleSwappable true" in r.of("init_power")


@pytest.mark.parametrize("text", ["source sink\n", "[none] source\n", "[sink\n"])
def test_a_role_it_cannot_read_is_none(phone, text):
    phone.typec()
    phone.put("typec/port0/power_role", text)
    r = phone.run("init_power", "get PowerRole")
    assert r.of("get PowerRole") == ["prop PowerRole ''"]


def test_without_a_partner_no_role(phone):
    r = swapping(phone, "timers", partner=False)
    assert r.of("init_power") == []
    assert r.of("timers") == ["timers"]


def test_the_partner_goes(phone):
    r = swapping(phone, "advance 3000", "timers", "rm typec/port0-partner", "advance 3000", "timers",
                 "get PowerRole", "get PowerRoleSwappable")
    # The poll keeps reading while the partner is there, and ends with it
    assert "power-poll:3000" in r.of("timers")[0]
    assert r.of("advance 3000", 1) == ["changed PowerRole ''", "changed PowerRoleSwappable false"]
    assert "power-poll" not in r.of("timers", 1)[0]
    assert r.of("get PowerRole") == ["prop PowerRole ''"]
    assert "USB-C partner detached" in r.stderr


def test_a_typec_event_reads_the_port_a_moment_later(phone):
    r = swapping(phone, "uevent typec", "uevent typec", "timers", "rm typec/port0-partner", "advance 200",
                 "timers")
    assert r.of("timers")[0].count("power-refresh:200") == 1
    assert r.of("advance 200") == ["changed PowerRole ''", "changed PowerRoleSwappable false"]
    assert "power-refresh" not in r.of("timers", 1)[0]


def test_a_typec_event_without_a_port(phone):
    (phone.d / "typec").rmdir()
    r = phone.run("init_power", "uevent typec", "timers")
    assert r.of("timers") == ["timers"]


# ---- SetPowerRole ----

def test_power_the_device(phone):
    r = swapping(phone, "call SetPowerRole source", "state", "pump swap", "state", "timers")
    # Asked; the answer comes once the partner agreed
    assert r.of("call SetPowerRole source") == []
    assert "busy=1 hold=1" in r.of("state")[1]
    assert r.of("pump swap") == ["driver power_role source", "changed PowerRole 'source'",
                                 "reply SetPowerRole ok", "pumped swap yes"]
    assert phone.get("typec/port0/power_role") == "[source] sink\n"
    # Charger detection follows the new direction for a while: the cable is looked at after it
    assert "busy=0 hold=1" in r.of("state", 1)[1]
    assert "settle:3000" in r.of("timers")[0]
    assert "Power role: asking the partner for source" in r.stderr


def test_take_power_again_needs_nobody(phone):
    r = swapping(phone, "bus locked 1", "bus caller 4321", "call SetPowerRole sink", "pump swap",
                 role="source")
    assert r.of("pump swap") == ["driver power_role sink", "changed PowerRole 'sink'",
                                 "reply SetPowerRole ok", "pumped swap yes"]


@pytest.mark.parametrize("bus,message", [
    ("bus locked 1", "Unlock the phone first"),
    ("bus caller 4321", "Only the user in front of the phone can do this"),
])
def test_powering_a_device_needs_the_unlocked_user(phone, bus, message):
    r = swapping(phone, bus, "call SetPowerRole source")
    assert r.of("call SetPowerRole source") == [f"reply SetPowerRole error g-dbus-error-quark:9: {message}"]
    assert "driver power_role" not in r.stdout


def test_the_role_it_has(phone):
    r = swapping(phone, "bus locked 1", "call SetPowerRole sink")
    assert r.of("call SetPowerRole sink") == ["reply SetPowerRole ok"]


def test_unknown_power_role(phone):
    r = swapping(phone, "call SetPowerRole dual")
    assert r.of("call SetPowerRole dual") == [
        "reply SetPowerRole error g-dbus-error-quark:16: Unknown power role dual"]


def test_nothing_connected(phone):
    r = swapping(phone, "call SetPowerRole source", partner=False)
    assert r.of("call SetPowerRole source") == [
        "reply SetPowerRole error g-dbus-error-quark:0: Nothing is connected"]


@pytest.mark.parametrize("capacity,ok", [("10", False), ("3", False), ("11", True)])
def test_no_powering_at_a_low_battery(phone, capacity, ok):
    phone.put("power_supply/battery/capacity", capacity + "\n")
    r = swapping(phone, "battery", "call SetPowerRole source", "pump swap")
    assert r.of("battery") == [f"battery {capacity} low={0 if ok else 1}"]
    if ok:
        assert "reply SetPowerRole ok" in r.of("pump swap")
    else:
        assert r.of("call SetPowerRole source") == [
            "reply SetPowerRole error g-dbus-error-quark:0: The battery is too low to power a device"]


def test_no_battery(phone):
    import shutil
    shutil.rmtree(phone.d / "power_supply/battery")
    # The KVM preview: a supply that is no battery, one without a level
    phone.put("power_supply/ac/type", "Mains\n")
    phone.put("power_supply/bms/type", "Battery\n")
    assert phone.run("battery").of("battery") == ["battery -1 low=0"]


def test_one_swap_at_a_time(phone):
    r = swapping(phone, "set busy 1", "call SetPowerRole source")
    assert r.of("call SetPowerRole source") == [
        "reply SetPowerRole error g-dbus-error-quark:0: The power is switching already"]


@pytest.mark.parametrize("mode,role,message", [
    ("refuse", "source", "The connected device does not take power from the phone"),
    ("refuse", "sink", "The connected device does not supply power"),
    ("silent", "source", "The connected device does not take power from the phone"),
    ("eio", "source", "The connected device does not support this"),
    ("eopnotsupp", "source", "This USB port cannot change the direction of power"),
    ("enxio", "source", "Switching the power failed: No such device or address"),
    ("enoent", "source", "Switching the power failed: No such file or directory"),
    ("vanish", "source", "Nothing is connected"),
])
def test_a_swap_that_does_not_happen(phone, mode, role, message):
    start = "sink" if role == "source" else "source"
    r = swapping(phone, f"swap {mode}", f"call SetPowerRole {role}", "pump swap", role=start)
    out = r.of("pump swap")
    assert out[-2:] == [f"reply SetPowerRole error g-dbus-error-quark:0: {message}", "pumped swap yes"]
    assert f"Power role {role} asked for: {message}" in r.stderr


def test_the_user_wins_over_the_preset(phone):
    phone.typec()
    r = phone.run("set supply 1", "init_power", "timers", "call SetPowerRole source", "timers", "pump swap",
                  "advance 2000")
    assert "power-preset:1000" in r.of("timers")[0]
    assert "power-preset" not in r.of("timers", 1)[0]
    assert "reply SetPowerRole ok" in r.of("pump swap")
    assert r.stdout.count("driver power_role") == 1


# ---- The preset: power every PD partner at plug-in ----

def preset(phone, *commands, **typec):
    """SupplyPowerOnConnect on, then a partner is plugged in"""
    phone.typec(partner=False, **typec)
    return phone.run("set supply 1", "init_power", r"put typec/port0-partner/supports_usb_power_delivery yes\n",
                     "power_refresh", "timers", *commands)


def test_preset_powers_the_device(phone):
    r = preset(phone, "advance 999", "advance 1", "pump swap", "state")
    assert "power-preset:1000" in r.of("timers")[0]
    assert r.of("advance 999") == []
    assert r.of("pump swap")[:2] == ["driver power_role source", "changed PowerRole 'source'"]
    # Nobody asked, nobody gets an answer
    assert not [line for line in r.lines if line.startswith("reply")]
    assert "role=source" in r.of("state")[1]


def test_preset_also_while_locked(phone):
    r = preset(phone, "bus locked 1", "advance 1000", "pump swap")
    assert "changed PowerRole 'source'" in r.of("pump swap")


def test_preset_refused_keeps_taking_power(phone):
    r = preset(phone, "swap refuse", "advance 1000", "pump swap", "timers")
    assert "Power role source of the preset: The connected device does not take power from the phone" in r.stderr
    assert "power-preset" not in r.of("timers", 1)[0]


def test_preset_waits_for_the_pd_contract(phone):
    r = preset(phone, "advance 7000", "timers", "advance 1000", "timers", mode="default")
    assert "power-preset:1000" in r.of("timers", 1)[0]
    assert "power-preset" not in r.of("timers", 2)[0]
    assert "The USB-C partner has no PD contract, the phone keeps taking power" in r.stderr
    assert "driver power_role" not in r.stdout


def test_preset_once_the_contract_is_there(phone):
    r = preset(phone, "advance 3000", r"put typec/port0/power_operation_mode usb_power_delivery\n",
               "advance 1000", "pump swap", mode="default")
    assert r.of("advance 3000") == []
    assert "changed PowerRole 'source'" in r.of("pump swap")


def test_preset_waits_for_a_swap_running(phone):
    r = preset(phone, "set busy 1", "advance 1000", "timers", "set busy 0", "advance 1000", "pump swap")
    assert "power-preset:1000" in r.of("timers", 1)[0]
    assert "changed PowerRole 'source'" in r.of("pump swap")


def test_preset_not_at_a_low_battery(phone):
    phone.put("power_supply/battery/capacity", "8\n")
    r = preset(phone, "advance 1000", "timers")
    assert "The battery is at 8 %, the phone keeps taking power" in r.stderr
    assert "power-preset" not in r.of("timers", 1)[0]
    assert "driver power_role" not in r.stdout


@pytest.mark.parametrize("change", [SOURCE, "setprop SupplyPowerOnConnect false", "rm typec/port0-partner"])
def test_preset_has_nothing_left_to_do(phone, change):
    r = preset(phone, change, "advance 1000", "timers", "advance 10000")
    # No swap asked for, and the preset's timer is gone
    assert "driver power_role" not in r.stdout
    assert "power-preset" not in r.of("timers", 1)[0]


def test_no_preset_without_the_setting(phone):
    r = swapping(phone, "timers")
    assert "power-preset" not in r.of("timers")[0]


# ---- The battery runs low while the phone powers the device ----

def test_low_battery_hands_the_power_back(phone):
    phone.put("power_supply/battery/capacity", "9\n")
    r = swapping(phone, "pump swap", "state", role="source")
    assert r.of("init_power") == ["changed PowerRole 'source'", "changed PowerRoleSwappable true",
                                  "changed Error 'The battery is low, the phone stopped powering the device'"]
    assert r.of("pump swap")[:2] == ["driver power_role sink", "changed PowerRole 'sink'"]
    assert "The battery is at 9 %, the phone stops powering the device" in r.stderr
    assert "asked=0" in r.of("state")[1]


def test_low_battery_asks_once(phone):
    phone.put("power_supply/battery/capacity", "9\n")
    r = swapping(phone, "swap refuse", "pump swap", "power_refresh", "advance 3000", "state", role="source")
    assert r.stdout.count("driver power_role sink") == 1
    assert "role=source" in r.of("state")[1]
    assert "asked=1" in r.of("state")[1]


def test_a_device_that_only_takes_power_keeps_it(phone):
    phone.put("power_supply/battery/capacity", "9\n")
    r = swapping(phone, "state", role="source", mode="default")
    assert "driver power_role" not in r.stdout
    assert "asked=0" in r.of("state")[1]


# ---- While the power changes direction the computer stays attached ----

def test_a_swap_holds_the_computer(phone):
    # Charger detection no longer sees the computer once the phone powers it; meanwhile it stays
    phone.computer()
    r = swapping(phone, "set connected 1", "call SetPowerRole source", r"put power_supply/usb/online 0\n",
                 "read_connected", "pump swap", "read_connected", "advance 2999", "state", "advance 1",
                 data="host")
    assert r.of("read_connected") == ["read_connected 1"]
    assert r.of("read_connected", 1) == ["read_connected 1"]
    assert r.of("state")[0].startswith("state connected=1")
    assert r.of("advance 1")[0] == "changed Connected false"


def test_a_swap_the_partner_asked_for_holds_too(phone):
    phone.computer()
    r = swapping(phone, "set connected 1", r"put power_supply/usb/online 0\n", SOURCE, "power_refresh",
                 "state", "timers", "read_connected", "advance 3000", data="host")
    assert r.of("power_refresh") == ["changed PowerRole 'source'"]
    assert "hold=1" in r.of("state")[1]
    assert "settle:3000" in r.of("timers")[0]
    assert r.of("read_connected") == ["read_connected 1"]
    assert r.of("advance 3000")[0] == "changed Connected false"


def test_no_hold_once_the_cable_is_out(phone):
    r = swapping(phone, "set connected 1", SOURCE, "power_refresh", "rm typec/port0-partner", "read_connected",
                 data="host")
    assert r.of("read_connected") == ["read_connected 0"]


def test_a_computer_the_phone_powers_stays(phone):
    # After the swap the computer is the host on the cable: attached as long as it stays so
    r = swapping(phone, "set connected 1", "call SetPowerRole source", "pump swap", "advance 3000", "state")
    assert r.of("advance 3000") == []
    assert r.of("state")[0].startswith("state connected=1")


# ---- Pogo pins ----

def pogo(phone, flag, *commands):
    phone.put("pogo_state", f"{flag}\n")
    return phone.run("pogo_init", *commands)


def test_no_pogo_pins(phone):
    r = phone.run("pogo_init", "uevent extcon", "timers", "state")
    assert r.of("timers") == ["timers"]
    assert r.of("state")[2].startswith("pogo present=0")


@pytest.mark.parametrize("flag,active,ticks", [
    (0, "none", False), (1, "usb-c", True), (2, "pogo", True), (3, "usb-c", True), (4, "usb-c", True),
    (5, "pogo", True), (6, "usb-c", True),
])
def test_pogo_at_start(phone, flag, active, ticks):
    r = pogo(phone, flag, "timers", "get ActivePort", "advance 10000")
    assert r.of("get ActivePort") == [f"prop ActivePort '{active}'"]
    assert r.of("timers") == (["timers pogo-tick:1000"] if ticks else ["timers"])
    assert f"Pogo state {flag}, active USB port: {active}" in r.stderr
    # Both there at the start: the driver decided long ago, nothing is asked
    assert "PortQuestion" not in r.stdout


def test_accessory_alone_takes_the_port(phone):
    r = pogo(phone, 0, r"put pogo_state 5\n", "uevent extcon", "timers", "advance 1000", "advance 10000",
             "timers", r"put pogo_state 0\n", "advance 1000", "timers")
    assert r.of("timers") == ["timers pogo-tick:1000"]
    assert r.of("advance 1000") == ["changed ActivePort 'pogo'"]
    # Read while it is attached: the driver tells nothing when a cable joins
    assert r.of("timers", 1) == ["timers pogo-tick:1000"]
    assert r.of("advance 1000", 1) == ["changed ActivePort 'none'"]
    # Nothing attached: the next plug-in is an event again
    assert r.of("timers", 2) == ["timers"]
    assert "PortQuestion" not in r.stdout
    assert phone.get("pogo_state") == "0\n"


def test_reads_go_on_for_a_while_after_an_event(phone):
    r = pogo(phone, 0, "uevent extcon", "advance 4000", "timers", "advance 1000", "timers")
    assert r.of("timers") == ["timers pogo-tick:1000"]
    assert r.of("timers", 1) == ["timers"]


def test_a_second_event_starts_the_reads_again(phone):
    r = pogo(phone, 0, "uevent extcon", "advance 3000", "uevent power_supply USB", "timers", "advance 4000",
             "timers", "advance 1000", "timers")
    # One timer, five more reads
    assert r.of("timers")[0].count("pogo-tick") == 1
    assert "pogo-tick" in r.of("timers", 1)[0]
    assert "pogo-tick" not in r.of("timers", 2)[0]


def test_a_state_that_cannot_be_read_stays(phone):
    r = pogo(phone, 5, "rm pogo_state", "advance 1000", "get ActivePort")
    assert r.of("advance 1000") == []
    assert r.of("get ActivePort") == ["prop ActivePort 'pogo'"]


def conflict(phone, flag, *commands, start=6, connected=True):
    """Something on USB-C (start), then the driver reports the conflict `flag` at the next read"""
    pre = ["set connected 1"] if connected else []
    return pogo(phone, start, *pre, rf"put pogo_state {flag}\n", "advance 1000", *commands)


def test_computer_then_accessory_asks(phone):
    r = conflict(phone, 1, "timers", "get PortQuestion", "call ChoosePort pogo", "state", "timers")
    assert r.of("advance 1000") == ["changed PortQuestion 'usb-c'"]
    assert "pogo-window:7500" in r.of("timers")[0]
    assert r.of("get PortQuestion") == ["prop PortQuestion 'usb-c'"]
    assert r.of("call ChoosePort pogo") == ["changed ActivePort 'pogo'", "changed PortQuestion ''",
                                            "reply ChoosePort ok"]
    # The driver is told to switch: "1" with USB-C first
    assert phone.get("pogo_state") == "1\n"
    assert "pogo-window" not in r.of("timers", 1)[0]
    assert "Pogo state 6 -> 1" in r.stderr


def test_keep_usb_c_when_asked(phone):
    r = conflict(phone, 1, "call ChoosePort usb-c")
    assert r.of("call ChoosePort usb-c") == ["changed PortQuestion ''", "reply ChoosePort ok"]
    assert phone.get("pogo_state") == "2\n"


def test_accessory_first_then_computer(phone):
    r = conflict(phone, 2, "call ChoosePort usb-c", start=5)
    assert r.of("advance 1000") == ["changed PortQuestion 'pogo'"]
    assert r.of("call ChoosePort usb-c") == ["changed ActivePort 'usb-c'", "changed PortQuestion ''",
                                             "reply ChoosePort ok"]
    assert phone.get("pogo_state") == "1\n"


def test_accessory_first_keeping_it_writes_nothing(phone):
    # With power from USB-C, the driver's own way after its timeout is the right one
    r = conflict(phone, 2, "call ChoosePort pogo", start=5)
    assert r.of("call ChoosePort pogo") == ["changed PortQuestion ''", "reply ChoosePort ok"]
    assert phone.get("pogo_state") == "2\n"


def test_a_device_the_phone_powers_is_worth_the_question(phone):
    r = conflict(phone, 3, start=4, connected=False)
    assert r.of("advance 1000") == ["changed PortQuestion 'usb-c'"]


def test_without_an_answer_what_was_active_stays(phone):
    r = conflict(phone, 1, "advance 7499", "advance 1", "timers")
    assert r.of("advance 7499") == []
    assert r.of("advance 1") == ["changed PortQuestion ''"]
    assert phone.get("pogo_state") == "2\n"
    assert "pogo-window" not in r.of("timers")[0]


def test_a_charger_and_an_accessory_ask_nothing(phone):
    # A wall charger has no data: the accessory gets the port when the time is up
    phone.computer("DCP")
    r = conflict(phone, 1, "advance 7500", connected=False)
    assert r.of("advance 1000") == []
    assert r.of("advance 7500") == ["changed ActivePort 'pogo'"]
    assert phone.get("pogo_state") == "1\n"


@pytest.mark.parametrize("bus", ["bus locked 1", "bus session -"])
def test_no_question_on_a_locked_phone(phone, bus):
    r = pogo(phone, 6, bus, "set connected 1", r"put pogo_state 1\n", "advance 1000", "advance 7500")
    assert r.of("advance 1000") == []
    # Its port stays: an accessory cannot change it without the owner
    assert r.of("advance 7500") == []
    assert phone.get("pogo_state") == "2\n"


@pytest.mark.parametrize("ticks,asked", [(5, True), (6, False)])
def test_a_question_needs_time_left_to_answer(phone, ticks, asked):
    # Charger detection is late: the computer shows up after some reads
    r = conflict(phone, 1, f"advance {ticks * 1000 - 1}", r"put power_supply/usb/online 1\n",
                 r"put power_supply/usb/usb_type [SDP]\n", "advance 1", "advance 5000", connected=False)
    assert r.of("advance 1000") == []
    if asked:
        assert r.of("advance 1") == ["changed PortQuestion 'usb-c'"]
    else:
        assert r.of("advance 1") == []
        # Too late to ask, but USB-C has a computer: it stays
        assert phone.get("pogo_state") == "2\n"


def test_the_conflict_ends_without_an_answer(phone):
    r = conflict(phone, 1, r"put pogo_state 6\n", "advance 1000", "timers")
    assert r.of("advance 1000", 1) == ["changed PortQuestion ''"]
    assert "pogo-window" not in r.of("timers")[0]
    assert phone.get("pogo_state") == "6\n"


def test_a_conflict_after_a_conflict(phone):
    r = conflict(phone, 1, r"put pogo_state 3\n", "advance 1000", "state")
    assert r.of("advance 1000", 1) == ["changed PortQuestion ''", "changed PortQuestion 'usb-c'"]
    assert "window=1" in r.of("state")[2]


@pytest.mark.parametrize("command,reply", [
    ("call ChoosePort hdmi", "error g-dbus-error-quark:16: Unknown port hdmi"),
    ("call ChoosePort pogo", "error g-dbus-error-quark:0: There is no question open"),
])
def test_choose_port_without_a_question(phone, command, reply):
    assert pogo(phone, 6, command).of(command) == [f"reply ChoosePort {reply}"]


@pytest.mark.parametrize("bus,message", [
    ("bus locked 1", "Unlock the phone first"),
    ("bus caller 4321", "Only the user in front of the phone can do this"),
])
def test_choose_port_by_someone_else(phone, bus, message):
    r = conflict(phone, 1, bus, "call ChoosePort pogo")
    assert r.of("call ChoosePort pogo") == [f"reply ChoosePort error g-dbus-error-quark:9: {message}"]
    assert phone.get("pogo_state") == "1\n"
