# waydroid-plinius-setup: the binder protocols for the system image, the VINTF entries of the
# bridges that run, display_settings.xml, Android's display size in waydroid.cfg and graphics on
# the GPU through the vendor's HALs. Every path is in tmp; mount, umount, systemctl and waydroid
# are stubs (the mount stub writes a build.prop with the API level of FAKE_SDK into the mount
# point and the libraries of FAKE_SYSTEM_LIBS and FAKE_VENDOR_LIBS into the images).

import configparser
import os
import shutil
import struct

import pytest

from conftest import PKG, load_script

SCRIPT = PKG / "waydroid-plinius" / "waydroid-plinius-setup"

MOUNT = r'''
mnt="${@: -1}"
image="${@: -2:1}"
[ -n "$FAKE_MOUNT_FAILS" ] && exit 32
case "$image" in
*vendor.img)
	mkdir -p "$mnt/lib64/hw"
	for l in $FAKE_VENDOR_LIBS; do : > "$mnt/lib64/$l"; done ;;
*)
	mkdir -p "$mnt/system/lib64"
	for l in $FAKE_SYSTEM_LIBS; do : > "$mnt/system/lib64/$l"; done
	[ -n "$FAKE_SDK" ] && printf 'ro.build.id=TQ3A\nro.build.version.sdk=%s\n' "$FAKE_SDK" > "$mnt/system/build.prop" ;;
esac
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
    # The phone's vendor partition (no graphics HALs unless a test adds them) and Halium's VNDK
    monkeypatch.setattr(mod, "VENDOR", str(tmp_path / "vendor"))
    monkeypatch.setattr(mod, "VNDK_GLOB", str(tmp_path / "proc" / "*" / "apex" / "vndk.v%s" / "lib64"))
    monkeypatch.setattr(mod, "GRAPHICS_STATE", str(tmp_path / "waydroid" / "plinius-graphics.list"))
    monkeypatch.setattr(mod, "HALIUM_SYSTEM", str(tmp_path / "halium-system"))
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


# ---- graphics on the GPU ----

def elf(path, needed=()):
    """A minimal 64-bit ELF file whose dynamic section names the libraries in needed."""
    dynstr = b"\0"
    offsets = []
    for name in needed:
        offsets.append(len(dynstr))
        dynstr += name.encode() + b"\0"
    dynamic = b"".join(struct.pack("<qQ", 1, o) for o in offsets) + struct.pack("<qQ", 0, 0)
    shstr = b"\0.dynstr\0.dynamic\0"
    dynstr_off = 64
    dynamic_off = dynstr_off + len(dynstr)
    shstr_off = dynamic_off + len(dynamic)
    shoff = shstr_off + len(shstr)
    header = b"\x7fELF" + bytes([2, 1, 1]) + bytes(9)
    header += struct.pack("<HHIQQQIHHHHHH", 3, 183, 1, 0, 0, shoff, 0, 64, 0, 0, 64, 4, 3)
    sections = [struct.pack("<IIQQQQIIQQ", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
                struct.pack("<IIQQQQIIQQ", 1, 3, 0, 0, dynstr_off, len(dynstr), 0, 0, 1, 0),
                struct.pack("<IIQQQQIIQQ", 9, 6, 0, 0, dynamic_off, len(dynamic), 1, 0, 8, 16),
                struct.pack("<IIQQQQIIQQ", 0, 3, 0, 0, shstr_off, len(shstr), 0, 0, 1, 0)]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(header + dynstr + dynamic + shstr + b"".join(sections))


def vendor_gpu(setup, egl32=False):
    """A vendor with an AIDL allocator, a stable-C mapper and a Mali driver behind MediaTek's EGL
    wrapper, as on the Plinius; Halium's VNDK APEX with the newer AIDL libraries."""
    v = setup.tmp / "vendor"
    (v / "bin" / "hw").mkdir(parents=True)
    (v / "bin" / "hw" / "android.hardware.graphics.allocator-V2-service-mediatek").write_text("")
    (v / "etc").mkdir()
    (v / "build.prop").write_text("ro.hardware.egl=meow\nro.vndk.version=34\n#ro.hardware.egl?=mali\n"
                                  "ro.vendor.arm.egl.configs.nv12.hal_format=0x200\nro.board.platform=mt6878\n")
    (v / "etc" / "extra.prop").write_text("ro.vendor.arm.egl.configs.nv12.recordable=true\n")
    (v / "etc" / "mali_platform.config").write_text("x")
    elf(v / "lib64" / "hw" / "mapper.mediatek.so", ["libged.so", "android.hardware.graphics.common-V4-ndk.so"])
    elf(v / "lib64" / "egl" / "libGLES_meow.so", ["libc.so"])
    elf(v / "lib64" / "egl" / "mt6878" / "libGLES_mali.so",
        ["libged.so", "android.hardware.graphics.allocator-V2-ndk.so", "libnowhere.so", "libc.so"])
    elf(v / "lib64" / "libged.so", ["libc.so", "liblog.so"])
    if egl32:
        (v / "lib" / "egl").mkdir(parents=True)
        (v / "lib" / "egl" / "libGLES_meow.so").write_text("")
    vndk = setup.tmp / "proc" / "251" / "apex" / "vndk.v34" / "lib64"
    elf(vndk / "android.hardware.graphics.allocator-V2-ndk.so",
        ["libbinder_ndk.so", "android.hardware.graphics.common-V4-ndk.so"])
    elf(vndk / "android.hardware.graphics.common-V4-ndk.so", ["libbinder_ndk.so"])
    return v


def read_cfg(setup):
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    cfg.read(setup.WAYDROID_CFG)
    return cfg


def overlay_libs(setup):
    lib64 = os.path.join(setup.OVERLAY, "vendor", "lib64")
    return sorted(f for f in os.listdir(lib64) if f.endswith(".so")) if os.path.isdir(lib64) else []


def test_graphics_on_the_gpu(setup, fake_bin, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_SYSTEM_LIBS", "libc.so liblog.so libbinder_ndk.so")
    vendor_gpu(setup)
    write_cfg(setup, {"persist.waydroid.width": "476", "ro.hardware.gralloc": "gbm", "ro.hardware.egl": "mesa",
                      "gralloc.gbm.device": "/dev/dri/card0"}, drm="/dev/dri/card0")
    assert setup.main() == 0
    cfg = read_cfg(setup)
    assert cfg["waydroid"]["bind_host_egl"] == "True" and "drm_device" not in cfg["waydroid"]
    props = cfg["properties"]
    assert props["ro.hardware.gralloc"] == "android" and props["ro.hardware.egl"] == "meow"
    assert "gralloc.gbm.device" not in props
    # Mali's EGL configs from the vendor's properties, from every file
    assert props["ro.vendor.arm.egl.configs.nv12.hal_format"] == "0x200"
    assert props["ro.vendor.arm.egl.configs.nv12.recordable"] == "true"
    # No 32-bit driver: the zygotes preload no graphics
    assert props["ro.zygote.disable_gl_preload"] == "true"
    assert fake_bin.calls("waydroid") == [["upgrade", "--offline"]]
    # The AIDL libraries of the newer Android, copied from Halium's VNDK, with what they need
    assert overlay_libs(setup) == ["android.hardware.graphics.allocator-V2-ndk.so",
                                   "android.hardware.graphics.common-V4-ndk.so"]
    lib64 = os.path.join(setup.OVERLAY, "vendor", "lib64")
    assert oct(os.stat(os.path.join(lib64, "android.hardware.graphics.common-V4-ndk.so")).st_mode & 0o777) == "0o644"
    # The mapper and the platform file where the vendor's libraries look for them
    assert os.readlink(os.path.join(lib64, "hw", "mapper.mediatek.so")) == "/vendor_extra/lib64/hw/mapper.mediatek.so"
    assert os.readlink(os.path.join(setup.OVERLAY, "vendor", "etc", "mali_platform.config")) == \
        "/vendor_extra/etc/mali_platform.config"
    assert open(os.path.join(setup.OVERLAY, "system", "etc", "hosthals.xml")).read() == setup.HOSTHALS_MAPPER
    out = capsys.readouterr().out
    assert "Graphics on the GPU: meow EGL" in out
    assert "found nowhere: libnowhere.so" in out
    # card0 is not opened for everybody any more
    assert mode(setup.dev / "card0") == 0o660


def test_graphics_a_second_time_changes_nothing(setup, fake_bin, monkeypatch):
    monkeypatch.setenv("FAKE_SYSTEM_LIBS", "libc.so liblog.so libbinder_ndk.so")
    vendor_gpu(setup)
    write_cfg(setup, {"persist.waydroid.width": "476"})
    setup.main()
    (fake_bin.path / "waydroid.log").unlink()
    setup.main()
    assert fake_bin.calls("waydroid") == []
    assert len(overlay_libs(setup)) == 2


def test_libraries_in_the_images_are_not_copied(setup, monkeypatch):
    monkeypatch.setenv("FAKE_SYSTEM_LIBS", "libc.so liblog.so libbinder_ndk.so")
    monkeypatch.setenv("FAKE_VENDOR_LIBS", "android.hardware.graphics.allocator-V2-ndk.so "
                                           "android.hardware.graphics.common-V4-ndk.so")
    (setup.images / "vendor.img").write_text("image")
    vendor_gpu(setup)
    write_cfg(setup, {"persist.waydroid.width": "476"})
    setup.main()
    assert overlay_libs(setup) == []


def test_32_bit_driver_keeps_preloading(setup):
    vendor_gpu(setup, egl32=True)
    write_cfg(setup, {"persist.waydroid.width": "476", "ro.zygote.disable_gl_preload": "true"})
    setup.main()
    assert "ro.zygote.disable_gl_preload" not in read_cfg(setup)["properties"]


def test_without_halium_s_vndk(setup, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_SYSTEM_LIBS", "libc.so liblog.so libbinder_ndk.so")
    vendor_gpu(setup)
    shutil.rmtree(setup.tmp / "proc")
    write_cfg(setup, {"persist.waydroid.width": "476"})
    setup.main()
    assert overlay_libs(setup) == []
    assert "android.hardware.graphics.allocator-V2-ndk.so" in capsys.readouterr().out


def test_vendor_without_graphics_hals_takes_the_overlay_back(setup, monkeypatch):
    monkeypatch.setenv("FAKE_SYSTEM_LIBS", "libc.so liblog.so libbinder_ndk.so")
    v = vendor_gpu(setup)
    write_cfg(setup, {"persist.waydroid.width": "476"})
    setup.main()
    before = read_cfg(setup)
    os.unlink(v / "bin" / "hw" / "android.hardware.graphics.allocator-V2-service-mediatek")
    setup.main()
    lib64 = os.path.join(setup.OVERLAY, "vendor", "lib64")
    assert overlay_libs(setup) == []
    assert not os.path.lexists(os.path.join(lib64, "hw", "mapper.mediatek.so"))
    assert not os.path.lexists(os.path.join(setup.OVERLAY, "vendor", "etc", "mali_platform.config"))
    assert open(os.path.join(setup.OVERLAY, "system", "etc", "hosthals.xml")).read() == setup.HOSTHALS_NONE
    # waydroid.cfg stays as it is
    after = read_cfg(setup)
    assert {s: dict(after[s]) for s in after.sections()} == {s: dict(before[s]) for s in before.sections()}


def test_without_graphics_hals_from_the_start(setup):
    write_cfg(setup, {"persist.waydroid.width": "476"})
    setup.main()
    assert open(os.path.join(setup.OVERLAY, "system", "etc", "hosthals.xml")).read() == setup.HOSTHALS_NONE
    assert open(setup.GRAPHICS_STATE).read() == ""


def test_a_link_that_was_there_already(setup):
    vendor_gpu(setup)
    link = os.path.join(setup.OVERLAY, "vendor", "lib64", "hw", "mapper.mediatek.so")
    os.makedirs(os.path.dirname(link))
    os.symlink("/somewhere/else", link)
    write_cfg(setup, {"persist.waydroid.width": "476"})
    setup.main()
    assert os.readlink(link) == "/vendor_extra/lib64/hw/mapper.mediatek.so"


def test_elf_needed_of_files_that_are_no_elf(setup, tmp_path):
    (tmp_path / "text").write_text("not an elf")
    assert setup.elf_needed(str(tmp_path / "text")) == []
    assert setup.elf_needed(str(tmp_path / "missing")) == []
    elf(tmp_path / "plain.so")
    assert setup.elf_needed(str(tmp_path / "plain.so")) == []


def test_unreadable_vendor_props(setup):
    os.makedirs(setup.tmp / "vendor" / "build.prop")
    assert setup.vendor_props() == {}


# ---- the vendor's cameras ----

VENDOR_MANIFEST = """<manifest version="2.0" type="device">
    <hal format="aidl">
        <name>android.hardware.camera.provider</name>
        <version>2</version>
        <interface>
            <name>ICameraProvider</name>
            <instance>internal/0</instance>
        </interface>
    </hal>
    <hal format="aidl">
        <name>android.hardware.nfc</name>
        <fqname>INfc/default</fqname>
    </hal>
    <hal format="hidl">
        <name>android.hardware.camera.provider</name>
        <fqname>@2.4::ICameraProvider/legacy/0</fqname>
    </hal>
</manifest>
"""


def vendor_cameras(setup, halium_sdk="34"):
    vintf = setup.tmp / "vendor" / "etc" / "vintf"
    (vintf / "manifest").mkdir(parents=True, exist_ok=True)
    (vintf / "manifest.xml").write_text(VENDOR_MANIFEST)
    (vintf / "manifest" / "broken.xml").write_text("<manifest")
    (vintf / "manifest" / "extra.xml").write_text(
        '<manifest version="2.0" type="device"><hal format="aidl">'
        "<name>android.hardware.camera.provider</name><fqname>ICameraProvider/external/1</fqname>"
        "</hal></manifest>")
    halium = setup.tmp / "halium-system"
    (halium / "lib64").mkdir(parents=True, exist_ok=True)
    (halium / "lib64" / "libcamera_metadata.so").write_text("android 14")
    if halium_sdk is not None:
        (halium / "build.prop").write_text(f"ro.build.version.sdk={halium_sdk}\n")


def camera_files(setup):
    return (os.path.join(setup.OVERLAY, "vendor", "etc", "vintf", "manifest", "plinius-camera.xml"),
            os.path.join(setup.OVERLAY, "system", "lib64", "libcamera_metadata.so"))


def test_cameras_of_the_vendor(setup):
    vendor_cameras(setup)
    write_cfg(setup, {"persist.waydroid.width": "476"})
    setup.main()
    manifest, metadata = camera_files(setup)
    # Each AIDL provider instance, declared at the version Android 13 knows
    assert open(manifest).read() == (
        '<manifest version="1.0" type="device">\n    <hal format="aidl">\n'
        "        <name>android.hardware.camera.provider</name>\n        <version>1</version>\n"
        "        <fqname>ICameraProvider/internal/0</fqname>\n"
        "        <fqname>ICameraProvider/external/1</fqname>\n    </hal>\n</manifest>\n")
    # Halium's Android (14) is newer than the image's (13): its camera metadata library
    assert open(metadata).read() == "android 14"


@pytest.mark.parametrize("sdk", ["33", None, "x"], ids=["same-android", "no-build-prop", "bad-sdk"])
def test_camera_metadata_only_from_a_newer_android(setup, sdk):
    vendor_cameras(setup, halium_sdk=sdk)
    _manifest, metadata = camera_files(setup)
    os.makedirs(os.path.dirname(metadata))
    with open(metadata, "w") as f:
        f.write("old")
    write_cfg(setup, {"persist.waydroid.width": "476"})
    setup.main()
    assert not os.path.exists(metadata)


def test_vendor_without_cameras_takes_them_back(setup):
    vendor_cameras(setup)
    write_cfg(setup, {"persist.waydroid.width": "476"})
    setup.main()
    shutil.rmtree(setup.tmp / "vendor" / "etc" / "vintf")
    setup.main()
    manifest, metadata = camera_files(setup)
    assert not os.path.exists(manifest) and not os.path.exists(metadata)


def test_unreadable_image_keeps_halium_s_camera_library_out(setup, monkeypatch):
    monkeypatch.setenv("FAKE_MOUNT_FAILS", "1")
    vendor_cameras(setup)
    write_cfg(setup, {"persist.waydroid.width": "476"})
    setup.main()
    manifest, metadata = camera_files(setup)
    assert os.path.exists(manifest) and not os.path.exists(metadata)
