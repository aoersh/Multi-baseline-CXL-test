#!/usr/bin/env python3
"""Per-access latency distribution via PEBS, restricted to the measurement window.

Why this is needed: multichase times a whole round of K interleaved loads, so
its "round time" is approximately the max of K concurrent latencies, not a
single access's latency. To separate "one access got slower" from "we waited
for the slowest of K", we need per-access samples.

rdtscp-per-access would work but serialises the lanes and destroys MLP, so the
only instrument left is PEBS. PEBS samples the whole process including the
init phase (which performs tens of millions of stores), so every sample is
filtered against the measurement window that multichase now prints.

CDF construction: repeat the run at several ldlat thresholds and count
in-window samples. A load is recorded only if its latency >= ldlat cycles, so
count(ldlat) = P(latency >= ldlat) * (recorded set). Comparing counts at the
same sample period across thresholds yields the tail shape.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MC = ROOT / 'multichase'


def run_one(k, ldlat, size_mb, stride, samples, period, node, cpu):
    data = Path(f'/tmp/pa-{k}-{ldlat}.data')
    out = subprocess.run(
        ['perf', 'record', '-q', f'-e', f'cpu/mem-loads,ldlat={ldlat}/pp',
         '-c', str(period), '-o', str(data), '--',
         'numactl', f'--physcpubind={cpu}', f'--membind={node}',
         str(MC), '--size-mb', str(size_mb), '--stride', str(stride),
         '--k', str(k), '--samples', str(samples), '--label', '/tmp/pa'],
        capture_output=True, text=True, timeout=900)
    win = None
    for line in out.stdout.splitlines():
        if line.startswith('# window'):
            f = line.split()
            win = (float(f[2]), float(f[3]))
    if win is None:
        return None, None, 0
    script = subprocess.run(['perf', 'script', '-i', str(data)],
                            capture_output=True, text=True, timeout=900)
    inside = 0
    total = 0
    pat = re.compile(r'(\d+\.\d+):')
    for line in script.stdout.splitlines():
        m = pat.search(line)
        if not m:
            continue
        t = float(m.group(1))
        total += 1
        if win[0] <= t <= win[1]:
            inside += 1
    data.unlink(missing_ok=True)
    return win, inside, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--k', type=int, nargs='+', default=[1, 8, 32])
    ap.add_argument('--ldlat', type=int, nargs='+',
                    default=[64, 128, 192, 256, 384, 512, 768, 1024])
    ap.add_argument('--size-mb', type=int, default=4096)
    ap.add_argument('--stride', type=int, default=64)
    ap.add_argument('--samples', type=int, default=400000)
    ap.add_argument('--period', type=int, default=1000)
    ap.add_argument('--node', type=int, default=2)
    ap.add_argument('--cpu', type=int, default=0)
    a = ap.parse_args()

    print(f'{"K":>3s} {"ldlat":>6s} {"in-window":>10s} {"total":>8s} {"frac":>7s}')
    for k in a.k:
        for ldlat in a.ldlat:
            win, inside, total = run_one(k, ldlat, a.size_mb, a.stride,
                                         a.samples, a.period, a.node, a.cpu)
            frac = inside / total if total else 0
            print(f'{k:3d} {ldlat:6d} {inside:10d} {total:8d} {frac:7.3f}',
                  flush=True)


if __name__ == '__main__':
    sys.exit(main())
