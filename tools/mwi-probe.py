#!/usr/bin/env python3
"""Probe MediaTek's Wi-Fi calling (Mwi) channel of IMtkRadioExIms through libgbinder.

Registers IMwiRadioResponse/IMwiRadioIndication with setResponseFunctionsMwi, sends the
requests given on the command line in order and prints every response (with its error
code) and every indication the RIL delivers.

Usage: mwi-probe.py SECONDS REQUEST[:ARG,ARG...] ...
  e.g. mwi-probe.py 60 enable assoc:Home,aa:bb:..,cc:dd:.. ip:192.0.2.2,24,192.0.2.1,192.0.2.1
"""
import ctypes as C
import struct
import sys
import time

SLOT = 'slot1'
IMS = b'vendor.mediatek.hardware.mtkradioex.ims.IMtkRadioExIms'
RESP = b'vendor.mediatek.hardware.mtkradioex.mwi.IMwiRadioResponse'
IND = b'vendor.mediatek.hardware.mtkradioex.mwi.IMwiRadioIndication'
CLIENT_ID = 1           # AIDL clientId for IMS, as Volla OS sends it
STABILITY_VENDOR = 0x03

# transaction codes, from docs/mtkradioex-aidl-codes.txt (IMtkRadioExIms)
REQ = {'getcfg': 46, 'screen': 47, 'emergency': 48, 'loc': 49, 'loccountry': 49, 'natt': 50,
       'setcfg': 51,
       'assoc': 52, 'enable': 53, 'disable': 53, 'ip': 54, 'ping': 55, 'signal': 56,
       'feature': 39, 'profile': 43}
SET_RESPONSE_FUNCTIONS_MWI = 59
RESP_NAMES = {1: 'getWfcConfigResponse', 2: 'notifyEPDGScreenStateResponse',
              3: 'setEmergencyAddressIdResponse', 4: 'setLocationInfoResponse',
              5: 'setNattKeepAliveStatusResponse', 6: 'setWfcConfigResponse',
              7: 'setWifiAssociatedResponse', 8: 'setWifiEnabledResponse',
              9: 'setWifiIpAddressResponse', 10: 'setWifiPingResultResponse',
              11: 'setWifiSignalLevelResponse'}
IND_NAMES = {1: 'onLocationRequest', 2: 'onNattKeepAliveChanged', 3: 'onPdnHandover',
             4: 'onWfcPdnError', 5: 'onWfcPdnStateChanged', 6: 'onWifiLock',
             7: 'onWifiMonitoringThreshouldChanged', 8: 'onWifiPdnActivate',
             9: 'onWifiPdnOOS', 10: 'onWifiPingRequest', 11: 'onWifiRoveout'}

gb = C.CDLL('libgbinder.so.1')
gl = C.CDLL('libglib-2.0.so.0')
P = C.c_void_p
for name, res, args in [
        ('gbinder_servicemanager_new', P, [C.c_char_p]),
        ('gbinder_servicemanager_get_service_sync', P, [P, C.c_char_p, C.POINTER(C.c_int)]),
        ('gbinder_client_new', P, [P, C.c_char_p]),
        ('gbinder_client_new_request2', P, [P, C.c_uint32]),
        ('gbinder_client_transact_sync_reply', P, [P, C.c_uint32, P, C.POINTER(C.c_int)]),
        ('gbinder_client_transact_sync_oneway', C.c_int, [P, C.c_uint32, P]),
        ('gbinder_local_request_init_writer', None, [P, P]),
        ('gbinder_writer_append_int32', None, [P, C.c_int32]),
        ('gbinder_writer_append_string16', None, [P, C.c_char_p]),
        ('gbinder_writer_append_local_object', None, [P, P]),
        ('gbinder_local_object_set_stability', None, [P, C.c_int]),
        ('gbinder_remote_request_init_reader', None, [P, P]),
        ('gbinder_reader_get_data', P, [P, C.POINTER(C.c_size_t)]),
        ('gbinder_reader_bytes_read', C.c_size_t, [P])]:
    f = getattr(gb, name)
    f.restype = res
    f.argtypes = args
gl.g_main_context_iteration.argtypes = [P, C.c_int]

HANDLER = C.CFUNCTYPE(P, P, P, C.c_uint, C.c_uint, C.POINTER(C.c_int), P)


def words(raw):
    return list(struct.unpack('<%di' % (len(raw) // 4), raw[:len(raw) // 4 * 4]))


def strings16(raw):
    """Best-effort decode of the AIDL string16 values in a parcel."""
    out, o = [], 0
    while o + 4 <= len(raw):
        n = struct.unpack_from('<i', raw, o)[0]
        if 0 <= n < 200 and o + 4 + 2 * n + 2 <= len(raw) and \
                raw[o + 4 + 2 * n:o + 6 + 2 * n] == b'\0\0':
            s = raw[o + 4:o + 4 + 2 * n].decode('utf-16-le', 'replace')
            if n == 0 or s.isprintable():
                out.append(s)
                o += (4 + 2 * n + 2 + 3) // 4 * 4
                continue
        o += 4
    return out


def payload(req):
    reader = C.create_string_buffer(64)
    gb.gbinder_remote_request_init_reader(req, reader)
    size = C.c_size_t()
    data = gb.gbinder_reader_get_data(reader, C.byref(size))
    raw = C.string_at(data, size.value)
    # AIDL header: strict mode, work source, 'TSYS' (vendor stability), descriptor string16
    n = struct.unpack_from('<i', raw, 12)[0]
    return raw[16 + (2 * n + 2 + 3) // 4 * 4:]


def on_response(obj, req, code, flags, status, user):
    raw = payload(req)
    w = words(raw)
    # RadioResponseInfo parcelable: non-null, size, type, serial, error
    info = 'serial=%d error=%d' % (w[3], w[4]) if len(w) >= 5 else 'raw'
    print('%s RESP %s %s rest=%s' % (time.strftime('%H:%M:%S'), RESP_NAMES.get(code, code),
                                     info, w[5:12]), flush=True)
    status[0] = 0
    return None


def on_indication(obj, req, code, flags, status, user):
    raw = payload(req)
    print('%s IND  %s ints=%s strings=%s' % (time.strftime('%H:%M:%S'), IND_NAMES.get(code, code),
                                             words(raw)[:24], strings16(raw)), flush=True)
    status[0] = 0
    return None


resp_cb = HANDLER(on_response)
ind_cb = HANDLER(on_indication)


def main():
    seconds = float(sys.argv[1])
    st = C.c_int()
    sm = gb.gbinder_servicemanager_new(b'/dev/binder')
    remote = gb.gbinder_servicemanager_get_service_sync(sm, IMS + b'/' + SLOT.encode(), C.byref(st))
    if not remote:
        sys.exit('IMtkRadioExIms/%s not found (%d)' % (SLOT, st.value))
    client = gb.gbinder_client_new(remote, IMS)
    new_local = gb.gbinder_servicemanager_new_local_object
    new_local.restype = P
    new_local.argtypes = [P, C.c_char_p, HANDLER, P]
    resp = new_local(sm, RESP, resp_cb, None)
    ind = new_local(sm, IND, ind_cb, None)
    gb.gbinder_local_object_set_stability(resp, STABILITY_VENDOR)
    gb.gbinder_local_object_set_stability(ind, STABILITY_VENDOR)

    writer = C.create_string_buffer(128)

    def request(code):
        r = gb.gbinder_client_new_request2(client, code)
        gb.gbinder_local_request_init_writer(r, writer)
        return r

    r = request(SET_RESPONSE_FUNCTIONS_MWI)
    gb.gbinder_writer_append_local_object(writer, resp)
    gb.gbinder_writer_append_local_object(writer, ind)
    gb.gbinder_client_transact_sync_reply(client, SET_RESPONSE_FUNCTIONS_MWI, r, C.byref(st))
    print('setResponseFunctionsMwi status=%d' % st.value, flush=True)

    i32 = lambda v: gb.gbinder_writer_append_int32(writer, int(v))
    s16 = lambda v: gb.gbinder_writer_append_string16(writer, v.encode())

    def svec(items):
        i32(len(items))
        for x in items:
            s16(x)

    for f, args in [('gbinder_writer_start_parcelable', [P, P]),
                    ('gbinder_writer_finish_parcelable', [P]),
                    ('gbinder_writer_append_bool', [P, C.c_int])]:
        getattr(gb, f).restype = None
        getattr(gb, f).argtypes = args
    data_remote = gb.gbinder_servicemanager_get_service_sync(
        sm, b'android.hardware.radio.data.IRadioData/' + SLOT.encode(), C.byref(st))
    data_client = gb.gbinder_client_new(data_remote, b'android.hardware.radio.data.IRadioData')

    def data_setup(serial, access, reason, apn):
        """IRadioData.setupDataCall, laid out like ofono-binder-plugin's AIDL writer."""
        r = gb.gbinder_client_new_request2(data_client, 13)
        gb.gbinder_local_request_init_writer(r, writer)
        dp = C.create_string_buffer(128)
        td = C.create_string_buffer(128)
        i32(serial)
        i32(access)                       # AccessNetwork: 3 EUTRAN, 5 IWLAN
        gb.gbinder_writer_start_parcelable(writer, dp)
        w = lambda v: gb.gbinder_writer_append_int32(dp, int(v))
        s = lambda v: gb.gbinder_writer_append_string16(dp, v.encode() if v is not None else None)
        b = lambda v: gb.gbinder_writer_append_bool(dp, int(v))
        # Like Volla OS binds the modem's own IMS PDN: profile IMS, IPV6 only, DNN "ims"
        w(2); s(apn); w(1); w(1); w(0)    # profileId IMS, apn, IPV6 twice, no auth
        s(''); s(''); w(0); w(0); w(0); w(0)
        b(1); w(64); w(0xFFFFF); w(0); w(0)  # enabled, APN type IMS, all bearers, MTUs
        b(0); b(0); b(0)                  # preferred, persistent, alwaysOn
        gb.gbinder_writer_start_parcelable(dp, td)
        gb.gbinder_writer_append_string16(td, apn.encode())  # dnn
        gb.gbinder_writer_append_int32(td, 0)     # osAppId: null parcelable
        gb.gbinder_writer_finish_parcelable(td)
        w(0)                              # infrastructureBitmap
        gb.gbinder_writer_finish_parcelable(dp)
        gb.gbinder_writer_append_bool(writer, 1)   # roamingAllowed
        i32(reason)                       # 1 NORMAL, 3 HANDOVER
        i32(0); i32(0); i32(0)            # addresses, dnses, pduSessionId
        gb.gbinder_writer_append_int32(writer, 0)  # sliceInfo: null parcelable
        gb.gbinder_writer_append_bool(writer, 1)          # matchAllRuleAllowed
        gb.gbinder_client_transact_sync_oneway(data_client, 13, r)

    def profile(dp, td, pid, apn, proto, types, bearer, dnn):
        """One DataProfileInfo, in the field order of ofono-binder-plugin's setDataProfile."""
        w = lambda v: gb.gbinder_writer_append_int32(dp, int(v))
        s = lambda v: gb.gbinder_writer_append_string16(dp, v.encode() if v is not None else None)
        b = lambda v: gb.gbinder_writer_append_bool(dp, int(v))
        gb.gbinder_writer_start_parcelable(writer, dp)
        w(pid); s(apn); w(proto); w(proto); w(0)   # id, apn, protocol, roaming protocol, auth
        s(''); s(''); w(0); w(0); w(0); w(0)       # user, password, type, maxConns*, waitTime
        b(1); w(types); w(bearer); w(0); w(0)      # enabled, APN types, bearers, MTUs
        b(0); b(0); b(0)                           # preferred, persistent, alwaysOn
        gb.gbinder_writer_start_parcelable(dp, td)
        gb.gbinder_writer_append_string16(td, dnn.encode() if dnn else None)
        gb.gbinder_writer_append_int32(td, 0)      # osAppId: null parcelable
        gb.gbinder_writer_finish_parcelable(td)
        w(0)                                       # infrastructureBitmap
        gb.gbinder_writer_finish_parcelable(dp)

    def data_profiles(serial, internet_apn):
        """IRadioData.setDataProfile like Volla OS: internet plus an IPv6 IMS profile that
        allows every radio technology including IWLAN (bearer bits 0..19)."""
        r = gb.gbinder_client_new_request2(data_client, 9)
        gb.gbinder_local_request_init_writer(r, writer)
        dp = C.create_string_buffer(128)
        td = C.create_string_buffer(128)
        i32(serial)
        i32(2)                                     # number of profiles
        profile(dp, td, 0, internet_apn, 2, 1 | 16, 0, internet_apn)  # DEFAULT|HIPRI, IPV4V6
        profile(dp, td, 2, 'ims', 1, 64, 0xFFFFF, 'ims')              # IMS, IPV6, all + IWLAN
        gb.gbinder_client_transact_sync_oneway(data_client, 9, r)

    serial = 930000
    for spec in sys.argv[2:]:
        name, _, a = spec.partition(':')
        a = a.split(',') if a else []
        serial += 1
        if name == 'datasetup':           # access network, reason[, apn]
            data_setup(serial, int(a[0]), int(a[1]), a[2] if len(a) > 2 else 'ims')
            print('%s sent datasetup serial=%d %s' % (time.strftime('%H:%M:%S'), serial, a), flush=True)
            continue
        if name == 'dataprofile':         # internet apn
            data_profiles(serial, a[0])
            print('%s sent dataprofile serial=%d %s' % (time.strftime('%H:%M:%S'), serial, a), flush=True)
            continue
        r = request(REQ[name])
        i32(serial)
        if name in ('enable', 'disable'):
            s16('wlan0'); i32(name == 'enable'); i32(0)
        elif name == 'assoc':      # ssid, bssid, ue mac[, conn state]
            svec(['wlan0', '1', a[0], a[1], '1500', a[2], a[3] if len(a) > 3 else '2'])
        elif name == 'ip':         # ipv4, prefix, gateway, dns[;dns...][, ipv6, prefix6, gateway6]
            # Volla OS (WifiPdnHandler) quotes every DNS server and joins them with commas
            dns = [x for x in a[3].split(';') if x] if len(a) > 3 else []
            v6 = a[4:7] if len(a) >= 7 else ['', '', '']
            svec(['wlan0', a[0], v6[0], a[1], v6[1], a[2], v6[2], str(len(dns)),
                  ','.join('"%s"' % x for x in dns) if dns else '""'])
        elif name == 'signal':     # rssi[, snr]
            i32(a[0]); i32(a[1] if len(a) > 1 else 0)
        elif name == 'loc':        # accountId[, country]: lat/lon 0.0 – a point off Africa!
            svec([a[0] if a else '0', '0', '0', '0', '0', 'Network', '', '', '',
                  a[1] if len(a) > 1 else 'DE', '', '0', '0', '0', '0', '0'])
        elif name == 'loccountry':  # accountId, country – no coordinates at all
            svec([a[0], '0', '', '', '', '', '', '', '', a[1], '', '', '', '', '', ''])
        elif name in ('screen', 'getcfg', 'profile'):
            i32(a[0])
        elif name == 'setcfg':     # setting, ifName, value
            i32(a[0]); s16(a[1]); s16(a[2])
        elif name == 'feature':    # featureId, network, value, isLast
            for x in a:
                i32(x)
        elif name == 'ping':       # rat, latency, packet loss
            for x in a:
                i32(x)
        i32(CLIENT_ID)
        gb.gbinder_client_transact_sync_oneway(client, REQ[name], r)
        print('%s sent %s serial=%d %s' % (time.strftime('%H:%M:%S'), name, serial, a), flush=True)
        end = time.time() + 1
        while time.time() < end:
            gl.g_main_context_iteration(None, 0)
            time.sleep(0.02)

    end = time.time() + seconds
    while time.time() < end:
        gl.g_main_context_iteration(None, 0)
        time.sleep(0.02)


main()
