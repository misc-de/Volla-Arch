"""Several floppy images as one drive A: for DOSBox 0.74, with the disks swapped by themselves.

DOSBox 0.74 only takes one floppy image per drive, and the DOSBox versions that take several
only swap on a hotkey, which a phone does not have. So all disks are unpacked into one
directory that DOSBox mounts as A:. Most files only exist on one disk and simply sit there.
Files that are on several disks with different contents (DISK.ID, INSTALL.DAT and the like:
how installers tell the disks apart) show the version of the disk that is "in the drive".

The disk changes when the installer is done with it: every larger file that only this disk
has was opened and read through. How far a file was read comes from the reading processes'
file positions (/proc/<pid>/fdinfo): inotify merges repeated read events, so counting them
cannot tell a full pass from a peek at the header. The prompt for the next disk then finds it already inserted
and only needs Enter. A disk can also be put in by hand (insert()), for installers that read
the disks in another order.
"""

import ctypes
import os
import struct
import threading
import time

from .fat import FatError, read_image

IN_ACCESS = 0x001
IN_CLOSE_NOWRITE = 0x010
IN_OPEN = 0x020
IN_NONBLOCK = 0o4000

# Only files above this size count for "done with the disk": small ones (README, a logo) are
# often never read by the installer and would keep the disk in the drive forever.
DATA_FILE_MIN = 64 * 1024
# Read through: the reader got at least this far into the file. Less is a peek at the header,
# like DOTT's installer looking at DOTT.001 before it starts.
READ_FRACTION = 0.5


class FloppySet:
    def __init__(self, images, directory, log=print):
        self.disks = [read_image(p) for p in images]
        self.dir = directory
        self.log = log
        self.current = 0
        self._lock = threading.Lock()
        # path -> {disk index: content}
        versions = {}
        for i, disk in enumerate(self.disks):
            for path, data in disk.files.items():
                versions.setdefault(path, {})[i] = data
        self.varying = {p: v for p, v in versions.items() if len(set(v.values())) > 1}
        # Per disk: the larger files only it has, which show that the installer is done with it
        self.owned = [set() for _ in self.disks]
        for path, v in versions.items():
            if len(v) == 1:
                (i, data), = v.items()
                if len(data) >= DATA_FILE_MIN:
                    self.owned[i].add(path)
        self._versions = versions
        self._done = set()
        # path -> furthest file position seen while it was open
        self._pos = {}
        self._size = {p: max(len(d) for d in v.values()) for p, v in versions.items()}

    @property
    def label(self):
        return self.disks[0].label

    def build(self):
        """Writes the union of all disks into the directory, disk 1 inserted."""
        os.makedirs(self.dir, exist_ok=True)
        for disk in self.disks:
            for d in disk.dirs:
                os.makedirs(self._host(d), exist_ok=True)
        for path, v in self._versions.items():
            data = v.get(0, next(iter(v.values())))
            with open(self._host(path), "wb") as f:
                f.write(data)
        self.log(f"{len(self.disks)} disks, {len(self._versions)} files, "
                 f"{len(self.varying)} differ between disks: {', '.join(sorted(self.varying))}")

    def _host(self, path):
        """The host path of a file on the disks; refuses names that leave the drive folder
        (fat.py refuses them already, this holds for any reader)."""
        parts = path.split("/")
        if any(p in ("", ".", "..") or "\\" in p or "\0" in p for p in parts):
            raise FatError(f"not a file of the drive: {path!r}")
        host = os.path.join(self.dir, *parts)
        root = os.path.realpath(self.dir)
        if os.path.commonpath([root, os.path.realpath(host)]) != root:
            raise FatError(f"not a file of the drive: {path!r}")
        return host

    def insert(self, index):
        """Puts disk index (0-based) into the drive: rewrites the files that differ."""
        with self._lock:
            if not 0 <= index < len(self.disks):
                return
            for path, v in self.varying.items():
                if index not in v:
                    continue
                # In place, so the file keeps its inode: DOSBox caches the directory
                with open(self._host(path), "r+b") as f:
                    f.write(v[index])
                    f.truncate()
            self.current = index
            self._done.clear()
            self.log(f"disk {index + 1} of {len(self.disks)} inserted")

    def _file_read(self, path):
        if path in self.owned[self.current]:
            self._done.add(path)
            if self._done >= self.owned[self.current] and self.current + 1 < len(self.disks):
                self.log(f"disk {self.current + 1} read")
                self.insert(self.current + 1)

    def _sample(self, pids):
        """Notes how far the processes got in the files of the drive they have open."""
        prefix = os.path.join(os.path.realpath(self.dir), "")
        for pid in pids():
            fd_dir = f"/proc/{pid}/fd"
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                continue
            for fd in fds:
                try:
                    target = os.readlink(f"{fd_dir}/{fd}")
                    if not target.startswith(prefix):
                        continue
                    with open(f"/proc/{pid}/fdinfo/{fd}") as f:
                        pos = int(f.readline().split()[1])
                except (OSError, ValueError, IndexError):
                    continue
                path = os.path.relpath(target, prefix).replace(os.sep, "/").upper()
                if pos > self._pos.get(path, 0):
                    self._pos[path] = pos

    def watch(self, stop, pids):
        """Follows the installer's reads until stop (a threading.Event) is set. pids returns the
        processes that read the drive (DOSBox)."""
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        fd = libc.inotify_init1(IN_NONBLOCK)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        watches = {}
        for root, _dirs, _files in os.walk(self.dir):
            wd = libc.inotify_add_watch(fd, root.encode(), IN_OPEN | IN_ACCESS | IN_CLOSE_NOWRITE)
            if wd < 0:
                raise OSError(ctypes.get_errno(), f"inotify_add_watch {root}")
            rel = os.path.relpath(root, self.dir)
            watches[wd] = "" if rel == "." else rel.replace(os.sep, "/").upper() + "/"
        open_files = set()
        try:
            while not stop.is_set():
                try:
                    buf = os.read(fd, 8192)
                except BlockingIOError:
                    buf = b""
                    if open_files:
                        self._sample(pids)
                    time.sleep(0.02)
                    continue
                self._sample(pids)
                pos = 0
                while pos < len(buf):
                    wd, mask, _cookie, length = struct.unpack_from("iIII", buf, pos)
                    name = buf[pos + 16:pos + 16 + length].rstrip(b"\0").decode(errors="replace")
                    pos += 16 + length
                    if not name:
                        continue
                    path = watches.get(wd, "") + name.upper()
                    if mask & IN_OPEN:
                        open_files.add(path)
                    elif mask & IN_CLOSE_NOWRITE:
                        open_files.discard(path)
                        got = self._pos.pop(path, 0)
                        if path in self.varying:
                            self.log(f"installer checked {path}")
                        elif got >= self._size.get(path, 0) * READ_FRACTION:
                            self._file_read(path)
        finally:
            os.close(fd)
