"""Background tests for reference images (Tools > Refs): the folder listing and thumbnails, placing a picture
with the trigger, touching and grabbing one, Face me, opacity, pinning, deleting, and the panel pages.

blender --background --factory-startup --python tests/test_refs.py
"""

import math
import os
import shutil
import sys
import tempfile
import traceback

import bpy
import numpy as np
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import vr_sculpt  # noqa: E402
from vr_sculpt import controllers, hud, interactions, lookdev, menus, operators, refs, vr_ui  # noqa: E402
import test_model as tm  # noqa: E402

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def widgets():
    return {w.id: w for w in menus.layout(bpy.context)[0]}


def press(wid):
    w = widgets().get(wid)
    if w is None:
        check(False, f"widget {wid} is on the panel")
        return
    w.on_press()


def make_png(folder, name, w, h, rgba):
    img = bpy.data.images.new(name, w, h, alpha=True)
    img.pixels.foreach_set(np.tile(np.array(rgba, np.float32), w * h))
    path = os.path.join(folder, name + ".png")
    img.filepath_raw = path
    img.file_format = 'PNG'
    img.save()
    bpy.data.images.remove(img)
    return path


def test_listing(folder):
    print("== the references folder")
    s = bpy.context.scene.vr_sculpt
    wide = make_png(folder, "wide", 64, 32, (1.0, 0.0, 0.0, 1.0))
    tall = make_png(folder, "tall", 16, 48, (0.0, 0.0, 1.0, 1.0))
    open(os.path.join(folder, "notes.txt"), "w").close()
    s.refs_folder = folder
    paths = refs.choices(bpy.context)
    check([os.path.basename(p) for p in paths] == ["tall.png", "wide.png"], f"pictures only, sorted ({paths})")
    s.refs_folder = ""
    check(refs.folder(bpy.context) == "", "no folder set and an unsaved file: nothing to list")
    s.refs_folder = folder

    other = tempfile.mkdtemp(prefix="vrs_refs_other_")
    texture = bpy.data.images.load(make_png(other, "texture", 8, 8, (0.5, 0.5, 0.5, 1.0)))
    check(len(refs.choices(bpy.context)) == 2, "an image in the file that isn't a reference isn't listed")
    elsewhere = refs.new_ref(bpy.context, make_png(other, "elsewhere", 8, 8, (0, 1, 0, 1)), Matrix(), 0.2)
    names = [os.path.basename(p) for p in refs.choices(bpy.context)]
    check(names == ["tall.png", "wide.png", "elsewhere.png"], f"a reference's picture from elsewhere is ({names})")
    bpy.data.objects.remove(elsewhere)
    bpy.data.images.remove(texture)

    px = refs.thumbnail(wide)
    check(px is not None and px.shape == (64, 64, 4), "a 64 px RGBA thumbnail")
    check(px[32, 32, 0] > 0.9 and px[32, 32, 2] < 0.1 and px[2, 32, 3] == 0.0 and px[32, 2, 3] > 0.9,
          "a wide picture is fitted across, clear above and below")
    px = refs.thumbnail(tall)
    check(px[32, 2, 3] == 0.0 and px[2, 32, 3] > 0.9, "a tall one is fitted top to bottom")
    check(lookdev.preview_pixels('REF', wide) is not None, "the previews cache makes them (kind REF)")
    check(refs.thumbnail(os.path.join(folder, "missing.png")) is None, "a missing file has no thumbnail")
    return wide, tall


def setup():
    operators.session_running = lambda context: True
    ss = tm.FakeSessionState()
    ctx = tm.FakeContext(tm.FakeWM(ss))
    s = bpy.context.scene.vr_sculpt
    s.mode, s.tool, s.tip_offset, s.show_panel = 'TOOLS', 'REFS', 0.0, False
    s.smooth_strokes = False
    s.snap_angle = s.grid_snap = False  # Grabs follow these, as in Model mode.
    return ss, ctx, s


def test_place_and_grab(wide):
    print("== placing and grabbing")
    ss, ctx, s = setup()
    s.ref_image, s.ref_size = "", 0.5
    tip = Matrix.Translation((0.5, 0.0, 1.5))
    h = refs.pick(ctx, s, False, tip, tip, 1.0)
    check(isinstance(h, interactions.Deselect), "no picture chosen: a press in empty space selects nothing")
    s.ref_image = wide
    h = refs.pick(ctx, s, True, tip, tip, 1.0)
    check(isinstance(h, interactions.Deselect), "the other hand in empty space doesn't place")
    h = refs.pick(ctx, s, False, tip, tip, 1.0)
    check(isinstance(h, refs.PlaceRef), "the main hand in empty space places the chosen picture")
    obj = h.obj
    check(refs.is_ref(obj) and obj.data.filepath and os.path.samefile(bpy.path.abspath(obj.data.filepath), wide),
          "it's an image empty of that file")
    check(abs(obj.empty_display_size - 0.5) < 1e-6, "as big as New size")
    to_viewer = Vector(ss.viewer_pose_location) - obj.matrix_world.translation
    to_viewer.z = 0.0
    normal = obj.matrix_world.to_3x3() @ Vector((0, 0, 1))
    up = obj.matrix_world.to_3x3() @ Vector((0, 1, 0))
    check(normal.angle(to_viewer) < 1e-3 and abs(up.z - 1.0) < 1e-6, "upright and facing the headset")
    h.update(ctx, None, Matrix.Translation((0.4, 0.2, 1.4)), 1.0)
    h.finish(ctx)
    check((obj.matrix_world.translation - Vector((0.4, 0.2, 1.4))).length < 1e-6, "it follows the tip while held")
    check(refs.selected_ref(bpy.context) == obj, "and ends up selected")

    corners = refs.corners(obj)
    width = (corners[1] - corners[0]).length
    height = (corners[3] - corners[0]).length
    check(abs(width - 0.5) < 1e-5 and abs(height - 0.25) < 1e-5, f"a 2:1 picture is 50 x 25 cm ({width:.3f} x {height:.3f})")
    center = obj.matrix_world.translation.copy()
    normal = obj.matrix_world.to_3x3() @ Vector((0, 0, 1))  # Where it faces now, after following the tip.
    check(refs.ref_at(bpy.context, center + normal * 0.01, 1.0) == obj, "a tip just in front touches it")
    check(refs.ref_at(bpy.context, center + normal * 0.05, 1.0) is None, "5 cm away doesn't")
    side = obj.matrix_world.to_3x3() @ Vector((1, 0, 0))
    check(refs.ref_at(bpy.context, center + side * 0.3, 1.0) is None, "past its edge doesn't")

    tip = Matrix.Translation(center + normal * 0.005)
    h = refs.pick(ctx, s, False, tip, tip, 1.0)
    check(isinstance(h, interactions.MoveObject) and h.snap is False, "a press on a picture grabs it (no surface snap)")
    move = Vector((0.0, 0.0, 0.2))
    turn = Matrix.Rotation(math.radians(30), 4, 'Z')
    h.update(ctx, None, Matrix.Translation(tip.translation + move) @ turn, 1.0)
    h.finish(ctx)
    check((obj.matrix_world.translation - (center + move)).length < 0.01, "it moves with the hand")
    turned = math.degrees((obj.matrix_world.to_3x3() @ Vector((0, 0, 1))).angle(normal))
    check(abs(turned - 30.0) < 0.5, f"and turns with it ({turned:.1f} degrees)")
    refs.face_me(ctx, obj)
    n2 = obj.matrix_world.to_3x3() @ Vector((0, 0, 1))
    v = Vector(ss.viewer_pose_location) - obj.matrix_world.translation
    v.z = 0
    check(n2.angle(v) < 1e-3, "Face me turns it back to the headset")
    return obj


def test_settings(obj):
    print("== opacity, pin, delete")
    s = bpy.context.scene.vr_sculpt
    refs.set_opacity(obj, 0.4)
    check(obj.use_empty_image_alpha and abs(obj.color[3] - 0.4) < 1e-6 and abs(refs.opacity(obj) - 0.4) < 1e-6,
          "Opacity uses the empty's own alpha (the desktop shows it too)")
    refs.set_opacity(obj, 1.0)
    check(not obj.use_empty_image_alpha, "full opacity turns it off again")
    refs.toggle_pin(bpy.context, obj)
    check(s.ref_pin == obj.name and refs.pinned(bpy.context) == obj, "Pin shows it beside the panel")
    refs.toggle_pin(bpy.context, obj)
    check(s.ref_pin == "", "pressing Pin again unpins")
    refs.toggle_pin(bpy.context, obj)
    check(refs.status(bpy.context) == "50 cm", f"the tip status shows its size ({refs.status(bpy.context)})")
    refs.forget_caches()
    refs.delete_ref(bpy.context, obj)
    check(s.ref_pin == "" and not [o for o in bpy.data.objects if refs.is_ref(o)], "Delete removes it and its pin")


def test_panel(wide, tall):
    print("== panel")
    s = bpy.context.scene.vr_sculpt
    s.mode, s.tool, s.ref_image = 'TOOLS', 'MEASURE', ""
    menus.reset()
    press("tool_refs")
    ids = set(widgets())
    check(s.tool == 'REFS' and {"ref_image", "ref_size", "refs_show"} <= ids and "ref_delete" not in ids,
          "Refs: the picture, New size and Show; nothing selected, no picture settings")
    press("ref_image")
    tiles = [w for w in widgets().values() if w.kind == 'tile']
    check(len(tiles) == 2 and [t.text for t in tiles] == ["tall", "wide"], "the tiles page lists the folder")
    check(all(t.image is not None for t in tiles), "with thumbnails")
    next(t for t in tiles if t.text == "wide").on_press()
    check(s.ref_image == wide and "ref_image" in widgets(), "choosing one makes it the picture, back on the page")
    obj = refs.new_ref(bpy.context, tall, Matrix.Translation((0, 0, 1)), 0.3)
    ids = set(widgets())
    check({"ref_obj_size", "ref_opacity", "ref_face", "ref_pin", "ref_delete"} <= ids,
          "a selected picture: its size, opacity, Face me, Pin, Delete")
    w = widgets()["ref_obj_size"]
    w.on_change(0.8)
    check(abs(obj.empty_display_size - 0.8) < 1e-6, "Size sets the picture's size")
    press("ref_pin")
    check(s.ref_pin == obj.name and "ref_unpin" in widgets(), "Pin, and an Unpin button while pinned")
    press("ref_delete")
    check(not refs.is_ref(bpy.data.objects.get("tall")), "Delete")
    for img in list(bpy.data.images):
        bpy.data.images.remove(img)  # The placed pictures' images would be listed too.
    s.refs_folder = os.path.join(tempfile.gettempdir(), "vrs_no_such_folder")
    press("ref_image")
    labels = [w.text for w in widgets().values() if w.kind == 'label']
    check(any("No pictures" in t for t in labels), "an empty folder says so")
    press("refs_back")
    s.tool = 'REFS'
    card = hud.card_content(s, None)
    check(card[0] == "Reference" and card[3] == "image", "the tip card names the tool")
    menus.reset()


def main():
    vr_sculpt.register()
    folder = tempfile.mkdtemp(prefix="vrs_refs_")
    try:
        wide, tall = test_listing(folder)
        obj = test_place_and_grab(wide)
        test_settings(obj)
        test_panel(wide, tall)
    except Exception:
        traceback.print_exc()
        failures.append("exception")
    finally:
        vr_sculpt.unregister()
        shutil.rmtree(folder, ignore_errors=True)
    print("RESULT:", "FAILED " + str(failures) if failures else "ALL PASSED")


main()
