"""Background tests for Model mode: primitives, the pies, and trigger interactions.

blender --background --factory-startup --python tests/test_model.py
"""

import math
import os
import sys
import time
import traceback

import bpy
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import vr_sculpt  # noqa: E402
from vr_sculpt import actions, interactions, menus, operators, primitives, radial, vr_ui  # noqa: E402

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def close(a, b, tol=1e-4):
    return all(abs(x - y) < tol for x, y in zip(a, b))


# --- fakes (same idea as test_modal.py) ---------------------------------

class FakeXR:
    def __init__(self, loc, rot=Quaternion(), value=1.0, hand=actions.HANDS[1]):
        self.action_set = actions.ACTIONMAP
        self.action = actions.ACTION_STROKE
        self.user_path = hand
        self.controller_location = Vector(loc)
        self.controller_rotation = Quaternion(rot)
        self.state = (value, 0.0)
        self.float_threshold = 0.3


class FakeEvent:
    __slots__ = ("type", "value", "xr")

    def __init__(self, type, value='NOTHING', xr=None):
        self.type, self.value, self.xr = type, value, xr


class FakeSessionState:
    viewer_scale = 1.0
    viewer_pose_location = Vector((0.0, -1.0, 1.5))

    def __init__(self):
        self.trigger = 1.0
        self.aim_loc = [Vector((-0.3, -0.5, 1.0)), Vector((0.3, -0.5, 1.0))]
        self.aim_rot = [Quaternion(), Quaternion()]

    def action_state_get(self, *args):
        return self.trigger

    def controller_aim_location_get(self, context, i):
        return self.aim_loc[i].copy()

    def controller_aim_rotation_get(self, context, i):
        return self.aim_rot[i].copy()

    def controller_grip_location_get(self, context, i):
        return Vector((-0.3, -0.5, 1.0)) if i == 0 else Vector((0.3, -0.5, 1.0))

    def haptic_action_apply(self, *args):
        return True


class FakeWM:
    def __init__(self, ss):
        self.xr_session_state = ss
        self.timers = set()
        self.windows = bpy.context.window_manager.windows

    def event_timer_add(self, step, window=None):
        t = object()
        self.timers.add(t)
        return t

    def event_timer_remove(self, t):
        self.timers.remove(t)

    def modal_handler_add(self, op):
        return True


class FakeContext:
    """Overrides a few members; everything else comes from bpy.context."""

    def __getattr__(self, name):
        return getattr(bpy.context, name)

    def __init__(self, wm):
        self.window_manager = wm
        self.window = None
        self.screen = None


def harness():
    cls = operators.VRSCULPT_OT_stroke
    names = ("invoke", "modal", "_modal", "_apply", "_finish", "_start_modal")
    h = type("TriggerHarness", (), {k: cls.__dict__[k] for k in names})()
    h.report = lambda kind, msg: print("    report:", kind, msg)
    return h


def press(ctx, ss, hand_idx, path_points, rot=Quaternion()):
    """Hold the trigger while the controller moves through path_points (aim locations), then release."""
    ss.trigger = 1.0
    ss.aim_loc[hand_idx] = Vector(path_points[0])
    ss.aim_rot[hand_idx] = Quaternion(rot)
    op = harness()
    r = op.invoke(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR(path_points[0], rot, hand=actions.HANDS[hand_idx])))
    if r != {'RUNNING_MODAL'}:
        return op, r
    for p in path_points[1:]:
        time.sleep(0.01)
        ss.aim_loc[hand_idx] = Vector(p)
        op.modal(ctx, FakeEvent('TIMER'))
    ss.trigger = 0.0
    time.sleep(0.01)
    return op, op.modal(ctx, FakeEvent('TIMER'))


def aim_at(target, normal, distance=0.3):
    """Controller location and rotation that point the aim ray (-Z) at target from the normal side."""
    origin = Vector(target) + Vector(normal).normalized() * distance
    rot = (Vector(target) - origin).to_track_quat('-Z', 'Y')
    return origin, rot


# --- tests ----------------------------------------------------------------

def test_primitives():
    print("== primitives")
    ctx = bpy.context
    cube = primitives.create(ctx, 'CUBE', (1, 2, 3))
    check(len(cube.data.vertices) == 8, "cube has 8 verts")
    check(close(cube.dimensions, (0.1, 0.1, 0.1)), f"cube default size 10 cm ({tuple(cube.dimensions)})")
    check(ctx.view_layer.objects.active == cube and cube.select_get(), "new primitive is active and selected")
    cube.vr_prim.size = (0.2, 0.4, 0.6)
    check(close(cube.dimensions, (0.2, 0.4, 0.6)), "size setting rebuilds the mesh")
    cube.vr_prim.subdivisions = 2
    check(len(cube.data.vertices) == 6 * 9 - 12 * 3 + 8 - 12 * 0 or len(cube.data.vertices) == 56,
          f"subdivisions add a grid ({len(cube.data.vertices)} verts)")
    check(close(cube.dimensions, (0.2, 0.4, 0.6)), "subdivided cube keeps its size")

    cyl = primitives.create(ctx, 'CYLINDER', (0, 0, 0))
    cyl.vr_prim.segments = 12
    check(len(cyl.data.vertices) == 24, f"cylinder with 12 segments has 24 verts ({len(cyl.data.vertices)})")
    cyl.vr_prim.radius = 0.3
    cyl.vr_prim.height = 0.5
    check(close(cyl.dimensions, (0.6, 0.6, 0.5), 1e-3), f"cylinder radius/height ({tuple(cyl.dimensions)})")

    center = primitives.fit_box(cube.vr_prim, (0, 0, 0), (0.3, -0.2, 0.1))
    check(close(cube.vr_prim.size, (0.3, 0.2, 0.1)) and close(center, (0.15, -0.1, 0.05)),
          "fit_box sizes a cube to the drag box")
    primitives.fit_box(cyl.vr_prim, (0, 0, 0), (0.4, 0.2, -0.3))
    check(abs(cyl.vr_prim.radius - 0.2) < 1e-6 and abs(cyl.vr_prim.height - 0.3) < 1e-6,
          "fit_box sizes a cylinder (radius from the wider side)")

    cube.location = (5, 5, 5)
    ctx.view_layer.update()
    check(primitives.touching(ctx, (5.0, 5.0, 5.05), 0.01) == cube, "touching finds an object around the tip")
    check(primitives.touching(ctx, (5.0, 5.0, 5.5), 0.01) is None, "touching ignores far points")

    dup = primitives.duplicate(ctx, cube)
    check(dup != cube and dup.data != cube.data and dup.vr_prim.kind == 'CUBE', "duplicate copies object, mesh and settings")
    dup.vr_prim.size = (1, 1, 1)
    check(close(cube.dimensions, (0.3, 0.2, 0.1)), "editing the duplicate leaves the original alone")
    name = dup.name
    primitives.delete(ctx, dup)
    check(name not in bpy.data.objects, "delete removes the object")

    primitives.forget(cube)
    before = len(cube.data.vertices)
    cube.vr_prim.size = (2, 2, 2)
    check(cube.vr_prim.kind == 'NONE' and len(cube.data.vertices) == before and close(cube.dimensions, (0.3, 0.2, 0.1)),
          "a forgotten (sculpted) primitive is no longer rebuilt")
    for o in (cube, cyl):
        primitives.delete(ctx, o)


def test_layout():
    print("== card layout and hit testing (the desktop screen's title bar and keyboard)")
    clicked = []
    rows = [[vr_ui.button("a", "A", lambda: clicked.append("a")), vr_ui.button("b", "B", lambda: None)],
            [vr_ui.label("hello")],
            [vr_ui.slider("s", "S", 0.5, 0.0, 1.0, lambda v: None)]]
    widgets, height = vr_ui.layout(rows)
    expected = vr_ui.PAD * 2 + vr_ui.ROW_H * 2 + vr_ui.LABEL_H + vr_ui.GAP * 2
    check(abs(height - expected) < 1e-9, f"height adds rows, gaps and padding ({height:.4f})")
    a, b, lab, s = widgets
    check(a.rect[1] > s.rect[1], "rows go top to bottom")
    check(a.rect[2] < b.rect[0], "cells in a row don't overlap")
    cx, cy = (a.rect[0] + a.rect[2]) / 2, (a.rect[1] + a.rect[3]) / 2
    check(vr_ui.hit_test(widgets, cx, cy) is a, "hit_test finds the button under the pointer")
    lx, ly = (lab.rect[0] + lab.rect[2]) / 2, (lab.rect[1] + lab.rect[3]) / 2
    check(vr_ui.hit_test(widgets, lx, ly) is None, "labels can't be hit")
    x0, _, x1, _ = s.rect
    check(abs(s.value_at(x0) - 0.0) < 1e-9 and abs(s.value_at(x1) - 1.0) < 1e-9, "slider maps its track ends to min/max")
    check(abs(s.value_at((x0 + x1) / 2) - 0.5) < 1e-6, "slider middle is the middle value")
    si = vr_ui.slider("i", "I", 3, 3, 64, lambda v: None, integer=True)
    vr_ui.layout([[si]])
    check(isinstance(si.value_at(0.0), int), "integer sliders give ints")

    m = vr_ui.panel_matrix(Vector((0, 0, 1)), Vector((0, -1, 1)), 1.0)
    hit = vr_ui.ray_hit(m, Vector((0.01, -0.5, 1.02)), Vector((0, 1, 0)), height, 1.0)
    check(hit is not None and abs(hit[0] - 0.01) < 1e-6 and abs(hit[1] - 0.02) < 1e-6,
          f"ray straight at the panel hits where it points ({hit})")
    check(vr_ui.ray_hit(m, Vector((0, -0.5, 1.02)), Vector((0, -1, 0)), height, 1.0) is None, "ray pointing away misses")
    check(vr_ui.ray_hit(m, Vector((0.5, -0.5, 1.02)), Vector((0, 1, 0)), height, 1.0) is None, "ray beside the panel misses")


def test_menus():
    print("== pies")
    ctx = bpy.context
    settings = ctx.scene.vr_sculpt
    for mode in ('SCULPT', 'MODEL'):
        for engine in ('PYTHON', 'NATIVE'):
            settings.mode, settings.engine = mode, engine
            ids = {i.id for i in menus.mode_level(ctx).items}
            check({"mode_sculpt", "mode_model"} <= ids, f"{mode}/{engine}: the mode pie has the modes")
            check(menus.tool_level(ctx).items and menus.tool_level(ctx).center is not None,
                  f"{mode}/{engine}: the tool pie has slices and a center dial")
    settings.mode = 'MODEL'
    cyl = primitives.create(ctx, 'CYLINDER', (0, 0, 0))
    ids = {i.id for i in radial.walk(menus.object_level(ctx))}
    check({"radius_prim", "height", "segments", "duplicate", "delete"} <= ids,
          "the object pie has the cylinder's settings when it's selected")
    seg = menus.find(ctx, "segments").dial
    seg.begin()
    seg.turn(-(seg.start - 8) * seg.step_angle)
    check(cyl.vr_prim.segments == 8 and len(cyl.data.vertices) == 16, "twisting the Segments dial edits the primitive")
    primitives.delete(ctx, cyl)
    settings.mode = 'SCULPT'
    settings.engine = 'PYTHON'


def test_trigger():
    print("== trigger in Model mode")
    operators.session_running = lambda context: True
    ss = FakeSessionState()
    wm = FakeWM(ss)
    ctx = FakeContext(wm)
    settings = bpy.context.scene.vr_sculpt
    settings.mode = 'MODEL'
    settings.tip_offset = 0.0

    settings.primitive = 'mesh.primitive_cube_add'
    n = len(bpy.data.objects)
    op, r = press(ctx, ss, 1, [(1.0, 0, 1), (1.1, 0.05, 1.05), (1.2, 0.1, 1.15)])
    obj = bpy.context.view_layer.objects.active
    check(r == {'FINISHED'} and len(bpy.data.objects) == n + 1, f"drag in empty space creates an object ({r})")
    check(obj.vr_prim.kind == 'CUBE' and close(obj.vr_prim.size, (0.2, 0.1, 0.15), 1e-5),
          f"cube fills the drag box ({tuple(round(v, 3) for v in obj.vr_prim.size)})")
    check(close(obj.location, (1.1, 0.05, 1.075), 1e-5), "cube is centered in the drag box")
    check(not wm.timers, "timer removed")

    settings.primitive = 'mesh.primitive_cylinder_add'
    op, r = press(ctx, ss, 0, [(3, 0, 1), (3.002, 0, 1)])
    cyl = bpy.context.view_layer.objects.active
    check(cyl.vr_prim.kind == 'CYLINDER' and abs(cyl.vr_prim.height - primitives.DEFAULT_SIZE) < 1e-6,
          "a click (tiny drag) makes a default-size cylinder, left hand works too")

    # Grab the cube from inside and move it.
    start = obj.matrix_world.copy()
    op, r = press(ctx, ss, 1, [(1.1, 0.05, 1.075), (1.3, 0.05, 1.075), (1.6, 0.25, 1.075)])
    check(type(op.handler).__name__ == "MoveObject", "trigger inside an object grabs it")
    check(close(obj.location, (1.6, 0.25, 1.075), 1e-5), f"grabbed object follows the controller ({tuple(obj.location)})")
    check(obj.matrix_world.to_3x3() == start.to_3x3(), "no rotation when the controller doesn't rotate")
    check(r == {'FINISHED'}, "move pushes an undo step")

    # Rotating the controller rotates the grabbed object around the tip.
    p = Vector((1.6, 0.25, 1.075))
    op = harness()
    ss.trigger = 1.0
    op.invoke(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR(p)))
    ss.aim_rot[1] = Quaternion((0, 0, 1), 1.5708)
    time.sleep(0.01)
    op.modal(ctx, FakeEvent('TIMER'))
    ss.trigger = 0.0
    time.sleep(0.01)
    op.modal(ctx, FakeEvent('TIMER'))
    ss.aim_rot[1] = Quaternion()
    check(abs(obj.matrix_world.to_euler().z - 1.5708) < 1e-3, "grab follows controller rotation")

    # A pie left open on Sculpt (Y, let go on a branch): the trigger chooses in it instead of creating.
    hand = Vector((-0.3, -0.5, 1.0))
    level = menus.mode_level(bpy.context)
    pie = radial.Pie('MODE', 0, level, Matrix.Translation(hand), Quaternion())
    pie.update(hand + Vector((0.0, 0.03, 0.0)), Quaternion())  # Sculpt is the top slice.
    pie.sticky = True
    radial.pie = pie
    check(pie.hovered().id == "mode_sculpt", "pointing at Sculpt in the mode pie")
    n = len(bpy.data.objects)
    op, r = press(ctx, ss, 1, [(1.0, 0.0, 1.0), (1.2, 0.1, 1.1)])
    check(type(op.handler).__name__ == "PieConfirm", "the trigger goes to the open pie")
    check(settings.mode == 'SCULPT' and len(bpy.data.objects) == n, "it switched mode, nothing created")
    check(radial.pie is None and r == {'FINISHED'}, "the pie closed; the choice is an undo step")

    # The right knob from one end to the other: radius at its minimum, then its maximum.
    settings.radius = 0.04
    knob = radial.Knob(1, menus.knob_dial(bpy.context, 'R'), Quaternion())
    knob.update(Quaternion((0, 0, 1), math.radians(720)))  # Two turns counter-clockwise, in steps.
    for a in range(0, 721, 30):
        knob.update(Quaternion((0, 0, 1), math.radians(a)))
    check(abs(settings.radius - 0.003) < 1e-4, f"twisting left takes the radius to its minimum ({settings.radius:.4f})")
    for a in range(720, -1441, -30):
        knob.update(Quaternion((0, 0, 1), math.radians(a)))
    check(abs(settings.radius - 0.3) < 1e-4, f"twisting right takes it to the maximum ({settings.radius:.4f})")
    check(knob.release() == 'done' and knob.changed, "letting go keeps it")

    # In Sculpt mode, sculpting a primitive turns it into a plain mesh.
    settings.engine = 'PYTHON'
    settings.radius = 0.05
    primitives.make_active(bpy.context, cyl)
    cyl.vr_prim.segments = 32
    tip = cyl.matrix_world @ Vector((cyl.vr_prim.radius, 0, 0))
    ss.aim_rot[0] = Quaternion()
    op, r = press(ctx, ss, 0, [tip, tip + Vector((0, 0.01, 0.01)), tip + Vector((0, 0.02, 0.02))])
    check(op.handler is None and cyl.vr_prim.kind == 'NONE', "sculpting a primitive makes it a plain mesh")


def main():
    vr_sculpt.register()
    try:
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        test_primitives()
        test_layout()
        test_menus()
        test_trigger()
    except Exception:
        traceback.print_exc()
        failures.append("exception")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "FAILED " + str(failures) if failures else "ALL PASSED")


if __name__ == "__main__":
    main()
