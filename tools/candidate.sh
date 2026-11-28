#!/bin/bash
# Checks whether our packages still work with the current state of the mirrors, before an
# approval (docs/PAKETIERUNG.md, "Prüf-Pipeline"). Runs without the device, in a fresh overlay
# copy of the build chroot:
#   1. resolution: every package of the local repo (except plinius-freigabe) installs together
#      with the current mirror state, the image selection included
#   2. file conflicts with ALARM (tools/check-conflicts.py)
#   3. ABI: ldd -r on every ELF file of our packages (missing libraries, undefined symbols)
#   4. known couplings that ldd does not see: PipeWire (SPA interface), Python (site-packages
#      path of the minor version)
#   5. load tests: systemd-analyze verify on our units, import of our Python modules
#   6. report: build/candidate/<time>-<arch>.txt
# Findings someone has looked at and accepted (e.g. undefined symbols of Python extensions, which
# the interpreter provides) are listed in tools/candidate-known-<arch>.txt; only other findings
# turn the result red.
#
# Usage: [ARCH=x86_64] tools/candidate.sh [--accept]
#   --accept  take the findings of the last run as known (after reading its report)
# Exit status 0 = green: tools/freigabe.py may approve the state (with --force when only the
# mirror moved and no package needs a rebuild), 1 = red, see the report.
set -euo pipefail
. "$(dirname "$0")/lib.sh"

[[ -d $CHROOT/usr ]] || die "build chroot missing, run tools/chroot-init.sh first"
[[ -e $LOCALREPO/plinius-local.db ]] || die "no local repo in $LOCALREPO"
check_host

out=$BUILD/candidate
mkdir -p "$out"
known=$VOLLA_DIR/tools/candidate-known-$ARCH.txt
if [[ ${1:-} == --accept ]]; then
	# Without a run ls fails, and with pipefail so would the assignment (no message)
	last=$(ls -t "$out"/*-"$ARCH".findings 2>/dev/null | head -1) || true
	[[ -n $last ]] || die "no run to accept yet"
	cp "$last" "$known"
	msg "$(grep -c . "$known" || true) findings of ${last##*/} are known now ($known)"
	exit 0
fi
stamp=$(date +%Y%m%d-%H%M)
report=$out/$stamp-$ARCH.txt
findings=$out/$stamp-$ARCH.findings
work=$(mktemp -d "$BUILD/work-candidate.XXXXXX")
trap 'ns_rm "$work"' EXIT
mkdir -p "$work"/{upper,ovl,merged}

# Newest version of each of our packages, without debug packages and the approval itself
mapfile -t ours < <(bsdtar -xOf "$LOCALREPO/plinius-local.db.tar.gz" '*/desc' |
	awk '/^%NAME%/ { getline; print }' | sort -u | grep -vxE '.*-debug|plinius-freigabe')
# The rest of the image (image/mkimage.sh) comes from ALARM and pulls in what users have
image_pkgs=$(sed -n '/^packages=(/,/)$/p' "$VOLLA_DIR/image/mkimage.sh" | tr -d '()' |
	sed 's/^packages=//' | tr -s ' \t\n' '\n' | grep -v '^$')
# snapshot and phosh-tour come as our snapshot-plinius and phosh-tour-plinius (replaces=)
recommended=$(grep -v '^#' "$PKGBUILDS/plinius-apps/recommended.list" | grep -vxE 'snapshot|phosh-tour' || true)

{
	echo "Candidate check $stamp, $ARCH"
	echo "Own packages: ${#ours[@]}"
	echo
} > "$report"
: > "$findings"

# 2. File conflicts, on the host against the .files databases of the mirrors
msg "File conflicts"
ARCH=$ARCH python3 - "$VOLLA_DIR" "$findings" "$report" "${ours[@]}" <<'EOF'
import os, subprocess, sys
sys.path.insert(0, os.path.join(sys.argv[1], "tools"))
import freigabe
findings, report, names = sys.argv[2], sys.argv[3], set(sys.argv[4:])
for r in freigabe.REPOS:
    freigabe.fetch(r, "files", 86400)
files = [path for name, (path, _arch, _base) in freigabe.our_packages().items() if name in names]
res = subprocess.run([sys.executable, os.path.join(freigabe.ROOT, "tools", "check-conflicts.py"),
                      freigabe.CACHE] + sorted(files), capture_output=True, text=True)
with open(report, "a") as f:
    f.write("== 2. File conflicts\n" + res.stdout + res.stderr + "\n")
with open(findings, "a") as f:
    for line in res.stdout.splitlines():
        if "<->" in line:
            # Without the version, so the same conflict in a rebuilt package counts as known
            pkg, rest = line.split(": ", 1)
            f.write(f"conflict {pkg.rsplit('-', 3)[0]}: {rest.split('  <->  ')[0]}\n")
EOF

msg "Chroot: upgrade, install ${#ours[@]} own packages and the image selection"
ns_run '
	lower=$1 work=$2 repo=$3 arch=$4 report=$5 findings=$6; shift 6
	r=$work/merged
	mount -t overlay overlay -o "userxattr,lowerdir=$lower,upperdir=$work/upper,workdir=$work/ovl" "$r"
	chroot_mounts "$r"
	mkdir -p "$r/repo"
	mount --bind "$repo" "$r/repo"
	sed -i "0,/^\[core\]/s//[plinius-local]\nSigLevel = Optional TrustAll\nServer = file:\/\/\/repo\n\n[core]/" \
		"$r/etc/pacman.conf"
	# Deleting files of the lower layer fails in an unprivileged overlay; pacman then keeps two
	# database entries of a package. Only a current base chroot avoids that.
	if ! out=$(in_chroot "$r" pacman -Syu --noconfirm 2>&1 >/dev/null) || grep -q "could not remove" <<< "$out"; then
		echo "$out" >&2
		echo "The base chroot is too old for an upgrade in the overlay: run tools/chroot-init.sh first" >&2
		exit 1
	fi

	ours=() image=()
	while [[ $1 != -- ]]; do ours+=("$1"); shift; done; shift
	image=("$@")
	# The approval holds back exactly what is being checked here. Software and the device package
	# depend on it: count it as installed without installing it, as a new approval would be.
	free=(--assume-installed "plinius-freigabe=$(date +%Y%m%d.%H%M)-1")
	# The build chroot already has packages a fresh device does not (iptables, which the image
	# replaces with iptables-legacy): answer "remove" to such conflicts (ALPM_QUESTION_CONFLICT_PKG)
	free+=(--ask=4)

	# 1. Resolution: everything at once, like a device that has all of it. --print resolves
	# dependencies and conflicts without installing; the real install follows for steps 3 to 5.
	{
		echo "== 1. Resolution"
		if out=$(in_chroot "$r" pacman -S --print --print-format "%n" --needed "${free[@]}" "${ours[@]}" "${image[@]}" 2>&1); then
			echo "ok, $(wc -l <<< "$out") packages"
		else
			echo "$out"
			grep -E "^(error|:: )" <<< "$out" | sed "s/^/resolution /" >> "$findings"
		fi
		echo
	} >> "$report"
	# Install what resolves; a failing image package must not hide ABI problems of the others
	if ! out=$(in_chroot "$r" pacman -S --noconfirm --needed "${free[@]}" "${ours[@]}" "${image[@]}" 2>&1); then
		{ echo "== Install with the image selection failed:"; grep -E "^(error|:: )" <<< "$out"; echo; } >> "$report"
		if ! out=$(in_chroot "$r" pacman -S --noconfirm --needed "${free[@]}" "${ours[@]}" 2>&1); then
			{ echo "== Install of our packages failed, steps 3-5 skipped:"; grep -E "^(error|warning|:: )" <<< "$out"; } >> "$report"
			echo "install failed" >> "$findings"
			exit 0
		fi
	fi

	# 3. ABI: every dynamically linked ELF file of our packages. Kernel modules (.ko) and
	# Android binaries (bionic, linked against /system) are not for this linker.
	echo "== 3. ABI (ldd -r)" >> "$report"
	# Plugins take part of their symbols from the program that loads them, which ldd -r does
	# not know: those count as resolved if the program or a library it links exports them
	plugin_host() {
		case $1 in
			/usr/lib/ofono/plugins/*) echo /usr/bin/ofonod ;;
			/usr/lib/phosh/plugins/*) echo /usr/lib/phosh/phosh ;;
			*.abi3.so | *.cpython-*.so) (cd "$r" && ls usr/lib/libpython3.*.so.1.0 2>/dev/null | sed "s|^|/|" | head -1) ;;
		esac
	}
	exports=$work/exports
	mkdir -p "$exports"
	n=0
	while read -r pkg file; do
		[[ -f $r$file && ! -L $r$file && $file != *.ko* ]] || continue
		[[ $(head -c4 "$r$file" | od -An -c | tr -d " ") == 177ELF ]] || continue
		out=$(in_chroot "$r" ldd -r "$file" 2>&1) || true
		grep -q "not a dynamic executable\|statically linked" <<< "$out" && continue
		n=$((n + 1))
		host=$(plugin_host "$file")
		if [[ -n $host && -f $r$host ]]; then
			list=$exports/${host//\//_}
			if [[ ! -e $list ]]; then
				libs=$(in_chroot "$r" ldd "$host" | sed -nE "s|.* => (/[^ ]+) .*|\1|p")
				for f in "$host" $libs; do
					readelf --dyn-syms --wide "$r$f" |
						awk '\''$7 != "UND" && $8 != "" { sub(/@.*/, "", $8); print $8 }'\''
				done | sort -u > "$list"
			fi
			out=$(grep -vwF -f <(sed "s/^/undefined symbol: /" "$list") <<< "$out" || true)
		fi
		grep -E "not found|undefined symbol" <<< "$out" | sed -E "s/\t/ /g; s/ \(.*//" |
			sed "s|^|abi $pkg $file: |" | tee -a "$report" >> "$findings" || true
	done < <(in_chroot "$r" pacman -Ql "${ours[@]}")
	echo "$n ELF files checked" >> "$report"
	echo >> "$report"

	# 4. Couplings ldd cannot see
	{
		echo "== 4. Known couplings"
		py=$(in_chroot "$r" python3 -c "import sys; print(f\"python{sys.version_info[0]}.{sys.version_info[1]}\")")
		for pkg in "${ours[@]}"; do
			# Python modules installed for another minor version are invisible to the interpreter
			stale=$(in_chroot "$r" pacman -Qlq "$pkg" | grep -oE "^/usr/lib/python3\.[0-9]+/" | sort -u |
				grep -v "/$py/" || true)
			[[ -z $stale ]] || echo "coupling $pkg: modules in ${stale//$'\''\n'\''/ }, interpreter is $py, rebuild"
			# SPA plugins and PipeWire clients are built against one SPA interface version
			if in_chroot "$r" pacman -Qi "$pkg" | grep -qE "^Depends On .*\b(libpipewire|pipewire)\b"; then
				f=$(in_chroot "$r" pacman -Sp --print-format %f "$pkg")
				built=$(bsdtar -xOf "$repo/$f" .BUILDINFO |
					sed -nE "s/^installed = pipewire-([^-]+-[^-]+)-[^-]+$/\1/p")
				now=$(in_chroot "$r" pacman -Q pipewire | cut -d" " -f2)
				[[ -z $built || $built == "$now" ]] ||
					echo "coupling $pkg: built against pipewire $built, now $now, rebuild"
			fi
		done | tee -a "$findings"
		echo
	} >> "$report"

	# 5. Load tests without hardware
	{
		echo "== 5. Load tests"
		units=$(in_chroot "$r" pacman -Qlq "${ours[@]}" |
			grep -E "^/usr/lib/systemd/(system|user)/[^/]+\.(service|socket|timer|path)$" || true)
		for u in $units; do
			# Only problems of the unit itself, not of units it pulls in from ALARM
			out=$(in_chroot "$r" systemd-analyze verify --man=no "$u" 2>&1 | grep -F "${u##*/}" || true)
			[[ -z $out ]] || sed "s|^|unit |" <<< "$out" | tee -a "$findings"
		done
		echo "$(wc -w <<< "$units") units verified"
		mods=$(in_chroot "$r" pacman -Qlq "${ours[@]}" |
			sed -nE "s|^/usr/lib/python3\.[0-9]+/site-packages/([A-Za-z_][A-Za-z0-9_]*)/__init__\.py$|\1|p" | sort -u)
		for m in $mods; do
			out=$(in_chroot "$r" timeout 60 python3 -c "import $m" 2>&1 | tail -1) ||
				echo "python import $m: $out" | tee -a "$findings"
		done
		echo "$(wc -w <<< "$mods") Python modules imported"
		echo
	} >> "$report"
' "$CHROOT" "$work" "$LOCALREPO" "$ARCH" "$report" "$findings" "${ours[@]}" -- $image_pkgs $recommended

# 6. Result: findings that are not known yet
sort -u -o "$findings" "$findings"
[[ -e $known ]] || : > "$known"
new=$(comm -23 "$findings" <(sort -u "$known"))
{
	echo "== Result"
	echo "Findings: $(grep -c . "$findings" || true), of these known: $(comm -12 "$findings" <(sort -u "$known") | grep -c . || true)"
	if [[ -n $new ]]; then
		echo "NEW (fix, or accept with tools/candidate.sh --accept after reading this report):"
		echo "$new"
	else
		echo "green: nothing new. Next: tools/freigabe.py (--force if only the mirror moved)"
	fi
} >> "$report"
msg "Report: $report"
sed -n '/^== Result/,$p' "$report" >&2
[[ -z $new ]]
