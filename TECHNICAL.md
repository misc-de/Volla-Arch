# Volla Plinius – Arch Linux ARM + Phosh via Halium

Goal: run a native **Arch Linux ARM** with **Phosh** as the user interface on the
**Volla Phone Plinius** (codename `ansuz`). The hardware is reached through **Halium 14**
(Android vendor drivers in an LXC container, accessed via libhybris/libgbinder).

## Key facts

| | |
|---|---|
| Device | Volla Phone Plinius / Plinius Plus (`ansuz`, `ansuz-plus`) |
| SoC | MediaTek MT6878 (Dimensity 7300), GPU Mali-G615, NPU MediaTek APU |
| Halium | 14 (Android 15 vendor according to the UT device page) |
| Kernel | own `plinius-kernel` 6.1.193: UT's Halium kernel (`android14-6.1-halium`, 6.1.145) merged with Google's ACK `android14-6.1` and linux-6.1.y, with the interface of the prebuilt MediaTek modules kept (2560 symbol CRCs checked at each build); boot header v4, separate `init_boot` partition |
| Kernel support | Google ACK `android14-6.1` until **2029-07-01**, kernel.org 6.1 LTS until Dec 2027 (planned) |
| Reference port | Ubuntu Touch 24.04, official reference port, largely usable day to day |
| Build host | Manjaro x86_64 with `qemu-user-static-binfmt` (aarch64 chroot without Docker) |

## Basic decisions

1. **Arch instead of Droidian, always the latest versions.** The base system and the desktop come
   unchanged and current from ALARM. Own packages only where the phone requires them: the
   hardware layer (libhybris, Android container, modem via binder, audio and camera bridges to
   PipeWire, GBM on gralloc for wlroots 0.20 …) and patched builds of a few apps (phoc, Phosh,
   GNOME Settings, Snapshot, Stevia). Those follow the version in ALARM and rebase on each new
   upstream release.
2. **Own rootfs and own kernel.** Firmware and the vendor modules come unchanged from the
   Ubuntu Touch port; `vendor_boot` loses `systempart=` from its command line. The Halium ramdisk is
   UT's with our LUKS step added (`plinius-crypt`). The kernel is our own (`plinius-kernel`): UT's
   Halium kernel on the current 6.1 stable release, delivered as a normal package update that
   becomes the default at once and keeps the previous kernel as a fallback in the boot menu. What
   the kernel lacks (backlight device, userspace LEDs) comes as out-of-tree modules (`plinius-kmod`).
3. **SD card by default, internal storage as an option.** The installer Plinius Boot, a separate
   project, puts Arch on a microSD card next to the existing system, which the boot menu then
   offers. Optionally Arch replaces Volla OS on the internal storage: the system lives in
   `rootfs.img` (40 GiB) on an ext4 `userdata`, and `boot`, `vendor_boot` and `init_boot` of the
   active slot come from the image. In both cases the bootloader, the partition table, `super` and
   `vbmeta` stay as they are.
4. **Ubuntu Touch is the reference.** Whatever runs there is studied (services, configs,
   packages) and carried over to Arch. Going back to Volla OS takes the backups made with Plinius
   Boot or Volla's public OS package. Restoring to internal storage is untested.

## State

Arch runs from the internal storage of one Plinius Plus, in place of Volla OS, as its owner's daily
phone. **It is not ready for anyone else:** the data partition with the system on it is encrypted
(LUKS2, password at boot), but the bootloader stays unlocked, so nothing verifies what starts;
user data has no backup, and an emergency call has never gone over a real network. There is no
image to download yet.

### Works

- **Display and graphics:** Phosh and apps on the GPU with OpenGL ES 3.2 and EGL 1.5, Vulkan 1.3 for apps and games, desktop OpenGL 2.1 for programs that need it (`gl4es <program>`), WebGL in Firefox, display at 60/90/120 Hz, night light, brightness, always-on display
- **Calls and messages:** outgoing and incoming calls over VoLTE through the earpiece, the speaker,
  a Bluetooth headset or a car kit; SMS; cell broadcast warnings
- **Mobile network:** mobile data
- **Connections:** Wi-Fi; hotspot, also sharing mobile data; NFC tags and cards; USB file sharing
  and network; printing
- **Audio:** speaker, headphones and microphone
- **Camera:** all four cameras in Snapshot with noise reduction: 64 MP main camera, 8 MP, 2 MP macro and 32 MP front camera; flashlight
- **Unlocking:** fingerprint under the display; PIN or pattern; biometrics only after the first PIN
- **Security and privacy:** encryption of the data partition with a password at boot; a DNS filter
  against ads, trackers and malware with rules per app
- **Location and sensors:** GPS outdoors; rotation, light, proximity, compass, wake on pick up;
  vibration
- **Battery:** charging limit; wireless charging with Qi
- **System and updates:** apps and updates from our own signed repository; a self-test after each
  boot with a way back from the last update; kernel updates as a package with the previous kernel
  as a fallback; restarting the UI from the power menu; compressed memory (zram) and a swap file
  as a reserve when memory runs short
- **Everyday use:** search from the notch across apps; offline speech to text in the on-screen
  keyboard; online accounts with Nextcloud contacts, calendars and files; a desktop with the
  phone as its computer, on a PC over VNC (USB or Wi-Fi) or streamed to a TV, where running apps
  switch seamlessly between the phone and the desktop view

### Partly working

| Function | What works | What is missing |
|---|---|---|
| Standby, display off | 82 to 91 % of the time asleep, about 2 % battery per hour | what the sleeping phone draws is not broken down |
| eSIM | profiles on the chip in Settings | moving an eSIM contract to the phone with guidance not tested as a whole |
| Face unlock | unlocking on the phone's own NPU, entirely local | no liveness check, a photo may unlock |
| App security | Access Control in Settings limits what each app may reach | not finished, not every way to a device or service is covered yet |
| Echo cancellation in calls | on during calls | the voice still varies a little |
| Camera | photos with noise reduction | motion and daylight still to be judged |

### Built, test on the device pending

- media keys of a car kit or headset
- calls and SMS while the phone sleeps
- headphones on USB-C
- casting to TVs and speakers
- VPN connections
- camera flash
- pogo pin accessories
- emergency calls
- Advanced Mobile Location (AML): the position of an emergency call by SMS, nothing is sent until
  a country's destination is known (Germany's is not public)
- web apps from Settings
- the DNS filter for clients of the hotspot and of USB
- Bluetooth earbuds dialled back when the display comes on
- mobile data that stays connected without internet after a cell change reconnects by itself
  (plinius-datawatch)
- mobile data from the first start: a new SIM gets the access point oFono provisioned for it
  (by network and the SIM's provider name), with roaming off, once per SIM (plinius-mobile-data)
- the kernel log in the journal without the vendor drivers' status lines: a sixth of the lines remain
  (plinius-kmsg; all of them stay in dmesg)

### Open

- Wi-Fi calling
- assisted GPS
- an image for download with a first start for strangers

## Compared to other systems

How this project relates to the other systems a Plinius owner might consider. The other columns
describe those projects as far as we know them in November 2026, not from tests on this phone;
corrections are welcome.

| | This project | Ubuntu Touch | Droidian | Nura (formerly postmarketOS) | Volla OS |
|---|---|---|---|---|---|
| Base | Arch Linux ARM, rolling | Ubuntu 24.04 | Debian | Alpine | Android (AOSP) |
| Hardware access | Halium 14: vendor drivers via libhybris/libgbinder | Halium | Halium | mainline kernel | native Android |
| On the Plinius | daily phone of its owner, one device | official port, stable channel 24.04-1.x | no port (other Volla phones have one) | no port; mainline for the MT6878 is at an early stage (no GPU, Wi-Fi, modem, audio) | factory system |
| User interface | Phosh (GTK4/libadwaita apps) | Lomiri | Phosh | Phosh, Plasma Mobile, Sxmo, … | Volla Launcher (Springboard) |
| Software updates | ALARM unchanged plus own packages, follows each upstream release | image-based OTA | apt | apk | OTA |
| System | writable, pacman, self-test after boot, way back from the last update | read-only image | writable | writable | read-only image |
| Apps | Arch packages, own signed repository, Flatpak | OpenStore (Click), Libertine for desktop apps | Debian packages, Flatpak | Alpine packages, Flatpak | Android apps |
| Encryption | LUKS2 for the data partition with the system, password at boot; bootloader unlocked | user data with fscrypt since 24.04-1.0, enabled per device | optional, LUKS | optional, LUKS (pmbootstrap, not in prebuilt images) | yes |
| Ready for other users | **no** (see State) | yes | not on the Plinius | not on the Plinius | yes |

**What this project adds:** current upstream software on a current MediaTek phone that mainline
Linux does not reach yet; GPU, all four cameras, fingerprint, face unlock on the phone's own NPU, VoLTE and cell broadcast
through the vendor drivers; settings pages of its own (software and updates, device security, USB,
hardware, DNS filter, web apps); offline speech to text in the keyboard; an always-on display; a
desktop on a PC or a TV; a kernel on the current 6.1 stable release instead of the vendor's; and figures
that are measured rather than claimed (standby, start times, currents).

**Where the others are ahead:** Ubuntu Touch and Volla OS are finished systems for everyone, with
backups, a verified boot chain and many users. This project is one person's phone, carries a large
set of its own patches that have not been submitted upstream yet, and depends on the Ubuntu Touch
port for the kernel's base, the Halium ramdisk and firmware.

## Architecture

### Boot flow

On an internal installation, kernel (`plinius-kernel`) and Halium ramdisk (UT's, with the
`plinius-crypt` step added) lie in the active slot of the internal storage. The ramdisk asks for the password, opens `userdata` (LUKS2) as
`plinius-data` and, without `systempart=` on the command line, finds the system in `rootfs.img`
there; it mounts it, the data partition and the Android image, then hands over to systemd. With an
SD card in the phone, the bootloader shows its boot menu first; it also carries the previous kernel
as a fallback, trial starts of a new ramdisk and a rescue system. An installation on the SD card
starts from this menu: kernel and ramdisk come from the card. The system lies as `rootfs.img` on the card's data partition,
as on the internal storage, so that the encryption covers it too; the entry names only that partition
(`datapart=`), and plinius-bootcheck judges the entry. Installations from older images keep their
system on a plain partition of its own (`systempart=`).

![Boot flow from the bootloader to Phosh](assets/boot-flow.svg)

### Layers

Each row shows which layers a function passes through, from the app to the hardware. There are
three ways down: telephony, Bluetooth, GPS, NFC, fingerprint and sensors talk to a HAL in the
Android container over binder (libgbinder). Display, audio, camera and face unlock load vendor
libraries into their own process via libhybris: phoc renders with the Mali driver into gralloc
buffers and shows them through KMS (Android's composer stays off), the NPU is reached through
the vendor's NNAPI support library. Wi-Fi, vibration, flashlight, brightness and battery need no
Android in the path and use kernel devices directly. Wi-Fi and the modem still depend on the
container at startup: its NVRAM services hand MAC address, IMEI and calibration data to the
drivers.

![Layers from the app down to the hardware](assets/layers.svg)

## Repository

| Directory | Content |
|---|---|
| [`pkgbuilds/`](pkgbuilds) | one PKGBUILD per package, with its patches and own sources |
| [`image/`](image) | the Arch image for the internal storage or the SD card (`mkimage.sh`), QEMU preview and tests |
| [`tools/`](tools) | build chroot, package builds, boot files, signing, publishing and approval of the repository, probes |
| [`assets/`](assets) | the diagrams of this page |

## Building

Everything runs on an x86_64 Linux host without root, through user namespaces and qemu-user.
The host needs `qemu-user-static-binfmt`, `android-tools`, `e2fsprogs`, `erofs-utils`, `pacman`
(for `repo-add`), `gnupg`, `curl` and an entry for your user in `/etc/subuid`. AI coding agents
find a short guide to the repository in [AGENTS.md](AGENTS.md).

### Your own image

```
tools/chroot-init.sh       # aarch64 build chroot from Arch Linux ARM (build/chroot)
tools/ut-fetch.sh          # vendor images and boot images of the matching UT build → build/ut/04112026
tools/repo-fetch.sh        # our signed packages from the published repository → build/repo/local
image/mkimage.sh -a build/ut/04112026/android/android-rootfs.img -P -k ~/.ssh/id_ed25519.pub \
    -r https://plinius.cais.de/repo/aarch64 -o build/my-plinius.img
```

`-P` asks for the PIN of the user `plinius` (4 to 12 digits, as the greeter takes it), `-k` puts
your SSH key in for root, and `-r` keeps the phone receiving updates from the repository. The PIN
goes into the image only as a yescrypt hash, as every password set on the phone, and never on a
command line; the image is readable only by its builder. Plinius Boot writes the image to a microSD
card or to the internal storage. The image carries what a start needs: Android images, kernel,
ramdisk and the boot images. A new system starts with the kernel of `plinius-kernel`, not UT's
6.1.145: `mkimage.sh` puts its `boot.img` into UT's boot partition content with the AVB data
moved behind it (`plinius-kernel-avb`) and its `Image.gz` into the boot entry. The older
`plinius-kernel` pinned in `image/fallback-kernel.sha256` (6.1.192) is kept in the image as the
fallback; at the first start `plinius-kernel-install` gives it its boot image and boot menu
entry. `tools/ut-build.sha256` pins every file of the UT build; `ut-fetch.sh`,
`tools/boot-files.sh` and `mkimage.sh` refuse a file that differs, a download or one already
there. `tools/boot-files.sh` also checks kernel and ramdisks against `tools/boot-files.sha256`,
because `plinius-kmod` is built for exactly that kernel. `tools/repo-fetch.sh` takes a channel only
with its signed, current stamp naming exactly the database it hands out, and each package only
with the SHA-256, name and version of its entry in that database. An image with your key and PIN
is meant for your own phone, not for passing on.

### Image and Plinius Boot for download

An image for download is built without `-k` and `-P`, but with `-r` and `-V`: the phone asks for
its PIN at the first start, and `vendor.img` and `vendor_dlkm.img` stay out. Plinius Boot copies
them from Volla OS on the phone (the active slot in `super`) and notes, without stopping, if they
differ from the pins in `/usr/share/plinius-boot/vendor.sha256`. `tools/image-check.sh` reads the
image with `debugfs` and refuses anything of the builder (SSH keys, host keys, machine-id, pacman
keyring, package cache, shell history, a password), the two vendor images, and anything missing
(`[plinius]` signed over https, `plinius-keyring`, the pinned Android and boot files, the kernel
of `plinius-kernel` in `zImage` and in `boot.img` with valid AVB data, the kept fallback kernel,
the pins of the vendor images and room for them, the first-start PIN, every path a service's
sandbox needs that neither systemd nor tmpfiles.d makes, from `tools/unit-paths.py`). Then
`image/test-image.sh` boots the image itself in QEMU (snapshot, the image stays unchanged) with ALARM's generic kernel and its
modules, up to `graphical.target`: every unit that fails or keeps restarting and is not named in
`image/test-image.expected` as one that needs the phone (Android container, GPU, charger,
sensors), a unit that must run and does not, broken package dependencies (`pacman -Dk`) or a
dead system bus stop the download. Plinius Boot finds the image through `images/latest.json` on
the web server.

```
tools/downloads.sh image build/plinius-arch-<date>.img   # check, boot test, pack, latest.json → build/downloads/images
image/test-image.sh build/plinius-arch-<date>.img        # the boot test alone (about 6 to 10 minutes) → build/image-test/
tools/downloads.sh app org.plinius.Boot-x86_64.flatpak plinius-boot.tar.gz   # Plinius Boot → build/downloads/downloads
DOWNLOAD_TARGET=user@host:/path tools/downloads.sh upload [images|app] # never an older image over a newer one
```

### Building packages

```
tools/build.sh libhybris      # build a package from pkgbuilds/ → build/repo/local
tools/devtree.sh <pkg> setup  # persistent build tree for quick incremental tests on the device
REMOTE=user@host tools/remote-build.sh <pkg>   # build on a second host, packages come back
tools/candidate.sh            # check all packages against the current mirrors before an approval
tools/freigabe.py             # approve the tested state of the repo
tools/publish.sh              # sign the local repo into the channel beta → build/repo/plinius/beta;
                              # only packages whose sources passed a full green tools/run-tests.sh
tools/release.sh              # after testing: the signed beta state 1:1 to stable, nothing re-signed
REPO_TARGET=user@host:/path tools/upload-repo.sh   # beta, then stable: packages, database, old packages last
tools/kernel-trial.sh build/ut/<build>   # try a new UT kernel through a second boot entry
tools/kernel-kmi.py -C build/kernel-trial types --genksyms <old> <new> --symtypes <obj> --needs pkgbuilds/plinius-kernel/vendor-needs.txt
                              # after a linux-6.1.y merge: what the vendor modules' interface sees changed
tools/kernel-checkbuild.sh [--package] <rev>   # check build of a kernel commit, pkgrel 99, never into the repo
tools/run-tests.sh            # the test suite; a full green run is the test gate of publish.sh
tools/phone-check.py          # everyday functions of the phone live over SSH (reads and scans only)
tools/lpm-night.sh start|status|fetch|stop   # passive night log of the SoC's low-power states and what blocks them
```

A merge of a new linux-6.1.y release into `plinius-kernel` is measured with `tools/kernel-kmi.py`
before it is built: `conflicts` classifies the conflict regions, `types` lists the definitions
that change among the types the prebuilt modules reach (with `--genksyms` as genksyms sees them
after the KMI repair: none may be left), `inlines` the macros and inline functions of the headers
(compiled into the modules, so no CRC shows them), `dupes` fixes a merge applies a second time,
`vendor-const` finds an inlined constant in the modules' code and `layout` compares the member
offsets of two builds. `tools/kernel-checkbuild.sh` then builds the commit with the PKGBUILD's own
`prepare()` and `build()`, which checks every imported CRC.

## Third-party projects and licenses

Own code in this repository (own files in `pkgbuilds/`, `tools/`, `image/` and the diagrams)
is licensed under the **MIT License**, see [LICENSE](LICENSE). Exceptions are code that has to
be GPL for the kernel: the modules of `plinius-kmod` and the BPF programs of `plinius-netlog`
(GPL-2.0-only). Patches in `pkgbuilds/` are under the license of the project they patch.
Packages that combine our code with third-party code carry both licenses, e.g.
`plinius-progress` (BuffyBox, GPL-3.0-or-later) and `gnome-control-center-plinius` (GNOME
Settings, GPL-2.0-or-later). The licenses of each package are recorded in its `PKGBUILD`
(`license=`).

### Upstream projects built from source

| Package | Upstream project | License | Our changes |
|---|---|---|---|
| `android-headers` | [Halium/android-headers](https://github.com/Halium/android-headers) | Apache-2.0 | none |
| `libhybris` | [libhybris/libhybris](https://github.com/libhybris/libhybris) | Apache-2.0, BSD-2-Clause | patches: rename libcamera to libcamera-hybris, GBM platform and dma-buf import, linux-dmabuf buffers for Wayland clients, swap with damage through the window system, the driver's shader cache on disk; own TLS padding for executables with TLS of their own (Firefox) |
| `hybris-gbm` | libgbm-hybris by D0gg0Man and Bardia Moshiri (FuriLabs) | BSD-2-Clause | reworked for linear gralloc buffers on real KMS, see `hybris_gbm.c` |
| `gl4es` | [ptitSeb/gl4es](https://github.com/ptitSeb/gl4es) | MIT | patches: builds with GCC 14, the EGL wrapper asks a GLES driver for a GLES config, a deleted program no longer stays the current one; a starter `gl4es <program>` |
| `vkmark-plinius` | [vkmark/vkmark](https://github.com/vkmark/vkmark) | LGPL-2.1-only | patches: fills the screen at a fractional output scale and in portrait, shows the scene and the score on screen |
| `lxc-android-halium` | [UBports lxc-android-config](https://gitlab.com/ubports/development/core/hybris-support/lxc-android-config) | GPL-3.0-or-later | the subset the Plinius uses under UT |
| `phoc-plinius` | [Phosh/phoc](https://gitlab.gnome.org/World/Phosh/phoc) | GPL-3.0-or-later | patches: GPU sync, keyboard only after a tap and unfolded by a tap into the field, 90/120 Hz, boot logo in the accent color, night light without CRTC gamma, custom accent color, animated background effect, outputs kept over a session pause, doze for an always-on display (also across a change of mode), virtual output on request and windows that follow the screen they were picked on, window scale that starts over when arranged anew, a crash fix (built with embedded wlroots, see below) |
| `phosh-plinius` | [Phosh/phosh](https://gitlab.gnome.org/World/Phosh/phosh), PKGBUILD based on Arch's | GPL-3.0-or-later | patches: restart and Restart UI in the power menu, top bar from the panel data, notch search across apps, settings, contacts and events (GNOME search providers), camera light that follows a turned screen, unlock pattern, NFC, cast, USB and desktop-over-VNC quick settings, hotspot on `ap0`, accent color, dimming fade, brightness without visible steps, brightness slider in auto mode, flashlight tile follows the button, lock state to logind at startup, Bluetooth headset as one device per direction, modem with a SIM first, always-on display (half as bright in the dark), emergency button on the lock screen and a full-screen emergency menu, unlocking on a second screen, folders sorted by name, "Remove from Folder" and "Dissolve Folder" in the folder menus, gradual automatic brightness; libical 4 patch from Arch |
| `stevia-plinius` | [Phosh/stevia](https://gitlab.gnome.org/World/Phosh/stevia) | GPL-3.0-or-later | patches: glass look of the password prompt at startup, glass or original look as a setting, microphone key for speech to text, symbol rows at full width, keys keep their height with a virtual output, starts along with Phosh |
| `gnome-control-center-plinius` | [GNOME/gnome-control-center](https://gitlab.gnome.org/GNOME/gnome-control-center), PKGBUILD based on Arch's | GPL-2.0-or-later | patches and own panels: phone display and notch, users with pattern, fingerprint and face unlock, encryption, Device Security, charging, eSIM, hotspot, NFC, VPN editors and dialogs as pages, accent color, Hardware page, beaconDB contribution, speech to text, Streaming, USB and Desktop over VNC panels, Memory and Swap, networks allowed for SSH, always-on display, look of the on-screen keyboard, no automatic focus into text fields, panels that open without waiting, battery use per app, DNS Filter, Web Apps, Access Control |
| `snapshot-plinius` | [GNOME/snapshot](https://gitlab.gnome.org/GNOME/snapshot) | GPL-3.0-or-later | patches: the thumbnail opens the default app for pictures and videos, flash button, keep the camera used last, real photos with noise reduction, exposure and scene settings through camera-hal-bridge, top bar below the front camera |
| `xdg-desktop-portal-wlr-plinius` | [emersion/xdg-desktop-portal-wlr](https://github.com/emersion/xdg-desktop-portal-wlr) | MIT | patches: screencasts with shm buffers without a render node; own chooser: a window to pick the screen, Cancel, ends itself after a minute |
| `wpa_supplicant-plinius` | [hostap](https://w1.fi/wpa_supplicant/), PKGBUILD based on Arch's | BSD-3-Clause | Arch's patches, own patch: no PSK-SHA256 for a hotspot without PMF |
| `buffybox` | [postmarketOS/buffybox](https://gitlab.postmarketos.org/postmarketOS/buffybox) | GPL-3.0-or-later | patches: glass theme with wider keys, Cyrillic font, Russian keyboard, placeholder, texts of the shutdown dialog |
| `plinius-progress` | own program on top of [postmarketOS/buffybox](https://gitlab.postmarketos.org/postmarketOS/buffybox) | GPL-3.0-or-later | same patches as `buffybox` |
| `mobile-config-firefox` | [postmarketOS/mobile-config-firefox](https://gitlab.postmarketos.org/postmarketOS/mobile-config-firefox) | MPL-2.0 | own defaults, signed uBlock Origin and swipe gestures included, preloaded after login for a fast first start |
| `biomd` | [FuriLabs/biomd](https://github.com/FuriLabs/biomd) | GPL-2.0-only | patches from LuneOS (see below) and our own: AIDL wire format, sensor under the display, session handling, no fingerprint before the first PIN, real enrollment progress |
| `bluebinder` | [mer-hybris/bluebinder](https://github.com/mer-hybris/bluebinder) | GPL-2.0-or-later | patches: poll the device state, HAL before the vhci device, drop a stray Command Status |
| `libgbinder-tools` | [mer-hybris/libgbinder](https://github.com/mer-hybris/libgbinder) | BSD-3-Clause | only the command line tools |
| `libgbinder-radio` | [mer-hybris/libgbinder-radio](https://github.com/mer-hybris/libgbinder-radio) | BSD-3-Clause | none |
| `libncicore` | [mer-hybris/libncicore](https://github.com/mer-hybris/libncicore) | BSD-3-Clause | patch: poll passively for tags |
| `libnciplugin` | [mer-hybris/libnciplugin](https://github.com/mer-hybris/libnciplugin) | BSD-3-Clause | none |
| `nfcd-binder-plugin` | [mer-hybris/nfcd-binder-plugin](https://github.com/mer-hybris/nfcd-binder-plugin) | BSD-3-Clause | none |
| `ofono-binder-plugin` | [mer-hybris/ofono-binder-plugin](https://github.com/mer-hybris/ofono-binder-plugin) | GPL-2.0-only | patches: SIM status and APDU requests, only registration failures with the display off, IMS data profile with IWLAN allowed, emergency numbers through emergencyDial, the IRadio version of a call stays when the MediaTek extension also reports it |
| `ofono-sailfish` | [sailfishos/ofono](https://github.com/sailfishos/ofono) | GPL-2.0-only | patches: logical channels to the eUICC (eSIM), public warnings never taken for the cell's name |
| `libdbusaccess` | [sailfishos/libdbusaccess](https://github.com/sailfishos/libdbusaccess) | BSD-3-Clause | none |
| `libmce-glib` | [sailfishos/libmce-glib](https://github.com/sailfishos/libmce-glib) | BSD-3-Clause | none |
| `libnfcdef` | [sailfishos/libnfcdef](https://github.com/sailfishos/libnfcdef) | BSD-3-Clause | none |
| `mce-dev` | [sailfishos/mce-dev](https://github.com/sailfishos/mce-dev) | LGPL-2.1-or-later | none |
| `nfcd` | [sailfishos/nfcd](https://github.com/sailfishos/nfcd) | BSD-3-Clause | own sysusers/tmpfiles |
| `ofono-binder-plugin-ext-mtk` | [UBports hybris-support/ofono-binder-plugin-ext-mtk](https://gitlab.com/ubports/development/core/hybris-support/ofono-binder-plugin-ext-mtk) | GPL-2.0-only AND BSD-3-Clause | patches: voice domains per SIM (GSM, LTE, 5G, Wi-Fi), emergency calls through emergencyDial on the IMS slot and when the modem wants to redial |
| `ofono2mm` | [droidian/oFono2MM](https://github.com/droidian/oFono2MM) | BSD-3-Clause | patches: modems that appear later, SIM locks, online once a SIM is inserted, a data SIM if oFono has none, call waiting through oFono, ModemManager's CellBroadcast interface, radio on for an emergency call; own udev rule and sysusers |
| `gnome-calls-plinius` | [GNOME/calls](https://gitlab.gnome.org/GNOME/calls) | GPL-3.0-or-later | patch: an emergency call ends other calls first, tries again and reports failure |
| `chatty-plinius` | [World/Chatty](https://gitlab.gnome.org/World/Chatty) | GPL-3.0-or-later | patches: upstream's vCard fix for evolution-data-server 3.60, follows address books that appear later |
| `gnome-online-accounts-plinius` | [GNOME/gnome-online-accounts](https://gitlab.gnome.org/GNOME/gnome-online-accounts) | LGPL-2.0-or-later | patch: Refresh button; own helper so that apps get an account's data after a start |
| `libhandy-plinius` | [GNOME/libhandy](https://gitlab.gnome.org/GNOME/libhandy) | LGPL-2.1-or-later | patch: a slow swipe no longer makes the page jump |
| `pipewire-audio-plinius` | [PipeWire](https://gitlab.freedesktop.org/pipewire/pipewire) (in place of Arch's `pipewire-audio`), PKGBUILD based on Arch's | MIT | patches: calls numbered from 1 for car kits, AT commands answered when their call goes away |
| `wayvnc-plinius` | [any1/wayvnc](https://github.com/any1/wayvnc) | ISC | patch: a resize keeps the output where it is; own PAM service |
| `lpac` | [estkme-group/lpac](https://github.com/estkme-group/lpac) | AGPL-3.0-only, LGPL-2.1-only | patch: APDU backend for oFono |
| `umtp-responder` | [viveris/uMTP-Responder](https://github.com/viveris/uMTP-Responder) | GPL-3.0-or-later | none (master after 1.8.1, for its path traversal fixes) |
| `bluez-obex-plinius` | [BlueZ](http://www.bluez.org/) (obexd only), PKGBUILD based on Arch's | GPL-2.0-only | patches: the phone book from evolution-data-server (for cars over PBAP), names for contacts that only have a display name, contacts of all address books |
| `gnome-usage-plinius` | [GNOME/gnome-usage](https://gitlab.gnome.org/GNOME/gnome-usage), PKGBUILD based on Arch's | GPL-3.0-or-later | patch: system services are not matched to apps |
| `onnxruntime-plinius` | [microsoft/onnxruntime](https://github.com/microsoft/onnxruntime) | MIT and others, see below | none (own build for aarch64) |
| `sherpa-onnx` | [k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | Apache-2.0 | none (speech recognition and VAD only, against `onnxruntime-plinius`) |
| `plinius-tflite` | [tensorflow/tensorflow](https://github.com/tensorflow/tensorflow) (TensorFlow Lite) | Apache-2.0 and others, see below | patch: NNAPI delegate outside of Android; own C interface |
| `plinius-kernel` | UT's Halium kernel [`android14-6.1-halium`](https://gitlab.com/ubports/porting/community-ports/android12/generic/kernel-android-common), merged with Google's [ACK `android14-6.1`](https://android.googlesource.com/kernel/common/) and [linux-6.1.y](https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git); source in [misc-de/plinius-kernel](https://github.com/misc-de/plinius-kernel) | GPL-2.0-only | the interface of the prebuilt MediaTek modules kept (KMI, every imported CRC checked at build), stable fixes ported where 6.1.y lacks them, a fix for a NULL folio in the ext4 buddy cache of 6.1.y, no f2fs; a pacman hook writes each new kernel as the default and keeps the previous one as a fallback entry in the boot menu |
| `plinius-kmod` | `uleds` from the [UBports kernel](https://gitlab.com/ubports/porting/community-ports/android12/generic/kernel-android-common) | GPL-2.0-only | none; next to our own modules `plinius-backlight`, `plinius-novt` and `plinius-btsuspend` |
| `furios-gps-contribute` | [misc-de/furios_gps](https://github.com/misc-de/furios_gps), only the beaconDB contribution tool | MIT | patches: wait past geoclue's cached location for a fresh fix, time for a cold start |
| `furios-audio-bt-reconnect` | [misc-de/furios_audio](https://github.com/misc-de/furios_audio), only the Bluetooth reconnect watcher | MIT | patch: also dials when the display comes on, when music starts and after a reboot; headphones only, no car kits |
| `schwupp` | [misc-de/Schwupp](https://github.com/misc-de/Schwupp) (own project) | MIT | none |
| `muga` | [misc-de/Muga](https://github.com/misc-de/Muga) (own project) | MIT | none |

### Embedded in the builds above

| Component | Used in | License |
|---|---|---|
| [wlroots](https://gitlab.freedesktop.org/wlroots/wlroots) 0.20.2 | `phoc-plinius` (statically embedded, as upstream phoc does) | MIT |
| [gvdb](https://gitlab.gnome.org/GNOME/gvdb) | `phoc-plinius` | LGPL-2.1-or-later |
| [libcall-ui](https://gitlab.gnome.org/World/Phosh/libcall-ui) | `phosh-plinius`, `gnome-calls-plinius` | LGPL-2.1-or-later |
| [libgnome-volume-control](https://gitlab.gnome.org/GNOME/libgnome-volume-control) | `phosh-plinius`, `gnome-control-center-plinius` | GPL-2.0-or-later |
| [libgxdp](https://gitlab.gnome.org/GNOME/libgxdp) | `gnome-control-center-plinius` | LGPL-2.1-or-later |
| [LVGL](https://github.com/lvgl/lvgl) | `buffybox`, `plinius-progress` | MIT |
| `fart_enums.h` from [FuriLabs/face-analysis-recognition-toolkit](https://github.com/FuriLabs/face-analysis-recognition-toolkit) | `biomd` (header only, libfart is not linked) | GPL-2.0-only |
| abseil, flatbuffers, gemmlowp, ruy, ml_dtypes, KleidiAI; XNNPACK; cpuinfo, pthreadpool; FP16, FXdiv, farmhash; Eigen; OouraFFT | `plinius-tflite` (statically linked) | Apache-2.0; BSD-3-Clause; BSD-2-Clause; MIT; MPL-2.0; own permissive license |
| ONNX Runtime's dependencies: onnx, protobuf (lite), abseil, re2, flatbuffers, Eigen, cpuinfo, pthreadpool, nlohmann-json, GSL, SafeInt, date, Boost.Mp11 and others (`cmake/deps.txt`) | `onnxruntime-plinius` (statically linked) | MIT; Apache-2.0; BSD-3-Clause; BSD-2-Clause; MPL-2.0; BSL-1.0 |
| kaldi-native-fbank, kaldi-decoder, OpenFst, simple-sentencepiece; nlohmann-json | `sherpa-onnx` (statically linked) | Apache-2.0; MIT |
| pyatv, pywebostv, miniaudio, tinytag; srptools | `schwupp` (vendored Python packages) | MIT; BSD-3-Clause |

### Third-party patches, models and artwork

| What | Origin | License | Used in |
|---|---|---|---|
| biomd patches `0001`–`0004` (build options, free-form finger names, AIDL backend) | Herman van Hazendonk, [LuneOS meta-webos-ports](https://github.com/webOS-ports/meta-webos-ports) | GPL-2.0-only (as biomd) | `biomd` |
| wpa_supplicant patches `0001`–`0008` and `wpa_supplicant_config` | Arch Linux package `wpa_supplicant` 2:2.12-2 | BSD-3-Clause (as wpa_supplicant) | `wpa_supplicant-plinius` |
| Face detection YuNet, face recognition SFace (converted from ONNX to TFLite by `tools/face-models.sh`) | [OpenCV Zoo](https://github.com/opencv/opencv_zoo) | YuNet MIT, SFace Apache-2.0 | `plinius-face` |
| Speech recognition NeMo Parakeet TDT 0.6B v3 (int8) and Silero VAD v5, as converted to ONNX by sherpa-onnx | [NVIDIA NeMo](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3), [Silero VAD](https://github.com/snakers4/silero-vad) | Parakeet CC-BY-4.0, Silero VAD MIT | `plinius-dictate-models` |
| Face Mesh (face landmarker v2) | [Google MediaPipe](https://github.com/google-ai-edge/mediapipe) | Apache-2.0 | `plinius-face` |
| Background of the password prompt (`glass-background.bin`) | derived from the "blobs" wallpaper of [gnome-backgrounds](https://gitlab.gnome.org/GNOME/gnome-backgrounds) (cropped, blurred, darkened) | CC-BY-SA-3.0 | `plinius-crypt` |
| App icon | [GNOME Software](https://gitlab.gnome.org/GNOME/gnome-software) 51.1, renamed | GPL-2.0-or-later | `plinius-software` |

### Used unchanged, not part of this repository

| What | Origin | License |
|---|---|---|
| Base system and desktop (wlroots0.20, PipeWire, libgbinder, lxc, ModemManager, Firefox, GNOME apps, …) | [Arch Linux ARM](https://archlinuxarm.org) packages | per package |
| UT's kernel 6.1.145 (`android14-6.1-halium`), the base of `plinius-kernel`, and in the rescue system | [UBports kernel-android-common](https://gitlab.com/ubports/porting/community-ports/android12/generic/kernel-android-common) | GPL-2.0-only |
| Halium ramdisk, overlays | [UT reference port volla-ansuz](https://gitlab.com/ubports/porting/reference-device-ports/halium-14/volla-phone-plinius/volla-ansuz) | per file in that repository |
| Android vendor image and firmware (`vendor`, `vendor_dlkm`) | from a UT build of the port, not part of this repository and not redistributed: the image for download leaves them out, Plinius Boot copies them from Volla OS on the phone | proprietary (Volla/MediaTek) |
