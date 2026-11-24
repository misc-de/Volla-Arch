#!/bin/bash
# Builds a preview for QEMU: the same Arch with current Phosh, but with a regular
# kernel and Mesa instead of Halium. Shows the UI, apps and package state, not the
# Plinius hardware. Start it with image/run-preview.sh.
#   ARCH=aarch64 (default): ALARM packages as on the device, emulated (slow)
#   ARCH=x86_64: Arch packages, near-native speed with KVM
#
# Usage: image/mkpreview.sh [-s size] [-o directory] [-d]
#   -s  image size (default: 12G)
#   -o  output directory (default: build/preview, build/preview-$ARCH for other architectures)
#   -d  debug: Wayland trace for phosh (WAYLAND_DEBUG=client), journal without rate limit
# PREVIEW_REPO_PORT: port of the local repo as seen from the VM (default 45461, see run-preview.sh)
# User "plinius", PIN/password 1234 - only for this VM, never for the device.
set -euo pipefail
. "$(dirname "$0")/../tools/lib.sh"

size=12G
out=$BUILD/preview
[[ $ARCH == aarch64 ]] || out=$BUILD/preview-$ARCH
debug=0
while getopts s:o:d opt; do
	case $opt in
		s) size=$OPTARG ;;
		o) out=$OPTARG ;;
		d) debug=1 ;;
		*) exit 2 ;;
	esac
done
[[ -d $CHROOT/usr ]] || die "build chroot missing, run tools/chroot-init.sh first"
[[ -e $LOCALREPO/plinius-local.db ]] || die "local repo empty, run tools/build.sh phoc-plinius first"
check_host

mkdir -p "$out"

case $ARCH in
	aarch64) kernel_pkg=linux-aarch64 kernel_file=/boot/Image ;;
	x86_64) kernel_pkg=linux kernel_file=/boot/vmlinuz-linux ;;
esac

packages=(
	base "$kernel_pkg" mkinitcpio sudo networkmanager greetd
	# Stevia instead of squeekboard: actively developed, word suggestions, per-app ignore-activation;
	# in the glass look of the password prompt (pkgbuilds/stevia-plinius)
	phosh phosh-mobile-settings stevia-plinius
	# phoc with embedded, patched wlroots (pkgbuilds/phoc-plinius) instead of the ALARM phoc
	phoc-plinius
	# GNOME Settings including the Plinius settings (pkgbuilds/gnome-control-center-plinius)
	gnome-control-center-plinius
	# phosh-session calls "which systemd-cat", but the phosh package does not depend on
	# which; without which the phoc output is lost. Wallpaper from gnome-backgrounds.
	which gnome-backgrounds
	# Optional full-disk encryption (pkgbuilds/plinius-crypt, buffybox)
	plinius-crypt buffybox
	# App selection as on the device: plinius-apps (fixed) and recommended.list (recommended, removable)
	plinius-apps
	# (without the apps below "# Not preinstalled", which are only offered)
	$(sed '/^# Not preinstalled/q' "$PKGBUILDS/plinius-apps/recommended.list" | grep -v -e "^#" -e "^$")
	# German, matching locale and keyboard below; Arch ships Firefox languages separately
	firefox-i18n-de
	mesa noto-fonts noto-fonts-emoji adwaita-fonts
)

work=$(mktemp -d "$BUILD/work-preview.XXXXXX")
trap 'ns_rm "$work"' EXIT
mkdir -p "$work"/{upper,ovl,merged,target}

msg "Installing ${#packages[@]} packages plus dependencies (takes a while under qemu)"
ns_run '
	lower=$1 work=$2 size=$3 out=$4 debug=$5 repo=$6 kernel_file=$7; shift 7
	r=$work/merged t=$work/target
	mount -t overlay overlay -o "userxattr,lowerdir=$lower,upperdir=$work/upper,workdir=$work/ovl" "$r"
	chroot_mounts "$r"
	mkdir -p "$r/target" "$r/repo"
	mount --bind "$t" "$r/target"
	mount --bind "$repo" "$r/repo"
	sed -i "0,/^\[core\]/s//[plinius-local]\nSigLevel = Optional TrustAll\nServer = file:\/\/\/repo\n\n[core]/" \
		"$r/etc/pacman.conf"

	install_root "$r" /target "$@"
	cp "$r/etc/pacman.d/mirrorlist" "$t/etc/pacman.d/mirrorlist"
	# Our packages (including new approvals, plinius-freigabe) arrive via pacman as on the device:
	# run-preview.sh serves the local repo over HTTP, 10.0.2.2 is the host in the QEMU network.
	sed -i "0,/^\\[core\\]/s//[plinius-local]\\nSigLevel = Optional TrustAll\\nServer = http:\\/\\/10.0.2.2:${PREVIEW_REPO_PORT:-45461}\\n\\n[core]/" \
		"$t/etc/pacman.conf"
	# Up to 10 downloads at once instead of the default 5, as in image/mkimage.sh
	sed -i "s/^#\?ParallelDownloads.*/ParallelDownloads = 10/" "$t/etc/pacman.conf"

	echo plinius-preview > "$t/etc/hostname"
	in_chroot "$r/target" useradd -m -u 1000 -c "John Doe" -G wheel,video,input,audio plinius
	echo "plinius:1234" | in_chroot "$r/target" chpasswd
	echo "%wheel ALL=(ALL:ALL) ALL" > "$t/etc/sudoers.d/wheel"
	chmod 0440 "$t/etc/sudoers.d/wheel"  # as sudo installs its own files (Lynis AUTH-9252)

	# VM only: phoc reports every input device you can type or click with in QEMU (tablet,
	# multitouch) as a pointer, and Phosh takes the output "Virtual-1" for an external monitor.
	# Phosh then switches to docked mode (windows not maximized, no on-screen keyboard,
	# docs/GRAFIK.md). So the phone settings are pinned via a dconf lockdown.
	mkdir -p "$t/etc/dconf/profile" "$t/etc/dconf/db/local.d/locks"
	printf "user-db:user\nsystem-db:local\n" > "$t/etc/dconf/profile/user"
	printf "%s\n" "[sm/puri/phoc]" "auto-maximize=true" "" \
		"[org/gnome/desktop/a11y/applications]" "screen-keyboard-enabled=true" "" \
		"[org/gnome/desktop/wm/preferences]" \
		> "$t/etc/dconf/db/local.d/00-plinius-preview"
	printf "button-layout=\047appmenu:\047\n" >> "$t/etc/dconf/db/local.d/00-plinius-preview"
	printf "%s\n" /sm/puri/phoc/auto-maximize \
		/org/gnome/desktop/a11y/applications/screen-keyboard-enabled \
		/org/gnome/desktop/wm/preferences/button-layout \
		> "$t/etc/dconf/db/local.d/locks/00-plinius-preview"
	in_chroot "$r/target" dconf update

	# VM only: with virgl QEMU shows the hardware cursor mirrored; so phoc draws it itself
	echo "WLR_NO_HARDWARE_CURSORS=1" >> "$t/etc/environment"

	# German, Europe/Berlin, German keyboard (console, Phosh and Stevia)
	ln -sf /usr/share/zoneinfo/Europe/Berlin "$t/etc/localtime"
	sed -i "s/^#de_DE.UTF-8 UTF-8/de_DE.UTF-8 UTF-8/" "$t/etc/locale.gen"
	in_chroot "$r/target" locale-gen >/dev/null
	echo LANG=de_DE.UTF-8 > "$t/etc/locale.conf"
	echo KEYMAP=de-latin1 > "$t/etc/vconsole.conf"
	printf "[org.gnome.desktop.input-sources]\nsources=[(\047xkb\047, \047de\047)]\n" \
		> "$t/usr/share/glib-2.0/schemas/90-plinius-preview.gschema.override"
	# As on the device: phone chassis and on-screen keyboard enabled (otherwise Phosh does not show it)
	echo CHASSIS=handset > "$t/etc/machine-info"
	printf "[org.gnome.desktop.a11y.applications]\nscreen-keyboard-enabled=true\n" \
		>> "$t/usr/share/glib-2.0/schemas/90-plinius-preview.gschema.override"
	# VM only: Stevia stays hidden as long as it sees a hardware keyboard (virtio-keyboard)
	printf "[mobi.phosh.osk]\nignore-hw-keyboards=true\n" \
		>> "$t/usr/share/glib-2.0/schemas/90-plinius-preview.gschema.override"
	in_chroot "$r/target" glib-compile-schemas /usr/share/glib-2.0/schemas

	# App grid: hide the Tour (still starts on first login via autostart), the Qt test programs
	# from v4l-utils (dependency of ffmpeg), lstopo from hwloc (appstream > libblake3 > onetbb),
	# Evince (dependency of phosh, duplicate next to Papers) and the print settings of
	# system-config-printer (Settings manages printers). Copies with NoDisplay in /usr/local
	# take precedence per XDG, the package files stay untouched.
	mkdir -p "$t/usr/local/share/applications"
	for app in mobi.phosh.PhoshTour qv4l2 qvidcap lstopo org.gnome.Evince system-config-printer; do
		sed "/^\[Desktop Entry\]/a NoDisplay=true" "$t/usr/share/applications/$app.desktop" \
			> "$t/usr/local/share/applications/$app.desktop"
	done
	# Papers opens documents; without a default GLib would pick Firefox for PDF. TIFF stays with Loupe.
	mime=$(sed -n "s/^MimeType=//p" "$t/usr/share/applications/org.gnome.Papers.desktop")
	{
		echo "[Default Applications]"
		tr ";" "\n" <<< "$mime" | grep -v -e "^$" -e "^image/tiff$" | sed "s/$/=org.gnome.Papers.desktop/"
	} > "$t/etc/xdg/mimeapps.list"

	# greetd logs "plinius" straight into Phosh; Phosh locks itself with the PIN.
	# A display manager would otherwise set these variables from phosh.desktop (DesktopNames=Phosh;GNOME;);
	# without XDG_CURRENT_DESKTOP, apps with OnlyShowIn=GNOME are missing, e.g. Settings.
	cat > "$t/etc/greetd/config.toml" <<-EOF
		[terminal]
		vt = 1

		[default_session]
		command = "agreety --cmd \"env XDG_SESSION_TYPE=wayland XDG_SESSION_DESKTOP=phosh XDG_CURRENT_DESKTOP=Phosh:GNOME phosh-session\""
		user = "greeter"

		[initial_session]
		command = "env XDG_SESSION_TYPE=wayland XDG_SESSION_DESKTOP=phosh XDG_CURRENT_DESKTOP=Phosh:GNOME phosh-session"
		user = "plinius"
	EOF
	in_chroot "$r" systemctl --root=/target enable greetd NetworkManager
	# Printing: CUPS on demand via its socket. avahi (printer discovery) as a service, because CUPS
	# reaches it over D-Bus through the alias dbus-org.freedesktop.Avahi.service, which only
	# "enable" of the service creates; the socket alone left discovery without a daemon.
	in_chroot "$r" systemctl --root=/target enable cups.socket avahi-daemon.service

	if [[ $debug == 1 ]]; then
		mkdir -p "$t/etc/systemd/user/mobi.phosh.Shell.service.d" "$t/etc/systemd/journald.conf.d"
		printf "[Service]\nEnvironment=WAYLAND_DEBUG=client\n" \
			> "$t/etc/systemd/user/mobi.phosh.Shell.service.d/50-wayland-debug.conf"
		printf "[Journal]\nRateLimitBurst=0\n" > "$t/etc/systemd/journald.conf.d/50-debug.conf"
	fi

	# initramfs with busybox init and the plinius-crypt hook (configuration from the package);
	# without autodetect, which in the build chroot would pick the modules of the build host
	kver=$(ls "$t/usr/lib/modules" | head -n 1)
	in_chroot "$r/target" mkinitcpio -c /usr/share/plinius-crypt/mkinitcpio.conf \
		-k "$kver" -S autodetect -g /boot/initramfs-preview.img
	cp "$t$kernel_file" "$out/kernel"
	cp "$t/boot/initramfs-preview.img" "$out/"

	# As on the device: own pacman keyring and machine ID, created on the first start
	image_first_boot "$r"

	# $t is a separate mount without the submounts (dev/proc/sys) of $r/target
	rm -f "$out/preview.img"
	mkfs.ext4 -q -L plinius-preview -d "$t" "$out/preview.img" "$size"
' "$CHROOT" "$work" "$size" "$out" "$debug" "$LOCALREPO" "$kernel_file" "${packages[@]}"

msg "Preview done: $out (start with ARCH=$ARCH image/run-preview.sh)"
