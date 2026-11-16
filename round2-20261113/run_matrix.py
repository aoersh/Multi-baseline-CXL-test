#!/usr/bin/env python3
"""Round-2 experiment matrix.

Answers three questions the round-1 data could not:
  Q1  Does MLC's 0-31 cycle population correspond to real accesses?
      -> run MLC and the independent per-access timer on identical conditions.
  Q2  Are the tail modes (1.4 / 2.0 / 3.9 us) periodic?
      -> per-access timestamps from chase.c.
  Q3  Does stride (prefetch reach) change the sub-32 rate?
      -> 64 B vs 4096 B chains, same page set.
"""
import argparse
import csv
import json
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CHASE = ROOT / 'chase'
MLC = Path('/home/hjy/projects/HotPageScheduling/third_party/intel-mlc/Linux/mlc')

# (name, memnode, cpu, stride, description)
PATHS = [
    ('n0-local',    0, 0,  'DRAM local (socket0)'),
    ('n1-cross',    1, 0,  'DRAM cross (socket1)'),
    ('n2-local',    2, 0,  'CXL node2 local (socket0)'),
    ('n3-cross',    3, 0,  'CXL node3 cross (socket1)'),
    ('n2-cross',    2, 16, 'CXL node2 cross (from socket1)'),
    ('n3-local',    3, 16, 'CXL node3 local (socket1)'),
]

LOADS = ['idle', 'read4']
STRIDES = [64, 4096]


def stamp():
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def run(cmd, log=None, timeout=180):
    fh = open(log, 'w') if log else subprocess.DEVNULL
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=timeout)
        if log:
            fh.write(p.stdout)
        return p.returncode, p.stdout
    finally:
        if log:
            fh.close()


def chase(prefix, memnode, cpu, stride, samples, trace=False, size=256, extra_env=None):
    cmd = ['numactl', f'--physcpubind={cpu}', f'--membind={memnode}',
           str(CHASE), '--size-mb', str(size), '--stride', str(stride),
           '--samples', str(samples), '--label', str(prefix)]
    if trace:
        cmd.append('--trace')
    return run(cmd)


def mlc_idle(cpu, memnode, size_mb=256, extra=()):
    cmd = ['numactl', f'--physcpubind={cpu}', f'--membind={memnode}',
           str(MLC), '--idle_latency', '-e', '-r', f'-c{cpu}', f'-j{memnode}',
           f'-b{size_mb}m', '-t3', '-f4', '--lat_hist_max_us=100', *extra]
    return run(cmd)


def start_load(memnode, pattern='R', cpus='1,2,3,4', directory=None):
    cfg = directory / 'load.cfg'
    cfg.write_text(f'{cpus} {pattern} seq 256m dram {memnode}\n')
    fh = (directory / 'load.txt').open('w')
    p = subprocess.Popen([str(MLC), '--loaded_latency', '-T', '-e', f'-o{cfg}',
                          '-d0', '-t40'],
                         stdout=fh, stderr=subprocess.STDOUT,
                         preexec_fn=os.setsid)
    time.sleep(3.0)
    return p, fh


def stop_load(p, fh):
    if p is None:
        return
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        p.wait(timeout=8)
    except Exception:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:
            pass
    try:
        fh.close()
    except Exception:
        pass


def parse_chase_summary(out):
    rec = {}
    for line in out.splitlines():
        if not line.startswith('# summary'):
            continue
        f = line.split()
        d = dict(zip(f[2::2], f[3::2]))
        for k in ('n', 'max_cycles'):
            if k in d:
                rec[k] = int(d[k])
        for k in ('mean_cycles', 'mean_ns', 'sub32_pct'):
            if k in d:
                rec[k] = float(d[k])
    for line in out.splitlines():
        if line.startswith('# numa_maps'):
            for tok in line.split():
                if tok.startswith('N2=') or tok.startswith('N3='):
                    pass
    return rec


def placement_ok(out, memnode):
    """Every large anonymous mapping must report 100% on the target node."""
    want = f'N{memnode}='
    total_pages = 0
    offnode = 0
    for line in out.splitlines():
        if not line.startswith('# numa_maps'):
            continue
        toks = line.split()
        if len(toks) < 2:
            continue
        n = 0
        for t in toks[2:]:
            if t.startswith('N') and '=' in t:
                k, v = t.split('=')
                try:
                    n += int(v)
                except ValueError:
                    pass
        if n < 10000:      # ignore tiny mappings (stack, ld.so)
            continue
        total_pages += n
        for t in toks[2:]:
            if t.startswith('N') and '=' in t and not t.startswith(want):
                try:
                    offnode += int(t.split('=')[1])
                except ValueError:
                    pass
    return {'pages': total_pages, 'offnode_pages': offnode,
            'ok': offnode == 0 and total_pages > 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--repeats', type=int, default=3)
    ap.add_argument('--samples', type=int, default=2000000)
    ap.add_argument('--trace-samples', type=int, default=4000000)
    ap.add_argument('--phase', default='all',
                    choices=['all', 'head2head', 'matrix', 'trace'])
    ap.add_argument('--outdir', default=None,
                    help='reuse an existing run directory instead of creating one')
    args = ap.parse_args()

    if args.outdir:
        outdir = Path(args.outdir).resolve()
        outdir.mkdir(parents=True, exist_ok=True)
    else:
        outdir = ROOT / 'runs' / time.strftime('m%Y%m%d-%H%M%S')
        outdir.mkdir(parents=True, exist_ok=False)
    meta = {'start': stamp(), 'repeats': args.repeats, 'samples': args.samples,
            'trace_samples': args.trace_samples, 'phase': args.phase,
            'chase_sha': subprocess.run(['sha256sum', str(CHASE)], capture_output=True,
                                        text=True).stdout.split()[0],
            'mlc_sha': subprocess.run(['sha256sum', str(MLC)], capture_output=True,
                                      text=True).stdout.split()[0]}
    (outdir / 'meta.json').write_text(json.dumps(meta, indent=2) + '\n')

    rows = []
    rng = random.Random(20261113)

    def cell(prefix, memnode, cpu, stride, load):
        d = outdir / prefix
        d.mkdir(exist_ok=True)
        lp = lf = None
        try:
            if load != 'idle':
                lp, lf = start_load(memnode, directory=d)
            rc, out = chase(str(d / 'chase'), memnode, cpu, stride, args.samples,
                            trace=False)
            (d / 'chase.txt').write_text(out)
            rec = parse_chase_summary(out)
            pl = placement_ok(out, memnode)
        finally:
            stop_load(lp, lf)
        rows.append({'cell': prefix, 'memnode': memnode, 'cpu': cpu,
                     'stride': stride, 'load': load, 'rc': rc,
                     'mean_ns': rec.get('mean_ns'),
                     'sub32_pct': rec.get('sub32_pct'),
                     'max_cycles': rec.get('max_cycles'),
                     'placement_ok': pl['ok'],
                     'offnode_pages': pl['offnode_pages']})
        print(f'  {prefix:34s} mean={rec.get("mean_ns", 0):8.1f}ns '
              f'sub32={rec.get("sub32_pct", 0):7.4f}% place={"OK" if pl["ok"] else "BAD"}',
              flush=True)

    if args.phase in ('all', 'head2head'):
        print('[head2head] MLC vs independent timer, identical condition', flush=True)
        for rep in range(1, args.repeats + 1):
            for name, node, cpu, _ in PATHS:
                mlc_idle(cpu, node)          # warm
                rc, out = mlc_idle(cpu, node)
                d = outdir / f'hh-mlc-{name}-r{rep}'
                d.mkdir(exist_ok=True)
                (d / 'mlc.txt').write_text(out)
                low = tot = 0
                for line in out.splitlines():
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
                    if hi < 32:          # strictly the sub-32-cycle population
                        low += c
                rows.append({'cell': f'hh-mlc-{name}-r{rep}', 'memnode': node,
                             'cpu': cpu, 'stride': 0, 'load': 'idle',
                             'tool': 'mlc', 'lowbin_pct': 100.0 * low / tot if tot else None,
                             'total': tot})
                print(f'  mlc {name:12s} r{rep} lowbin(0-95cyc)={100.0*low/tot:6.2f}%',
                      flush=True)

    if args.phase in ('all', 'matrix'):
        print('[matrix] stride x path x load', flush=True)
        for rep in range(1, args.repeats + 1):
            order = [(p, l, s) for p in PATHS for l in LOADS for s in STRIDES]
            rng.shuffle(order)
            for (name, node, cpu, _), load, stride in order:
                cell(f'rt{rep}-{name}-s{stride}-{load}', node, cpu, stride, load)

    if args.phase in ('all', 'trace'):
        print('[trace] per-access timestamps for periodicity', flush=True)
        for name, node, cpu, _ in PATHS:
            for load in LOADS:
                d = outdir / f'tr-{name}-{load}'
                d.mkdir(exist_ok=True)
                lp = lf = None
                try:
                    if load != 'idle':
                        lp, lf = start_load(node, directory=d)
                    prefix = str(d / 'chase')
                    rc, out = chase(prefix, node, cpu, 64, args.trace_samples,
                                    trace=True)
                    (d / 'chase.txt').write_text(out)
                    for f in d.glob('*.bin'):
                        pass
                    # move bins up
                    for suffix in ('.lat.bin', '.tsc.bin'):
                        src = Path(prefix + suffix)
                        if src.exists():
                            src.rename(d / ('samples' + suffix))
                finally:
                    stop_load(lp, lf)
                print(f'  trace {name:12s} {load}', flush=True)

    results = outdir / 'results.csv'
    if args.outdir and results.exists():
        # keep rows written by earlier phases, replace cells this phase redid
        fresh = {r['cell'] for r in rows}
        with results.open(newline='') as f:
            kept = [r for r in csv.DictReader(f) if r['cell'] not in fresh]
        rows = kept + rows
    with results.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r}))
        w.writeheader()
        w.writerows(rows)
    print(f'\nresults: {outdir}', flush=True)


if __name__ == '__main__':
    sys.exit(main())
