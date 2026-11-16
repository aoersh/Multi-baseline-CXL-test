#!/usr/bin/env python3
"""Round-2 analysis.

Deliverables:
  A. MLC-vs-independent-timer agreement (does the 12% sub-32 population exist?)
  B. stride 64 vs 4096 sub-32 rate under load (prefetch reach test)
  C. per-access lat / tsc analysis:
       - exact quantiles, tail exceedance
       - tail-mode clustering (what the round-1 histogram showed as 1.4/2.0/3.9 us)
       - periodicity: inter-arrival histogram and autocorrelation of tail events
"""
import argparse
import collections
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def quant(v, p):
    if not v:
        return None
    i = min(int(p * (len(v) - 1) + 0.5), len(v) - 1)
    return v[i]


def read_bin(path, fmt, width):
    data = path.read_bytes()
    n = len(data) // width
    return n, data


def load_lat(path):
    data = path.read_bytes()
    n = len(data) // 4
    return list(struct.unpack(f'<{n}I', data[:n * 4]))


def load_tsc(path):
    data = path.read_bytes()
    n = len(data) // 8
    return list(struct.unpack(f'<{n}Q', data[:n * 8]))


def section_a(outdir):
    """MLC vs independent timer on identical conditions."""
    print('=' * 78)
    print('A. MLC low-bin population vs an independent per-access timer')
    print('=' * 78)
    rows = []
    for d in sorted(outdir.glob('hh-mlc-*')):
        txt = (d / 'mlc.txt').read_text()
        low = tot = 0
        mean = None
        for line in txt.splitlines():
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
        rows.append({'cell': d.name, 'mean_ns': mean, 'total': tot,
                     'sub32_pct': 100.0 * low / tot if tot else None})
    for r in rows:
        print(f"  {r['cell']:26s} MLC mean={r['mean_ns'] or 0:7.1f} ns  "
              f"MLC sub-32 = {r['sub32_pct']:6.2f}%  (n={r['total']:,})")
    return rows


def section_b(outdir):
    print()
    print('=' * 78)
    print('B. stride test: does the sub-32 population track prefetch reach?')
    print('=' * 78)
    agg = collections.defaultdict(list)
    for d in sorted(outdir.glob('rt*')):
        f = d / 'chase.txt'
        if not f.exists():
            continue
        txt = f.read_text()
        for line in txt.splitlines():
            if not line.startswith('# summary'):
                continue
            f2 = line.split()
            kv = dict(zip(f2[2::2], f2[3::2]))
            # rt<rep>-<name>-s<stride>-<load>
            parts = d.name.split('-')
            rep = parts[0][2:]
            stride = None
            load = None
            for p in parts:
                if p.startswith('s') and p[1:].isdigit():
                    stride = p[1:]
                if p in ('idle', 'read4'):
                    load = p
            name = d.name[len(f'rt{rep}-'):]
            name = name.split('-s')[0]
            key = (name, stride, load)
            agg[key].append({'sub32': float(kv.get('sub32_pct', 'nan')),
                             'mean': float(kv.get('mean_ns', 'nan'))})
    print(f"  {'path':12s} {'load':6s} {'stride':>7s} {'mean ns':>9s} {'sub-32 %':>9s}  n")
    for key in sorted(agg):
        vals = agg[key]
        m = sum(v['mean'] for v in vals) / len(vals)
        s = sum(v['sub32'] for v in vals) / len(vals)
        print(f'  {key[0]:12s} {key[1]:6s} {key[2]:>7s} {m:9.1f} {s:9.4f}  {len(vals)}')
    # explicit contrast
    print()
    for load in ('idle', 'read4'):
        for name in sorted({k[0] for k in agg}):
            a = agg.get((name, '64', load))
            b = agg.get((name, '4096', load))
            if a and b:
                ra = sum(v['sub32'] for v in a) / len(a)
                rb = sum(v['sub32'] for v in b) / len(b)
                print(f'  {name:12s} {load:6s} sub-32  stride64={ra:7.4f}%  '
                      f'stride4096={rb:7.4f}%  ratio={ra/rb if rb else float("nan"):.1f}x')
    return agg


def section_c(outdir):
    print()
    print('=' * 78)
    print('C. per-access timing: quantiles, tail modes, periodicity')
    print('=' * 78)
    for d in sorted(outdir.glob('tr-*')):
        latf = d / 'samples.lat.bin'
        tscf = d / 'samples.tsc.bin'
        if not latf.exists():
            continue
        lat = load_lat(latf)
        n = len(lat)
        srt = sorted(lat)
        mean = sum(lat) / n
        print(f'\n  {d.name}  n={n:,}  mean={mean:8.1f} cyc  '
              f'p50={quant(srt,.5)}  p99={quant(srt,.99)}  p999={quant(srt,.999)}  '
              f'max={srt[-1]}')
        sub32 = sum(1 for x in lat if x < 32)
        print(f'    sub-32 cycles: {100.0*sub32/n:8.4f}%')
        # tail modes: exact-value histogram above 2000 cycles
        hist = collections.Counter(x for x in lat if x >= 2000)
        top = hist.most_common(12)
        if top:
            print('    most frequent tail values (cycles / share):')
            for v, c in top:
                print(f'      {v:7d}  n={c:7d}  {100.0*c/n:7.4f}%')
        # periodicity of tail events
        if tscf.exists():
            ts = load_tsc(tscf)
            m = min(len(ts), n)
            idx = [i for i in range(m) if lat[i] >= 2000]
            print(f'    tail(>=2000cyc) events: {len(idx):,}')
            if len(idx) > 200:
                gaps = [ts[idx[i + 1]] - ts[idx[i]] for i in range(len(idx) - 1)
                        if idx[i + 1] - idx[i] <= 64]
                if gaps:
                    gaps.sort()
                    print(f'    inter-event cycles (local gaps, n={len(gaps):,}): '
                          f'p10={quant(gaps,.1):,} p50={quant(gaps,.5):,} '
                          f'p90={quant(gaps,.9):,}')
                    g = collections.Counter(gaps)
                    print('    most common inter-event gaps: '
                          + ', '.join(f'{v}({c})' for v, c in g.most_common(6)))
                    # cluster size distribution
                    runs = []
                    cur = 1
                    for i in range(1, len(idx)):
                        if idx[i] - idx[i - 1] <= 64:
                            cur += 1
                        else:
                            runs.append(cur)
                            cur = 1
                    runs.append(cur)
                    runs.sort()
                    print(f'    tail burst sizes: mean={sum(runs)/len(runs):.2f} '
                          f'p50={quant(runs,.5)} p90={quant(runs,.9)} max={runs[-1]}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('outdir', nargs='?')
    a = ap.parse_args()
    outdir = Path(a.outdir) if a.outdir else max(ROOT.glob('runs/m*'))
    print(f'analysing {outdir}\n')
    section_a(outdir)
    section_b(outdir)
    section_c(outdir)


if __name__ == '__main__':
    sys.exit(main())
