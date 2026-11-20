#!/usr/bin/env python3
"""E5: symmetric DRAM baseline, to separate socket asymmetry from link effects.

The paper's causal claim compares CPU0->node2 (Gen4, socket 0) against
CPU16->node3 (Gen5, socket 1). That comparison confounds three things:

  (a) link speed            16 vs 32 GT/s
  (b) device/slot identity  two different physical devices on two boards
  (c) socket/core asymmetry CPU0 on socket 0 vs CPU16 on socket 1

The existing data set has only half the controls. It measured
CPU0->node0 (local DRAM) and CPU0->node1 (cross DRAM), but never
CPU16->node1 (local DRAM) or CPU16->node0 (cross DRAM).

Filling the 2x2 on plain DRAM isolates (c) completely, because DRAM links are
identical on both sockets: any residual difference between the two local cells,
or between the two cross cells, is socket-induced.

Cells (membind covers both DRAM nodes so each cell measures its own path):
  CPU0  -> node0   CPU0 -> node1
  CPU16 -> node1   CPU16 -> node0
"""
import argparse
import csv
import json
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MC = ROOT / 'multichase'
TSC_GHZ = 2.3

# (tag, cpu, memnode)
CELLS = [
    ('cpu0-n0',  0, 0),
    ('cpu0-n1',  0, 1),
    ('cpu16-n1', 16, 1),
    ('cpu16-n0', 16, 0),
]


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    return p.stdout + p.stderr


def stats(d):
    f = d / 'mc.lat.bin'
    if not f.exists():
        return None
    b = f.read_bytes()
    n = len(b) // 4
    r = struct.unpack(f'<{n}I', b[:n * 4])
    return {'rounds': n, 'mean_round_cycles': sum(r) / n,
            'max_round_cycles': max(r)}


def placement_ok(out):
    total = off = 0
    for line in out.splitlines():
        if not line.startswith('# numa_maps:'):
            continue
        f = line.split()
        n = 0
        nodes = []
        for t in f[2:]:
            if t.startswith('N') and '=' in t and t[1].isdigit():
                try:
                    v = int(t.split('=')[1])
                    n += v
                    nodes.append((t[1], v))
                except ValueError:
                    pass
        if n < 10000:
            continue
        total += n
        if len(nodes) == 1:
            off += 0     # single-node mapping, fine
        else:
            # multi-node mapping: count pages off the dominant node
            nodes.sort(key=lambda x: -x[1])
            off += n - nodes[0][1]
    return off == 0 and total > 0, off


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--reps', type=int, default=3)
    ap.add_argument('--ks', type=int, nargs='+', default=[1, 8])
    ap.add_argument('--size-mb', type=int, default=4096)
    ap.add_argument('--stride', type=int, default=64)
    ap.add_argument('--samples', type=int, default=943718)
    a = ap.parse_args()

    outdir = ROOT / 'runs' / time.strftime('e5-%Y%m%d-%H%M%S')
    outdir.mkdir(parents=True, exist_ok=False)
    (outdir / 'meta.json').write_text(json.dumps({
        'start': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'reps': a.reps,
        'ks': a.ks, 'size_mb': a.size_mb, 'stride': a.stride,
        'samples': a.samples,
        'purpose': 'symmetric DRAM baseline: isolate socket asymmetry',
    }, indent=2) + '\n')

    rows = []
    for rep in range(1, a.reps + 1):
        for tag, cpu, node in CELLS:
            for k in a.ks:
                d = outdir / f'{tag}-k{k}-r{rep}'
                d.mkdir(exist_ok=True)
                out = run(['numactl', f'--physcpubind={cpu}',
                           f'--membind={node}', str(MC),
                           '--size-mb', str(a.size_mb),
                           '--stride', str(a.stride), '--k', str(k),
                           '--samples', str(a.samples),
                           '--label', str(d / 'mc')])
                (d / 'stdout.txt').write_text(out)
                s = stats(d)
                ok, off = placement_ok(out)
                if not s:
                    print(f'  {tag} k{k} r{rep}: NO DATA', flush=True)
                    continue
                rec = {'cell': f'{tag}-k{k}-r{rep}', 'tag': tag, 'cpu': cpu,
                       'memnode': node, 'k': k, 'rep': rep,
                       'ns_per_access': s['mean_round_cycles'] / TSC_GHZ / k,
                       'mean_round_ns': s['mean_round_cycles'] / TSC_GHZ,
                       'rounds': s['rounds'], 'placement_ok': ok,
                       'offnode_pages': off}
                rows.append(rec)
                print(f"  {tag:10s} k={k:<2d} r{rep}  "
                      f"{rec['ns_per_access']:7.1f} ns/acc  "
                      f"place={'OK' if ok else 'BAD'}", flush=True)

    with (outdir / 'results.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r}))
        w.writeheader()
        w.writerows(rows)
    print(f'\nresults: {outdir}')


if __name__ == '__main__':
    sys.exit(main())
