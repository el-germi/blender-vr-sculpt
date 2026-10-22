"""Background tests for every Add > Mesh primitive (with Extra Mesh Objects) and the picker.

blender --background --factory-startup --python tests/test_generic.py
(--factory-startup: enabling the extension here never touches saved preferences.)
"""

import os
import sys
import time
import traceback

import addon_utils
import bpy
from mathutils import Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import vr_sculpt  # noqa: E402
from vr_sculpt import catalog, generic, menus, primitives, vr_ui  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_model as tm  # noqa: E402  (fakes and the press() helper)

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def clear():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)


def world_bounds(obj):
    pts = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    lo = Vector([min(p[i] for p in pts) for i in range(3)])
    hi = Vector([max(p[i] for p in pts) for i in range(3)])
    return lo, hi


def test_catalog():
    print("== catalog")
    entries = catalog.entries(refresh=True)
    labels = {e.label for e in entries}
    check(len(entries) >= 40, f"found {len(entries)} Add > Mesh entries")
    check({"Plane", "UV Sphere", "Torus", "Monkey", "Quad Sphere"} <= labels, "built-in primitives found")
    gear = next((e for e in entries if e.idname == "mesh.primitive_gear"), None)
    check(gear is not None and gear.category == "Gears" and gear.preset == {"change": False},
          f"extension gear found with its submenu and preset ({gear})")
    check(not any(e.idname.startswith("object.") for e in entries), "non-mesh operators (Parent to Empty) skipped")
    check(len({e.key for e in entries}) == len(entries), "entry keys are unique")


def test_every_entry():
    print("== every entry: create, fit to 10 cm, regenerate")
    ctx = bpy.context
    rows = []
    for e in catalog.entries():
        clear()
        try:
            t0 = time.perf_counter()
            obj = primitives.create_from_entry(ctx, e.key, (0, 0, 0))
            t_create = (time.perf_counter() - t0) * 1000
            if obj.vr_prim.kind == 'OPERATOR':
                a, b = Vector((1.0, 2.0, 3.0)), Vector((1.1, 2.06, 3.03))
                t0 = time.perf_counter()
                generic.fit(ctx, obj, a, b, 0.005)
                t_fit = (time.perf_counter() - t0) * 1000
                ctx.view_layer.update()
                lo, hi = world_bounds(obj)
                ext = max((hi - lo)[:])
                center = (lo + hi) / 2
                degenerate = obj.vr_prim.op_extent < 1e-6  # single vertices etc.
                size_ok = degenerate or abs(ext - 0.1) < 0.002
                center_ok = (center - (a + b) / 2).length < 0.002
                # Change the first numeric setting and regenerate.
                specs = generic.params(e, obj)
                num = next((s for s in specs if s.type in ('INT', 'FLOAT')), None)
                regen_ok, t_regen, note = True, 0.0, ""
                if num is not None:
                    t0 = time.perf_counter()
                    new_value = num.hi if num.type == 'FLOAT' else max(num.lo, min(num.hi, num.value + 1))
                    accepted = generic.set_param(ctx, obj, num.id, new_value)
                    t_regen = (time.perf_counter() - t0) * 1000
                    # A value the operator rejects must leave the old mesh and setting in place.
                    value_ok = (generic.stored(obj).get(num.id) == new_value) if accepted \
                        else (num.id not in generic.stored(obj))
                    note = "" if accepted else " (value rejected, kept old mesh)"
                    regen_ok = (value_ok and obj.name in bpy.data.objects and obj.data is not None
                                and ctx.view_layer.objects.active == obj and len(bpy.data.objects) == 1)
                ok = size_ok and center_ok and regen_ok
                rows.append((e, ok, f"create {t_create:.1f} ms, fit {t_fit:.1f} ms, regen {t_regen:.1f} ms; "
                             f"size {ext:.3f}{' (degenerate)' if degenerate else ''}, "
                             f"{len(specs)} settings{', changed ' + num.id + note if num else ''}"
                             + ("" if ok else f" | size_ok={size_ok} center_ok={center_ok} regen_ok={regen_ok}")))
            else:
                rows.append((e, True, f"own {obj.vr_prim.kind}, create {t_create:.1f} ms"))
        except Exception as ex:
            traceback.print_exc()
            rows.append((e, False, f"ERROR {ex}"))
    for e, ok, info in rows:
        check(ok, f"{(e.category + ' / ') if e.category else ''}{e.label}: {info}")


def test_picker():
    print("== Add picker")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    s.mode = 'MODEL'
    s.primitive = primitives.DEFAULT_ADD
    clear()
    widgets, _ = vr_ui.layout(menus.tray_rows(ctx, "model"))
    add = vr_ui.find(widgets, "add")
    check(add is not None and add.kind == 'select' and vr_ui.find(widgets, "shape_cube").active,
          "the Model token's tray: Cube is taken, All shapes is a select")
    add.on_press()
    check(menus.picker_open(), "pressing it opens the picker")
    widgets, _ = vr_ui.layout(menus.picker_rows())
    ids = [w.id for w in widgets]
    check("cat_Mesh" in ids and "cat_Gears" in ids, "picker starts on categories (Mesh, Gears, ...)")
    check(vr_ui.find(widgets, "cat_Mesh").active, "the current value's category is highlighted")
    vr_ui.find(widgets, "cat_Gears").on_press()
    widgets, _ = vr_ui.layout(menus.picker_rows())
    gear_key = next(e.key for e in catalog.entries() if e.idname == "mesh.primitive_gear")
    check(vr_ui.find(widgets, "opt_" + gear_key) is not None, "Gears category lists Gear")
    vr_ui.find(widgets, "picker_back").on_press()
    widgets, _ = vr_ui.layout(menus.picker_rows())
    check(vr_ui.find(widgets, "cat_Gears") is not None and menus.picker_open(), "Back returns to categories")
    vr_ui.find(widgets, "cat_Mesh").on_press()
    widgets, _ = vr_ui.layout(menus.picker_rows())
    mesh_items = [w for w in widgets if w.id.startswith("opt_")]
    check(len(mesh_items) == len([e for e in catalog.entries() if not e.category]), "Mesh shows all built-ins")
    vr_ui.find(widgets, "cat_Mesh") is None
    vr_ui.find(widgets, "picker_back").on_press()
    widgets, _ = vr_ui.layout(menus.picker_rows())
    vr_ui.find(widgets, "cat_Gears").on_press()
    widgets, _ = vr_ui.layout(menus.picker_rows())
    vr_ui.find(widgets, "opt_" + gear_key).on_press()
    check(s.primitive == gear_key and not menus.picker_open(), "choosing Gear sets it and closes the picker")
    widgets, _ = vr_ui.layout(menus.tray_rows(ctx, "model"))
    check(vr_ui.find(widgets, "add").value_text == "Gear", "All shapes now shows Gear")

    # Paging: a picker with many options.
    menus.open_picker("Many", [menus.Option(i, f"Item {i}") for i in range(40)], 0, lambda v: None)
    widgets, _ = vr_ui.layout(menus.picker_rows())
    shown = [w for w in widgets if w.id.startswith("opt_")]
    check(len(shown) == menus.PICKER_COLS * menus.PICKER_ROWS and vr_ui.find(widgets, "picker_next"),
          "long lists are paged")
    vr_ui.find(widgets, "picker_next").on_press()
    widgets, _ = vr_ui.layout(menus.picker_rows())
    check(vr_ui.find(widgets, "opt_18") is not None and vr_ui.find(widgets, "picker_prev"), "Next shows page 2")
    menus.close_picker()


def chips_of(ctx, obj):
    """The selected object's chips, expanded (More): an operator primitive's settings are there."""
    menus._set_expanded(obj, True)
    return vr_ui.layout(menus.object_rows(ctx, obj))[0]


def test_settings_panel():
    print("== settings chips for operator primitives")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    s.mode = 'MODEL'
    clear()
    gear_key = next(e.key for e in catalog.entries() if e.idname == "mesh.primitive_gear")
    gear = primitives.create_from_entry(ctx, gear_key, (0, 0, 0))
    collapsed = {w.id for w in vr_ui.layout(menus.object_rows(ctx, gear))[0]}
    check("param_number_of_teeth" not in collapsed and "more" in collapsed, "settings wait under More")
    widgets = chips_of(ctx, gear)
    ids = {w.id for w in widgets}
    check("param_number_of_teeth" in ids and "param_radius" in ids, "gear shows Teeth and Radius")
    check(not ({"param_change", "param_Gear", "param_name", "param_align"} & ids), "internal/placement settings hidden")
    check("params_next" in ids, "gear's 11 settings are paged")
    teeth = vr_ui.find(widgets, "param_number_of_teeth")
    verts = len(gear.data.vertices)
    teeth.on_change(20)
    check(len(gear.data.vertices) > verts and generic.stored(gear)["number_of_teeth"] == 20,
          "Teeth slider regenerates a gear with more teeth")
    vr_ui.find(widgets, "params_next").on_press()
    widgets2 = chips_of(ctx, gear)
    check(vr_ui.find(widgets2, "param_number_of_teeth") is None and "params_prev" in {w.id for w in widgets2},
          "Next shows the second settings page")

    clear()
    torus_key = next(e.key for e in catalog.entries() if e.idname == "mesh.primitive_torus_add")
    torus = primitives.create_from_entry(ctx, torus_key, (0, 0, 0))
    widgets = chips_of(ctx, torus)
    mode = vr_ui.find(widgets, "param_mode")
    check(mode is not None and mode.kind == 'select', "enum settings (torus Dimensions Mode) are selects")
    mode.on_press()
    widgets, _ = vr_ui.layout(menus.picker_rows())
    check(vr_ui.find(widgets, "opt_EXT_INT") is not None, "the enum picker lists its items directly")
    vr_ui.find(widgets, "opt_EXT_INT").on_press()
    check(generic.stored(torus)["mode"] == 'EXT_INT' and not menus.picker_open(), "picking an item sets the setting")
    # The torus has 8 settings: Generate UVs is on page 2.
    vr_ui.find(chips_of(ctx, torus), "params_next").on_press()
    uvs = vr_ui.find(chips_of(ctx, torus), "param_generate_uvs")
    check(uvs is not None and uvs.kind == 'button' and uvs.active, "bool settings are toggle buttons")
    uvs.on_press()
    check(generic.stored(torus)["generate_uvs"] is False, "toggling flips the bool")


def test_slow_generator():
    print("== slow generators regenerate on slider release")
    ctx = bpy.context
    clear()
    wall_key = next(e.key for e in catalog.entries() if e.idname == "mesh.wall_add")
    wall = primitives.create_from_entry(ctx, wall_key, (0, 0, 0))
    generic.fit(ctx, wall, (0, 0, 0), (0.1, 0.1, 0.1), 0.005)
    check(wall.get("vrs_slow", False), "Wall Factory is detected as slow")
    widgets = chips_of(ctx, wall)
    slider = next(w for w in widgets if w.kind == 'slider' and w.id.startswith("param_"))
    mesh_before = wall.data
    t0 = time.perf_counter()
    slider.on_change(slider.min + (slider.max - slider.min) * 0.7)
    ms = (time.perf_counter() - t0) * 1000
    check(ms < 20 and wall.data == mesh_before, f"slider change on a slow generator is instant ({ms:.1f} ms), no regenerate yet")
    generic.flush_pending(ctx)
    check(wall.data != mesh_before, "release (flush) regenerates it")


def test_drag_gear():
    print("== drag-create a gear with the trigger")
    clear()
    tm.operators.session_running = lambda context: True
    ss = tm.FakeSessionState()
    wm = tm.FakeWM(ss)
    ctx = tm.FakeContext(wm)
    s = bpy.context.scene.vr_sculpt
    s.mode, s.tip_offset, s.show_panel = 'MODEL', 0.0, False
    s.primitive = next(e.key for e in catalog.entries() if e.idname == "mesh.primitive_gear")
    op, r = tm.press(ctx, ss, 1, [(1, 0, 1), (1.1, 0.1, 1.02), (1.3, 0.2, 1.05)])
    obj = bpy.context.view_layer.objects.active
    bpy.context.view_layer.update()
    lo, hi = world_bounds(obj)
    check(r == {'FINISHED'} and obj.vr_prim.kind == 'OPERATOR', "drag created an operator primitive")
    check(abs(max((hi - lo)[:]) - 0.3) < 0.005, f"gear's largest side matches the drag box (0.3 m, got {max((hi - lo)[:]):.3f})")
    check(((lo + hi) / 2 - Vector((1.15, 0.1, 1.025))).length < 0.005, "gear centered in the drag box")
    check(len(bpy.data.objects) == 1, "no leftover temporary objects")


def main():
    mod = addon_utils.enable("bl_ext.blender_org.extra_mesh_objects", default_set=True)
    print("Extra Mesh Objects enabled:", mod is not None)
    vr_sculpt.register()
    try:
        clear()
        test_catalog()
        test_every_entry()
        test_picker()
        test_settings_panel()
        test_slow_generator()
        test_drag_gear()
    except Exception:
        traceback.print_exc()
        failures.append("exception")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "FAILED " + str(failures) if failures else "ALL PASSED")


main()
