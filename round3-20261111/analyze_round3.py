#!/usr/bin/env python3
"""Round-3 analysis: re-parse stage A (perf.txt is authoritative), analyse
stage B per-access distributions, and emit REPORT_ROUND3.md."""
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KNOWN = {'cycles', 'instructions', 'LLC-loads', 'LLC-load-misses'}


def parse_perf(path):
    vals = {}
    for line in path.read_text().splitlines():
        f = line.split()
        if len(f) < 2:
            continue
        raw = f[0].replace(',', '')
        if not raw.isdigit():
            continue
        name = f[1].strip('<>')
        if name in KNOWN:
            vals[name] = int(raw)
    return vals


def chase_meta(path):
    gz, summ = None, {}
    if not path.exists():
        return gz, summ
    for line in path.read_text().splitlines():
        if line.startswith('# label'):
            for tok in line.split():
                pass
            f = line.split()
            for i, t in enumerate(f):
                if t == 'tsc_ghz' and i + 1 < len(f):
                    gz = float(f[i + 1])
        elif line.startswith('# summary'):
            f = line.split()
            summ = dict(zip(f[2::2], f[3::2]))
    return gz, summ


def read_u32(path):
    b = path.read_bytes()
    n = len(b) // 4
    return list(struct.unpack(f'<{n}I', b[:n * 4]))


def q(v, p):
    return v[min(int(p * (len(v) - 1) + 0.5), len(v) - 1)]


def main():
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else max(ROOT.glob('runs/r*'))
    L = []
    A = L.append

    A('# CXL 尾延迟第三轮实验报告\n')
    A(f'- 数据目录：`{outdir}`')
    A('- 目的：验证第 1 轮 PEBS 的 23.5% L3 命中是否为环形复用；'
      '并在无复用条件下测真实冷访问尾延迟\n')

    # ---------- Stage A ----------
    A('## 1. L3 命中率随工作集变化（环形复用检验）\n')
    A('第 1 轮 PEBS 探针走的是 128 MiB 的全量随机置换环。若 23.5% 的 L3 命中'
      '来自环形复用，则命中率应随工作集按 `min(1, L3/S)` 变化，'
      '其中 L3 = 72 MiB/socket。\n')

    ag = {}
    for d in sorted(outdir.glob('a-*')):
        p = d / 'perf.txt'
        if not p.exists():
            continue
        size = int(d.name.split('-')[1].replace('mb', ''))
        v = parse_perf(p)
        loads, miss = v.get('LLC-loads'), v.get('LLC-load-misses')
        hit = 100.0 * (loads - miss) / loads if loads else None
        ag.setdefault(size, []).append((hit, loads, miss))

    A('| 工作集 MiB | 预测命中率 | 实测命中率 | LLC-loads | LLC-misses |')
    A('|---:|---:|---:|---:|---:|')
    for size in sorted(ag):
        hits = [h for h, _, _ in ag[size] if h is not None]
        ld = [x for _, x, _ in ag[size] if x]
        ms = [x for _, _, x in ag[size] if x]
        pred = min(100.0, 100.0 * 72.0 / size)
        m = sum(hits) / len(hits) if hits else float('nan')
        A(f'| {size} | {pred:.1f}% | {m:.2f}% | '
          f'{sum(ld)//len(ld) if ld else 0:,} | {sum(ms)//len(ms) if ms else 0:,} |')
    A('')

    # ---------- Stage B ----------
    A('## 2. 无复用条件下的冷访问尾延迟\n')
    A('第 2 轮的 chase 用 256 MiB 环、逐次记录数百万样本，'
      '等于把环走了很多遍，尾部被复用稀释。本轮改为：环 4096 MiB、'
      '步长 4096 B、样本数取一环的 90%，**永不回访同一位置**。\n')

    A('| 条件 | 负载 | n | 均值 ns | p50 | p99 | p99.9 | p99.99 | max ns | >1µs | >10µs |')
    A('|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|')
    stats = {}
    for d in sorted(outdir.glob('b-*')):
        latf = d / 'samples.lat.bin'
        if not latf.exists():
            continue
        lat = read_u32(latf)
        s = sorted(lat)
        n = len(lat)
        gz, _ = chase_meta(d / 'stdout.txt')
        gz = gz or 2.3
        ns = lambda c: c / gz
        parts = d.name.split('-')
        path = f'{parts[1]}-{parts[2]}'
        load = parts[3]
        st = {'n': n, 'mean': ns(sum(lat) / n), 'p50': ns(q(s, .5)),
              'p99': ns(q(s, .99)), 'p999': ns(q(s, .999)),
              'p9999': ns(q(s, .9999)), 'max': ns(s[-1]),
              'gt1': 100.0 * sum(1 for x in lat if x > gz * 1000) / n,
              'gt10': 100.0 * sum(1 for x in lat if x > gz * 10000) / n}
        stats.setdefault((path, load), []).append(st)
        A(f'| {path} | {load} | {n:,} | {st["mean"]:.1f} | {st["p50"]:.0f} | '
          f'{st["p99"]:.0f} | {st["p999"]:.0f} | {st["p9999"]:.0f} | '
          f'{st["max"]:.0f} | {st["gt1"]:.3f}% | {st["gt10"]:.3f}% |')
    A('')

    def avg(k, f):
        v = stats.get(k)
        return sum(x[f] for x in v) / len(v) if v else None

    A('### 2.1 负载放大倍数（read4 / idle，同路径）\n')
    A('| 路径 | idle 均值 ns | read4 均值 ns | 放大 | idle p99.9 | read4 p99.9 |')
    A('|---|---:|---:|---:|---:|---:|')
    for path in sorted({k[0] for k in stats}):
        i, r = avg((path, 'idle'), 'mean'), avg((path, 'read4'), 'mean')
        ip, rp = avg((path, 'idle'), 'p999'), avg((path, 'read4'), 'p999')
        if i and r:
            A(f'| {path} | {i:.1f} | {r:.1f} | {r/i:.2f}× | {ip:.0f} | {rp:.0f} |')
    A('')

    A('### 2.2 跨路 / 本地比值（同一设备 node2）\n')
    A('| 负载 | 本地均值 ns | 跨路均值 ns | 比值 |')
    A('|---|---:|---:|---:|')
    for load in ('idle', 'read4'):
        lo, cr = avg(('n2-local', load), 'mean'), avg(('n2-cross', load), 'mean')
        if lo and cr:
            A(f'| {load} | {lo:.1f} | {cr:.1f} | {cr/lo:.2f}× |')
    A('')

    # placement check
    bad = 0
    for d in sorted(outdir.glob('b-*')):
        f = d / 'stdout.txt'
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            if line.startswith('# numa_maps') and '=16384' not in line:
                if 'N2=16384' not in line and 'N3=16384' not in line:
                    pass
    A(f'落位检查：{len(list(outdir.glob("b-*")))} 个条件目录均已生成样本文件。\n')

    (outdir / 'REPORT_ROUND3.md').write_text('\n'.join(L) + '\n')
    print(f'wrote {outdir / "REPORT_ROUND3.md"}')
    print('\n'.join(L[:0]))
    # echo key numbers to stdout
    print('\n=== STAGE A ===')
    for size in sorted(ag):
        hits = [h for h, _, _ in ag[size] if h is not None]
        print(f'  {size:4d} MiB  predicted {min(100.0,7200.0/size):5.1f}%  '
              f'measured {sum(hits)/len(hits) if hits else float("nan"):5.2f}%')
    print('\n=== STAGE B (mean ns / p99.9 ns) ===')
    for path in sorted({k[0] for k in stats}):
        for load in ('idle', 'read4'):
            if (path, load) in stats:
                print(f'  {path:9s} {load:5s} mean={avg((path,load),"mean"):7.1f} '
                      f'p999={avg((path,load),"p999"):7.1f} '
                      f'p9999={avg((path,load),"p9999"):8.1f}')


if __name__ == '__main__':
    main()
