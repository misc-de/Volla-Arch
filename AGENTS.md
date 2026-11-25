# AGENTS.md – guide for AI coding agents

Arch Linux ARM with Phosh for the Volla Phone Plinius (codename `ansuz`, MediaTek MT6878). The
phone's hardware is reached through Halium 14: the Android vendor drivers run in an LXC container
and are used through libhybris (GPU, audio, camera, NPU) and libgbinder (modem, Bluetooth, GPS,
NFC, fingerprint, sensors). Everything else comes unchanged from Arch Linux ARM (ALARM). This
repository holds our own packages, the image builder and the tools around them. README.md is the
short overview shown on GitHub; TECHNICAL.md has the full picture: state, architecture, licenses.

## Make an image for your own phone

Host: x86_64 Linux, no root needed (user namespaces + qemu-user). Packages: `qemu-user-static-binfmt`
(the qemu-aarch64 binfmt entry needs the `F` flag), `android-tools` (`unpack_bootimg`),
`e2fsprogs` (`debugfs`), `erofs-utils`, `pacman` (`repo-add`), `gnupg`, `curl`, `libarchive`. Your
user needs a range in `/etc/subuid` and `/etc/subgid`. Plan for about 30 GB in `build/`.

```
tools/chroot-init.sh       # aarch64 build chroot from the ALARM tarball → build/chroot
tools/ut-fetch.sh          # UT build 04112026 from abm.volla.tech → build/ut/04112026 (5 GB download)
tools/repo-fetch.sh        # our ~100 signed packages from plinius.cais.de → build/repo/local
image/mkimage.sh -a build/ut/04112026/android/android-rootfs.img -P -k ~/.ssh/id_ed25519.pub \
    -r https://plinius.cais.de/repo/aarch64 -o build/my-plinius.img
```

- `-P` asks for the PIN of the user `plinius` (4 to 12 digits, stored as a yescrypt hash); without
  it the password stays locked and the lock screen cannot be passed. `-k` gives root your SSH key;
  without it nobody can log in over SSH. `-r` keeps the phone receiving updates of our packages.
  The image holds `/etc/shadow` and is readable only by you (0600).
- The image is written to the phone with Plinius Boot, a separate PC app (microSD card by default,
  internal storage optional). Never write to the phone's partitions without the owner's consent.
- `tools/ut-build.sha256` pins every file of that UT build (boot images, Android images).
  `ut-fetch.sh` checks each file against it before use, files already there included,
  `boot-files.sh` and `mkimage.sh` check what they are given; a file that differs stops the run.
  `tools/boot-files.sha256` pins kernel and ramdisks besides: `plinius-kmod` is built against
  exactly that kernel's symbol CRCs. A new UT build needs a new `plinius-kmod` and both files anew.
- `tools/repo-fetch.sh` checks every file against `pkgbuilds/plinius-keyring`, the channel's
  signed stamp as a phone does (channel, at most 30 days old, SHA-256 of the database) and each
  package, also one already in `build/repo/local`, against name, version and SHA-256 of its
  database entry. `CHANNEL=beta` takes the beta channel instead of stable.
- `mkimage.sh` installs ALARM packages at the versions approved by `plinius-freigabe`. If ALARM has
  moved past an approved version, the run stops with a version conflict; that needs a new approval
  (`tools/candidate.sh`, `tools/freigabe.py`, rebuild `plinius-freigabe`), not a forced install.

## Repository map

| Path | Content |
|---|---|
| `pkgbuilds/<pkg>/` | one PKGBUILD per package with its patches and own sources (C, Python, shell) |
| `pkgbuilds/*-plinius/` | patched upstream apps (phoc, Phosh, GNOME Settings, Snapshot, Stevia, …), rebased on each upstream release |
| `pkgbuilds/device-volla-ansuz/` | device configuration: udev, systemd units, firmware paths |
| `image/` | `mkimage.sh` (the image), QEMU preview and its tests |
| `tools/` | build chroot, package builds, boot files, signing, publishing, probes; `lib.sh` holds the shared paths |
| `tests/` | host-side tests (pytest, C harnesses); `tests/README.md` explains the areas |
| `assets/` | diagrams of TECHNICAL.md |
| `build/` | everything generated: chroots, caches, repos, images (not versioned) |

## Building and testing packages

```
tools/build.sh <pkg>          # fresh overlay of the chroot, result → build/repo/local
tools/devtree.sh <pkg> setup  # persistent build tree for quick incremental builds
tools/run-tests.sh            # all host tests, about 10 minutes; tools/run-tests.sh tests/boot for one area
tools/run-tests.sh --coverage # line coverage of Python and C in build/coverage/
```

`tools/publish.sh` puts a package into beta only through the test gate (`tools/test-gate.py`):
it must be built from the version its PKGBUILD has in the tree, and its directory must be
unchanged since the last full green run of `tools/run-tests.sh` (no paths, no pytest arguments;
a red full run takes every package back). So after the last change to a package: build it, run
the full suite, then publish. `--untested` overrides the gate for an emergency and says so.

The phone is reachable over USB networking at `10.15.19.82` (the PC side is `10.15.19.100`), SSH
with keys only. `tools/phone-check.py` checks its everyday functions live over that link (SIM,
network, calls, SMS, mobile data, Wi-Fi, Bluetooth, GPS, sensors, cameras, audio, OpenGL, Vulkan
and more, about 25 s): it only reads and scans, and calls, sends, pairs, plays and lights nothing
(OpenGL draws off screen). Run it after installing packages on the phone; `--host` takes
another address, `--list` shows the checks.

## Conventions

- Code, comments, commit messages and user-visible strings are in English. Strings shown on the
  phone are translated through `.po` files in de, es, fr, it, nl, pl, pt and ru; add new strings
  to all of them.
- Every change to a package bumps `pkgrel` (or `pkgver`) in its PKGBUILD and updates the
  checksums. Patches are `git format-patch` files, numbered, applied in `prepare()`.
- Follow the current ALARM version of every package we patch; never pin an old one.
- Tests come with the change, in `tests/`; aim for at least 98 % line coverage with tests of real
  behaviour.
- No personal data in code, tests, comments or commit messages: no device models or hostnames of
  your own, no home IP addresses, no phone numbers, IMEI, EID or serials. Use neutral examples
  (`192.0.2.x`, `printer.local`).
- README.md and TECHNICAL.md describe the current state, not how it was reached.
- Shell scripts start with `set -euo pipefail` and source `tools/lib.sh` for paths and helpers.
