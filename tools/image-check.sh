#!/bin/bash
# Checks an image from image/mkimage.sh before it is offered for download (docs/PLAN-IMAGE.md,
# part B1): it must carry nothing of its builder and everything a stranger's phone needs.
# Reads the ext4 file system with debugfs, mounts nothing and changes nothing.
#
# Nothing of the builder: no SSH key for root or plinius, no SSH host keys, an empty machine-id,
# no pacman keyring (each phone makes its own), no package cache, no shell history, and no
# password: root and plinius stay locked, the phone asks for a PIN at its first start
# (plinius-greeter, the mark Plinius Boot looks for).
# Nothing that is Volla's and MediaTek's alone: vendor.img and vendor_dlkm.img stay out (mkimage.sh
# -V); vendor.sha256 names them as pinned, Plinius Boot copies them from Volla OS on the phone, and
# the file system keeps room for them.
# Everything a phone needs: [plinius] with signed packages only from an https server and
# plinius-keyring to check them, android-rootfs.img and system_dlkm.img and UT's init_boot.img as
# pinned in tools/ut-build.sha256, plinius-kernel's kernel in zImage and boot.img (there inside
# valid AVB data) with an older plinius-kernel kept as the fallback, and the boot images and
# ramdisk with the password prompt that Plinius Boot writes.
#
# Usage: tools/image-check.sh IMAGE.img
# Prints one line per check and exits 1 if any fails.
set -euo pipefail
. "$(dirname "$0")/lib.sh"

img=${1:?usage: tools/image-check.sh IMAGE.img}
[[ -f $img ]] || die "$img missing"
command -v debugfs > /dev/null || die "debugfs missing (package e2fsprogs)"
debugfs -R "stats" "$img" 2>/dev/null | grep -q "^Filesystem volume name:" ||
	die "$img is not an ext4 image (a .zst has to be unpacked first)"

failed=0
pass() { echo "ok: $*"; }
fail() { echo "FAIL: $*"; failed=1; }
# check CONDITION-COMMAND... -- TEXT
check() {
	local text=${*: -1}
	if "${@:1:$#-2}"; then pass "$text"; else fail "$text"; fi
}

# Lines "mode name size" of a directory, without . and .. (nothing if it does not exist)
entries() {
	debugfs -R "ls -p \"$1\"" "$img" 2>/dev/null |
		awk -F/ 'NF >= 7 && $6 != "." && $6 != ".." { print $3, $6, $7 }'
}
# exists PATH: there is something of that name
exists() {
	local dir=${1%/*} name=${1##*/}
	entries "${dir:-/}" | awk -v n="$name" '$2 == n { found = 1 } END { exit !found }'
}
absent() { ! exists "$1"; }
content() { debugfs -R "cat \"$1\"" "$img" 2>/dev/null; }
size_of() { entries "${1%/*}" | awk -v n="${1##*/}" '$2 == n { print $3 }'; }
# empty_dir PATH: a directory without entries, or no directory at all
empty_dir() { [[ -z $(entries "$1") ]]; }
# matches PATH PIN-FILE NAME: the file has the SHA-256 that PIN-FILE gives for NAME
matches() {
	local want got
	want=$(awk -v n="$3" '$1 !~ /^#/ && $2 == n { print $1 }' "$2")
	[[ $want =~ ^[0-9a-f]{64}$ ]] || return 1
	exists "$1" || return 1
	got=$(content "$1" | sha256sum)
	[[ ${got%% *} == "$want" ]]
}

# same PATH OTHER: both files have the same content
same() {
	local a b
	exists "$1" && exists "$2" || return 1
	a=$(content "$1" | sha256sum)
	b=$(content "$2" | sha256sum)
	[[ $a == "$b" ]]
}
# boot_holds BOOT KERNEL: the partition content BOOT starts with the boot image KERNEL, and its AVB
# footer names a vbmeta block whose hash descriptor has that image's size and digest
boot_holds() {
	local tmp ok=0
	exists "$1" && exists "$2" || return 1
	tmp=$(mktemp -d)
	content "$1" > "$tmp/part"
	content "$2" > "$tmp/kernel"
	python3 -I - "$tmp/part" "$tmp/kernel" <<-'EOF' || ok=1
	import hashlib, struct, sys
	part, img = (open(p, 'rb').read() for p in sys.argv[1:3])
	assert img and part[:len(img)] == img
	footer = part[-64:]
	assert footer[:4] == b'AVBf'
	_, _, size, voff, vsize = struct.unpack_from('>IIQQQ', footer, 4)
	assert size == len(img) and voff >= len(img)
	vb = part[voff:voff + vsize]
	assert vb[:4] == b'AVB0'
	auth, _ = struct.unpack_from('>QQ', vb, 12)
	doff, dsize = struct.unpack_from('>QQ', vb, 96)
	p, end, found = 256 + auth + doff, 256 + auth + doff + dsize, 0
	while p < end:
	    tag, following = struct.unpack_from('>QQ', vb, p)
	    if tag == 2:
	        algorithm = vb[p + 24:p + 56].split(b'\0')[0].decode()
	        name_len, salt_len, digest_len = struct.unpack_from('>III', vb, p + 56)
	        q = p + 132 + name_len
	        assert struct.unpack_from('>Q', vb, p + 16)[0] == len(img)
	        want = hashlib.new(algorithm, vb[q:q + salt_len] + img).digest()
	        assert vb[q + salt_len:q + salt_len + digest_len] == want
	        found += 1
	    p += 16 + following
	assert found == 1
	EOF
	rm -rf "$tmp"
	return "$ok"
}
# kept_fallback REL KERNEL: the release REL (not KERNEL) is kept with its files and modules, as
# image/mkimage.sh puts the fallback of image/fallback-kernel.sha256 there
kept_fallback() {
	local d=/var/lib/plinius-kernel/kept/$1 f
	[[ -n $1 && $1 != "$2" ]] || return 1
	for f in boot.img Image.gz system_dlkm.img modules; do exists "$d/$f" || return 1; done
}

# --- nothing of the builder ---
for home in /root /home/plinius; do
	check absent "$home/.ssh/authorized_keys" -- "no SSH key in $home"
	for history in .bash_history .python_history .lesshst .viminfo; do
		check absent "$home/$history" -- "no $history in $home"
	done
done
# Listings go into a variable first: grep -q stops at the first match, and with pipefail the
# SIGPIPE of debugfs behind it would count as failure (a large directory such as pacman's)
ssh_dir=$(entries /etc/ssh)
check eval '! grep -q " ssh_host_" <<< "$ssh_dir"' -- "no SSH host keys (sshd makes them on the phone)"
check eval '[[ $(size_of /etc/machine-id) == 0 ]]' -- "machine-id is there and empty"
check absent /etc/pacman.d/gnupg -- "no pacman keyring (plinius-pacman-init makes one per phone)"
check empty_dir /var/cache/pacman/pkg -- "no package cache"
shadow=$(content /etc/shadow)
for user in root plinius; do
	# Locked: the password field is empty of a hash, "!" or "*" at its start
	check eval "grep -Eq '^$user:[!*][^:\$]*:' <<< \"\$shadow\"" -- "$user has no password"
done
check exists /usr/lib/systemd/system/plinius-greeter-password.path \
	-- "the phone asks for a PIN at its first start (plinius-greeter)"

# --- nothing of Volla's and MediaTek's alone ---
pins=$(content /usr/share/plinius-boot/vendor.sha256)
need=0
for name in vendor vendor_dlkm; do
	check absent "/var/lib/lxc/android/$name.img" -- "no $name.img (Plinius Boot copies it from the phone)"
	want=$(awk -v n="android/$name.img" '$1 !~ /^#/ && $2 == n { print $1 }' "$VOLLA_DIR/tools/ut-build.sha256")
	line=$(awk -v n="$name" '$3 == n' <<< "$pins")
	check eval '[[ $want =~ ^[0-9a-f]{64}$ && ${line%% *} == "$want" && $line =~ ^[0-9a-f]{64}\ [0-9]+\ [a-z_]+$ ]]' \
		-- "vendor.sha256 names $name.img as pinned in tools/ut-build.sha256"
	size=$(awk '{ print $2 }' <<< "$line")
	need=$((need + ${size:-0}))
done
stats=$(debugfs -R stats "$img" 2>/dev/null)
free=$(( $(awk -F: '/^Free blocks:/ { print $2 }' <<< "$stats") * $(awk -F: '/^Block size:/ { print $2 }' <<< "$stats") ))
# Some room beyond the two files themselves: their blocks and the journal of the copy
check eval '(( free >= need + need / 50 + (64 << 20) ))' -- "room for vendor.img and vendor_dlkm.img"

# --- everything a phone needs ---
pacman_conf=$(content /etc/pacman.conf)
plinius=$(awk '/^\[/ { on = ($0 == "[plinius]") } on' <<< "$pacman_conf")
check eval 'grep -qx "SigLevel = Required" <<< "$plinius"' -- "[plinius] takes signed packages only"
check eval 'grep -Eqx "Server = https://[A-Za-z0-9._:/-]+" <<< "$plinius"' -- "[plinius] comes from an https server"
installed=$(entries /var/lib/pacman/local)
check eval 'grep -Eq " plinius-keyring-[^ ]+-[^ ]+ " <<< "$installed"' \
	-- "plinius-keyring is installed"
for name in android-rootfs system_dlkm; do
	check matches "/var/lib/lxc/android/$name.img" "$VOLLA_DIR/tools/ut-build.sha256" "android/$name.img" \
		-- "$name.img as pinned in tools/ut-build.sha256"
done
check matches /usr/share/plinius-boot/ut/init_boot.img "$VOLLA_DIR/tools/ut-build.sha256" init_boot.img \
	-- "UT's init_boot.img as pinned in tools/ut-build.sha256"
# UT's vendor_boot.img without systempart= on its command line (tools/boot-files.sh), so no pin
check exists /usr/share/plinius-boot/ut/vendor_boot.img -- "UT's vendor_boot.img for halium-rebuild"
for name in vendor_boot init_boot; do
	# The ones with the password prompt of plinius-crypt, which Plinius Boot writes
	check exists "/usr/share/plinius-boot/$name.img" -- "$name.img with the password prompt"
done
# The kernel: plinius-kernel's, the one release installed (image/mkimage.sh, image_kernel)
releases=$(entries /usr/lib/plinius/kernel | awk '$1 ~ /^04/ { print $2 }')
nrel=$(grep -c . <<< "$releases" || true)
check eval '[[ $nrel == 1 ]]' -- "one release of plinius-kernel installed"
kdir=/usr/lib/plinius/kernel/$(head -1 <<< "$releases")
check same /usr/share/plinius-boot/zImage "$kdir/Image.gz" -- "zImage is the kernel of plinius-kernel"
check boot_holds /usr/share/plinius-boot/boot.img "$kdir/boot.img" \
	-- "boot.img holds the kernel of plinius-kernel, its AVB data valid"
previous=$(content /var/lib/plinius-kernel/previous)
check kept_fallback "$previous" "${kdir##*/}" -- "an older plinius-kernel kept as the fallback"
check exists /usr/share/plinius-boot/initrd.cpio.gz -- "initrd.cpio.gz for a boot entry"
# The paths the services' sandboxes need (tools/unit-paths.py): a missing one stops the service
# at every start of a newly installed phone (status 226/NAMESPACE)
units=$(mktemp -d)
trap 'rm -rf "$units"' EXIT
mkdir -p "$units/usr/lib/systemd" "$units/etc/systemd" "$units/usr/lib" "$units/etc"
# debugfs cannot hand the files to their owners as an ordinary user and says so: only noise
for dir in /usr/lib/systemd/system /usr/lib/systemd/user /etc/systemd/system /etc/systemd/user \
	/usr/lib/tmpfiles.d /etc/tmpfiles.d; do
	debugfs -R "rdump \"$dir\" \"$units${dir%/*}\"" "$img" > /dev/null 2>&1 || true
done
unit_paths=$(python3 -I "$VOLLA_DIR/tools/unit-paths.py" "$units") || die "tools/unit-paths.py failed"
missing=
while read -r unit path; do
	[[ -n $path ]] || continue
	exists "$path" || missing+=${missing:+, }"$path ($unit)"
done <<< "$unit_paths"
if [[ -z $missing ]]; then
	pass "every path the services' sandboxes need is there"
else
	fail "every path the services' sandboxes need is there: missing $missing"
fi

if ((failed)); then
	echo "$img must not be offered for download" >&2
	exit 1
fi
msg "$img can be offered for download"
