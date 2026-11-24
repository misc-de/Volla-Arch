#!/bin/bash
# Runs the host-side tests under tests/ (and the C tests of the packages that have a check()).
# Builds no package and touches neither the phone nor the running system.
#
# Usage: tools/run-tests.sh [--quick] [--coverage] [--jobs N] [PYTEST ARGS...]
#   --quick      leave out tests marked slow
#   --jobs N     N pytest processes side by side, each on a group of test files of about the same
#                running time (tools/test-shards.py; times of the last run in
#                build/test-durations.json); default: half the CPUs, --jobs 1 runs one process
#   --coverage   line coverage of the Python scripts and tools (needs python-coverage) and of
#                the C programs the tests compile (gcov, built with -O0 then), reports in
#                build/coverage/ (report.txt Python, c-report.txt C)
set -euo pipefail
cd "$(dirname "$0")/.."

args=()
paths=()
coverage=0
jobs=$(($(nproc) / 2))
next_is_jobs=0
for a in "$@"; do
	if ((next_is_jobs)); then
		jobs=$a
		next_is_jobs=0
		continue
	fi
	case $a in
	--quick) args+=(-m "not slow") ;;
	--coverage) coverage=1 ;;
	--jobs) next_is_jobs=1 ;;
	--jobs=*) jobs=${a#--jobs=} ;;
	-*) args+=("$a") ;;
	*) if [[ -e ${a%%::*} ]]; then paths+=("$a"); else args+=("$a"); fi ;;
	esac
done
((jobs >= 1)) || jobs=1

# One pytest, or one per group of test files side by side. Each group gets a short base
# directory of its own (Unix socket paths in it must stay below 108 bytes) and a JUnit report,
# from which the files' times are kept for the next split.
work=
run_pytest() {
	local -a runner=(python3 -m pytest)
	((coverage)) && runner=(python3 -m coverage run --rcfile=tools/coverage.ini -m pytest)
	if ((jobs == 1)); then
		"${runner[@]}" "${args[@]}" "${paths[@]}"
		return
	fi
	local groups=() pids=() status=0 i
	work=$(mktemp -d /tmp/plinius-tests.XXXXXX)
	mapfile -t groups < <(python3 tools/test-shards.py "$jobs" "${paths[@]}")
	for i in "${!groups[@]}"; do
		read -ra files <<<"${groups[$i]}"
		"${runner[@]}" "${args[@]}" -q --basetemp="$work/$i" --junitxml="$work/$i.xml" \
			"${files[@]}" >"$work/$i.log" 2>&1 &
		pids+=($!)
	done
	for i in "${!pids[@]}"; do
		wait "${pids[$i]}" || status=1
	done
	# The failures and the summary line of every group, the full output in the logs
	for i in "${!groups[@]}"; do
		grep -E '^(FAILED|ERROR|XPASS) ' "$work/$i.log" || true
	done
	for i in "${!groups[@]}"; do
		printf 'group %s: %s\n' "$i" "$(tail -n 1 "$work/$i.log")"
	done
	python3 tools/test-shards.py --record "$work"/*.xml || true
	if ((status)); then
		echo "Logs of the groups: $work/*.log"
	elif ! ((coverage)); then  # with --coverage the C counters are still to be read
		command rm -rf "$work"
	fi
	return $status
}

export PYTHONDONTWRITEBYTECODE=1
if ((coverage)); then
	mkdir -p build/coverage
	# Absolute: the programs started by the tests run in their own directories
	export COVERAGE_FILE=$PWD/build/coverage/.coverage
	# C: a fresh directory for the list of builds; their counters sit next to them in pytest's
	# tmp_path until the report below is written
	export PLINIUS_TEST_GCOV=build/coverage/gcov
	command rm -rf "$PLINIUS_TEST_GCOV"
	mkdir -p "$PLINIUS_TEST_GCOV"
	PLINIUS_TEST_GCOV=$(realpath "$PLINIUS_TEST_GCOV")
	command rm -f build/coverage/.coverage build/coverage/.coverage.*
	run_pytest || status=$?
	python3 -m coverage combine --rcfile=tools/coverage.ini -q || true
	python3 -m coverage report --rcfile=tools/coverage.ini --skip-empty --sort=cover \
		>build/coverage/report.txt || true
	python3 -m coverage html --rcfile=tools/coverage.ini -q -d build/coverage/html --skip-empty || true
	tail -n 1 build/coverage/report.txt || true
	python3 tools/c-coverage.py "$PLINIUS_TEST_GCOV" build/coverage/c-report.txt \
		--json build/coverage/c-coverage.json || true
	[[ -n $work && ${status:-0} == 0 ]] && command rm -rf "$work"
	echo "Coverage report: build/coverage/report.txt, build/coverage/html/index.html, build/coverage/c-report.txt"
	exit "${status:-0}"
fi
run_pytest
