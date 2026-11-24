# plinius-aml (Advanced Mobile Location, ETSI TS 103 625): the message of Annex A (fields, no
# location, partial IMSI/IMEI, message length), its GSM 7-bit packing in both bit orders, the
# NMEA parsing (GGA, GST, checksums) and the configuration; then the daemon itself on a private
# bus: a fake oFono announces calls and takes SendDataMessage, a fake gnss-nmea-bridge socket
# delivers fixes. Nothing is ever sent anywhere: the "modem" is a Python object in the test.

import math
import os
import socket
import tempfile
import threading
import time
import warnings
from pathlib import Path

import pytest

from dhw_daemon_bus import Daemon, PrivateBus, poll, sanitized_env
from dhw_harness import PKG, build_harness, run, sandboxed_source

AML = PKG / "plinius-aml"
SRC = AML / "plinius-aml.c"

HARNESS = r"""
#define main real_main
#include "plinius-aml.c"
#undef main

static void print_country(gpointer key, gpointer value, gpointer data)
{
	Country *c = value;
	g_autofree char *nums = c->numbers ? g_strjoinv(",", c->numbers) : g_strdup("*");
	printf("country=%s enabled=%d number=%s roaming=%s port=%d source=%d legacy=%d partial=%d t1=%d numbers=%s\n",
	       c->mcc, c->enabled, c->number ? c->number : "-", c->roaming_number ? c->roaming_number : "-",
	       c->port, c->source_port, c->legacy_packing, c->partial_imei, c->t1, nums);
}

int main(int argc, char **argv)
{
	setvbuf(stdout, NULL, _IOLBF, 0);
	if (argc > 1 && !strcmp(argv[1], "daemon"))
		return real_main(argc - 1, argv + 1);
	if (argc == 4 && !strcmp(argv[1], "pack")) {
		g_autoptr(GBytes) b = pack_gsm7(argv[3], !strcmp(argv[2], "legacy"));
		gsize n;
		const guint8 *d;
		if (!b) {
			printf("NULL\n");
			return 0;
		}
		d = g_bytes_get_data(b, &n);
		for (gsize i = 0; i < n; i++)
			printf("%02x", d[i]);
		printf("\n");
		return 0;
	}
	if (argc == 14 && !strcmp(argv[1], "compose")) {
		/* compose fix|none lat lon hdop sigma imsi home_len imei partial mcc mnc top_s */
		Fix fix = {.valid = TRUE, .lat = g_ascii_strtod(argv[3], NULL), .lon = g_ascii_strtod(argv[4], NULL),
			   .hdop = g_ascii_strtod(argv[5], NULL)};
		double sigma = g_ascii_strtod(argv[6], NULL);
		fix.sigma_lat = fix.sigma_lon = sigma;
		Facts f = {.fix = !strcmp(argv[2], "fix") ? &fix : NULL,
			   .top_us = g_ascii_strtoll(argv[13], NULL, 10) * G_USEC_PER_SEC,
			   .imsi = *argv[7] ? argv[7] : NULL, .home_len = atoi(argv[8]),
			   .imei = *argv[9] ? argv[9] : NULL, .partial_imei = atoi(argv[10]),
			   .mcc = *argv[11] ? argv[11] : NULL, .mnc = *argv[12] ? argv[12] : NULL};
		g_autofree char *text = aml_compose(&f);
		printf("%s\n", text ? text : "NULL");
		return 0;
	}
	if (argc > 2 && !strcmp(argv[1], "nmea")) {
		Fix fix = {.hdop = -1, .sigma_lat = -1, .sigma_lon = -1};
		for (int i = 2; i < argc; i++) {
			gboolean got = nmea_parse(argv[i], &fix, 1000);
			printf("got=%d valid=%d lat=%.6f lon=%.6f radius=%d\n", got, fix.valid, fix.lat, fix.lon,
			       fix.valid ? fix_radius(&fix) : -1);
		}
		return 0;
	}
	if (argc == 3 && !strcmp(argv[1], "config")) {
		Config c = {0};
		config_load(&c, argv[2]);
		config_load(&c, argv[2]); /* a second load replaces the first */
		printf("t1=%d good=%d minbattery=%d\n", c.t1, c.good_accuracy, c.min_battery);
		g_hash_table_foreach(c.countries, print_country, NULL);
		config_clear(&c);
		return 0;
	}
	return 2;
}
"""


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    d = tmp_path_factory.mktemp("aml")
    sandboxed_source(SRC, d, [
        ('"/etc/plinius-aml/aml.conf"', f'"{d}/aml.conf"'),
        ('"/run/gnss-nmea-bridge/nmea.sock"', f'"{d}/nmea.sock"'),
        ('"/sys/class/power_supply/battery"', f'"{d}/battery"'),
    ])
    # The daemon runs as a separate process: without sanitizers, they would only slow the waits
    return build_harness(d, "aml", HARNESS, pkgs=("gio-unix-2.0",), sanitize=False)


def h(harness, *args):
    return run(harness, *args).stdout


# ---- GSM 7-bit packing, checked against an independent implementation ----

def pack_reference(text, legacy):
    bits = []
    for ch in text.encode("ascii"):
        if legacy:
            bits += [(ch >> b) & 1 for b in range(6, -1, -1)]
        else:
            bits += [(ch >> b) & 1 for b in range(7)]
    out = bytearray((len(bits) + 7) // 8)
    for i, bit in enumerate(bits):
        if bit:
            out[i // 8] |= (0x80 >> (i % 8)) if legacy else (1 << (i % 8))
    return bytes(out)


def unpack(data, n, legacy):
    chars = []
    for i in range(n):
        c = 0
        for b in range(7):
            pos = i * 7 + b
            if legacy:
                bit = (data[pos // 8] >> (7 - pos % 8)) & 1
                c = (c << 1) | bit
            else:
                c |= ((data[pos // 8] >> (pos % 8)) & 1) << b
        chars.append(chr(c))
    return "".join(chars)


def decode(data, legacy=False):
    """The AML text of packed bytes: as many characters as fit, cut where ml= says"""
    text = unpack(data, len(data) * 8 // 7, legacy)
    ml = int(text.split("ml=")[1].split(";")[0].rstrip("@" + chr(0)))
    return text[:ml]


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("text", ["A", "AB", "hellohello", 'A"ML=1;lt=+54.76397;lg=-0.18305;ml=31', "x" * 152])
def test_pack_matches_reference(harness, legacy, text):
    hexed = h(harness, "pack", "legacy" if legacy else "gsm", text).strip()
    assert bytes.fromhex(hexed) == pack_reference(text, legacy)
    assert unpack(bytes.fromhex(hexed), len(text), legacy) == text


def test_pack_gsm_known_value(harness):
    # The textbook example of TS 23.038: "hellohello" -> E8329BFD4697D9EC37
    assert h(harness, "pack", "gsm", "hellohello").strip() == "e8329bfd4697d9ec37"


@pytest.mark.parametrize("text", ["ä", "a b", "a_b", "@", "{"])
def test_pack_refuses_characters_outside_the_shared_set(harness, text):
    assert h(harness, "pack", "gsm", text).strip() == "NULL"


def test_pack_empty_text_gives_no_bytes(harness):
    assert h(harness, "pack", "gsm", "").strip() == ""


# ---- the message ----

TOP = 1373983175  # 2013-07-16 13:59:35 UTC


def compose(harness, fix="fix", lat="54.76397", lon="-0.18305", hdop="2", sigma="-1", imsi="234301234567890",
            home_len="5", imei="356708041746734", partial="0", mcc="234", mnc="30", top=str(TOP)):
    return h(harness, "compose", fix, lat, lon, hdop, sigma, imsi, home_len, imei, partial, mcc, mnc, top).strip()


def fields(text):
    assert text.startswith('A"ML=1;')
    return dict(part.split("=", 1) for part in text.split(";"))


def test_message_with_a_fix(harness):
    text = compose(harness)
    assert text == ('A"ML=1;lt=+54.76397;lg=-0.18305;rd=10;top=20130716135935;lc=68;pm=G;'
                    'si=234300000000000;ei=356708041746734;mcc=234;mnc=30;ml=' + str(len(text)))
    assert int(fields(text)["ml"]) == len(text)


def test_message_length_counts_its_own_digits(harness):
    # Around 99/100 characters the length field grows by a digit
    for imei in ("", "356708041746734"):
        text = compose(harness, imei=imei)
        assert int(fields(text)["ml"]) == len(text)


def test_message_without_location(harness):
    text = compose(harness, fix="none")
    f = fields(text)
    assert (f["lt"], f["lg"], f["rd"], f["lc"], f["pm"]) == ("+00.00000", "+000.00000", "N", "0", "N")
    assert f["top"] == "20130716135935"
    assert int(f["ml"]) == len(text)


def test_coordinates_signs_and_rounding(harness):
    f = fields(compose(harness, lat="-33.8688197", lon="151.2092955"))
    assert (f["lt"], f["lg"]) == ("-33.86882", "+151.20930")
    # A value that rounds to zero carries no minus sign
    f = fields(compose(harness, lat="-0.000001", lon="0"))
    assert (f["lt"], f["lg"]) == ("+0.00000", "+0.00000")


def test_radius_from_gst_sigma_else_hdop(harness):
    assert fields(compose(harness, sigma="3"))["rd"] == str(math.ceil(math.sqrt(18)))
    assert fields(compose(harness, hdop="0.8"))["rd"] == "4"
    # Without any quality the radius is that of an HDOP of 10, never 0
    assert fields(compose(harness, hdop="-1"))["rd"] == "50"
    assert fields(compose(harness, sigma="100000"))["rd"] == "99999"


def test_imsi_keeps_mcc_and_mnc_only(harness):
    assert fields(compose(harness, imsi="262019876543210", home_len="5"))["si"] == "262010000000000"
    assert fields(compose(harness, imsi="310260123456789", home_len="6"))["si"] == "310260000000000"
    # No SIM identity, or garbage: zeros
    assert fields(compose(harness, imsi=""))["si"] == "000000000000000"
    assert fields(compose(harness, imsi="26201abc"))["si"] == "000000000000000"


def test_imei_full_partial_and_missing(harness):
    assert fields(compose(harness, partial="1"))["ei"] == "356708040000000"
    assert fields(compose(harness, imei=""))["ei"] == "000000000000000"
    assert fields(compose(harness, imei="12345"))["ei"] == "000000000000000"


def test_network_codes_fall_back_to_zeros(harness):
    f = fields(compose(harness, mcc="", mnc="1"))
    assert (f["mcc"], f["mnc"]) == ("000", "00")
    f = fields(compose(harness, mcc="262", mnc="01"))
    assert (f["mcc"], f["mnc"]) == ("262", "01")


def test_longest_message_fits_one_sms(harness):
    text = compose(harness, lat="-89.999999", lon="-179.999999", sigma="70000", imei="3567080417467341",
                   mcc="310", mnc="260", home_len="6")
    assert len(text) <= 152
    assert h(harness, "pack", "gsm", text).strip() != "NULL"


# ---- NMEA ----

def nmea(body):
    s = 0
    for ch in body.encode():
        s ^= ch
    return f"${body}*{s:02X}"


GGA = nmea("GNGGA,123519.00,4807.0380,N,01131.0000,E,1,08,0.9,545.4,M,46.9,M,,")


def parse(harness, *lines):
    out = []
    for line in h(harness, "nmea", *lines).splitlines():
        out.append(dict(kv.split("=") for kv in line.split()))
    return out


def test_gga_gives_a_fix(harness):
    (r,) = parse(harness, GGA)
    assert r["got"] == "1" and r["valid"] == "1"
    assert float(r["lat"]) == pytest.approx(48 + 7.038 / 60, abs=1e-6)
    assert float(r["lon"]) == pytest.approx(11 + 31 / 60, abs=1e-6)
    assert r["radius"] == "5"  # HDOP 0.9 x 5 m


def test_southern_and_western_hemispheres(harness):
    (r,) = parse(harness, nmea("GPGGA,000001,3352.1292,S,15112.5577,W,1,05,1.0,,,,,,"))
    assert float(r["lat"]) < 0 and float(r["lon"]) < 0


def test_gst_refines_the_radius_either_order(harness):
    gst = nmea("GNGST,123519.00,1.2,2.0,1.5,30,6.0,8.0,5.0")
    after = parse(harness, GGA, gst)
    assert after[0]["radius"] == "5"  # HDOP 0.9 x 5 m
    assert after[1]["got"] == "1" and after[1]["radius"] == "10"  # sqrt(36 + 64)
    before = parse(harness, gst, GGA)
    assert before[0]["got"] == "0" and before[0]["valid"] == "0"
    assert before[1]["radius"] == "10"
    # GST of another epoch does not touch the fix
    other = parse(harness, GGA, nmea("GNGST,123520.00,1,1,1,0,30.0,40.0,5"))
    assert other[1]["got"] == "0"


@pytest.mark.parametrize("line", [
    GGA[:-2] + "00",                                                     # wrong checksum
    GGA.replace("$", ""),                                                # no $
    "$GNGGA,123519.00,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,",  # no checksum
    nmea("GNGGA,123519.00,4807.038,N,01131.000,E,0,00,99.9,,,,,,"),       # quality 0: no fix
    nmea("GNGGA,123519.00,4807.038,N,01131.000,E,,00,99.9,,,,,,"),        # quality empty
    nmea("GNGGA,123519.00,,,,,1,08,0.9,,,,,,"),                         # no position
    nmea("GNGGA,123519.00,4807.038,X,01131.000,E,1,08,0.9,,,,,,"),       # bad hemisphere
    nmea("GNGGA,123519.00,4807.038,,01131.000,E,1,08,0.9,,,,,,"),        # missing hemisphere
    nmea("GNGGA,123519.00,48x7.038,N,01131.000,E,1,08,0.9,,,,,,"),       # not a number
    nmea("GNGGA,123519.00,-4807.038,N,01131.000,E,1,08,0.9,,,,,,"),      # negative
    nmea("GNGGA,1,2"),                                                  # too short
    nmea("GNRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,,"),  # other sentences
    nmea("GNGST,123519.00,1,1,1,0,,4.0,5"),                             # GST without sigma
    nmea("GNGST,,1,1,1,0,3.0,4.0,5"),                                   # GST without time
    nmea("GGA,1,2,3,4,5,6,7,8,9"),                                      # no talker
    "$GNGGA*zz",
])
def test_lines_without_a_fix_are_ignored(harness, line):
    (r,) = parse(harness, line)
    assert r["got"] == "0" and r["valid"] == "0"


def test_gga_hdop_missing_gives_the_default_radius(harness):
    (r,) = parse(harness, nmea("GPGGA,000001,4807.0380,N,01131.0000,E,1,05,,,,,,,"))
    assert r["radius"] == "50"


# ---- configuration ----

def config(harness, tmp_path, text):
    p = tmp_path / "aml.conf"
    p.write_text(text)
    r = run(harness, "config", p)
    lines = r.stdout.splitlines()
    general = dict(kv.split("=") for kv in lines[0].split())
    countries = {}
    for line in lines[1:]:
        d = dict(kv.split("=", 1) for kv in line.split())
        countries[d["country"]] = d
    return general, countries, r.stderr


def test_shipped_configuration_sends_nowhere(harness, tmp_path):
    general, countries, err = config(harness, tmp_path, (AML / "aml.conf").read_text())
    assert general == {"t1": "20", "good": "30", "minbattery": "3"}
    assert set(countries) == {"262"}
    assert all(c["enabled"] == "0" for c in countries.values())
    assert err == ""


def test_missing_file_means_no_country(harness, tmp_path):
    r = run(harness, "config", tmp_path / "missing.conf")
    assert r.stdout.splitlines() == ["t1=20 good=30 minbattery=3"]
    assert "AML is off in every country" in r.stderr


def test_country_settings(harness, tmp_path):
    general, countries, err = config(harness, tmp_path, """
[General]
T1=15
GoodAccuracy=50
MinBattery=5
[Country 001]
Enabled=true
Number=+00112
RoamingNumber=+001112
Port=12345
SourcePort=0
Packing=legacy
IMEI=partial
EmergencyNumbers=112;110
[Country 002]
Enabled=true
Number=112
Port=1
T1=3
EmergencyNumbers=
""")
    assert general == {"t1": "15", "good": "50", "minbattery": "5"}
    assert countries["001"] == {"country": "001", "enabled": "1", "number": "+00112", "roaming": "+001112",
                                "port": "12345", "source": "0", "legacy": "1", "partial": "1", "t1": "15",
                                "numbers": "112,110"}
    assert countries["002"] == {"country": "002", "enabled": "1", "number": "112", "roaming": "-", "port": "1",
                                "source": "1", "legacy": "0", "partial": "0", "t1": "3", "numbers": "*"}
    assert err == ""


def test_bad_values_fall_back_or_switch_off(harness, tmp_path):
    general, countries, err = config(harness, tmp_path, """
[General]
T1=0
GoodAccuracy=many
[Country 26]
Enabled=true
[Country 262x]
Enabled=true
[Country 003]
Enabled=yes please
Number=112
Port=7
[Country 004]
Enabled=true
Number=11a2
Port=7
RoamingNumber=+
[Country 005]
Enabled=true
Number=112
Port=70000
Packing=ucs2
IMEI=some
[Country 006]
Enabled=true
Number=+
Port=7
""")
    assert general["t1"] == "20" and general["good"] == "30"
    assert set(countries) == {"003", "004", "005", "006"}
    assert all(c["enabled"] == "0" for c in countries.values())
    assert countries["004"]["roaming"] == "-"
    assert (countries["005"]["legacy"], countries["005"]["partial"]) == ("0", "0")
    for text in ("T1=0 is outside", "GoodAccuracy", "[Country 26]: no MCC", "[Country 262x]: no MCC",
                 "Country 003: Enabled", "RoamingNumber is no phone number", "Port=70000 is outside",
                 "Packing=ucs2", "IMEI=some", "enabled without a valid Number and Port"):
        assert text in err


# ---- the daemon on a private bus ----

OFONO_XML = """<node>
<interface name="org.ofono.Modem"><method name="GetProperties"><arg type="a{sv}" direction="out"/></method></interface>
<interface name="org.ofono.SimManager"><method name="GetProperties"><arg type="a{sv}" direction="out"/></method></interface>
<interface name="org.ofono.NetworkRegistration"><method name="GetProperties"><arg type="a{sv}" direction="out"/></method></interface>
<interface name="org.ofono.MessageManager">
  <method name="GetProperties"><arg type="a{sv}" direction="out"/></method>
  <method name="SendDataMessage">
    <arg type="s" direction="in"/><arg type="ay" direction="in"/><arg type="i" direction="in"/>
    <arg type="i" direction="in"/><arg type="u" direction="in"/><arg type="o" direction="out"/>
  </method>
</interface>
<interface name="org.ofono.VoiceCallManager"><signal name="CallAdded"><arg type="o"/><arg type="a{sv}"/></signal></interface>
</node>"""

IMEI = "356708041746734"
IMSI = "262019876543210"


class FakeOfono:
    """org.ofono with one modem /ril_0 on the private bus, in a thread of its own. `sends`
    collects SendDataMessage calls; `send_errors` makes the next ones fail."""

    def __init__(self, bus, network=None):
        from gi.repository import Gio, GLib
        self.GLib = GLib
        self.sends = []
        self.send_errors = 0
        self.failing = set()  # interfaces whose GetProperties fails
        self.network = network or {"MobileCountryCode": "001", "MobileNetworkCode": "01", "Status": "registered"}
        self.ctx = GLib.MainContext.new()
        self.loop = GLib.MainLoop.new(self.ctx, False)
        ready = threading.Event()

        def props(iface):
            V = GLib.Variant
            if iface == "org.ofono.Modem":
                return {"Serial": V("s", IMEI), "Online": V("b", True)}
            if iface == "org.ofono.SimManager":
                return {"SubscriberIdentity": V("s", IMSI), "MobileCountryCode": V("s", "262"),
                        "MobileNetworkCode": V("s", "01")}
            if iface == "org.ofono.NetworkRegistration":
                return {k: V("s", v) for k, v in self.network.items()}
            return {}

        def on_call(conn, sender, obj, iface, method, params, invocation):
            if method == "GetProperties" and iface in self.failing:
                invocation.return_dbus_error("org.ofono.Error.NotAvailable", "Not available")
                return
            if method == "GetProperties":
                invocation.return_value(GLib.Variant("(a{sv})", (props(iface),)))
                return
            to, data, src, dst, flags = params.unpack()
            self.sends.append({"to": to, "data": bytes(data), "src": src, "dst": dst, "flags": flags,
                               "at": time.monotonic()})
            if self.send_errors:
                self.send_errors -= 1
                invocation.return_dbus_error("org.ofono.Error.Failed", "Operation failed")
                return
            invocation.return_value(GLib.Variant("(o)", ("/ril_0/message_1",)))

        def thread():
            self.ctx.push_thread_default()
            self.conn = bus.connect()
            node = Gio.DBusNodeInfo.new_for_xml(OFONO_XML)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                for info in node.interfaces:
                    self.conn.register_object("/ril_0", info, on_call, None, None)
            self.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                "RequestName", GLib.Variant("(su)", ("org.ofono", 0)), GLib.VariantType("(u)"),
                                Gio.DBusCallFlags.NONE, 5000, None)
            ready.set()
            self.loop.run()
            self.ctx.pop_thread_default()

        self.thread = threading.Thread(target=thread, daemon=True)
        self.thread.start()
        assert ready.wait(10)

    def call_added(self, path="/ril_0/voicecall01", emergency=True, state="dialing", number="112"):
        GLib = self.GLib
        props = {"State": GLib.Variant("s", state), "LineIdentification": GLib.Variant("s", number)}
        if emergency is not None:
            props["Emergency"] = GLib.Variant("b", emergency)
        done = threading.Event()

        def emit():
            self.conn.emit_signal(None, "/ril_0", "org.ofono.VoiceCallManager", "CallAdded",
                                  GLib.Variant("(oa{sv})", (path, props)))
            self.conn.flush_sync(None)
            done.set()
            return False

        self.ctx.invoke_full(0, emit)
        assert done.wait(10)

    def close(self):
        self.ctx.invoke_full(0, lambda: (self.conn.close_sync(None), self.loop.quit(), False)[-1])
        self.thread.join(10)


class FakeBridge:
    """The NMEA socket of gnss-nmea-bridge: counts clients (= receiver on) and sends lines"""

    def __init__(self, path):
        self.path = str(path)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(4)
        self.clients = []
        self.closed = 0
        self.lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.server.accept()
            except OSError:
                return
            with self.lock:
                self.clients.append(c)
            threading.Thread(target=self._watch, args=(c,), daemon=True).start()

    def _watch(self, c):
        try:
            while c.recv(64):
                pass
        except OSError:
            pass
        with self.lock:
            if c in self.clients:
                self.clients.remove(c)
                self.closed += 1

    def connected(self):
        with self.lock:
            return len(self.clients)

    def send(self, *lines):
        with self.lock:
            for c in self.clients:
                c.sendall("".join(line + "\r\n" for line in lines).encode())

    def close(self):
        self.server.close()
        with self.lock:
            for c in self.clients:
                c.close()


COUNTRY = """
[General]
T1={t1}
GoodAccuracy=30
MinBattery=5
[Country 001]
Enabled=true
Number=112
RoamingNumber=+001112
Port=12345
SourcePort=12346
{extra}
"""


@pytest.fixture
def env(harness, tmp_path):
    """Bus, oFono, bridge and a function that starts the daemon with a configuration"""
    # A socket path has at most 107 bytes, tmp_path may be longer
    short = Path(tempfile.mkdtemp(prefix="aml-"))
    bus = PrivateBus(tmp_path)
    state = {"bus": bus, "daemon": None, "ofono": None, "bridge": FakeBridge(short / "nmea.sock")}
    battery = tmp_path / "battery"
    battery.mkdir()
    (battery / "capacity").write_text("80\n")
    (battery / "status").write_text("Discharging\n")
    state["battery"] = battery

    def start(t1=5, extra="", network=None, conf=None):
        state["ofono"] = FakeOfono(bus, network)
        p = tmp_path / "aml.conf"
        p.write_text(conf if conf is not None else COUNTRY.format(t1=t1, extra=extra))
        d = Daemon([harness, "daemon", "--config", p, "--nmea-socket", state["bridge"].path,
                    "--battery", battery], sanitized_env(DBUS_SYSTEM_BUS_ADDRESS=bus.address))
        state["daemon"] = d
        d.wait_for("Waiting for emergency calls")
        return state["ofono"], state["bridge"], d

    yield start, state
    if state["daemon"]:
        code, err, _ = state["daemon"].stop()
        assert code == 0, err
        state["stderr"] = err
    if state["ofono"]:
        state["ofono"].close()
    state["bridge"].close()
    bus.close()
    for f in short.iterdir():
        f.unlink()
    short.rmdir()


def gga(lat="4807.0380", ns="N", lon="01131.0000", ew="E", hdop="0.9", t="123519.00"):
    return nmea(f"GNGGA,{t},{lat},{ns},{lon},{ew},1,08,{hdop},545.4,M,46.9,M,,")


def test_good_fix_goes_out_at_once_and_gnss_stops(env):
    start, state = env
    ofono, bridge, d = start(t1=30)
    ofono.call_added()
    poll(lambda: bridge.connected(), what="receiver on")
    t0 = time.monotonic()
    bridge.send(nmea("GNRMC,123519,A,4807.038,N,01131.000,E,0,0,230394,,"), gga())
    poll(lambda: ofono.sends, what="SendDataMessage")
    assert time.monotonic() - t0 < 5  # long before T1 (30 s)
    s = ofono.sends[0]
    assert (s["to"], s["src"], s["dst"], s["flags"]) == ("112", 12346, 12345, 0)
    text = decode(s["data"])
    f = fields(text)
    assert f["lt"] == "+48.11730" and f["lg"] == "+11.51667"
    assert (f["rd"], f["lc"], f["pm"]) == ("5", "68", "G")
    assert f["si"] == "262010000000000" and f["ei"] == IMEI
    assert (f["mcc"], f["mnc"]) == ("001", "01")
    assert int(f["ml"]) == len(text)
    assert abs(time.mktime(time.strptime(f["top"], "%Y%m%d%H%M%S")) - time.mktime(time.gmtime())) < 30
    poll(lambda: bridge.closed == 1 and not bridge.connected(), what="receiver off")
    d.wait_for("AML message handed to the modem")
    # Neither the position nor the identifiers reach the journal
    assert "48.1" not in "".join(d.seen) and IMEI not in "".join(d.seen) and IMSI not in "".join(d.seen)


def test_no_fix_until_t1_sends_no_location(env):
    start, state = env
    ofono, bridge, d = start(t1=1)
    ofono.call_added()
    poll(lambda: ofono.sends, timeout=10, what="SendDataMessage at T1")
    f = fields(decode(ofono.sends[0]["data"]))
    assert (f["lt"], f["lg"], f["rd"], f["lc"], f["pm"]) == ("+00.00000", "+000.00000", "N", "0", "N")
    d.wait_for("no location after")
    poll(lambda: not bridge.connected(), what="receiver off")


def test_coarse_fix_waits_for_t1_and_the_best_one_wins(env):
    start, state = env
    ofono, bridge, d = start(t1=2)
    ofono.call_added()
    poll(lambda: bridge.connected(), what="receiver on")
    bridge.send(gga(hdop="20", t="1"), gga(lat="4807.1000", hdop="8", t="2"), gga(hdop="15", t="3"))
    time.sleep(0.5)
    assert not ofono.sends  # 40 m is not good enough to go early
    poll(lambda: ofono.sends, timeout=10, what="SendDataMessage at T1")
    f = fields(decode(ofono.sends[0]["data"]))
    assert f["rd"] == "40" and f["lt"] == "+48.11833"


def test_gst_after_gga_describes_the_same_fix(env):
    start, state = env
    ofono, bridge, d = start(t1=2)
    ofono.call_added()
    poll(lambda: bridge.connected(), what="receiver on")
    # HDOP says 45 m, GST of the same epoch 50 m: the fix goes out at T1 with the radius of GST
    bridge.send(gga(hdop="9", t="7"), nmea("GNGST,7,1,1,1,0,30.0,40.0,5"))
    poll(lambda: ofono.sends, timeout=10, what="SendDataMessage at T1")
    assert fields(decode(ofono.sends[0]["data"]))["rd"] == "50"


def test_legacy_packing_and_partial_imei(env):
    start, state = env
    ofono, bridge, d = start(t1=1, extra="Packing=legacy\nIMEI=partial")
    ofono.call_added()
    poll(lambda: ofono.sends, timeout=10, what="SendDataMessage")
    f = fields(decode(ofono.sends[0]["data"], legacy=True))
    assert f["ei"] == "356708040000000"


def test_roaming_uses_the_roaming_number(env):
    start, state = env
    ofono, bridge, d = start(t1=1, network={"MobileCountryCode": "001", "MobileNetworkCode": "02",
                                            "Status": "roaming"})
    ofono.call_added()
    poll(lambda: ofono.sends, timeout=10, what="SendDataMessage")
    assert ofono.sends[0]["to"] == "+001112"
    assert fields(decode(ofono.sends[0]["data"]))["mnc"] == "02"
    d.wait_for("roaming number")


def test_failed_sends_are_retried_three_times(env):
    start, state = env
    ofono, bridge, d = start(t1=1)
    ofono.send_errors = 5
    ofono.call_added()
    poll(lambda: len(ofono.sends) == 3, timeout=30, what="three attempts")
    d.wait_for("AML not sent (attempt 3 of 3): Operation failed")
    d.wait_for("AML given up")
    assert 4 <= ofono.sends[1]["at"] - ofono.sends[0]["at"] <= 8
    time.sleep(0.3)
    assert len(ofono.sends) == 3


def test_retry_succeeds(env):
    start, state = env
    ofono, bridge, d = start(t1=1)
    ofono.send_errors = 1
    ofono.call_added()
    poll(lambda: len(ofono.sends) == 2, timeout=20, what="second attempt")
    d.wait_for("AML message handed to the modem")


@pytest.mark.parametrize("call", [
    {"emergency": False},
    {"emergency": None},
    {"state": "incoming"},
    {"state": "waiting"},
])
def test_no_trigger_without_an_outgoing_emergency_call(env, call):
    start, state = env
    ofono, bridge, d = start(t1=1)
    ofono.call_added(**call)
    time.sleep(2)
    assert not ofono.sends and bridge.connected() == 0 and bridge.closed == 0


def test_country_without_destination_gets_nothing_not_even_gnss(env):
    start, state = env
    ofono, bridge, d = start(conf=(AML / "aml.conf").read_text(),
                             network={"MobileCountryCode": "262", "MobileNetworkCode": "01", "Status": "registered"})
    ofono.call_added()
    d.wait_for("Emergency call in country 262: no AML destination known there, nothing sent")
    time.sleep(0.5)
    assert not ofono.sends and bridge.connected() == 0 and bridge.closed == 0


def test_unknown_network_gets_nothing(env):
    start, state = env
    ofono, bridge, d = start(network={"Status": "searching"})
    ofono.call_added()
    d.wait_for("Emergency call in country unknown")
    assert not ofono.sends


def test_ofono_errors_count_as_unknown(env):
    start, state = env
    ofono, bridge, d = start(t1=1)
    ofono.failing.add("org.ofono.NetworkRegistration")
    ofono.call_added()
    d.wait_for("org.ofono.NetworkRegistration: Not available")
    d.wait_for("Emergency call in country unknown")
    assert not ofono.sends


def test_message_without_modem_and_sim_answers(env):
    start, state = env
    ofono, bridge, d = start(t1=1)
    ofono.failing |= {"org.ofono.Modem", "org.ofono.SimManager"}
    ofono.call_added()
    poll(lambda: ofono.sends, timeout=10, what="SendDataMessage")
    f = fields(decode(ofono.sends[0]["data"]))
    assert (f["si"], f["ei"]) == ("000000000000000", "000000000000000")


def test_number_list_limits_the_trigger(env):
    start, state = env
    ofono, bridge, d = start(t1=1, extra="EmergencyNumbers=112")
    ofono.call_added(number="110")
    d.wait_for("to a number without AML")
    time.sleep(1.5)
    assert not ofono.sends
    ofono.call_added(path="/ril_0/voicecall02", number="112")
    poll(lambda: ofono.sends, timeout=10, what="SendDataMessage for 112")


def test_second_emergency_call_during_one_is_ignored(env):
    start, state = env
    ofono, bridge, d = start(t1=1)
    ofono.call_added()
    ofono.call_added(path="/ril_0/voicecall02")
    d.wait_for("already under way, nothing for /ril_0/voicecall02")
    poll(lambda: ofono.sends, timeout=10, what="SendDataMessage")
    time.sleep(1.5)
    assert len(ofono.sends) == 1


def test_next_emergency_call_after_one_sends_again(env):
    start, state = env
    ofono, bridge, d = start(t1=1)
    ofono.call_added()
    poll(lambda: ofono.sends, timeout=10, what="first")
    d.wait_for("handed to the modem")
    ofono.call_added(path="/ril_0/voicecall02")
    poll(lambda: len(ofono.sends) == 2, timeout=10, what="second")


@pytest.mark.parametrize("status,sent_at_once", [("Discharging", True), ("Charging", False)])
def test_low_battery_skips_gnss_unless_charging(env, status, sent_at_once):
    start, state = env
    (state["battery"] / "capacity").write_text("2\n")
    (state["battery"] / "status").write_text(status + "\n")
    ofono, bridge, d = start(t1=3)
    ofono.call_added()
    if sent_at_once:
        poll(lambda: ofono.sends, timeout=3, what="SendDataMessage at once")
        assert fields(decode(ofono.sends[0]["data"]))["pm"] == "N"
        assert bridge.connected() == 0 and bridge.closed == 0
        d.wait_for("Battery low")
    else:
        poll(lambda: bridge.connected(), what="receiver on while charging")


def test_unreadable_battery_counts_as_enough(env):
    start, state = env
    (state["battery"] / "capacity").write_text("unknown\n")
    ofono, bridge, d = start(t1=3)
    ofono.call_added()
    poll(lambda: bridge.connected(), what="receiver on")


def test_missing_battery_counts_as_enough(env):
    start, state = env
    (state["battery"] / "capacity").unlink()
    ofono, bridge, d = start(t1=3)
    ofono.call_added()
    poll(lambda: bridge.connected(), what="receiver on")


def test_bridge_missing_then_back_reconnects(env):
    start, state = env
    bridge = state["bridge"]
    bridge.server.close()
    os.unlink(bridge.path)
    ofono, _, d = start(t1=8)
    ofono.call_added()
    d.wait_for("GNSS: ")  # connect refused
    state["bridge"] = bridge = FakeBridge(bridge.path)
    poll(lambda: bridge.connected(), timeout=10, what="reconnected")
    bridge.send(gga())
    poll(lambda: ofono.sends, timeout=10, what="SendDataMessage")
    assert fields(decode(ofono.sends[0]["data"]))["pm"] == "G"


def test_bridge_closing_the_stream_reconnects(env):
    start, state = env
    ofono, bridge, d = start(t1=8)
    ofono.call_added()
    poll(lambda: bridge.connected(), what="receiver on")
    with bridge.lock:
        for c in bridge.clients:
            c.shutdown(socket.SHUT_RDWR)
    d.wait_for("the bridge closed the stream")
    poll(lambda: bridge.connected(), timeout=10, what="reconnected")


def test_stop_during_a_call_switches_gnss_off(env):
    start, state = env
    ofono, bridge, d = start(t1=30)
    ofono.call_added()
    poll(lambda: bridge.connected(), what="receiver on")
    code, err, _ = d.stop()
    state["daemon"] = None
    assert code == 0
    assert "cut short: the daemon stops" in err
    poll(lambda: not bridge.connected(), what="receiver off")
    assert not ofono.sends


def test_bad_option_and_missing_bus(harness, tmp_path):
    r = run(harness, "daemon", "--no-such-option", check=False, env=sanitized_env())
    assert r.returncode == 2
    r = run(harness, "daemon", "--config", tmp_path / "none.conf", check=False, env=sanitized_env())
    assert r.returncode == 1 and "System bus" in r.stderr


def test_unit_is_sandboxed_and_reaches_socket_and_bus():
    unit = (AML / "plinius-aml.service").read_text()
    for line in ("SupplementaryGroups=geoclue", "CapabilityBoundingSet=", "PrivateNetwork=yes",
                 "RestrictAddressFamilies=AF_UNIX", "ProtectSystem=strict", "Restart=always"):
        assert line + "\n" in unit
    pkgbuild = (AML / "PKGBUILD").read_text()
    assert "backup=(etc/plinius-aml/aml.conf)" in pkgbuild
    assert "multi-user.target.wants" in pkgbuild
