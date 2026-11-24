# Runs a program the way `python3 <program>` would, i.e. with __name__ == "__main__", inside the
# test process, with gi.repository namespaces replaced (toolkits, buses, the main loop) for the
# run. The program's own `if __name__ == "__main__":` block runs, nothing opens a window or a bus.

import runpy
import sys

import gi
import pytest


def run_as_main(path, argv=(), **namespaces):
    """namespaces: gi.repository names to replace (Gtk=..., Gio=..., GLib=...). Returns the exit
    code given to sys.exit(), or None when the program returned normally."""
    with pytest.MonkeyPatch.context() as mp:
        real = gi.require_version
        mp.setattr(gi, "require_version",
                   lambda ns, version: None if ns in namespaces else real(ns, version))
        repository = sys.modules["gi.repository"]
        for name, obj in namespaces.items():
            mp.setattr(repository, name, obj, raising=False)
            mp.setitem(sys.modules, "gi.repository." + name, obj)
        mp.setattr(sys, "argv", [str(path), *argv])
        try:
            runpy.run_path(str(path), run_name="__main__")
        except SystemExit as e:
            return e.code
        return None
