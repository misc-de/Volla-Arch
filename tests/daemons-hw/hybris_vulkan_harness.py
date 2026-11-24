# Shared by the hybris-vulkan tests: the generated linux-dmabuf protocol code and the build of a
# harness that includes the ICD's two C files.

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from conftest import REPO
from dhw_harness import PKG, build_harness, sandboxed_source

DIR = PKG / "hybris-vulkan"
CORE = DIR / "hybris_vulkan.c"
WSI = DIR / "wsi_wayland.c"
PROTOCOL_XML = "stable/linux-dmabuf/linux-dmabuf-v1.xml"


# Without vulkan-headers on the host the headers come from the package in the ALARM cache: it is
# arch "any" (the same headers on every architecture), unpacked once under build/test-cache.
PACMAN_CACHE = REPO / "build" / "cache" / "pacman"
HEADER_CACHE = REPO / "build" / "test-cache" / "vulkan-headers"


def _compiles(flags):
    cc = shutil.which("cc")
    if cc is None:
        return False
    r = subprocess.run([cc, *flags, "-E", "-x", "c", "-"], input="#include <vulkan/vk_icd.h>\n",
                       capture_output=True, text=True)
    return r.returncode == 0


def _cached_headers():
    """-I flag for vulkan-headers unpacked from the newest package in the pacman cache, or None"""
    packages = sorted(p for p in PACMAN_CACHE.glob("vulkan-headers-*-any.pkg.tar.*")
                      if not p.name.endswith(".sig"))
    if not packages or shutil.which("tar") is None:
        return None
    pkg = packages[-1]
    target = HEADER_CACHE / pkg.name.split(".pkg.tar")[0]
    if not (target / "usr/include/vulkan/vk_icd.h").exists():
        HEADER_CACHE.mkdir(parents=True, exist_ok=True)
        # Unpacked beside and renamed: test groups running side by side never see half a tree
        tmp = Path(tempfile.mkdtemp(dir=HEADER_CACHE, prefix=".unpack-"))
        r = subprocess.run(["tar", "-xf", str(pkg), "-C", str(tmp), "usr/include"],
                           capture_output=True)
        try:
            if r.returncode != 0:
                return None
            os.rename(tmp, target)
        except OSError:  # another group was first
            pass
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return f"-I{target / 'usr/include'}"


def _vulkan_flags():
    if _compiles([]):
        return []
    cached = _cached_headers()
    return [cached] if cached and _compiles([cached]) else None


VULKAN_FLAGS = _vulkan_flags()  # [] host headers, [-I...] cached ones, None: none at all


def vulkan_headers_present():
    """vulkan-icd-loader brings vulkan.pc, but the headers are vulkan-headers"""
    return VULKAN_FLAGS is not None


needs_vulkan = pytest.mark.skipif(not vulkan_headers_present(),
                                  reason="no Vulkan headers on the host (vulkan-headers) nor in "
                                         "build/cache/pacman")


def generate_protocol(out_dir, *, server=False):
    """Client (and server) header plus the interface code for linux-dmabuf, into out_dir"""
    if shutil.which("wayland-scanner") is None or shutil.which("pkg-config") is None:
        pytest.skip("no wayland-scanner")
    r = subprocess.run(["pkg-config", "--variable=pkgdatadir", "wayland-protocols"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("no wayland-protocols on the host")
    xml = Path(r.stdout.strip()) / PROTOCOL_XML
    out_dir = Path(out_dir)
    kinds = [("client-header", "linux-dmabuf-v1-client-protocol.h"),
             ("private-code", "linux-dmabuf-v1-protocol.c")]
    if server:
        kinds.append(("server-header", "linux-dmabuf-v1-server-protocol.h"))
    for kind, name in kinds:
        subprocess.run(["wayland-scanner", kind, str(xml), str(out_dir / name)], check=True)
    return out_dir / "linux-dmabuf-v1-protocol.c"


def build(out_dir, name, harness_code, *, replacements_wsi=(), pkgs=("wayland-client",), server=False):
    """Copy both C files (and the header) into out_dir and compile the harness that includes
    them; the protocol code is compiled into it"""
    out_dir = Path(out_dir)
    protocol = generate_protocol(out_dir, server=server)
    sandboxed_source(CORE, out_dir, [])
    sandboxed_source(WSI, out_dir, list(replacements_wsi))
    shutil.copy(DIR / "hybris_vulkan.h", out_dir / "hybris_vulkan.h")
    return build_harness(out_dir, name, harness_code + f'\n#include "{protocol}"\n', pkgs=pkgs,
                         flags=["-pthread", *(VULKAN_FLAGS or [])])
