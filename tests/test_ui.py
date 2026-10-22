"""Background tests for the hand panel itself: progressive disclosure, second levels, the reveal rule
(presence.py), steppers, slider detents, hover ticks, layout and icons.

blender --background --factory-startup --python tests/test_ui.py
"""

import math
import os
import sys
import traceback

import bpy
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import vr_sculpt  # noqa: E402
from vr_sculpt import (actions, gfx, hud, interactions, menus, operators, presence, primitives,  # noqa: E402
                       vr_ui)

sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_model as tm  # noqa: E402  (fakes and the press() helper)

failures = []
MODES = ('SCULPT', 'MODEL', 'DRAW', 'PAINT', 'POSE', 'TOOLS')


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
    op = type("H", (), {"execute": operators.VRSCULPT_OT_cycle_brush.execute, "left_hand": True,
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
    for page in ('SHAPE', 'BOOLEAN', 'FINISH', 'ARRAY'):
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
    op = type("H", (), {"execute": radius.execute, "left_hand": True})()
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


def main():
    vr_sculpt.register()
    try:
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        for t in (test_disclosure, test_sheets, test_layout, test_stepper_and_detents, test_presence, test_status):
            try:
                t()
            except Exception:
                traceback.print_exc()
                failures.append(t.__name__ + " raised")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")


main()
