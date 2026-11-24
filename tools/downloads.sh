#!/bin/bash
# Prepares and uploads the downloads of the web server (docs/PLAN-IMAGE.md, part B): the image
# under images/, which Plinius Boot finds through images/latest.json, and Plinius Boot itself under
# downloads/. Everything is put together in build/downloads first, as the server will have it.
#
#   tools/downloads.sh image IMAGE.img [VERSION]
#       Checks the image (tools/image-check.sh), packs it with zstd and writes latest.json:
#       {"version", "url", "size", "sha256"} as Plinius Boot reads it (latest_image), and
#       IMAGE.img.zst.sha256 for a check by hand. VERSION defaults to the date. The image
#       latest.json named before stays as the previous one, older ones leave.
#   tools/downloads.sh app FILE.flatpak
#       Puts Plinius Boot's Flatpak (tools/flatpak.sh of plinius-boot) under downloads/, under
#       its own name and as org.plinius.Boot-x86_64.flatpak, the name the homepage links.
#   DOWNLOAD_TARGET=user@host:/var/www/plinius [DOWNLOAD_SSH_PORT=22] tools/downloads.sh upload
#       Images first, latest.json after them (a phone's PC never finds a latest.json whose image
#       is not there yet), then the images latest.json no longer names leave; the screenshots
#       in images/ are not touched. Then downloads/.
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

do_app() {
	local app=${1:?usage: tools/downloads.sh app FILE.flatpak} dir=$out/downloads name
	[[ -f $app ]] || die "$app missing"
	name=$(basename "$app")
	[[ $name =~ ^org\.plinius\.Boot[A-Za-z0-9._-]*\.flatpak$ ]] || die "$name is not a Flatpak of Plinius Boot"
	mkdir -p "$dir"
	install -m644 "$app" "$dir/$name"
	[[ $name == org.plinius.Boot-x86_64.flatpak ]] || install -m644 "$app" "$dir/org.plinius.Boot-x86_64.flatpak"
	(cd "$dir" && sha256sum org.plinius.Boot-x86_64.flatpak > org.plinius.Boot-x86_64.flatpak.sha256)
	chmod 644 "$dir/org.plinius.Boot-x86_64.flatpak.sha256"
	msg "$name in $dir"
}

do_upload() {
	local target=${DOWNLOAD_TARGET:?set DOWNLOAD_TARGET=user@host:/path/to/web/root} ssh_cmd options images
	ssh_cmd="ssh -p ${DOWNLOAD_SSH_PORT:-22} -o BatchMode=yes"
	options=(-rltz --mkpath --no-owner --no-group --chmod=D755,F644 --partial-dir=.upload -e "$ssh_cmd")
	# Only what this script makes; anything else in images/ (the screenshots) stays as it is
	images=(--include='*.img.zst' --include='*.img.zst.sha256' --include=latest.json --exclude='*')
	[[ -f $out/images/latest.json || -d $out/downloads ]] ||
		die "nothing in $out, run tools/downloads.sh image or app first"
	if [[ -f $out/images/latest.json ]]; then
		msg "Images to $target/images"
		rsync "${options[@]}" --exclude=latest.json "${images[@]}" "$out/images/" "$target/images/"
		msg "latest.json"
		rsync "${options[@]}" --delay-updates "${images[@]}" "$out/images/" "$target/images/"
		msg "Removing images latest.json no longer names"
		rsync "${options[@]}" --delete-after "${images[@]}" "$out/images/" "$target/images/"
	fi
	if [[ -d $out/downloads ]]; then
		msg "Plinius Boot to $target/downloads"
		rsync "${options[@]}" "$out/downloads/" "$target/downloads/"
	fi
	msg "Done"
}

ZSTD_LEVEL=${ZSTD_LEVEL:-15}
[[ $ZSTD_LEVEL =~ ^([1-9]|1[0-9])$ ]] || die "ZSTD_LEVEL must be 1 to 19"
case ${1:-} in
	image) shift; do_image "$@" ;;
	app) shift; do_app "$@" ;;
	upload) shift; do_upload ;;
	*) die "usage: tools/downloads.sh image IMAGE.img [VERSION] | app FILE.flatpak | upload" ;;
esac
