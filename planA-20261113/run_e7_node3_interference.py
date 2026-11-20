#!/usr/bin/env python3
"""E7: interference-domain 2x2 on node3 (the Gen5 device).

E3 ran the decomposition only on node2, so the paper could not claim that the
interference-domain effect is independent of link speed. E7 mirrors it on node3.

Mirror design — everything moved to socket 1 so the geometry matches E3:

  E3 (node2, socket 0)          E7 (node3, socket 1)
  probe CPU 0  = local          probe CPU 16 = local
  probe CPU 16 = cross          probe CPU 0  = cross
  load on CPUs 1-4              load on CPUs 17-20
  load -> node0 (other device)  load -> node1 (other device)
  load -> node2 (same device)   load -> node3 (same device)

Both use K=1 and K=8, three repetitions.
"""
import argparse
import csv
import json
import os
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MC = ROOT / 'multichase'
MLC = Path('/home/hjy/projects/HotPageScheduling/third_party/intel-mlc/Linux/mlc')
TSC_GHZ = 2.3

# (tag, probe cpu, load node)
CELLS = [
    ('local-loadN1', 16, 1),
    ('local-loadN3', 16, 3),
    ('cross-loadN1', 0, 1),
    ('cross-loadN3', 0, 3),
]
LOAD_CPUS = '17,18,19,20'
PROBE_NODE = 3


def start_load(memnode, d):
    cfg = d / 'load.cfg'
    cfg.write_text(f'{LOAD_CPUS} R seq 256m dram {memnode}\n')
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


def stats(d):
    f = d / 'mc.lat.bin'
    if not f.exists():
        return None
    b = f.read_bytes()
    n = len(b) // 4
    r = struct.unpack(f'<{n}I', b[:n * 4])
    return {'rounds': n, 'mean_round_cycles': sum(r) / n}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--reps', type=int, default=3)
    ap.add_argument('--ks', type=int, nargs='+', default=[1, 8])
    ap.add_argument('--size-mb', type=int, default=4096)
    ap.add_argument('--stride', type=int, default=64)
    ap.add_argument('--samples', type=int, default=943718)
    a = ap.parse_args()

    outdir = ROOT / 'runs' / time.strftime('e7-%Y%m%d-%H%M%S')
    outdir.mkdir(parents=True, exist_ok=False)
    (outdir / 'meta.json').write_text(json.dumps({
        'start': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'reps': a.reps,
        'ks': a.ks, 'load_cpus': LOAD_CPUS, 'probe_node': PROBE_NODE,
        'purpose': 'mirror E3 on node3 to test link-speed independence',
    }, indent=2) + '\n')

    rows = []
    for rep in range(1, a.reps + 1):
        for tag, cpu, lnode in CELLS:
            for k in a.ks:
                d = outdir / f'{tag}-k{k}-r{rep}'
                d.mkdir(exist_ok=True)
                lp = lf = None
                try:
                    lp, lf = start_load(lnode, d)
                    p = subprocess.run(
                        ['numactl', f'--physcpubind={cpu}',
                         f'--membind={PROBE_NODE}', str(MC),
                         '--size-mb', str(a.size_mb), '--stride', str(a.stride),
                         '--k', str(k), '--samples', str(a.samples),
                         '--label', str(d / 'mc')],
                        capture_output=True, text=True, timeout=900)
                    (d / 'stdout.txt').write_text(p.stdout + p.stderr)
                finally:
                    stop(lp, lf)
                s = stats(d)
                if not s:
                    print(f'  {tag} k{k} r{rep}: NO DATA', flush=True)
                    continue
                rec = {'cell': f'{tag}-k{k}-r{rep}', 'tag': tag, 'cpu': cpu,
                       'load_node': lnode, 'k': k, 'rep': rep,
                       'ns_per_access': s['mean_round_cycles'] / TSC_GHZ / k,
                       'rounds': s['rounds']}
                rows.append(rec)
                print(f"  {tag:14s} k={k:<2d} r{rep}  "
                      f"{rec['ns_per_access']:7.1f} ns/acc", flush=True)

    with (outdir / 'results.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r}))
        w.writeheader()
        w.writerows(rows)
    print(f'\nresults: {outdir}')


if __name__ == '__main__':
    sys.exit(main())
