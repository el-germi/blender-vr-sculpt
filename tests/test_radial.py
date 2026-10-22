"""Background tests for the radial UI: pie selection (hand and stick), nesting, sticky pies, dials and
knobs (twist scrubbing), every pie's contents in every mode, and the pie / knob operators.

blender --background --factory-startup --python tests/test_radial.py
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
from vr_sculpt import (actions, anim, interactions, menus, navigation, operators, primitives, radial,  # noqa: E402
                       radial_draw)
from vr_sculpt.radial import Dial, Item, Level, Pie  # noqa: E402

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def twist_q(degrees):
    """A controller turned clockwise (as the user sees it, looking along the aim) by degrees."""
    return Quaternion((0.0, 0.0, 1.0), -math.radians(degrees))


def at(x, y):
    return Vector((x, y, 0.0))


def test_math():
    print("== slices and twist")
    check(radial.slice_index(0.0, 1.0, 8) == 0, "up is the first slice")
    check(radial.slice_index(1.0, 0.0, 8) == 2, "right is the third of 8 (clockwise)")
    check(radial.slice_index(0.0, -1.0, 8) == 4 and radial.slice_index(-1.0, 0.0, 8) == 6, "down and left")
    check(radial.slice_index(0.7, 0.7, 8) == 1, "up-right is the second")
    check(radial.slice_index(1.0, 0.0, 4) == 1 and radial.slice_index(0.0, -1.0, 3) in (1, 2), "other counts")
    a = radial.slice_angle(2, 8)
    check(abs(a) < 1e-9, "slice_angle: the third of 8 points right")
    t = radial.twist(Quaternion(), twist_q(30))
    check(abs(t - math.radians(30)) < 1e-6, f"clockwise twist is positive ({math.degrees(t):.1f})")
    check(abs(radial.twist(Quaternion(), twist_q(-50)) + math.radians(50)) < 1e-6, "counter-clockwise is negative")
    # Pointing somewhere else doesn't change the twist about the aim.
    base = Quaternion((1.0, 0.0, 0.0), 0.7)
    t = radial.twist(base, base @ twist_q(20))
    check(abs(t - math.radians(20)) < 1e-6, "twist is about the controller's own aim axis")
    swing = Quaternion((1.0, 0.0, 0.0), 0.4)
    check(abs(radial.twist(Quaternion(), swing)) < 1e-6, "tilting the controller is no twist")


def test_dials():
    print("== dials")
    box = {"v": 0.04}
    d = Dial("Radius", lambda: box["v"], lambda v: box.__setitem__("v", v), 0.003, 0.3, 'LOG')
    d.begin()
    d.turn(math.radians(90))
    check(abs(box["v"] - 0.08) < 1e-9, f"LOG: a quarter turn doubles ({box['v']:.4f})")
    d.turn(math.radians(-90))
    check(abs(box["v"] - 0.02) < 1e-9, "LOG: a quarter turn back halves (from the start)")
    d.turn(math.radians(2000))
    check(box["v"] == 0.3, "LOG: clamped to the top")
    check(abs(d.fraction() - 1.0) < 1e-9, "fraction at the top is 1")
    box["v"] = 0.5
    lin = Dial("Strength", lambda: box["v"], lambda v: box.__setitem__("v", v), 0.0, 1.0)
    lin.begin()
    lin.turn(math.radians(90))
    check(abs(box["v"] - 1.0) < 1e-9, "LINEAR: a half turn sweeps the range (a quarter = half of it)")
    box["v"] = 3
    st = Dial("Count", lambda: box["v"], lambda v: box.__setitem__("v", v), 1, 24, 'STEP')
    st.begin()
    st.turn(math.radians(46))
    check(box["v"] == 6 and isinstance(box["v"], int), f"STEP: one step per 15 degrees, integers ({box['v']})")
    check(st.notch(math.radians(46)) == 3, "notches count steps (haptic ticks)")
    seen = []
    st.on_turn = seen.append
    st.turn(math.radians(60))
    check(seen == [st], "on_turn hears about changes")


def make_level(log):
    """8 slices: actions, a branch (with its own actions), a dial, a dangerous one."""
    box = {"v": 0.5}
    sub = lambda: Level("Sub", [Item("sub_a", "Sub A", action=lambda: log.append("sub_a")),  # noqa: E731
                                Item("sub_b", "Sub B", action=lambda: log.append("sub_b"))])
    items = [
        Item("a", "A", action=lambda: log.append("a")),
        Item("branch", "Branch", children=sub, action=lambda: log.append("branch")),
        Item("c", "C", action=lambda: log.append("c")),
        Item("dial", "Dial", dial=Dial("Dial", lambda: box["v"], lambda v: box.__setitem__("v", v), 0.0, 1.0)),
        Item("e", "E", action=lambda: log.append("e")),
        Item("f", "F", action=lambda: log.append("f"), danger=True),
        Item("g", "G", action=lambda: log.append("g")),
        Item("h", "H", action=lambda: log.append("h")),
    ]
    center = Item("center", "Size", dial=Dial("Size", lambda: box["v"], lambda v: box.__setitem__("v", v), 0.0, 1.0))
    return Level("Test", items, center=center), box


def pie(level, t0):
    return Pie('TOOL', 1, level, Matrix.Identity(4), Quaternion(), now=t0)


def test_pie():
    print("== pie: hand, stick, nesting")
    t = time.monotonic()
    log = []
    level, box = make_level(log)
    check(radial.find(level, "sub_b") is not None and radial.path_to(level, "sub_b") == [1, 1],
          "find / path_to open branches")

    # A quick press and release is a tap.
    p = pie(level, t)
    p.update(at(0, 0), Quaternion(), now=t + 0.05)
    check(p.release(now=t + 0.1) == 'tap' and p.closed and not log, "press and let go at once: a tap")

    # Move the hand right: the slice on the right (index 2 of 8) lights up; letting go runs it.
    p = pie(level, t)
    events = p.update(at(0.035, 0.0), Quaternion(), now=t + 0.1)
    check(p.hover == 2 and 'hover' in events, f"moving the hand toward a slice hovers it ({p.hover}, {events})")
    check(p.release(now=t + 0.5) == 'done' and log == ["c"] and p.ran.id == "c", "letting go runs it")
    log.clear()

    # A small move stays in the middle (the center dial is hovered, nothing runs).
    p = pie(level, t)
    p.update(at(0.008, 0.004), Quaternion(), now=t + 0.1)
    check(p.hover == radial.CENTER, "inside the dead zone: the center")
    check(p.release(now=t + 0.6) == 'cancel' and not log, "letting go in the middle (not a tap) does nothing")

    # Stick: flick up-left picks slice 7; it holds after the stick springs back.
    p = pie(level, t)
    p.update(at(0, 0), Quaternion(), (-0.7, 0.7), now=t + 0.05)
    check(p.hover == 7 and p.by_stick, "a stick flick picks the slice in its direction")
    p.update(at(0.002, 0), Quaternion(), (0.0, 0.0), now=t + 0.1)
    check(p.hover == 7, "the stick's pick holds when the stick springs back")
    p.update(at(0.0, -0.04), Quaternion(), (0.0, 0.0), now=t + 0.15)
    check(p.hover == 4, "moving the hand takes over from the stick")
    check(p.release(now=t + 0.3) == 'done' and log == ["e"], "and letting go runs the hand's choice")
    log.clear()

    # Push through a branch: its pie opens where the hand is; then pick in it.
    p = pie(level, t)
    d = Vector((math.cos(radial.slice_angle(1, 8)), math.sin(radial.slice_angle(1, 8))))
    p.update(at(*(d * 0.04)), Quaternion(), now=t + 0.05)
    check(p.hover == 1 and len(p.frames) == 1, "hovering a branch doesn't open it at once")
    events = p.update(at(*(d * 0.08)), Quaternion(), now=t + 0.1)
    check(len(p.frames) == 2 and 'open' in events and p.level.title == "Sub", "pushing through it opens it")
    check((p.center - d * 0.08).length < 1e-6, "the sub pie is centered at the hand")
    p.update(at(*(d * 0.08 + Vector((0.0, -0.03)))), Quaternion(), now=t + 0.2)  # Sub has 2 slices: up, down.
    check(p.hover == 1, "picking in the sub pie")
    check(p.release(now=t + 0.3) == 'done' and log == ["sub_b"], "letting go runs the sub pie's slice")
    log.clear()

    # Dwell opens a branch too; returning to the parent pie's center goes back.
    p = pie(level, t)
    p.update(at(*(d * 0.035)), Quaternion(), now=t + 0.05)
    p.update(at(*(d * 0.035)), Quaternion(), now=t + 0.05 + radial.DWELL + 0.05)
    check(len(p.frames) == 2, "dwelling on a branch opens it")
    p.update(at(*(d * 0.035 + Vector((0.0, 0.03)))), Quaternion(), now=t + 0.7)
    events = p.update(at(0.0, 0.0), Quaternion(), now=t + 0.8)
    check(len(p.frames) == 1 and 'back' in events, "pulling the hand back to where the pie opened goes up a level")

    # Stick dwell on a branch opens it; the stick must spring back before it picks again.
    p = pie(level, t)
    p.update(at(0, 0), Quaternion(), (0.7, 0.7), now=t + 0.05)
    p.update(at(0, 0), Quaternion(), (0.7, 0.7), now=t + 0.05 + radial.STICK_DWELL + 0.05)
    check(len(p.frames) == 2, "holding the stick on a branch opens it")
    p.update(at(0, 0), Quaternion(), (0.7, 0.7), now=t + 0.5)
    check(p.hover is None, "the held stick doesn't pick in the new pie")
    p.update(at(0, 0), Quaternion(), (0.0, 0.0), now=t + 0.55)
    p.update(at(0, 0), Quaternion(), (0.0, -0.9), now=t + 0.6)
    check(p.hover == 1, "after springing back, the stick picks in the new pie")

    # Letting go on a branch (or a slice with an action) runs the action.
    p = pie(level, t)
    p.update(at(*(d * 0.04)), Quaternion(), now=t + 0.05)
    check(p.release(now=t + 0.2) == 'done' and log == ["branch"], "letting go on a branch with an action runs it")
    log.clear()


def test_sticky():
    print("== pie: sticky")
    t = time.monotonic()
    log = []
    sub = Level("Sub", [Item("x", "X", action=lambda: log.append("x")), Item("y", "Y", action=lambda: log.append("y"))])
    level = Level("Root", [Item("only_branch", "Branch", children=lambda: sub), Item("z", "Z", action=lambda: 0)])
    p = pie(level, t)
    p.update(at(0.0, 0.03), Quaternion(), now=t + 0.05)
    check(p.release(now=t + 0.2) == 'sticky' and p.sticky and not p.closed and p.level is sub,
          "letting go on a branch without an action leaves its pie open")
    check(p.release(now=t + 0.3) == 'sticky', "a later release doesn't close it")
    p.update(at(0.0, -0.03), Quaternion(), now=t + 0.4)
    check(p.hover == 1 and p.confirm(now=t + 0.5) == 'done' and log == ["y"], "a trigger press chooses in it")
    p = pie(level, t)
    p.update(at(0.0, 0.03), Quaternion(), now=t + 0.05)
    p.release(now=t + 0.2)
    p.update(at(0.0, 0.03), Quaternion(), now=t + 0.3)
    check(p.confirm(now=t + 0.4) == 'cancel' and p.closed, "a trigger press in the middle closes it")
    p = pie(level, t)
    p.update(at(0.0, 0.03), Quaternion(), now=t + 0.05)
    p.release(now=t + 0.2)
    check(p.update(at(0.5, 0.0), Quaternion(), now=t + 0.3) == ['close'] and p.closed,
          "moving the hand far away closes it")


def test_scrub():
    print("== pie: scrubbing dials")
    t = time.monotonic()
    log = []
    level, box = make_level(log)
    # Hover the dial (slice 3, down-right) and twist: it grabs and turns.
    d = Vector((math.cos(radial.slice_angle(3, 8)), math.sin(radial.slice_angle(3, 8))))
    p = pie(level, t)
    p.update(at(*(d * 0.04)), Quaternion(), now=t + 0.05)
    check(p.hover == 3, "hovering the dial")
    p.update(at(*(d * 0.04)), twist_q(4), now=t + 0.1)
    check(p.grabbed is None and box["v"] == 0.5, "a small twist doesn't grab")
    events = p.update(at(*(d * 0.04)), twist_q(45), now=t + 0.15)
    check(p.grabbed is level.items[3] and 'grab' in events, "twisting past the threshold grabs the dial")
    check(abs(box["v"] - 0.75) < 1e-6, f"45 degrees clockwise: +25% of the range ({box['v']:.3f})")
    p.update(at(0.0, 0.08), twist_q(45), now=t + 0.2)
    check(p.grabbed is level.items[3] and len(p.frames) == 1, "once grabbed, moving the hand doesn't change slice")
    events = p.update(at(0.0, 0.08), twist_q(20), now=t + 0.25)
    check(abs(box["v"] - (0.5 + 20 / 180)) < 1e-6 and 'tick' in events, "turning back lowers it, with ticks")
    p.update(at(0.0, 0.08), twist_q(20), (1.0, 0.0), now=t + 0.3, dt=0.1)
    expected = 0.5 + (20 + math.degrees(radial.STICK_RATE * 0.1)) / 180
    check(abs(box["v"] - expected) < 1e-6, f"the stick's X nudges a held dial ({box['v']:.3f})")
    check(p.release(now=t + 0.4) == 'done' and p.changed and p.ran is None, "letting go keeps the value")

    # The center dial: twist without moving the hand.
    box["v"] = 0.2
    p = pie(level, t)
    p.update(at(0.0, 0.0), twist_q(-90), now=t + 0.05)
    check(p.grabbed is level.center and box["v"] == 0.0, "twisting in the middle turns the center dial (clamped)")
    check(p.release(now=t + 0.1) == 'done', "and letting go keeps it")

    # Knob (a stick click held).
    box["v"] = 0.04
    k = radial.Knob(1, Dial("R", lambda: box["v"], lambda v: box.__setitem__("v", v), 0.003, 0.3, 'LOG'),
                    Quaternion(), now=t)
    check(k.update(twist_q(3)) == [] and box["v"] == 0.04, "knob: a small twist is ignored")
    events = k.update(twist_q(90))
    check('grab' in events and abs(box["v"] - 0.08) < 1e-6, f"knob: a quarter turn doubles the radius ({box['v']})")
    # Twisting past half a turn keeps counting (angles are accumulated frame by frame).
    for a in (150, 210, 270):
        k.update(twist_q(a))
    check(abs(box["v"] - 0.3) < 1e-6, "knob: turns add up past 180 degrees")
    check(k.release(now=t + 1.0) == 'done', "knob: let go after a twist keeps it")
    k = radial.Knob(1, None, Quaternion(), now=t)
    check(k.release(now=t + 0.1) == 'tap', "knob: a click without a twist is a tap")


# --- every pie in every mode -------------------------------------------------------

def scene_for_pies():
    ctx = bpy.context
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
    ctx.scene.collection.objects.link(cam)
    ctx.scene.camera = cam
    arm = bpy.data.objects.new("Rig", bpy.data.armatures.new("Rig"))
    ctx.scene.collection.objects.link(arm)
    return ctx


def walk_ids(level):
    ids = []
    for item in radial.walk(level):
        ids.append(item.id)
        item.text()
        if item.dial is not None:
            item.dial.fraction()
            item.dial.text()
    return set(ids)


def test_contents():
    print("== every pie, every mode")
    ctx = scene_for_pies()
    s = ctx.scene.vr_sculpt
    cube = primitives.create(ctx, 'CUBE', (0, 0, 0))
    expect = {
        'SCULPT': ({"brush_DRAW", "brush_SMOOTH", "strength", "radius"}, {"sym_X", "engine_native"}),
        'MODEL': ({"add", "add_as_difference", "mark_hard", "grid_snap", "snap_surface", "grid_size"},
                  {"page_shape", "size_X", "subdivisions", "page_boolean", "page_finish", "shade_auto", "bevel",
                   "page_array", "array_count", "mirror_X", "mirror_here", "solidify", "duplicate", "delete"}),
        'DRAW': ({"style_TUBE", "style_NOTE", "tool_EDIT", "tool_ERASE", "color_GREEN", "draw_width"},
                 {"mirror_x", "mirror_here", "draw_thickness", "opt_TAPER", "draw_taper", "draw_smoothing",
                  "undo_stroke"}),
        'PAINT': (set(), set()),  # Not ready to paint: checked in test_paint.
        'POSE': ({"pose_fk", "pose_ik", "pose_chain", "pose_mirror", "pose_bones", "pose_rig", "rig_Rig"},
                 {"bind_left", "bind_head", "puppet_rec"}),
        'TOOLS': ({"tool_measure", "tool_camera", "grid_show", "grid_size"}, {"measures_show", "measure_clear"}),
    }
    for mode, (tool_ids, object_ids) in expect.items():
        s.mode = mode
        for engine in (('PYTHON', 'NATIVE') if mode == 'SCULPT' else ('PYTHON',)):
            s.engine = engine
            rig = bpy.data.objects["Rig"]
            primitives.make_active(ctx, rig if mode == 'POSE' else cube)
            menus.set_object_mode(ctx, rig, 'POSE' if mode == 'POSE' else 'OBJECT')
            tool = walk_ids(menus.tool_level(ctx))
            obj = walk_ids(menus.object_level(ctx))
            want = tool_ids if engine == 'PYTHON' else {"strength", "radius"}
            check(want <= tool, f"{mode}/{engine}: tool pie has {sorted(want - tool) or 'all'}")
            check(object_ids <= obj, f"{mode}/{engine}: object pie has {sorted(object_ids - obj) or 'all'}")
            check(len(menus.tool_level(ctx).items) <= radial.MAX_SLICES, f"{mode}: at most 8 slices")
    s.mode, s.engine = 'SCULPT', 'PYTHON'
    s.tool = 'CAMERA'
    s.mode = 'TOOLS'
    obj = walk_ids(menus.object_level(ctx))
    check({"camera", "cam_Cam", "camera_new", "camera_preview", "camera_lens"} <= obj, "Tools/Camera: camera pie")
    s.show_new_camera = False
    check("camera_new" not in walk_ids(menus.object_level(ctx)), "New here can be hidden (VR Extras)")
    s.show_new_camera = True
    s.tool = 'MEASURE'
    modes = walk_ids(menus.mode_level(ctx))
    check({"mode_sculpt", "mode_model", "mode_draw", "mode_paint", "mode_pose", "mode_tools", "view", "hints",
           "show_desktop", "passthrough", "camera_preview", "nav_walk", "nav_fly", "nav_speed", "tracking",
           "tracking_record", "reset_view", "brush_INFLATE", "add_as_union"} <= modes,
          "mode pie: modes (with their tools), View, Help")
    tm = walk_ids(menus.time_level(ctx))
    check({"play", "key_next", "key_prev", "key_insert", "key_delete", "autokey", "undo", "redo", "interp",
           "ease_LINEAR", "range_start", "range_end", "show_paths", "onion", "frame"} <= tm, "time pie")
    check(menus.time_level(ctx).center.id == "frame", "the time pie's center is the frame")
    s.mode = 'SCULPT'

    # Pies with more than 8 entries page with "More".
    many = Level("Many", [Item(f"i{i}", str(i), action=lambda: None) for i in range(20)])
    check(len(many.items) == 8 and many.items[-1].kind == 'branch', "20 items: 7 + More")
    check(radial.find(many, "i19") is not None, "the last of them is reachable through More")

    # Choosing in a mode's tools from the Mode pie switches to that mode first.
    level = menus.mode_level(ctx)
    draw_tools = level.items[[i.id for i in level.items].index("mode_draw")].children()
    tube = next(i for i in draw_tools.items if i.id == "style_RIBBON")
    tube.action()
    check(s.mode == 'DRAW' and s.draw_style == 'RIBBON', "Mode pie > Draw > Ribbon switches mode and style")
    menus.set_mode(ctx, 'SCULPT')
    check(menus.previous_mode(ctx) == 'DRAW' and s.mode == 'DRAW', "Y tap goes back to the previous mode")
    menus.set_mode(ctx, 'SCULPT')

    # Dials in the pies edit the real settings.
    s.radius = 0.04
    r = radial.find(menus.tool_level(ctx), "radius").dial
    r.begin()
    r.turn(math.radians(90))
    check(abs(s.radius - 0.08) < 1e-6, "Sculpt radius dial")
    s.mode = 'MODEL'
    primitives.make_active(ctx, cube)
    sx = radial.find(menus.object_level(ctx), "size_X").dial
    before = cube.vr_prim.size[0]
    sx.begin()
    sx.turn(math.radians(90))
    check(abs(cube.vr_prim.size[0] - 2 * before) < 1e-6, "Model: Shape > Size X dial resizes the primitive")
    knob = menus.knob_dial(ctx, 'R')
    check(knob is sx, "Model's R knob turns the last value turned on the object")
    s.mode = 'SCULPT'
    primitives.delete(ctx, cube)


# --- operators --------------------------------------------------------------------------

class FakeXR:
    def __init__(self, action, hand_idx, loc, rot=Quaternion(), value=1.0):
        self.action_set = actions.ACTIONMAP
        self.action = action
        self.user_path = actions.HANDS[hand_idx]
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
        self.values = {}  # (action, hand index) -> value
        self.aim_loc = [Vector((-0.3, -0.5, 1.0)), Vector((0.3, -0.5, 1.0))]
        self.aim_rot = [Quaternion(), Quaternion()]

    def action_state_get(self, context, actionmap, action, user_path):
        return self.values.get((action, actions.hand_index(user_path)), 0.0)

    def controller_aim_location_get(self, context, i):
        return self.aim_loc[i].copy()

    def controller_aim_rotation_get(self, context, i):
        return self.aim_rot[i].copy()

    def controller_grip_location_get(self, context, i):
        return self.aim_loc[i].copy()

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
    def __getattr__(self, name):
        return getattr(bpy.context, name)

    def __init__(self, wm):
        self.window_manager = wm
        self.window = None
        self.screen = None


def op_harness(cls):
    names = [k for k, v in cls.__dict__.items() if callable(v) and not k.startswith("__")]
    h = type(cls.__name__ + "Harness", (), {k: cls.__dict__[k] for k in names})()
    h.report = lambda kind, msg: print("    report:", kind, msg)
    return h


def tick(op, ctx):
    time.sleep(0.012)
    return op.modal(ctx, FakeEvent('TIMER'))


def test_operators():
    print("== pie and knob operators")
    operators.session_running = lambda context: True
    ss = FakeSessionState()
    ctx = FakeContext(FakeWM(ss))
    s = bpy.context.scene.vr_sculpt
    s.mode, s.engine, s.brush = 'SCULPT', 'PYTHON', 'DRAW'
    radial.reset()
    B = (actions.ACTION_PIE, 1)

    # Hold B: the tool pie opens at the right hand; move toward Smooth (slice 1 of 6) and let go.
    ss.values[B] = 1.0
    op = op_harness(operators.VRSCULPT_OT_pie)
    r = op.invoke(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR(*B, ss.aim_loc[1])))
    check(r == {'RUNNING_MODAL'} and radial.pie is not None and radial.pie.kind == 'TOOL', "B opens the tool pie")
    check(radial.busy(1) and not radial.busy(0), "the right stick belongs to the pie now")
    n = len(radial.pie.level.items)
    a = radial.slice_angle(1, n)
    ss.aim_loc[1] = radial.pie.matrix @ Vector((0.04 * math.cos(a), 0.04 * math.sin(a), 0.0))
    tick(op, ctx)
    check(radial.pie.hovered().id == "brush_SMOOTH", f"moving the hand hovers Smooth ({radial.pie.hovered().id})")
    ss.values[B] = 0.0
    r = tick(op, ctx)
    check(r == {'FINISHED'} and s.brush == 'SMOOTH' and radial.pie is None, "letting go picks it (one undo step)")
    check(not ctx.window_manager.timers, "the pie's timer is removed")
    check(radial.current_toast() is not None and radial.current_toast()[0] == "Smooth", "a short toast names it")

    # Tap B: the quick action (next brush).
    ss.aim_loc[1] = Vector((0.3, -0.5, 1.0))
    ss.values[B] = 1.0
    op = op_harness(operators.VRSCULPT_OT_pie)
    op.invoke(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR(*B, ss.aim_loc[1])))
    ss.values[B] = 0.0
    r = tick(op, ctx)
    check(s.brush == 'GRAB' and r == {'CANCELLED'}, f"tapping B steps to the next brush ({s.brush})")

    # Stick: hold B, flick the right stick to the left (slice 3 of 6 is down-left... use the up one).
    ss.values[B] = 1.0
    op = op_harness(operators.VRSCULPT_OT_pie)
    op.invoke(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR(*B, ss.aim_loc[1])))
    ss.values[("fly_up", 1)] = 0.9
    tick(op, ctx)
    check(radial.pie.hovered().id == "brush_DRAW", "flicking the right stick up hovers the top slice")
    ss.values[("fly_up", 1)] = 0.0
    ss.values[B] = 0.0
    tick(op, ctx)
    check(s.brush == 'DRAW', "and letting go of B picks it")

    # Twist in the middle of the tool pie: the radius (its center dial).
    s.radius = 0.04
    ss.values[B] = 1.0
    op = op_harness(operators.VRSCULPT_OT_pie)
    op.invoke(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR(*B, ss.aim_loc[1])))
    ss.aim_rot[1] = twist_q(90)
    tick(op, ctx)
    check(abs(s.radius - 0.08) < 1e-6, f"twisting the hand in the pie's middle doubles the radius ({s.radius:.3f})")
    ss.values[B] = 0.0
    r = tick(op, ctx)
    ss.aim_rot[1] = Quaternion()
    check(r == {'FINISHED'}, "letting go keeps it (an undo step)")

    # Release on a branch leaves it open; the trigger chooses in it.
    s.mode = 'DRAW'
    ss.values[B] = 1.0
    op = op_harness(operators.VRSCULPT_OT_pie)
    op.invoke(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR(*B, ss.aim_loc[1])))
    level = radial.pie.level
    i = [it.id for it in level.items].index("draw_color")
    a = radial.slice_angle(i, len(level.items))
    ss.aim_loc[1] = radial.pie.matrix @ Vector((0.035 * math.cos(a), 0.035 * math.sin(a), 0.0))
    tick(op, ctx)
    ss.values[B] = 0.0
    r = tick(op, ctx)
    check(r == {'RUNNING_MODAL'} and radial.sticky_pie() is not None and radial.pie.level.title == "Color",
          "letting go on Color leaves the color pie open")
    sub = radial.pie
    j = [it.id for it in sub.level.items].index("color_GREEN")
    a = radial.slice_angle(j, len(sub.level.items))
    ss.aim_loc[1] = sub.matrix @ Vector((sub.center.x + 0.035 * math.cos(a), sub.center.y + 0.035 * math.sin(a), 0.0))
    tick(op, ctx)
    check(sub.hovered().id == "color_GREEN", "pointing at Green in it")
    trig = op_harness(operators.VRSCULPT_OT_stroke)
    xr = FakeXR(actions.ACTION_STROKE, 0, ss.aim_loc[0])
    ss.values[(actions.ACTION_STROKE, 0)] = 1.0
    r = trig.invoke(ctx, FakeEvent('XR_ACTION', 'PRESS', xr))
    check(isinstance(trig.handler, interactions.PieConfirm) and s.draw_color == 'GREEN',
          "the trigger (either hand) chooses in the open pie, instead of drawing")
    ss.values[(actions.ACTION_STROKE, 0)] = 0.0
    r = tick(trig, ctx)
    check(r == {'FINISHED'}, "choosing with the trigger is an undo step")
    check(tick(op, ctx) == {'CANCELLED'} and radial.pie is None and not ctx.window_manager.timers,
          "the pie's operator ends once it is closed")

    # A / X: object and time pies; X tap plays.
    s.mode = 'SCULPT'
    X = (actions.ACTION_PIE_ALT, 0)
    ss.values[X] = 1.0
    op = op_harness(operators.VRSCULPT_OT_pie)
    op.invoke(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR(*X, ss.aim_loc[0])))
    check(radial.pie.kind == 'TIME' and radial.busy(0), "X opens the time pie at the left hand")
    bpy.context.scene.frame_current = 10
    ss.aim_rot[0] = twist_q(-30)
    tick(op, ctx)
    check(bpy.context.scene.frame_current < 10, f"twisting left in the time pie scrubs back "
                                                f"({bpy.context.scene.frame_current})")
    ss.aim_rot[0] = Quaternion()
    ss.values[X] = 0.0
    tick(op, ctx)

    # Knob: hold the right stick click and twist.
    K = (actions.ACTION_KNOB, 1)
    s.radius = 0.04
    ss.values[K] = 1.0
    op = op_harness(operators.VRSCULPT_OT_knob)
    op.invoke(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR(*K, ss.aim_loc[1])))
    check(radial.busy(1), "a held knob keeps the stick from navigating")
    ss.aim_rot[1] = twist_q(-90)
    tick(op, ctx)
    check(abs(s.radius - 0.02) < 1e-6, f"knob: a quarter turn left halves the radius ({s.radius:.3f})")
    ss.values[K] = 0.0
    ss.aim_rot[1] = Quaternion()
    r = tick(op, ctx)
    check(r == {'FINISHED'} and not radial.busy(1), "knob: let go keeps it")
    # Left knob: strength.
    s.strength = 0.5
    K = (actions.ACTION_KNOB, 0)
    ss.values[K] = 1.0
    op = op_harness(operators.VRSCULPT_OT_knob)
    op.invoke(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR(*K, ss.aim_loc[0])))
    ss.aim_rot[0] = twist_q(45)
    tick(op, ctx)
    ss.values[K] = 0.0
    tick(op, ctx)
    ss.aim_rot[0] = Quaternion()
    check(abs(s.strength - 0.75) < 1e-6, f"left knob turns the strength ({s.strength:.2f})")
    # Tap: R steps the radius.
    s.radius = 0.04
    ss.values[(actions.ACTION_KNOB, 1)] = 1.0
    op = op_harness(operators.VRSCULPT_OT_knob)
    op.invoke(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR(actions.ACTION_KNOB, 1, ss.aim_loc[1])))
    ss.values[(actions.ACTION_KNOB, 1)] = 0.0
    tick(op, ctx)
    check(abs(s.radius - 0.08) < 1e-6, "right stick click (tap) steps the radius, as before")

    # Navigation: the stick reads back from the fly actions, and doesn't move you while a pie holds it.
    ss.values[("fly_turnright", 1)] = 0.8
    ss.values[("fly_down", 1)] = 0.4
    x, y = navigation.stick(ctx, ss, 1)
    check(abs(x - 0.8) < 1e-6 and abs(y + 0.4) < 1e-6, f"stick() reads the right stick ({x:.2f}, {y:.2f})")
    ss.values.clear()
    radial.knobs[0] = object()
    nav = op_harness(navigation.VRSCULPT_OT_navigate)
    ev = FakeEvent('XR_ACTION', 'PRESS', FakeXR("fly_forward", 0, ss.aim_loc[0]))
    check(nav.invoke(ctx, ev) == {'CANCELLED'}, "the left stick doesn't walk while its knob is held")
    radial.knobs.clear()
    radial.reset()


def test_drawing():
    print("== drawing helpers (no GPU in background: geometry only)")
    from vr_sculpt import icons
    missing = [n for n, v in icons.ICONS.items() if not icons.triangles(n)]
    check(not missing, f"every icon tessellates ({missing})")
    used = set()
    ctx = bpy.context
    for mode in ('SCULPT', 'MODEL', 'DRAW', 'PAINT', 'POSE', 'TOOLS'):
        ctx.scene.vr_sculpt.mode = mode
        for build in (menus.tool_level, menus.object_level, menus.time_level, menus.mode_level):
            used |= {i.icon for i in radial.walk(build(ctx)) if i.icon and i.swatch is None}
    ctx.scene.vr_sculpt.mode = 'SCULPT'
    unknown = sorted(n for n in used if not icons.exists(n) and not n.startswith(("opt_", "param_", "cat_")))
    check(len(unknown) <= 3, f"slices have icons (label fallback for: {unknown})")
    check(len(radial_draw._sector(0, 0, 0.02, 0.06, 0.0, 1.0)) % 3 == 0, "wedges are triangles")


def main():
    vr_sculpt.register()
    try:
        test_math()
        test_dials()
        test_pie()
        test_sticky()
        test_scrub()
        test_contents()
        test_operators()
        test_drawing()
    except Exception:
        traceback.print_exc()
        failures.append("exception")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "FAILED " + str(failures) if failures else "ALL PASSED")


if __name__ == "__main__":
    main()
