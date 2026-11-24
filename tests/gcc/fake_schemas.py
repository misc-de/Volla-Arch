# GSettings schema directories without some of our schemas, for the panel tests in tests/gcc that
# check what a panel does when the package owning a schema is not installed
# (test_gcc_device.py, test_gcc_buttons.py, test_gcc_assistant.py).

import shutil
import subprocess
from pathlib import Path


def schemas_without(session, directory, *schema_ids):
    """A compiled schema directory with every schema of the session but those whose file is
    named <id>.gschema.xml; set it as GSETTINGS_SCHEMA_DIR (World.extra_env)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    left_out = {f"{i}.gschema.xml" for i in schema_ids}
    for f in session.schemas.iterdir():
        if f.suffix == ".xml" and f.name not in left_out:
            shutil.copy(f, directory / f.name)
    subprocess.run(["glib-compile-schemas", str(directory)], check=True, capture_output=True)
    for i in schema_ids:
        assert not (directory / f"{i}.gschema.xml").exists()
    return directory


def no_schemas(directory):
    """A schema directory without any schema: GLib has no schema source at all then (the test's
    XDG_DATA_DIRS holds none either)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


__all__ = ["schemas_without", "no_schemas"]
