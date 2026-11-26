#!/bin/bash
# Prepares and uploads the downloads of the web server (docs/PLAN-IMAGE.md, part B): the image
# under images/, which Plinius Boot finds through images/latest.json, and Plinius Boot itself under
# downloads/. Everything is put together in build/downloads first, as the server will have it.
#
#   tools/downloads.sh image IMAGE.img [VERSION]
#       Checks the image (tools/image-check.sh) and boots it in QEMU (image/test-image.sh;
#       IMAGE_BOOT_TEST=off leaves that out and says so), packs it with zstd and writes latest.json:
#       {"version", "url", "size", "sha256"} as Plinius Boot reads it (latest_image), and
#       IMAGE.img.zst.sha256 for a check by hand. VERSION defaults to the date. The image
#       latest.json named before stays as the previous one, older ones leave.
#   tools/downloads.sh app FILE...
#       Puts Plinius Boot under downloads/, from its release (tools/flatpak.sh --release of
#       plinius-boot): the Flatpak as org.plinius.Boot-x86_64.flatpak and the archive as
#       plinius-boot.tar.gz, the names the homepage links, each also under its own name and with
#       a .sha256. The homepage offers the archive and names the Flatpak below it. For the
#       archive app.json says {"version", "url", "size", "sha256"} as latest.json does for the
#       image; the version is the newest release in the archive's metainfo. Its url is the
#       archive under the name with that version, plinius-boot-VERSION.tar.gz, which no later
#       release overwrites: whoever read app.json before an upload still gets the archive its
#       size and SHA-256 belong to, while plinius-boot.tar.gz already is the next one.
#   DOWNLOAD_TARGET=user@host:/var/www/plinius [DOWNLOAD_SSH_PORT=22] tools/downloads.sh upload [images|app]
#       Images first, latest.json after them (a phone's PC never finds a latest.json whose image
#       is not there yet), then the images latest.json no longer names leave; the screenshots
#       in images/ are not touched. Then downloads/. "images" or "app" uploads only that part;
#       without one, whatever build/downloads holds. Before the images it reads the server's
#       latest.json: when that names a newer image than build/downloads (an image uploaded from
#       elsewhere, or after this folder was prepared), it stops before anything is sent, since
#       the upload would put the older image back and remove the newer one.
#
# DOWNLOAD_URL (default https://plinius.cais.de) is where the server is reached: Plinius Boot
# takes only an image on the server of latest.json itself. ZSTD_LEVEL (default 15) is the
# compression level; no --long, so the window stays within what Python's zstd unpacks by
# default (Plinius Boot unpacks with compression.zstd).
set -euo pipefail
. "$(dirname "$0")/lib.sh"

out=$BUILD/downloads
base=${DOWNLOAD_URL:-https://plinius.cais.de}
[[ $base =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || die "DOWNLOAD_URL must be https://host: $base"

# The name rule of Plinius Boot (IMAGE_NAME_RE), for the packed file
name_ok() { [[ $1 =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.img\.zst$ ]]; }

# Prints the file name latest.json in $1 points at, if there is one
named_image() {
	[[ -f $1/latest.json ]] || return 0
	python3 -I -c 'import json, os, sys, urllib.parse
print(os.path.basename(urllib.parse.urlsplit(json.load(open(sys.argv[1]))["url"]).path))' "$1/latest.json"
}

do_image() {
	local img=${1:?usage: tools/downloads.sh image IMAGE.img [VERSION]} version=${2:-$(date +%Y.%m.%d)}
	local dir=$out/images name previous size sha f
	[[ -f $img ]] || die "$img missing"
	name=$(basename "$img").zst
	name_ok "$name" || die "$name is not a name Plinius Boot takes (letters, digits, . _ -, ending in .img.zst)"
	[[ $version =~ ^[A-Za-z0-9._-]{1,40}$ ]] || die "invalid version: $version"
	"$VOLLA_DIR/tools/image-check.sh" "$img" || die "$img failed the check, it is not offered"
	if [[ ${IMAGE_BOOT_TEST:-on} == off ]]; then
		msg "boot test left out (IMAGE_BOOT_TEST=off)"
	else
		"$VOLLA_DIR/image/test-image.sh" "$img" || die "$img failed the boot test, it is not offered"
	fi
	mkdir -p "$dir"
	previous=$(named_image "$dir")
	msg "Packing $img (zstd -$ZSTD_LEVEL)"
	zstd -q -f -T0 "-$ZSTD_LEVEL" -o "$dir/$name.part" "$img"
	zstd -q -t "$dir/$name.part" || die "the packed image does not unpack"
	mv -f "$dir/$name.part" "$dir/$name"
	chmod 644 "$dir/$name"
	size=$(stat -c %s "$dir/$name")
	sha=$(sha256sum < "$dir/$name")
	sha=${sha%% *}
	echo "$sha  $name" > "$dir/$name.sha256"
	python3 -I -c 'import json, sys
version, url, size, sha = sys.argv[1:5]
json.dump({"version": version, "url": url, "size": int(size), "sha256": sha}, sys.stdout, indent=2)
print()' "$version" "$base/images/$name" "$size" "$sha" > "$dir/latest.json.part"
	mv -f "$dir/latest.json.part" "$dir/latest.json"
	chmod 644 "$dir/$name.sha256" "$dir/latest.json"
	# Only the new image and the one before stay
	for f in "$dir"/*.img.zst "$dir"/*.img.zst.sha256; do
		[[ -e $f ]] || continue
		case ${f##*/} in "$name" | "$name.sha256") continue ;; esac
		[[ -n $previous ]] && case ${f##*/} in "$previous" | "$previous.sha256") continue ;; esac
		rm -f "$f"
	done
	msg "$name ($size bytes) in $dir, latest.json names version $version"
}

# The version of Plinius Boot in archive $1: the newest release its metainfo names
archive_version() {
	python3 -I -c 'import re, sys, tarfile
import xml.etree.ElementTree as ET
with tarfile.open(sys.argv[1], "r:gz") as tar:
    member = next(m for m in tar.getmembers() if m.isfile() and m.name.endswith("/org.plinius.Boot.metainfo.xml"))
    release = ET.parse(tar.extractfile(member)).getroot().find("releases/release")
version = release.get("version")
if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", version):
    raise SystemExit(1)
print(version)' "$1" 2>/dev/null
}

# The name the homepage links for a file of Plinius Boot; fails for any other file
linked_name() {
	if [[ $1 =~ ^org\.plinius\.Boot[A-Za-z0-9._-]*\.flatpak$ ]]; then
		echo org.plinius.Boot-x86_64.flatpak
	elif [[ $1 =~ ^plinius-boot[A-Za-z0-9._-]*\.tar\.gz$ ]]; then
		echo plinius-boot.tar.gz
	else
		return 1
	fi
}

do_app() {
	(($#)) || die "usage: tools/downloads.sh app FILE..."
	local dir=$out/downloads app name linked version= archive= versioned size sha
	# All of them checked first, so that a wrong one changes nothing
	for app in "$@"; do
		[[ -f $app ]] || die "$app missing"
		linked=$(linked_name "$(basename "$app")") ||
			die "$(basename "$app") is neither the Flatpak nor the archive of Plinius Boot"
		if [[ $linked == plinius-boot.tar.gz ]]; then
			version=$(archive_version "$app") || die "$(basename "$app") names no version of Plinius Boot"
			archive=$app
		fi
	done
	mkdir -p "$dir"
	for app in "$@"; do
		name=$(basename "$app")
		linked=$(linked_name "$name")
		install -m644 "$app" "$dir/$name"
		[[ $name == "$linked" ]] || install -m644 "$app" "$dir/$linked"
		(cd "$dir" && sha256sum "$linked" > "$linked.sha256")
		chmod 644 "$dir/$linked.sha256"
		msg "$name in $dir"
	done
	[[ -n $version ]] || return 0
	# The archive under its version, for app.json: never overwritten by a later release
	versioned=plinius-boot-$version.tar.gz
	install -m644 "$archive" "$dir/$versioned"
	(cd "$dir" && sha256sum "$versioned" > "$versioned.sha256")
	chmod 644 "$dir/$versioned.sha256"
	size=$(stat -c %s "$dir/$versioned")
	sha=$(sha256sum < "$dir/$versioned")
	sha=${sha%% *}
	python3 -I -c 'import json, sys
version, url, size, sha = sys.argv[1:5]
json.dump({"version": version, "url": url, "size": int(size), "sha256": sha}, sys.stdout, indent=2)
print()' "$version" "$base/downloads/$versioned" "$size" "$sha" > "$dir/app.json"
	chmod 644 "$dir/app.json"
	msg "app.json names Plinius Boot $version"
}

# The version a latest.json on stdin names (empty for none or an unreadable one)
json_version() {
	python3 -I -c 'import json, sys
try:
    version = json.load(sys.stdin)["version"]
except Exception:
    version = ""
print(version if isinstance(version, str) else "")'
}

# Stops when the server's latest.json names a newer image than the one in $out/images
check_server_image() {
	local host=${target%%:*} dir=${target#*:} remote status here there
	set +e
	remote=$($ssh_cmd "$host" "f=$(printf %q "$dir/images/latest.json"); [ ! -e \"\$f\" ] || cat \"\$f\"")
	status=$?
	set -e
	((status == 0)) || die "cannot read $target/images/latest.json (ssh exit $status), nothing was uploaded"
	[[ -n $remote ]] || return 0
	here=$(json_version < "$out/images/latest.json")
	there=$(json_version <<<"$remote")
	[[ -n $there && $there != "$here" ]] || return 0
	if [[ $(printf '%s\n%s\n' "$here" "$there" | sort -V | tail -n 1) == "$there" ]]; then
		die "the server offers image $there, newer than $here in $out/images: uploading would put the older" \
			"one back and remove the newer one. Nothing was uploaded. Prepare the newer image with" \
			"tools/downloads.sh image, or upload only the app: tools/downloads.sh upload app"
	fi
}

do_upload() {
	local part=${1:-} target=${DOWNLOAD_TARGET:?set DOWNLOAD_TARGET=user@host:/path/to/web/root} ssh_cmd options images
	local up_images=0 up_app=0
	case $part in
		"") [[ -f $out/images/latest.json ]] && up_images=1; [[ -d $out/downloads ]] && up_app=1 ;;
		images) [[ -f $out/images/latest.json ]] || die "no image in $out, run tools/downloads.sh image first"; up_images=1 ;;
		app) [[ -d $out/downloads ]] || die "no app in $out, run tools/downloads.sh app first"; up_app=1 ;;
		*) die "usage: tools/downloads.sh upload [images|app]" ;;
	esac
	ssh_cmd="ssh -p ${DOWNLOAD_SSH_PORT:-22} -o BatchMode=yes"
	options=(-rltz --mkpath --no-owner --no-group --chmod=D755,F644 --partial-dir=.upload -e "$ssh_cmd")
	# Only what this script makes; anything else in images/ (the screenshots) stays as it is
	images=(--include='*.img.zst' --include='*.img.zst.sha256' --include=latest.json --exclude='*')
	((up_images || up_app)) || die "nothing in $out, run tools/downloads.sh image or app first"
	# Checked before anything is sent: a refused image leaves the app unsent as well
	((up_images)) && check_server_image
	if ((up_images)); then
		msg "Images to $target/images"
		rsync "${options[@]}" --exclude=latest.json "${images[@]}" "$out/images/" "$target/images/"
		msg "latest.json"
		rsync "${options[@]}" --delay-updates "${images[@]}" "$out/images/" "$target/images/"
		msg "Removing images latest.json no longer names"
		rsync "${options[@]}" --delete-after "${images[@]}" "$out/images/" "$target/images/"
	fi
	if ((up_app)); then
		msg "Plinius Boot to $target/downloads"
		# All files appear together, app.json never names an archive not there yet. No --delete:
		# the archives of earlier versions stay for those who read an earlier app.json
		rsync "${options[@]}" --delay-updates "$out/downloads/" "$target/downloads/"
	fi
	msg "Done"
}

ZSTD_LEVEL=${ZSTD_LEVEL:-15}
[[ $ZSTD_LEVEL =~ ^([1-9]|1[0-9])$ ]] || die "ZSTD_LEVEL must be 1 to 19"
case ${1:-} in
	image) shift; do_image "$@" ;;
	app) shift; do_app "$@" ;;
	upload) shift; do_upload "$@" ;;
	*) die "usage: tools/downloads.sh image IMAGE.img [VERSION] | app FILE... | upload [images|app]" ;;
esac
