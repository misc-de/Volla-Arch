# Shared builders for the boot tests: synthetic Android boot images (AOSP boot image header v4,
# vendor_boot header v4), newc cpio archives and lz4/zstd packing. Everything is made in memory or
# in tmp_path; no image of a real device is read.

import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

from conftest import PKG

BOOT_IMAGE = PKG / "plinius-crypt" / "boot-image"
PAGE = 4096
VENDOR_HEADER_SIZE = 2128   # AOSP vendor_boot_img_hdr_v4
BOOT_HEADER_SIZE = 1584     # AOSP boot_img_hdr_v4
ENTRY_SIZE = 108            # vendor_ramdisk_table_entry_v4: size, offset, type, name[32], board_id[16]


def align(n, a=PAGE):
    return (n + a - 1) // a * a


def pad(data, a=PAGE):
    return data + b"\0" * (align(len(data), a) - len(data))


def vendor_boot(ramdisks, *, dtb=b"DTB-BLOB" * 50, cmdline=b"console=ttyS0 bootconfig",
                bootconfig=b"androidboot.hardware=mt6789\n", page=PAGE, version=4,
                magic=b"VNDRBOOT", names=None):
    """A vendor_boot v4 image. ramdisks: list of (bytes, type); type 1 platform, 2 recovery."""
    blob = b"".join(r for r, _ in ramdisks)
    head = struct.pack("<8sIIIII", magic, version, page, 0x40000000, 0x41000000, len(blob))
    head += cmdline.ljust(2048, b"\0") + struct.pack("<I", 0x47c80000) + b"plinius".ljust(16, b"\0")
    table = b""
    offset = 0
    for i, (r, kind) in enumerate(ramdisks):
        name = (names[i] if names else f"rd{i}".encode()).ljust(32, b"\0")
        table += struct.pack("<III", len(r), offset, kind) + name + b"\0" * 64
        offset += len(r)
    head += struct.pack("<IIQ", VENDOR_HEADER_SIZE, len(dtb), 0x47c80000)
    head += struct.pack("<IIII", len(table), len(ramdisks), ENTRY_SIZE, len(bootconfig))
    assert len(head) == VENDOR_HEADER_SIZE
    return (pad(head, page) + pad(blob, page) + pad(dtb, page) + pad(table, page)
            + pad(bootconfig, page))


def init_boot(ramdisk, *, total=None, kernel_size=0, version=4, magic=b"ANDROID!", signature=b"",
              ramdisk_size=None):
    """A boot image v4 without a kernel (init_boot), filled up to total bytes."""
    head = struct.pack("<8sIIII", magic, kernel_size,
                       len(ramdisk) if ramdisk_size is None else ramdisk_size, 0, BOOT_HEADER_SIZE)
    head += b"\0" * 16 + struct.pack("<I", version) + b"\0" * 1536 + struct.pack("<I", len(signature))
    assert len(head) == BOOT_HEADER_SIZE
    image = pad(head) + pad(ramdisk) + pad(signature)
    if total is not None:
        assert total >= len(image)
        image = image.ljust(total, b"\0")
    return image


def newc(entries):
    """A newc cpio archive of (name, mode, body) entries plus the trailer (like boot-image cpio)."""
    out = bytearray()

    def add(name, mode, body, ino):
        name_b = name.encode() + b"\0"
        nlink = 2 if (mode & 0o170000) == 0o040000 else 1
        fields = (ino, mode, 0, 0, nlink, 0, len(body), 0, 0, 0, 0, len(name_b), 0)
        out.extend(b"070701" + b"".join(b"%08X" % f for f in fields) + name_b)
        out.extend(b"\0" * (-len(out) % 4))
        out.extend(body)
        out.extend(b"\0" * (-len(out) % 4))

    for ino, (name, mode, body) in enumerate(entries, start=1):
        add(name, mode, body, ino)
    add("TRAILER!!!", 0, b"", 0)
    return bytes(out)


def parse_newc(data):
    """(name, mode, body) of every entry, archive after archive (zero padding skipped)."""
    pos, out = 0, []
    while pos < len(data):
        if data[pos] == 0:
            pos += 1
            continue
        assert data[pos:pos + 6] == b"070701", f"no cpio entry at {pos}"
        h = data[pos:pos + 110]
        mode, size, namesize = (int(h[a:a + 8], 16) for a in (14, 54, 94))
        name = data[pos + 110:pos + 110 + namesize - 1].decode()
        pos = (pos + 110 + namesize + 3) & ~3
        out.append((name, mode, data[pos:pos + size]))
        pos = (pos + size + 3) & ~3
    return out


def lz4_legacy(data):
    return subprocess.run(["lz4", "-q", "-12", "-l", "-c"], input=data, capture_output=True,
                          check=True).stdout


def unlz4(data):
    return subprocess.run(["lz4", "-q", "-dc"], input=data, capture_output=True, check=True).stdout


def zstd_pipe(data, wlog=23, level=3):
    """A zstd frame made from a pipe (no content size, so no single segment)."""
    return subprocess.run(["zstd", "-q", f"-{level}", f"--zstd=wlog={wlog}", "-T1", "-c"],
                          input=data, capture_output=True, check=True).stdout


def need(*tools):
    import pytest
    for t in tools:
        if shutil.which(t) is None:
            pytest.skip(f"{t} not on the host")


def run_boot_image(*args, script=BOOT_IMAGE, **kw):
    return subprocess.run([sys.executable, "-I", str(script), *map(str, args)],
                          capture_output=True, **kw)


def boot_image_namespace(script=BOOT_IMAGE):
    """The functions of boot-image without running its command line part."""
    src = Path(script).read_text()
    cut = src.index("\nif not usage_ok():")
    ns = {"__name__": "boot_image_under_test", "__file__": str(script)}
    exec(compile(src[:cut], str(script), "exec"), ns)
    return ns


# UT's scripts/halium, reduced to the places boot-image halium-script changes
UT_HALIUM = (
    "#!/bin/sh\n"
    "# UT's comment at the top\n"
    "mountroot() {\n"
    "\tfor modname in $(cat /lib/modules/modules.load); do\n"
    "\t\t/sbin/modprobe \"$modname\" > /dev/kmsg 2>&1\n"
    "\tdone\n"
    "\ttell_kmsg \"Finished loading kernel modules\"\n"
    "\tsleep 1\n"
    "\t  # an indented comment\n"
    "\tpath=/dev/sda32\n"
    "\t# Format a wiped userdata without a filesystem header\n"
    "\tmount \"$path\" /tmpmnt\n"
    "}\n"
)
UT_ORDER = "/scripts/init-top/udev \"$@\"\n[ -e /conf/param.conf ] && . /conf/param.conf\n"


def ut_init_ramdisk(halium=UT_HALIUM, order=UT_ORDER, archives=1, extra=b""):
    """UT's init_boot ramdisk: lz4 legacy archives with scripts/halium and init-top/ORDER."""
    out = b""
    for i in range(archives):
        body = halium if i == archives - 1 else halium.replace("sleep 1", "sleep 1 # older")
        entries = [("scripts", 0o40755, b""), ("scripts/halium", 0o100750, body.encode()),
                   ("scripts/init-top", 0o40755, b""),
                   ("scripts/init-top/ORDER", 0o100644, order.encode()),
                   ("init", 0o100755, b"#!/bin/sh\necho init\n" + extra)]
        out += lz4_legacy(newc(entries))
    return out


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def lvgl_xrgb(w, h, pixel=lambda x, y: (10, 20, 30), stride=None):
    """An LVGL v9 XRGB8888 image (header + B, G, R, X rows)."""
    stride = stride or w * 4
    head = struct.pack("<BBHHHHH", 0x19, 0x11, 0, w, h, stride, 0)
    rows = bytearray()
    for y in range(h):
        row = bytearray()
        for x in range(w):
            b, g, r = pixel(x, y)
            row += bytes((b, g, r, 0xFF))
        rows += row.ljust(stride, b"\xee")
    return head + bytes(rows)


def env_with(**extra):
    env = dict(os.environ)
    env.update({k: str(v) for k, v in extra.items()})
    return env


# --- a fork server for boot-image ----------------------------------------------------------------
# halium-rebuild runs boot-image some 20 times per build; a fresh Python each time is most of the
# time its tests take. In those tests "boot-image" is a small C client that hands its arguments,
# working directory and stdin/stdout/stderr to a Python process that has the unchanged script
# compiled already and runs it in a forked child. test_boot_image.py runs the script itself.

SERVER_PY = r'''
import os, signal, socket, struct, sys, traceback
import subprocess  # noqa: F401  (what boot-image imports, loaded once)
sock_path, script = sys.argv[1:3]
code = compile(open(script).read(), script, "exec")
signal.signal(signal.SIGCHLD, signal.SIG_IGN)
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(sock_path)
srv.listen(64)
print("ready", flush=True)
while True:
    conn, _ = srv.accept()
    if os.fork():
        conn.close()
        continue
    srv.close()
    head, fds, _, _ = socket.recv_fds(conn, 4, 3)
    (n,) = struct.unpack("<I", head)
    data = b""
    while len(data) < n:
        data += conn.recv(n - len(data))
    parts = [os.fsdecode(p) for p in data.split(b"\0")[:-1]]
    for i, fd in enumerate(fds):
        os.dup2(fd, i)
        os.close(fd)
    os.chdir(parts[0])
    sys.argv = [script, *parts[1:]]
    rc = 0
    try:
        exec(code, {"__name__": "__main__", "__file__": script})
    except SystemExit as e:
        if isinstance(e.code, int):
            rc = e.code
        elif e.code is not None:
            print(e.code, file=sys.stderr)
            rc = 1
    except BaseException:
        traceback.print_exc()
        rc = 1
    for f in (sys.stdout, sys.stderr):
        try:
            f.flush()
        except Exception:
            rc = rc or 1
    conn.sendall(struct.pack("<i", rc))
    os._exit(rc)
'''

CLIENT_C = r'''
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

int main(int argc, char **argv)
{
    static char buf[1 << 16];
    size_t n = 0;
    if (!getcwd(buf, PATH_MAX))
        return 111;
    n = strlen(buf) + 1;
    for (int i = 1; i < argc; i++) {
        size_t l = strlen(argv[i]) + 1;
        if (n + l > sizeof buf)
            return 111;
        memcpy(buf + n, argv[i], l);
        n += l;
    }
    int s = socket(AF_UNIX, SOCK_STREAM, 0);
    struct sockaddr_un addr = { .sun_family = AF_UNIX };
    strncpy(addr.sun_path, SERVER_PATH, sizeof addr.sun_path - 1);
    if (connect(s, (struct sockaddr *) &addr, sizeof addr) < 0) {
        perror("boot-image client");
        return 111;
    }
    uint32_t len = n;
    struct iovec iov[2] = { { &len, 4 }, { buf, n } };
    int fds[3] = { 0, 1, 2 };
    char control[CMSG_SPACE(sizeof fds)];
    struct msghdr msg = { .msg_iov = iov, .msg_iovlen = 2, .msg_control = control,
                          .msg_controllen = sizeof control };
    struct cmsghdr *c = CMSG_FIRSTHDR(&msg);
    c->cmsg_level = SOL_SOCKET;
    c->cmsg_type = SCM_RIGHTS;
    c->cmsg_len = CMSG_LEN(sizeof fds);
    memcpy(CMSG_DATA(c), fds, sizeof fds);
    if (sendmsg(s, &msg, 0) < 0)
        return 111;
    int32_t rc;
    size_t got = 0;
    while (got < 4) {
        ssize_t r = read(s, (char *) &rc + got, 4 - got);
        if (r <= 0)
            return 111;
        got += r;
    }
    return rc;
}
'''


class BootImageServer:
    """Starts the server for SCRIPT; client_into(path) puts the client there."""

    def __init__(self, workdir, script=BOOT_IMAGE):
        from conftest import compile_c
        self.dir = Path(workdir)
        self.sock = self.dir / "s"
        (self.dir / "server.py").write_text(SERVER_PY)
        (self.dir / "client.c").write_text(CLIENT_C)
        self.client = compile_c(self.dir / "client", [self.dir / "client.c"],
                                flags=[f'-DSERVER_PATH="{self.sock}"'])
        # The server reaches only lz4 (slim, halium-script)
        tools = self.dir / "bin"
        tools.mkdir()
        (tools / "lz4").symlink_to(shutil.which("lz4"))
        self.proc = subprocess.Popen([sys.executable, "-I", "-S", str(self.dir / "server.py"),
                                      str(self.sock), str(script)],
                                     stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                     env={"PATH": str(tools), "LC_ALL": "C.UTF-8"})
        assert self.proc.stdout.readline() == b"ready\n"

    def client_into(self, path):
        shutil.copy(self.client, path)

    def stop(self):
        self.proc.kill()
        self.proc.wait()
