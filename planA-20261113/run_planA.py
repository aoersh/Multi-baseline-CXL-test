#!/usr/bin/env python3
"""Plan A matrix runner: E1 (link speed), E2 (MLP knee), E3 (interference 2x2).

All cells walk a 4 GiB ring at 4096 B stride so they stay cold: at most one
pass is ever sampled, so no cell is contaminated by cache reuse.
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
MC = ROOT / 'multichase'
MLC = Path('/home/hjy/projects/HotPageScheduling/third_party/intel-mlc/Linux/mlc')

# name, memnode for the probe, cpu for the probe
PATHS = [
    ('n0-local', 0, 0),
    ('n1-cross', 1, 0),
    ('n2-local', 2, 0),
    ('n3-cross', 3, 0),
    ('n2-cross', 2, 16),
    ('n3-local', 3, 16),
]
KS = [1, 2, 4, 8, 16, 32]


def stamp():
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def run(cmd, out, timeout=900):
    with open(out, 'w') as fh:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=timeout)
        fh.write(p.stdout)
    return p.stdout


def parse(out):
    rec = {}
    for line in out.splitlines():
        if line.startswith('# summary'):
            f = line.split()
            k = dict(zip(f[2::2], f[3::2]))
            for key in ('rounds', 'k', 'ns_per_access', 'mean_round_ns',
                        'mean_round_cycles', 'max_round_cycles'):
                if key in k:
                    rec[key] = float(k[key]) if '.' in k[key] else int(k[key])
        elif line.startswith('# measure_seconds'):
            f = line.split()
            k = dict(zip(f[2::2], f[3::2]))
            for key in ('measure_seconds', 'total_accesses', 'accesses_per_second'):
                if key in k:
                    rec[key] = float(k[key])
        elif line.startswith('# init_seconds'):
            rec['init_seconds'] = float(line.split()[2])
    return rec


def placement(out, node):
    """Return (ok, offnode_pages) for the large anonymous mapping.

    multichase prints only the node counters (N0=..N3=) for the big mapping, so
    there is no 'anon=' field to key on. Sum all N* counters instead.
    """
    total = off = 0
    for line in out.splitlines():
        if not line.startswith('# numa_maps:'):
            continue
        f = line.split()
        n = 0
        for t in f[2:]:
            if t.startswith('N') and '=' in t and t[1].isdigit():
                try:
                    n += int(t.split('=')[1])
                except ValueError:
                    pass
        if n < 10000:
            continue
        total += n
        for t in f[2:]:
            if t.startswith('N') and '=' in t and t[1].isdigit():
                if not t.startswith(f'N{node}='):
                    try:
                        off += int(t.split('=')[1])
                    except ValueError:
                        pass
    return (off == 0 and total > 0), off


def start_load(memnode, d, cpus='1,2,3,4'):
    cfg = d / 'load.cfg'
    cfg.write_text(f'{cpus} R seq 256m dram {memnode}\n')
    fh = (d / 'load.txt').open('w')
    p = subprocess.Popen([str(MLC), '--loaded_latency', '-T', '-e', f'-o{cfg}',
                          '-d0', '-t90'],
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stage', default='all', choices=['all', 'e1', 'e2', 'e3'])
    ap.add_argument('--reps', type=int, default=3)
    ap.add_argument('--size-mb', type=int, default=4096)
    ap.add_argument('--stride', type=int, default=4096)
    ap.add_argument('--samples', type=int, default=943718)
    a = ap.parse_args()

    outdir = ROOT / 'runs' / time.strftime('p%Y%m%d-%H%M%S')
    outdir.mkdir(parents=True, exist_ok=False)
    (outdir / 'meta.json').write_text(json.dumps({
        'start': stamp(), 'stage': a.stage, 'reps': a.reps,
        'size_mb': a.size_mb, 'stride': a.stride, 'samples': a.samples,
        'ks': KS,
        'mc_sha': subprocess.run(['sha256sum', str(MC)], capture_output=True,
                                 text=True).stdout.split()[0],
    }, indent=2) + '\n')

    rows = []

    def cell(tag, path, node, cpu, k, loadnode):
        d = outdir / tag
        d.mkdir(exist_ok=True)
        lp = lf = None
        try:
            if loadnode is not None:
                lp, lf = start_load(loadnode, d)
            out = run(['numactl', f'--physcpubind={cpu}', f'--membind={node}',
                       str(MC), '--size-mb', str(a.size_mb),
                       '--stride', str(a.stride), '--k', str(k),
                       '--samples', str(a.samples), '--label', str(d / 'mc')],
                      d / 'stdout.txt')
        finally:
            stop(lp, lf)
        rec = parse(out)
        ok, off = placement(out, node)
        rec.update({'cell': tag, 'path': path, 'probe_node': node,
                    'probe_cpu': cpu, 'k': k, 'load_node': loadnode,
                    'placement_ok': ok, 'offnode_pages': off})
        rows.append(rec)
        print(f"  {tag:34s} K={k:<2d} {rec.get('ns_per_access',0):7.1f} ns/acc "
              f"place={'OK' if ok else 'BAD'}", flush=True)

    if a.stage in ('all', 'e2'):
        print('[E2] MLP knee, idle', flush=True)
        for rep in range(1, a.reps + 1):
            for name, node, cpu in PATHS:
                for k in KS:
                    cell(f'e2-{name}-k{k}-r{rep}', name, node, cpu, k, None)

    if a.stage in ('all', 'e1'):
        print('[E1] link speed under load', flush=True)
        for rep in range(1, a.reps + 1):
            for name, node, cpu in PATHS:
                for k in (1, 8, 32):
                    cell(f'e1-{name}-k{k}-r{rep}', name, node, cpu, k, node)

    if a.stage in ('all', 'e3'):
        print('[E3] interference 2x2 (probe node2, load node0 vs node2)', flush=True)
        for rep in range(1, a.reps + 1):
            for pname, pcpu in (('local', 0), ('cross', 16)):
                for lnode in (0, 2):
                    for k in (1, 8):
                        cell(f'e3-probe{pname}-load{lnode}-k{k}-r{rep}',
                             f'n2-{pname}', 2, pcpu, k, lnode)

    with (outdir / 'results.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r}))
        w.writeheader()
        w.writerows(rows)
    print(f'\nresults: {outdir}', flush=True)


if __name__ == '__main__':
    sys.exit(main())
