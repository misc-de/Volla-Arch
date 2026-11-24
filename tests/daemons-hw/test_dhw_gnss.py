# gnss-nmea-bridge: the NMEA it builds from GnssLocation (coordinates, checksum, units) and the
# filter for the HAL's own sentences. libgbinder is replaced by tests/daemons-hw/fakes/gbinder.h;
# clients are socket pairs, so broadcast() itself runs too.

import functools

import pytest

from dhw_harness import PKG, build_harness, kv, run, sandboxed_source

SRC = PKG / "gnss-nmea-bridge" / "gnss-nmea-bridge.c"

HARNESS = r"""
#include <glib.h>
#include <gio/gio.h>
#include <sys/socket.h>
#include <fcntl.h>
#include <stdlib.h>
static gint64 fake_now;
#define g_get_monotonic_time() (fake_now)
#define main real_main
#include "gnss-nmea-bridge.c"
#undef main

static int peer = -1;

/* One connected client: the daemon's end in bridge.clients, ours in peer */
static void add_client(void)
{
	int sv[2];
	if (socketpair(AF_UNIX, SOCK_STREAM, 0, sv) < 0)
		abort();
	GSocket *s = g_socket_new_from_fd(sv[0], NULL);
	GSocketConnection *c = g_socket_connection_factory_create_connection(s);
	g_object_unref(s);
	bridge.clients = g_list_prepend(bridge.clients, c);
	peer = sv[1];
	fcntl(peer, F_SETFL, O_NONBLOCK);
}

/* What the client received, with CR and LF made visible */
static void dump(void)
{
	char buf[8192];
	ssize_t n = read(peer, buf, sizeof buf - 1);
	if (n < 0)
		n = 0;
	buf[n] = 0;
	printf("got=");
	for (char *p = buf; *p; p++) {
		if (*p == '\r')
			fputs("<CR>", stdout);
		else if (*p == '\n')
			fputs("<LF>|", stdout);
		else
			putchar(*p);
	}
	putchar('\n');
}

int main(int argc, char **argv)
{
	const char *cmd = argv[1];
	add_client();
	if (!strcmp(cmd, "coord")) {
		char buf[16], hemi;
		format_coord(g_ascii_strtod(argv[2], NULL), atoi(argv[3]), buf, sizeof buf, &hemi);
		printf("coord=%s %c\n", buf, hemi);
	} else if (!strcmp(cmd, "sentence")) {
		send_sentence(argv[2]);
		dump();
	} else if (!strcmp(cmd, "location")) {
		emit_location(atoi(argv[2]), g_ascii_strtod(argv[3], NULL), g_ascii_strtod(argv[4], NULL),
			      g_ascii_strtod(argv[5], NULL), g_ascii_strtod(argv[6], NULL),
			      g_ascii_strtod(argv[7], NULL), g_ascii_strtod(argv[8], NULL),
			      g_ascii_strtoll(argv[9], NULL, 10));
		dump();
	} else if (!strcmp(cmd, "fix")) {
		printf("fix=%d\n", nmea_has_fix(argv[2]));
	} else if (!strcmp(cmd, "nmea")) {
		/* argv[2]: 16 (UTF-16 string) or 8 (UTF-8 string) */
		FakeGbValue v[] = {{'l', 1}, {argv[2][0] == '8' ? '8' : 's', 0, 0, argv[3]}};
		fake_gb_request_vals = v;
		fake_gb_request_n = 2;
		GBinderReader r;
		gbinder_remote_request_init_reader(NULL, &r);
		fake_now = 5000000;
		handle_nmea(&r);
		printf("last_nmea=%" G_GINT64_FORMAT "\n", bridge.last_nmea_us);
		dump();
	} else if (!strcmp(cmd, "location-cb")) {
		/* A GnssLocation parcel, then the same after HAL NMEA argv[2] µs before */
		FakeGbValue v[] = {
			{'i', 1}, {'i', 92}, {'i', 0x1f},
			{'d', 0, 52.520008}, {'d', 0, 13.404954}, {'d', 0, 34.5}, {'d', 0, 10.0},
			{'d', 0, 270.0}, {'d', 0, 10.0}, {'d', 0, 3}, {'d', 0, 1}, {'d', 0, 1},
			{'l', 1700000000123LL},
		};
		fake_gb_request_vals = v;
		fake_gb_request_n = G_N_ELEMENTS(v);
		GBinderReader r;
		bridge.last_nmea_us = 100 * G_USEC_PER_SEC;
		fake_now = bridge.last_nmea_us + g_ascii_strtoll(argv[2], NULL, 10);
		gbinder_remote_request_init_reader(NULL, &r);
		handle_location(&r);
		dump();
	} else if (!strcmp(cmd, "location-short")) {
		/* A parcel that ends early: nothing may be sent */
		FakeGbValue v[] = {{'i', 1}, {'i', 92}, {'i', 0x1f}, {'d', 0, 52.5}};
		fake_gb_request_vals = v;
		fake_gb_request_n = G_N_ELEMENTS(v);
		GBinderReader r;
		fake_now = 1000 * G_USEC_PER_SEC;
		gbinder_remote_request_init_reader(NULL, &r);
		handle_location(&r);
		dump();
	}
	return 0;
}
"""


@pytest.fixture(scope="module")
def gnss(tmp_path_factory):
    d = tmp_path_factory.mktemp("gnss")
    # The socket path is only used by main(), which the harness does not run
    sandboxed_source(SRC, d, [("/run/gnss-nmea-bridge/nmea.sock", d / "nmea.sock")], allow=("/dev/binder",))
    exe = build_harness(d, "gnss", HARNESS, pkgs=("gio-unix-2.0",))
    return functools.partial(run, exe)


def nmea_checksum(body):
    s = 0
    for ch in body.encode():
        s ^= ch
    return f"{s:02X}"


def framed(body):
    return f"${body}*{nmea_checksum(body)}<CR><LF>|"


@pytest.mark.parametrize("deg,lat,expected", [
    ("52.520008", 1, "5231.2005 N"),      # Berlin: 31.20048 minutes
    ("13.404954", 0, "01324.2972 E"),     # longitude has three degree digits
    ("-33.8688", 1, "3352.1280 S"),       # southern hemisphere
    ("-70.6693", 0, "07040.1580 W"),      # western hemisphere
    ("10.99999999999", 1, "1100.0000 N"),  # rounds up into the next degree, never "60.0000"
    ("0", 1, "0000.0000 N"),
    ("179.99999999", 0, "18000.0000 E"),
])
def test_format_coord(gnss, deg, lat, expected):
    assert kv(gnss("coord", deg, lat).stdout)["coord"] == expected


def test_sentence_checksum_and_framing(gnss):
    body = "GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,"
    # The checksum of this classic example sentence is 47
    assert nmea_checksum(body) == "47"
    assert kv(gnss("sentence", body).stdout)["got"] == framed(body)


def test_location_to_rmc_and_gga(gnss):
    # 2023-11-14 22:13:20.123 UTC; 10 m/s = 19.4 kn; HDOP approximated as 10 m / 5
    out = kv(gnss("location", 0x1f, "52.520008", "13.404954", "34.5", "10", "270", "10",
                  "1700000000123").stdout)["got"]
    rmc = "GPRMC,221320.12,A,5231.2005,N,01324.2972,E,19.4,270.0,141123,,,A"
    gga = "GPGGA,221320.12,5231.2005,N,01324.2972,E,1,04,2.0,34.5,M,,M,,"
    assert out == framed(rmc) + framed(gga)


def test_location_without_optional_fields(gnss):
    # Only HAS_LAT_LONG: speed, bearing and altitude 0, HDOP 1
    out = kv(gnss("location", 0x1, "-33.8688", "-70.6693", "999", "99", "99", "99",
                  "0").stdout)["got"]
    assert out == (framed("GPRMC,000000.00,A,3352.1280,S,07040.1580,W,0.0,0.0,010170,,,A")
                   + framed("GPGGA,000000.00,3352.1280,S,07040.1580,W,1,04,1.0,0.0,M,,M,,"))


def test_location_hdop_floor(gnss):
    # A very good accuracy still gives HDOP 0.5, not 0.0 (geoclue would read 0 as unknown)
    out = kv(gnss("location", 0x11, "1", "1", "0", "0", "0", "1", "0").stdout)["got"]
    assert ",1,04,0.5," in out


def test_location_without_lat_long_sends_nothing(gnss):
    assert kv(gnss("location", 0x1e, "1", "1", "1", "1", "1", "1", "0").stdout)["got"] == ""


@pytest.mark.parametrize("line,fix", [
    ("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47", 1),
    ("$GNGGA,123519,4807.038,N,01131.000,E,2,08,0.9,545.4,M,46.9,M,,*47", 1),
    ("$GPGGA,123519,4807.038,N,01131.000,E,5,08,0.9,545.4,M,46.9,M,,*47", 1),
    ("$GPGGA,123519,,,,,0,00,,,M,,M,,*47", 0),                  # no fix
    ("$GPGGA,123519,4807.038,N,01131.000,E,6,08,0.9,545.4,M,46.9,M,,*47", 0),  # estimated
    ("$GPGGA,123519,4807.038,N,01131.000,E,8,08,0.9,545.4,M,46.9,M,,*47", 0),  # simulated
    ("$GPGGA,123519,4807.038,N,01131.000,E,,08*47", 0),         # empty quality
    ("$GPGGA,123519,4807.038,N,01131.000,E,12,08*47", 0),       # two-digit quality
    ("$GPGGA,123519,4807.038", 0),                               # too few fields
    ("$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A", 1),  # NMEA 2.0
    ("$GPRMC,123519,V,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A", 0),  # void
    ("$GNRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,,,A*6A", 1),
    ("$GNRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,,,D*6A", 1),     # differential
    ("$GNRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,,,E*6A", 0),     # dead reckoning
    ("$GNRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,,,N*6A", 0),
    ("$GNRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,,,S*6A", 0),     # simulator
    ("$GNRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,,,M,V*6A", 0),   # NMEA 4.1, manual
    ("$GNRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,,,A,V*6A", 1),
    ("$GPRMC,1", 0),
    ("$GPGSV,3,1,11,03,03,111,00,04,15,270,00,06,01,010,00,13,06,292,00*74", 1),  # no position
    ("$GP", 1),
])
def test_nmea_has_fix(gnss, line, fix):
    assert kv(gnss("fix", line).stdout)["fix"] == str(fix)


def test_hal_nmea_passes_lines_with_fix(gnss):
    gga = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
    gsv = "$GPGSV,3,1,11,03,03,111,00*74"
    nofix = "$GPGGA,123519,,,,,0,00,,,M,,M,,*47"
    out = kv(gnss("nmea", "16", f"{gga}\r\n{nofix}\r\n{gsv}\r\n").stdout)
    assert out["got"] == f"{gga}<CR><LF>|{gsv}<CR><LF>|"
    assert out["last_nmea"] == "5000000"


def test_hal_nmea_utf8_fallback(gnss):
    gsv = "$GPGSV,3,1,11,03,03,111,00*74"
    assert kv(gnss("nmea", "8", gsv).stdout)["got"] == f"{gsv}<CR><LF>|"


def test_hal_nmea_drops_control_characters_and_overlong_lines(gnss):
    good = "$GPGSV,1,1,00*79"
    evil = "$GPTXT,01,01,02,\x1b[2Jcleared*00"
    long = "$GPTXT," + "A" * 300
    tail = "garbage$GPGSV,1,1,00*79"   # a half line that does not start with $
    out = kv(gnss("nmea", "16", f"{evil}\r\n{long}\n{tail}\n{good}").stdout)
    assert out["got"] == f"{good}<CR><LF>|"


def test_hal_nmea_must_start_with_dollar(gnss):
    out = kv(gnss("nmea", "16", "GPGSV,1,1,00*79\r\n$GPGSV,1,1,00*79").stdout)
    # Not NMEA at all: nothing passes and the HAL does not count as reporting NMEA
    assert out["got"] == ""
    assert out["last_nmea"] == "0"


def test_location_callback_only_while_hal_is_silent(gnss):
    rmc = "GPRMC,221320.12,A,5231.2005,N,01324.2972,E,19.4,270.0,141123,,,A"
    # HAL NMEA 1 s ago: the HAL's own sentences are used, none are generated
    assert kv(gnss("location-cb", 1_000_000).stdout)["got"] == ""
    # 3.5 s without HAL NMEA: generated from the location
    assert kv(gnss("location-cb", 3_500_000).stdout)["got"].startswith(framed(rmc))


def test_truncated_location_parcel_sends_nothing(gnss):
    assert kv(gnss("location-short").stdout)["got"] == ""
