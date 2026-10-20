"""Per-frame cost of rebuilding a stroke while drawing.

blender --background --factory-startup --python tests/bench_strokes.py
"""

import os
import sys
import time

import bpy
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vr_sculpt import strokes  # noqa: E402

mesh = bpy.data.meshes.new("bench")
print(f"{'samples':>8} {'style':7} {'mirror':6} {'verts':>8} {'build':>7} {'write':>7} {'total':>7}  (budget 11.1 ms @ 90 Hz)")
for n in (100, 300, 600, 1200):
    t = np.linspace(0, 6 * np.pi, n)
    p = np.stack([np.cos(t) * 0.2, np.sin(t) * 0.2, t * 0.02], axis=1)
    for style, sym in (('TUBE', 0), ('TUBE', 1), ('RIBBON', 0)):
        s = {"style": style, "sym": sym, "c": np.zeros(3), "p": p, "w": np.full(n, 0.01),
             "s": np.tile([0.0, 0.0, 1.0], (n, 1))}
        times_b, times_w = [], []
        for _ in range(5):
            t0 = time.perf_counter()
            arrays = strokes.build(s, preview=True)  # what each frame of drawing costs
            t1 = time.perf_counter()
            strokes.write_mesh(mesh, *arrays, smooth=style == 'TUBE')
            t2 = time.perf_counter()
            times_b.append((t1 - t0) * 1000)
            times_w.append((t2 - t1) * 1000)
        b, w = min(times_b), min(times_w)
        print(f"{n:>8} {style:7} {('X' if sym else '-'):6} {len(mesh.vertices):>8,} {b:7.2f} {w:7.2f} {b + w:7.2f}")
