"""Background test: register the add-on, run synthetic strokes, benchmark.

blender --background --factory-startup --python tests/test_brushes.py
"""

import os
import sys
import time
import traceback

import bpy
import numpy as np
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import vr_sculpt  # noqa: E402
from vr_sculpt import brushes  # noqa: E402

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def make_sphere(subdiv, location=(0, 0, 0), scale=1.0):
    bpy.ops.object.select_all(action='DESELECT')
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=subdiv, radius=1.0, location=location)
    obj = bpy.context.active_object
    obj.scale = (scale, scale, scale)
    bpy.context.view_layer.update()
    return obj


def positions(obj):
    n = len(obj.data.vertices)
    a = np.empty(n * 3)
    obj.data.attributes["position"].data.foreach_get("vector", a)
    return a.reshape(n, 3)


def run_line(stroke, start, end, steps=20, pose_rot=None):
    for i in range(steps + 1):
        p = Vector(start).lerp(Vector(end), i / steps)
        pose = Matrix.Translation(p)
        if pose_rot is not None:
            pose = pose @ pose_rot(i / steps)
        stroke.update(p, pose, pressure=1.0)


def test_brushes():
    print("== brush behavior")
    obj = make_sphere(4)
    before = positions(obj)
    top = Vector((0, 0, 1.0))

    # Draw: verts near the top move outward.
    st = brushes.MeshState(obj)
    s = brushes.Stroke(st, "DRAW", 0.3, 1.0)
    run_line(s, (-0.2, 0, 1.0), (0.2, 0, 1.0))
    after = positions(obj)
    moved = np.linalg.norm(after - before, axis=1)
    near = np.linalg.norm(before - np.array(top), axis=1) < 0.25
    far = np.linalg.norm(before - np.array(top), axis=1) > 0.6
    check(moved[near].max() > 1e-3, f"draw moves verts near the stroke (max {moved[near].max():.4f})")
    check(moved[far].max() < 1e-9, "draw leaves far verts alone")
    r_before = np.linalg.norm(before[near], axis=1).mean()
    r_after = np.linalg.norm(after[near], axis=1).mean()
    check(r_after > r_before, f"draw pushes outward ({r_before:.4f} -> {r_after:.4f})")

    # Invert pushes inward.
    before = positions(obj)
    s = brushes.Stroke(brushes.MeshState(obj), "DRAW", 0.3, 1.0, invert=True)
    run_line(s, (-0.2, 0, 1.0), (0.2, 0, 1.0))
    after = positions(obj)
    check(np.linalg.norm(after[near], axis=1).mean() < np.linalg.norm(before[near], axis=1).mean(),
          "inverted draw pushes inward")

    # Smooth reduces roughness.
    rng = np.random.default_rng(1)
    noisy = positions(obj) + rng.normal(0, 0.01, positions(obj).shape)
    obj.data.attributes["position"].data.foreach_set("vector", noisy.astype(np.float32).ravel())
    obj.data.update()
    rough = lambda p: np.abs(np.linalg.norm(p[near], axis=1) - np.linalg.norm(p[near], axis=1).mean()).mean()
    before = positions(obj)
    s = brushes.Stroke(brushes.MeshState(obj), "SMOOTH", 0.4, 1.0)
    for _ in range(3):
        s.last_local = None
        run_line(s, (-0.2, 0, 1.0), (0.2, 0, 1.0))
    after = positions(obj)
    check(rough(after) < rough(before) * 0.7, f"smooth reduces noise ({rough(before):.4f} -> {rough(after):.4f})")

    # Grab translates verts with the controller.
    before = positions(obj)
    s = brushes.Stroke(brushes.MeshState(obj), "GRAB", 0.3, 1.0, grab_rotate=False)
    run_line(s, (0, 0, 1.0), (0, 0, 1.5))
    after = positions(obj)
    # Earlier strokes dented the top, so use the vertex closest to the grab point.
    top_i = int(np.argmin(np.linalg.norm(before - np.array(top), axis=1)))
    dz = after[top_i, 2] - before[top_i, 2]
    check(dz > 0.4, f"grab moves the vertex under the tip with it (dz {dz:.3f} of 0.5)")
    check(np.abs(after[far] - before[far]).max() < 1e-9, "grab leaves far verts alone")

    # Grab with rotation.
    before = positions(obj)
    s = brushes.Stroke(brushes.MeshState(obj), "GRAB", 0.5, 1.0, grab_rotate=True)
    rot = lambda t: Quaternion((0, 0, 1), t * 1.0).to_matrix().to_4x4()
    p = Vector(before[top_i])
    for i in range(11):
        t = i / 10
        s.update(p, Matrix.Translation(p) @ rot(t))
    after = positions(obj)
    check(np.abs(after - before).max() > 1e-3, "rotating grab twists the surface")

    # Symmetry: a stroke at +X also moves -X.
    obj2 = make_sphere(4)
    before = positions(obj2)
    s = brushes.Stroke(brushes.MeshState(obj2), "INFLATE", 0.3, 1.0, symmetry_x=True)
    run_line(s, (1.0, 0, -0.1), (1.0, 0, 0.1))
    after = positions(obj2)
    moved = np.linalg.norm(after - before, axis=1)
    left = before[:, 0] < -0.8
    right = before[:, 0] > 0.8
    check(moved[left].max() > 1e-3 and moved[right].max() > 1e-3, "X symmetry mirrors the stroke")
    check(abs(moved[left].max() - moved[right].max()) < moved[right].max() * 0.2, "mirrored side moves about equally")

    # Object transforms: a scaled, moved object is sculpted where the tip is in world space.
    obj3 = make_sphere(4, location=(5, 0, 0), scale=2.0)
    before = positions(obj3)
    s = brushes.Stroke(brushes.MeshState(obj3), "DRAW", 0.6, 1.0)
    run_line(s, (4.6, 0, 2.0), (5.4, 0, 2.0))
    after = positions(obj3)
    moved = np.linalg.norm(after - before, axis=1)
    check(moved[before[:, 2] > 0.9].max() > 1e-3, "world-space tip hits a moved/scaled object")
    check(moved[before[:, 2] < 0].max() < 1e-9, "...and only where the tip is")

    # Undo-safety: a fresh MeshState re-reads the mesh.
    check(brushes.MeshState(obj3).co.shape == before.shape, "fresh state reads the current mesh")


def test_grid():
    print("== grid selection matches brute force")
    obj = make_sphere(7)
    st = brushes.MeshState(obj)
    check(len(st.co) >= brushes.GRID_MIN_VERTS, f"mesh uses the grid ({len(st.co):,} verts)")
    rng = np.random.default_rng(3)
    ok = True
    for _ in range(25):
        c = rng.normal(size=3)
        c = c / np.linalg.norm(c) * rng.uniform(0.9, 1.1)
        r = 0.12
        idx, w = st.select(c, r)
        brute = np.flatnonzero(np.linalg.norm(st.co - c, axis=1) < r)
        ok &= np.array_equal(np.sort(idx), brute)
        # After drifting, the grid must still find everything.
        st.co[brute] += 0.04
        st.moved(0.04)
        idx2, _ = st.select(c, r)
        brute2 = np.flatnonzero(np.linalg.norm(st.co - c, axis=1) < r)
        ok &= np.array_equal(np.sort(idx2), brute2)
    check(ok, "grid finds exactly the brute-force vertices, also after drift")

    print("== smooth adjacency matches edge scan")
    idx, _ = st.select(st.co[0], 0.1)
    mean = st.neighbor_mean(idx)
    e = st.edges
    ref = []
    for v in idx[:50]:
        nb = np.concatenate([e[e[:, 0] == v, 1], e[e[:, 1] == v, 0]])
        ref.append(st.co[nb].mean(axis=0))
    check(np.allclose(mean[:50], np.array(ref)), "CSR neighbor mean is correct")
    bpy.data.objects.remove(obj)


def bench():
    # A hand moving 1 m/s at 90 Hz moves ~11 mm per frame. Sphere radius 1, brush radius 0.1.
    print("== benchmark: ms per VR frame (hand moving 11 mm/frame, brush radius 0.1, sphere radius 1)")
    rows = []
    for subdiv in (5, 6, 7, 8):
        obj = make_sphere(subdiv)
        n = len(obj.data.vertices)
        for brush in ("DRAW", "SMOOTH", "GRAB", "INFLATE"):
            t0 = time.perf_counter()
            st = brushes.MeshState(obj)
            s = brushes.Stroke(st, brush, 0.1, 0.5)
            s.update(Vector((-0.3, 0, 1.0)))
            start_ms = (time.perf_counter() - t0) * 1000
            frame, write, evalt = [], [], []
            for i in range(1, 50):
                p = Vector((-0.3 + i * 0.011, 0, 1.0))
                t0 = time.perf_counter()
                s.update(p, Matrix.Translation(p + Vector((0, 0, 0.003 * i))))
                t1 = time.perf_counter()
                bpy.context.view_layer.update()
                t2 = time.perf_counter()
                frame.append((t1 - t0) * 1000)
                write.append(st.last_write_ms)
                evalt.append((t2 - t1) * 1000)
            rows.append((n, brush, start_ms, np.mean(frame), np.max(frame), np.mean(write), np.mean(evalt)))
        bpy.data.objects.remove(obj)
    print(f"  {'verts':>9} {'brush':8} {'start':>7} {'frame':>7} {'max':>7} {'(write)':>8} {'depsgr':>7}   budget 11.1 ms @ 90 Hz")
    for n, b, s0, f, fm, w, e in rows:
        print(f"  {n:>9,} {b:8} {s0:7.1f} {f:7.2f} {fm:7.2f} {w:8.2f} {e:7.2f}")


def main():
    vr_sculpt.register()
    try:
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete()
        test_brushes()
        test_grid()
        bench()
        print("== register/unregister")
        vr_sculpt.unregister()
        vr_sculpt.register()
        check(hasattr(bpy.context.scene, "vr_sculpt"), "re-register works")
    except Exception:
        traceback.print_exc()
        failures.append("exception")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "FAILED " + str(failures) if failures else "ALL PASSED")


main()
