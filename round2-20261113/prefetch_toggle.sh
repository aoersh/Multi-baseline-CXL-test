#!/usr/bin/env bash
# Decisive test: are the sub-32-cycle samples caused by the hardware prefetchers?
#
# Reads MSR 0x1A4 (MSR_PREFETCH_CONTROL on Intel), which controls:
#   bit0 L2 hardware prefetcher      bit1 L2 adjacent cacheline prefetcher
#   bit2 DCU prefetcher              bit3 DCU IP prefetcher
# Sets/clears it on every CPU, runs the measurement, then ALWAYS restores the
# original value (trap on EXIT/INT/TERM).
#
# Usage: sudo bash prefetch_toggle.sh <state> <cpu> <memnod> <label>
#        state = orig | off      (off = disable all four prefetchers)
#
# Read-only parts need no privilege; only the MSR writes do.
set -uo pipefail

MSR=0x1a4
SKIP=$((MSR / 8))

if [[ ${1:-} == --help ]]; then
    sed -n '2,15p' "$0"
    exit 0
fi
if (( EUID != 0 )); then
    echo "Run as: sudo bash $0 <orig|off> <cpu> <memnod> <label>" >&2
    exit 1
fi

state=${1:?state required: orig|off}
cpu=${2:?cpu required}
memnod=${3:?memnod required}
label=${4:?label required}

MLC=/home/hjy/projects/HotPageScheduling/third_party/intel-mlc/Linux/mlc
CHASE=/home/hjy/tools/experiments/cxl-tail-20261109/round2-20261113/chase
outdir=/home/hjy/tools/experiments/cxl-tail-20261109/round2-20261113/runs
mkdir -p "$outdir"
res="$outdir/prefetch-$label"
rm -rf "$res"; mkdir -p "$res"

ncpu=$(nproc)
declare -a saved

read_msr() { dd if="/dev/cpu/$1/msr" bs=8 skip="$SKIP" count=1 2>/dev/null | od -An -tx8 | tr -d ' \n'; }
write_msr() { printf "$(printf '\\x%02x\\x%02x\\x%02x\\x%02x\\x%02x\\x%02x\\x%02x\\x%02x' \
    $(( $2       & 255)) $(( ($2>>8)  & 255)) $(( ($2>>16) & 255)) $(( ($2>>24) & 255)) \
    $(( ($2>>32) & 255)) $(( ($2>>40) & 255)) $(( ($2>>48) & 255)) $(( ($2>>56) & 255)))" \
    | dd of="/dev/cpu/$1/msr" bs=8 seek="$SKIP" count=1 conv=notrunc 2>/dev/null; }

restore() {
    local i
    for i in "${!saved[@]}"; do
        [[ -n ${saved[$i]:-} ]] && write_msr "$i" "${saved[$i]}"
    done
    echo "restored prefetch MSR on $ncpu CPUs"
}
trap restore EXIT INT TERM

: > "$res/msr-before.txt"
for (( i = 0; i < ncpu; i++ )); do
    v=$(read_msr "$i")
    [[ -z $v ]] && v=0
    saved[$i]=$((16#$v))
    echo "cpu$i 0x$v" >> "$res/msr-before.txt"
done

if [[ $state == off ]]; then
    for (( i = 0; i < ncpu; i++ )); do
        write_msr "$i" $(( saved[$i] | 0xF ))
    done
fi

: > "$res/msr-active.txt"
for c in 0 "$cpu" 16; do
    echo "cpu$c 0x$(read_msr $c)" >> "$res/msr-active.txt" 2>/dev/null
done

# --- measurement: MLC histogram + independent per-access timer, same condition
numactl --physcpubind="$cpu" --membind="$memnod" \
    "$MLC" --idle_latency -e -r -c"$cpu" -j"$memnod" -b256m -t3 \
    -f4 --lat_hist_max_us=100 > "$res/mlc.txt" 2>&1
numactl --physcpubind="$cpu" --membind="$memnod" \
    "$CHASE" --size-mb 256 --stride 4096 --samples 2000000 \
    --label "$res/chase" > "$res/chase.txt" 2>&1

chown -R -- "${SUDO_UID:-0}:${SUDO_GID:-0}" "$res" 2>/dev/null || true
grep -E 'iteration took|^\[000-015\]|^\[016-031\]|^\[032-047\]' "$res/mlc.txt" \
    | sed 's/^/  mlc: /'
grep -E '^# summary' "$res/chase.txt" | sed 's/^/chase: /'
echo "results: $res"
