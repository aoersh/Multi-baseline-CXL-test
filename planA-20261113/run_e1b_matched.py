#!/usr/bin/env python3
"""E1b: link-speed natural experiment with MATCHED load intensity.

Why this exists: in E1 the load threads were pinned to CPUs 1-4 (socket 0) for
every cell. For node2 that is a local access, but for node3 those same threads
cross sockets, so the load was far weaker for the node3 cells. Any node2-vs-node3
comparison under load from E1 is therefore confounded.

Fix: pin the load to cores on the same socket as the memory node under test, so
both devices see a comparable injection rate.

  node2 (socket0)  probe cpu0   load cpus 1,2,3,4
  node3 (socket1)  probe cpu16  load cpus 17,18,19,20

Real device traffic is recorded with the CXL PMU during each cell, so the claim
"the load was matched" is verifiable rather than assumed.
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

# name, probe cpu, memnode, load cpus (same socket as memnode)
PATHS = [
    ('n2-local', 0, 2, '1,2,3,4'),
    ('n3-local', 16, 3, '17,18,19,20'),
    ('n2-cross', 16, 2, '1,2,3,4'),
    ('n3-cross', 0, 3, '17,18,19,20'),
]


def stamp():
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def start_load(memnode, cpus, d):
    cfg = d / 'load.cfg'
    cfg.write_text(f'{cpus} R seq 256m dram {memnode}\n')
    fh = (d / 'load.txt').open('w')
    p = subprocess.Popen([str(MLC), '--loaded_latency', '-T', '-e', f'-o{cfg}',
                          '-d0', '-t90'],
                         stdout=fh, stderr=subprocess.STDOUT,
                         preexec_fn=os.setsid)
    time.sleep(4.0)
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


def parse_summary(out):
    for line in out.splitlines():
        if line.startswith('# summary'):
            f = line.split()
            return dict(zip(f[2::2], f[3::2]))
    return {}


def parse_pmu(text):
    vals = {}
    for m in re.finditer(r'([\d,]+)\s+(cxl_pmu_mem\d\.\d/ddr_cas\w+/)', text):
        vals[m.group(2)] = int(m.group(1).replace(',', ''))
    return vals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--reps', type=int, default=3)
    ap.add_argument('--ks', type=int, nargs='+', default=[1, 8, 32])
    ap.add_argument('--size-mb', type=int, default=4096)
    ap.add_argument('--stride', type=int, default=64)
    ap.add_argument('--samples', type=int, default=943718)
    a = ap.parse_args()

    outdir = ROOT / 'runs' / time.strftime('p%Y%m%d-%H%M%S')
    outdir.mkdir(parents=True, exist_ok=False)
    (outdir / 'meta.json').write_text(json.dumps({
        'start': stamp(), 'reps': a.reps, 'ks': a.ks,
        'size_mb': a.size_mb, 'stride': a.stride, 'samples': a.samples,
        'paths': [{'name': n, 'cpu': c, 'node': m, 'load_cpus': l}
                  for n, c, m, l in PATHS],
    }, indent=2) + '\n')

    rows = []
    for rep in range(1, a.reps + 1):
        for name, cpu, node, lcpus in PATHS:
            for k in a.ks:
                tag = f'e1b-{name}-k{k}-r{rep}'
                d = outdir / tag
                d.mkdir(exist_ok=True)
                lp = lf = None
                try:
                    lp, lf = start_load(node, lcpus, d)
                    p = subprocess.run(
                        ['perf', 'stat', '-e', PMU, '--',
                         'numactl', f'--physcpubind={cpu}', f'--membind={node}',
                         str(MC), '--size-mb', str(a.size_mb),
                         '--stride', str(a.stride), '--k', str(k),
                         '--samples', str(a.samples), '--label', str(d / 'mc')],
                        capture_output=True, text=True, timeout=900)
                    (d / 'perf.txt').write_text(p.stdout + p.stderr)
                finally:
                    stop(lp, lf)
                summ = parse_summary(p.stdout)
                pmu = parse_pmu(p.stdout + p.stderr)
                rd2 = pmu.get('cxl_pmu_mem0.0/ddr_casrd/', 0)
                rd3 = pmu.get('cxl_pmu_mem1.0/ddr_casrd/', 0)
                rec = {'cell': tag, 'path': name, 'k': k, 'rep': rep,
                       'probe_cpu': cpu, 'memnode': node, 'load_cpus': lcpus,
                       'ns_per_access': float(summ.get('ns_per_access', 'nan')),
                       'mean_round_ns': float(summ.get('mean_round_ns', 'nan')),
                       'mem0_casrd': rd2, 'mem1_casrd': rd3}
                rows.append(rec)
                print(f"  {tag:26s} K={k:<2d} {rec['ns_per_access']:7.1f} ns/acc  "
                      f"mem0_rd={rd2:>12,} mem1_rd={rd3:>12,}", flush=True)

    with (outdir / 'results.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r}))
        w.writeheader()
        w.writerows(rows)
    print(f'\nresults: {outdir}', flush=True)


if __name__ == '__main__':
    sys.exit(main())
