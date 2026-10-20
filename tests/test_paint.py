"""Background tests for Paint mode: colors, surface queries, UV sampling, stroke undo and the panel.
Painting itself needs a 3D view (see paint_preview.py).

blender --background --factory-startup --python tests/test_paint.py
"""

import math
import os
import sys
import traceback

import bpy
import numpy as np
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import vr_sculpt  # noqa: E402
from vr_sculpt import actions, menus, operators, paint, vr_ui  # noqa: E402

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def close(a, b, tol=1e-4):
    return all(abs(x - y) < tol for x, y in zip(a, b))


def make_paintable():
    """A UV sphere with a 64x64 image in a paint slot: left half red, right half blue."""
    bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16, radius=0.5, location=(1.0, 0.0, 0.0))
    obj = bpy.context.active_object
    img = bpy.data.images.new("PaintTest", 64, 64, alpha=True)
    px = np.zeros((64, 64, 4), dtype=np.float32)
    px[:, :32] = (1.0, 0.0, 0.0, 1.0)
    px[:, 32:] = (0.0, 0.0, 1.0, 1.0)
    img.pixels.foreach_set(px.ravel())
    mat = bpy.data.materials.new("PaintTestMat")
    mat.use_nodes = True
    tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
    tex.image = img
    mat.node_tree.links.new(tex.outputs["Color"], mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"])
    obj.data.materials.append(mat)
    return obj, img


def test_colors():
    print("== colors")
    ctx = bpy.context
    ip = ctx.tool_settings.image_paint
    check(paint.color_owner(ctx) is not None, "there is a place for paint colors")
    paint.set_color(ctx, (1.0, 0.5, 0.0))
    paint.set_color(ctx, (0.0, 0.0, 1.0), secondary=True)
    check(close(paint.get_color(ctx), (1.0, 0.5, 0.0)) and close(paint.get_color(ctx, True), (0, 0, 1)),
          "primary and secondary colors")
    paint.swap_colors(ctx)
    check(close(paint.get_color(ctx), (0, 0, 1)) and close(paint.get_color(ctx, True), (1.0, 0.5, 0.0)), "swap")
    paint.set_color(ctx, (1.0, 0.0, 0.0))
    paint.set_hsv(ctx, h=1 / 3)
    check(close(paint.get_color(ctx), (0.0, 1.0, 0.0), 1e-3), f"hue 120 degrees is green ({paint.get_color(ctx)})")
    paint.set_hsv(ctx, v=0.5)
    check(close(paint.get_color(ctx), (0.0, 0.5, 0.0), 1e-3), "value halves the brightness")
    paint.set_hsv(ctx, s=0.0)
    check(close(paint.get_color(ctx), (0.5, 0.5, 0.5), 1e-3), "no saturation is gray")


def test_surface():
    print("== surface queries and sampling")
    ctx = bpy.context
    obj, img = make_paintable()
    check(paint.ready(obj) and paint.paint_image(obj) == img, "UVs plus a paint slot image = ready")
    h = paint.surface_near(obj, Vector((1.0, -0.52, 0.0)), 0.03)
    check(h is not None and abs(h[0].y + 0.5) < 0.02 and h[1].y < -0.95, f"contact finds the surface below the tip")
    check(paint.surface_near(obj, Vector((1.0, -0.6, 0.0)), 0.03) is None, "out of reach: nothing")
    h2 = paint.surface_along(obj, Vector((1.0, -3.0, 0.0)), Vector((0, 1, 0)))
    check(h2 is not None and abs(h2[0].y + 0.5) < 0.02, "spray ray hits the front")
    uv = paint.uv_at(obj, h2[0], h2[2])
    check(uv is not None and 0.0 <= uv.x <= 1.0 and 0.0 <= uv.y <= 1.0, f"UV at the hit ({tuple(uv)})")
    check(abs(uv.y - 0.5) < 0.05, "on the equator, v is about one half")
    rgb = paint.sample_color(obj, h2[0], h2[2])
    expect = (1.0, 0.0, 0.0) if uv.x < 0.5 else (0.0, 0.0, 1.0)
    check(rgb is not None and close(rgb, expect, 1e-3), f"sampled color matches the texture ({rgb})")
    # The far side has the other color (u differs by a half).
    h3 = paint.surface_along(obj, Vector((1.0, 3.0, 0.0)), Vector((0, -1, 0)))
    rgb3 = paint.sample_color(obj, h3[0], h3[2])
    check(rgb3 is not None and not close(rgb3, rgb, 1e-3), "the other side samples the other color")

    # Pick tool sets the color and goes back to painting.
    settings = ctx.scene.vr_sculpt
    settings.paint_reach, settings.paint_tool = 'SPRAY', 'PICK'
    aim = Matrix.LocRotScale(Vector((1.0, -3.0, 0.0)), Vector((0, 1, 0)).to_track_quat('-Z', 'Y'), None)
    paint.PickColor(ctx, settings, obj, aim.translation, aim, 1.0)
    check(close(paint.get_color(ctx), rgb, 1e-3) and settings.paint_tool == 'PAINT', "Pick takes the color")
    paint.PickColor(ctx, settings, obj, aim.translation, Matrix.LocRotScale(
        Vector((1.0, 3.0, 0.0)), Vector((0, -1, 0)).to_track_quat('-Z', 'Y'), None), 1.0, secondary=True)
    check(close(paint.get_color(ctx, True), rgb3, 1e-3), "left hand picks the second color")
    settings.paint_reach = 'CONTACT'
    return obj, img


def test_undo(obj, img):
    print("== stroke undo")
    ctx = bpy.context
    paint.forget_history()
    before = np.empty(64 * 64 * 4, dtype=np.float32)
    img.pixels.foreach_get(before)
    paint._snapshot(img)
    img.pixels.foreach_set(np.ones(64 * 64 * 4, dtype=np.float32))
    check(paint.undo_last(ctx), "undo restores a stroke")
    after = np.empty(64 * 64 * 4, dtype=np.float32)
    img.pixels.foreach_get(after)
    check(np.allclose(before, after), "pixels are back")
    check(not paint.undo_last(ctx), "nothing more to undo")
    # The memory cap drops the oldest snapshots (but always keeps the last one).
    old = paint.HISTORY_BYTES
    paint.HISTORY_BYTES = before.nbytes * 2
    for _ in range(5):
        paint._snapshot(img)
    check(len(paint._history) == 2, f"history is capped by memory ({len(paint._history)} kept)")
    paint.HISTORY_BYTES = old
    paint.forget_history()
    # A stroke that painted nothing drops its snapshot.
    s = paint.PaintStroke(ctx, ctx.scene.vr_sculpt, obj, False, 1.0)
    s.snapshotted = True
    paint._snapshot(img)
    s.finish(ctx)
    check(not paint._history, "an empty stroke leaves no undo entry")


def test_panel(obj):
    print("== panel and buttons")
    ctx = bpy.context
    settings = ctx.scene.vr_sculpt
    for o in ctx.view_layer.objects:
        o.select_set(False)
    ctx.view_layer.objects.active = obj
    settings.mode = 'PAINT'
    widgets, height = vr_ui.layout(menus.rows(ctx))
    ids = {w.id for w in widgets}
    need = {"mode_paint", "reach_contact", "reach_spray", "paint_pick", "paint_hue", "paint_sat", "paint_val",
            "paint_radius", "paint_undo", "paint_swap"}
    check(need <= ids, f"ready mesh: paint page (missing {sorted(need - ids)})")
    check(height < 0.42, f"panel height {height:.3f} m")
    check(sum(1 for w in widgets if w.id.startswith("pcolor_")) == 8, "8 color swatches")
    vr_ui.find(widgets, "pcolor_YELLOW").on_press()
    check(close(paint.get_color(ctx), next(rgb for i, _n, rgb in vr_sculpt.strokes.PALETTE if i == 'YELLOW')),
          "a swatch sets the color")
    vr_ui.find(widgets, "reach_spray").on_press()
    check(settings.paint_reach == 'SPRAY', "Spray button")
    settings.paint_reach = 'CONTACT'
    vr_ui.find(widgets, "paint_pick").on_press()
    check(settings.paint_tool == 'PICK', "Pick color arms the eyedropper")
    settings.paint_tool = 'PAINT'

    bare = bpy.data.objects.new("Bare", bpy.data.meshes.new("Bare"))
    ctx.scene.collection.objects.link(bare)
    ctx.view_layer.objects.active = bare
    widgets, _ = vr_ui.layout(menus.rows(ctx))
    check(vr_ui.find(widgets, "paint_setup") is not None and vr_ui.find(widgets, "paint_size_2048") is not None,
          "a mesh without UVs/image offers Set up for painting")
    check(paint.pick(ctx, settings, False, Matrix(), Matrix(), 1.0) is None, "and the trigger does nothing on it")
    bpy.data.objects.remove(bare)
    ctx.view_layer.objects.active = obj

    # Right stick click cycles the paint radius in Paint mode; Y goes on to Tools.
    op = type("H", (), {"execute": operators.VRSCULPT_OT_cycle_radius.execute, "left_hand": False})()
    settings.paint_radius, settings.radius = 0.02, 0.04
    op.execute(ctx)
    check(abs(settings.paint_radius - 0.04) < 1e-6 and abs(settings.radius - 0.04) < 1e-6,
          "stick click: next paint radius, sculpt radius untouched")
    ob = type("H", (), {"execute": operators.VRSCULPT_OT_cycle_brush.execute, "left_hand": True,
                        "report": lambda *a: None})()
    ob.execute(ctx)
    check(settings.mode == 'TOOLS', "Y after Paint goes to Tools")
    settings.mode = 'DRAW'
    ob.execute(ctx)
    check(settings.mode == 'PAINT', "and Paint comes after Draw")

    # The tip touching the mesh keeps the trigger from a desktop screen behind it.
    settings.paint_reach = 'CONTACT'
    check(operators._tip_engaged(ctx, settings, Vector((1.0, -0.51, 0.0)), 1.0), "contact tip is engaged")
    check(not operators._tip_engaged(ctx, settings, Vector((1.0, -0.9, 0.0)), 1.0), "away from it, it isn't")


def main():
    vr_sculpt.register()
    try:
        test_colors()
        obj, img = test_surface()
        test_undo(obj, img)
        test_panel(obj)
    except Exception:
        traceback.print_exc()
        failures.append("raised")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")


main()
