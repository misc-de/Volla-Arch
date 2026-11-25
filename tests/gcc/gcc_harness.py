# Host tests of our GNOME Control Center panel files (pkgbuilds/gnome-control-center-plinius/
# cc-*.c): one file at a time, compiled with the parts of GNOME Settings it needs into a small
# GTK program (c/gcc_driver.c) that shows the panel in a headless display and runs a script of
# actions against it, while fake D-Bus services answer and record what the panel asks.
#
# - The source tree is GNOME Settings at the pinned commit with our prepare() applied (patches,
#   copied files), made once and kept in build/test-cache/gcc/ under a key of the commit, the
#   PKGBUILD and every local source file; nothing is built with meson.
# - A test program is c/gcc_driver.c + the test's glue (a few lines of C calling the file's entry
#   point) + shell/cc-panel.c and panels/common/cc-list-row.c of the tree + the panel file as a
#   copy whose paths into the system are replaced by paths in tmp (sandboxed_copy refuses
#   leftovers; the copy starts with #line naming the file in pkgbuilds/, so gcov counts it there).
#   The window, the application and the shell model are stand-ins (c/stubs/); the two templates
#   GNOME writes in Blueprint are in ui/ as GtkBuilder XML (no blueprint-compiler on the host).
# - Display: gtk4-broadwayd on unix sockets only (no TCP port), one per session.
# - D-Bus: per test a private session bus and a private "system" bus (DBUS_SYSTEM_BUS_ADDRESS),
#   with gcc_fakebus.py serving the services a test describes (interfaces taken from the real
#   services' C sources where we have them), recording every call.
# - Files: HOME, XDG dirs and XDG_DATA_DIRS point into tmp_path; programs come from fake_bin.

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from conftest import GCOV_DIR, PKG, REPO, FakeBin, compile_c

HERE = Path(__file__).resolve().parent
CDIR = HERE / "c"
STUBS = CDIR / "stubs"
UI = HERE / "ui"
GCC = PKG / "gnome-control-center-plinius"
NAME = "gnome-control-center-plinius"
CACHE = REPO / "build" / "test-cache" / "gcc"
# The stable branch the pinned commit is on (PKGBUILD); its newest commit in the mirror stands in
# when the mirror lacks the pinned one (fetched on the build server only): the difference is
# translations and fixes upstream, the files a test compiles are ours and GNOME's headers.
FALLBACK_BRANCH = "refs/heads/gnome-51"

BASE_PKGS = ("gtk4", "libadwaita-1", "json-glib-1.0", "gio-unix-2.0")
SYSTEM_PREFIXES = ("/sys/", "/dev/", "/proc/", "/run/", "/var/", "/etc/", "/usr/", "/home/",
                   "/root/", "/boot/", "/opt/", "/srv/", "/mnt/", "/media/", "/lib/", "/bin/",
                   "/data/")
STRING_LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"')


class Unavailable(Exception):
    """Something the tests need is missing on the host: the tests skip."""


def _tool(name):
    path = shutil.which(name)
    if not path:
        raise Unavailable(f"no {name} on the host")
    return path


# ----- The prepared source tree -----

def _git(*args, cwd=None):
    return subprocess.run(["git", "-c", "safe.directory=*", *args], cwd=cwd, check=True,
                          capture_output=True, text=True)


def _resolve(mirror, ref):
    try:
        return _git("rev-parse", "-q", "--verify", f"{ref}^{{commit}}", cwd=mirror).stdout.strip()
    except subprocess.CalledProcessError:
        return None


def _mirrors(fname):
    """Where makepkg keeps a git source: the build's SRCDEST, or next to the PKGBUILD."""
    import prepare_helper
    return [d for d in (prepare_helper.SRCDEST / fname, GCC / fname) if d.is_dir()]


def _plan():
    """[(entry kind, srcdir name, location, commit)] and whether every commit is the pinned one."""
    import prepare_helper
    sources, _noextract, _ = prepare_helper.read(NAME)
    plan, pinned = [], True
    for entry in sources:
        fname, kind, loc, fragment = prepare_helper.parse(entry)
        if kind == "local":
            plan.append(("local", Path(loc).name, GCC / loc, None))
            continue
        if kind != "git":
            raise Unavailable(f"{entry}: not a git source")
        ref = fragment.partition("=")[2] if fragment else "HEAD"
        found = None
        for mirror in _mirrors(fname):
            commit = _resolve(mirror, ref)
            if commit:
                found = (mirror, commit)
                break
        if not found and fname == "gnome-control-center":
            for mirror in _mirrors(fname):
                commit = _resolve(mirror, FALLBACK_BRANCH)
                if commit:
                    found, pinned = (mirror, commit), False
                    break
        if not found:
            raise Unavailable(f"{fname}: {ref} is in no mirror ({prepare_helper.SRCDEST}, {GCC})")
        plan.append(("git", fname, found[0], found[1]))
    return plan, pinned


def _key(plan):
    h = hashlib.sha256()
    h.update((GCC / "PKGBUILD").read_bytes())
    for kind, fname, loc, commit in plan:
        h.update(f"{kind} {fname} {commit}\n".encode())
        if kind == "local":
            h.update(Path(loc).read_bytes())
    return h.hexdigest()[:16]


class Tree:
    def __init__(self, root, commit, pinned):
        self.root = Path(root)
        self.commit = commit
        self.pinned = pinned

    def find(self, name):
        """The directory of a file of ours inside the tree (where prepare() copied it)."""
        hits = sorted(p for p in (self.root / "panels").rglob(name)) + \
            sorted(p for p in (self.root / "shell").glob(name))
        if not hits:
            raise AssertionError(f"{name} is not in the prepared tree")
        return hits[0].parent


def prepared_tree():
    """GNOME Settings with our prepare() applied; made once per change of the PKGBUILD, a local
    source or the commit, under build/test-cache/gcc/<key>."""
    import prepare_helper
    plan, pinned = _plan()
    key = _key(plan)
    commit = next(c for k, f, _l, c in plan if f == "gnome-control-center")
    done = CACHE / key / "done"
    root = CACHE / key / "src" / "gnome-control-center"
    CACHE.mkdir(parents=True, exist_ok=True)
    with open(CACHE / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if done.exists():
            return Tree(root, commit, pinned)
        # Only the newest tree is kept
        for old in CACHE.iterdir():
            if old.is_dir():
                shutil.rmtree(old, ignore_errors=True)
        work = CACHE / key
        srcdir = work / "src"
        srcdir.mkdir(parents=True)
        for kind, fname, loc, commit_ in plan:
            if kind == "local":
                os.symlink(loc, srcdir / fname)
                continue
            _git("clone", "-q", "-s", "--no-checkout", str(loc), str(srcdir / fname))
            _git("checkout", "-q", "--detach", commit_, cwd=srcdir / fname)
        real = prepare_helper.populate
        prepare_helper.populate = lambda name, srcdir: None
        try:
            r = prepare_helper.run_prepare(NAME, work)
        finally:
            prepare_helper.populate = real
        if r.returncode != 0:
            shutil.rmtree(work, ignore_errors=True)
            raise AssertionError(f"prepare() failed:\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}")
        # The links to pkgbuilds/ are not needed any more: the tree has its copies
        for kind, fname, _loc, _c in plan:
            if kind == "local":
                (srcdir / fname).unlink()
        (work / "commit").write_text(f"{commit}{'' if pinned else ' (fallback: ' + FALLBACK_BRANCH + ')'}\n")
        done.write_text("")
    return Tree(root, commit, pinned)


# ----- Session resources: templates, schemas, display -----

class Session:
    """Per pytest session: the compiled templates, the GSettings schemas and the display."""

    def __init__(self, tree, base):
        self.tree = tree
        self.base = Path(base)
        self.short = Path(tempfile.mkdtemp(prefix="gcc-", dir="/tmp"))
        self.display = ":3"
        self.broadwayd = self.browser = None
        self._make_resources()
        self._make_schemas()
        self._start_display()

    def _make_resources(self):
        tool = _tool("glib-compile-resources")
        self.resources = []
        # Registered when the program starts (cc-list-row.c registers its own again: harmless)
        for name, c_name in (("shell", "cc_shell"), ("common", "cc_common")):
            xml = UI / f"{name}.gresource.xml"
            out_c = self.base / f"{name}-resources.c"
            out_h = self.base / f"cc-{name}-resources.h"
            for target, kind in ((out_c, "--generate-source"), (out_h, "--generate-header")):
                subprocess.run([tool, kind, f"--c-name={c_name}",
                                f"--sourcedir={UI}", f"--target={target}", str(xml)],
                               check=True, capture_output=True)
            self.resources.append(out_c)

    def _make_schemas(self):
        """The host's schemas (GNOME's desktop settings) and every schema of ours, compiled
        into one directory: GSETTINGS_SCHEMA_DIR, with the memory backend."""
        tool = _tool("glib-compile-schemas")
        self.schemas = self.base / "schemas"
        self.schemas.mkdir()
        for f in Path("/usr/share/glib-2.0/schemas").glob("*.gschema.xml"):
            shutil.copy(f, self.schemas / f.name)
        # The enums the schemas name (org.gnome.desktop.enums.xml: without it GNOME's
        # interface, background and screensaver schemas are left out)
        for f in Path("/usr/share/glib-2.0/schemas").glob("*.enums.xml"):
            shutil.copy(f, self.schemas / f.name)
        for f in PKG.glob("*/*.gschema.xml"):
            shutil.copy(f, self.schemas / f.name)
        r = subprocess.run([tool, str(self.schemas)], capture_output=True, text=True)
        if r.returncode != 0:
            # A host schema that does not compile alone: ours and GNOME's desktop ones suffice
            for f in self.schemas.glob("*.gschema.xml"):
                if not (f.name.startswith("org.gnome.desktop.") or any(PKG.glob(f"*/{f.name}"))):
                    f.unlink()
            subprocess.run([tool, "--strict", str(self.schemas)], check=True, capture_output=True)

    def _start_display(self):
        tool = _tool("gtk4-broadwayd")
        run = self.short / "run"
        run.mkdir(mode=0o700)
        env = dict(os.environ, XDG_RUNTIME_DIR=str(run))
        # HTTP on a unix socket: no TCP port is opened
        self.broadwayd = subprocess.Popen([tool, "-u", str(run / "http"), self.display], env=env,
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          text=True)
        n = int(self.display[1:]) + 1
        self.socket = run / f"broadway{n}.socket"
        deadline = time.monotonic() + 10
        while not self.socket.exists() or not (run / "http").exists():
            if self.broadwayd.poll() is not None or time.monotonic() > deadline:
                out = self.broadwayd.stdout.read() if self.broadwayd.poll() is not None else ""
                self.close()
                raise Unavailable(f"gtk4-broadwayd did not start: {out}")
            time.sleep(0.01)
        # The browser that answers the frames (see gcc_broadway_client.py)
        self.browser = subprocess.Popen(["python3", "-I", str(HERE / "gcc_broadway_client.py"),
                                         str(run / "http")], stdout=subprocess.PIPE, text=True)
        if self.browser.stdout.readline().strip() != "READY":
            self.close()
            raise AssertionError("the broadway client did not connect")

    def close(self):
        for p in (self.browser, self.broadwayd):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
            if p and p.stdout:
                p.stdout.close()
        shutil.rmtree(self.short, ignore_errors=True)

    def new_short_dir(self):
        return Path(tempfile.mkdtemp(dir=self.short))


@pytest.fixture(scope="session")
def gcc_session(tmp_path_factory):
    try:
        tree = prepared_tree()
        session = Session(tree, tmp_path_factory.mktemp("gcc-session"))
    except Unavailable as e:
        pytest.skip(str(e))
    yield session
    session.close()


# ----- Building a test program -----

def sandboxed_copy(src, out_dir, replacements=(), allow=()):
    """Copy src into out_dir with (old, new) text replacements; refuse a copy whose string
    literals still point into the system (except the prefixes in allow, only read or compared).
    Without a moved line the copy starts with #line naming src, so gcov counts src's lines."""
    src = Path(src)
    original = src.read_text()
    text = original
    for old, new in replacements:
        assert old in text, f"{old!r} not in {src}: the source changed, update the test"
        text = text.replace(old, str(new))
    out_dir = Path(out_dir)
    left = []
    for m in STRING_LITERAL.finditer(text):
        lit = m.group(1)
        if lit.startswith(str(out_dir)) or lit.startswith("/tmp/"):
            continue
        if lit.startswith(SYSTEM_PREFIXES) and not lit.startswith(tuple(allow)):
            left.append(lit)
    assert not left, f"{src.name}: system paths left in the test copy: {sorted(set(left))}"
    if text.count("\n") == original.count("\n"):
        text = f'#line 1 "{src.resolve()}"\n' + text
    dst = out_dir / src.name
    dst.write_text(text)
    return dst


class Program:
    def __init__(self, path, root):
        self.path = Path(path)
        # Stands for / in the panel's replaced paths (files the test writes go there)
        self.root = Path(root)


def build_panel(session, out_dir, name, files, glue, *, replacements=None, allow=(), pkgs=(),
                flags=(), upstream=(), sources=()):
    """Compile files (our panel files, names in pkgbuilds/gnome-control-center-plinius) with the
    glue into the test program out_dir/name. replacements: {file: [(old, new)]}, new may use
    {root}, the directory that stands for / of the system. upstream: more files of the tree
    (relative to its root) to compile. sources: more C files (paths) compiled as they are, e.g.
    a test's own gresource (compile_resources) or a sandboxed copy of a file of the tree."""
    extra_sources = tuple(sources)
    out_dir = Path(out_dir)
    build = out_dir / name
    build.mkdir(parents=True, exist_ok=True)
    root = out_dir / f"{name}-root"
    root.mkdir(exist_ok=True)
    tree = session.tree.root
    replacements = replacements or {}
    sources = [CDIR / "gcc_driver.c"]
    glue_c = build / f"glue_{name}.c"
    glue_c.write_text(glue)
    sources.append(glue_c)
    include = [STUBS, CDIR, build, session.base, tree, tree / "shell", tree / "panels" / "common"]
    for f in files:
        reps = [(old, new.format(root=root)) for old, new in replacements.get(f, ())]
        sources.append(sandboxed_copy(GCC / f, build, reps, allow))
        d = session.tree.find(f)
        if d not in include:
            include.append(d)
    for rel in ("shell/cc-panel.c", "panels/common/cc-list-row.c", *upstream):
        # A copy, so that its "..." includes find the stand-ins before the tree's own headers
        dst = build / Path(rel).name
        shutil.copy(tree / rel, dst)
        sources.append(dst)
        if (tree / rel).parent not in include:
            include.append((tree / rel).parent)
    sources.extend(session.resources)
    sources.extend(Path(s) for s in extra_sources)
    program = compile_c(out_dir / name / "program", sources,
                        flags=[*(f"-I{d}" for d in include), "-Wno-deprecated-declarations",
                               "-Wno-unused-variable", "-Wno-unused-but-set-variable", "-lm", *flags],
                        pkgs=(*BASE_PKGS, *pkgs))
    return Program(program, root)


def compile_resources(out_dir, xml, c_name):
    """The C source of a gresource bundle (glib-compile-resources, files next to xml), registered
    when the program starts: for a test's own templates, e.g. a hand-written .ui of a .blp."""
    xml = Path(xml)
    out_c = Path(out_dir) / f"{c_name}-resources.c"
    subprocess.run([_tool("glib-compile-resources"), "--generate-source", f"--c-name={c_name}",
                    f"--sourcedir={xml.parent}", f"--target={out_c}", str(xml)],
                   check=True, capture_output=True)
    return out_c


def uname_shim(out_dir):
    """A library for LD_PRELOAD whose uname () reports PLINIUS_TEST_UNAME_RELEASE and
    PLINIUS_TEST_UNAME_VERSION (c/gcc_uname_shim.c)."""
    return compile_c(Path(out_dir) / "gcc_uname_shim.so", [CDIR / "gcc_uname_shim.c"],
                     flags=["-shared", "-fPIC", "-ldl"], counted=False)


# ----- D-Bus fakes and the run of a program -----

def interface_xml(c_source, interface):
    """The introspection XML a service's C source registers (string literals and macros that
    are string literals), the <node> containing `interface`."""
    text = Path(c_source).read_text()
    defines = {m.group(1): m.group(2) for m in
               re.finditer(r'^#define\s+(\w+)\s+"((?:[^"\\]|\\.)*)"\s*$', text, re.M)}
    for m in re.finditer(r'"<node>"(.*?)"</node>"', text, re.S):
        parts = ["<node>"]
        # C comments between the literals say nothing (the literals themselves are kept whole)
        body = re.sub(r'("(?:[^"\\\n]|\\.)*")|/\*.*?\*/|//[^\n]*', lambda c: c.group(1) or "",
                      m.group(1), flags=re.S)
        for tok in re.finditer(r'"((?:[^"\\]|\\.)*)"|([A-Za-z_]\w*)', body):
            if tok.group(1) is not None:
                parts.append(tok.group(1))
            else:
                parts.append(defines[tok.group(2)])
        parts.append("</node>")
        xml = "".join(parts).replace('\\"', '"')
        if f"'{interface}'" in xml or f'"{interface}"' in xml:
            return xml
    raise AssertionError(f"no introspection with {interface} in {c_source}")


BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={socket}</listen>
  <auth>EXTERNAL</auth>
  <servicedir>{services}</servicedir>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


class Node(dict):
    """A widget of a dump: type, text, subtitle, active, … and children."""

    def walk(self):
        yield self
        for c in self.get("children", ()):
            yield from Node(c).walk()

    def matches(self, type=None, text=None, contains=None, subtitle=None, **other):
        if type is not None and self.get("type") not in ((type,) if isinstance(type, str) else type):
            return False
        if text is not None and self.get("text") != text:
            return False
        if contains is not None and contains not in (self.get("text") or ""):
            return False
        if subtitle is not None and self.get("subtitle") != subtitle:
            return False
        return all(self.get(k) == v for k, v in other.items())


ROW_TYPES = ("AdwActionRow", "AdwSwitchRow", "AdwComboRow", "AdwEntryRow", "AdwExpanderRow",
             "AdwSpinRow", "AdwPasswordEntryRow", "AdwButtonRow", "CcListRow")


class Screen:
    """A dump of what was on screen (all mapped widgets worth naming, in order)."""

    def __init__(self, nodes):
        self.nodes = [Node(n) for n in nodes]

    def all(self):
        for n in self.nodes:
            yield from n.walk()

    def find(self, **sel):
        return [Node(n) for n in self.all() if Node(n).matches(**sel)]

    def one(self, **sel):
        hits = self.find(**sel)
        assert len(hits) == 1, f"{len(hits)} widgets match {sel}:\n{self.outline()}"
        return hits[0]

    def has(self, **sel):
        return bool(self.find(**sel))

    def within(self, **sel):
        """A screen of the children of the one widget matching sel."""
        return Screen(self.one(**sel).get("children", []))

    def texts(self):
        return [n["text"] for n in self.all() if n.get("text")]

    def rows(self):
        return [(n.get("text"), n.get("subtitle")) for n in self.all() if n.get("type") in ROW_TYPES]

    def outline(self, nodes=None, depth=0):
        lines = []
        for n in nodes if nodes is not None else self.nodes:
            facts = {k: v for k, v in n.items() if k not in ("children", "type")}
            lines.append("  " * depth + n["type"] + (f" {facts}" if facts else ""))
            lines.extend(self.outline(n.get("children", []), depth + 1).splitlines())
        return "\n".join(lines)


class Result:
    def __init__(self, data, stderr, returncode):
        self.data = data
        self.stderr = stderr
        self.returncode = returncode
        self.failure = data.get("failure")
        self.screen = Screen(data.get("screen", []))
        self.visibility = {v["id"]: v["visibility"] for v in data.get("visibility", [])}
        self.app_actions = data.get("app_actions", [])
        self.messages = data.get("messages", [])

    def values(self, label):
        return [r["value"] for r in self.data["results"] if r["label"] == label]

    def value(self, label):
        v = self.values(label)
        assert len(v) == 1, f"{len(v)} results labelled {label!r}"
        return v[0]

    def dump(self, label):
        return Screen(self.value(label))

    def warnings(self, ignore=()):
        """Warnings and criticals, without those matching a pattern of ignore (regular
        expressions on the message)."""
        return [m for m in self.messages
                if not any(re.search(p, m["message"]) for p in (*ignore, *World.IGNORED))]


def _jsonable(v):
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, bytes):
        return list(v)
    return v


class World:
    """Everything one test's program meets: home and data directories in tmp_path, programs from
    fake_bin, two private buses with the fake services, the session's display."""

    # The host has no icon theme in the test's XDG_DATA_DIRS: GTK says so, nothing else changes.
    # GDK 4.24's broadway backend submits a frame's timings twice and warns about it; that is the
    # test display's frame clock (broadway.js answers roundtrips the same way), not the panel.
    IGNORED = (r"^Could not load a pixbuf", r"icon theme", r"^Unable to acquire session bus",
               r"^Duplicate call to gdk_frame_timings_submitted\(\) with different values\.$")

    def __init__(self, session, tmp_path):
        self.session = session
        self.tmp = Path(tmp_path)
        self.short = session.new_short_dir()
        self.home = self.tmp / "home"
        self.data_dir = self.tmp / "share"
        for d in (self.home / ".config", self.home / ".local/share", self.home / ".cache",
                  self.home / ".local/state", self.data_dir / "applications"):
            d.mkdir(parents=True, exist_ok=True)
        self.runtime = self.short / "rt"
        self.runtime.mkdir(mode=0o700)
        # The display's socket, as GDK looks for it in XDG_RUNTIME_DIR
        (self.runtime / self.session.socket.name).symlink_to(self.session.socket)
        self.fake_bin = FakeBin(self.tmp / "bin")
        self.services = []
        self.buses = {}
        self.procs = []
        self.fake = None
        self.call_log = self.tmp / "dbus-calls.jsonl"
        self.extra_env = {}

    # Fake services
    def service(self, bus, name, path, xml, properties=None, replies=None, own=True,
                set_errors=None):
        """A fake object at name/path on "system" or "session" with the interfaces of xml.
        properties: {prop: GVariant text}; replies: {method: reply} where a reply is GVariant
        text of the out arguments as a tuple, {"error": name, "message": text},
        {"delay_ms": n, "reply": text} (answered later), or a list of those for successive calls
        (the last one stays). Methods without a reply answer with
        zeros. own=False: the object is there but the name is not owned (a service that left).
        set_errors: {prop: (D-Bus error name, message)}, the service refuses setting it."""
        self.services.append({"bus": bus, "name": name, "path": path, "xml": xml,
                              "properties": properties or {}, "replies": replies or {},
                              "own": own, "set_errors": set_errors or {}})

    def _start_bus(self, kind):
        d = self.short / kind
        (d / "services").mkdir(parents=True)
        sock = d / "bus"
        conf = d / "bus.conf"
        conf.write_text(BUS_CONFIG.format(socket=sock, services=d / "services"))
        p = subprocess.Popen([_tool("dbus-daemon"), f"--config-file={conf}", "--nofork",
                              "--print-address=1", "--nopidfile"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(p)
        address = p.stdout.readline().strip()
        if not address:
            raise AssertionError(f"dbus-daemon did not start: {p.stderr.read()}")
        self.buses[kind] = address

    def start(self):
        if self.buses:
            return
        try:
            _tool("dbus-daemon")
            subprocess.run(["python3", "-I", "-c", "from gi.repository import Gio"], check=True,
                           capture_output=True)
        except Unavailable as e:
            pytest.skip(str(e))
        except subprocess.CalledProcessError:
            pytest.skip("no PyGObject (python-gobject) for the fake D-Bus services")
        for kind in ("system", "session"):
            self._start_bus(kind)
        spec = self.tmp / "fakebus.json"
        spec.write_text(json.dumps({"services": self.services, "log": str(self.call_log)}))
        env = dict(os.environ, DBUS_SYSTEM_BUS_ADDRESS=self.buses["system"],
                   DBUS_SESSION_BUS_ADDRESS=self.buses["session"])
        self.fake = subprocess.Popen(["python3", "-I", str(HERE / "gcc_fakebus.py"), str(spec)],
                                     env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True)
        self.procs.append(self.fake)
        line = self.fake.stdout.readline()
        if line.strip() != "READY":
            raise AssertionError(f"fake services did not start: {line}{self.fake.stderr.read()}")

    def env(self):
        env = self.fake_bin.env(isolated=True)
        for k in ("DISPLAY", "WAYLAND_DISPLAY", "LANGUAGE", "LANG", "GTK_THEME", "GTK_MODULES",
                  "G_MESSAGES_DEBUG", "GTK_DEBUG", "GSETTINGS_SCHEMA_DIR", "XDG_CURRENT_DESKTOP"):
            env.pop(k, None)
        env.update({
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.home / ".local/share"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "XDG_STATE_HOME": str(self.home / ".local/state"),
            "XDG_RUNTIME_DIR": str(self.runtime),
            "XDG_DATA_DIRS": str(self.data_dir),
            "XDG_CONFIG_DIRS": str(self.tmp / "etc-xdg"),
            "GDK_BACKEND": "broadway",
            "BROADWAY_DISPLAY": self.session.display,
            "GSETTINGS_BACKEND": "memory",
            "GSETTINGS_SCHEMA_DIR": str(self.session.schemas),
            "DBUS_SYSTEM_BUS_ADDRESS": self.buses["system"],
            "DBUS_SESSION_BUS_ADDRESS": self.buses["session"],
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
            "NO_AT_BRIDGE": "1",
            "GTK_A11Y": "none",
            "GIO_USE_VFS": "local",
            "GIO_USE_VOLUME_MONITOR": "unix",
            "ADW_DISABLE_PORTAL": "1",
            # Links open through the portal, which a test can fake and record
            "GDK_DEBUG": "portals",
        })
        env.update(self.extra_env)
        return env

    def run(self, program, steps, params=None, timeout=60, check=True, session_bus=True,
            system_bus=True):
        """Run the program with a script (list of steps); returns a Result. check: a failing
        step fails the test, with the screen at that moment. session_bus=False,
        system_bus=False: the program finds no such bus."""
        self.start()
        n = len(list(self.tmp.glob("script-*.json")))
        script = self.tmp / f"script-{n}.json"
        out = self.tmp / f"out-{n}.json"
        script.write_text(json.dumps({"params": params or {}, "steps": steps,
                                      "timeout": timeout * (5 if GCOV_DIR else 1)}))
        env = self.env()
        if not session_bus:
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={self.short}/no-bus"
        if not system_bus:
            env["DBUS_SYSTEM_BUS_ADDRESS"] = f"unix:path={self.short}/no-bus"
        r = subprocess.run([str(program.path), str(script), str(out)], env=env,
                           capture_output=True, text=True,
                           timeout=(timeout + 10) * (5 if GCOV_DIR else 1))
        data = json.loads(out.read_text()) if out.exists() else {}
        result = Result(data, r.stderr, r.returncode)
        if check:
            assert r.returncode == 0, (f"exit {r.returncode}: {result.failure}\n{r.stderr[-3000:]}"
                                       f"\nscreen:\n{result.screen.outline()}")
        return result

    def calls(self, name=None, member=None):
        """The calls the fake services got: dicts of bus, name, path, interface, member, args
        (for a property's Set: member "Set", args [interface, property, value])."""
        if not self.call_log.exists():
            return []
        out = []
        for line in self.call_log.read_text().splitlines():
            c = json.loads(line)
            if (name is None or c["name"] == name) and (member is None or c["member"] == member):
                out.append(c)
        return out

    def calls_after(self, method, nth=0, name=None, member=None):
        """The calls after the nth call of a FakeControl step (method: "Emit", "Own", …)."""
        all_ = self.calls()
        marks = [i for i, c in enumerate(all_) if c["name"] == "org.plinius.TestFake"
                 and c["member"] == method]
        rest = all_[marks[nth] + 1:]
        return [c for c in rest if (name is None or c["name"] == name)
                and (member is None or c["member"] == member)]

    def control(self, bus="system"):
        """Steps that make a fake service do something while the program runs."""
        return FakeControl(bus)

    def close(self):
        for p in reversed(self.procs):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
            for f in (p.stdout, p.stderr):
                if f:
                    f.close()
        shutil.rmtree(self.short, ignore_errors=True)


class FakeControl:
    """Script steps for the fake services (org.plinius.TestFake on each bus)."""

    def __init__(self, bus):
        self.bus = bus

    def _step(self, method, signature, args, label=None):
        step = {"op": "dbus", "bus": self.bus, "dest": "org.plinius.TestFake",
                "path": "/org/plinius/TestFake", "iface": "org.plinius.TestFake",
                "method": method, "signature": signature, "args": args}
        if label:
            step["label"] = label
        return step

    @staticmethod
    def _q(s):
        return json.dumps(s)

    def emit(self, path, interface, signal, args):
        """The object at path sends signal with args (GVariant text of the tuple)."""
        q = self._q
        return self._step("Emit", "(ssss)", f"({q(path)}, {q(interface)}, {q(signal)}, {q(args)})")

    def emit_raw(self, path, interface, signal, args):
        """Like emit, with arguments of any type (GVariant text of a tuple, types inferred)."""
        q = self._q
        return self._step("EmitRaw", "(ssss)", f"({q(path)}, {q(interface)}, {q(signal)}, {q(args)})")

    def set_property(self, path, interface, prop, value):
        """The service changes a property itself (PropertiesChanged follows)."""
        q = self._q
        return self._step("SetProperty", "(ssss)",
                          f"({q(path)}, {q(interface)}, {q(prop)}, {q(value)})")

    def set_reply(self, path, interface, method, reply):
        """From now on the method answers with reply (as in World.service)."""
        q = self._q
        return self._step("SetReply", "(ssss)",
                          f"({q(path)}, {q(interface)}, {q(method)}, {q(json.dumps(reply))})")

    def release(self, name):
        """The service leaves the bus (its name has no owner any more)."""
        return self._step("Release", "(s)", f"({self._q(name)},)")

    def own(self, name):
        """The service (back) on the bus."""
        return self._step("Own", "(s)", f"({self._q(name)},)")


@pytest.fixture
def gcc_world(gcc_session, tmp_path):
    world = World(gcc_session, tmp_path)
    yield world
    world.close()


# Steps, for the scripts' readability
def click(**sel):
    return {"op": "click", "target": sel}


def set_text(value, **sel):
    return {"op": "set_text", "target": sel, "text": value}


def enter(**sel):
    return {"op": "enter", "target": sel}


def apply(**sel):
    return {"op": "apply", "target": sel}


def set_active(active, **sel):
    return {"op": "set_active", "target": sel, "active": active}


def select(index=None, item=None, name=None, **sel):
    step = {"op": "select", "target": sel}
    if index is not None:
        step["index"] = index
    if item is not None:
        step["item"] = item
    if name is not None:
        step["name"] = name
    return step


def wait_for(timeout=5000, gone=False, **sel):
    return {"op": "wait_for", "target": sel, "timeout": timeout, "gone": gone}


def dump(label):
    return {"op": "dump", "label": label}


def settle(ms=50):
    return {"op": "settle", "ms": ms}


def push(tag):
    return {"op": "push", "tag": tag}


def back():
    return {"op": "back"}


def pop_to(tag):
    return {"op": "pop_to", "tag": tag}


def wait(ms):
    return {"op": "wait", "ms": ms}


def call(name, *args, label=None):
    return {"op": "call", "name": name, "args": list(args), "label": label or name}


def drag(points, **sel):
    """A finger drawn over the widget through points [(fx, fy), ...], fractions of its width and
    height (drag-begin, drag-update, drag-end on its GtkGestureDrag)."""
    return {"op": "drag", "target": sel, "points": [list(p) for p in points]}


__all__ = ["Unavailable", "prepared_tree", "gcc_session", "gcc_world", "build_panel",
           "sandboxed_copy", "interface_xml", "World", "Screen", "Result", "click", "set_text",
           "enter", "apply", "set_active", "select", "wait_for", "dump", "settle", "push", "back",
           "pop_to", "wait", "call", "drag", "uname_shim", "compile_resources", "GCC"]
