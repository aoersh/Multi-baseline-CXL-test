#!/usr/bin/env python3
"""Rebuild results.csv from the run directory so the summary artifact matches
what is actually on disk.

The per-phase writer in run_matrix.py only records rows for the phase that is
running; a phase that crashes before its write (or a trace phase, which never
appends) leaves the CSV short. This script recomputes every row from the raw
files, which are the authority.
"""
import collections
import csv
import struct
import sys
from pathlib import Path


def q(v, p):
    return v[min(int(p * (len(v) - 1) + 0.5), len(v) - 1)]


def read_u32(path):
    d = path.read_bytes()
    n = len(d) // 4
    return list(struct.unpack(f'<{n}I', d[:n * 4]))


def mlc_low(path):
    low = tot = 0
    mean = None
    for line in path.read_text().splitlines():
        if 'Each iteration took' in line:
            try:
                mean = float(line.split('(')[1].split('ns')[0])
            except Exception:
                pass
        if not line.startswith('['):
            continue
        label, _, cnt = line.partition(']\t')
        try:
            c = int(cnt)
        except ValueError:
            continue
        tot += c
        body = label[1:]
        if body.startswith('>'):
            continue
        try:
            lo, hi = (int(x) for x in body.split('-'))
        except ValueError:
            continue
        if hi < 32:
            low += c
    return mean, (100.0 * low / tot if tot else None), tot


def chase_summary(path):
    out = {}
    for line in path.read_text().splitlines():
        if not line.startswith('# summary'):
            continue
        f = line.split()
        out = dict(zip(f[2::2], f[3::2]))
    return out


def main():
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else max(
        Path(__file__).resolve().parent.glob('runs/m*'))
    rows = []

    # head2head: MLC only
    for d in sorted(outdir.glob('hh-mlc-*')):
        f = d / 'mlc.txt'
        if not f.exists():
            continue
        mean, pct, tot = mlc_low(f)
        cell = d.name
        body = cell[len('hh-mlc-'):]
        path, _, rep = body.rpartition('-r')
        node = {'n0-local': 0, 'n1-cross': 1, 'n2-local': 2,
                'n2-cross': 2, 'n3-local': 3, 'n3-cross': 3}.get(path)
        cpu = 16 if path.endswith('cross') and path.startswith('n2') else (
            0 if path.endswith('cross') and path.startswith('n3') else
            (16 if path.startswith('n3-local') else 0))
        rows.append({'cell': cell, 'tool': 'mlc', 'memnode': node, 'cpu': cpu,
                     'load': 'idle', 'mean_ns': mean, 'sub32_pct': pct,
                     'total': tot})

    # matrix: chase
    for d in sorted(outdir.glob('rt*')):
        f = d / 'chase.txt'
        if not f.exists():
            continue
        s = chase_summary(f)
        parts = d.name.split('-')
        stride = load = None
        for p in parts:
            if p.startswith('s') and p[1:].isdigit():
                stride = p[1:]
            if p in ('idle', 'read4'):
                load = p
        rows.append({'cell': d.name, 'tool': 'chase',
                     'path': d.name.split('-', 1)[1].split('-s')[0],
                     'stride': stride, 'load': load,
                     'mean_ns': float(s.get('mean_ns', 'nan')),
                     'sub32_pct': float(s.get('sub32_pct', 'nan')),
                     'max_cycles': s.get('max_cycles')})

    # trace
    for d in sorted(outdir.glob('tr-*')):
        latf = d / 'samples.lat.bin'
        if not latf.exists():
            continue
        lat = read_u32(latf)
        n = len(lat)
        srt = sorted(lat)
        s = chase_summary(d / 'chase.txt') if (d / 'chase.txt').exists() else {}
        parts = d.name.split('-')
        load = 'read4' if parts[-1] == 'read4' else 'idle'
        rows.append({'cell': d.name, 'tool': 'chase+trace',
                     'path': '-'.join(parts[1:-1]), 'load': load,
                     'samples': n,
                     'mean_cycles': sum(lat) / n,
                     'p50_cycles': q(srt, .5),
                     'p99_cycles': q(srt, .99),
                     'p999_cycles': q(srt, .999),
                     'max_cycles': srt[-1],
                     'sub32_pct': 100.0 * sum(1 for x in lat if x < 32) / n,
                     'mean_ns': float(s.get('mean_ns', 'nan')) if s else ''})

    out = outdir / 'results.csv'
    with out.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r}))
        w.writeheader()
        w.writerows(rows)
    print(f'rebuilt {out}: {len(rows)} rows')
    for t, c in collections.Counter(r['tool'] for r in rows).items():
        print(f'  {t}: {c}')


if __name__ == '__main__':
    main()
