"""Probe: drive Blender's own texture paint (paint.image_paint) from a "controller" pose.

Idea: projection painting projects dabs along the 3D view, so for each dab we point the desktop
3D view from the controller at the tip, paint at the region center, and put the view back.

blender --factory-startup test_scene.blend --python tests/probe_texpaint.py
"""

import math
import os
import time
import traceback

import bpy
import numpy as np
from mathutils import Matrix, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "tests", "probe_texpaint_result.txt")
lines = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    lines.append(s)


def view3d():
    win = bpy.context.window_manager.windows[0]
    area = max((a for a in win.screen.areas if a.type == 'VIEW_3D'), key=lambda a: a.width * a.height)
    return win, area, next(r for r in area.regions if r.type == 'WINDOW')


def setup_object():
    obj = bpy.data.objects["SculptBall"]
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    log("uv layers:", [uv.name for uv in obj.data.uv_layers], "verts", len(obj.data.vertices))
    img = bpy.data.images.new("VRPaint", 512, 512, alpha=False)
    img.generated_color = (1, 1, 1, 1)
    mat = bpy.data.materials.new("VRPaintMat")
    mat.use_nodes = True
    tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
    tex.image = img
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    mat.node_tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    mat.node_tree.nodes.active = tex
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    return obj, img


def uv_at(obj, world_point, direction):
    """UV where a ray from world_point along direction hits obj (object space math)."""
    inv = obj.matrix_world.inverted()
    o = inv @ world_point
    d = (inv.to_3x3() @ direction).normalized()
    ok, loc, _n, face = obj.ray_cast(o, d)
    if not ok:
        return None
    mesh = obj.data
    poly = mesh.polygons[face]
    uvl = mesh.uv_layers.active.data
    # Fan-triangulate the face and find the triangle containing loc.
    from mathutils.geometry import barycentric_transform, intersect_point_tri
    vids = [mesh.loops[i].vertex_index for i in poly.loop_indices]
    uvs = [uvl[i].uv for i in poly.loop_indices]
    cos = [mesh.vertices[v].co for v in vids]
    for k in range(1, len(vids) - 1):
        a, b, c = cos[0], cos[k], cos[k + 1]
        if intersect_point_tri(loc, a, b, c) or k == len(vids) - 2:
            p = barycentric_transform(loc, a, b, c, uvs[0].to_3d(), uvs[k].to_3d(), uvs[k + 1].to_3d())
            return p.xy
    return None


def red_stats(img):
    px = np.empty(img.size[0] * img.size[1] * 4, dtype=np.float32)
    img.pixels.foreach_get(px)
    px = px.reshape(img.size[1], img.size[0], 4)
    red = (px[..., 0] > 0.6) & (px[..., 1] < 0.4)
    ys, xs = np.nonzero(red)
    if len(xs) == 0:
        return 0, None
    return len(xs), ((xs.mean() + 0.5) / img.size[0], (ys.mean() + 0.5) / img.size[1])


def look_from(rv3d, eye, target):
    """Point the 3D view from eye at target."""
    d = target - eye
    rot = d.to_track_quat('-Z', 'Y')
    rv3d.view_perspective = 'PERSP'
    rv3d.view_rotation = rot
    rv3d.view_distance = d.length
    rv3d.view_location = target
    rv3d.update()


def paint_at(win, area, region, eye, target, pressure=1.0, prev=None):
    rv3d = area.spaces.active.region_3d
    saved = (rv3d.view_location.copy(), rv3d.view_rotation.copy(), rv3d.view_distance, rv3d.view_perspective)
    look_from(rv3d, eye, target)
    mouse = (region.width / 2, region.height / 2)
    elem = {"name": "", "location": tuple(target), "mouse": mouse, "mouse_event": mouse, "pressure": pressure,
            "size": 30.0, "x_tilt": 0.0, "y_tilt": 0.0, "time": 0.0, "is_start": True}
    t0 = time.perf_counter()
    with bpy.context.temp_override(window=win, area=area, region=region):
        ok = bpy.ops.paint.image_paint.poll()
        if not ok:
            log("image_paint poll failed; tool:", bpy.context.workspace.tools.from_space_view3d_mode(
                bpy.context.mode, create=False).idname)
            bpy.ops.wm.tool_set_by_id(name="builtin.brush", space_type='VIEW_3D')
            ok = bpy.ops.paint.image_paint.poll()
            log("after brush tool:", ok)
        if ok:
            bpy.ops.paint.image_paint(stroke=[elem], mode='NORMAL')
    ms = (time.perf_counter() - t0) * 1000
    rv3d.view_location, rv3d.view_rotation, rv3d.view_distance, rv3d.view_perspective = saved
    rv3d.update()
    return ms


def setup():
    try:
        win, area, region = view3d()
        obj, img = setup_object()
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.object.mode_set(mode='TEXTURE_PAINT')
        ts = bpy.context.tool_settings
        ip = ts.image_paint
        log("mode:", obj.mode, "image_paint.mode:", ip.mode, "canvas:", ip.canvas, "brush:",
            ip.brush.name if ip.brush else None)
        ups = ip.unified_paint_settings if hasattr(ip, "unified_paint_settings") else ts.unified_paint_settings
        log("unified settings owner:", type(ups).__name__, "use_unified_color:", getattr(ups, "use_unified_color", "?"))
        for o in (ups, ip.brush):
            if o is not None and hasattr(o, "color"):
                o.color = (1.0, 0.0, 0.0)
        # A front point on the ball, seen from a "controller" 30 cm away at an angle.
        center = obj.matrix_world.translation
        target = center + Vector((0.0, -0.25, 0.1)).normalized() * 0.25
        eye = target + Vector((0.15, -0.25, 0.05))
        expected = uv_at(obj, eye, (target - eye).normalized())
        log("target", tuple(round(c, 3) for c in target), "expected uv", expected)

        results = []
        for lock, size in (('SCENE', 0.04), ('SCENE', 0.08), ('VIEW', None)):
            img.pixels.foreach_set(np.ones(512 * 512 * 4, dtype=np.float32))
            for o in (ups, ip.brush):
                if o is None:
                    continue
                if hasattr(o, "use_locked_size"):
                    o.use_locked_size = lock
                if size and hasattr(o, "unprojected_size"):
                    o.unprojected_size = size
            ms = paint_at(win, area, region, eye, target)
            n, c = red_stats(img)
            results.append((lock, size, n, c, ms))
            log(f"lock={lock} size={size}: {n} red px, centroid {c}, {ms:.1f} ms")
        # Location sensitivity: same view, different "location" far away.
        img.pixels.foreach_set(np.ones(512 * 512 * 4, dtype=np.float32))
        for o in (ups, ip.brush):
            if o is not None and hasattr(o, "use_locked_size"):
                o.use_locked_size = 'SCENE'
        ms = paint_at(win, area, region, eye, target)
        n1, c1 = red_stats(img)
        # Timing of 20 dabs along a line.
        t0 = time.perf_counter()
        for i in range(20):
            p = target + Vector((0.004 * i, 0.0, 0.0))
            paint_at(win, area, region, p + Vector((0.15, -0.25, 0.05)), p)
        log(f"20 dabs: {(time.perf_counter() - t0) * 1000 / 20:.1f} ms each; red px now {red_stats(img)[0]}")
        log("image dirty:", img.is_dirty)
        img.filepath_raw = os.path.join(ROOT, "tests", "probe_texpaint.png")
        img.file_format = 'PNG'
        img.save()
        log("saved", img.filepath_raw)
    except Exception:
        log(traceback.format_exc())
    done()


def done():
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    bpy.ops.wm.quit_blender()


bpy.app.timers.register(setup, first_interval=1.5)
