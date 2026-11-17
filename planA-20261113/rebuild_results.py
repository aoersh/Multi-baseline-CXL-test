#!/usr/bin/env python3
"""Rebuild results.csv for every stage from the per-run mc.lat.bin files.

The hand-written summaries were lost when the working tree was switched between
git branches, but each run directory still holds mc.lat.bin: one uint32 per
round, in TSC cycles. Every metric in the original CSVs derives from those, so
the tables can be reconstructed exactly rather than approximated.

ns_per_access = mean(round_cycles) / tsc_ghz / K
"""
import csv
import glob
import os
import re
import struct
import sys

TSC_GHZ = 2.3   # multichase calibrates ~2.2999 on this box; summaries used 2.3


def rounds(path):
    b = open(path, 'rb').read()
    n = len(b) // 4
    return struct.unpack(f'<{n}I', b[:n * 4])


def stats(d):
    f = os.path.join(d, 'mc.lat.bin')
    if not os.path.exists(f):
        return None
    r = rounds(f)
    n = len(r)
    m = sum(r) / n
    return {'rounds': n, 'mean_round_cycles': m,
            'max_round_cycles': max(r)}


def emit(outdir, rows, fields):
    with open(os.path.join(outdir, 'results.csv'), 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


PATH_RE = re.compile(r'^(e\d[ab]?)-(n\d-(?:local|cross))-k(\d+)-r(\d+)$')
E2B_RE = re.compile(r'^e2b-(n\d)-load(\d+)-r(\d+)$')
E3_RE = re.compile(r'^e3-probe(local|cross)-load(\d+)-k(\d+)-r(\d+)$')

PROBE_CPU = {'n2-local': 0, 'n3-local': 16, 'n2-cross': 16, 'n3-cross': 0,
             'n0-local': 0, 'n1-cross': 0}
PROBE_NODE = {'n0-local': 0, 'n1-cross': 1, 'n2-local': 2, 'n3-cross': 3,
              'n2-cross': 2, 'n3-local': 3}


def rebuild_simple(outdir, prefix):
    rows = []
    for d in sorted(glob.glob(os.path.join(outdir, f'{prefix}-*'))):
        name = os.path.basename(d)
        m = PATH_RE.match(name)
        if not m:
            continue
        _, path, k, rep = m.groups()
        k = int(k)
        s = stats(d)
        if not s:
            continue
        rows.append({
            'cell': name, 'path': path, 'k': k, 'rep': int(rep),
            'probe_node': PROBE_NODE.get(path), 'probe_cpu': PROBE_CPU.get(path),
            'ns_per_access': s['mean_round_cycles'] / TSC_GHZ / k,
            'mean_round_ns': s['mean_round_cycles'] / TSC_GHZ,
            'rounds': s['rounds'], 'max_round_cycles': s['max_round_cycles'],
        })
    if rows:
        emit(outdir, rows, sorted({k for r in rows for k in r}))
    return rows


def rebuild_e2b(outdir):
    rows = []
    for d in sorted(glob.glob(os.path.join(outdir, 'e2b-*'))):
        name = os.path.basename(d)
        m = E2B_RE.match(name)
        if not m:
            continue
        dev, nl, rep = m.groups()
        s = stats(d)
        if not s:
            continue
        rows.append({
            'cell': name, 'device': dev, 'load_threads': int(nl),
            'rep': int(rep), 'k': 8,
            'ns_per_access': s['mean_round_cycles'] / TSC_GHZ / 8,
            'mean_round_ns': s['mean_round_cycles'] / TSC_GHZ,
            'rounds': s['rounds'], 'max_round_cycles': s['max_round_cycles'],
        })
    if rows:
        emit(outdir, rows, sorted({k for r in rows for k in r}))
    return rows


def rebuild_e3(outdir):
    rows = []
    for d in sorted(glob.glob(os.path.join(outdir, 'e3-*'))):
        name = os.path.basename(d)
        m = E3_RE.match(name)
        if not m:
            continue
        probe, ln, k, rep = m.groups()
        k = int(k)
        s = stats(d)
        if not s:
            continue
        rows.append({
            'cell': name, 'path': f'n2-{probe}', 'load_node': ln, 'k': k,
            'rep': int(rep),
            'probe_cpu': 0 if probe == 'local' else 16,
            'probe_node': 2,
            'ns_per_access': s['mean_round_cycles'] / TSC_GHZ / k,
            'mean_round_ns': s['mean_round_cycles'] / TSC_GHZ,
            'rounds': s['rounds'], 'max_round_cycles': s['max_round_cycles'],
        })
    if rows:
        emit(outdir, rows, sorted({k for r in rows for k in r}))
    return rows


STAGES = {
    'p20261113-193614': ('simple', 'e2'),
    'p20261113-195247': ('simple', 'e1'),
    'p20261116-152618': ('simple', 'e1b'),
    'p20261116-152522': ('simple', 'e1b'),
    'p20261113-200454': ('e3', None),
    'p20261116-162039': ('e2b', None),
}


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else 'runs'
    total = 0
    for name, (kind, pref) in sorted(STAGES.items()):
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            print(f'{name}: missing')
            continue
        if kind == 'simple':
            rows = rebuild_simple(d, pref)
        elif kind == 'e2b':
            rows = rebuild_e2b(d)
        else:
            rows = rebuild_e3(d)
        print(f'{name} ({kind} {pref or ""}): {len(rows)} rows')
        total += len(rows)
    print(f'total {total} rows rebuilt')


if __name__ == '__main__':
    main()
