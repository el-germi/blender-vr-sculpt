"""Background tests for the hand panel itself: progressive disclosure, second levels, the reveal rule
(presence.py), steppers, slider detents, hover ticks, layout and icons.

blender --background --factory-startup --python tests/test_ui.py
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
from vr_sculpt import (actions, gfx, hud, interactions, menus, operators, presence, primitives,  # noqa: E402
                       vr_ui)

sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_model as tm  # noqa: E402  (fakes and the press() helper)

failures = []
MODES = ('SCULPT', 'MODEL', 'DRAW', 'PAINT', 'RIG', 'POSE', 'PHYSICS', 'TOOLS')


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


class SS(tm.FakeSessionState):
    """The fake session, with a head rotation and a log of haptic pulses: (hand path, amplitude)."""

    def __init__(self):
        super().__init__()
        self.viewer_pose_rotation = Quaternion()
        self.pulses = []

    def haptic_action_apply(self, context, actionmap, action, user_path, duration, frequency, amplitude):
        self.pulses.append((user_path, amplitude))
        return True


def ids(context=None):
    return {w.id for w in menus.layout(context or bpy.context)[0] if w.id}


def content_rows(context=None):
    return menus.panel(context or bpy.context).rows


def select_only(obj):
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    if obj is not None:
        primitives.make_active(bpy.context, obj)


# --- progressive disclosure ------------------------------------------------------------

def test_disclosure():
    print("== progressive disclosure")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    menus.reset()
    box = primitives.create(ctx, 'CUBE', (0, 0, 0))
    from vr_sculpt import paint
    paint.setup(ctx, box, 1024)  # So Paint shows its painting controls.
    for mode in MODES:
        menus.set_mode(ctx, mode)
        select_only(box)
        rows = content_rows()
        kinds = [{w.kind for w in row} for row in rows]
        has_more = 'more' in kinds[-1]
        primary = len(rows) - 1 - (1 if has_more else 0)  # Not the header, not the More row.
        check(2 <= primary <= 5, f"{mode}: {primary} controls up front (header and More aside)")
        check('title' in kinds[0] and {"settings", "pin"} <= {w.id for w in rows[0]},
              f"{mode}: the header has the title, Settings and Pin")
        if mode == 'TOOLS':
            check(not has_more, "Tools: nothing hidden")
            continue
        check(has_more, f"{mode}: a More row at the bottom")
        before = ids()
        vr_ui.find(menus.layout(ctx)[0], "more").on_press()
        after = ids()
        check(menus.more_open(mode) and len(after) > len(before), f"{mode}: More shows {len(after - before)} more")
        more = vr_ui.find(menus.layout(ctx)[0], "more")
        check(more.text == "Less" and more.active, f"{mode}: and turns into Less")
        more.on_press()
        check(ids() == before, f"{mode}: Less hides them again")

    # The More row stays where it was (the rows open upward), and so do the mode icons.
    menus.set_mode(ctx, 'PAINT')
    select_only(box)
    widgets, h1 = menus.layout(ctx)
    more1 = vr_ui.find(widgets, "more").rect
    rail1 = [w.rect for w in widgets if w.kind == 'rail']
    menus.set_more('PAINT', True)
    widgets, h2 = menus.layout(ctx)
    more2 = vr_ui.find(widgets, "more").rect
    rail2 = [w.rect for w in widgets if w.kind == 'rail']
    check(h2 > h1 and abs(more1[1] - more2[1]) < 1e-9, f"Paint: the panel grows ({h1:.3f} -> {h2:.3f} m) and "
          "More stays under the pointer")
    check(rail1 == rail2, "the mode icons don't move")

    # Each mode keeps its own More.
    menus.set_mode(ctx, 'DRAW')
    check(not menus.more_open('DRAW') and menus.more_open('PAINT'), "Draw's More is its own (Paint's stays open)")
    menus.reset()
    check(not menus.more_open('PAINT'), "reset() closes them all")

    # Pose: a puppet in use brings its controls up front.
    from vr_sculpt import puppet
    menus.set_mode(ctx, 'POSE')
    check("puppet_rec" not in ids(), "Pose: the puppet's Rec is under More")
    real = puppet.binding_count
    puppet.binding_count = lambda: 1
    try:
        check({"puppet_rec", "unbind", "bind_left"} <= ids(), "with a binding, Bind / Unbind / Rec come up front")
    finally:
        puppet.binding_count = real
    menus.set_mode(ctx, 'SCULPT')
    bpy.data.objects.remove(box)
    s.mode = 'SCULPT'


# --- second levels --------------------------------------------------------------------

def test_sheets():
    print("== second levels")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    menus.reset()
    for mode in MODES:
        menus.set_mode(ctx, mode)
        vr_ui.find(menus.layout(ctx)[0], "settings").on_press()
        check(menus.sheet() == 'SETTINGS' and "nav_walk" in ids(), f"{mode}: the gear opens Settings")
        vr_ui.find(menus.layout(ctx)[0], "mode_" + mode.lower()).on_press()
        check(menus.sheet() is None and s.mode == mode, f"{mode}: its own rail icon goes back to it")
    vr_ui.find(menus.layout(ctx)[0], "settings").on_press()
    vr_ui.find(menus.layout(ctx)[0], "mode_draw").on_press()
    check(menus.sheet() is None and s.mode == 'DRAW', "another rail icon switches mode and leaves Settings")

    menus.set_mode(ctx, 'MODEL')
    box = primitives.create(ctx, 'CUBE', (0, 0, 0))
    vr_ui.find(menus.layout(ctx)[0], "edit_object").on_press()
    s.model_page = 'FINISH'
    s.model_mark = 'HARD'
    check(menus.sheet() == 'OBJECT' and "page_finish" in ids(), "the selection chip opens the object's settings")
    vr_ui.find(menus.layout(ctx)[0], "back").on_press()
    check(menus.sheet() is None and s.model_mark == 'NONE', "Back closes it and turns the edge Mark tool off")
    vr_ui.find(menus.layout(ctx)[0], "edit_object").on_press()
    box.select_set(False)
    check("edit_object" not in ids() and menus.sheet() is None and "add" in ids(),
          "deselecting the object goes back to Model")
    select_only(box)
    vr_ui.find(menus.layout(ctx)[0], "edit_object").on_press()
    vr_ui.find(menus.layout(ctx)[0], "delete").on_press()
    check("add" in ids() and menus.sheet() is None, "Delete in the sheet goes back to Model")
    op = type("H", (), {"execute": operators.VRSCULPT_OT_cycle_brush.execute, "off_hand": True,
                        "report": lambda *a: None})()
    vr_ui.find(menus.layout(ctx)[0], "settings").on_press()
    op.execute(ctx)
    check(s.mode == 'DRAW' and menus.sheet() is None, "Y (next mode) leaves a sheet too")

    # Pickers open in place of the content, from a select, and Back closes them.
    menus.set_mode(ctx, 'TOOLS')
    s.tool = 'CAMERA'
    vr_ui.find(menus.layout(ctx)[0], "camera").on_press()
    check(menus.picker_open() and "picker_back" in ids(), "a select opens its picker in the panel")
    vr_ui.find(menus.layout(ctx)[0], "picker_back").on_press()
    check(not menus.picker_open() and "camera" in ids(), "Back closes it")
    s.tool = 'MEASURE'
    menus.set_mode(ctx, 'SCULPT')


# --- widgets -------------------------------------------------------------------------

def test_layout():
    print("== layout of every panel")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    menus.reset()
    box = primitives.create(ctx, 'CUBE', (0, 0, 0))
    states = []
    for mode in MODES:
        for expanded in (False, True):
            states.append((mode, expanded, None))
    for page in ('SHAPE', 'BOOLEAN', 'FINISH', 'ARRAY', 'STACK'):
        states.append(('MODEL', True, page))
    states.append(('TOOLS', False, 'SETTINGS'))
    rail = None
    used_icons = set()
    for mode, expanded, sheet in states:
        menus.reset()
        menus.set_mode(ctx, mode)
        select_only(box)
        if sheet == 'SETTINGS':
            menus.open_sheet('SETTINGS')
        elif sheet:
            s.model_page = sheet
            menus.open_sheet('OBJECT')
        for key in (mode, 'OBJECT_' + (sheet or "")):
            menus.set_more(key, expanded)
        widgets, height = menus.layout(ctx)
        name = f"{mode}{' ' + sheet if sheet else ''}{' + More' if expanded else ''}"
        inside = all(-vr_ui.WIDTH / 2 - 1e-9 <= w.rect[0] <= w.rect[2] <= vr_ui.WIDTH / 2 + 1e-9 and
                     -1e-9 <= w.rect[1] <= w.rect[3] <= height + 1e-9 for w in widgets)
        overlap = any(a is not b and a.rect[0] < b.rect[2] - 1e-9 and b.rect[0] < a.rect[2] - 1e-9 and
                      a.rect[1] < b.rect[3] - 1e-9 and b.rect[1] < a.rect[3] - 1e-9
                      for a in widgets for b in widgets)
        check(inside and not overlap and height < 0.32, f"{name}: {len(widgets)} widgets fit, none overlap, "
              f"{height:.3f} m tall")
        r = [w.rect for w in widgets if w.kind == 'rail']
        rail = rail or r
        if r != rail:
            check(False, f"{name}: the rail moved")
        used_icons |= {w.icon for w in widgets if w.icon}
    tw, _th = vr_ui.layout(menus.timeline_rows(ctx, s), interactions.TIMELINE_WIDTH)
    used_icons |= {w.icon for w in tw if w.icon}
    for mode in MODES:
        s.mode = mode
        used_icons.add(hud.card_content(s, None)[3])
    s.mode = 'SCULPT'
    missing = used_icons - set(gfx.ICONS)
    check(not missing, f"every icon the panel uses is drawn ({len(used_icons)} used, missing {sorted(missing)})")
    bad = []
    for name in gfx.ICONS:
        cv = gfx.Canvas()
        gfx.icon(cv, name, 0.0, 0.0, 0.01, (1, 1, 1, 1))
        if not cv.pos or any(abs(p[0]) > 0.0075 or abs(p[1]) > 0.0075 for p in cv.pos):
            bad.append(name)
    check(not bad, f"all {len(gfx.ICONS)} icons draw inside their box (bad: {bad})")
    menus.reset()
    bpy.data.objects.remove(box)


def test_stepper_and_detents():
    print("== steppers, slider detents, hover ticks")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    ss = SS()
    menus.reset()
    menus.set_mode(ctx, 'MODEL')
    box = primitives.create(ctx, 'CUBE', (0, 0, 0))
    s.model_page = 'SHAPE'
    menus.open_sheet('OBJECT')
    w = vr_ui.find(menus.layout(ctx)[0], "subdivisions")
    x0, y0, x1, y1 = w.rect
    plus = (x1 - 0.003, (y0 + y1) / 2)
    minus = (x0 + (x1 - x0) * vr_ui.STEPPER_SPLIT + 0.003, (y0 + y1) / 2)
    label = (x0 + 0.003, (y0 + y1) / 2)
    check(w.step_at(plus[0]) == 1 and w.step_at(minus[0]) == -1 and w.step_at(label[0]) == 0,
          "stepper: the right half steps up, the left half down, the label does nothing")
    check(w.stepped(-1) is None, "and it doesn't go below its minimum")
    op = interactions.UIPress(ctx, ss, w, plus)
    check(box.vr_prim.subdivisions == 1 and op.changed, "pressing + steps up")
    pose = Matrix()
    op.update(ctx, pose, pose, 1.0)
    check(box.vr_prim.subdivisions == 1, "holding it doesn't repeat at once")
    op.t0 -= 1.0
    op.update(ctx, pose, pose, 1.0)
    op.update(ctx, pose, pose, 1.0)
    check(box.vr_prim.subdivisions == 2, "after a moment it repeats (once per due step)")
    op.finish(ctx)
    w = vr_ui.find(menus.layout(ctx)[0], "subdivisions")
    op = interactions.UIPress(ctx, ss, w, label)
    check(box.vr_prim.subdivisions == 2 and not op.changed, "pressing its label changes nothing")
    op.finish(ctx)

    # Slider detents: a light tick on the right hand every tenth of the track.
    menus.reset()
    menus.set_mode(ctx, 'SCULPT')
    w = vr_ui.find(menus.layout(ctx)[0], "radius")
    s.radius = 0.005
    w.value = s.radius
    ss.pulses.clear()
    op = interactions.UIPress(ctx, ss, w, (w.rect[0], 0.0))
    for i in range(101):
        x = w.rect[0] + (w.rect[2] - w.rect[0]) * i / 100
        w.value = s.radius
        op._set(ctx, w, x)
    op.finish(ctx)
    ticks = [p for p in ss.pulses if p[0] == actions.HANDS[1]]
    check(9 <= len(ticks) <= 11, f"dragging across the radius slider: {len(ticks)} detent ticks")
    menus.set_mode(ctx, 'POSE')

    # Hover: a tick when the ray moves onto a new widget, none while it stays.
    ss.pulses.clear()
    for hid in ("a", "a", "b", None, "b", "b"):
        interactions.note_hover(ctx, ss, hid)
    check(len(ss.pulses) == 3, f"hover ticks on a, b and b again ({len(ss.pulses)})")
    interactions.note_hover(ctx, ss, None)
    menus.reset()
    menus.set_mode(ctx, 'SCULPT')
    bpy.data.objects.remove(box)


# --- presence: when the panel shows ---------------------------------------------------

def facing_rotation(direction):
    """A left controller rotation whose -X (the back of the hand) points along direction."""
    return Vector((-1.0, 0.0, 0.0)).rotation_difference(Vector(direction).normalized())


def settle(ctx, ss, t0, t1, step=0.02):
    """Run the presence update from t0 to t1 like frames would; returns the alpha."""
    t = t0
    a = presence.alpha()
    while t < t1:
        t = min(t + step, t1)
        a = presence.update(ctx, ss, now=t)
    return a


def test_presence():
    print("== reveal rule (presence.py)")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    ss = SS()
    head = Vector(ss.viewer_pose_location)
    hand = Vector(ss.controller_grip_location_get(ctx, 0))
    to_head = (head - hand).normalized()

    check(presence.wrist_turned(facing_rotation(to_head), hand, head), "back of the hand toward the face: turned")
    check(not presence.wrist_turned(Quaternion(), hand, head), "holding the controller normally: not turned")
    side = to_head.cross(Vector((0, 0, 1))).normalized()
    at62 = Quaternion(side, math.radians(62)) @ to_head
    check(not presence.wrist_turned(facing_rotation(at62), hand, head, False) and
          presence.wrist_turned(facing_rotation(at62), hand, head, True),
          "62 degrees off: stays as it was (hysteresis between 55 and 70)")
    target = hand + Vector((0, 0, 0.12))
    look = (target - head).to_track_quat('-Z', 'Y')
    away = Vector((1, 0, 0)).to_track_quat('-Z', 'Z')
    check(presence.looking_at(head, look, target) and not presence.looking_at(head, away, target),
          "looking at the hand counts, looking away doesn't")

    s.show_panel, s.panel_pin, s.panel_reveal = True, False, 'WRIST'
    presence.reset()
    ss.aim_rot[0] = Quaternion()
    ss.viewer_pose_rotation = look
    a = presence.update(ctx, ss, now=0.0)
    check(not presence._state["shown"] and a == 0.0, "Wrist: looking at the hand without turning it: hidden")
    ss.aim_rot[0] = facing_rotation(to_head)
    ss.pulses.clear()
    presence.update(ctx, ss, now=0.1)
    check(presence._state["shown"], "turning the wrist toward the face shows it")
    check(any(p[0] == actions.HANDS[0] for p in ss.pulses), "with a tap on the left hand")
    a = settle(ctx, ss, 0.1, 1.0)
    check(a > 0.99, f"it fades in ({a:.3f})")
    ss.aim_rot[0] = Quaternion()
    presence.update(ctx, ss, now=1.2)
    check(presence._state["shown"], f"turning back: it lingers ({presence.LINGER} s)")
    a = settle(ctx, ss, 1.2, 3.0)
    check(not presence._state["shown"] and a < 0.01, f"then fades out ({a:.3f})")
    presence.update(ctx, ss, using=True, now=3.1)
    check(presence._state["shown"], "pointing at it (or its ghost) brings it back")
    presence.update(ctx, ss, now=4.0)
    ss.viewer_pose_rotation = away
    ss.aim_rot[0] = facing_rotation(to_head)
    presence.update(ctx, ss, now=5.0)
    check(not presence._state["shown"], "a turned wrist you aren't looking at: hidden")
    s.panel_reveal = 'LOOK'
    ss.aim_rot[0] = Quaternion()
    ss.viewer_pose_rotation = look
    presence.update(ctx, ss, now=6.0)
    check(presence._state["shown"], "Look: looking at the hand is enough")
    s.panel_reveal = 'ALWAYS'
    ss.viewer_pose_rotation = away
    presence.update(ctx, ss, now=7.0)
    check(presence._state["shown"], "Always: always")
    s.panel_reveal = 'WRIST'
    presence.update(ctx, ss, now=8.0)
    presence.update(ctx, ss, now=9.0)
    check(not presence._state["shown"], "back to Wrist, looking away: hidden")
    radius = operators.VRSCULPT_OT_cycle_radius
    op = type("H", (), {"execute": radius.execute, "off_hand": True})()
    op.execute(ctx)
    presence.update(ctx, ss, now=9.1)
    check(s.panel_pin and presence._state["shown"], "left stick click pins it: shown")
    s.show_panel = False
    presence.update(ctx, ss, now=9.2)
    check(not presence._state["shown"], "Show Hand Panel off: never")
    s.show_panel = True
    op.execute(ctx)
    check(not s.panel_pin, "left stick click again unpins it")

    # A hidden panel doesn't take presses: the trigger goes to the scene.
    operators.session_running = lambda context: True
    wm = tm.FakeWM(ss)
    fctx = tm.FakeContext(wm)
    s.tip_offset = 0.0
    menus.reset()
    menus.set_mode(ctx, 'MODEL')
    presence.reset()
    ss.aim_rot[0] = Quaternion()
    ss.viewer_pose_rotation = away
    matrix, widgets, _h = interactions.panel_frame(fctx, ss)
    tab = vr_ui.find(widgets, "mode_draw")
    target = matrix @ Vector(((tab.rect[0] + tab.rect[2]) / 2, (tab.rect[1] + tab.rect[3]) / 2, 0.0))
    normal = (matrix.to_3x3() @ Vector((0, 0, 1))).normalized()
    origin, rot = tm.aim_at(target, normal)
    n = len(bpy.data.objects)
    op, r = tm.press(fctx, ss, 1, [origin], rot)
    check(type(op.handler).__name__ != "UIPress" and s.mode == 'MODEL',
          "hidden: a press where the panel would be isn't a panel press")
    for o in list(bpy.data.objects)[n:]:
        bpy.data.objects.remove(o)
    s.panel_pin = True
    op, r = tm.press(fctx, ss, 1, [origin], rot)
    check(type(op.handler).__name__ == "UIPress" and s.mode == 'DRAW', "pinned: the same press switches to Draw")
    s.panel_pin = False
    presence.reset()
    menus.set_mode(ctx, 'SCULPT')


def test_ghost():
    print("== the hidden panel's ghost")
    m = vr_ui.panel_matrix(Vector((0, 0, 1)), Vector((0, -1, 1)), 1.0)
    center = m @ Vector((0.0, vr_ui.GHOST_Y, 0.0))
    normal = (m.to_3x3() @ Vector((0, 0, 1))).normalized()

    def points_at(dx, dy):
        target = m @ Vector((dx, vr_ui.GHOST_Y + dy, 0.0))
        origin = target + normal * 0.4
        return vr_ui.ghost_hit(m, origin, target - origin)

    check(vr_ui.GHOST_R >= 0.011, f"it's drawn {vr_ui.GHOST_R * 2000:.0f} mm across")
    check(vr_ui.GHOST_HIT > vr_ui.GHOST_R * 2, "and its hit area is about twice that")
    check(points_at(0, 0) and points_at(0.02, 0) and points_at(0, -0.02) and points_at(-0.015, 0.015),
          "pointing within 2 cm of it counts, below it too")
    check(not points_at(0.035, 0) and not points_at(0, -0.035), "3.5 cm away doesn't")
    origin = center + normal * 0.4
    check(not vr_ui.ghost_hit(m, origin, origin - center), "pointing away doesn't")
    check(vr_ui.GHOST_Y - vr_ui.GHOST_R > 0.0, "the disc sits above the hand, not on it")


def test_controller_hints():
    print("== controller button hints")
    from vr_sculpt import controllers
    s = bpy.context.scene.vr_sculpt
    Info = controllers.Info

    def label(hand, button, info=Info()):
        h = controllers.hints(s, info)[hand][button]
        return h[1] if h else None

    s.mode, s.engine, s.brush, s.left_inverts = 'SCULPT', 'PYTHON', 'DRAW', True
    check(label('right', 'trigger') == "Draw" and label('left', 'trigger') == "Draw, inverted",
          "Sculpt: right trigger draws, left inverts")
    s.brush = 'SMOOTH'
    check(label('left', 'trigger') == "Smooth", "a brush that doesn't invert: the same on both hands")
    s.engine = 'NATIVE'
    check(label('right', 'trigger', Info(native_brush="Clay Strips")) == "Clay", "native brushes by short name")
    s.engine, s.brush = 'PYTHON', 'DRAW'
    check(label('right', 'top') == "Next brush" and label('right', 'stick_click') == "Brush size",
          "Sculpt: B next brush, stick click brush size")

    s.mode = 'MODEL'
    check(label('right', 'trigger', Info(primitive="Torus")) == "Add Torus", "Model: the right trigger adds")
    check(label('left', 'trigger', Info(primitive="Torus")) == "Select, move", "Model: the left trigger selects")
    s.model_add_as = 'DIFFERENCE'
    check(label('right', 'trigger', Info(cut_target=True)) == "Cut", "New as Cut, with an object to cut: Cut")
    check(label('right', 'trigger').startswith("Add"), "nothing to cut: still adds")
    s.model_add_as = 'OBJECT'
    s.model_mark = 'HARD'
    check(label('right', 'trigger') == "Mark hard" and label('left', 'trigger') == "Clear marks",
          "edge marks: right marks, left clears")
    s.model_mark = 'NONE'
    check(label('right', 'top') == "Next shape" and label('right', 'stick_click') is None,
          "Model: B next shape; the stick click has nothing to say")

    s.mode, s.draw_tool, s.draw_style = 'DRAW', 'DRAW', 'RIBBON'
    check(label('right', 'trigger') == "Ribbon" and label('right', 'top') == "Undo stroke", "Draw: the style; B undoes")
    s.draw_tool = 'ERASE'
    check(label('right', 'trigger') == "Erase", "Erase tool")
    s.draw_tool, s.draw_style = 'DRAW', 'TUBE'

    s.mode = 'PAINT'
    check(label('right', 'trigger', Info(paint_ready=False)) is None, "Paint before setup: no trigger hint")
    check(label('right', 'trigger') == "Paint" and label('left', 'trigger') == "2nd color", "Paint: left paints 2nd")
    s.paint_reach = 'SPRAY'
    check(label('right', 'trigger') == "Spray", "Spray")
    s.paint_reach, s.paint_tool = 'CONTACT', 'PICK'
    check(label('right', 'trigger') == "Pick color" and label('left', 'trigger') == "Pick 2nd color", "Pick color")
    s.paint_tool = 'PAINT'

    s.mode, s.pose_tool = 'POSE', 'IK'
    check(label('right', 'trigger', Info(has_rig=True)) == "Pose IK" and label('right', 'top') == "FK",
          "Pose: the tool, and B switches to the other")
    check(label('right', 'trigger') == "Move object" and label('right', 'stick_click') == "IK chain",
          "no rig: moves objects; stick click: IK chain")
    s.pose_tool = 'FK'

    s.mode, s.tool = 'TOOLS', 'CAMERA'
    check(label('right', 'trigger') == "Aim camera" and label('right', 'top') == "Next camera", "Tools: camera")
    s.tool = 'MEASURE'
    check(label('right', 'trigger') == "Measure" and label('right', 'top') == "Undo measure", "Tools: measure")

    s.mode = 'SCULPT'
    check(label('right', 'trigger', Info(ray_on_panel=True)) == "Press", "the ray on the panel: Press")
    check(label('right', 'trigger', Info(ray_on_panel=True, picker_open=True)) == "Choose", "in a picker: Choose")
    desk = Info(ray_on_desktop=True)
    check(label('right', 'trigger', desk) == "Click" and label('left', 'trigger', desk) == "Middle click" and
          label('right', 'top', desk) == "Right click" and label('right', 'stick', desk) == "Scroll",
          "on the desktop screen: the mouse")
    names = {'SCULPT': "Model", 'MODEL': "Draw", 'DRAW': "Paint", 'PAINT': "Rig", 'RIG': "Pose", 'POSE': "Physics",
             'PHYSICS': "Tools", 'TOOLS': "Sculpt"}
    icons = set()
    for mode, nxt in names.items():
        s.mode = mode
        for info in (Info(), Info(ray_on_panel=True), desk, Info(has_rig=True, cut_target=True)):
            table = controllers.hints(s, info)
            icons |= {h[0] for hand in table.values() for h in hand.values() if h}
        if label('left', 'top') != "Next: " + nxt:
            check(False, f"{mode}: Y names the next mode")
    check(not (icons - set(gfx.ICONS)), f"every hint icon is drawn (missing {sorted(icons - set(gfx.ICONS))})")
    s.mode = 'SCULPT'
    s.panel_pin = False
    check(label('left', 'stick_click') == "Pin panel", "left stick click: Pin panel")
    s.panel_pin = True
    check(label('left', 'stick_click') == "Unpin panel", "pinned: Unpin panel")
    s.panel_pin = False
    s.nav_mode = 'FLY'
    check(label('left', 'stick') == "Fly" and label('right', 'stick') == "Up, down, turn", "Fly sticks")
    s.nav_mode = 'WALK'
    check(label('left', 'stick') == "Walk" and label('right', 'stick') == "Turn", "Walk sticks")
    check(label('left', 'grip') == "Move world" and label('right', 'bottom') == "Reset view",
          "grips and A / X: Blender's navigation")


class InputSS(SS):
    """Action states by (action, hand path)."""

    def __init__(self, states):
        super().__init__()
        self.states = states

    def action_state_get(self, context, actionmap, action, user_path):
        if (action, user_path) not in self.states:
            raise RuntimeError("no such action")
        return self.states[(action, user_path)]


def test_controller_inputs():
    print("== controller inputs, hint reveal, geometry")
    from vr_sculpt import controllers
    ctx = bpy.context
    L, R = actions.HANDS
    ss = InputSS({(actions.ACTION_STROKE, R): 0.6, ("nav_grab", R): 0.0, (actions.ACTION_BRUSH, R): 1.0,
                  ("nav_reset", R): 0.0, (actions.ACTION_RADIUS, R): 0.0,
                  ("fly_turnright", R): 0.8, ("fly_turnleft", R): 0.0, ("fly_up", R): 0.0, ("fly_down", R): 0.5,
                  ("teleport", L): 0.2, ("fly_forward", L): -0.7, ("fly_back", L): -0.7,
                  ("fly_right", L): 0.25, ("fly_left", L): 0.25})
    r = controllers.read_inputs(ctx, ss, 1)
    check(abs(r.trigger - 0.6) < 1e-9 and r.top and not r.bottom and not r.stick_click,
          "right: trigger value, B pressed")
    check(abs(r.stick_x - 0.8) < 1e-9 and abs(r.stick_y + 0.5) < 1e-9, "stick halves as magnitudes: (0.8, -0.5)")
    lft = controllers.read_inputs(ctx, ss, 0)
    check(abs(lft.trigger - 0.2) < 1e-9, "sculpting off: the stock trigger action is read")
    check(abs(lft.stick_y + 0.7) < 1e-9 and abs(lft.stick_x - 0.25) < 1e-9, "stick halves reading the signed axis")
    check(lft.grip == 0.0, "an action that isn't there reads as released")
    check(controllers.working(r) and not controllers.working(controllers.Inputs(0, 0, False, False, False, 0.1, 0)),
          "a pressed button or tilted stick is work; a resting hand isn't")

    controllers.reset_hints()
    a = 0.0
    t = 0.0
    while t < 1.0:
        t += 0.05
        a = controllers.update_hints(0, (0, 0, 1), 1.0, looking=False, busy=False, now=t)
    check(a < 0.01, "not looking, still for under a second: no hints")
    while t < 2.2:
        t += 0.05
        a = controllers.update_hints(0, (0, 0, 1), 1.0, looking=False, busy=False, now=t)
    check(a > 0.9, f"held still for {controllers.STILL_TIME} s: the hints fade in ({a:.2f})")
    for i in range(10):
        t += 0.05
        a = controllers.update_hints(0, (0, 0.02 * i, 1), 1.0, looking=False, busy=False, now=t)
    check(a < 0.05, "moving the hand: they fade out")
    for i in range(10):
        t += 0.05
        a = controllers.update_hints(0, (0, 0.2, 1), 1.0, looking=True, busy=False, now=t)
    check(a > 0.9, "looking at the hand: they show")
    for i in range(6):
        t += 0.05
        a = controllers.update_hints(0, (0, 0.2, 1), 1.0, looking=True, busy=True, now=t)
    check(a < 0.05, "pressing a button (working): they fade out fast")
    controllers.reset_hints()
    check(controllers.legend_alpha(0, 1.0, 1.0) == 0.0 and abs(controllers.legend_alpha(0, 1.0, 0.25) - 0.75) < 1e-9,
          "the left legend fades out as the hand panel fades in over it")
    check(controllers.legend_alpha(1, 1.0, 1.0) == 1.0, "the right one stays")

    check(controllers.available(), "the controller models are in the add-on")
    rest = controllers.Inputs(0.0, 0.0, False, False, False, 0.0, 0.0)
    for hand in ('left', 'right'):
        mdl = controllers.model(hand)
        far = max(c.length for c in mdl.centers.values())
        check(set(mdl.centers) >= {'body', 'top', 'bottom', 'stick', 'trigger', 'grip'} and far < 0.08,
              f"{hand}: every part, within {far * 100:.1f} cm of the model's origin")
        check(len(mdl.tris['body']) > 1000 and mdl.tex_data.shape[2] == 3, f"{hand}: a real mesh and its texture")
        where = controllers.layout(hand)
        inward = 1 if hand == 'left' else -1
        check(where['top'].x * inward > 0 and where['stick'].x * inward < 0 and where['top'].z < where['bottom'].z,
              f"{hand}: face buttons on the inner side (B / Y ahead of A / X), the stick on the outer")
        check(where['trigger'].y < where['top'].y and where['trigger'].z < where['grip'].z,
              f"{hand}: the trigger under the face, ahead of the grip")

        def moved(name, inputs, point):
            return mdl.part_matrix(name, inputs) @ point - point
        trig = moved('trigger', rest._replace(trigger=1.0), mdl.centers['trigger'])
        check(trig.z > 0.002, f"{hand}: a pulled trigger swings back toward the handle ({trig.z * 1000:.1f} mm)")
        cap = mdl.tops['stick']
        fwd = moved('stick', rest._replace(stick_y=1.0), cap)
        right = moved('stick', rest._replace(stick_x=1.0), cap)
        check(fwd.z < -0.002 and abs(fwd.x) < 0.001 and right.x > 0.002 and abs(right.z) < 0.001,
              f"{hand}: the stick tilts the way it's pushed (forward {fwd.z * 1000:.1f} mm, right "
              f"{right.x * 1000:.1f} mm)")
        press = moved('top', rest._replace(top=True), mdl.centers['top'])
        check(-0.003 < press.y < -0.001, f"{hand}: a pressed button sinks ({press.y * 1000:.1f} mm)")
        check(moved('trigger', rest, mdl.centers['trigger']).length < 1e-9, f"{hand}: at rest nothing moves")
    ss2 = SS()
    m = controllers.controller_matrix(ss2, ctx, 1)
    aim = Vector(ss2.controller_aim_location_get(ctx, 1))
    check(abs((m.translation - aim).length - controllers.MODEL_OFFSET.length) < 1e-6,
          "the model sits on the aim pose (the tip ring and the ray are untouched)")


def _box(center, size, open_bottom=False):
    """(pos, nor, uv, tris) of a box with a vertex per face corner, outward-facing (CCW)."""
    cx, cy, cz = center
    hx, hy, hz = (s / 2 for s in size)
    pos, nor, uv, tris = [], [], [], []
    for axis in range(3):
        for sign in (1.0, -1.0):
            if open_bottom and axis == 1 and sign < 0:
                continue
            n = [0.0, 0.0, 0.0]
            n[axis] = sign
            u_axis, v_axis = (axis + 1) % 3, (axis + 2) % 3  # u x v = +axis
            if sign < 0:
                u_axis, v_axis = v_axis, u_axis  # keep the winding outward
            base = len(pos)
            for du, dv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                p = [0.0, 0.0, 0.0]
                p[axis] = sign
                p[u_axis], p[v_axis] = du, dv
                pos.append((cx + p[0] * hx, cy + p[1] * hy, cz + p[2] * hz))
                nor.append(n)
                uv.append(((du + 1) / 2, (dv + 1) / 2))
            tris += [(base, base + 1, base + 2), (base, base + 2, base + 3)]
    return (np.array(pos, np.float32), np.array(nor, np.float32), np.array(uv, np.float32),
            np.array(tris, np.uint16))


def _glb(parts, png):
    """A .glb of named meshes [(name, (pos, nor, uv, tris), color or None for the png)]."""
    import json
    import struct
    binary, views, accessors, meshes, nodes, materials = b"", [], [], [], [], []

    def add(arr, ctype, kind):
        nonlocal binary
        views.append({"buffer": 0, "byteOffset": len(binary), "byteLength": arr.nbytes})
        binary += arr.tobytes() + b"\0" * (-arr.nbytes % 4)
        accessors.append({"bufferView": len(views) - 1, "componentType": ctype, "count": len(arr), "type": kind})
        return len(accessors) - 1

    views.append({"buffer": 0, "byteOffset": 0, "byteLength": len(png)})
    binary = png + b"\0" * (-len(png) % 4)
    for name, (pos, nor, uv, tris), color in parts:
        mat = {"pbrMetallicRoughness": ({"baseColorTexture": {"index": 0}} if color is None else
                                        {"baseColorFactor": [*color, 1.0]})}
        materials.append(mat)
        prim = {"attributes": {"POSITION": add(pos, 5126, "VEC3"), "NORMAL": add(nor, 5126, "VEC3"),
                               "TEXCOORD_0": add(uv, 5126, "VEC2")},
                "indices": add(tris.ravel(), 5123, "SCALAR"), "material": len(materials) - 1}
        meshes.append({"name": name, "primitives": [prim]})
        nodes.append({"name": name, "mesh": len(meshes) - 1})
    gltf = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": list(range(len(nodes)))}],
            "nodes": nodes, "meshes": meshes, "materials": materials, "accessors": accessors,
            "bufferViews": views, "buffers": [{"byteLength": len(binary)}],
            "images": [{"bufferView": 0, "mimeType": "image/png"}], "textures": [{"source": 0}]}
    js = json.dumps(gltf).encode()
    js += b" " * (-len(js) % 4)
    body = struct.pack("<I4s", len(js), b"JSON") + js + struct.pack("<I4s", len(binary), b"BIN\0") + binary
    return struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body


def _png(folder):
    img = bpy.data.images.new("vrs_test_tex", 2, 2)
    img.pixels.foreach_set(np.array([1, 0, 0, 1] * 4, np.float32))  # all red
    path = os.path.join(folder, "tex.png")
    img.filepath_raw, img.file_format = path, 'PNG'
    img.save()
    bpy.data.images.remove(img)
    with open(path, "rb") as f:
        return f.read()


def test_controller_fetched():
    print("== controller models from the runtime")
    import importlib.util
    import json
    import shutil
    import tempfile
    from vr_sculpt import controllers, ui
    spec = importlib.util.spec_from_file_location("controller_worker", controllers.WORKER)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)

    names = {"body": 'body', "button_y": 'top', "button_b": 'top', "button_x": 'bottom', "button_a": 'bottom',
             "button_enter": 'system', "button_grip": 'grip', "thumbstick": 'stick', "trigger": 'trigger',
             "trackpad": 'body'}
    check(all(worker.part_of(n) == p for n, p in names.items()), "SteamVR's node names map to our parts")
    check(not worker.is_controller(["body"]) and worker.is_controller(["body", "trigger"]),
          "a lone body is the hand tracking placeholder, not a controller")

    tmp = tempfile.mkdtemp(prefix="vrs_ctrl_")
    saved = controllers._cache_dir[0]
    try:
        # A right controller in the model's own frame (y up, the handle toward +z): body, open-bottomed
        # face buttons and stick on top, trigger below the front, grip on the inner (-x) side.
        parts = [("body", _box((0.0, 0.0, 0.05), (0.04, 0.03, 0.1)), None),
                 ("button_b", _box((-0.008, 0.017, 0.03), (0.008, 0.004, 0.008), True), (0.0, 0.0, 1.0)),
                 ("button_a", _box((-0.008, 0.017, 0.045), (0.008, 0.004, 0.008), True), (0.0, 0.0, 1.0)),
                 ("thumbstick", _box((0.01, 0.022, 0.04), (0.01, 0.014, 0.01), True), (0.0, 1.0, 0.0)),
                 ("trigger", _box((0.0, -0.025, 0.02), (0.01, 0.02, 0.006)), (0.0, 1.0, 0.0)),
                 ("button_grip", _box((-0.022, -0.01, 0.07), (0.004, 0.02, 0.02)), (0.0, 1.0, 0.0))]
        glb = _glb(parts, _png(tmp))
        # The model in the aim pose's frame: turned 30 degrees about X and moved, as a runtime might.
        turn = Matrix.Rotation(math.radians(30), 4, 'X')
        model_in_aim = Matrix.Translation((0.0, -0.01, 0.02)) @ turn
        arrays = worker.convert(glb, np.array(model_in_aim), tmp)
        pos, part = arrays["pos"], arrays["part"]
        body_idx = list(worker.PARTS).index('body')
        want = model_in_aim @ Vector((0.0, 0.0, 0.05))
        check((Vector(pos[part == body_idx].mean(axis=0)) - want).length < 1e-5,
              "the mesh lands in the aim frame (moved by where the model sits on the aim pose)")
        check(not arrays["offset"].any() and arrays["tex"].shape == (1024, 1024, 3),
              "offset 0 (already on the aim pose), a 1024 px texture")

        def texel(part_name):
            uv = arrays["uv"][part == list(worker.PARTS).index(part_name)].mean(axis=0)
            h, w = arrays["tex"].shape[:2]
            return arrays["tex"][min(int(uv[1] * h), h - 1), min(int(uv[0] * w), w - 1)]
        check(tuple(texel('body')) == (255, 0, 0) and tuple(texel('top')) == (0, 0, 255) and
              tuple(texel('trigger')) == (0, 255, 0),
              "each part samples its own texture or color in the shared texture (V flipped from glTF)")

        np.savez_compressed(os.path.join(tmp, "right.npz"), **arrays)
        shutil.copy(os.path.join(tmp, "right.npz"), os.path.join(tmp, "left.npz"))
        controllers._cache_dir[0] = tmp
        controllers.clear()
        check(controllers.fetched() and controllers.model_path('right').startswith(tmp),
              "fetched models in the cache are drawn instead of the bundled ones")
        mdl = controllers.model('right')
        check(mdl.offset.length == 0.0 and set(mdl.centers) >= {'body', 'top', 'bottom', 'stick', 'trigger', 'grip'},
              "it loads with every part and no offset")
        rest = controllers.Inputs(0.0, 0.0, False, False, False, 0.0, 0.0)
        up = turn.to_3x3() @ Vector((0, 1, 0))

        def moved(name, inputs, point):
            return mdl.part_matrix(name, inputs) @ point - point
        press = moved('top', rest._replace(top=True), mdl.centers['top'])
        check(abs(press.dot(up) + worker.BUTTON_TRAVEL) < 2e-4 and (press - up * press.dot(up)).length < 2e-4,
              f"a pressed button sinks into the face ({press.dot(up) * 1000:.2f} mm)")
        cap = mdl.tops['stick']
        fwd = moved('stick', rest._replace(stick_y=1.0), cap)
        right = moved('stick', rest._replace(stick_x=1.0), cap)
        check(fwd.z < -0.002 and right.x > 0.002, f"the stick tilts the way it's pushed (forward "
              f"{fwd.z * 1000:.1f} mm, right {right.x * 1000:.1f} mm)")
        click = moved('stick', rest._replace(stick_click=True), cap)
        check(click.dot(up) < -0.001, "a clicked stick sinks along its axis")
        trig = moved('trigger', rest._replace(trigger=1.0), mdl.centers['trigger'])
        check(trig.z > 0.001, f"a pulled trigger swings back ({trig.z * 1000:.1f} mm)")
        grip = moved('grip', rest._replace(grip=1.0), mdl.centers['grip'])
        check(grip.x > 0.001 and abs(grip.y) + abs(grip.z) < 1e-6, "a squeezed grip moves in, toward the body")
        check(moved('trigger', rest, mdl.centers['trigger']).length < 1e-9, "at rest nothing moves")
        ss = SS()
        m = controllers.controller_matrix(ss, bpy.context, 1)
        check((m.translation - Vector(ss.controller_aim_location_get(bpy.context, 1))).length < 1e-9,
              "the fetched model's origin is the aim pose itself")

        # The worker's answer, taken in from the app timer.
        class Done:
            def poll(self):
                return 0
        with open(os.path.join(tmp, "status.json"), "w") as f:
            json.dump({"ok": False, "message": "no controllers seen", "hands": {}}, f)
        controllers._fetch.update(proc=Done(), started=0.0)
        check(controllers.fetching(), "while the worker runs, a fetch is pending")
        controllers.tick(bpy.context, False)
        check(not controllers.fetching() and controllers.fetch_message() == "no controllers seen",
              "its message is shown once it's done")
        real = operators.session_running
        operators.session_running = lambda context: True
        try:
            check(not ui.VRSCULPT_OT_fetch_controllers.poll(bpy.context), "no fetch during a VR session")
            operators.session_running = lambda context: False
            check(ui.VRSCULPT_OT_fetch_controllers.poll(bpy.context), "outside one, it can run")
        finally:
            operators.session_running = real
        controllers.forget_fetched()
        check(not controllers.fetched() and controllers.model('right').offset == controllers.MODEL_OFFSET,
              "forgetting them brings the bundled models back")
    finally:
        controllers._cache_dir[0] = saved
        controllers.clear()
        shutil.rmtree(tmp, ignore_errors=True)


def test_controller_visibility():
    print("== Blender's controllers hidden and restored")
    from vr_sculpt import controllers
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    xs = ctx.window_manager.xr_session_settings
    xs.show_controllers = True
    s.own_controllers = True
    controllers.on_session_start(ctx)
    check(not xs.show_controllers, "session start: Blender's controllers are hidden")
    controllers.tick(ctx, True)
    check(not xs.show_controllers, "and stay hidden while it runs")
    controllers.tick(ctx, False)
    check(xs.show_controllers, "session over: back as they were")
    xs.show_controllers = False
    controllers.on_session_start(ctx)
    controllers.tick(ctx, False)
    check(not xs.show_controllers, "a user who had them off: still off afterwards")
    xs.show_controllers = True
    s.own_controllers = False
    controllers.on_session_start(ctx)
    check(xs.show_controllers, "Controller Models off: Blender's stay")
    real = operators.session_running
    operators.session_running = lambda context: True
    try:
        s.own_controllers = True
        check(not xs.show_controllers, "switching it on mid-session hides Blender's")
        s.own_controllers = False
        check(xs.show_controllers, "and off again brings them back")
        s.own_controllers = True
        controllers.unregister()
        check(xs.show_controllers, "turning the add-on off puts them back too")
    finally:
        operators.session_running = real
    vr_ui.find(menus.layout(ctx)[0], "settings").on_press()
    widgets = menus.layout(ctx)[0]
    check(vr_ui.find(widgets, "controllers_blender") is not None and vr_ui.find(widgets, "controller_hints"),
          "Settings: Hands Quest / Blender and Hints")
    vr_ui.find(widgets, "controllers_blender").on_press()
    check(not s.own_controllers, "Blender's from the panel")
    s.own_controllers = True
    menus.reset()


def test_status():
    print("== tip status")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    for mode in MODES:
        s.mode = mode
        title, desc, color, icon = hud.card_content(s, None)
        check(title and len(color) >= 3 and icon in gfx.ICONS, f"{mode}: '{title}' with the {icon} icon")
    s.mode = 'SCULPT'
    s.radius = 0.04
    check(hud.status_size(ctx, s) == "4.0 cm", "Sculpt shows the radius")
    s.mode = 'POSE'
    s.pose_tool = 'IK'
    s.pose_chain = 2
    check(hud.status_size(ctx, s) == "chain 2", "Pose IK shows the chain length")
    s.pose_tool = 'FK'
    s.mode = 'SCULPT'


def test_dyntopo():
    print("== dyntopo (native engine)")
    from vr_sculpt import native
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    sculpt = ctx.tool_settings.sculpt
    menus.reset()
    bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8)  # Comes with a UV map.
    ball = ctx.active_object
    s.engine = 'PYTHON'
    menus.set_mode(ctx, 'SCULPT')
    menus.set_more('SCULPT', True)
    check("dyntopo" not in ids(), "Python engine: no dyntopo row")
    menus.set_engine(ctx, 'NATIVE')
    check(ball.mode == 'SCULPT' and "dyntopo" in ids() and "detail_brush" not in ids(),
          "native: a Dyntopo toggle under More, its settings hidden while it's off")
    sculpt.detail_type_method = 'RELATIVE'
    vr_ui.find(menus.layout(ctx)[0], "dyntopo").on_press()
    check(ball.use_dynamic_topology_sculpting and sculpt.detail_type_method == 'BRUSH',
          "one press turns it on (the UVs are kept), with detail relative to the brush instead of the desktop view")
    check(content_rows()[0][0].value_text == "Native, dyntopo", "the header says dyntopo is on")
    got = ids()
    check({"detail_brush", "detail_constant", "detail_manual", "detail_size", "refine_subdivide_collapse"} <= got
          and "detail_fill" not in got, "on: detail and refine rows; no Fill with brush detail")
    vr_ui.find(menus.layout(ctx)[0], "detail_size").on_change(0.5)
    check(abs(sculpt.detail_percent - 50.0) < 1e-4, "the slider sets the brush percentage")
    vr_ui.find(menus.layout(ctx)[0], "refine_collapse").on_press()
    check(sculpt.detail_refine_method == 'COLLAPSE', "Refine sets Blender's refine method")
    vr_ui.find(menus.layout(ctx)[0], "detail_constant").on_press()
    slider = vr_ui.find(menus.layout(ctx)[0], "detail_size")
    slider.on_change(0.01)
    check(sculpt.detail_type_method == 'CONSTANT' and abs(sculpt.constant_detail_resolution - 100.0) < 1e-3
          and slider.fmt(0.01) == "1.0 cm" and "detail_fill" in ids(),
          "Constant: the slider is the edge length (1 cm = resolution 100), with Fill beside it")
    vr_ui.find(menus.layout(ctx)[0], "detail_fill").on_press()
    check("desktop" in native.message(), "Fill without a window says so (detail_flood_fill would crash)")
    vr_ui.find(menus.layout(ctx)[0], "detail_manual").on_press()
    check("refine_collapse" not in ids() and "detail_fill" in ids(), "Manual: Fill, and no refine row")
    sculpt.detail_type_method = 'BRUSH'  # The most rows.
    widgets, height = menus.layout(ctx)
    overlap = any(a is not b and a.rect[0] < b.rect[2] - 1e-9 and b.rect[0] < a.rect[2] - 1e-9 and
                  a.rect[1] < b.rect[3] - 1e-9 and b.rect[1] < a.rect[3] - 1e-9 for a in widgets for b in widgets)
    check(not overlap and height < 0.32, f"the panel fits with every dyntopo row open ({height:.3f} m)")
    vr_ui.find(menus.layout(ctx)[0], "dyntopo").on_press()
    check(not ball.use_dynamic_topology_sculpting and "detail_brush" not in ids(), "one press turns it off")

    # Multires is refused.
    menus.set_object_mode(ctx, ball, 'OBJECT')
    ball.modifiers.new("Multires", 'MULTIRES')
    menus.set_object_mode(ctx, ball, 'SCULPT')
    native.toggle_dyntopo(ctx)
    check(not ball.use_dynamic_topology_sculpting and "Multires" in native.message(), "Multires: refused")
    menus.set_object_mode(ctx, ball, 'OBJECT')
    me = ball.data
    bpy.data.objects.remove(ball)
    bpy.data.meshes.remove(me)
    s.engine = 'PYTHON'
    menus.reset()
    s.mode = 'SCULPT'


def test_left_handed():
    print("== left handed: the hands swap")
    from vr_sculpt import controllers, navigation
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    s.mode, s.engine, s.brush, s.left_inverts = 'SCULPT', 'PYTHON', 'DRAW', True
    right_handed = controllers.hints(s, controllers.Info())
    s.left_handed = True
    try:
        check(actions.main_index() == 0 and actions.off_index() == 1, "the left controller is the main hand")
        check(actions.is_off_hand(actions.HANDS[1]) and not actions.is_off_hand(actions.HANDS[0]),
              "the right one is the off hand")
        check(actions.by_role(("L", "R")) == ("R", "L"), "by_role: (off hand's, main hand's)")

        lefty = controllers.hints(s, controllers.Info())
        check(lefty['left'] == right_handed['right'] and lefty['right'] == right_handed['left'],
              "the button hints swap controllers")
        check(lefty['left']['trigger'][1] == "Draw" and lefty['right']['trigger'][1] == "Draw, inverted",
              "Sculpt: the left trigger draws, the right one inverts")
        check(controllers.legend_alpha(1, 1.0, 1.0, actions.off_index()) == 0.0 and
              controllers.legend_alpha(0, 1.0, 1.0, actions.off_index()) == 1.0,
              "the right legend gives way to the panel")

        check(set(navigation.SWAPPED) == set(navigation.ACTIONS) and
              all(navigation.SWAPPED[b] == a for a, b in navigation.SWAPPED.items()) and
              all((navigation.ACTIONS[a][0] == 'MOVE') != (navigation.ACTIONS[b][0] == 'MOVE')
                  for a, b in navigation.SWAPPED.items()),
              "sticks: every fly action reads as the other stick's (move <-> lift / turn)")
        check(navigation.ACTIONS[navigation.SWAPPED["fly_forward"]][0] == 'LIFT',
              "the left stick goes up / down and turns, the right one moves")

        # The panel sits over the right hand, the right wrist reveals it, the left controller presses it.
        ss = SS()
        head = Vector(ss.viewer_pose_location)
        hand = Vector(ss.controller_grip_location_get(ctx, 1))
        to_head = (head - hand).normalized()
        back = Vector((1.0, 0.0, 0.0)).rotation_difference(to_head)
        check(presence.wrist_turned(back, hand, head, side=1.0) and not presence.wrist_turned(back, hand, head),
              "the back of a right hand is its controller's +X")
        operators.session_running = lambda context: True
        fctx = tm.FakeContext(tm.FakeWM(ss))
        matrix, widgets, _h = interactions.panel_frame(fctx, ss)
        check(matrix.translation.x > 0.2, "the panel is above the right controller")
        s.show_panel, s.panel_pin, s.panel_reveal, s.tip_offset = True, False, 'WRIST', 0.0
        presence.reset()
        target = hand + Vector((0, 0, 0.12))
        ss.viewer_pose_rotation = (target - head).to_track_quat('-Z', 'Y')
        ss.aim_rot[1] = back
        presence.update(fctx, ss, now=0.0)
        check(presence._state["shown"] and any(p[0] == actions.HANDS[1] for p in ss.pulses),
              "turning the right wrist shows it, with a tap on the right hand")

        menus.reset()
        menus.set_mode(ctx, 'MODEL')
        s.panel_pin = True
        matrix, widgets, _h = interactions.panel_frame(fctx, ss)
        tab = vr_ui.find(widgets, "mode_draw")
        target = matrix @ Vector(((tab.rect[0] + tab.rect[2]) / 2, (tab.rect[1] + tab.rect[3]) / 2, 0.0))
        normal = (matrix.to_3x3() @ Vector((0, 0, 1))).normalized()
        origin, rot = tm.aim_at(target, normal)
        n = len(bpy.data.objects)
        op, r = tm.press(fctx, ss, 1, [origin], rot)
        check(type(op.handler).__name__ != "UIPress" and s.mode == 'MODEL', "the right trigger doesn't press the panel")
        for o in list(bpy.data.objects)[n:]:
            bpy.data.objects.remove(o)
        op, r = tm.press(fctx, ss, 0, [origin], rot)
        check(type(op.handler).__name__ == "UIPress" and s.mode == 'DRAW', "the left one does")

        # Face buttons and stick clicks follow the hands too.
        brush = operators.VRSCULPT_OT_cycle_brush
        op = type("H", (), {"execute": brush.execute, "invoke": brush.invoke, "off_hand": False,
                            "report": lambda *a: None})()
        op.invoke(fctx, tm.FakeEvent('XR_ACTION', 'PRESS', tm.FakeXR((0, 0, 0), hand=actions.HANDS[1])))
        check(op.off_hand and s.mode == 'PAINT', "the right top button (B) switches mode")
        radius = operators.VRSCULPT_OT_cycle_radius
        op = type("H", (), {"execute": radius.execute, "invoke": radius.invoke, "off_hand": False})()
        op.invoke(fctx, tm.FakeEvent('XR_ACTION', 'PRESS', tm.FakeXR((0, 0, 0), hand=actions.HANDS[1])))
        check(op.off_hand and not s.panel_pin, "the right stick click unpins the panel")
    finally:
        s.left_handed = False
        s.panel_pin = False
        presence.reset()
        menus.set_mode(ctx, 'SCULPT')
    check(actions.main_index() == 1, "off again: the right hand is the main one")


def main():
    vr_sculpt.register()
    try:
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        for t in (test_disclosure, test_sheets, test_layout, test_stepper_and_detents, test_presence, test_ghost,
                  test_controller_hints, test_controller_inputs, test_controller_fetched, test_controller_visibility,
                  test_status,
                  test_dyntopo, test_left_handed):
            try:
                t()
            except Exception:
                traceback.print_exc()
                failures.append(t.__name__ + " raised")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")


main()
