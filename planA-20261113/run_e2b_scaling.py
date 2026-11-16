#!/usr/bin/env python3
"""E2b: link-speed gap as a function of aggregate load intensity.

E1b showed the 16 vs 32 GT/s gap grows from ~20% (idle) to ~60% (4 load threads).
The multi-core probe showed the device still has headroom at 173 M access/s, so
the gap should keep growing until the link becomes the bottleneck. This stage
measures that curve directly:

  for each device: probe on its own socket at K=8, load threads on the same
  socket, sweeping the load thread count 0,1,2,4,8,16.

Device traffic is recorded with the CXL PMU so "load was matched" is verified,
not assumed.
"""
import argparse
import csv
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MC = ROOT / 'multichase'
MLC = Path('/home/hjy/projects/HotPageScheduling/third_party/intel-mlc/Linux/mlc')

PMU = ('cxl_pmu_mem0.0/ddr_casrd/,cxl_pmu_mem0.0/ddr_caswr/,'
       'cxl_pmu_mem1.0/ddr_casrd/,cxl_pmu_mem1.0/ddr_caswr/')

# device, probe cpu, memnode, load cpu pool (same socket as memnode)
DEVICES = [
    ('n2', 0, 2, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 32]),
    ('n3', 16, 3, [17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 48]),
]
LOADS = [0, 1, 2, 4, 8, 16]


def start_load(memnode, cpus, d):
    if not cpus:
        return None, None
    cfg = d / 'load.cfg'
    cfg.write_text(f'{",".join(map(str, cpus))} R seq 256m dram {memnode}\n')
    fh = (d / 'load.txt').open('w')
    p = subprocess.Popen([str(MLC), '--loaded_latency', '-T', '-e', f'-o{cfg}',
                          '-d0', '-t120'],
                         stdout=fh, stderr=subprocess.STDOUT,
                         preexec_fn=os.setsid)
    time.sleep(4.0 + 0.5 * len(cpus))
    return p, fh


def stop(p, fh):
    if p:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            p.wait(timeout=10)
        except Exception:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                pass
    try:
        if fh:
            fh.close()
    except Exception:
        pass


def parse(out):
    summ = {}
    for line in out.splitlines():
        if line.startswith('# summary'):
            f = line.split()
            summ = dict(zip(f[2::2], f[3::2]))
    pmu = {}
    for m in re.finditer(r'([\d,]+)\s+(cxl_pmu_mem\d\.\d/ddr_cas\w+/)', out):
        pmu[m.group(2)] = int(m.group(1).replace(',', ''))
    return summ, pmu


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--reps', type=int, default=3)
    ap.add_argument('--k', type=int, default=8)
    ap.add_argument('--size-mb', type=int, default=4096)
    ap.add_argument('--stride', type=int, default=64)
    ap.add_argument('--samples', type=int, default=943718)
    a = ap.parse_args()

    outdir = ROOT / 'runs' / time.strftime('p%Y%m%d-%H%M%S')
    outdir.mkdir(parents=True, exist_ok=False)
    (outdir / 'meta.json').write_text(json.dumps({
        'start': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'reps': a.reps, 'k': a.k,
        'size_mb': a.size_mb, 'stride': a.stride, 'samples': a.samples,
        'loads': LOADS,
    }, indent=2) + '\n')

    rows = []
    for rep in range(1, a.reps + 1):
        for name, cpu, node, pool in DEVICES:
            for nl in LOADS:
                cpus = pool[:nl]
                tag = f'e2b-{name}-load{nl}-r{rep}'
                d = outdir / tag
                d.mkdir(exist_ok=True)
                lp = lf = None
                try:
                    lp, lf = start_load(node, cpus, d)
                    p = subprocess.run(
                        ['perf', 'stat', '-e', PMU, '--',
                         'numactl', f'--physcpubind={cpu}', f'--membind={node}',
                         str(MC), '--size-mb', str(a.size_mb),
                         '--stride', str(a.stride), '--k', str(a.k),
                         '--samples', str(a.samples), '--label', str(d / 'mc')],
                        capture_output=True, text=True, timeout=1200)
                    (d / 'perf.txt').write_text(p.stdout + p.stderr)
                finally:
                    stop(lp, lf)
                # perf stat writes its counters to stderr, multichase to stdout;
                # parse the union or every PMU field silently reads as zero.
                summ, pmu = parse(p.stdout + p.stderr)
                rec = {
                    'cell': tag, 'device': name, 'load_threads': nl, 'rep': rep,
                    'k': a.k,
                    'ns_per_access': float(summ.get('ns_per_access', 'nan')),
                    'mean_round_ns': float(summ.get('mean_round_ns', 'nan')),
                    'accesses_per_second': float(summ.get('accesses_per_second', 'nan')),
                    'mem0_casrd': pmu.get('cxl_pmu_mem0.0/ddr_casrd/', 0),
                    'mem1_casrd': pmu.get('cxl_pmu_mem1.0/ddr_casrd/', 0),
                }
                rows.append(rec)
                dev_rd = rec['mem0_casrd'] if name == 'n2' else rec['mem1_casrd']
                print(f"  {tag:26s} {rec['ns_per_access']:8.1f} ns/acc  "
                      f"dev_rd={dev_rd:>13,}", flush=True)

    with (outdir / 'results.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r}))
        w.writeheader()
        w.writerows(rows)
    print(f'\nresults: {outdir}', flush=True)


if __name__ == '__main__':
    sys.exit(main())
