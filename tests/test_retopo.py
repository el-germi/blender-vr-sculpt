"""Background tests for Retopo mode: the reference and its surface, Strip (laying quads, carrying on from
a border edge, never back over its face), Quad (clicks, the open quad carrying on, snapping, bow ties,
triangles, pulling a border edge out, sliding and merging vertices), Relax, Tweak, the off hand's
delete, Mirror X (twins, the center line, a strip along it), the undo guard, the hand panel, mode
switching, hints and status, the trigger operator's undo steps, and speed on a 100k-face reference.

blender --background --factory-startup --python tests/test_retopo.py
"""

import math
import os
import sys
import time
import traceback

import bpy
import numpy as np
from mathutils import Matrix, Quaternion, Vector
from mathutils.bvhtree import BVHTree

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import vr_sculpt  # noqa: E402
from vr_sculpt import controllers, gfx, hud, menus, operators, retopo, vr_ui  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_model as tm  # noqa: E402  (the trigger operator harness)

failures = []
C = Vector((0.0, 0.0, 1.0))  # the reference sphere's center
R = 0.3


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def at(point):
    return Matrix.LocRotScale(Vector(point), Quaternion(), None)


def on_sphere(theta, phi, lift=0.004):
    """A point lift above the reference sphere, theta from +Z and phi from +X, in degrees."""
    t, p = math.radians(theta), math.radians(phi)
    return C + (R + lift) * Vector((math.sin(t) * math.cos(p), math.sin(t) * math.sin(p), math.cos(t)))


def arc(theta, phi0, phi1, step=0.4):
    n = max(int(abs(phi1 - phi0) / step), 1)
    return [on_sphere(theta, phi0 + (phi1 - phi0) * i / n) for i in range(n + 1)]


def press(ctx, point, path=(), off=False, value=1.0):
    """A trigger press at point, moved through path, released. Returns the handler (or None)."""
    s = ctx.scene.vr_sculpt
    handler = retopo.pick(ctx, s, off, at(point), at(point), 1.0, value)
    if handler is None:
        return None
    for p in path:
        handler.update(ctx, at(p), at(p), value)
    handler.finish(ctx)
    return handler


def ids():
    return {w.id for w in menus.layout(bpy.context)[0] if w.id}


def widget(wid):
    return vr_ui.find(menus.layout(bpy.context)[0], wid)


def make_reference(ctx, segments=64, rings=32, name="Head"):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=segments, ring_count=rings, radius=R, location=C)
    obj = ctx.active_object
    obj.name = name
    return obj


class Truth:
    """An independent BVH of the reference, to check that vertices sit on its surface."""

    def __init__(self, ref):
        me = ref.data
        mw = ref.matrix_world
        self.tree = BVHTree.FromPolygons([mw @ v.co for v in me.vertices], [p.vertices[:] for p in me.polygons])

    def distance(self, p):
        return self.tree.find_nearest(Vector(p))[3]


def world_verts(obj):
    return [obj.matrix_world @ v.co for v in obj.data.vertices]


def on_surface(truth, obj, tol=1e-4):
    worst = max((truth.distance(p) for p in world_verts(obj)), default=0.0)
    return worst <= tol, worst


def manifold(obj):
    """Every edge has one or two faces."""
    counts = {}
    for p in obj.data.polygons:
        for k in p.edge_keys:
            counts[k] = counts.get(k, 0) + 1
    return all(1 <= c <= 2 for c in counts.values())


def outward(obj):
    """Every face faces away from the sphere's center."""
    mw = obj.matrix_world
    n3 = mw.inverted_safe().transposed().to_3x3()
    return all((n3 @ p.normal).dot(mw @ p.center - C) > 0.0 for p in obj.data.polygons)


def parts(obj):
    """How many connected pieces the faces make."""
    me = obj.data
    parent = list(range(len(me.vertices)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for e in me.edges:
        a, b = find(e.vertices[0]), find(e.vertices[1])
        parent[a] = b
    used = {v for p in me.polygons for v in p.vertices}
    return len({find(v) for v in used})


def fresh(ctx, tool):
    """A new retopo mesh from the next stroke on, with this tool."""
    retopo.new_mesh(ctx)
    retopo.set_tool(ctx, tool)


# --- reference and surface -------------------------------------------------------------------

def test_enter(ref):
    print("== entering Retopo")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    menus.set_mode(ctx, 'RETOPO')
    check(s.mode == 'RETOPO' and retopo.reference(ctx) == ref and retopo.result(ctx) is None,
          "the active mesh is the reference; no retopo mesh until the first stroke")
    check(retopo._surface["surface"] is not None and retopo._surface["surface"].triangles == len(ref.data.loop_triangles),
          "the surface tree is built on entering")
    surf = retopo.surface(ctx, ref)
    check(surf is retopo._surface["surface"], "and kept while the reference doesn't change")
    hit = surf.nearest(C + Vector((0.0, 0.0, R + 0.02)), 0.05)
    check(hit is not None and abs((hit[0] - C).length - R) < 2e-3 and hit[1].dot(Vector((0, 0, 1))) > 0.99,
          "closest point: on the surface, with its normal")
    inside = C + Vector((0.0, -R + 0.08, 0.0))  # 8 cm inside the front
    check(surf.nearest(inside, retopo.REACH) is None, "a tip deep inside is out of reach of the closest point")
    hit = surf.locate(inside, C + Vector((0.0, -1.0, 0.0)), retopo.REACH)
    check(hit is not None and hit[0].y < -R + 0.005, "but the ray from the controller finds the front surface")
    check(surf.locate(C + Vector((0.0, -1.0, 0.0)), C + Vector((0.0, -1.5, 0.0)), retopo.REACH) is None,
          "far from the surface: nowhere")
    check(retopo.engaged(ctx, s, on_sphere(90, -90), 1.0) and not retopo.engaged(ctx, s, C + Vector((0, -1, 0)), 1.0),
          "engaged near the surface only")
    v = retopo._ref_version[0]
    ref.data.update()
    ctx.view_layer.update()
    check(retopo._ref_version[0] > v, "a geometry change of the reference is counted")
    old = retopo._surface["surface"]
    check(retopo.surface(ctx, ref) is not old, "and the tree is built again")


# --- strip ------------------------------------------------------------------------------

def test_strip(ref, truth):
    print("== Strip")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    s.retopo_size = 0.02
    retopo.set_tool(ctx, 'STRIP')
    h = press(ctx, on_sphere(80, -60), [on_sphere(80, -59.6)])
    check(h is not None and not h.changed and retopo.result(ctx) is None, "a click lays nothing (no mesh made)")
    path = arc(80, -60, -20)
    h = press(ctx, path[0], path[1:])
    obj = retopo.result(ctx)
    check(h.changed and obj is not None and obj.name == ref.name + retopo.SUFFIX, "a drag makes '<ref>.retopo'")
    check(obj.get(retopo.LINK) == ref and obj.parent == ref and obj in ref.users_collection[0].objects.values(),
          "linked to the reference (ID property), parented to it, in its collection")
    me = obj.data
    length = R * math.sin(math.radians(80)) * math.radians(40)
    n = len(me.polygons)
    check(int(length / 0.02) - 2 <= n <= int(length / 0.02) + 1 and len(me.vertices) == 2 * n + 2,
          f"{n} quads over {length * 100:.1f} cm at 2 cm, one strip ({len(me.vertices)} vertices)")
    check(all(len(p.vertices) == 4 for p in me.polygons), "all quads")
    sides = [(Vector(world_verts(obj)[e.vertices[0]]) - Vector(world_verts(obj)[e.vertices[1]])).length for e in me.edges]
    check(min(sides) > 0.012 and max(sides) < 0.028, f"roughly square: edges {min(sides) * 100:.1f}-{max(sides) * 100:.1f} cm")
    ok, worst = on_surface(truth, obj)
    check(ok, f"every vertex on the surface (worst {worst * 1000:.3f} mm)")
    check(outward(obj) and manifold(obj), "faces face outward, edges have one or two faces")
    check(obj.display_type == 'WIRE' and obj.show_in_front, "shown as a wire while edited (the headset draws it)")
    return obj


def test_continue(ref, truth, obj):
    print("== Strip carries on from a border edge")
    ctx = bpy.context
    me = obj.data
    n, nv = len(me.polygons), len(me.vertices)
    # The last row: the border edge farthest along the strip.
    end = on_sphere(80, -20)
    w = world_verts(obj)
    border = [e for e in me.edges if sum(1 for p in me.polygons if e.key in p.edge_keys) == 1]
    last = min(border, key=lambda e: ((w[e.vertices[0]] + w[e.vertices[1]]) / 2 - end).length)
    mid = (w[last.vertices[0]] + w[last.vertices[1]]) / 2
    start = C + (mid - C).normalized() * (R + 0.004)
    h = press(ctx, start, [on_sphere(80, -20 + 0.4 * i) for i in range(1, 10)] + arc(80, -16, 0)[1:])
    added = len(me.polygons) - n
    check(h.changed and added >= 4 and len(me.vertices) - nv == 2 * added,
          f"{added} more quads, starting from the edge (2 new vertices each, none for a first row)")
    check(parts(obj) == 1 and manifold(obj), "still one strip, edges with one or two faces")
    ok, worst = on_surface(truth, obj)
    check(ok and outward(obj), f"on the surface (worst {worst * 1000:.3f} mm), facing out")
    # Back over its own face from the start edge: nothing.
    w = world_verts(obj)
    border = [e for e in me.edges if sum(1 for p in me.polygons if e.key in p.edge_keys) == 1]
    first = min(border, key=lambda e: ((w[e.vertices[0]] + w[e.vertices[1]]) / 2 - on_sphere(80, -60)).length)
    mid = (w[first.vertices[0]] + w[first.vertices[1]]) / 2
    n = len(me.polygons)
    h = press(ctx, C + (mid - C).normalized() * (R + 0.004), arc(80, -60, -45)[1:])
    check(not h.changed and len(me.polygons) == n, "a drag from a border edge back over its face lays nothing")
    h = press(ctx, C + (mid - C).normalized() * (R + 0.004), arc(80, -60, -75)[1:])
    check(h.changed and len(me.polygons) > n and parts(obj) == 1, "the other way, it carries the strip on")


# --- quad -------------------------------------------------------------------------------

def test_quad(ref, truth):
    print("== Quad")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    fresh(ctx, 'QUAD')
    g = [[on_sphere(90 - 4 * j, 30 + 4 * i) for i in range(3)] for j in range(3)]  # g[row][col], ~2 cm apart
    for p in (g[0][0], g[0][1], g[1][1]):
        h = press(ctx, p)
        check(h is not None and h.changed, "a click on the surface: a vertex (one undo step)")
    obj = retopo.result(ctx)
    me = obj.data
    check(len(me.vertices) == 3 and len(me.polygons) == 0 and retopo.pen_count() == 3, "three points, open quad of 3")
    check(hud.status_size(ctx, s) == "3 of 4", "the status counts them")
    press(ctx, g[1][0])
    check(len(me.polygons) == 1 and len(me.polygons[0].vertices) == 4 and outward(obj), "the fourth makes a quad, facing out")
    check(retopo.pen_count() == 2, "its last edge stays open")
    press(ctx, g[2][0])
    press(ctx, g[2][1])
    check(len(me.polygons) == 2 and len(me.vertices) == 6 and manifold(obj), "two more clicks: the next quad on that edge")
    nv = len(me.vertices)
    press(ctx, g[2][1] + Vector((0.004, 0.0, 0.0)))
    check(len(me.vertices) == nv and retopo.pen_count() == 2, "a click on a vertex of the open quad changes nothing")
    retopo.end_quad()
    press(ctx, g[2][1] + Vector((0.004, 0.0, 0.0)))
    check(len(me.vertices) == nv and retopo.pen_count() == 1, "a click by a vertex snaps to it (no new vertex)")
    retopo.end_quad()
    # A bow tie order still makes a proper quad.
    b = [on_sphere(70, -90 + d) for d in (0, 4)] + [on_sphere(66, -90 + d) for d in (0, 4)]
    for p in (b[0], b[3], b[1], b[2]):
        press(ctx, p)
    face = me.polygons[-1]
    ws = [obj.matrix_world @ me.vertices[i].co for i in face.vertices]
    diag = sorted((ws[i] - ws[(i + 2) % 4]).length for i in range(2))
    perimeter = sum((ws[i] - ws[i - 1]).length for i in range(4))
    check(len(me.polygons) == 3 and perimeter < 0.09 and diag[0] > 0.025, f"a bow-tie click order: the shortest way round "
          f"({perimeter * 100:.1f} cm)")
    retopo.end_quad()
    # Three and the first again: a triangle.
    t = [on_sphere(110, 60), on_sphere(110, 64), on_sphere(106, 62)]
    for p in t + [t[0]]:
        press(ctx, p)
    check(len(me.polygons) == 4 and len(me.polygons[-1].vertices) == 3, "three, then the first again: a triangle")
    ok, worst = on_surface(truth, obj)
    check(ok, f"all on the surface (worst {worst * 1000:.3f} mm)")
    return obj, g


def test_quad_edges(ref, truth, obj, g):
    print("== Quad: border edges and vertices")
    ctx = bpy.context
    me = obj.data
    retopo.end_quad()
    nf, nv = len(me.polygons), len(me.vertices)
    # Click the right border edge of the first quads, then two points: a quad on it.
    edge_mid = (g[0][1] + g[1][1]) / 2
    h = press(ctx, C + (edge_mid - C).normalized() * (R + 0.003))
    check(isinstance(h, retopo.EdgePress) and not h.changed and retopo.pen_count() == 2,
          "a click on a border edge opens a quad from it (no undo step)")
    press(ctx, g[1][2])
    press(ctx, g[0][2])
    check(len(me.polygons) == nf + 1 and len(me.vertices) == nv + 2 and manifold(obj), "two clicks close it")
    retopo.end_quad()
    # Drag the top edge out.
    nf, nv = len(me.polygons), len(me.vertices)
    top = (g[2][0] + g[2][1]) / 2
    start = C + (top - C).normalized() * (R + 0.003)
    h = press(ctx, start, [on_sphere(82 - 4 - 1.0 * k, 32) for k in range(1, 5)])
    check(isinstance(h, retopo.EdgePress) and h.changed and len(me.polygons) == nf + 1 and len(me.vertices) == nv + 2,
          "dragging a border edge pulls a quad out of it")
    ok, worst = on_surface(truth, obj)
    check(ok and outward(obj) and manifold(obj), f"on the surface (worst {worst * 1000:.3f} mm), facing out")
    # Drag a vertex onto another: merged.
    nv = len(me.vertices)
    w = world_verts(obj)
    corner = min(range(nv), key=lambda i: (w[i] - g[0][2]).length)
    other = min((i for i in range(nv) if i != corner), key=lambda i: (w[i] - w[corner]).length)
    target = w[other]
    h = press(ctx, w[corner], [w[corner].lerp(target, f) for f in (0.9, 1.0)])  # Past the grab threshold, then on.
    check(isinstance(h, retopo.VertexPress) and h.changed and len(me.vertices) == nv - 1,
          "dragging a vertex onto another merges them")
    nv = len(me.vertices)
    w = world_verts(obj)
    i = nv - 1
    h = press(ctx, w[i], [w[i] + Vector((0.0, 0.0, 0.016)), w[i] + Vector((0.0, 0.0, 0.02))])
    moved = obj.matrix_world @ me.vertices[i].co
    check(h.changed and (moved - w[i]).length > 0.012 and truth.distance(moved) < 1e-4 and len(me.vertices) == nv,
          "a dragged vertex slides along the surface")


# --- relax, tweak, delete -------------------------------------------------------------

def test_relax(ref, truth):
    print("== Relax")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    fresh(ctx, 'STRIP')
    path = arc(95, 100, 140)
    press(ctx, path[0], path[1:])
    obj = retopo.result(ctx)
    me = obj.data
    # Squeeze one side vertex toward its neighbor along the side.
    w = world_verts(obj)
    border_nbrs = {}
    for e in me.edges:
        a, b = e.vertices
        border_nbrs.setdefault(a, []).append(b)
        border_nbrs.setdefault(b, []).append(a)
    inner = [i for i in range(len(w)) if len(border_nbrs[i]) == 3]
    i = inner[len(inner) // 2]
    side = [j for j in border_nbrs[i] if len(border_nbrs[j]) == 3]
    j = side[0]
    corners = {k: w[k].copy() for k in range(len(w)) if len(border_nbrs[k]) == 2}
    me.vertices[i].co = obj.matrix_world.inverted() @ retopo.surface(ctx, ref).project(w[i].lerp(w[j], 0.6))
    me.update()

    def spread():
        ws = world_verts(obj)
        a, b = (ws[i] - ws[j]).length, (ws[i] - ws[[k for k in side if k != j][0]]).length if len(side) > 1 else 0.0
        return abs(a - b)
    before = spread()
    s.retopo_relax = 0.5
    retopo.set_tool(ctx, 'RELAX')
    h = retopo.pick(ctx, s, False, at(w[i]), at(w[i]), 1.0, 1.0)
    check(isinstance(h, retopo.Relax), "Relax: a brush")
    for _ in range(40):
        h.step(w[i], 1.0, 1.0 / 90.0)
    h.finish(ctx)
    after = spread()
    check(h.changed and after < before * 0.4, f"evens the spacing along the side ({before * 100:.2f} -> {after * 100:.2f} cm)")
    ok, worst = on_surface(truth, obj)
    check(ok, f"keeps every vertex on the surface (worst {worst * 1000:.3f} mm)")
    ws = world_verts(obj)
    check(all((ws[k] - p).length < 1e-6 for k, p in corners.items()), "corners stay")
    return obj


def test_tweak(ref, truth, obj):
    print("== Tweak")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    retopo.set_tool(ctx, 'TWEAK')
    s.retopo_size = 0.02
    w = world_verts(obj)
    i = len(w) // 2
    far = [k for k in range(len(w)) if (w[k] - w[i]).length > 0.03]
    near = [k for k in range(len(w)) if k != i and 0.004 < (w[k] - w[i]).length < 0.018]
    check(retopo.pick(ctx, s, False, at(C + Vector((0, 0, 2))), at(C + Vector((0, 0, 2))), 1.0, 1.0) is None,
          "nothing in reach: no handler")
    up = Vector((0.0, 0.0, 0.02))
    h = press(ctx, w[i], [w[i] + up * 0.85, w[i] + up])  # Past the grab threshold, then on.
    ws = world_verts(obj)
    moved = (ws[i] - w[i]).length
    check(h.changed and 0.014 < moved < 0.021, f"the vertex at the tip follows it ({moved * 100:.2f} cm)")
    check(all((ws[k] - w[k]).length < moved for k in near) and all((ws[k] - w[k]).length < 1e-7 for k in far),
          "with falloff: near ones less, far ones not at all")
    ok, worst = on_surface(truth, obj)
    check(ok, f"sliding on the surface (worst {worst * 1000:.3f} mm)")


def test_delete(ref, obj):
    print("== the off hand deletes")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    s.retopo_size = 0.02
    me = obj.data
    nf = len(me.polygons)
    centers = [obj.matrix_world @ p.center for p in me.polygons]
    h = press(ctx, C + (centers[0] - C).normalized() * (R + 0.003), off=True)
    check(isinstance(h, retopo.Delete) and h.changed and len(me.polygons) == nf - 1, "a press on a face deletes it")
    loose = [v for v in me.vertices if not any(v.index in e.vertices for e in me.edges)]
    check(not loose, "and the vertices only it used")
    nf = len(me.polygons)
    path = [C + (c - C).normalized() * (R + 0.003) for c in centers[2:5]]
    h = press(ctx, path[0], path[1:], off=True)
    check(h.changed and len(me.polygons) == nf - 3, "a sweep deletes every face it passes")
    retopo._state.update(pen=[(0, tuple(me.vertices[0].co))], pen_key=obj.name)
    nf = len(me.polygons)
    h = press(ctx, C + Vector((0.0, 0.0, -1.0)), off=True)
    check(h is not None and not h.changed and len(me.polygons) == nf and retopo.pen_count() == 0,
          "in empty space: nothing deleted (no undo step), the open quad ends")


# --- mirror -----------------------------------------------------------------------------

def _mirrored_everywhere(obj, tol=2e-4):
    co = [v.co.copy() for v in obj.data.vertices]
    return all(any((Vector((-a.x, a.y, a.z)) - b).length < tol for b in co) for a in co)


def test_mirror(ref, truth):
    print("== Mirror X")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    s.retopo_mirror = True
    s.retopo_size = 0.02
    fresh(ctx, 'STRIP')
    path = arc(60, -70, -30)
    press(ctx, path[0], path[1:])
    obj = retopo.result(ctx)
    me = obj.data
    n = len(me.polygons)
    xs = [v.co.x for v in me.vertices]
    check(n >= 6 and n % 2 == 0 and sum(1 for x in xs if x > 0) == sum(1 for x in xs if x < 0),
          f"a strip on one side: its mirror image on the other ({n} quads)")
    check(_mirrored_everywhere(obj) and outward(obj) and manifold(obj), "every vertex has its twin; faces face out")
    ok, worst = on_surface(truth, obj)
    check(ok, f"on the surface (worst {worst * 1000:.3f} mm)")
    # A strip along the center line: symmetric, nothing doubled.
    fresh(ctx, 'STRIP')
    path = [C + (Vector((0.002, -math.sin(math.radians(a)), math.cos(math.radians(a))))).normalized() * (R + 0.004)
            for a in np.linspace(40, 80, 80)]
    press(ctx, path[0], path[1:])
    obj = retopo.result(ctx)
    me = obj.data
    n = len(me.polygons)
    check(n >= 8 and len(me.vertices) == 2 * n + 2 and manifold(obj),
          f"a strip along the center line: one strip, centered ({n} quads, {len(me.vertices)} vertices)")
    check(_mirrored_everywhere(obj, 1e-5), "its sides mirror each other")
    # A click near the center line lands on it.
    fresh(ctx, 'QUAD')
    press(ctx, C + Vector((0.003, -1.0, 0.1)).normalized() * (R + 0.003))
    obj = retopo.result(ctx)
    check(len(obj.data.vertices) == 1 and obj.data.vertices[0].co.x == 0.0, "a point near the center line sits on it")
    press(ctx, on_sphere(80, -70))
    check(len(obj.data.vertices) == 3, "one off it gets its twin")
    retopo.end_quad()
    # Relax and Tweak keep the twins mirrored.
    fresh(ctx, 'STRIP')
    path = arc(100, -60, -30)
    press(ctx, path[0], path[1:])
    obj = retopo.result(ctx)
    retopo.set_tool(ctx, 'TWEAK')
    w = world_verts(obj)
    i = max(range(len(w)), key=lambda k: w[k].x)
    h = press(ctx, w[i], [w[i] + Vector((0.0, 0.0, 0.017)), w[i] + Vector((0.0, 0.0, 0.02))])
    check(h.changed and (world_verts(obj)[i] - w[i]).length > 0.01, "a tweak on one side")
    retopo.set_tool(ctx, 'RELAX')
    h = retopo.pick(ctx, s, False, at(w[i]), at(w[i]), 1.0, 1.0)
    for _ in range(10):
        h.step(w[i], 1.0, 1.0 / 90.0)
    check(_mirrored_everywhere(obj, 1e-5), "Tweak and Relax move twins the mirrored way")
    nf = len(obj.data.polygons)
    c = obj.matrix_world @ obj.data.polygons[0].center
    press(ctx, C + (c - C).normalized() * (R + 0.003), off=True)
    check(len(obj.data.polygons) == nf - 2 and _mirrored_everywhere(obj, 1e-5), "the off hand deletes both sides")
    s.retopo_mirror = False


# --- undo guard, panel, modes, hints ----------------------------------------------------

def test_undo_guard(ref):
    print("== undo guard")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    fresh(ctx, 'QUAD')
    press(ctx, on_sphere(130, 0))
    obj = retopo.result(ctx)
    retopo.view(obj)
    check(retopo._surface["surface"] is not None and retopo._view["key"] is not None and retopo.pen_count() == 1,
          "caches: the surface, the drawn mesh, the open quad")
    operators._on_data_swap()
    check(retopo._surface["surface"] is None and retopo._view["key"] is None and retopo.pen_count() == 0,
          "an undo clears them all")
    retopo.set_tool(ctx, 'STRIP')
    h = retopo.pick(ctx, s, False, at(on_sphere(130, 10)), at(on_sphere(130, 10)), 1.0, 1.0)
    h.abandon()
    check(h.ed.freed, "a held stroke dropped by an undo frees its bmesh")
    # The pen survives our own writes (indices stay), not the mesh changing under it.
    retopo.set_tool(ctx, 'QUAD')
    press(ctx, on_sphere(130, 20))
    press(ctx, on_sphere(130, 24))
    check(retopo.pen_count() == 2, "the open quad")
    obj.data.vertices[-1].co.x += 0.01
    obj.data.update()
    press(ctx, on_sphere(126, 24))
    check(retopo.pen_count() == 1, "a vertex of it moved elsewhere: it starts again")
    retopo.end_quad()


def test_rows(ref):
    print("== hand panel")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    menus.reset()
    pages = []
    for tool in ('STRIP', 'QUAD', 'RELAX', 'TWEAK'):
        retopo.set_tool(ctx, tool)
        for expanded in (False, True):
            menus.set_more('RETOPO', expanded)
            widgets, height = menus.layout(ctx)
            overlap = any(a is not b and a.rect[0] < b.rect[2] - 1e-9 and b.rect[0] < a.rect[2] - 1e-9 and
                          a.rect[1] < b.rect[3] - 1e-9 and b.rect[1] < a.rect[3] - 1e-9
                          for a in widgets for b in widgets)
            check(height < 0.32 and not overlap, f"{tool}{' + More' if expanded else ''}: fits ({height:.3f} m)")
            pages.append(widgets)
    menus.set_more('RETOPO', False)
    rows = menus.panel(ctx).rows
    check(2 <= len(rows) - 2 <= 5, f"{len(rows) - 2} controls up front")
    menus.set_more('RETOPO', True)
    need = {"retopo_tool_STRIP", "retopo_tool_QUAD", "retopo_tool_RELAX", "retopo_tool_TWEAK", "retopo_size",
            "retopo_mirror", "retopo_opacity", "retopo_mesh", "retopo_finish", "retopo_ref", "retopo_new",
            "retopo_wrap", "retopo_relax", "mode_retopo"}
    check(need <= ids(), f"tools, size, mirror, opacity, mesh, finish; More: reference, new, shrinkwrap, relax "
          f"(missing {sorted(need - ids())})")
    used = {w.icon for page in pages for w in page if w.icon}
    check(not (used - set(gfx.ICONS)), f"every icon is drawn (missing {sorted(used - set(gfx.ICONS))})")
    widget("retopo_tool_RELAX").on_press()
    check(s.retopo_tool == 'RELAX', "tool segment")
    widget("retopo_mirror").on_press()
    check(s.retopo_mirror, "Mirror X toggle")
    widget("retopo_mirror").on_press()
    obj = retopo.result(ctx)
    retopo._state["message"] = ("", 0.0)
    check(obj is not None and f"{len(obj.data.polygons)} faces" in menus.panel(ctx).rows[0][0].value_text,
          "the header counts the faces")
    widget("retopo_wrap").on_press()
    mod = obj.modifiers.get(retopo.SHRINKWRAP)
    check(mod is not None and mod.target == ref and retopo.has_shrinkwrap(obj), "Shrinkwrap: a modifier onto the reference")
    widget("retopo_wrap").on_press()
    check(not retopo.has_shrinkwrap(obj), "and off again")
    widget("retopo_mesh").on_press()
    check(menus.picker_open() and "opt_" in ids() and "opt_" + obj.name in ids(),
          "the Mesh select lists this reference's retopo meshes and New")
    vr_ui.find(menus.layout(ctx)[0], "opt_").on_press()
    check(retopo.result(ctx) is None and obj.display_type != 'WIRE', "New mesh: the old one shows as a mesh again")
    widget("retopo_mesh").on_press()
    vr_ui.find(menus.layout(ctx)[0], "opt_" + obj.name).on_press()
    check(retopo.result(ctx) == obj and obj.display_type == 'WIRE', "choosing it carries on with it")
    retopo.set_tool(ctx, 'QUAD')
    press(ctx, on_sphere(140, 40))
    check("retopo_end" in ids(), "an open quad: the header's End")
    widget("retopo_end").on_press()
    check(retopo.pen_count() == 0, "which ends it")
    menus.reset()


def test_modes(ref, obj):
    print("== switching modes")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    retopo.choose_result(ctx, obj.name)
    menus.set_mode(ctx, 'SCULPT')
    check(obj.display_type != 'WIRE' and not obj.show_in_front and ctx.view_layer.objects.active == ref,
          "leaving Retopo: the mesh shows as it was, the reference is still active (Sculpt sculpts it)")
    bpy.ops.vrsculpt.cycle_brush(off_hand=True)
    check(s.mode == 'RETOPO' and retopo.result(ctx) == obj and obj.display_type == 'WIRE',
          "Y after Sculpt: Retopo, carrying on with the same mesh")
    bpy.ops.vrsculpt.cycle_brush(off_hand=True)
    check(s.mode == 'MODEL', "then Model")
    # Starting from the retopo mesh itself.
    from vr_sculpt import primitives
    primitives.make_active(ctx, obj)
    s.retopo_object = None
    menus.set_mode(ctx, 'RETOPO')
    check(retopo.reference(ctx) == ref and retopo.result(ctx) == obj and ctx.view_layer.objects.active == ref,
          "entering with a retopo mesh active: it carries on, on its reference")
    # Starting from the reference with no pointer: its latest retopo mesh.
    menus.set_mode(ctx, 'SCULPT')
    s.retopo_object = None
    menus.set_mode(ctx, 'RETOPO')
    check(retopo.result(ctx) in retopo.results_of(ctx, ref), "entering on the reference: its latest retopo mesh carries on")
    retopo.set_tool(ctx, 'STRIP')
    bpy.ops.vrsculpt.cycle_brush()
    check(s.retopo_tool == 'QUAD', "B: next tool")
    retopo._state.update(pen=[(0, (0, 0, 0))], pen_key="x")
    bpy.ops.vrsculpt.cycle_brush()
    check(s.retopo_tool == 'QUAD' and retopo.pen_count() == 0, "B with an open quad: ends it")
    s.retopo_size = 0.02
    bpy.ops.vrsculpt.cycle_radius()
    check(abs(s.retopo_size - 0.04) < 1e-6, "stick click: the next size")
    s.retopo_size = 0.02
    retopo.choose_result(ctx, obj.name)
    n = len(obj.data.polygons)
    retopo.finish(ctx)
    check(s.mode == 'MODEL' and ctx.view_layer.objects.active == obj and obj.select_get() and obj.display_type != 'WIRE',
          "Finish: Model mode, the retopo mesh selected and shown as a mesh")
    check(retopo.message().endswith(f"{n} faces"), "and says how many faces")
    primitives.make_active(ctx, ref)
    menus.set_mode(ctx, 'RETOPO')


def test_hints_and_status():
    print("== hints and tip status")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    Info = controllers.Info

    def label(hand, button, info=Info()):
        h = controllers.hints(s, info)[hand][button]
        return h[1] if h else None

    s.mode = 'RETOPO'
    names = {'STRIP': "Lay strip", 'QUAD': "Place point", 'RELAX': "Relax", 'TWEAK': "Tweak"}
    for tool, name in names.items():
        retopo.set_tool(ctx, tool)
        title, desc, color, icon = hud.card_content(s, None)
        check(label('right', 'trigger') == name and label('left', 'trigger') == "Delete" and icon in gfx.ICONS
              and title == retopo.INFO[tool][0], f"{tool}: '{name}', the off hand deletes; status '{title}'")
    check(label('right', 'top') == "Next tool" and label('right', 'stick_click') == "Size", "B: next tool; click: size")
    retopo._state.update(pen=[(0, (0, 0, 0))], pen_key="x")
    check(label('right', 'top') == "End quad", "B with an open quad: End quad")
    retopo.end_quad()
    check(label('left', 'top') == "Next: Model", "Y: Model comes next")
    s.mode = 'SCULPT'
    check(label('left', 'top') == "Next: Retopo", "after Sculpt: Retopo")
    s.mode = 'RETOPO'
    retopo.set_tool(ctx, 'STRIP')
    s.retopo_size = 0.02
    check(hud.status_size(ctx, s) == "2.0 cm", "the status shows the size")


def test_operator(ref):
    print("== the trigger operator")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    s.show_panel = False
    s.tip_offset = 0.0
    fresh(ctx, 'STRIP')
    ss = tm.FakeSessionState()
    fctx = tm.FakeContext(tm.FakeWM(ss))
    running = operators.session_running
    operators.session_running = lambda context: True
    try:
        _op, r = tm.press(fctx, ss, 1, [on_sphere(120, -100), on_sphere(120, -99.8)])
        check(r == {'CANCELLED'}, "a click with Strip is no undo step")
        _op, r = tm.press(fctx, ss, 1, arc(120, -100, -80, 1.0))
        obj = retopo.result(ctx)
        check(r == {'FINISHED'} and obj is not None and len(obj.data.polygons) >= 4, "a strip is one undo step")
        n = len(obj.data.polygons)
        c = obj.matrix_world @ obj.data.polygons[0].center
        _op, r = tm.press(fctx, ss, 0, [C + (c - C).normalized() * (R + 0.003)])
        check(r == {'FINISHED'} and len(obj.data.polygons) == n - 1, "the left trigger deletes a face (one step)")
        _op, r = tm.press(fctx, ss, 0, [C + Vector((0.0, 0.0, -1.0))])
        check(r == {'CANCELLED'}, "and in empty space it's no step")
    finally:
        operators.session_running = running
        s.show_panel = True


def test_speed(ctx):
    print("== a 100k-face reference")
    s = ctx.scene.vr_sculpt
    menus.set_mode(ctx, 'SCULPT')
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    big = make_reference(ctx, 320, 160, "Big")
    big.data.update()
    menus.set_mode(ctx, 'RETOPO')
    surf = retopo._surface["surface"]
    check(surf.triangles > 100000 and surf.ms < 500, f"the tree of {surf.triangles} triangles in {surf.ms:.0f} ms")
    fresh(ctx, 'STRIP')
    path = arc(85, -170, 170, 0.25)
    h = retopo.pick(ctx, s, False, at(path[0]), at(path[0]), 1.0, 1.0)
    times = []
    for p in path[1:]:
        t0 = time.perf_counter()
        h.update(ctx, at(p), at(p), 1.0)
        times.append(time.perf_counter() - t0)
    h.finish(ctx)
    obj = retopo.result(ctx)
    check(len(obj.data.polygons) > 80 and max(times) < 0.01,
          f"a strip around it ({len(obj.data.polygons)} quads): {np.mean(times) * 1000:.2f} ms an update on average, "
          f"{max(times) * 1000:.2f} ms at most")
    retopo.set_tool(ctx, 'RELAX')
    w = world_verts(obj)
    h = retopo.pick(ctx, s, False, at(w[10]), at(w[10]), 1.0, 1.0)
    t0 = time.perf_counter()
    for _ in range(20):
        h.step(w[10], 1.0, 1.0 / 90.0)
    ms = (time.perf_counter() - t0) / 20 * 1000
    check(ms < 5.0, f"a Relax step: {ms:.2f} ms")
    t0 = time.perf_counter()
    retopo.view(obj)
    hv = retopo.hover(ctx, s, retopo._view, surf, (w[3], w[10]), (at(w[3]), at(w[10])), 1.0)
    ms = (time.perf_counter() - t0) * 1000
    check(hv["hit"] is not None and ms < 5.0, f"rebuilding the drawn mesh and the hover tests: {ms:.2f} ms")


def main():
    vr_sculpt.register()
    try:
        ctx = bpy.context
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        s = ctx.scene.vr_sculpt
        s.grab_delay = 0.0  # Drags open the gate by moving.
        s.grid_snap = False
        s.retopo_mirror = False
        ref = make_reference(ctx)
        truth = Truth(ref)
        test_enter(ref)
        obj = test_strip(ref, truth)
        test_continue(ref, truth, obj)
        quad, g = test_quad(ref, truth)
        test_quad_edges(ref, truth, quad, g)
        relaxed = test_relax(ref, truth)
        test_tweak(ref, truth, relaxed)
        test_delete(ref, relaxed)
        test_mirror(ref, truth)
        test_undo_guard(ref)
        test_rows(ref)
        test_modes(ref, obj)
        test_hints_and_status()
        test_operator(ref)
        test_speed(ctx)
    except Exception:
        traceback.print_exc()
        failures.append("raised")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")


if __name__ == "__main__":
    main()
