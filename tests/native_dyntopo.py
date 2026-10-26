"""Windowed test of the native engine with dynamic topology, without a headset.

blender --factory-startup --python tests/native_dyntopo.py

Writes tests/native_dyntopo_result.txt. Dyntopo only writes obj.data back when Sculpt Mode ends, so a
stroke's snapshot of the mesh can be far behind the surface it sculpts. This grows a bump stroke by
stroke, each one further out than the snapshot reaches, with and without NativeStroke's fallback.
"""

import os
import sys
import traceback

import bpy
import numpy as np
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "tests", "native_dyntopo_result.txt")
lines = []
failures = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    lines.append(s)


def check(cond, msg):
    log(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def quit_cleanly():
    try:
        import vr_sculpt
        vr_sculpt.unregister()
    except Exception:
        traceback.print_exc()
    bpy.ops.wm.quit_blender()


def positions(obj):
    a = np.empty(len(obj.data.vertices) * 3)
    obj.data.vertices.foreach_get("co", a)
    return a.reshape(-1, 3)


def uv_edges(me, name):
    u = np.empty(len(me.loops) * 2)
    me.uv_layers[name].data.foreach_get("uv", u)
    u = u.reshape(-1, 2)
    worst = 0.0
    for p in me.polygons:
        f = u[p.loop_start:p.loop_start + p.loop_total]
        worst = max(worst, float(np.linalg.norm(f - np.roll(f, 1, axis=0), axis=1).max()))
    return worst


def keeps_uvs(ctx, menus, native):
    """Real dyntopo strokes on a UV sphere with a planar UV map and a shape key (rest.py)."""
    from vr_sculpt import rest
    bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16, radius=1.0)
    obj = ctx.active_object
    me = obj.data
    co = positions(obj)
    lv = np.array([lp.vertex_index for lp in me.loops])
    me.uv_layers.new(name="Probe").data.foreach_set("uv", (co[lv, :2] * 0.5 + 0.5).ravel())
    obj.shape_key_add(name="Basis", from_mix=False)
    smile = obj.shape_key_add(name="Smile", from_mix=False)
    smile.data.foreach_set("co", (co + np.stack([0 * co[:, 0]] * 2 + [0.1 * co[:, 0]], axis=1)).ravel())
    n0 = len(me.vertices)
    menus.set_object_mode(ctx, obj, 'SCULPT')
    native.activate_brush(ctx, "Draw")
    native.toggle_dyntopo(ctx)
    sculpt = ctx.tool_settings.sculpt
    sculpt.detail_type_method = 'CONSTANT'
    sculpt.constant_detail_resolution = 40.0
    for k in range(4):
        s = native.NativeStroke(ctx, obj, 0.1)
        for i in range(15):
            a = (i - 7) * 0.02
            s.update(Vector((np.sin(a), -np.cos(a), 0.1 * k)).normalized() * (1.0 + 0.02 * k), None, 1.0)
            bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
        s.finish()
    # Leave as the desktop would, to see the new mesh before the live timer's tick restores it.
    with ctx.temp_override(window=ctx.window_manager.windows[0]):
        win = ctx.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        with ctx.temp_override(window=win, area=area, region=region):
            bpy.ops.object.mode_set(mode='OBJECT')
    r = rest._vectors(me, rest.REST)
    lv = np.array([lp.vertex_index for lp in me.loops])
    u = np.empty(len(me.loops) * 2)
    me.uv_layers["Probe"].data.foreach_get("uv", u)
    before = np.abs(u.reshape(-1, 2) - (r[lv, :2] * 0.5 + 0.5))
    log(f"dyntopo on a UV sphere: verts {n0} -> {len(me.vertices)}; as Blender left them,"
        f" {(before.max(axis=1) > 0.01).mean():.0%} of the Probe corners are off (max {before.max():.2f}),"
        f" session open: {rest.is_open(me)}")
    rest.tick(ctx)
    me.uv_layers["Probe"].data.foreach_get("uv", u)
    err = np.abs(u.reshape(-1, 2) - (r[lv, :2] * 0.5 + 0.5)).max()
    check(len(me.vertices) > n0 and err < 1e-3, f"planar UVs back after real dyntopo strokes (max error {err:.2e})")
    edge = uv_edges(me, "UVMap")
    check(edge < 0.25, f"the seamed UVMap: no face across the seam (longest UV edge {edge:.3f})")
    keys = me.shape_keys
    if keys is None or "Smile" not in keys.key_blocks:
        check(False, "shape keys back")
    else:
        k = np.empty(len(me.vertices) * 3)
        keys.key_blocks["Smile"].data.foreach_get("co", k)
        off = k.reshape(-1, 3) - positions(obj)
        check(np.abs(off[:, 2] - 0.1 * r[:, 0]).max() < 1e-3, "Smile keeps its offset on the new vertices")
    bpy.data.objects.remove(obj)


def main():
    import vr_sculpt
    from vr_sculpt import menus, native
    vr_sculpt.register()
    try:
        ctx = bpy.context
        win = ctx.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        rv3d = area.spaces.active.region_3d
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0, 0, 0)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.5708)
        rv3d.view_distance = 6.0
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        heights = {}
        for fallback in (True, False):
            bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=4, radius=1.0, calc_uvs=False)
            obj = ctx.active_object
            n0 = len(obj.data.vertices)
            menus.set_object_mode(ctx, obj, 'SCULPT')
            native.activate_brush(ctx, "Draw")
            native.toggle_dyntopo(ctx)
            sculpt = ctx.tool_settings.sculpt
            sculpt.detail_type_method = 'CONSTANT'
            sculpt.constant_detail_resolution = 60.0
            check(obj.use_dynamic_topology_sculpting, "dyntopo on from toggle_dyntopo()")
            if fallback:
                # A stroke in the air: dabs go out (nothing to aim at in reach), the mesh doesn't change.
                s = native.NativeStroke(ctx, obj, 0.08)
                for i in range(10):
                    s.update(Vector((0.02 * i, -1.6, 0.0)), None, 1.0)
                s.finish()
                air_dabs = s.dabs
            total = 0
            # Each stroke starts further out: 8 strokes, 3 cm apart, from the sphere's surface.
            for k in range(8):
                s = native.NativeStroke(ctx, obj, 0.08)
                s.dyntopo = fallback
                for i in range(12):
                    a = (i - 6) * 0.01
                    s.update(Vector((np.sin(a), -np.cos(a), 0.0)) * (1.0 + 0.03 * k), None, 1.0)
                    bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
                s.finish()
                total += s.dabs
            menus.set_object_mode(ctx, obj, 'OBJECT')
            co = positions(obj)
            heights[fallback] = float(-co[:, 1].min())
            log(f"fallback {'on ' if fallback else 'off'}: {total} dabs, verts {n0} -> {len(co)},"
                f" bump top at {heights[fallback]:.3f} (sphere 1.0, stroke 8 starts at 1.21)")
            if fallback:
                check(air_dabs > 0, f"a stroke in the air still sends dabs with dyntopo ({air_dabs})")
                check(len(co) > n0, "dyntopo added vertices")
            bpy.data.objects.remove(obj)
        check(heights[True] > 1.0 + 0.08 + 0.05, "the bump grows past what the stale snapshot reaches")
        check(heights[True] > heights[False] + 0.03, "and higher than without the fallback")
        keeps_uvs(ctx, menus, native)
    except Exception:
        log(traceback.format_exc())
        failures.append("raised")
    log("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    quit_cleanly()


bpy.app.timers.register(lambda: (main(), None)[1], first_interval=1.5)
