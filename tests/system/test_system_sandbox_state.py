"""Sandbox drop-ins of device-volla-ansuz and the directories they need writable.

ReadWritePaths= of a path that does not exist stops the service at the NAMESPACE step (status
226), every time it starts. A state directory the service's own package does not ship has to
come from StateDirectory=, which systemd creates on the first start of a new system.
"""
import pathlib
import re

import pytest

DEVICE = pathlib.Path(__file__).resolve().parents[2] / "pkgbuilds" / "device-volla-ansuz"

# Directories under /var/lib that the package of the service ships itself (checked on a newly
# installed phone, 25.11.2026); everything else under /var/lib needs StateDirectory=.
SHIPPED = {"/var/lib/bluetooth", "/var/lib/nfcd", "/var/lib/ofono"}


def settings(path):
    """{key: [values]} of the [Service] section of a drop-in."""
    out = {}
    for line in path.read_text().splitlines():
        m = re.match(r"^(\w+)=(.*)$", line)
        if m:
            out.setdefault(m.group(1), []).append(m.group(2))
    return out


DROPINS = sorted(DEVICE.glob("*-sandbox.conf"))


def test_dropins_found():
    assert any(p.name == "biomd-sandbox.conf" for p in DROPINS)


@pytest.mark.parametrize("path", DROPINS, ids=lambda p: p.name)
def test_var_lib_paths_exist_on_a_new_system(path):
    conf = settings(path)
    writable = [p for v in conf.get("ReadWritePaths", []) for p in v.split()]
    missing = [p for p in writable if p.startswith("/var/lib/") and p not in SHIPPED]
    assert not missing, f"{path.name}: {missing} may not exist yet, use StateDirectory="


def test_biomd_gets_its_database_directory_from_systemd():
    conf = settings(DEVICE / "biomd-sandbox.conf")
    assert conf["StateDirectory"] == ["biomd"]
    assert conf["StateDirectoryMode"] == ["0700"]
    assert conf["ProtectSystem"] == ["strict"]
