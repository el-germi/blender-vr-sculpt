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
from vr_sculpt import catalog, generic, menus, primitives, radial  # noqa: E402
from vr_sculpt.radial import Item, Level  # noqa: E402

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


def test_add_pie():
    print("== Add pie")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    s.mode = 'MODEL'
    s.primitive = primitives.DEFAULT_ADD
    clear()
    add = radial.find(menus.tool_level(ctx), "add")
    check(add is not None and add.kind == 'branch' and add.label == "Cube",
          "Add (B pie) is a branch named after the current primitive")
    cats = add.children()
    ids = {i.id for i in radial.walk(cats, depth=1)}
    check("cat_Mesh" in ids and "cat_Gears" in ids, "it opens on the categories (Mesh, Gears, ...)")
    check(len(cats.items) <= radial.MAX_SLICES, f"at most 8 slices ({len(cats.items)})")
    check(radial.find(cats, "cat_Mesh").active, "the current value's category is highlighted")
    gear_key = next(e.key for e in catalog.entries() if e.idname == "mesh.primitive_gear")
    gears = radial.find(cats, "cat_Gears").children()
    check(radial.find(gears, "opt_" + gear_key) is not None, "Gears lists Gear")
    mesh = radial.find(cats, "cat_Mesh").children()
    mesh_items = [i for i in radial.walk(mesh) if i.id.startswith("opt_")]
    check(len(mesh_items) == len([e for e in catalog.entries() if not e.category]),
          f"Mesh has all built-ins ({len(mesh_items)}), paged with More")
    path = radial.path_to(add.children(), "opt_" + gear_key)
    check(path is not None and len(path) >= 2, f"Gear is reachable: Add > ... > Gear ({path})")
    radial.find(gears, "opt_" + gear_key).action()
    check(s.primitive == gear_key, "choosing Gear sets it")
    check(radial.find(menus.tool_level(ctx), "add").label == "Gear", "Add now shows Gear")

    # Paging: a pie with many options.
    many = Level("Many", [Item(f"opt_{i}", f"Item {i}", action=lambda: None) for i in range(40)])
    check(len(many.items) == radial.MAX_SLICES and many.items[-1].label == "More", "long lists are paged")
    check(radial.find(many.items[-1].children(), "opt_18") is not None, "More shows the next page")
    check(len([i for i in radial.walk(many) if i.id.startswith("opt_")]) == 40, "and every item is reachable")


def test_settings_pie():
    print("== settings pie for operator primitives")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    s.mode = 'MODEL'
    clear()
    gear_key = next(e.key for e in catalog.entries() if e.idname == "mesh.primitive_gear")
    gear = primitives.create_from_entry(ctx, gear_key, (0, 0, 0))
    shape = radial.find(menus.object_level(ctx), "page_shape").children()
    ids = {i.id for i in radial.walk(shape)}
    check("param_number_of_teeth" in ids and "param_radius" in ids, "gear shows Teeth and Radius (A > Shape)")
    check(not ({"param_change", "param_Gear", "param_name", "param_align"} & ids), "internal/placement settings hidden")
    check(shape.items[-1].label == "More", "gear's 11 settings are paged")
    teeth = radial.find(shape, "param_number_of_teeth")
    check(teeth.kind == 'dial', "Teeth is a dial")
    verts = len(gear.data.vertices)
    teeth.dial.begin()
    start = teeth.dial.get()
    teeth.dial.turn((20 - start) * teeth.dial.step_angle)
    check(len(gear.data.vertices) > verts and generic.stored(gear)["number_of_teeth"] == 20,
          f"twisting Teeth regenerates a gear with more teeth (from {start})")
    second = {i.id for i in radial.walk(shape.items[-1].children())}
    check("param_number_of_teeth" not in second and second, "More shows the other settings")

    clear()
    torus_key = next(e.key for e in catalog.entries() if e.idname == "mesh.primitive_torus_add")
    torus = primitives.create_from_entry(ctx, torus_key, (0, 0, 0))
    shape = radial.find(menus.object_level(ctx), "page_shape").children()
    mode = radial.find(shape, "param_mode")
    check(mode is not None and mode.kind == 'branch', "enum settings (torus Dimensions Mode) open a pie")
    ext = radial.find(mode.children(), "opt_EXT_INT")
    check(ext is not None, "it lists the enum's items directly")
    ext.action()
    check(generic.stored(torus)["mode"] == 'EXT_INT', "choosing an item sets the setting")
    uvs = radial.find(menus.object_level(ctx), "param_generate_uvs")
    check(uvs is not None and uvs.kind == 'action' and uvs.active, "bool settings are toggles")
    uvs.action()
    check(generic.stored(torus)["generate_uvs"] is False, "toggling flips the bool")


def test_slow_generator():
    print("== slow generators regenerate when the dial is let go")
    ctx = bpy.context
    clear()
    wall_key = next(e.key for e in catalog.entries() if e.idname == "mesh.wall_add")
    wall = primitives.create_from_entry(ctx, wall_key, (0, 0, 0))
    generic.fit(ctx, wall, (0, 0, 0), (0.1, 0.1, 0.1), 0.005)
    check(wall.get("vrs_slow", False), "Wall Factory is detected as slow")
    shape = radial.find(menus.object_level(ctx), "page_shape").children()
    dial = next(i.dial for i in radial.walk(shape) if i.dial is not None and i.dial.scale != 'STEP')
    mesh_before = wall.data
    t0 = time.perf_counter()
    dial.set(dial.lo + (dial.hi - dial.lo) * 0.7)
    ms = (time.perf_counter() - t0) * 1000
    check(ms < 20 and wall.data == mesh_before, f"turning a slow generator's dial is instant ({ms:.1f} ms), no regenerate yet")
    generic.flush_pending(ctx)
    check(wall.data != mesh_before, "letting go (flush) regenerates it")


def test_drag_gear():
    print("== drag-create a gear with the trigger")
    clear()
    tm.operators.session_running = lambda context: True
    ss = tm.FakeSessionState()
    wm = tm.FakeWM(ss)
    ctx = tm.FakeContext(wm)
    s = bpy.context.scene.vr_sculpt
    s.mode, s.tip_offset = 'MODEL', 0.0
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
        test_add_pie()
        test_settings_pie()
        test_slow_generator()
        test_drag_gear()
    except Exception:
        traceback.print_exc()
        failures.append("exception")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "FAILED " + str(failures) if failures else "ALL PASSED")


main()
