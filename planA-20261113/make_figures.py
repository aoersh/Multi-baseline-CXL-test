#!/usr/bin/env python3
"""Generate the paper's figures as SVG.

No plotting library is available on this machine (no matplotlib/numpy, and
installing is blocked), so these are emitted directly as SVG. That is also the
better output for a LaTeX paper: vector, no rasterisation, selectable text.

Palette: the validated default categorical order (blue, orange, aqua,
yellow...) from the dataviz reference. Figures are single- or two-series, so
only the first two or three slots are used.

Usage: python3 make_figures.py [outdir]
"""
import collections
import csv
import os
import statistics
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

# Categorical slots, fixed order, never cycled.
BLUE = '#2a78d6'
ORANGE = '#eb6834'
AQUA = '#1baf7a'
INK = '#0b0b0b'
INK2 = '#52514e'
MUTED = '#8a8a86'
GRID = '#e2e1dc'
SURFACE = '#fcfcfb'


def load(path):
    a = collections.defaultdict(list)
    for r in csv.DictReader(open(path)):
        a[(r['path'], int(r['k']))].append(float(r['ns_per_access']))
    return a


def load_dev(path):
    a = collections.defaultdict(list)
    for r in csv.DictReader(open(path)):
        a[(r['device'], int(r['load_threads']))].append(float(r['ns_per_access']))
    return a


def load_e3(path):
    a = collections.defaultdict(list)
    for r in csv.DictReader(open(path)):
        a[(r['path'], r['load_node'], int(r['k']))].append(float(r['ns_per_access']))
    return a


def m(a, key):
    return statistics.mean(a[key])


class Svg:
    """Minimal SVG canvas with a data->pixel mapping."""

    def __init__(self, w, h, pad_l=64, pad_r=24, pad_t=28, pad_b=52):
        self.w, self.h = w, h
        self.pl, self.pr, self.pt, self.pb = pad_l, pad_r, pad_t, pad_b
        self.parts = []

    def add(self, s):
        self.parts.append(s)

    def px(self, x, x0, x1):
        span = self.pl and (self.w - self.pl - self.pr)
        return self.pl + (x - x0) / (x1 - x0) * span

    def py(self, y, y0, y1):
        span = self.h - self.pt - self.pb
        return self.h - self.pb - (y - y0) / (y1 - y0) * span

    def axes(self, x0, x1, y0, y1, xticks, yticks, xlabel, ylabel,
             yfmt='{:.0f}', xtick_labels=None):
        for yt in yticks:
            Y = self.py(yt, y0, y1)
            self.add(f'<line x1="{self.pl}" y1="{Y:.1f}" x2="{self.w-self.pr}" '
                     f'y2="{Y:.1f}" stroke="{GRID}" stroke-width="1"/>')
            self.add(f'<text x="{self.pl-8}" y="{Y+4:.1f}" text-anchor="end" '
                     f'font-size="11" fill="{INK2}">{yfmt.format(yt)}</text>')
        for i, xt in enumerate(xticks):
            X = self.px(xt, x0, x1)
            lab = (xtick_labels[i] if xtick_labels else
                   (str(int(xt)) if float(xt).is_integer() else str(xt)))
            # A label may be a tuple of lines; SVG does not break on \n.
            lines = lab if isinstance(lab, (list, tuple)) else (lab,)
            for j, ln in enumerate(lines):
                self.add(f'<text x="{X:.1f}" y="{self.h-self.pb+18+j*13:.1f}" '
                         f'text-anchor="middle" font-size="11" fill="{INK2}">{ln}</text>')
        self.add(f'<line x1="{self.pl}" y1="{self.h-self.pb}" '
                 f'x2="{self.w-self.pr}" y2="{self.h-self.pb}" '
                 f'stroke="{INK2}" stroke-width="1"/>')
        self.add(f'<text x="{(self.pl+self.w-self.pr)/2:.0f}" '
                 f'y="{self.h-12}" text-anchor="middle" font-size="12" '
                 f'fill="{INK}">{xlabel}</text>')
        mid = (self.pt + self.h - self.pb) / 2
        self.add(f'<text transform="translate(16,{mid:.0f}) rotate(-90)" '
                 f'text-anchor="middle" font-size="12" fill="{INK}">{ylabel}</text>')

    def polyline(self, pts, color, width=2, dash=None, marker=True):
        d = f' stroke-dasharray="{dash}"' if dash else ''
        p = ' '.join(f'{x:.1f},{y:.1f}' for x, y in pts)
        self.add(f'<polyline points="{p}" fill="none" stroke="{color}" '
                 f'stroke-width="{width}" stroke-linejoin="round"{d}/>')
        if marker:
            for x, y in pts:
                self.add(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" '
                         f'fill="{color}" stroke="{SURFACE}" stroke-width="2"/>')

    def text(self, x, y, s, size=11, color=INK, anchor='start', weight='normal'):
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" '
                 f'font-size="{size}" fill="{color}" font-weight="{weight}">{s}</text>')

    def legend(self, items, x, y):
        px = x
        for label, color, dash in items:
            d = f' stroke-dasharray="{dash}"' if dash else ''
            self.add(f'<line x1="{px}" y1="{y}" x2="{px+18}" y2="{y}" '
                     f'stroke="{color}" stroke-width="2"{d}/>')
            self.add(f'<circle cx="{px+9}" cy="{y}" r="4" fill="{color}" '
                     f'stroke="{SURFACE}" stroke-width="2"/>')
            self.add(f'<text x="{px+24}" y="{y+4}" font-size="11" '
                     f'fill="{INK}">{label}</text>')
            px += 26 + len(label) * 6.2

    def save(self, path):
        head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" '
                f'height="{self.h}" viewBox="0 0 {self.w} {self.h}">\n'
                f'<rect width="{self.w}" height="{self.h}" fill="{SURFACE}"/>\n')
        open(path, 'w').write(head + '\n'.join(self.parts) + '\n</svg>\n')


def fig1_linkgap_vs_load(e2b8, e2b16, out):
    """The headline figure: link-speed gap versus competing load."""
    loads = [0, 1, 2, 4, 8, 16]
    g8 = [100 * (m(e2b8, ('n2', n)) - m(e2b8, ('n3', n))) / m(e2b8, ('n3', n))
          for n in loads]
    g16 = [100 * (m(e2b16, ('n2', n)) - m(e2b16, ('n3', n))) / m(e2b16, ('n3', n))
           for n in loads]

    s = Svg(560, 340)
    x0, x1 = -0.4, 16.4
    y0, y1 = 0, 140
    s.axes(x0, x1, y0, y1, loads, [0, 20, 40, 60, 80, 100, 120, 140],
           'Competing load threads (same socket)', 'Link-speed gap (%)')
    p8 = [(s.px(n, x0, x1), s.py(g, y0, y1)) for n, g in zip(loads, g8)]
    p16 = [(s.px(n, x0, x1), s.py(g, y0, y1)) for n, g in zip(loads, g16)]
    s.polyline(p8, BLUE)
    s.polyline(p16, ORANGE, dash='6 4')
    # direct labels on the final points
    s.text(p8[-1][0] + 8, p8[-1][1] + 4, '83.3%', 11, BLUE)
    s.text(p16[-1][0] - 8, p16[-1][1] - 10, '125.7%', 11, ORANGE, anchor='end')
    # idle reference line
    s.add(f'<line x1="{s.pl}" y1="{s.py(g8[0], y0, y1):.1f}" '
          f'x2="{s.w-s.pr}" y2="{s.py(g8[0], y0, y1):.1f}" '
          f'stroke="{MUTED}" stroke-width="1" stroke-dasharray="3 3"/>')
    s.text(s.w - s.pr - 4, s.py(g8[0], y0, y1) - 6,
           'idle: 18.5%', 10, MUTED, anchor='end')
    s.legend([('probe K=8', BLUE, None), ('probe K=16', ORANGE, '6 4')], 78, 22)
    s.save(out)


def fig2_latency_vs_k(e2, out):
    """All six paths: per-access cost versus probe concurrency, idle.

    K takes powers of two, so a linear axis would crowd 1 against 2 and leave
    a gap at the top. Positions are ordinal (evenly spaced) and labelled with
    the actual K.
    """
    ks = [1, 2, 4, 8, 16, 32]
    series = [
        ('node2 local CXL (Gen4)', 'n2-local', BLUE),
        ('node3 local CXL (Gen5)', 'n3-local', ORANGE),
        ('node0 local DRAM', 'n0-local', AQUA),
    ]
    s = Svg(560, 360)
    x0, x1 = -0.5, len(ks) - 0.5
    y0, y1 = 0, 520
    s.axes(x0, x1, y0, y1, list(range(len(ks))),
           [0, 100, 200, 300, 400, 500],
           'Probe concurrency (K chains, log-spaced)', 'Per-access cost (ns)',
           xtick_labels=[str(k) for k in ks])
    items = []
    for label, path, color in series:
        pts = [(s.px(i, x0, x1), s.py(m(e2, (path, k)), y0, y1))
               for i, k in enumerate(ks)]
        s.polyline(pts, color)
        items.append((label, color, None))
    s.legend(items, 74, 22)
    s.save(out)


def fig3_interference(e3, out):
    """2x2 interference decomposition, K=1."""
    groups = [('probe local (CPU 0)', 'n2-local'), ('probe cross (CPU 16)', 'n2-cross')]
    s = Svg(480, 340, pad_l=64, pad_b=64)
    x0, x1 = -0.5, 1.5
    y0, y1 = 0, 1800
    s.axes(x0, x1, y0, y1, [0, 1], [0, 400, 800, 1200, 1600],
           'Probe position (device under test: node 2)',
           'Latency (ns)',
           xtick_labels=[('probe local', '(CPU 0)'),
                         ('probe cross-socket', '(CPU 16)')])
    bw = 0.30
    for i, (_, path) in enumerate(groups):
        for j, (ln, color, lab) in enumerate((('0', BLUE, 'load → node0 DRAM'),
                                              ('2', ORANGE, 'load → node2 CXL'))):
            cx = i - bw / 2 + j * bw
            v = m(e3, (path, ln, 1))
            X = s.px(cx, x0, x1)
            Y = s.py(v, y0, y1)
            base = s.py(0, y0, y1)
            w = (s.px(i - 0.5 + bw, x0, x1) - s.px(i - 0.5, x0, x1)) * 0.78
            s.add(f'<rect x="{X-w/2:.1f}" y="{Y:.1f}" width="{w:.1f}" '
                  f'height="{base-Y:.1f}" fill="{color}" rx="3"/>')
            s.text(X, Y - 7, f'{v:.0f}', 11, INK, anchor='middle')
    s.legend([('load → node0 DRAM', BLUE, None),
              ('load → node2 CXL (same device)', ORANGE, None)], 62, 22)
    s.save(out)


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'figures')
    os.makedirs(outdir, exist_ok=True)
    e2 = load(os.path.join(ROOT, 'runs/p20261113-193614/results.csv'))
    e2b8 = load_dev(os.path.join(ROOT, 'runs/p20261116-162039/results.csv'))
    e2b16 = load_dev(os.path.join(ROOT, 'runs/p20261117-181603/results.csv'))
    e3 = load_e3(os.path.join(ROOT, 'runs/p20261113-200454/results.csv'))

    fig1_linkgap_vs_load(e2b8, e2b16, os.path.join(outdir, 'fig1-linkgap-vs-load.svg'))
    fig2_latency_vs_k(e2, os.path.join(outdir, 'fig2-latency-vs-k.svg'))
    fig3_interference(e3, os.path.join(outdir, 'fig3-interference.svg'))
    for f in sorted(os.listdir(outdir)):
        p = os.path.join(outdir, f)
        print(f'{f}: {os.path.getsize(p)} bytes')


if __name__ == '__main__':
    main()
