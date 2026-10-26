"""Background tests for kit parts (kit.py): listing asset libraries and catalogs, previews read
without appending, dropping parts onto surfaces (plain, as cutters, collections), stamping, Add to
Kit, and the Kit sheet on the hand panel.

blender --background --factory-startup --python tests/test_kit.py
"""

import math
import os
import shutil
import sys
import tempfile
import time
import traceback

import bpy
from mathutils import Matrix, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import vr_sculpt  # noqa: E402
from vr_sculpt import controllers, hardsurface, hud, kit, menus, operators, primitives, vr_ui  # noqa: E402
import test_model as tm  # noqa: E402  (fakes and the press() helper)

failures = []
GPU = [False]


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def close(a, b, tol=1e-4):
    return all(abs(x - y) < tol for x, y in zip(a, b))


def clear():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    for c in list(bpy.data.collections):
        bpy.data.collections.remove(c)


def cube(size, loc, name="Cube"):
    obj = primitives.create(bpy.context, 'CUBE', Vector(loc))
    obj.name = name
    obj.vr_prim.size = (size,) * 3
    bpy.context.view_layer.update()
    return obj


def setup():
    operators.session_running = lambda context: True
    ss = tm.FakeSessionState()
    ctx = tm.FakeContext(tm.FakeWM(ss))
    settings = bpy.context.scene.vr_sculpt
    settings.mode = 'MODEL'
    settings.tip_offset = 0.0
    settings.show_panel = False  # Presses go to the scene, not the panel.
    settings.model_add_as = 'OBJECT'
    settings.snap_surface = settings.snap_angle = settings.grid_snap = False
    settings.kit_stamp = False
    settings.kit_scale = 1.0
    settings.kit_spacing = 0.03
    return ss, ctx, settings


def starter(name):
    return next(a for a in kit.library(kit.STARTER).assets if a.name == name)


def volume(obj):
    import bmesh
    bpy.context.view_layer.update()
    ev = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    bm = bmesh.new()
    bm.from_mesh(ev.to_mesh())
    v = abs(bm.calc_volume())
    bm.free()
    ev.to_mesh_clear()
    return v


def make_library(folder):
    """A user asset library: one .blend with a mesh asset in a catalog, and one without a preview."""
    os.makedirs(os.path.join(folder, "sub"))
    with open(os.path.join(folder, kit.CATS_FILE), "w") as f:
        f.write(kit.CATS_HEADER + "11111111-2222-4333-8444-555555555555:Hull/Plates:Hull-Plates\n")
    mesh = bpy.data.meshes.new("Plate")
    import bmesh
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=0.1)
    bm.to_mesh(mesh)
    bm.free()
    plate = bpy.data.objects.new("Plate", mesh)
    plate.asset_mark()
    plate.asset_data.catalog_id = "11111111-2222-4333-8444-555555555555"
    prev = plate.preview_ensure()
    prev.image_size = (8, 8)
    prev.image_pixels_float = [0.0, 1.0, 0.0, 1.0] * 64
    bare = bpy.data.objects.new("Bare", mesh)
    bare.asset_mark()
    lamp = bpy.data.objects.new("Lamp", bpy.data.lights.new("Lamp", 'POINT'))
    lamp.asset_mark()  # Not a mesh: not a part.
    bpy.data.libraries.write(os.path.join(folder, "sub", "plates.blend"), {plate, bare, lamp}, fake_user=True)
    with open(os.path.join(folder, "sub", "broken.blend"), "wb") as f:
        f.write(b"not a blend file")
    for o in (plate, bare, lamp):
        bpy.data.objects.remove(o)


# --- tests ---------------------------------------------------------------------

def test_listing(lib_dir):
    print("== listing libraries")
    clear()
    objects_before, libraries_before = len(bpy.data.objects), len(bpy.data.libraries)
    t0 = time.perf_counter()
    kit.scan()
    check(kit.pending() and time.perf_counter() - t0 < 0.5,
          f"scanning doesn't wait: a background Blender lists the files ({time.perf_counter() - t0:.2f} s)")
    check(kit.library(kit.STARTER).assets == [], "until it's done, the parts aren't there yet")
    libs = kit.scan(wait=True)
    check(not kit.pending(), "the worker is done")
    names = [lib.name for lib in libs]
    check(names[:2] == [kit.STARTER, kit.MINE], f"starter kit and My Kit come first ({names})")
    check(os.path.basename(lib_dir) in names, "an asset library from the Preferences is listed")
    check(len(bpy.data.objects) == objects_before and len(bpy.data.libraries) == libraries_before,
          "listing adds nothing to the open file")
    start = kit.library(kit.STARTER)
    check(start.catalogs() == ["Bolts", "Greebles", "Panels", "Pipes", "Vents"],
          f"the starter kit's catalogs ({start.catalogs()})")
    vents = start.in_catalog("Vents")
    check(len(vents) == 4 and all(a.cutter for a in vents), "its vents are cutters")
    check(not starter("Hex Bolt").cutter, "a bolt isn't")
    check(starter("Pipe Run").id_type == 'COLLECTION', "collection assets are listed too")

    user = kit.library(os.path.basename(lib_dir))
    found = {a.name: a for a in user.assets}
    check(set(found) == {"Plate", "Bare"}, f"mesh assets in subfolders are found, a light isn't ({sorted(found)})")
    check(found["Plate"].catalog == "Hull/Plates" and found["Bare"].catalog == "",
          "parts get their catalog paths (or none)")
    check(user.catalogs() == ["Hull/Plates", ""] and len(user.in_catalog("Hull")) == 1,
          "a catalog holds its sub-catalogs' parts")
    check(kit.catalog_label("Hull/Plates") == "Hull / Plates" and kit.catalog_label("") == kit.UNASSIGNED,
          "catalog labels")

    # Cached on disk by file size and time: a new session lists from the cache with no worker; a changed
    # file goes to the worker again; an unreadable one is listed empty, once.
    check(os.path.isfile(os.path.join(lib_dir, "sub", "broken.blend")) and not kit.pending(),
          "an unreadable file doesn't stop the rest")
    path = found["Plate"].blend
    kit._listings.clear()
    kit._state["libraries"] = None
    kit.scan()
    check(not kit.pending() and len(kit.library(user.name).assets) == 2 and len(kit.library(kit.STARTER).assets) == 17,
          "a new session lists everything from the cache, no worker")
    os.utime(path, (os.path.getatime(path), os.path.getmtime(path) + 5))
    kit.scan()
    check(kit.pending(), "a changed file goes to the worker again")
    while kit.pending():
        time.sleep(0.05)
        kit.tick()
    check(len(kit.library(user.name).assets) == 2, "tick() takes it in when the worker is done")

    key = starter("Hex Bolt").key
    check(kit.is_key(key) and kit.find(key).name == "Hex Bolt", "parts are found by key")
    kit._state["libraries"] = None
    asset = kit.find(key)
    check(asset is not None and asset.name == "Hex Bolt" and asset.library == "", "even before any scan")
    kit.scan()
    check(kit.find("kit:C:/nowhere.blend|OBJECT|X") is None, "a key whose file is gone finds nothing")
    check(primitives.label(key) == "Hex Bolt", "the Add label shows the part's name")
    check(kit.siblings(key) == [a.key for a in kit.library(kit.STARTER).in_catalog("Bolts")],
          "siblings are the parts of the same catalog")


def test_previews(lib_dir):
    print("== previews, from the cache")
    px = kit.preview_pixels(starter("Hex Bolt"))
    check(px is not None and px[0] == kit.THUMB and px[1] == kit.THUMB, "a starter part's preview is read")
    w, h, data = px
    alpha = data.reshape(h, w, 4)[..., 3]
    check(alpha[h // 2, w // 2] > 0.9 and alpha[0, 0] < 0.01, "it's the part on a transparent background")
    user = {a.name: a for a in kit.library(os.path.basename(lib_dir)).assets}
    w, h, data = kit.preview_pixels(user["Plate"])
    check((w, h) == (8, 8) and close(data[:4], (0, 1, 0, 1), 0.01), "a library asset's own preview pixels come back")
    check(kit.preview_pixels(user["Bare"]) is None, "an asset without a preview has none")
    check(len(bpy.data.objects) == 0 and len(bpy.data.libraries) == 0, "reading previews appends nothing")
    if GPU[0]:
        t = kit.thumb(user["Plate"])
        tex = None
        for _ in range(10):  # A few new textures a frame.
            kit._clock[0] = 0.0
            tex = t.texture()
            if tex is not None:
                break
        check(tex is not None and tex.width == 8, "the thumbnail becomes a GPU texture")
        check(kit.thumb(user["Bare"]) is None, "one without a preview: the panel shows an icon")
    else:
        print("  skip GPU textures (no GPU)")


def test_drop():
    print("== dropping parts")
    clear()
    ss, ctx, settings = setup()
    base = cube(0.2, (0, 0, 0), "Base")  # Top at z = 0.1.
    settings.primitive = starter("Hex Bolt").key
    primitives.make_active(bpy.context, None)
    # Pressed in empty space, the part appears at the tip and is carried; near the top it sits on it.
    op, r = tm.press(ctx, ss, 1, [(0.5, 0, 0.5), (0.3, 0, 0.3), (0.02, 0.01, 0.11)])
    bolt = bpy.context.view_layer.objects.active
    check(r == {'FINISHED'} and bolt is not None and bolt.name.startswith("Hex Bolt"),
          f"a press places the part ({r}, {bolt.name if bolt else None})")
    check(bolt.name in bpy.context.collection.objects and bolt.asset_data is None,
          "it's in the scene, no longer an asset")
    lo, hi = primitives.local_bounds(bolt)
    bottom = bolt.matrix_world @ Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z))
    check(close(bottom, (0.02, 0.01, 0.1), 1e-4), f"it stands on the surface ({tuple(round(c, 4) for c in bottom)})")
    # Away from surfaces it hangs from the tip.
    settings.kit_scale = 2.0
    op, r = tm.press(ctx, ss, 1, [(0.5, 0, 0.5), (0.6, 0, 0.6)])
    free = bpy.context.view_layer.objects.active
    lo, hi = primitives.local_bounds(free)
    check(close(free.matrix_world @ Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z)), (0.6, 0, 0.6), 1e-4),
          "in the air, its bottom center is at the tip")
    check(close(free.matrix_world.to_scale(), (2, 2, 2)), "Part Size scales it")
    check(free.data == bolt.data, "copies share the mesh (Append Reuse Data)")
    settings.kit_scale = 1.0
    # The right trigger places another even on a part; the left one grabs it.
    op, r = tm.press(ctx, ss, 1, [(0.6, 0, 0.6005), (0.7, 0, 0.7)])
    check(isinstance(op.handler, operators.interactions.PlaceAsset), "the right trigger on a part places one more")
    op, r = tm.press(ctx, ss, 0, [(0.7, 0, 0.7005), (0.8, 0, 0.8)])
    check(isinstance(op.handler, operators.interactions.MoveObject) and
          not isinstance(op.handler, operators.interactions.PlaceAsset), "the left trigger grabs it")
    check(volume(base) > 0.0079, "nothing was cut")


def test_cutter_parts():
    print("== cutter parts")
    clear()
    ss, ctx, settings = setup()
    base = cube(0.2, (0, 0, 0), "Base")
    v0 = volume(base)
    primitives.make_active(bpy.context, base)
    menus.choose_part(bpy.context, starter("Round Hole").key)
    check(settings.model_add_as == 'DIFFERENCE', "choosing a cutter part switches New As to Cut")
    op, r = tm.press(ctx, ss, 1, [(0.5, 0, 0.5), (0.3, 0, 0.3), (0.0, 0.0, 0.11)])
    hole = bpy.context.view_layer.objects.active
    check(hardsurface.is_cutter(hole) and hardsurface.target_of(hole) == base, "it's a cutter on the selection")
    check(hole.parent == base, "attached to its target once placed")
    lo, hi = primitives.local_bounds(hole)
    center = hole.matrix_world @ ((lo + hi) / 2)
    check(close(center, (0, 0, 0.1), 1e-4), f"its center sank to the surface ({tuple(round(c, 4) for c in center)})")
    check(volume(base) < v0 - 1e-5, "the hole is cut")
    menus.choose_part(bpy.context, starter("Hex Bolt").key)
    check(settings.model_add_as == 'OBJECT', "a plain part after a cutter part switches back to Object")
    settings.model_add_as = 'UNION'
    menus.choose_part(bpy.context, starter("Rivet").key)
    check(settings.model_add_as == 'UNION', "a choice made by hand stays")
    settings.model_add_as = 'DIFFERENCE'
    menus.choose_part(bpy.context, starter("Pipe Run").key)
    primitives.make_active(bpy.context, base)
    op, r = tm.press(ctx, ss, 1, [(0.5, 0, 0.5), (0.4, 0, 0.4)])
    check(not hardsurface.is_cutter(bpy.context.view_layer.objects.active), "a collection part never cuts")


def test_collection_part():
    print("== collection parts")
    clear()
    ss, ctx, settings = setup()
    cube(0.2, (0, 0, 0), "Base")
    settings.primitive = starter("Pipe Run").key
    primitives.make_active(bpy.context, None)
    op, r = tm.press(ctx, ss, 1, [(0.5, 0, 0.5), (0.3, 0, 0.3), (0.0, 0.0, 0.11)])
    inst = bpy.context.view_layer.objects.active
    check(primitives.is_instance(inst) and inst.instance_collection.name.startswith("Pipe Run"),
          "placed as a collection instance")
    check(inst.instance_collection.name not in bpy.context.scene.collection.children,
          "the collection itself isn't added to the scene")
    lo, hi = primitives.local_bounds(inst)
    check((hi - lo).x > 0.25, f"its bounds come from the collection's meshes ({tuple(round(c, 3) for c in hi - lo)})")
    bottom = inst.matrix_world @ Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z))
    check(close(bottom, (0, 0, 0.1), 1e-4), "it stands on the surface")
    check(primitives.touching(bpy.context, inst.matrix_world @ ((lo + hi) / 2), 0.01) == inst,
          "it can be touched (and grabbed)")
    op, r = tm.press(ctx, ss, 1, [(0.5, 0, 0.5), (0.6, 0, 0.6)])
    second = bpy.context.view_layer.objects.active
    check(second != inst and second.instance_collection == inst.instance_collection,
          "a second one instances the same collection")


def test_stamp():
    print("== stamping")
    clear()
    ss, ctx, settings = setup()
    base = cube(0.2, (0, 0, 0), "Base")
    settings.primitive = starter("Hex Bolt").key
    settings.kit_stamp = True
    primitives.make_active(bpy.context, base)
    sweep = [(-0.08 + 0.005 * i, 0.0, 0.11) for i in range(33)]  # x from -8 cm to 8 cm, 1 cm over the top
    op, r = tm.press(ctx, ss, 1, sweep)
    bolts = [o for o in bpy.data.objects if o.name.startswith("Hex Bolt")]
    check(r == {'FINISHED'} and len(bolts) == 6, f"a stamp every 3 cm along 16 cm ({len(bolts)})")
    check(len({b.data for b in bolts}) == 1, "they share one mesh")
    xs = sorted(round(b.matrix_world.translation.x, 4) for b in bolts)
    check(close(xs, [-0.08, -0.05, -0.02, 0.01, 0.04, 0.07], 1e-4), f"evenly spaced ({xs})")
    check(all(abs(b.matrix_world.translation.z - 0.1) < 1e-4 for b in bolts), "each stands on the surface")
    check(all((b.matrix_world.to_3x3() @ Vector((0, 0, 1))).angle(Vector((0, 0, 1))) < 1e-4 for b in bolts),
          "each turned to the normal")
    check(all(abs((b.matrix_world.to_3x3() @ Vector((1, 0, 0))).y) < 1e-4 for b in bolts),
          "and along the sweep")
    check(bpy.context.view_layer.objects.active == base, "the selection stays (stamp more, or switch to Cut)")
    # Pressing a stamped part stamps (doesn't grab).
    op, r = tm.press(ctx, ss, 1, [(0.07, 0.0, 0.103)])
    check(isinstance(op.handler, operators.interactions.StampAssets), "with Stamp on, the trigger always stamps")
    n = len([o for o in bpy.data.objects if o.name.startswith("Hex Bolt")])
    op, r = tm.press(ctx, ss, 1, [(0.5, 0, 0.5 + 0.01 * i) for i in range(10)])
    check(r == {'CANCELLED'} and len([o for o in bpy.data.objects if o.name.startswith("Hex Bolt")]) == n,
          "nothing is stamped in the air (and no undo step)")

    # Cutter stamps: one cutter, one Boolean.
    v0 = volume(base)
    booleans = len(hardsurface.booleans(base))
    menus.choose_part(bpy.context, starter("Round Hole").key)
    settings.kit_spacing = 0.04
    primitives.make_active(bpy.context, base)
    op, r = tm.press(ctx, ss, 1, [(-0.06 + 0.005 * i, 0.03, 0.105) for i in range(25)])
    cutters = hardsurface.cutters(base)
    check(len(hardsurface.booleans(base)) == booleans + 1 and len(cutters) == 1,
          "the stamps make one cutter with one Boolean")
    stamps = cutters[0]
    part_polys = 34
    check(len(stamps.data.polygons) == 4 * part_polys, f"holding 4 holes ({len(stamps.data.polygons)} faces)")
    check(stamps.parent == base and stamps.name.startswith("Round Hole Stamps"), "attached to its target")
    check(volume(base) < v0 - 3 * 1e-5, "they're cut")
    check(not any(m.name.startswith("Round Hole") and m.users == 0 for m in bpy.data.meshes),
          "the part's own mesh isn't left behind")
    settings.kit_stamp = False
    settings.kit_spacing = 0.03


def test_add_to_kit(mine):
    print("== add to kit")
    clear()
    ss, ctx, settings = setup()
    part = cube(0.1, (1, 0, 0), "Greeble")
    part.scale = (2, 1, 1)
    part.modifiers.new("Bevel", 'BEVEL').width = 0.01
    bpy.context.view_layer.update()
    asset = kit.add_to_kit(bpy.context, part)
    check(asset is not None and asset.library == kit.MINE and asset.catalog == "Parts",
          f"it's listed in My Kit, in Parts ({asset})")
    check(os.path.isfile(os.path.join(mine, "Greeble.blend")) and os.path.isfile(os.path.join(mine, kit.CATS_FILE)),
          "a .blend and the catalog file are written")
    check(len(bpy.data.objects) == 1 and "Greeble.001" not in bpy.data.meshes, "nothing is left in the open file")
    if GPU[0]:
        px = kit.preview_pixels(asset)
        check(px is not None and px[2].reshape(px[1], px[0], 4)[..., 3].max() > 0.9, "with a rendered preview")
    obj = kit.place(bpy.context, asset)
    lo, hi = primitives.local_bounds(obj)
    check(len(obj.data.vertices) > 8, "its modifiers were applied")
    check(close(hi - lo, (0.2, 0.1, 0.1), 1e-3) and close(obj.scale, (1, 1, 1)), "its scale was baked in")

    cutter = cube(0.05, (0, 0, 0), "Vent")
    target = cube(0.2, (0, 0, 0), "Hull")
    hardsurface.make_cutter(bpy.context, cutter, target)
    second = kit.add_to_kit(bpy.context, cutter)
    check(second.cutter and second.catalog == "Cutters", "a cutter is saved as a cutter part")
    third = kit.add_to_kit(bpy.context, part)
    check(third.blend.endswith("Greeble_2.blend"), "a name already taken gets a new file")
    try:
        kit.add_to_kit(bpy.context, None)
        check(False, "nothing selected is refused")
    except ValueError:
        check(True, "nothing selected is refused")


def test_panel():
    print("== the Kit sheet")
    clear()
    ss, ctx, settings = setup()
    menus.reset()
    menus._kit.update(library=None, catalog=None, page=0)
    settings.primitive = "mesh.primitive_cube_add"
    widgets = vr_ui.layout_panel(menus.panel(bpy.context))[0]
    kit_button = vr_ui.find(widgets, "kit")
    check(kit_button is not None, "the Model page has a Kit button next to Add")
    kit_button.on_press()
    check(menus.sheet() == 'KIT' and menus._kit["library"] == kit.STARTER, "it opens the Kit on the starter kit")
    rows = menus.rows(bpy.context)
    tiles = [w for row in rows for w in row if w.kind == 'tile']
    check(len(tiles) == menus.KIT_COLS * menus.KIT_ROWS, f"a page of thumbnails ({len(tiles)})")
    check(all(t.image is not None for t in tiles), "each with its preview")
    check(vr_ui.find([w for row in rows for w in row], "kit_next") is not None, "and a pager (17 parts)")
    widgets, height = vr_ui.layout_panel(menus.panel(bpy.context))
    check(height < 0.3, f"the sheet stays a sensible size ({height:.3f} m)")
    tile_ws = [w for w in widgets if w.kind == 'tile']
    check(all(abs((w.rect[3] - w.rect[1]) - vr_ui.TILE_H) < 1e-9 for w in tile_ws), "tiles are TILE_H tall")
    widths = {round(w.rect[2] - w.rect[0], 6) for w in tile_ws}
    check(len(widths) == 1, "and all the same width")
    tiles[0].on_press()
    check(settings.primitive == tiles[0].id[len("part_"):], "pressing a tile chooses that part")
    check(menus.sheet() == 'KIT', "and the sheet stays open")
    rows = menus.rows(bpy.context)
    check(next(w for row in rows for w in row if w.id == tiles[0].id).active, "the chosen part is highlighted")

    menus.open_catalog_picker(bpy.context)
    picker = menus._picker
    check(kit.STARTER in picker["categories"] and kit.MINE in picker["categories"],
          "the catalog picker lists the libraries")
    values = [o.value for o in picker["options"] if o.category == kit.STARTER]
    menus._pick(next(v for v in values if v.endswith("\nVents")))
    rows = menus.rows(bpy.context)
    tiles = [w for row in rows for w in row if w.kind == 'tile']
    check(len(tiles) == 4 and menus._kit["catalog"] == "Vents", "choosing a catalog shows its parts")
    check(all(w.icon == "cut" for w in tiles), "cutters fall back to the cut icon")
    stamp = vr_ui.find([w for row in rows for w in row], "kit_place_stamp")
    stamp.on_press()
    check(settings.kit_stamp, "Drop / Stamp switches the trigger")
    settings.primitive = starter("Grille").key
    check(hud.card_content(settings, None)[0] == "Stamp Grille", "the tip status says what the trigger does")
    info = controllers.Info(primitive="Grille", kit_part=True)
    check(controllers._trigger(settings, info)[0] == "stamp", "so does the trigger's hint")
    settings.kit_stamp = False

    # B cycles through the catalog's parts.
    keys = kit.siblings(settings.primitive)
    i = keys.index(settings.primitive)
    fake = type("Op", (), {"off_hand": False, "report": lambda *a: None})()
    operators.VRSCULPT_OT_cycle_brush.execute(fake, bpy.context)  # No XR session: not pointing at the desktop.
    check(settings.primitive == keys[(i + 1) % len(keys)], "B steps to the next part in the catalog")

    menus.set_mode(bpy.context, 'SCULPT')
    check(menus.sheet() is None, "leaving Model closes the Kit")
    settings.mode = 'MODEL'


def main():
    try:
        import gpu
        gpu.init()
        GPU[0] = True
    except Exception as ex:
        print("No GPU:", ex)
    lib_dir = tempfile.mkdtemp(prefix="vrs_lib_")
    mine = tempfile.mkdtemp(prefix="vrs_mine_")
    cache = tempfile.mkdtemp(prefix="vrs_cache_")
    kit._user_dir[0] = mine
    kit._cache_dir[0] = cache
    vr_sculpt.register()
    try:
        clear()
        make_library(lib_dir)
        bpy.ops.preferences.asset_library_add(directory=lib_dir)
        test_listing(lib_dir)
        test_previews(lib_dir)
        test_drop()
        test_cutter_parts()
        test_collection_part()
        test_stamp()
        test_add_to_kit(mine)
        test_panel()
    except Exception:
        traceback.print_exc()
        failures.append("exception")
    finally:
        vr_sculpt.unregister()
        kit._user_dir[0] = kit._cache_dir[0] = None
        shutil.rmtree(cache, ignore_errors=True)
        shutil.rmtree(lib_dir, ignore_errors=True)
        shutil.rmtree(mine, ignore_errors=True)
    print("RESULT:", "FAILED " + str(failures) if failures else "ALL PASSED")


if __name__ == "__main__":
    main()
