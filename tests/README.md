# Host-side tests

Tests that run on the build host (x86_64) in a few seconds, without building a package and
without the phone: `tools/run-tests.sh` (all), `tools/run-tests.sh tests/boot` (one area),
`tools/run-tests.sh --coverage` (line coverage in `build/coverage/`: `report.txt` for the Python
code, `c-report.txt` for the C code the tests compile, counted by gcov with `-O0`; it also lists
the versioned C files no test compiles). `--jobs N` runs N pytest processes side by side (default: half the CPUs), each on a group of test files of about the same running time (`tools/test-shards.py`, times of the last run in `build/test-durations.json`); the whole suite takes about 9 minutes that way, 11 with `--coverage`.

Aim: at least 98 % of the lines, reached with tests of real behaviour, not with tests that only
run lines; what cannot run on the host (main loops, hardware, BPF loading) is named, not hidden
behind blanket `pragma: no cover`.

| Directory | What |
|---|---|
| `lint/` | every PKGBUILD against its files (sources, checksums, forgotten files); syntax of all scripts and configuration (Python, shell, Lua, JSON, XML, po, desktop, udev, systemd, D-Bus, pacman hooks, AppArmor) |
| `patches/` | every package's `prepare()` from the source cache `build/cache/sources`, without network: all patches apply to the pinned commit, none only with fuzz (slow, about 20 s; skipped for sources missing in the cache) |
| `telephony/` | ofono2mm as built (commit + patches): polkit actions per call, who may make an emergency call, no method or writable property outside the table |
| `boot/` | boot images, initramfs hook, encryption, Halium ramdisk |
| `release/` | kernel install, boot check, rollback, the tools for releases (`tools/`) |
| `apps/` | Python programs with network or package access (Software, CVE list, Widevine, accounts) |
| `desktop/` | Python UI programs: their logic without a display |
| `system/` | shell scripts and Lua scripts that change the system |
| `daemons-security/`, `daemons-hw/` | C programs: their parsing and decision logic, compiled for the host |
| `gcc/` | our GNOME Control Center panel files, each compiled into a small GTK program and driven headless against fake D-Bus services (see below) |

The packages' own `check()` functions (`test-*.c` next to a PKGBUILD) stay; the tests here also
compile and run them.

## Rules

- **Nothing reaches the running system.** `conftest.py` removes `DISPLAY`/`WAYLAND_DISPLAY` and points both
  D-Bus addresses at nothing for the whole run; a test that needs a display or a bus starts its own.
  Beyond that: Programs a script calls come from stubs
  (`fake_bin` in `conftest.py`, preferably `env(isolated=True)`), paths it writes to point into
  `tmp_path`. A script with fixed absolute paths is tested as a copy in `tmp_path` with the
  paths replaced, or only its functions (`bash_functions`). No root, no network, no `adb`/`ssh`.
  `system_helpers.rewritten_copy` refuses a copy that still names `/etc/`, `/var/`, `/home/` and
  the other places of persistent state outside a comment (`allow_left` for what is only read);
  `dhw_harness.sandboxed_source` does the same for C string literals.
- **No package builds.** Small C test programs are compiled for the host with `compile_c`;
  a library missing on the host skips the test.
- **A found bug stays visible:** the test states the right behaviour and is marked
  `@pytest.mark.xfail(strict=True, reason=...)`, so it fails once the bug is fixed and the mark
  can go.
- Test file names are unique across all directories (pytest imports them by name).
- English, like the rest of the code.

## Helpers (`conftest.py`)

- `load_script(path)` imports a Python script, also one without `.py`, without running its
  `main`.
- `fake_bin.stub(name, script)` writes a stand-in program; `fake_bin.calls(name)` returns the
  argument lists it was called with; `fake_bin.env(isolated=True)` is an environment whose PATH
  has only the stubs and basic tools.
- `bash_functions(script, *names)` returns the definitions of a script's functions, to source
  them without running its top level.
- `compile_c(out, sources, flags=..., pkgs=...)` compiles for the host, `pkgs` via pkg-config;
  with `--coverage` it adds gcov's counters. A C copy made by `sandboxed_source` starts with a
  `#line` naming the file in `pkgbuilds/` (when no replacement moved a line), so its coverage
  counts for that file.
- `git_files(path)`, `REPO`, `PKG`, `TOOLS`.

## GNOME Control Center panels (`gcc/gcc_harness.py`)

Our panel files in `pkgbuilds/gnome-control-center-plinius/` are tested one at a time, without
building GNOME Settings:

- **Source tree:** GNOME Settings at the pinned commit with our `prepare()` applied, made once in
  `build/test-cache/gcc/<key>` (key: commit, PKGBUILD, every local source). Without the pinned
  commit in the mirrors the newest commit of `gnome-51` stands in (`build/test-cache/gcc/<key>/commit`
  says which); without a mirror the tests skip.
- **Program:** `build_panel(session, dir, name, files, glue, replacements=...)` compiles
  `gcc/c/gcc_driver.c`, a few lines of glue in C that call the file's entry point
  (`gcc_test_setup`), the tree's `shell/cc-panel.c` and `panels/common/cc-list-row.c`, and the panel
  file as a copy whose paths into the system are replaced by paths in the program's root
  (`{root}`; leftovers are refused). The copy starts with `#line` naming the file in
  `pkgbuilds/`, so gcov counts it there. Window, application and shell model are stand-ins
  (`gcc/c/stubs/`); the two Blueprint templates the shell needs are in `gcc/ui/` as GtkBuilder XML.
  A panel file's own templates are written by hand into `gcc/ui/` too (a test compiles them with
  `compile_resources` and passes them to `build_panel(..., sources=[...])`);
  `gcc/test_gcc_blp_ui_match.py` checks each `.ui` against its `.blp` (classes and ids in order,
  signals, style classes, translatable texts, the C file's template children and callbacks).
- **Display:** `gtk4-broadwayd` on unix sockets only (no TCP port), one per session, with
  `gcc/gcc_broadway_client.py` as the browser that answers each frame (without one GTK's frame
  clock stops after the first frame and nothing is laid out again).
- **World per test** (`gcc_world` fixture): HOME, XDG directories and XDG_DATA_DIRS in tmp_path,
  GSettings in memory, programs from `gcc_world.fake_bin`, a private session bus and a private
  "system" bus (DBUS_SYSTEM_BUS_ADDRESS) with `gcc/gcc_fakebus.py` serving the services a test
  describes with `gcc_world.service(bus, name, path, xml, properties=..., replies=...)`; the
  interfaces come from the services' C sources (`interface_xml`). Every call is recorded
  (`gcc_world.calls(name, member)`); `gcc_world.control()` gives steps that make a service emit a
  signal, change a property or a reply, leave or come back. Links open through a fakeable portal.
- **More fakes:** `gcc/fake_nm.py` is a NetworkManager for libnm's NMClient on the "system" bus
  (manager, devices, saved connections, activations; `FakeNM(gcc_world)`), and
  `gcc/fake_file_chooser.py` the file chooser portal GtkFileDialog asks (`FileChooserPortal`);
  both log into `gcc_world.calls`. `gcc/fake_schemas.py` makes schema directories without some of
  our schemas (a package not installed). A test that needs GNOME's own shell model (the panel
  list's GtkListStore, e.g. for the hubs) compiles `shell/cc-shell-model.c` and
  `panels/common/cc-util.c` with `-DGCC_REAL_SHELL_MODEL` (`test_gcc_hubs.py`).
- **Script:** `gcc_world.run(program, steps)` runs the program with steps (`click`, `set_text`,
  `enter`, `apply`, `set_active`, `select`, `push`, `back`, `wait_for`, `settle`, `dump`, `call`,
  D-Bus and GSettings steps); widgets are found by type, shown text, subtitle, tooltip, tag,
  `sensitive` or `within` another one. `dump(label)` records what is on screen (mapped widgets with their text,
  state and style classes) for the test's assertions; warnings and criticals are recorded too. At
  the end the window is destroyed, so a crash while freeing fails the test.
- Missing tools (`gtk4-broadwayd`, `dbus-daemon`, `glib-compile-resources`, PyGObject, a library)
  skip the tests. The session's setup takes about 3 s, a test 0.3 to 1 s.
- **Programs and the network:** `gcc/fake_programs.py` writes stand-ins (pacman, flatpak, pkexec,
  plinius-pkg …) that log their arguments and answer by rules; `gcc/fake_http.py` is a web server
  on 127.0.0.1. `test_gcc_software.py` moves AppStream to the test's catalog files and the
  screenshots' https addresses to that server (a header forced in with `-include`), so nothing
  reaches the host's catalog or the network. `LocalWeb(host)` serves on another loopback address
  (127.0.0.2, …); `test_gcc_webapps.py` lets the panel see such a server at a public or private
  address of the test's choice. Decoding images goes through glycin, which needs its loaders and
  shared-mime-info in XDG_DATA_DIRS and `bwrap` in PATH (that test links the host's).
- **Generated headers:** what meson makes with tools the host lacks is written by hand, e.g.
  `gcc/c/number-row/cc-number-row-enums.[ch]` (glib-mkenums), with a test that it matches the
  enums of the tree's header (`test_gcc_display.py`).

