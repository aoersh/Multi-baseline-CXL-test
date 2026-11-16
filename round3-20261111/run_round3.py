#!/usr/bin/env python3
"""Round-3 experiments.

A. L3 reuse sweep
   The round-1 PEBS probe walks a full random permutation of an N MiB ring.
   Hypothesis: its 23.5% "L3 hit" is ordinary cyclic reuse, not prefetch.
   Prediction from capacity: hit fraction ~= min(1, L3/S).
   Test: vary S, measure LLC-loads / LLC-load-misses with perf.

B. Cold-access tail under load
   Round-2 chase used a 256 MiB ring and traced millions of accesses, so it
   re-walked the ring many times and its tail was diluted by reuse.
   Fix: one ring pass only (samples < node count), ring far larger than L3.
   Gives a genuinely cold per-access distribution, idle and 4-thread read.
"""
import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CHASE = ROOT / 'chase'
PROBE = ROOT / 'pebs_probe'
MLC = Path('/home/hjy/projects/HotPageScheduling/third_party/intel-mlc/Linux/mlc')


def stamp():
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def sh(cmd, out=None, timeout=300):
    fh = open(out, 'w') if out else subprocess.DEVNULL
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=timeout)
        if out:
            fh.write(p.stdout)
        return p.returncode, p.stdout
    finally:
        if out:
            fh.close()


def parse_perf(m):
    """Parse `perf stat -e a,b,c` default output.

    Real format is two columns: a comma-grouped count, then the event name,
    optionally followed by '# ...' commentary. Older/newer perf builds
    sometimes wrap the name in angle brackets, so strip those too.
    """
    known = {'cycles', 'instructions', 'LLC-loads', 'LLC-load-misses'}
    vals = {}
    for line in m.splitlines():
        f = line.split()
        if len(f) < 2:
            continue
        raw = f[0].replace(',', '')
        if not raw.isdigit():
            continue
        name = f[1].strip('<>')
        if name in known:
            vals[name] = int(raw)
    return vals


def start_load(node, d):
    cfg = d / 'load.cfg'
    cfg.write_text(f'1,2,3,4 R seq 256m dram {node}\n')
    fh = (d / 'load.txt').open('w')
    p = subprocess.Popen([str(MLC), '--loaded_latency', '-T', '-e', f'-o{cfg}',
                          '-d0', '-t60'],
                         stdout=fh, stderr=subprocess.STDOUT,
                         preexec_fn=os.setsid)
    time.sleep(3.0)
    return p, fh


def stop(p, fh):
    if p:
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


def stage_a(outdir, repeats):
    print('[A] L3 reuse sweep', flush=True)
    rows = []
    sizes = [32, 64, 96, 128, 192, 256, 384, 512]
    for rep in range(1, repeats + 1):
        for mb in sizes:
            d = outdir / f'a-{mb}mb-r{rep}'
            d.mkdir(exist_ok=True)
            rc, out = sh(['perf', 'stat', '-e',
                          'cycles,instructions,LLC-loads,LLC-load-misses',
                          'numactl', '--physcpubind=0', '--membind=2',
                          str(PROBE), '--size-mb', str(mb), '1.5'],
                         out=d / 'perf.txt')
            v = parse_perf(out)
            loads = v.get('LLC-loads', 0)
            miss = v.get('LLC-load-misses', 0)
            hitpct = 100.0 * (loads - miss) / loads if loads else None
            rows.append({'stage': 'A', 'cell': f'a-{mb}mb-r{rep}',
                         'size_mb': mb, 'llc_loads': loads,
                         'llc_misses': miss, 'l3_hit_pct': hitpct,
                         'predicted_pct': min(100.0, 100.0 * 72.0 / mb)})
            print(f'  {mb:4d} MiB r{rep}  hits={hitpct if hitpct is None else round(hitpct,1)}%'
                  f'  predicted={min(100.0,7200.0/mb):.1f}%', flush=True)
    return rows


def stage_b(outdir, repeats, size_mb, stride):
    print(f'[B] cold-access tail (ring {size_mb} MiB, stride {stride})', flush=True)
    nodes = size_mb * 1024 * 1024 // stride
    samples = int(nodes * 0.9)          # one partial pass => no revisit
    print(f'  ring nodes={nodes:,} samples={samples:,} '
          f'({100.0*samples/nodes:.0f}% of one pass)', flush=True)
    rows = []
    conditions = [('n2-local', 2, 0), ('n2-cross', 2, 16), ('n3-cross', 3, 0)]
    for rep in range(1, repeats + 1):
        for name, node, cpu in conditions:
            for load in ('idle', 'read4'):
                d = outdir / f'b-{name}-{load}-r{rep}'
                d.mkdir(exist_ok=True)
                lp = lf = None
                try:
                    if load != 'idle':
                        lp, lf = start_load(node, d)
                    rc, out = sh(['numactl', f'--physcpubind={cpu}',
                                  f'--membind={node}', str(CHASE),
                                  '--size-mb', str(size_mb),
                                  '--stride', str(stride),
                                  '--samples', str(samples),
                                  '--trace', '--label', str(d / 'chase')],
                                 out=d / 'stdout.txt', timeout=600)
                finally:
                    stop(lp, lf)
                for suf in ('.lat.bin', '.tsc.bin'):
                    src = Path(str(d / 'chase') + suf)
                    if src.exists():
                        src.rename(d / ('samples' + suf))
                summ = {}
                for line in (d / 'stdout.txt').read_text().splitlines():
                    if line.startswith('# summary'):
                        f = line.split()
                        summ = dict(zip(f[2::2], f[3::2]))
                rows.append({'stage': 'B', 'cell': d.name, 'path': name,
                             'load': load, 'rep': rep,
                             'samples': samples,
                             'mean_ns': summ.get('mean_ns'),
                             'sub32_pct': summ.get('sub32_pct'),
                             'max_cycles': summ.get('max_cycles')})
                print(f'  {name:9s} {load:5s} r{rep} mean={summ.get("mean_ns","?")} ns '
                      f'sub32={summ.get("sub32_pct","?")}%', flush=True)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stage', default='all', choices=['all', 'a', 'b'])
    ap.add_argument('--repeats', type=int, default=3)
    ap.add_argument('--size-mb', type=int, default=4096)
    ap.add_argument('--stride', type=int, default=4096)
    a = ap.parse_args()

    outdir = ROOT / 'runs' / time.strftime('r%Y%m%d-%H%M%S')
    outdir.mkdir(parents=True, exist_ok=False)
    (outdir / 'meta.json').write_text(json.dumps({
        'start': stamp(), 'repeats': a.repeats, 'stage': a.stage,
        'size_mb': a.size_mb, 'stride': a.stride,
        'chase_sha': subprocess.run(['sha256sum', str(CHASE)],
                                    capture_output=True, text=True).stdout.split()[0],
        'probe_sha': subprocess.run(['sha256sum', str(PROBE)],
                                    capture_output=True, text=True).stdout.split()[0],
    }, indent=2) + '\n')

    rows = []
    if a.stage in ('all', 'a'):
        rows += stage_a(outdir, a.repeats)
    if a.stage in ('all', 'b'):
        rows += stage_b(outdir, a.repeats, a.size_mb, a.stride)

    with (outdir / 'results.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r}))
        w.writeheader()
        w.writerows(rows)
    print(f'\nresults: {outdir}', flush=True)


if __name__ == '__main__':
    sys.exit(main())
