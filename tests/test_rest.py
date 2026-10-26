"""Background tests for rest.py: UVs, corner data and shape keys kept through dyntopo.

blender --background --factory-startup --python tests/test_rest.py

Real dyntopo strokes need a window (tests/native_dyntopo.py does those). Here a session's topology
change is faked with bmesh, which interpolates point attributes the way dyntopo does: every edge split,
triangulated, the corner layers wiped and the vertices moved.
"""

import os
import sys
import time
import traceback

import bmesh
import bpy
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import vr_sculpt  # noqa: E402
from vr_sculpt import menus, native, rest  # noqa: E402

failures = []
SMEARED = 0.25  # a face across the UV sphere's seam spans ~1 in UV; its own faces up to ~0.1 (diagonals)


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def positions(me):
    a = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", a)
    return a.reshape(-1, 3)


def loop_verts(me):
    a = np.empty(len(me.loops), dtype=np.int64)
    me.loops.foreach_get("vertex_index", a)
    return a


def uv(me, name):
    a = np.empty(len(me.loops) * 2)
    me.uv_layers[name].data.foreach_get("uv", a)
    return a.reshape(-1, 2)


def build():
    """A UV sphere (its UVMap has a seam), a planar Probe UV map, a corner color, an int corner layer
    and a shape key with settings and a driver."""
    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=1.0)
    obj = bpy.context.active_object
    me = obj.data
    co = positions(me)
    lv = loop_verts(me)
    probe = me.uv_layers.new(name="Probe")
    probe.data.foreach_set("uv", (co[lv, :2] * 0.5 + 0.5).ravel())
    me.uv_layers.active = me.uv_layers["UVMap"]
    col = me.color_attributes.new("Paint", 'FLOAT_COLOR', 'CORNER')
    c = np.ones((len(lv), 4))
    c[:, 0] = co[lv, 2] * 0.5 + 0.5
    col.data.foreach_set("color", c.ravel())
    side = me.attributes.new("side", 'INT', 'CORNER')
    side.data.foreach_set("value", (co[lv, 0] > 0).astype(np.int32))
    obj.vertex_groups.new(name="Mouth")
    obj.shape_key_add(name="Basis", from_mix=False)
    smile = obj.shape_key_add(name="Smile", from_mix=False)
    smile.data.foreach_set("co", (co + np.stack([0 * co[:, 0], 0 * co[:, 0], 0.1 * co[:, 0]], axis=1)).ravel())
    smile.slider_min = -1.0
    smile.value = 0.4
    smile.vertex_group = "Mouth"
    drv = me.shape_keys.driver_add('key_blocks["Smile"].value')
    drv.driver.expression = "frame / 100"
    obj.active_shape_key_index = 1
    return obj


def fake_session(obj):
    """What dyntopo does to obj.data, roughly: new vertices, triangles, corner layers reset, sculpted."""
    me = obj.data
    bm = bmesh.new()
    bm.from_mesh(me)
    bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=1, use_grid_fill=True)
    bmesh.ops.triangulate(bm, faces=bm.faces[:])
    bm.to_mesh(me)
    bm.free()
    for a in [a for a in me.attributes if a.domain == 'CORNER' and not a.name.startswith(".")]:
        if a.data_type == 'FLOAT2':
            a.data.foreach_set("vector", np.zeros(len(me.loops) * 2))
        elif a.data_type == 'FLOAT_COLOR':
            a.data.foreach_set("color", np.ones(len(me.loops) * 4))
        elif a.data_type == 'INT':
            a.data.foreach_set("value", np.zeros(len(me.loops), dtype=np.int32))
    co = positions(me)
    co *= (1.0 + 0.15 * np.sin(co[:, 2] * 5.0))[:, None]  # A sculpted wave.
    me.vertices.foreach_set("co", co.ravel())
    me.update()


def max_uv_edge(me, name):
    """The longest edge in UV space over all faces: a face smeared across a seam spans ~1."""
    u = uv(me, name)
    starts = np.empty(len(me.polygons), dtype=np.int64)
    totals = np.empty(len(me.polygons), dtype=np.int64)
    me.polygons.foreach_get("loop_start", starts)
    me.polygons.foreach_get("loop_total", totals)
    worst = 0.0
    for s, t in zip(starts, totals):
        f = u[s:s + t]
        worst = max(worst, float(np.linalg.norm(f - np.roll(f, 1, axis=0), axis=1).max()))
    return worst


def test_restore():
    print("== corners and shape keys back after a (faked) session")
    obj = build()
    me = obj.data
    seam_edge = max_uv_edge(me, "UVMap")
    check(rest.worth_keeping(me), "a mesh with UVs and shape keys is worth keeping")
    check(rest.prepare(obj), "prepare() opens a session")
    src = me.get(rest.SOURCE)
    check(src is not None and src.use_fake_user and rest.REST in me.attributes,
          "with rest positions and a fake-user source mesh")
    check(me.shape_keys is None and any(a.name.startswith(rest.KEY_PREFIX) for a in me.attributes),
          "shape keys become offsets while dyntopo runs")
    check(not rest.prepare(obj), "prepare() again does nothing while a session is open")
    fake_session(obj)
    n_verts = len(me.vertices)
    rest_co = rest._vectors(me, rest.REST)  # Where the new vertices are on the mesh from before.
    check(rest.finish(obj), "finish() closes it")
    co = positions(me)
    check(np.allclose(rest._vectors(me, rest.REST), co), "and takes the new rest positions")
    check(rest.SOURCE not in me and not any(m.name.startswith(".VRS rest") for m in bpy.data.meshes),
          "the source mesh is gone")
    lv = loop_verts(me)
    # The rest positions of the new vertices lie on the old sphere's faces, so a planar map comes back
    # exactly (it's linear) and the colors too.
    err = np.abs(uv(me, "Probe") - (rest_co[lv, :2] * 0.5 + 0.5)).max()
    check(err < 1e-4, f"planar UVs come back exactly (max error {err:.2e}) on {n_verts} vertices")
    check(max_uv_edge(me, "UVMap") < SMEARED,
          f"the seamed UV map: no face smeared across the seam (longest UV edge {max_uv_edge(me, 'UVMap'):.3f},"
          f" was {seam_edge:.3f}; triangles add diagonals)")
    check(me.uv_layers.active.name == "UVMap", "the active UV map is kept")
    col = np.empty(len(lv) * 4)
    me.color_attributes["Paint"].data.foreach_get("color", col)
    err = np.abs(col.reshape(-1, 4)[:, 0] - (rest_co[lv, 2] * 0.5 + 0.5)).max()
    check(err < 1e-4, f"corner colors come back (max error {err:.2e})")
    side = np.empty(len(lv), dtype=np.int32)
    me.attributes["side"].data.foreach_get("value", side)
    off = np.abs(rest_co[lv, 0]) > 0.15
    check((side[off] == (rest_co[lv, 0] > 0)[off]).all(), "an int corner layer takes the nearest corner")
    keys = me.shape_keys
    check(keys is not None and [k.name for k in keys.key_blocks] == ["Basis", "Smile"], "shape keys rebuilt")
    if keys is not None:
        smile = keys.key_blocks["Smile"]
        k = np.empty(len(me.vertices) * 3)
        smile.data.foreach_get("co", k)
        offset = k.reshape(-1, 3) - co
        check(np.abs(offset[:, 2] - 0.1 * rest_co[:, 0]).max() < 1e-5 and np.abs(offset[:, :2]).max() < 1e-6,
              "Smile keeps its offset on the new vertices")
        check(smile.relative_key.name == "Basis" and abs(smile.slider_min + 1.0) < 1e-6 and
              abs(smile.value - 0.4) < 1e-6 and smile.vertex_group == "Mouth",
              "and its settings (relative key, range, value, vertex group)")
        drivers = keys.animation_data.drivers if keys.animation_data else []
        check(len(drivers) == 1 and drivers[0].driver.expression == "frame / 100", "and its driver")
        check(obj.active_shape_key_index == 1, "the active shape key is kept")
    me_name = me.name
    bpy.data.objects.remove(obj)
    bpy.data.meshes.remove(bpy.data.meshes[me_name])


def uv_intact(me, seam_edge):
    """Probe is planar in the rest positions, and the seamed UVMap has no face across its seam."""
    r = rest._vectors(me, rest.REST)
    err = np.abs(uv(me, "Probe") - (r[loop_verts(me), :2] * 0.5 + 0.5)).max()
    edge = max_uv_edge(me, "UVMap")
    print(f"       (Probe error {err:.2e}, longest UVMap edge {edge:.3f})")
    return err < 1e-4 and edge < SMEARED


def test_toggle_paths():
    print("== the dyntopo toggle and Sculpt Mode exits")
    ctx = bpy.context
    obj = build()
    me = obj.data
    seam_edge = max_uv_edge(me, "UVMap")
    menus.set_object_mode(ctx, obj, 'SCULPT')
    native.toggle_dyntopo(ctx)
    check(obj.use_dynamic_topology_sculpting and rest.is_open(me) and me.get(rest.DYNTOPO),
          "turning dyntopo on from VR opens a session (no second press)")
    native.toggle_dyntopo(ctx)
    check(not obj.use_dynamic_topology_sculpting and rest.is_open(me) and not me.get(rest.DIRTY),
          "turning it off restores the corners right away, still in Sculpt Mode")
    native.toggle_dyntopo(ctx)
    menus.set_object_mode(ctx, obj, 'OBJECT')
    check(not rest.is_open(me) and me.shape_keys is not None and uv_intact(me, seam_edge),
          "leaving Sculpt Mode closes it: UVs and shape keys back")
    menus.set_object_mode(ctx, obj, 'SCULPT')
    check(obj.use_dynamic_topology_sculpting and rest.is_open(me) and me.shape_keys is None,
          "entering again: Blender turns dyntopo back on, and the session opened first")
    # Left from the desktop (no VR involved): the live timer's tick closes it.
    with ctx.temp_override(active_object=obj, object=obj):
        bpy.ops.object.mode_set(mode='OBJECT')
    check(rest.is_open(me), "a desktop exit leaves the session open...")
    rest.tick(ctx)
    check(not rest.is_open(me) and me.shape_keys is not None, "...until the next tick closes it")
    # Dyntopo turned on from the desktop, on a mesh with rest positions: the tick opens a session.
    with ctx.temp_override(active_object=obj, object=obj):
        bpy.ops.object.mode_set(mode='SCULPT')
    rest.tick(ctx)
    check(rest.is_open(me), "a desktop dyntopo session on a mesh with rest positions gets a source too")
    with ctx.temp_override(active_object=obj, object=obj):
        bpy.ops.object.mode_set(mode='OBJECT')
    rest.tick(ctx)
    check(not rest.is_open(me) and uv_intact(me, seam_edge), "and its UVs back after")
    # Sculpt Mode left for Edit Mode: Edit Mode would write its own copy back, so wait for it to end.
    menus.set_object_mode(ctx, obj, 'SCULPT')
    with ctx.temp_override(active_object=obj, object=obj):
        bpy.ops.object.mode_set(mode='EDIT')
    rest.tick(ctx)
    check(rest.is_open(me), "Sculpt to Edit Mode: the session waits")
    with ctx.temp_override(active_object=obj, object=obj):
        bpy.ops.object.mode_set(mode='OBJECT')
    rest.tick(ctx)
    check(not rest.is_open(me) and uv_intact(me, seam_edge) and me.shape_keys is not None,
          "and closes after Edit Mode, UVs and shape keys back")
    # Nothing to keep: no rest positions, no session.
    bpy.ops.mesh.primitive_ico_sphere_add(calc_uvs=False)
    plain = ctx.active_object
    menus.set_object_mode(ctx, plain, 'SCULPT')
    native.toggle_dyntopo(ctx)
    check(not rest.is_open(plain.data) and rest.REST not in plain.data.attributes,
          "a mesh with nothing to keep gets no session")
    menus.set_object_mode(ctx, plain, 'OBJECT')
    for o in (obj, plain):
        me = o.data
        bpy.data.objects.remove(o)
        bpy.data.meshes.remove(me)


def test_texture_refresh():
    print("== Show texture / Auto (dyntopo session kept going)")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    obj = build()
    me = obj.data
    seam_edge = max_uv_edge(me, "UVMap")
    s.engine = 'NATIVE'
    menus.set_mode(ctx, 'SCULPT')
    menus.set_more('SCULPT', True)
    from vr_sculpt import vr_ui
    check("dyntopo_texture" not in {w.id for w in menus.layout(ctx)[0]}, "no texture button while dyntopo is off")
    native.toggle_dyntopo(ctx)
    ids = {w.id for w in menus.layout(ctx)[0]}
    check({"dyntopo_texture", "dyntopo_auto_texture"} <= ids, "dyntopo on a mesh with UVs: Show texture and Auto")
    widgets, height = menus.layout(ctx)
    check(height < 0.32, f"the panel still fits ({height:.3f} m)")
    first_source = me[rest.SOURCE]
    vr_ui.find(widgets, "dyntopo_texture").on_press()
    check(obj.mode == 'SCULPT' and obj.use_dynamic_topology_sculpting and rest.is_open(me)
          and me[rest.SOURCE] != first_source and me.shape_keys is None,
          "Show texture: back in Sculpt Mode with dyntopo on, in a new session")
    check(uv_intact(me, seam_edge), "with the UVs restored on the mesh it shows")
    # Auto: only once a dyntopo stroke has ended, AUTO_TEXTURE_SECONDS ago, with no hand on a trigger.
    source = me[rest.SOURCE]
    s.dyntopo_auto_texture = True
    native.auto_texture(ctx, s, False)
    check(me[rest.SOURCE] == source, "Auto: nothing to do before a stroke")
    native._dyntopo.update(stale=True, stroke_end=time.monotonic())
    native.auto_texture(ctx, s, False)
    check(me[rest.SOURCE] == source, "nor right after one")
    native._dyntopo["stroke_end"] -= native.AUTO_TEXTURE_SECONDS + 0.1
    native.auto_texture(ctx, s, True)
    check(me[rest.SOURCE] == source, "nor while a trigger is held")
    native.auto_texture(ctx, s, False)
    check(me[rest.SOURCE] != source and rest.is_open(me) and obj.use_dynamic_topology_sculpting,
          "but after the pause it shows the texture")
    s.dyntopo_auto_texture = False
    menus.set_object_mode(ctx, obj, 'OBJECT')
    check(not rest.is_open(me) and me.shape_keys is not None and uv_intact(me, seam_edge),
          "leaving afterwards still gives everything back")
    s.engine = 'PYTHON'
    menus.reset()
    bpy.data.objects.remove(obj)
    bpy.data.meshes.remove(me)


def main():
    vr_sculpt.register()
    try:
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        for t in (test_restore, test_toggle_paths, test_texture_refresh):
            try:
                t()
            except Exception:
                traceback.print_exc()
                failures.append(t.__name__ + " raised")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")


main()
