#!/usr/bin/env python3
"""Turn a completed run directory into REPORT_ROUND2.md.

Consumes: results.csv, hh-mlc-*/, rt*/, tr-*/, optional prefetch-*/
Writes:   REPORT_ROUND2.md in the same directory.
"""
import collections
import csv
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def q(v, p):
    return v[min(int(p * (len(v) - 1) + 0.5), len(v) - 1)]


def read_u32(path):
    d = path.read_bytes()
    n = len(d) // 4
    return list(struct.unpack(f'<{n}I', d[:n * 4]))


def read_u64(path):
    d = path.read_bytes()
    n = len(d) // 8
    return list(struct.unpack(f'<{n}Q', d[:n * 8]))


def parse_mlc_low(path):
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


def main():
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else max(ROOT.glob('runs/m*'))
    L = []
    A = L.append

    A('# CXL 尾延迟第二轮实验报告\n')
    A(f'运行目录：`{outdir}`\n')

    # --- A: MLC vs independent timer
    A('## 1. MLC 的低延迟样本是否真实存在\n')
    rows = []
    for d in sorted(outdir.glob('hh-mlc-*')):
        f = d / 'mlc.txt'
        if not f.exists():
            continue
        mean, pct, tot = parse_mlc_low(f)
        rows.append((d.name, mean, pct, tot))
    if rows:
        A('| 运行 | MLC 均值 ns | MLC sub-32 周期 | 样本数 |')
        A('|---|---:|---:|---:|')
        for n, m, p, t in rows:
            A(f'| {n} | {m:.1f} | {p:.2f}% | {t:,} |')
        A('')

    # --- B: stride
    A('## 2. 步长对照（预取可达范围）\n')
    agg = collections.defaultdict(list)
    for d in sorted(outdir.glob('rt*')):
        f = d / 'chase.txt'
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            if not line.startswith('# summary'):
                continue
            kv = dict(zip(line.split()[2::2], line.split()[3::2]))
            parts = d.name.split('-')
            stride = load = None
            for p in parts:
                if p.startswith('s') and p[1:].isdigit():
                    stride = p[1:]
                if p in ('idle', 'read4'):
                    load = p
            name = d.name.split('-', 1)[1].split('-s')[0]
            agg[(name, stride, load)].append(
                (float(kv.get('mean_ns', 'nan')), float(kv.get('sub32_pct', 'nan'))))
    if agg:
        A('| 路径 | 负载 | 步长 | 均值 ns | sub-32 % | n |')
        A('|---|---|---:|---:|---:|---:|')
        for k in sorted(agg):
            v = agg[k]
            m = sum(x[0] for x in v) / len(v)
            s = sum(x[1] for x in v) / len(v)
            A(f'| {k[0]} | {k[1]} | {k[2]} | {m:.1f} | {s:.4f} | {len(v)} |')
        A('')

    # --- C: per-access
    A('## 3. 逐次访问：分位、尾部模态、周期性\n')
    for d in sorted(outdir.glob('tr-*')):
        latf = d / 'samples.lat.bin'
        if not latf.exists():
            continue
        lat = read_u32(latf)
        n = len(lat)
        srt = sorted(lat)
        sub = sum(1 for x in lat if x < 32)
        A(f'### {d.name}\n')
        A(f'- n = {n:,}，均值 {sum(lat)/n:.1f} 周期')
        A(f'- p50 {q(srt,.5)}，p99 {q(srt,.99)}，p99.9 {q(srt,.999)}，'
          f'p99.99 {q(srt,.9999)}，max {srt[-1]} 周期')
        A(f'- sub-32 占比 {100.0*sub/n:.4f}%\n')
        hist = collections.Counter(x for x in lat if x >= 2000)
        top = hist.most_common(8)
        if top:
            A('尾部高频精确值（周期 / 占比）：\n')
            A('| 周期 | n | 占比 |')
            A('|---:|---:|---:|')
            for v, c in top:
                A(f'| {v} | {c} | {100.0*c/n:.4f}% |')
            A('')
        tscf = d / 'samples.tsc.bin'
        if tscf.exists():
            ts = read_u64(tscf)
            m = min(len(ts), n)
            idx = [i for i in range(m) if lat[i] >= 2000]
            A(f'尾部（≥2000 周期）事件数：{len(idx):,}，'
              f'占 {100.0*len(idx)/m:.3f}%\n')
            if len(idx) > 200:
                gaps = [ts[idx[i+1]] - ts[idx[i]] for i in range(len(idx)-1)
                        if idx[i+1] - idx[i] <= 64]
                if gaps:
                    g = sorted(gaps)
                    A(f'簇内相邻事件间隔（周期）：p10 {q(g,.1):,}，'
                      f'p50 {q(g,.5):,}，p90 {q(g,.9):,}\n')
                    cnt = collections.Counter(gaps)
                    A('最常见的间隔值：' +
                      '，'.join(f'{v}({c})' for v, c in cnt.most_common(5)) + '\n')
                runs, cur = [], 1
                for i in range(1, len(idx)):
                    if idx[i] - idx[i-1] <= 64:
                        cur += 1
                    else:
                        runs.append(cur); cur = 1
                runs.append(cur)
                runs.sort()
                A(f'尾部爆发簇长度：均值 {sum(runs)/len(runs):.2f}，'
                  f'p50 {q(runs,.5)}，p90 {q(runs,.9)}，max {runs[-1]}\n')

    # --- D: prefetch, if the user ran it
    prefs = sorted(outdir.parent.glob('prefetch-*/'))
    if prefs:
        A('## 4. 预取器判别（sudo 实验）\n')
        A('| 条件 | MSR 0x1A4 | MLC 均值 ns | MLC sub-32 | chase sub-32 |')
        A('|---|---|---:|---:|---:|')
        for d in prefs:
            msr = ''
            f = d / 'msr-active.txt'
            if f.exists():
                for line in f.read_text().splitlines():
                    if line.startswith('cpu0'):
                        msr = line.split()[-1]
            mean = pct = None
            f = d / 'mlc.txt'
            if f.exists():
                mean, pct, _ = parse_mlc_low(f)
            chase_pct = None
            f = d / 'chase.txt'
            if f.exists():
                for line in f.read_text().splitlines():
                    if line.startswith('# summary'):
                        kv = dict(zip(line.split()[2::2], line.split()[3::2]))
                        chase_pct = float(kv.get('sub32_pct', 'nan'))
            A(f"| {d.name} | {msr} | {mean if mean else 0:.1f} | "
              f"{pct if pct else 0:.2f}% | {chase_pct if chase_pct else 0:.4f}% |")
        A('')

    (outdir / 'REPORT_ROUND2.md').write_text('\n'.join(L) + '\n')
    print(f'wrote {outdir / "REPORT_ROUND2.md"}')


if __name__ == '__main__':
    main()
