# waydroid-plinius-setup: the binder protocols for the system image, the VINTF entries of the
# bridges that run, display_settings.xml and Android's display size in waydroid.cfg. Every path
# is in tmp; mount, umount, systemctl and waydroid are stubs (the mount stub writes a build.prop
# with the API level of FAKE_SDK into the mount point).

import configparser
import os

import pytest

from conftest import PKG, load_script

SCRIPT = PKG / "waydroid-plinius" / "waydroid-plinius-setup"

MOUNT = r'''
mnt="${@: -1}"
[ -n "$FAKE_MOUNT_FAILS" ] && exit 32
mkdir -p "$mnt/system"
[ -n "$FAKE_SDK" ] && printf 'ro.build.id=TQ3A\nro.build.version.sdk=%s\n' "$FAKE_SDK" > "$mnt/system/build.prop"
exit 0
'''
# is-active: active if the unit is in FAKE_ACTIVE (space-separated)
SYSTEMCTL = r'''
if [ "$1" = is-active ]; then
	for u in $FAKE_ACTIVE; do [ "$u" = "$3" ] && exit 0; done
	exit 3
fi
exit 0
'''


@pytest.fixture
def setup(tmp_path, fake_bin, monkeypatch):
    mod = load_script(SCRIPT)
    lib = tmp_path / "waydroid"
    images = tmp_path / "images"
    images.mkdir()
    (images / "system.img").write_text("image")
    monkeypatch.setattr(mod, "WAYDROID_CFG", str(lib / "waydroid.cfg"))
    monkeypatch.setattr(mod, "OVERLAY", str(lib / "overlay"))
    monkeypatch.setattr(mod, "GBINDER_CONF", str(tmp_path / "gbinder.d" / "60-waydroid-anbox.conf"))
    monkeypatch.setattr(mod, "DATA", str(tmp_path / "share"))
    monkeypatch.setattr(mod, "VENDOR_ETC", str(tmp_path / "vendor-etc"))
    # Device nodes as files in tmp, with udev's modes on the phone
    dev = tmp_path / "dev"
    (dev / "dma_heap").mkdir(parents=True)
    for name, mode in (("ashmem", 0o660), ("dma_heap/system", 0o600), ("card0", 0o660)):
        (dev / name).write_text("")
        os.chmod(dev / name, mode)
    monkeypatch.setattr(mod, "DEVICE_MODES", {str(dev / "ashmem"): 0o666,
                                              str(dev / "dma_heap" / "system"): 0o444,
                                              str(dev / "dma_heap" / "system-uncached"): 0o444})
    mod.dev = dev
    (tmp_path / "vendor-etc").mkdir()
    (tmp_path / "vendor-etc" / "libnfc-nci.conf").write_text("NFC_DEBUG_ENABLED=0\n")
    share = tmp_path / "share"
    (share / "vintf").mkdir(parents=True)
    for name in mod.BRIDGES.values():
        (share / "vintf" / name).write_text(f"<manifest>{name}</manifest>\n")
    (share / "display_settings.xml").write_text("<display-settings/>\n")
    fake_bin.stub("mount", MOUNT)
    fake_bin.stub("umount")
    fake_bin.stub("systemctl", SYSTEMCTL)
    fake_bin.stub("waydroid")
    monkeypatch.setenv("PATH", fake_bin.env(isolated=True)["PATH"])
    monkeypatch.setenv("FAKE_SDK", "33")
    monkeypatch.setenv("FAKE_ACTIVE", " ".join(mod.BRIDGES))
    mod.tmp = tmp_path
    mod.images = images
    return mod


def write_cfg(mod, properties=None, images=None, drm=None):
    lines = ["[waydroid]", f"images_path = {images or mod.images}", "vendor_type = HALIUM_14"]
    if drm:
        lines.append(f"drm_device = {drm}")
    if properties is not None:
        lines.append("[properties]")
        lines += [f"{k} = {v}" for k, v in properties.items()]
    os.makedirs(os.path.dirname(mod.WAYDROID_CFG), exist_ok=True)
    with open(mod.WAYDROID_CFG, "w") as f:
        f.write("\n".join(lines) + "\n")


def manifests(mod):
    d = os.path.join(mod.OVERLAY, "vendor", "etc", "vintf", "manifest")
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


@pytest.mark.parametrize("api,expected", [
    (27, ("aidl", "aidl")), (28, ("aidl2", "aidl2")), (30, ("aidl3", "aidl3")), (31, ("aidl4", "aidl3")),
    (33, ("aidl3", "aidl3")), (34, ("aidl3", "aidl3")), (35, ("aidl3", "aidl5")), (36, ("aidl3", "aidl6")),
])
def test_protocols_as_waydroid_picks_them(setup, api, expected):
    assert setup.protocols(api) == expected


def test_not_initialised_does_nothing(setup, fake_bin, capsys):
    assert setup.main() == 0
    assert "not initialised" in capsys.readouterr().out
    assert fake_bin.calls("mount") == [] and fake_bin.calls("systemctl") == []


def test_first_start_sets_everything_up(setup, fake_bin, capsys):
    write_cfg(setup, {"persist.waydroid.multi_windows": "true"})
    assert setup.main() == 0
    out = capsys.readouterr().out
    # Android 13 (API 33): aidl3 on both, as Waydroid uses itself
    assert open(setup.GBINDER_CONF).read() == (
        "# Written by waydroid-plinius-setup for the system image (Android API 33)\n"
        "[Protocol]\n/dev/anbox-binder = aidl3\n[ServiceManager]\n/dev/anbox-binder = aidl3\n")
    assert oct(os.stat(setup.GBINDER_CONF).st_mode & 0o777) == "0o644"
    # The image mounted read-only and released again; the bridges started anew for the new file
    mount = fake_bin.calls("mount")[0]
    assert mount[:3] == ["-o", "ro,loop", str(setup.images / "system.img")]
    assert fake_bin.calls("umount") == [[mount[3]]]
    assert not os.path.exists(mount[3])
    assert ["try-restart", *setup.BRIDGES] in fake_bin.calls("systemctl")
    # Every bridge runs: all four VINTF entries, and the display settings
    assert manifests(setup) == sorted(setup.BRIDGES.values())
    ds = os.path.join(setup.OVERLAY, "vendor", "etc", "display_settings.xml")
    assert open(ds).read() == "<display-settings/>\n"
    # The display size, kept with the other properties, then Waydroid makes its properties again
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    cfg.read(setup.WAYDROID_CFG)
    assert dict(cfg["properties"]) == {"persist.waydroid.multi_windows": "true",
                                       "persist.waydroid.width": "476", "persist.waydroid.height": "1012"}
    assert cfg["waydroid"]["vendor_type"] == "HALIUM_14"
    assert fake_bin.calls("waydroid") == [["upgrade", "--offline"]]
    assert "Android API 33: aidl3, aidl3" in out and "476 x 1012" in out


def test_second_start_changes_nothing(setup, fake_bin):
    write_cfg(setup)
    setup.main()
    before = os.stat(setup.GBINDER_CONF).st_mtime_ns
    for name in ("systemctl", "waydroid"):
        (fake_bin.path / f"{name}.log").unlink()
    setup.main()
    assert os.stat(setup.GBINDER_CONF).st_mtime_ns == before
    # Neither the bridges started anew nor the properties made again
    assert not any(c[0] == "try-restart" for c in fake_bin.calls("systemctl"))
    assert fake_bin.calls("waydroid") == []


def test_another_android_changes_the_protocols(setup, fake_bin, monkeypatch):
    write_cfg(setup)
    setup.main()
    monkeypatch.setenv("FAKE_SDK", "36")
    setup.main()
    assert "/dev/anbox-binder = aidl6" in open(setup.GBINDER_CONF).read()
    assert [c for c in fake_bin.calls("systemctl") if c[0] == "try-restart"] == [["try-restart", *setup.BRIDGES]] * 2


@pytest.mark.parametrize("env,images", [
    ({"FAKE_MOUNT_FAILS": "1"}, None),     # the image cannot be mounted
    ({"FAKE_SDK": ""}, None),              # no build.prop in it
    ({"FAKE_SDK": "thirteen"}, None),      # no number
    ({}, "missing"),                       # no system.img
], ids=["mount-fails", "no-build-prop", "bad-sdk", "no-image"])
def test_unreadable_image_leaves_the_protocols(setup, fake_bin, monkeypatch, capsys, env, images):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    write_cfg(setup, images=setup.tmp / images if images else None)
    os.makedirs(os.path.dirname(setup.GBINDER_CONF))
    with open(setup.GBINDER_CONF, "w") as f:
        f.write("before\n")
    setup.main()
    assert open(setup.GBINDER_CONF).read() == "before\n"
    assert "No readable system image" in capsys.readouterr().out
    # The rest is set up all the same
    assert len(manifests(setup)) == 5


def test_unreadable_build_prop(setup, tmp_path):
    assert setup.read_prop(str(tmp_path / "none"), "ro.build.version.sdk") is None


def test_only_bridges_that_run_get_an_entry(setup, monkeypatch, capsys):
    write_cfg(setup)
    setup.main()
    monkeypatch.setenv("FAKE_ACTIVE", "sensor-proxy-bridge.service health-hal-bridge.service")
    setup.main()
    assert manifests(setup) == ["plinius-health.xml", "plinius-sensors.xml"]
    out = capsys.readouterr().out
    assert "gnss-nmea-bridge.service does not run: plinius-gnss.xml left out" in out
    # One that never had an entry is not mentioned
    monkeypatch.setenv("FAKE_ACTIVE", "")
    setup.main()
    assert manifests(setup) == []
    assert "gnss-nmea-bridge" not in capsys.readouterr().out


@pytest.mark.parametrize("props", [{"persist.waydroid.width": "500"}, {"persist.waydroid.height": "900"}])
def test_a_size_set_by_hand_stays(setup, fake_bin, props):
    write_cfg(setup, props)
    setup.main()
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    cfg.read(setup.WAYDROID_CFG)
    assert dict(cfg["properties"]) == props
    assert fake_bin.calls("waydroid") == []


def test_cfg_without_properties_section(setup):
    write_cfg(setup)
    setup.main()
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    cfg.read(setup.WAYDROID_CFG)
    assert cfg["properties"]["persist.waydroid.height"] == "1012"


def test_display_size_is_the_space_between_the_bars(setup):
    assert (setup.DISPLAY_WIDTH, setup.DISPLAY_HEIGHT) == (476, 1012)


def test_build_prop_without_the_key(setup, tmp_path):
    prop = tmp_path / "build.prop"
    prop.write_text("# comment\nro.build.id=TQ3A\nro.build.version.sdk\n")
    assert setup.read_prop(str(prop), "ro.build.version.sdk") is None
    assert setup.read_prop(str(prop), "ro.build.id") == "TQ3A"


def test_vendor_nfc_settings_go_to_android(setup):
    write_cfg(setup)
    setup.main()
    conf = os.path.join(setup.OVERLAY, "vendor", "etc", "libnfc-nci.conf")
    assert open(conf).read() == "NFC_DEBUG_ENABLED=0\n"
    assert oct(os.stat(conf).st_mode & 0o777) == "0o644"


def test_no_vendor_nfc_settings(setup):
    os.unlink(os.path.join(setup.VENDOR_ETC, "libnfc-nci.conf"))
    write_cfg(setup)
    setup.main()
    assert not os.path.exists(os.path.join(setup.OVERLAY, "vendor", "etc", "libnfc-nci.conf"))


def mode(path):
    return os.stat(path).st_mode & 0o7777


def test_devices_get_android_s_modes(setup, capsys):
    write_cfg(setup, {"persist.waydroid.width": "476"}, drm=setup.dev / "card0")
    assert setup.main() == 0
    assert mode(setup.dev / "ashmem") == 0o666
    # Bits are only added: root keeps writing the heap
    assert mode(setup.dev / "dma_heap" / "system") == 0o644
    assert mode(setup.dev / "card0") == 0o666
    out = capsys.readouterr().out
    assert f"{setup.dev / 'ashmem'}: 660 -> 666" in out
    # A heap the kernel does not have is left out; a second start changes nothing
    assert not (setup.dev / "dma_heap" / "system-uncached").exists()
    assert setup.main() == 0
    assert "->" not in capsys.readouterr().out


def test_devices_without_drm_device_in_the_cfg(setup):
    write_cfg(setup, {"persist.waydroid.width": "476"})
    setup.main()
    assert mode(setup.dev / "card0") == 0o660 and mode(setup.dev / "ashmem") == 0o666


def test_device_that_cannot_be_changed(setup, monkeypatch, capsys):
    def refuse(path, mode):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(setup.os, "chmod", refuse)
    assert setup.add_mode(str(setup.dev / "ashmem"), 0o666) is False
    assert "Operation not permitted" in capsys.readouterr().err
