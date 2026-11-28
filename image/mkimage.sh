#!/bin/bash
# Builds the ext4 image for the Arch partition (SD card via multi-boot, later system_a).
# With -a it carries everything a start needs, kernel and ramdisk included: no other system has to
# be on the card (docs/PLAN-IMAGE.md).
#
# Usage: image/mkimage.sh [-a android-rootfs.img] [-V] [-k ssh-pubkey] [-P] [-r repo-url] [-s size] [-o output.img]
#   -a  Android image from the UT installation (/var/lib/lxc/android/android-rootfs.img), as
#       tools/ut-fetch.sh lays it out. Each UT file used must be the one pinned in
#       tools/ut-build.sha256, checked before anything is unpacked or installed.
#       vendor.img, vendor_dlkm.img and system_dlkm.img next to it are taken along: UT keeps
#       them there, and without them mount-android-partitions would use the partitions of the
#       internal storage (Volla OS). Without -a the system still boots up to SSH, just without
#       the Android container. boot.img, vendor_boot.img and init_boot.img one directory above
#       go to /usr/share/plinius-boot (tools/boot-files.sh), from where Plinius Boot takes them:
#       zImage and initrd.cpio.gz for a boot entry, the images for the phone's own storage. The
#       kernel in zImage and boot.img is plinius-kernel's, not UT's 6.1.145 (it lacks Landlock
#       and what the AppArmor profiles need): its boot.img inside UT's partition content with the
#       AVB data moved behind it (plinius-kernel-avb). The fallback is the older plinius-kernel
#       pinned in image/fallback-kernel.sha256, taken from the local repo and kept in
#       /var/lib/plinius-kernel; the first start gives it its boot image and boot menu entry.
#       Without that file the image has no fallback kernel. The
#       ramdisks there carry the password prompt of the optional encryption
#       (tools/crypt-ramdisk.sh; plain data partitions start as before). Its vibration needs the
#       motor's firmware from vendor.img: dump.erofs (erofs-utils), or HAPTIC_FW=haptic_ram.bin.
#       /usr/share/plinius-boot/vendor.sha256 names vendor.img and vendor_dlkm.img as pinned ("sha256
#       size name").
#   -V  vendor.img and vendor_dlkm.img stay out of the image (an image for download: they are Volla's
#       and MediaTek's to pass on). Plinius Boot copies them from Volla OS on the phone, from super,
#       and says so if they differ from vendor.sha256; without a copy mount-android-partitions takes
#       them from super as the phone starts. The image keeps room for them.
#   -k  public SSH key for root, e.g. ~/.ssh/id_ed25519.pub for a phone of your own. Without -k
#       root has no key in the image: an image for others must not carry the builder's root access
#       (attack review 2026-11-12, supply chain L10). sshd takes keys only, so without -k nobody
#       logs in over SSH until a key is added on the phone (~/.ssh/authorized_keys of plinius).
#   -P  ask for the PIN of the user "plinius" (Phosh unlocks with it): 4 to 12 digits, as the
#       greeter takes it. It is hashed with yescrypt like every password on the phone and reaches
#       the image through a file only the builder can read, never on a command line. Without -P
#       the password stays locked; set it later over SSH (passwd plinius).
#   -r  server of our own repo [plinius] for updates of our packages (docs/PAKETIERUNG.md,
#       hosting; tools/publish.sh), e.g. http://10.15.19.100:45461/aarch64 for the build host on
#       the USB network. The image trusts only packages signed with the key from plinius-keyring
#       (tools/keygen.sh). Without -r our packages on the device get no updates, not even a new
#       approval (plinius-freigabe).
#   -s  image size (default: 8G; grown to the partition after writing)
#   -o  output file (default: build/plinius-arch.img); only the builder can read it (0600), it
#       holds /etc/shadow
set -euo pipefail
. "$(dirname "$0")/../tools/lib.sh"

# check_ut_files ANDROID_IMG: dies unless every UT file -a takes is the one pinned in
# tools/ut-build.sha256: the Android images next to ANDROID_IMG that are there and the three boot
# images one directory above (tools/boot-files.sh checks those again)
check_ut_files() {
	local dir=${1%/*} name f want got
	for name in android/android-rootfs.img android/vendor.img android/vendor_dlkm.img \
		android/system_dlkm.img boot.img vendor_boot.img init_boot.img; do
		case $name in
			android/android-rootfs.img) f=$1 ;;
			android/*) f=$dir/${name#android/}; [[ -f $f ]] || continue ;;
			*) f=${dir%/*}/$name; [[ -f $f ]] || die "$f missing (-a takes the boot images from there)" ;;
		esac
		want=$(awk -v n="$name" '$1 !~ /^#/ && $2 == n { print $1 }' "$VOLLA_DIR/tools/ut-build.sha256")
		got=$(sha256sum < "$f")
		[[ $want =~ ^[0-9a-f]{64}$ && ${got%% *} == "$want" ]] ||
			die "$f is not $name of the UT build pinned in tools/ut-build.sha256 (SHA-256 differs)"
	done
	msg "UT files as pinned in tools/ut-build.sha256"
}

# Prints the /etc/shadow hash of the PIN read from stdin: yescrypt with libxcrypt's default cost,
# as every password set on the phone (pam_unix with libxcrypt's default, login.defs ENCRYPT_METHOD
# YESCRYPT), instead of sha512crypt with its 5000 rounds
hash_pin() {
	python3 -I -c '
import ctypes, ctypes.util, sys
name = ctypes.util.find_library("crypt")
if not name:
    sys.exit("libcrypt (libxcrypt) missing")
lib = ctypes.CDLL(name)
lib.crypt_gensalt.restype = lib.crypt.restype = ctypes.c_char_p
lib.crypt_gensalt.argtypes = [ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p, ctypes.c_int]
lib.crypt.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
pin = sys.stdin.buffer.read()
setting = lib.crypt_gensalt(b"$y$", 0, None, 0)
hashed = setting and lib.crypt(pin, setting)
if not hashed or not hashed.startswith(b"$y$") or lib.crypt(pin, hashed) != hashed:
    sys.exit(f"{name} cannot hash with yescrypt")
print(hashed.decode())
'
}

# Asks twice for the PIN of plinius and prints its hash. The rule is the greeter's: 4 to 12 digits.
read_pin() {
	local pin pin2 hashed
	read -rsp "PIN for plinius (4 to 12 digits): " pin; echo >&2
	read -rsp "PIN again: " pin2; echo >&2
	[[ ${#pin} -ge 4 && ${#pin} -le 12 && $pin != *[!0123456789]* ]] || die "a PIN has 4 to 12 digits"
	[[ $pin == "$pin2" ]] || die "the PINs differ"
	# Only the hash goes into the build; the PIN itself is not kept. printf is a shell builtin,
	# the PIN appears on no command line.
	hashed=$(printf '%s' "$pin" | hash_pin) || die "the PIN could not be hashed"
	[[ $hashed == '$y$'* ]] || die "the PIN could not be hashed with yescrypt"
	echo "$hashed"
}

# db_file NAME: the package file the database of the local repo names for package NAME
db_file() {
	bsdtar -xOf "$LOCALREPO/plinius-local.db.tar.gz" 2>/dev/null | awk -v n="$1" '
		/^%FILENAME%$/ { getline f; next }
		/^%NAME%$/ { getline m; if (m == n) { print f; exit } }'
}

# fallback_kernel: the package file pinned in image/fallback-kernel.sha256, checked; nothing if it
# is not in the local repo
fallback_kernel() {
	local want name f got
	read -r want name < <(grep -v '^#' "$VOLLA_DIR/image/fallback-kernel.sha256")
	f=$LOCALREPO/$name
	[[ -f $f ]] || return 0
	got=$(sha256sum < "$f")
	[[ ${got%% *} == "$want" ]] || die "$f is not the fallback kernel pinned in image/fallback-kernel.sha256"
	echo "$f"
}

# image_kernel BOOT_FILES WORK: the kernel a new system starts with is plinius-kernel's (the one
# the image installs): its Image.gz becomes BOOT_FILES/zImage for a boot entry, its boot.img goes
# into UT's partition content BOOT_FILES/boot.img, the way plinius-kernel-install writes
# boot<slot>. The fallback package, when there, is linked as WORK/fallback-kernel.pkg.
image_kernel() {
	local files=$1 work=$2 kpkg fallback rel
	local -a kimgs
	kpkg=$(db_file plinius-kernel)
	[[ -n $kpkg && -f $LOCALREPO/$kpkg ]] || die "plinius-kernel missing in $LOCALREPO"
	mkdir -p "$work/kernel"
	bsdtar -xf "$LOCALREPO/$kpkg" -C "$work/kernel" 'usr/lib/plinius/kernel/*/boot.img' \
		'usr/lib/plinius/kernel/*/Image.gz'
	kimgs=("$work"/kernel/usr/lib/plinius/kernel/*/boot.img)
	[[ ${#kimgs[@]} == 1 && -f ${kimgs[0]} ]] || die "$kpkg holds no single kernel"
	python3 -I "$PKGBUILDS/plinius-kernel/plinius-kernel-avb" "${kimgs[0]}" "$files/boot.img" \
		"$work/boot.img.new" || die "the kernel of $kpkg does not fit into UT's boot.img"
	mv "$work/boot.img.new" "$files/boot.img"
	cp "${kimgs[0]%/*}/Image.gz" "$files/zImage"
	rel=${kimgs[0]%/*}
	msg "Kernel of the image: ${rel##*/}"
	fallback=$(fallback_kernel)
	if [[ -n $fallback ]]; then
		ln -sf "$fallback" "$work/fallback-kernel.pkg"
		msg "Fallback kernel: ${fallback##*/}"
	else
		msg "Note: no fallback kernel ($(grep -v '^#' "$VOLLA_DIR/image/fallback-kernel.sha256" |
			awk '{ print $2 }') is not in $LOCALREPO)"
	fi
}

android_img=
ssh_key=
size=8G
ask_pin=0
vendor_from_phone=0
pin_hash=
repo_url=
out=$BUILD/plinius-arch.img
while getopts a:Vk:Pr:s:o: opt; do
	case $opt in
		a) android_img=$(realpath "$OPTARG") ;;
		k) ssh_key=$OPTARG ;;
		P) ask_pin=1 ;;
		V) vendor_from_phone=1 ;;
		r) repo_url=$OPTARG ;;
		s) size=$OPTARG ;;
		o) out=$OPTARG ;;
		*) exit 2 ;;
	esac
done
[[ -z $ssh_key || -f $ssh_key ]] || die "SSH key $ssh_key missing (-k)"
[[ -z $repo_url || $repo_url =~ ^https?://[A-Za-z0-9._:/-]+$ ]] || die "invalid repo URL: $repo_url"
[[ -z $android_img || -f $android_img ]] || die "$android_img missing"
for img in vendor vendor_dlkm system_dlkm; do
	[[ -z $android_img || -f $(dirname "$android_img")/$img.img ]] ||
		msg "Note: no $img.img next to $android_img, the container would use the internal $img partition"
done
[[ -z $android_img ]] || check_ut_files "$android_img"
(( ! vendor_from_phone )) || [[ -n $android_img ]] || die "-V needs -a: the pins of vendor.img come from the UT build"
[[ -d $CHROOT/usr ]] || die "build chroot missing, run tools/chroot-init.sh first"
[[ -e $LOCALREPO/plinius-local.db ]] || die "local repo empty, run tools/build.sh device-volla-ansuz first"
check_host
(( ! ask_pin )) || pin_hash=$(read_pin)

# Base system, SSH over USB (phase 2) and Phosh (phase 5): phosh-plinius (restart in the power
# menu), phoc with embedded, patched wlroots
# (pkgbuilds/phoc-plinius), Stevia as on-screen keyboard (stevia-plinius: glass look of the
# password prompt), greetd for the automatic login. which:
# phosh-session needs it for systemd-cat, otherwise the phoc output is lost. WLAN (phase 6):
# NetworkManager with wpa_supplicant; plinius-wlan.service (device package) powers it on.
# Bluetooth: BlueZ, bluebinder passes the Android HAL through vhci. Vibration: feedbackd with
# vibrator-ff-bridge on the vendor haptics FF device. Mobile network: ofono (Sailfish fork) with
# the binder plugin, oFono2MM provides the ModemManager API for Phosh, Calls and Chatty. NFC: nfcd
# with the binder plugin (also offers the neard API). GPS: gnss-nmea-bridge feeds geoclue.
# furios-gps-contribute: "Contribute to beaconDB" on the Location page, off until switched on.
# Audio: audio-hal-bridge puts the vendor audio HAL into PipeWire (user service). Sensors:
# sensor-proxy-bridge serves the sensors HAL as iio-sensor-proxy (rotation, proximity, light, compass).
# Cameras: camera-hal-bridge offers them as PipeWire camera nodes (user service), Snapshot as app
# (snapshot-plinius: pictures in the gallery can be zoomed). phosh-tour-plinius: the tour of the
# first login also explains what phosh-plinius adds. gnome-usage-plinius: Usage without
# system services in the app list (cupsd was "Manage Printing"). gnome-calls-plinius: Calls that
# doesn't lose an emergency call (docs/NOTRUF.md), plinius-apps takes it for gnome-calls.
# gnome-online-accounts-plinius: Refresh button; apps get an account's data after a start without
# network; brings the file backends. Named here, else pacman takes gnome-online-accounts.
# chatty-plinius: Chatty that sees address books arriving after its start, as those of an online
# account do. Named here, else plinius-apps pulls in chatty.
# libhandy-plinius: swipes follow the finger, a slow one on the lock screen made the page jump by
# whole screens. Named here, else pacman takes libhandy for Phosh and Stevia.
# gtk3-plinius: the same for scrolling, Phosh's app grid jumped back and forth by whole rows.
# Named here, else pacman takes gtk3.
# gnome-keyring: Phosh's WLAN password prompt first looks into the Secret Service and gives up
# without it ("No agents were available"). rtkit: realtime priority for PipeWire.
# gnome-control-center-plinius: GNOME Settings with our own pages; Phosh's "Sound Settings"
# and the Bluetooth/WLAN long press open its panels. plinius-kmod: backlight device for Phosh's
# brightness slider and uleds, through which plinius-torch offers the flash LEDs as torch.
# glibc-locales: all languages and formats precompiled, otherwise Settings offers none to choose.
# lpac: eSIM profiles (Settings > Mobile Network), through oFono's eUICC channels.
# plinius-wake: double tap and tilt to wake (off until switched on in Settings > Display).
# plinius-assist: actions for the assistant button (chosen in Settings > Power).
# plinius-sar: SAR back-off, lowers the modem's transmit power while a body is near (docs/MTKRADIOEX.md).
# plinius-datawatch: reconnects mobile data that is up but carries nothing (after a cell change).
# plinius-kmsg: the kernel log into the journal without the vendor drivers' status lines
#   (journald itself does not read it, ReadKMsg=no in device-volla-ansuz).
# cups-plinius: printing (Settings > Printers), Arch's cups with the fix for printers found on the
#   network (2.4.20 broke their temporary queues); avahi (its dependency) finds network printers,
#   nss-mdns (also a dependency) resolves their .local names, cups-pk-helper comes with Settings.
#   Both services are enabled below.
# plinius-pdf-printer: "Print to PDF" (cups-pdf), never the default next to a real printer.
# plinius-remote: the phone as Bluetooth touchpad and keyboard for another computer
#   (Settings > Mouse/Keyboard), and for its own desktop on a TV or over VNC (a virtual mouse and
#   keyboard through python-evdev and uinput; phoc leaves the keyboard on the desktop's screen).
# Hotspot: NetworkManager shares through dnsmasq and iptables-legacy (the kernel has no nf_tables);
#   wpa_supplicant-plinius offers WPA2-PSK the WLAN driver accepts (docs/HARDWARE.md, Hotspot).
# Fingerprint: biomd talks to the fingerprint HAL (enabled below), biomd-fprintd gives Settings the
#   fprintd interface, biomd-session unlocks Phosh after a match, plinius-udfps shows the touch
#   target over the sensor under the display.
# plinius-charge: stop charging at 80 % and fast charging (Settings > Power), enables itself.
# plinius-energy: estimated battery use per app (Settings > Battery > Battery Usage), enables itself.
# plinius-dnsfilter: DNS filter for the phone, its hotspot and USB clients (Settings > Network >
#   DNS Filter); on from the start (its install script), /etc/resolv.conf links to
#   NetworkManager's file through it.
# ofono-binder-plugin-ext-mtk: MediaTek's IMS/VoLTE extension for the binder plugin.
# plinius-pinguard: growing wait after wrong PINs for the lock screen, sudo and polkit, on from
#   the start (its install script; socket enables itself).
# plinius-software: installing apps and whole-system updates, shown in Settings (Apps, Updates);
#   brings plinius-freigabe.
# schwupp: casting to TVs, behind Phosh's cast quick setting; the screen comes from
# xdg-desktop-portal-wlr-plinius (named here, else pacman takes ALARM's portal for phosh's dependency).
# plinius-send-to-tv: Firefox hands YouTube links to the TV Schwupp casts to (long press on a link,
#   button in the address bar).
# plinius-usb: what a computer gets over USB (USB quick setting), umtp-responder its file sharing.
# plinius-epo: GPS orbit predictions every 6 hours, a fix in seconds instead of a minute (off by
#   default: they come from MediaTek's servers; Settings > Privacy > Location > Faster GPS Fix). Also
#   a dependency of the device package.
# plinius-ssh: the networks SSH answers on (USB only by default; Settings > System > Secure Shell).
# plinius-desktop: a desktop on another computer's screen over VNC (off by default; Settings > Desktop
#   over VNC), brings wayvnc-plinius.
# plinius-cardshare: the contact card over NFC, a QR code and Bluetooth (quick setting My Card;
#   Settings > NFC > Contact Card).
# plinius-swap: 8 GiB swap file behind zram, used only when memory runs short (Settings > System > Memory
#   and Swap); the file is made at the first boot on the phone, not in the image.
# plinius-login-keyring: opens the login keyring with a per-user device key at the automatic login,
#   instead of gnome-keyring's unlock prompt after every start (it named no application).
# plinius-crypt: optional encryption of the data partition (Settings > Device Security), with the
#   password prompt in the ramdisk; keeps the prompt's look in line with the session.
# plinius-crypt-ramdisk: the programs of that prompt; an update reaches the ramdisk through a trial start.
# plinius-satellite: tells when the phone is on a satellite network (Starlink Direct to Cell and
#   alike), meters the connection meanwhile and says what works; Phosh shows a satellite then.
# vim: the editor in the terminal ("vi" starts it, alias of device-volla-ansuz).
# plinius-greeter: with more than one account (Settings > Accounts > Local Accounts > Add) the phone
#   starts with an account picker in front of Phosh; with one it logs in at once as before.
# flatpak: apps from outside the Arch repositories; no remote is set up by default.
# plinius-home: /home on the data partition. The only package here that is enabled below instead of
#   by itself: on a phone that already runs, plinius-home-move has to copy the data first.
# VPN: WireGuard is part of NetworkManager; networkmanager-openvpn covers nearly every commercial
#   provider (.ovpn import in Settings). networkmanager-openconnect (company and university VPNs)
#   is not in the image but can be installed; its login window runs from Phosh (phosh-plinius
#   patch 0020).
# hybris-tls-relocate: moves Firefox's own TLS off bionic's TLS slots, else Mali's GL driver
#   crashes it (pacman hook, also after Firefox updates).
# plinius-health: self-test after each boot (timer); plinius-rollback: take back the last update.
# plinius-mce: tells the modem and Wi-Fi when the display is off (suspend modes).
# plinius-suspend: puts the phone to sleep whenever the display is off (docs/STANDBY.md).
# plinius-face: face unlock on the NPU (with plinius-tflite), set up in Settings.
# plinius-security, plinius-netlog: the Device Security page (CVE list, which app connects where).
# plinius-ambient: the moving wallpaper.
# plinius-dictate: speech to text behind the keyboard's microphone key (started on demand), with
#   its model (plinius-dictate-models, ~0.5 GB) so that dictation works without a download.
# bluez-obex-plinius: obexd with the phone book from evolution-data-server, so that a car gets
#   the contacts over Bluetooth (PBAP); named here, else pacman takes ALARM's bluez-obex.
# pipewire-audio-plinius: Arch's pipewire-audio with a Bluetooth plugin whose hands-free gateway
#   asks ofono2mm for network and calls, so that a car can call; named here, else pacman takes
#   ALARM's pipewire-audio. It needs ALARM's PipeWire of the same release (see its PKGBUILD).
# plinius-vdec: hardware video decoding (H.264, H.265, VP9) for GStreamer apps such as Showtime.
# plinius-apparmor: AppArmor profiles of the network and radio services, loaded by its own
#   service (Arch's apparmor.service with its untried profiles stays off).
# plinius-kernel: our kernel and its modules. The image starts with the kernel of the boot files
#   (-a); its hook cannot run while the image is built (no active slot there, nothing is written)
#   and puts the kernel in place with its first update on the device.
# furios-audio-bt-reconnect: calls Bluetooth headphones back after a restart or when they come
#   into range again.
# biomd-pam (PAM module only, no PAM file uses it), libgbinder-tools (binder-list/-ping/-call),
#   android-headers and mce-dev (headers, for building on the device): every package of ours is
#   in the image, except the game emulators (plinius-retro-games with plinius-games and
#   plinius-dos-installer), which Software only offers under "Recommended", and plinius-nfc-lab
#   (NFC tags and cards), a tool to install from Software.
# plinius-speech: the speech output of the system and the caller announcement in a Bluetooth
#   headset (Settings > Sound > Speech Output), with the default voice of the image's language
#   (plinius-speech-voice-en-us-joe, ~63 MB); the other voices (about 63 MB each) are installed
#   in Settings, like the emulators they are not in the image.
# plinius-appaccess: which app may use the cameras, security keys and network (Settings > Privacy);
#   only an optional dependency of Settings, so it is named here.
# python-evdev: touch gestures in phonebridge (installed by the user, needs the module).
# hybris-vulkan: Vulkan for the apps (the Android driver through libhybris; phoc and GTK stay on
#   GLES), vkmark-plinius the Vulkan Benchmark in the app list to try it, full screen with its
#   result on the screen.
# noto-fonts-cjk: Chinese, Japanese and Korean characters (web pages, messages, contact names)
#   instead of empty boxes; about 310 MB installed.
# gvfs-mtp, gvfs-smb: cameras and phones on USB, Windows and NAS shares in Files (Nautilus).
# exfatprogs, dosfstools: checking and formatting SD cards and USB sticks (the kernel reads both).
# less: the pager of journalctl, git and man; without it they print everything at once.
# All of them enable their services themselves.
packages=(base openssh sudo device-volla-ansuz hybris-tls-relocate
	phosh-plinius phoc-plinius stevia-plinius greetd which gnome-backgrounds gnome-console noto-fonts noto-fonts-emoji adwaita-fonts
	networkmanager wpa_supplicant-plinius dnsmasq iptables-legacy iw bluez bluez-utils bluebinder feedbackd vibrator-ff-bridge
	ofono-sailfish ofono-binder-plugin ofono2mm nfcd nfcd-binder-plugin gnss-nmea-bridge audio-hal-bridge
	sensor-proxy-bridge camera-hal-bridge snapshot-plinius phosh-tour-plinius gnome-usage-plinius gnome-keyring rtkit gnome-control-center-plinius
	gnome-online-accounts-plinius chatty-plinius libhandy-plinius gtk3-plinius
	plinius-kmod plinius-torch glibc-locales lpac plinius-wake plinius-assist plinius-sar plinius-datawatch plinius-kmsg cups-plinius
	plinius-pdf-printer plinius-remote biomd biomd-fprintd biomd-session plinius-udfps plinius-charge
	ofono-binder-plugin-ext-mtk plinius-pinguard plinius-software schwupp plinius-send-to-tv xdg-desktop-portal-wlr-plinius
	networkmanager-openvpn plinius-usb umtp-responder furios-gps-contribute
	plinius-health plinius-rollback plinius-mce plinius-face plinius-tflite plinius-security plinius-netlog
	plinius-ambient plinius-suspend plinius-dictate bluez-obex-plinius plinius-callfocus plinius-screentime plinius-satellite
	pipewire-audio-plinius plinius-ssh plinius-desktop plinius-cardshare plinius-swap plinius-login-keyring vim gnome-calls-plinius plinius-home flatpak
	plinius-crypt plinius-crypt-ramdisk plinius-vdec plinius-energy plinius-dnsfilter plinius-apparmor
	hybris-vulkan vkmark-plinius
	plinius-dictate-models plinius-kernel furios-audio-bt-reconnect biomd-pam libgbinder-tools
	android-headers mce-dev python-evdev plinius-greeter plinius-appaccess plinius-epo
	plinius-speech plinius-speech-voice-en-us-joe
	noto-fonts-cjk gvfs-mtp gvfs-smb exfatprogs dosfstools less)
# App selection as in the preview (image/mkpreview.sh): plinius-apps (calls, SMS, contacts; fixed)
# and recommended.list (removable in Software). snapshot, phosh-tour and gnome-usage come as
# snapshot-plinius, phosh-tour-plinius and gnome-usage-plinius above, which conflict with them;
# names already listed are skipped.
# Apps below "# Not preinstalled" are only offered in Software, the image leaves them out.
packages+=(plinius-apps)
while read -r app; do
	[[ $app == "# Not preinstalled"* ]] && break
	[[ -n $app && $app != \#* && $app != snapshot && $app != phosh-tour && $app != gnome-usage && " ${packages[*]} " != *" $app "* ]] && packages+=("$app")
done < "$PKGBUILDS/plinius-apps/recommended.list"
# Without the key in the image the device could not verify any update from [plinius]
if [[ -n $repo_url ]]; then
	descs=$(bsdtar -xOf "$LOCALREPO/plinius-local.db.tar.gz" '*/desc')
	grep -qx plinius-keyring <<< "$descs" ||
		die "plinius-keyring missing in the local repo (-r): run tools/keygen.sh, then tools/build.sh plinius-keyring"
	packages+=(plinius-keyring)
fi

work=$(mktemp -d "$BUILD/work-image.XXXXXX")
trap 'ns_rm "$work"' EXIT
mkdir -p "$work"/{upper,ovl,merged,target}
# The PIN's hash for chpasswd, readable only by the builder ($work is 0700 as well)
[[ -z $pin_hash ]] || (umask 077; printf 'plinius:%s\n' "$pin_hash" > "$work/pin-hash")
# Packages fetched from [plinius] (tools/repo-fetch.sh) come with signatures, which pacman checks
# against the keyring of the build chroot; it gets the key of plinius-keyring in the overlay only
cp "$PKGBUILDS/plinius-keyring/plinius.gpg" "$PKGBUILDS/plinius-keyring/plinius-trusted" "$work/"
# Before anything is installed: a kernel that does not fit should not cost a whole run
if [[ -n $android_img ]]; then
	# What Plinius Boot compares the vendor partitions of Volla OS with (checked against the pins above)
	for img in vendor vendor_dlkm; do
		src=$(dirname "$android_img")/$img.img
		[[ -f $src ]] || continue
		printf '%s %s %s\n' "$(awk -v n="android/$img.img" '$1 !~ /^#/ && $2 == n { print $1 }' \
			"$VOLLA_DIR/tools/ut-build.sha256")" "$(stat -c %s "$src")" "$img"
	done > "$work/vendor.sha256"
	"$VOLLA_DIR/tools/boot-files.sh" "$(dirname "$(dirname "$android_img")")" "$work/boot-files"
	# The same boot files with the password prompt of plinius-crypt (docs/PLAN-VERSCHLUESSELUNG.md):
	# its ramdisks replace UT's, kernel and boot.img stay. halium-rebuild in the system takes them as
	# the base for the prompt's look.
	[[ -n ${HAPTIC_FW:-} ]] || export VENDOR_IMG=$(dirname "$android_img")/vendor.img
	PKGS=$LOCALREPO "$VOLLA_DIR/tools/crypt-ramdisk.sh" "$work/boot-files" "$work/crypt-files"
	# UT's own images stay in ut/: the phone puts the prompt's ramdisk together from them after an
	# update of plinius-crypt-ramdisk (halium-rebuild); Plinius Boot reads only the top level
	mkdir -p "$work/boot-files/ut"
	cp "$work"/boot-files/{vendor_boot.img,init_boot.img} "$work/boot-files/ut/"
	cp "$work"/crypt-files/{vendor_boot.img,init_boot.img,initrd.cpio.gz} "$work/boot-files/"
	image_kernel "$work/boot-files" "$work"
fi

msg "Installing ${packages[*]}"
ns_run '
	lower=$1 work=$2 repo=$3 key=$4 android=$5 size=$6 out=$7 repo_url=$8 vendor_phone=$9; shift 9
	r=$work/merged t=$work/target
	mount -t overlay overlay -o "userxattr,lowerdir=$lower,upperdir=$work/upper,workdir=$work/ovl" "$r"
	chroot_mounts "$r"
	mkdir -p "$r/repo" "$r/target"
	mount --bind "$repo" "$r/repo"
	mount --bind "$t" "$r/target"
	sed -i "0,/^\[core\]/s//[plinius-local]\nSigLevel = Optional TrustAll\nServer = file:\/\/\/repo\n\n[core]/" \
		"$r/etc/pacman.conf"

	if compgen -G "$repo/*.sig" > /dev/null; then
		cp "$work/plinius.gpg" "$r/tmp/plinius.gpg"
		in_chroot "$r" pacman-key --add /tmp/plinius.gpg > /dev/null
		while IFS=: read -r fpr _; do
			if [[ $fpr =~ ^[0-9A-F]{40}$ ]]; then in_chroot "$r" pacman-key --lsign-key "$fpr" > /dev/null; fi
		done < "$work/plinius-trusted"
	fi
	install_root "$r" /target "$@"
	cp "$r/etc/pacman.d/mirrorlist" "$t/etc/pacman.d/mirrorlist"
	# Our repo before the ALARM repos (rule 5), signed only (rule 6). The key comes from
	# plinius-keyring; the device takes it into its keyring on the first start (image_first_boot).
	if [[ -n $repo_url ]]; then
		sed -i "0,/^\[core\]/s##[plinius]\nSigLevel = Required\nServer = $repo_url\n\n[core]#" \
			"$t/etc/pacman.conf"
	fi
	# Up to 10 downloads at once instead of the default 5
	sed -i "s/^#\?ParallelDownloads.*/ParallelDownloads = 10/" "$t/etc/pacman.conf"
	# The Halium kernel has no Landlock, so every pacman download would fail. Drop only the
	# filesystem part of the sandbox: the switch to the alpm user and the syscall filter stay.
	sed -i "s/^\[options\]/[options]\nDisableSandboxFilesystem/" "$t/etc/pacman.conf"

	# Device specific, not part of any package
	touch "$t/.writable_image"          # otherwise the Halium ramdisk mounts / read-only
	echo plinius > "$t/etc/hostname"
	# English (US) until the user picks a language in Settings
	echo LANG=en_US.UTF-8 > "$t/etc/locale.conf"
	# hostnamed does not detect the chassis under Halium; Phosh adapts to it
	echo CHASSIS=handset > "$t/etc/machine-info"
	# Groups found in /etc/group without asking systemd-userdbd as well (no systemd-homed here).
	# polkit looks up every group of the user for each check: 77 -> 39 ms per check.
	# (double quotes: this whole script is in single quotes for ns_run)
	sed -i "s/^group: files \[SUCCESS=merge\] systemd\$/group: files systemd/" "$t/etc/nsswitch.conf"
	if [[ -n $key ]]; then
		install -d -m700 "$t/root/.ssh"
		install -m600 "$key" "$t/root/.ssh/authorized_keys"
	fi
	mkdir -p "$t/var/lib/lxc/android"
	if [[ -n $android ]]; then
		cp "$android" "$t/var/lib/lxc/android/android-rootfs.img"
		for img in vendor vendor_dlkm system_dlkm; do
			src=$(dirname "$android")/$img.img
			# -V: these two come from the phone (Plinius Boot), system_dlkm belongs to the kernel
			[[ $vendor_phone == 1 && $img != system_dlkm ]] && continue
			[[ ! -f $src ]] || cp "$src" "$t/var/lib/lxc/android/"
		done
		install -Dm644 "$work/vendor.sha256" "$t/usr/share/plinius-boot/vendor.sha256"
		install -Dm644 -t "$t/usr/share/plinius-boot" "$work"/boot-files/*.img "$work"/boot-files/zImage \
			"$work"/boot-files/initrd.cpio.gz
		install -Dm644 -t "$t/usr/share/plinius-boot/ut" "$work"/boot-files/ut/*
		# The parts of the prompt in those ramdisks are the confirmed ones: they start, as the
		# installation shows, and an update of plinius-crypt-ramdisk compares against them
		install -Dm644 -t "$t/var/lib/plinius-crypt/halium/confirmed" "$work"/crypt-files/parts/*
	fi
	# The fallback kernel, kept as plinius-kernel-install keeps a release whose package is gone:
	# its files with its modules, named as previous (its first start makes the rest)
	if [[ -e $work/fallback-kernel.pkg ]]; then
		mkdir -p "$work/fallback"
		bsdtar -xf "$work/fallback-kernel.pkg" -C "$work/fallback" usr/lib/plinius
		for d in "$work"/fallback/usr/lib/plinius/kernel/*/; do
			rel=$(basename "$d")
			install -d -m 700 "$t/var/lib/plinius-kernel" "$t/var/lib/plinius-kernel/kept"
			cp -r "$d" "$t/var/lib/plinius-kernel/kept/$rel"
			if [[ -d $work/fallback/usr/lib/plinius/modules/$rel ]]; then
				cp -r "$work/fallback/usr/lib/plinius/modules/$rel" "$t/var/lib/plinius-kernel/kept/$rel/modules"
			fi
			echo "$rel" > "$t/var/lib/plinius-kernel/previous"
		done
		[[ -s $t/var/lib/plinius-kernel/previous ]] || { echo "Error: no kernel in the fallback package" >&2; exit 1; }
	fi
	in_chroot "$r" systemctl --root=/target enable sshd systemd-networkd NetworkManager \
		bluebinder bluetooth vibrator-ff-bridge ofono ModemManager nfcd \
		gnss-nmea-bridge sensor-proxy-bridge plinius-wake biomd systemd-timesyncd
	in_chroot "$r" systemctl --root=/target --global enable audio-hal-bridge camera-hal-bridge
	# Printing: CUPS starts on demand through its socket. avahi finds network printers and is
	# enabled as a service (not only its socket): CUPS reaches it over D-Bus, which activates the
	# alias dbus-org.freedesktop.Avahi.service that only "enable" of the service creates.
	in_chroot "$r" systemctl --root=/target enable cups.socket avahi-daemon.service
	# Nothing at boot waits for the network (docs/ROADMAP.md, start time)
	in_chroot "$r" systemctl --root=/target disable NetworkManager-wait-online systemd-networkd-wait-online

	# User for Phosh, shown as "John Doe" (Settings > Users > Name; the login name stays
	# plinius). UID 32011 as on Ubuntu Touch: 1000 and the following are Android AIDs
	# (1000 = system) shared with the container, a user with them would act as Android system.
	# aid_audio/aid_media: the vendor audio HAL in audio-hal-bridge opens the DSP and IPI nodes.
	# rfkill: the GKI kernel has no CONFIG_TMPFS_POSIX_ACL, so the uaccess ACL never reaches
	# /dev/rfkill. Without write access gsd-rfkill sees no Bluetooth and Phosh hides it.
	# privileged: nfcd lets only this group switch NFC (Settings, Phosh quick setting).
	in_chroot "$r/target" useradd -m -u 32011 -U -c "John Doe" -G wheel,video,audio,radio,aid_audio,aid_media,rfkill,privileged plinius
	# The PIN hash from a file on stdin: as an argument it would be visible in /proc/*/cmdline.
	# chpasswd -e writes it as it is, without PAM.
	if [[ -f $work/pin-hash ]]; then
		in_chroot "$r/target" chpasswd -e < "$work/pin-hash"
		rm -f "$work/pin-hash"
		grep -q "^plinius:[\$]y[\$]" "$t/etc/shadow" || { echo "Error: the PIN did not reach /etc/shadow" >&2; exit 1; }
	fi
	echo "%wheel ALL=(ALL:ALL) ALL" > "$t/etc/sudoers.d/wheel"
	chmod 0440 "$t/etc/sudoers.d/wheel"  # as sudo installs its own files (Lynis AUTH-9252)
	# greetd logs plinius straight into Phosh, which locks itself with the PIN. The variables a
	# display manager would take from phosh.desktop (without XDG_CURRENT_DESKTOP, apps with
	# OnlyShowIn=GNOME are missing). phoc renders on the GPU; its EGL setup comes from
	# device-volla-ansuz (system.conf.d, ld.so.preload).
	cat > "$t/etc/greetd/config.toml" <<-EOF
		[terminal]
		vt = 1

		[default_session]
		command = "agreety --cmd phosh-session"
		user = "greeter"

		[initial_session]
		command = "env XDG_SESSION_TYPE=wayland XDG_SESSION_DESKTOP=phosh XDG_CURRENT_DESKTOP=Phosh:GNOME phosh-session"
		user = "plinius"
	EOF
	in_chroot "$r" systemctl --root=/target enable greetd

	# /home comes from the data partition of this system. Without the generator being off, systemd
	# would mount the first partition of type "Linux /home" on the card, even that of another system.
	mkdir -p "$t/etc/systemd/system-generators"
	ln -s /dev/null "$t/etc/systemd/system-generators/systemd-gpt-auto-generator"
	in_chroot "$r" systemctl --root=/target enable home.mount

	# Own pacman keyring and machine ID per device, created on the first start
	image_first_boot "$r"

	# $t is a separate mount without the submounts (dev/proc/sys) of $r/target. The image holds
	# /etc/shadow: only the builder may read it (mkfs.ext4 -d keeps the modes of the files in it)
	rm -f "$out"
	umask 077
	mkfs.ext4 -q -L plinius-arch -d "$t" "$out" "$size"
' "$CHROOT" "$work" "$LOCALREPO" "$ssh_key" "$android_img" "$size" "$out" "$repo_url" "$vendor_from_phone" "${packages[@]}"

msg "Image done: $out"
[[ -n $repo_url ]] || msg "Note: without a repo (-r) our packages on the device get no updates"
[[ -n $android_img ]] || msg "Note: without an Android image (-a) there is no Android container"
[[ -n $pin_hash ]] || msg "Note: without -P the user plinius has no PIN; set it over SSH: passwd plinius"
[[ -n $ssh_key ]] || msg "Note: without -k root has no SSH key in the image"
(( ! vendor_from_phone )) || msg "Note: -V, vendor.img and vendor_dlkm.img come from the phone (Plinius Boot)"
