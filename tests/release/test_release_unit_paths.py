"""tools/unit-paths.py: the paths of a service's sandbox that only the image can provide.

A unit tree as image-check.sh dumps it from the image lies in tmp_path; the script runs on it as
a program, as image-check.sh runs it, and its functions are used for the finer rules.
"""
import importlib.util
import subprocess
import sys

import pytest

from conftest import TOOLS

SCRIPT = TOOLS / "unit-paths.py"
spec = importlib.util.spec_from_file_location("unit_paths", SCRIPT)
unit_paths = importlib.util.module_from_spec(spec)
spec.loader.exec_module(unit_paths)


def tree(root, files):
    for path, text in files.items():
        f = root / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return root


def run(root):
    return subprocess.run([sys.executable, "-I", str(SCRIPT), str(root)], capture_output=True,
                          text=True)


def listed(root):
    """What the script prints, from its functions in this process"""
    return "".join(f"{name} {path}\n" for name, path in unit_paths.missing(root))


SYS = "usr/lib/systemd/system"


def test_a_path_only_another_service_makes_is_listed(tmp_path):
    root = tree(tmp_path, {
        f"{SYS}/bluebinder.service": "[Service]\nExecStart=/usr/sbin/bluebinder\n",
        f"{SYS}/bluebinder.service.d/10-sandbox.conf":
            "[Service]\nProtectSystem=strict\nReadWritePaths=/var/lib/bluetooth\n",
        f"{SYS}/bluetooth.service": "[Service]\nStateDirectory=bluetooth\n",
    })
    r = run(root)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "bluebinder.service /var/lib/bluetooth\n"


def test_what_the_phone_makes_itself_is_not_listed(tmp_path):
    root = tree(tmp_path, {
        f"{SYS}/a.service": "[Service]\nStateDirectory=a/sub\nCacheDirectory=c\nLogsDirectory=l\n"
                            "ConfigurationDirectory=e\nReadWritePaths=/var/lib/a/sub/x /var/cache/c "
                            "/var/log/l /etc/e\n",
        f"{SYS}/b.service": "[Service]\nReadWritePaths=-/var/lib/maybe /run/b /tmp/b %S/b\n"
                            "InaccessiblePaths=/proc/x /sys/y\n",
        f"{SYS}/c.service": "[Service]\nReadWritePaths=/var/lib/nfcd/ /var/spool/q\n",
        "usr/lib/tmpfiles.d/nfcd.conf": "# made at boot\nd /var/lib/nfcd 0700 nfc nfc -\n",
        "etc/tmpfiles.d/q.conf": "D /var/spool/q 0755 - - -\n",
        f"{SYS}/d.service": "[Unit]\nConditionPathIsDirectory=/etc/pacman.d/gnupg/\n"
                            "[Service]\nReadWritePaths=/etc/pacman.d/gnupg\n",
    })
    assert listed(root) == ""


def test_negated_and_triggering_conditions_do_not_guard(tmp_path):
    root = tree(tmp_path, {
        f"{SYS}/a.service": "[Unit]\nConditionPathExists=!/var/lib/a\n"
                            "[Service]\nReadWritePaths=/var/lib/a\n",
        f"{SYS}/b.service": "[Unit]\nConditionPathExists=|/var/lib/b\nConditionPathExists=|/etc/x\n"
                            "[Service]\nReadWritePaths=/var/lib/b\n",
    })
    assert listed(root) == "a.service /var/lib/a\nb.service /var/lib/b\n"


def test_every_kind_of_sandbox_path_counts(tmp_path):
    root = tree(tmp_path, {
        f"{SYS}/a.service": "[Service]\n"
                            "ReadOnlyPaths=/usr/ro\nInaccessiblePaths=/etc/no\nExecPaths=/opt/x\n"
                            "NoExecPaths=/srv/y\nBindPaths=/var/src:/dst -/var/opt:/o\n"
                            "BindReadOnlyPaths=+/var/plus:/p:rbind\n",
    })
    assert listed(root).split("\n")[:-1] == [
        f"a.service {p}" for p in ("/etc/no", "/opt/x", "/srv/y", "/usr/ro", "/var/plus", "/var/src")]


def test_continuations_quotes_comments_and_resets(tmp_path):
    root = tree(tmp_path, {
        f"{SYS}/a.service": "# ReadWritePaths=/var/lib/comment\n[Service]\n"
                            "ReadWritePaths=/var/lib/one \\\n    \"/var/lib/two words\"\n"
                            "; ReadWritePaths=/var/lib/semicolon\n",
        # An empty assignment in a later drop-in clears the list, a new one starts it again
        f"{SYS}/b.service": "[Service]\nReadWritePaths=/var/lib/old\n",
        f"{SYS}/b.service.d/20-reset.conf": "[Service]\nReadWritePaths=\nReadWritePaths=/var/lib/new\n",
        # Only [Service] holds the sandbox
        f"{SYS}/c.service": "[Install]\nReadWritePaths=/var/lib/install\n",
        # A continuation at the very end of the file
        f"{SYS}/d.service": "[Service]\nReadWritePaths=/var/lib/end \\",
    })
    assert listed(root) == ("a.service /var/lib/one\na.service /var/lib/two words\n"
                                "b.service /var/lib/new\nd.service /var/lib/end\n")


def test_a_drop_in_in_etc_replaces_the_one_of_the_same_name(tmp_path):
    root = tree(tmp_path, {
        f"{SYS}/a.service": "[Service]\nExecStart=/x\n",
        f"{SYS}/a.service.d/sandbox.conf": "[Service]\nReadWritePaths=/var/lib/usr\n",
        "etc/systemd/system/a.service.d/sandbox.conf": "[Service]\nReadWritePaths=/var/lib/etc\n",
        # A drop-in without its unit, symlinks (masks, aliases, .wants/) and other unit types
        "etc/systemd/system/gone.service.d/x.conf": "[Service]\nReadWritePaths=/var/lib/gone\n",
        f"{SYS}/x.socket": "[Service]\nReadWritePaths=/var/lib/socket\n",
    })
    (tmp_path / "etc/systemd/system/masked.service").symlink_to("/dev/null")
    assert listed(root) == "a.service /var/lib/etc\n"


def test_user_units_get_no_system_directories(tmp_path):
    root = tree(tmp_path, {
        "usr/lib/systemd/user/a.service": "[Service]\nStateDirectory=a\nReadWritePaths=/var/lib/a %h/x\n",
    })
    # StateDirectory= of a user unit is under ~/.local/state, not /var/lib
    assert listed(root) == "a.service /var/lib/a\n"


def test_an_unclosed_quote_is_read_word_by_word(tmp_path):
    root = tree(tmp_path, {f"{SYS}/a.service": "[Service]\nReadWritePaths=/var/lib/a \"/var/lib/b\n"})
    # The words still count; the broken one is no absolute path
    assert listed(root) == "a.service /var/lib/a\n"


def test_a_missing_tree_lists_nothing(tmp_path):
    r = run(tmp_path / "none")
    assert r.returncode == 0 and r.stdout == ""


def test_usage(tmp_path):
    r = subprocess.run([sys.executable, "-I", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 1 and "Usage: tools/unit-paths.py ROOT" in r.stderr


@pytest.mark.parametrize("line", ["", "   ", "#d /var/lib/x", "x"])
def test_tmpfiles_lines_that_make_nothing(tmp_path, line):
    tree(tmp_path, {"usr/lib/tmpfiles.d/a.conf": line + "\nz /var/lib/z\n"})
    assert unit_paths.tmpfiles_made(tmp_path) == set()


def test_main_in_process(tmp_path, capsys):
    tree(tmp_path, {f"{SYS}/a.service": "[Service]\nReadWritePaths=/var/lib/a\n"})
    assert unit_paths.main(["unit-paths.py", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "a.service /var/lib/a\n"
    assert unit_paths.main(["unit-paths.py"]) == 1
