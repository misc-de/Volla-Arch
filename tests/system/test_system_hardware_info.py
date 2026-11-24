# plinius-hardware-info: the key file for Settings > System > Hardware, built from firmware
# partitions, sysfs, bootconfig and Android's build.prop files. Run as a copy on a small fake
# root in tmp_path (partitions are plain files); journalctl is a stub.

import configparser

import pytest

from conftest import PKG
from system_helpers import rewritten_copy, run

SRC = PKG / "device-volla-ansuz" / "plinius-hardware-info"


def w(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode()
    path.write_bytes(data)


def junk(*parts):
    # Binary images: the build strings sit between non-printable bytes
    return b"\x00\x01\xff".join(p.encode() if isinstance(p, str) else p for p in parts)


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "root"
    w(r / "proc/bootconfig", 'androidboot.slot_suffix = "_b"\nandroidboot.hardware = "mt6878"\n'
      'androidboot.vbmeta.avb_version = "1.2"\nandroidboot.vbmeta.device_state = "unlocked"\n')
    w(r / "sys/class/misc/mali0/device/gpuinfo", "Mali-G615 2 cores r1p3 0xB8A3\n")
    w(r / "sys/block/sda/size", "8192\n")
    w(r / "sys/block/sdc/size", "999999\n")
    w(r / "sys/block/sdc/device/vendor", "SKhynix ")
    w(r / "sys/block/sdc/device/model", "HN8T15DEHKX077  \n")
    w(r / "sys/block/sdc/device/rev", "A002\n")
    w(r / "sys/bus/spi/devices/spi0.0/fts_fw_version", "0x12\n")
    w(r / "sys/bus/spi/devices/spi0.0/fts_driver_info", "IC ID:0x5452 (FT8057P)\n")
    w(r / "sys/bus/i2c/devices/3-0008/name", "st21nfc\n")
    w(r / "sys/bus/i2c/devices/5-002b/name", "mt5706\n")
    w(r / "sys/bus/i2c/devices/5-002b/fw_version", "1.2.3\n")
    w(r / "sys/bus/i2c/devices/6-0010/name", "other\n")
    v = r / "android/vendor"
    w(v / "firmware/st54l_fw.bin", b"\0")
    w(v / "firmware/AudioParamVersionInfo.txt", "chip:MT6878+MT6369+AW87565\nversion:1.0.5\n")
    w(v / "build.prop", "ro.vendor.build.version.release=14\nro.vendor.build.id=UP1A.231005\n"
      "ro.vendor.build.security_patch=2025-06-05\n"
      "ro.vendor.mediatek.version.release=alps-mp-u0.mp1\n")
    w(v / "lib64/egl/libGLES_mali.so", junk("ELF", "r49p1-01eac0", "x"))
    w(r / "android/vendor_dlkm/etc/build.prop", "ro.vendor_dlkm.build.version.release=14\n"
      "ro.vendor_dlkm.build.id=UP1A\nro.vendor_dlkm.build.date.utc=1750000000\n")
    w(r / "android/system/build.prop", "ro.build.version.release=14\nro.build.id=UQ1A\n"
      "ro.build.date.utc=1751000000\nro.build.version.security_patch=2025-07-05\n")
    w(r / "dev/sda", junk("MMM", "V1.2.3", "20250101-120000", "end"))
    p = r / "dev/disk/by-partlabel"
    w(p / "lk_b", junk("LK", "abcdef01_20250626122900"))
    w(p / "lk_a", junk("LK", "00000000_20200101000000"))  # the other slot is never read
    w(p / "tee_b", junk("lts-v2.10.3", "Built : 12:29:00, Jun 26 2025", "Kinibi-500A_V1-20250101_x"))
    w(p / "gz_b", junk("mTEE_SDK: 3.2.1.0 rel, Built: 12:00:00 Jun  5 2025",
                       "GZ_hypervisor: 3.2.1.1 rel, Built: 12:00:00 Jun  6 2025"))
    w(p / "spmfw_b", junk("pcm_suspend_v1.2_20250101"))
    w(p / "modem_b", junk("MOLY.NR17.R1.MP.V1.2", "2025/06/26 12:29*TK_MD"))
    w(p / "connsys_wifi_b", junk("t-neptune-x_MT6631-20240101000000",
                                 "t-neptune-cl_MT6637-20250626122900"))
    w(p / "connsys_bt_b", junk("t-neptune-abc_6637_BT_x-20250627122900"))
    return r


def hardware_info(tmp_path, fake_bin, root, adie="6637"):
    out = tmp_path / "hardware"
    script = rewritten_copy(SRC, tmp_path / "plinius-hardware-info", {
        "OUT=/run/plinius-hardware": f"OUT={out}",
        "VENDOR=/android/vendor": f"VENDOR={root}/android/vendor",
        "/proc/bootconfig": root / "proc/bootconfig",
        "/sys/": f"{root}/sys/",
        "/dev/disk/by-partlabel/": f"{root}/dev/disk/by-partlabel/",
        "if=/dev/sda": f"if={root}/dev/sda",
        "/android/vendor_dlkm/": f"{root}/android/vendor_dlkm/",
        "/android/system/": f"{root}/android/system/",
    }, check_left=("=/run", "=/android", "/proc/", "/sys/", "if=/dev", "p=/android"))
    fake_bin.stub("journalctl", f'[ -n "{adie}" ] && echo "[FM] connsys adie=0x{adie} ok"')
    r = run(["sh", script], fake_bin.env(isolated=True))
    assert r.returncode == 0, r.stderr
    kf = configparser.ConfigParser(interpolation=None)
    kf.optionxform = str
    kf.read_string(out.read_text())
    assert oct(out.stat().st_mode & 0o777) == "0o644"
    assert not (tmp_path / "hardware.tmp").exists()
    return {s: dict(kf[s]) for s in kf.sections()}


def test_hardware_info_full(tmp_path, fake_bin, root):
    kf = hardware_info(tmp_path, fake_bin, root)
    assert kf["soc"] == {"Name": "MediaTek MT6878"}
    assert kf["gpu"] == {"Name": "Mali-G615 MC2 (r1p3)"}
    # The largest LUN is the user area
    assert kf["storage"] == {"Name": "SKhynix HN8T15DEHKX077", "Firmware": "A002"}
    assert kf["connectivity"] == {"Name": "MediaTek MT6637"}
    assert kf["touch"] == {"Name": "FocalTech (IC 0x5452)", "Firmware": "0x12"}
    assert kf["nfc"] == {"Name": "STMicroelectronics ST54L"}
    assert kf["wireless-charging"] == {"Name": "MT5706", "Firmware": "1.2.3"}
    assert kf["audio"] == {"Name": "MT6369, AW87565"}
    assert kf["preloader"] == {"Version": "V1.2.3", "Date": "2025-01-01"}
    assert kf["bootloader"] == {"Name": "LK", "Version": "abcdef01", "Date": "2025-06-26"}
    assert kf["atf"] == {"Version": "2.10.3", "Date": "2025-06-26"}
    assert kf["tee"] == {"Name": "Trustonic Kinibi", "Version": "500A_V1", "Date": "2025-01-01"}
    assert kf["trusty"] == {"Name": "MediaTek mTEE (Trusty)", "Version": "3.2.1.0",
                            "Date": "2025-06-05"}
    assert kf["hypervisor"] == {"Name": "MediaTek GenieZone", "Version": "3.2.1.1",
                                "Date": "2025-06-06"}
    assert kf["spm"] == {"Name": "SPM", "Version": "1.2", "Date": "2025-01-01"}
    assert kf["verified-boot"] == {"Name": "AVB 1.2", "State": "unlocked", "Slot": "b"}
    assert kf["modem"] == {"Version": "MOLY.NR17.R1.MP.V1.2", "Date": "2025-06-26"}
    # The connsys image of this phone's A-die, not the first one in the partition
    assert kf["wifi"] == {"Date": "2025-06-26"}
    assert kf["bluetooth"] == {"Date": "2025-06-27"}
    assert kf["vendor"] == {"Version": "Android 14 (UP1A.231005)", "Patch": "2025-06-05"}
    assert kf["bsp"] == {"Version": "alps-mp-u0.mp1"}
    assert kf["vendor-modules"] == {"Version": "Android 14 (UP1A)", "Date": "2025-06-15"}
    assert kf["gpu-driver"] == {"Version": "r49p1-01eac0"}
    assert kf["audio-params"] == {"Version": "1.0.5"}
    assert kf["system"] == {"Name": "Halium", "Version": "Android 14 (UQ1A)",
                            "Date": "2025-06-27", "Patch": "2025-07-05"}


def test_hardware_info_missing_sources_leave_groups_out(tmp_path, fake_bin, root):
    import shutil
    shutil.rmtree(root / "android")
    shutil.rmtree(root / "sys/bus")
    (root / "sys/class/misc/mali0/device/gpuinfo").unlink()
    kf = hardware_info(tmp_path, fake_bin, root, adie="")
    for group in ("gpu", "connectivity", "touch", "nfc", "wireless-charging", "audio", "vendor",
                  "bsp", "vendor-modules", "gpu-driver", "audio-params"):
        assert group not in kf
    # Without the A-die from the kernel log the MT6637 image is assumed
    assert kf["wifi"] == {"Date": "2025-06-26"}
    # Groups with a fixed name stay, with only what could be read
    assert kf["system"] == {"Name": "Halium"}


def test_hardware_info_strips_control_characters(tmp_path, fake_bin, root):
    # A value with a newline or escape would break the key file Settings parses
    w(root / "sys/bus/i2c/devices/5-002b/fw_version", "1.2\x1b[31m\n[evil]\nx=1\n")
    kf = hardware_info(tmp_path, fake_bin, root)
    assert kf["wireless-charging"]["Firmware"] == "1.2[31m[evil]x=1"
    assert "evil" not in kf
