# Syntax of every versioned script and configuration file, with the checkers of the host.
# A checker that is not installed skips its files.

import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest

from conftest import REPO, git_files

FILES = [f for f in git_files(REPO) if f.is_file() and f.suffix != ".patch"
         and not f.is_symlink()]


def rel(f):
    return str(f.relative_to(REPO))


def head(f):
    try:
        with open(f, "rb") as fh:
            return fh.readline(200).decode("utf-8", "replace")
    except OSError:
        return ""


def kind(f):
    first = head(f)
    if f.suffix == ".py" or (first.startswith("#!") and "python" in first):
        return "python"
    if first.startswith("#!") and re.search(r"\b(ba)?sh\b", first):
        return "sh" if re.search(r"/sh\b|busybox", first) else "bash"
    if f.suffix == ".sh":
        return "bash"
    if f.suffix == ".install" or (f.suffix in (".hook", "") and f.parent.name == "pkgbuilds"):
        return "bash"
    if f.suffix == ".hook":
        # pacman hooks are ini files, mkinitcpio's run hooks are shell
        return "alpm-hook" if "[Trigger]" in f.read_text(errors="replace") else "sh"
    if f.suffix == ".rules":
        return "polkit-js" if "polkit." in f.read_text(errors="replace") else "udev"
    if f.name == "PKGBUILD":
        return "bash"
    if f.suffix == ".service" and "[D-BUS Service]" in f.read_text(errors="replace"):
        return "dbus-service"
    return {".lua": "lua", ".json": "json", ".xml": "xml", ".policy": "xml", ".svg": "xml",
            ".po": "po", ".desktop": "desktop", ".js": "js", ".service": "unit",
            ".socket": "unit", ".timer": "unit", ".path": "unit", ".mount": "unit",
            ".target": "unit"}.get(f.suffix)


BY_KIND = {}
for _f in FILES:
    BY_KIND.setdefault(kind(_f), []).append(_f)


def files(k):
    return [pytest.param(f, id=rel(f)) for f in BY_KIND.get(k, [])]


def need(tool):
    if shutil.which(tool) is None:
        pytest.skip(f"{tool} is not installed")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


@pytest.mark.parametrize("f", files("python"))
def test_python_compiles(f):
    compile(f.read_bytes(), str(f), "exec")


def test_python_no_undefined_names():
    """ruff's error classes only (syntax, undefined names, broken comparisons), not its style."""
    need("ruff")
    targets = [str(f) for f in BY_KIND.get("python", [])]
    r = run(["ruff", "check", "--no-cache", "--quiet", "--output-format", "concise",
             "--select", "E9,F63,F7,F82",
             # D-Bus signatures as annotations (dbus-fast): "o", "a{sv}" are no Python
             "--per-file-ignores", "tools/mms-standin.py:F821,F722", *targets], cwd=REPO)
    lines = [line for line in r.stdout.splitlines() if line and not line.startswith("Found ")]
    assert not lines, "\n".join(lines)


@pytest.mark.parametrize("f", files("bash"))
def test_bash_syntax(f):
    r = run(["bash", "-n", str(f)])
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("f", files("sh"))
def test_posix_sh_syntax(f):
    """Scripts for /bin/sh (in the initramfs that is busybox ash): dash when installed."""
    shell = "dash" if shutil.which("dash") else "bash"
    cmd = [shell, "-n", str(f)] if shell == "dash" else ["bash", "--posix", "-n", str(f)]
    r = run(cmd)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("f", files("lua"))
def test_lua_syntax(f):
    need("luac")
    r = run(["luac", "-p", str(f)])
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("f", files("json"))
def test_json(f):
    json.loads(f.read_text())


@pytest.mark.parametrize("f", files("xml"))
def test_xml_well_formed(f):
    ET.parse(f)


@pytest.mark.parametrize("f", files("js") + files("polkit-js"))
def test_javascript_syntax(f):
    need("node")
    # node --check wants a .js name; polkit rules are plain scripts
    r = run(["node", "--check", "-"], input=f.read_text())
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("f", files("po"))
def test_po_translations(f):
    """msgfmt -c: header, plural forms and format strings (%s, %d) that match the original."""
    need("msgfmt")
    r = run(["msgfmt", "-c", "--check-format", "-o", "/dev/null", str(f)])
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("f", files("desktop"))
def test_desktop_entry(f):
    need("desktop-file-validate")
    r = run(["desktop-file-validate", str(f)])
    errors = [line for line in r.stdout.splitlines() if ": error: " in line]
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize("f", files("udev"))
def test_udev_rules(f):
    need("udevadm")
    r = run(["udevadm", "verify", "--no-style", str(f)], env={"LC_ALL": "C", "PATH": "/usr/bin"})
    # Users and groups of the phone (aid_*, plinius-*) do not exist on the build host
    problems = [line for line in r.stderr.splitlines()
                if re.match(r".*:\d+ ", line) and "Failed to resolve" not in line]
    assert not problems, "\n".join(problems)


SECTIONS = {
    "unit": {"Unit", "Service", "Socket", "Timer", "Path", "Mount", "Install", "Automount",
             "Target"},
    "alpm-hook": {"Trigger", "Action"},
}


def ini_problems(f, sections, flags=()):
    problems = []
    current = None
    for n, line in enumerate(f.read_text().splitlines(), 1):
        s = line.strip()
        if not s or s.startswith(("#", ";")):
            continue
        m = re.fullmatch(r"\[([A-Za-z]+)\]", s)
        if m:
            current = m.group(1)
            if current not in sections:
                problems.append(f"{n}: unknown section [{current}]")
            continue
        if current is None:
            problems.append(f"{n}: line outside a section")
        elif not re.match(r"[A-Za-z][A-Za-z0-9]*\s*=", s) and not line[:1].isspace() \
                and not problems_continuation(f, n) and s not in flags:
            problems.append(f"{n}: not KEY=value: {s}")
    return problems


def problems_continuation(f, n):
    lines = f.read_text().splitlines()
    return n >= 2 and lines[n - 2].rstrip().endswith("\\")


@pytest.mark.parametrize("f", files("unit"))
def test_systemd_unit_structure(f):
    assert not ini_problems(f, SECTIONS["unit"])


def test_systemd_units_verify():
    """systemd-analyze verify on all units together: unknown keys, bad values, bad
    dependencies. Programs that only exist on the phone are not missing."""
    need("systemd-analyze")
    units = [str(f) for f in BY_KIND.get("unit", [])]
    r = run(["systemd-analyze", "verify", "--man=no", "--generators=no", *units],
            env={"LC_ALL": "C", "PATH": "/usr/bin", "SYSTEMD_LOG_LEVEL": "warning"})
    ignore = re.compile(
        r"is not executable|not found|No such file|Failed to (prepare|load|add)|"
        r"Unit .* not found|ignoring: Unknown (user|group)|Failed to resolve|"
        r"Requirement dependency|is not a valid unit|Unit configuration has fatal error|"
        r"Cannot add dependency job|Cannot find unit|Unit .* is masked|has a bad unit file|"
        r"Failed to create .*: Invalid argument|Command .* is not executable|"
        r"Neither a valid executable name nor an absolute path|Exited with status|"
        r"DynamicUser|BindPaths|ReadWritePaths|ConditionPathExists|"
        r"Ignoring unknown escape|Failed to set up mount namespacing|Failed to verify|"
        # var-lib-android-data.mount is installed as var-lib-android\x2ddata.mount
        r"var-lib-android-data\.mount: Where= setting doesn't match|"
        # routectld.service comes from mmsd4ofono's own build
        r"routectld\.service not loaded")
    problems = sorted({line for line in r.stderr.splitlines() if line and not ignore.search(line)})
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("f", files("dbus-service"))
def test_dbus_service_file(f):
    text = f.read_text()
    assert re.search(r"^\[D-BUS Service\]\s*$", text, re.M)
    name = re.search(r"^Name=(\S+)\s*$", text, re.M)
    assert name, "Name= missing"
    assert f.name == name.group(1) + ".service", "D-Bus wants the file named after the service"
    assert re.search(r"^(Exec=/\S+|SystemdService=\S+)", text, re.M), "Exec= or SystemdService="


@pytest.mark.parametrize("f", files("alpm-hook"))
def test_pacman_hook(f):
    text = f.read_text()
    assert not ini_problems(f, SECTIONS["alpm-hook"], flags=("NeedsTargets", "AbortOnFail"))
    for key in ("Operation", "Type", "Target", "When", "Exec"):
        assert re.search(rf"^{key}\s*=", text, re.M), f"{key} missing"
    assert re.search(r"^When\s*=\s*(PreTransaction|PostTransaction)\s*$", text, re.M)


APPARMOR = [f for f in FILES if f.parent.name == "plinius-apparmor"
            and f.name.startswith("plinius.")]


@pytest.mark.parametrize("f", [pytest.param(f, id=rel(f)) for f in APPARMOR])
def test_apparmor_profile(f):
    need("apparmor_parser")
    r = run(["apparmor_parser", "-Q", "-K", "--skip-cache", str(f)])
    assert r.returncode == 0, r.stderr


def test_every_file_kind_was_checked():
    """Sanity check of kind(): the main file kinds of the repository all have files."""
    for k in ("python", "bash", "sh", "unit", "dbus-service", "udev", "po", "xml", "desktop",
              "alpm-hook"):
        assert BY_KIND.get(k), k


def _shadowed_gettext(tree):
    """Functions that assign to "_" and also call _(): the assignment makes "_" local to the
    whole function, so the translation call fails (desk-network.py had it twice)."""
    import ast
    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stores = calls = False
        todo = list(ast.iter_child_nodes(fn))
        while todo:
            node = todo.pop()
            # Nested functions and comprehensions have scopes of their own
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef,
                                 ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                continue
            if isinstance(node, ast.Name) and node.id == "_" and isinstance(node.ctx, ast.Store):
                stores = True
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_"):
                calls = True
            todo.extend(ast.iter_child_nodes(node))
        if stores and calls:
            found.append(f"line {fn.lineno}: {getattr(fn, 'name', 'lambda')}")
    return found


@pytest.mark.parametrize("f", files("python"))
def test_gettext_not_shadowed(f):
    import ast
    assert not _shadowed_gettext(ast.parse(f.read_bytes(), str(f)))
