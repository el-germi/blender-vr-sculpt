"""Background tests for Rig mode: building bone chains with trigger presses (roots, connected bones,
branching from a joint, moving joints, ending chains, deleting, the panel's undo, Mirror X), binding a
mesh with automatic weights, painting weights (add, subtract with the left hand, smooth, falloff,
mirror, B's undo, picking a bone by touch), mode switching, the hand panel pages, hints and status,
and the trigger operator's undo steps.

blender --background --factory-startup --python tests/test_rig.py
"""

import os
import sys
import traceback

import bpy
import numpy as np
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import vr_sculpt  # noqa: E402
from vr_sculpt import controllers, gfx, hud, menus, operators, rig, vr_ui  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_model as tm  # noqa: E402  (the trigger operator harness)

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def near(a, b, tol=1e-4):
    return (Vector(a) - Vector(b)).length < tol


def at(point):
    return Matrix.LocRotScale(Vector(point), Quaternion(), None)


def press(ctx, point, left=False, path=()):
    """A trigger press at point, moved through path, released. Returns the handler (or None)."""
    s = ctx.scene.vr_sculpt
    handler = rig.pick(ctx, s, left, at(point), 1.0)
    if handler is None:
        return None
    for p in path:
        handler.update(ctx, at(p), at(p), 1.0)
    handler.finish(ctx)
    return handler


def ids():
    return {w.id for w in menus.layout(bpy.context)[0] if w.id}


def widget(wid):
    return vr_ui.find(menus.layout(bpy.context)[0], wid)


def edit_bones(arm):
    return arm.data.edit_bones


def world(arm, v):
    return arm.matrix_world @ Vector(v)


def make_body(ctx):
    """A closed tube along Z from 0.4 to 1.6, 0.15 m in radius, with rings every 5 cm (for weights)."""
    bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=0.15, depth=1.2, location=(0.0, 0.0, 1.0))
    body = ctx.active_object
    body.name = "Body"
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.subdivide(number_cuts=23)
    bpy.ops.object.mode_set(mode='OBJECT')
    return body


# --- bones -------------------------------------------------------------------------

def test_chain(body):
    print("== building a chain")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    menus.set_mode(ctx, 'RIG')
    check(s.mode == 'RIG' and s.rig_mesh == body and rig.target(ctx) is None,
          "Rig mode with a mesh and no rig: the mesh is the one to bind, no rig yet")
    h = press(ctx, (0.0, 0.0, 0.5))
    check(isinstance(h, rig.PlaceBone) and not h.changed and not rig.rigs(ctx),
          "a click in empty space only sets the root joint (no rig, no undo step)")
    check(rig.anchor_world(None) is not None and near(rig.anchor_world(None), (0, 0, 0.5)), "the rubber band starts there")
    h = press(ctx, (0.0, 0.0, 0.9))
    arm = rig.target(ctx)
    check(h.changed and arm is not None and arm.mode == 'EDIT' and ctx.view_layer.objects.active == arm,
          "the next press makes a rig, active and in Edit Mode")
    check(near(arm.matrix_world.translation, (0, 0, 0.5)), "the rig's origin is its first joint")
    eb = edit_bones(arm)
    check(len(eb) == 1 and near(world(arm, eb[0].head), (0, 0, 0.5)) and near(world(arm, eb[0].tail), (0, 0, 0.9)),
          "a bone from the root to the press")
    first = eb[0].name
    h = press(ctx, (0.0, 0.0, 1.2))
    eb = edit_bones(arm)
    second = h.bone
    check(len(eb) == 2 and eb[second].parent == eb[first] and eb[second].use_connect,
          "the next press: a connected child from the last joint")
    h = press(ctx, (0.0, 0.0, 1.35), path=[(0.0, 0.05, 1.45), (0.0, 0.0, 1.5)])
    third = h.bone
    check(near(world(arm, edit_bones(arm)[third].tail), (0, 0, 1.5)), "holding: the tail follows the tip to release")
    check(rig.active_bone(arm) == third, "the new bone is the active one")
    h = press(ctx, (0.0, 0.0, 1.503))
    check(h is not None and not h.changed and len(edit_bones(arm)) == 3,
          "a press on the last joint carries on from it, no bone")
    check(near(rig.anchor_world(arm), (0, 0, 1.5)), "still anchored at the last joint")
    return arm, first, second, third


def test_branch_and_move(arm, first, second, third):
    print("== branching, moving joints")
    ctx = bpy.context
    h = press(ctx, (0.004, 0.0, 0.9))  # The joint between the first and second bones.
    check(isinstance(h, rig.JointPress) and not h.changed, "a click on a joint selects it")
    check(near(rig.anchor_world(arm), (0, 0, 0.9)), "and the chain carries on from there")
    h = press(ctx, (0.3, 0.0, 0.9))
    eb = edit_bones(arm)
    branch = h.bone
    check(eb[branch].parent == eb[first] and eb[branch].use_connect and len(eb[first].children) == 2,
          "the next bone branches off the first one")
    # Drag the joint between the second and third bones: both ends there follow.
    h = rig.pick(ctx, ctx.scene.vr_sculpt, False, at((0.0, 0.0, 1.2)), 1.0)
    check(isinstance(h, rig.JointPress), "press on an inner joint")
    for p in ((0.0, 0.0, 1.2), (0.05, 0.0, 1.2), (0.1, 0.0, 1.2)):
        h.update(ctx, at(p), at(p), 1.0)
    h.finish(ctx)
    eb = edit_bones(arm)
    check(h.changed and near(world(arm, eb[second].tail), (0.1, 0, 1.2), 1e-3) and
          near(world(arm, eb[third].head), (0.1, 0, 1.2), 1e-3), "dragging it moves the tail and the child's head")
    check(near(rig.anchor_world(arm), (0.3, 0.0, 0.9)), "a drag doesn't move the anchor")
    # A root's head moves alone.
    h = rig.pick(ctx, ctx.scene.vr_sculpt, False, at((0.0, 0.0, 0.5)), 1.0)
    for p in ((0.0, 0.0, 0.5), (0.0, 0.0, 0.45)):
        h.update(ctx, at(p), at(p), 1.0)
    h.finish(ctx)
    check(near(world(arm, edit_bones(arm)[first].head), (0, 0, 0.45), 1e-3), "the root's head moves")
    h.update(ctx, at((0.0, 0.0, 0.5)), at((0.0, 0.0, 0.5)), 1.0)  # Put it back for the weights.
    return branch


def test_end_delete_undo(arm, first, branch):
    print("== ending chains, deleting, undo")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    bpy.ops.vrsculpt.cycle_brush()  # B, right hand.
    check(rig.anchor(arm) is None, "B ends the chain")
    press(ctx, (0.6, 0.0, 1.0))
    h = press(ctx, (0.6, 0.0, 1.3))
    eb = edit_bones(arm)
    root = h.bone
    check(eb[root].parent is None and near(world(arm, eb[root].head), (0.6, 0, 1.0)),
          "after it, a click and a press start a new root chain")
    tip = press(ctx, (0.6, 0.0, 1.5)).bone
    check(edit_bones(arm)[tip].parent == edit_bones(arm)[root], "which carries on")
    h = press(ctx, (0.6, 0.0, 1.15), left=True)
    eb = edit_bones(arm)
    check(isinstance(h, rig.DeleteBone) and h.changed and root not in eb, "left trigger on a bone deletes it")
    check(eb[tip].parent is None and not eb[tip].use_connect, "its child is kept, now a root")
    check(press(ctx, (2.0, 2.0, 2.0), left=True) is None and rig.anchor(arm) is None,
          "left trigger in empty space ends the chain")
    n = len(eb)
    s.rig_tool = 'BONES'
    press(ctx, (0.6, 0.0, 1.5))  # Carry on from the loose bone's tail.
    made = press(ctx, (0.6, 0.0, 1.7)).bone
    check(len(edit_bones(arm)) == n + 1, "one more bone")
    check(rig.undo_bone(ctx) == made and len(edit_bones(arm)) == n, "the panel's undo removes the last bone made")
    check(near(rig.anchor_world(arm), (0.6, 0, 1.5)), "and the chain carries on from its parent")
    # Clean up: the loose chain isn't needed for the weights tests.
    rig.delete_bone(arm, tip)
    rig.end_chain()
    check(set(edit_bones(arm).keys()) >= {first, branch}, "the main chain is still there")


def test_mirror(arm, first):
    print("== Mirror X")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    s.rig_mirror = True
    press(ctx, (0.0, 0.0, 1.5))  # The top joint (on the mirror plane).
    h = press(ctx, (0.3, 0.0, 1.4))
    eb = edit_bones(arm)
    name = h.bone
    other = bpy.utils.flip_name(name)
    check(name.endswith(".L") and other in eb, f"a bone off the plane gets a counterpart ({name} / {other})")
    check(near(world(arm, eb[other].tail), (-0.3, 0, 1.4)) and eb[other].parent == eb[name].parent,
          "mirrored, with the same parent")
    nxt = press(ctx, (0.5, 0.0, 1.3)).bone
    eb = edit_bones(arm)
    check(eb[bpy.utils.flip_name(nxt)].parent == eb[other], "the chain goes on mirrored")
    h = rig.pick(ctx, s, False, at((0.5, 0.0, 1.3)), 1.0)
    for p in ((0.5, 0.0, 1.3), (0.5, 0.1, 1.3)):
        h.update(ctx, at(p), at(p), 1.0)
    h.finish(ctx)
    check(near(world(arm, edit_bones(arm)[bpy.utils.flip_name(nxt)].tail), (-0.5, 0.1, 1.3), 1e-3),
          "moving a joint moves its mirror")
    press(ctx, (0.4, 0.05, 1.35), left=True)  # Delete the outer pair (the middle of the moved bone).
    eb = edit_bones(arm)
    check(nxt not in eb and bpy.utils.flip_name(nxt) not in eb, "deleting deletes both")
    rig.delete_bone(arm, name, mirror=True)
    check(name not in edit_bones(arm) and other not in edit_bones(arm), "pairs go together")
    s.rig_mirror = False
    rig.end_chain()


# --- bind and weights -----------------------------------------------------------------

def test_bind(arm, body, first, second):
    print("== bind with automatic weights")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    rig.set_tool(ctx, 'WEIGHTS')
    check(arm.mode == 'OBJECT' and rig.target(ctx) == arm, "Weights: the rig in Object Mode")
    check("rig_bind" in ids() and "weight_bone" not in ids(), "unbound: the page offers Bind instead of brushes")
    widget("rig_bind").on_press()
    mods = [m for m in body.modifiers if m.type == 'ARMATURE']
    check(len(mods) == 1 and mods[0].object == arm and body.parent == arm, "an Armature modifier, parented")
    check(first in body.vertex_groups and second in body.vertex_groups, "a vertex group per bone")
    w = rig.read_weights(body, first)
    check(w.max() > 0.9 and (w > 0).sum() < len(w), f"automatic weights ({(w > 0).sum()} of {len(w)} vertices)")
    check(rig.message().startswith("Bound Body"), f"the header says so: '{rig.message()}'")
    rig.bind(ctx)
    check(len([m for m in body.modifiers if m.type == 'ARMATURE']) == 1, "binding again keeps one modifier")
    check(ctx.view_layer.objects.active == arm and arm.mode == 'OBJECT', "the rig stays active, Weights' mode")
    check("weight_bone" in ids() and s.rig_mesh == body, "bound: the brush controls")


def test_weights(arm, body, first, second):
    print("== painting weights")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    s.weight_radius, s.weight_strength, s.weight_brush = 0.08, 1.0, 'ADD'
    rig.set_active_bone(arm, second)
    surface = Vector((0.15, 0.0, 0.7))  # On the tube's side, low (first bone's region).
    before = rig.read_weights(body, second)
    h = rig.pick(ctx, s, False, at(surface), 1.0)
    check(isinstance(h, rig.PaintWeights) and h.brush == 'ADD', "a press on the surface paints the active bone")
    n = h.dab(surface, 1.0, 0.1)
    h.finish(ctx)
    after = rig.read_weights(body, second)
    gained = after - before
    check(n > 0 and gained.max() > 0.3, f"Add raises the weights ({n} vertices, up to {gained.max():.2f})")
    co = rig.world_positions(ctx, body)[0]
    d = np.linalg.norm(co - np.array(surface), axis=1)
    check(np.all(gained[d > 0.08 + 1e-6] == 0.0), "only inside the radius")
    inner, outer = gained[d < 0.02], gained[(d > 0.06) & (d < 0.08)]
    check(len(inner) and len(outer) and inner.mean() > outer.mean(), "more at the center (falloff)")
    check(np.allclose(rig.layer(body, second), after, atol=1e-6), "the brush's copy matches the vertex group")

    h = rig.pick(ctx, s, True, at(surface), 1.0)
    check(h.brush == 'SUBTRACT', "the left hand subtracts")
    for _ in range(10):
        h.dab(surface, 1.0, 0.05)
    h.finish(ctx)
    w = rig.read_weights(body, second)
    check(w[d < 0.02].max() < 1e-4, "down to zero at the center")
    g = body.vertex_groups[second]
    center = int(np.argmin(d))
    try:
        g.weight(center)
        out = False
    except RuntimeError:
        out = True
    check(out, "a vertex brought to zero leaves the group")

    # Smooth evens out a hard edge in the first bone's weights.
    s.weight_brush = 'SMOOTH'
    rig.set_active_bone(arm, first)
    edge = Vector((0.0, -0.15, 0.9))  # Not +X: the branch bone comes out of the surface there.
    w0 = rig.read_weights(body, first)
    near_edge = np.linalg.norm(co - np.array(edge), axis=1) < 0.04
    h = rig.pick(ctx, s, True, at(edge), 1.0)
    check(h.brush == 'SMOOTH', "Smooth smooths with either hand")
    for _ in range(5):
        h.dab(edge, 1.0, 0.05)
    h.finish(ctx)
    w1 = rig.read_weights(body, first)
    check(np.ptp(w1[near_edge]) < np.ptp(w0[near_edge]) - 1e-3,
          f"Smooth narrows the spread ({np.ptp(w0[near_edge]):.2f} -> {np.ptp(w1[near_edge]):.2f})")

    # B takes back the last stroke (the smooth), then the subtract.
    bpy.ops.vrsculpt.cycle_brush()
    check(np.allclose(rig.read_weights(body, first), w0, atol=1e-5), "B undoes the last weight stroke")
    rig.undo_stroke(ctx)
    check(np.allclose(rig.read_weights(body, second), after, atol=1e-5), "and the one before")

    # Mirror: painting the right side paints the left on the counterpart (here: the same center bone).
    s.weight_brush, s.weight_mirror = 'ADD', True
    rig.set_active_bone(arm, second)
    w0 = rig.read_weights(body, second)
    h = rig.pick(ctx, s, False, at((0.15, 0.0, 1.3)), 1.0)
    h.dab((0.15, 0.0, 1.3), 1.0, 0.1)
    h.finish(ctx)
    gained = rig.read_weights(body, second) - w0
    left = np.linalg.norm(co - np.array((-0.15, 0.0, 1.3)), axis=1) < 0.03
    check(gained[left].min() > 0.05, "Mirror X paints the other side too")
    s.weight_mirror = False

    # Touching a bone inside the mesh (away from the surface) picks it.
    s.weight_radius = 0.02
    h = rig.pick(ctx, s, False, at((0.0, 0.0, 0.7)), 1.0)
    check(isinstance(h, rig.SelectBone) and rig.active_bone(arm) == first, "touching a bone picks it to paint")
    s.weight_radius = 0.08

    # The heat map's colors: blue for 0, red for 1.
    c = rig.heat_colors(np.array([0.0, 0.5, 1.0], dtype=np.float32))
    check(np.allclose(c[0, :3], (0, 0, 1)) and np.allclose(c[1, :3], (0, 1, 0)) and np.allclose(c[2, :3], (1, 0, 0)),
          "heat map: blue, green, red")

    # Normalize: every vertex's deform weights add up to 1.
    check(rig.normalize(ctx), "Normalize runs")
    totals = np.zeros(len(body.data.vertices))
    deform = {g.index for g in body.vertex_groups if g.name in arm.data.bones}
    for v in body.data.vertices:
        totals[v.index] = sum(e.weight for e in v.groups if e.group in deform)
    has = totals > 0
    check(np.allclose(totals[has], 1.0, atol=1e-3), "weights add up to 1 after Normalize")


def test_undo_guard(arm, body, second):
    print("== undo guard")
    ctx = bpy.context
    rig.layer(body, second)
    rig._history.append({"mesh": body.name, "groups": {}})
    operators._on_data_swap()
    check(not rig._layers and not rig._history and rig._heat["co"] is None,
          "an undo clears the weights, history and heat map caches")


# --- panel, modes, hints ---------------------------------------------------------------

def test_rows(arm, body):
    print("== hand panel pages")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    menus.reset()
    rig.set_tool(ctx, 'BONES')
    check(arm.mode == 'EDIT', "the Bones segment puts the rig in Edit Mode")
    check({"rig_pick", "rig_mesh", "rig_tool_bones", "rig_tool_weights", "rig_end", "rig_delete", "rig_bind",
           "rig_undo"} <= ids(), "Bones page: rig, mesh, tools, End chain, Delete, Bind, undo")
    pages = []
    for tool in ('BONES', 'WEIGHTS'):
        rig.set_tool(ctx, tool)
        key = 'RIG' if tool == 'BONES' else 'RIG_WEIGHTS'
        for expanded in (False, True):
            menus.set_more(key, expanded)
            widgets, height = menus.layout(ctx)
            overlap = any(a is not b and a.rect[0] < b.rect[2] - 1e-9 and b.rect[0] < a.rect[2] - 1e-9 and
                          a.rect[1] < b.rect[3] - 1e-9 and b.rect[1] < a.rect[3] - 1e-9
                          for a in widgets for b in widgets)
            check(height < 0.32 and not overlap, f"{tool}{' + More' if expanded else ''}: fits ({height:.3f} m)")
            pages.append(widgets)
    menus.set_more('RIG_WEIGHTS', False)
    rows = menus.panel(ctx).rows
    primary = len(rows) - 2  # Not the header, not the More row.
    menus.set_more('RIG_WEIGHTS', True)
    check(2 <= primary <= 5, f"Weights: {primary} controls up front")
    check({"weight_bone", "wbrush_ADD", "wbrush_SUBTRACT", "wbrush_SMOOTH", "weight_radius", "weight_strength",
           "weight_mirror", "weight_show", "weight_normalize", "rig_rebind", "weight_undo"} <= ids(),
          "Weights page: bone, brushes, radius, strength; More: mirror, show, normalize, rebind")
    used = {w.icon for page in pages for w in page if w.icon}
    check(not (used - set(gfx.ICONS)), f"every icon is drawn (missing {sorted(used - set(gfx.ICONS))})")
    widget("wbrush_SMOOTH").on_press()
    check(s.weight_brush == 'SMOOTH', "brush segment")
    widget("weight_bone").on_press()
    check(menus.picker_open(), "the Bone select opens a picker")
    first = vr_ui.find(menus.layout(ctx)[0], "opt_" + arm.data.bones[0].name)
    first.on_press()
    check(not menus.picker_open() and rig.active_bone(arm) == arm.data.bones[0].name, "choosing sets the bone to paint")
    widget("rig_tool_bones").on_press()
    check(s.rig_tool == 'BONES' and arm.mode == 'EDIT', "back to Bones: Edit Mode again")
    widget("rig_pick").on_press()
    vr_ui.find(menus.layout(ctx)[0], "opt_").on_press()  # New rig.
    check(rig.target(ctx) is None and arm.mode == 'OBJECT', "New rig: the old one leaves Edit Mode, next joint starts one")
    menus.close_picker()
    rig.choose_rig(ctx, arm.name)
    check(rig.target(ctx) == arm and arm.mode == 'EDIT', "choosing the rig again")
    s.weight_brush = 'ADD'
    menus.reset()


def test_modes(arm, body):
    print("== switching modes")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    menus.set_mode(ctx, 'PAINT')
    check(arm.mode == 'OBJECT' and ctx.view_layer.objects.active == body,
          "leaving Rig: the rig leaves Edit Mode, the mesh is active again")
    bpy.ops.vrsculpt.cycle_brush(left_hand=True)
    check(s.mode == 'RIG' and ctx.view_layer.objects.active == arm and arm.mode == 'EDIT',
          "Y after Paint: Rig, on the mesh's rig")
    bpy.ops.vrsculpt.cycle_brush(left_hand=True)
    check(s.mode == 'POSE' and arm.mode == 'POSE', "then Pose, which poses it")
    menus.set_mode(ctx, 'RIG')
    check(s.mode == 'RIG' and arm.mode == 'EDIT', "and back")


def test_hints_and_status():
    print("== hints and tip status")
    s = bpy.context.scene.vr_sculpt
    Info = controllers.Info

    def label(hand, button, info=Info()):
        h = controllers.hints(s, info)[hand][button]
        return h[1] if h else None

    s.mode, s.rig_tool = 'RIG', 'BONES'
    check(label('right', 'trigger') == "Place joint" and label('left', 'trigger') == "Delete bone" and
          label('right', 'top') == "End chain", "Bones: place, delete, B ends the chain")
    check(label('left', 'top') == "Next: Pose", "Y: Pose comes next")
    s.rig_tool = 'WEIGHTS'
    check(label('right', 'trigger') == "Add weight" and label('left', 'trigger') == "Subtract" and
          label('right', 'top') == "Undo stroke" and label('right', 'stick_click') == "Brush size",
          "Weights: add, left subtracts, B undoes, stick click resizes")
    check(label('right', 'trigger', Info(rig_bound=False)) is None, "unbound: nothing to paint")
    title, desc, color, icon = hud.card_content(s, None)
    check(title == "Add Weight" and icon in gfx.ICONS, "tip status: the brush")
    s.weight_radius = 0.04
    check(hud.status_size(bpy.context, s) == "4.0 cm", "and its radius")
    s.rig_tool = 'BONES'
    check(hud.card_content(s, None)[0] == "Bones" and hud.status_size(bpy.context, s) == "", "Bones status")
    s.mode = 'PAINT'
    check(label('left', 'top') == "Next: Rig", "Y after Paint names Rig")


def test_operator(arm):
    print("== the trigger operator")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    s.show_panel = False
    s.tip_offset = 0.0
    menus.set_mode(ctx, 'RIG')
    rig.set_tool(ctx, 'BONES')
    rig.end_chain()
    ss = tm.FakeSessionState()
    fctx = tm.FakeContext(tm.FakeWM(ss))
    running = operators.session_running
    operators.session_running = lambda context: True
    try:
        n = len(edit_bones(arm))
        _op, r = tm.press(fctx, ss, 1, [(0.9, 0.0, 1.0)])
        check(r == {'CANCELLED'}, "a root click is no undo step")
        _op, r = tm.press(fctx, ss, 1, [(0.9, 0.0, 1.2), (0.9, 0.0, 1.3)])
        check(r == {'FINISHED'} and len(edit_bones(arm)) == n + 1, "a placed bone is one undo step")
        _op, r = tm.press(fctx, ss, 0, [(0.9, 0.0, 1.1)])
        check(r == {'FINISHED'} and len(edit_bones(arm)) == n, "the left trigger deletes it (one step)")
    finally:
        operators.session_running = running
        s.show_panel = True


def main():
    vr_sculpt.register()
    try:
        ctx = bpy.context
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        s = ctx.scene.vr_sculpt
        s.grab_delay = 0.0  # Joints are dragged in single steps.
        s.grid_snap = False
        body = make_body(ctx)
        arm, first, second, third = test_chain(body)
        branch = test_branch_and_move(arm, first, second, third)
        test_end_delete_undo(arm, first, branch)
        test_mirror(arm, first)
        test_bind(arm, body, first, second)
        test_weights(arm, body, first, second)
        test_undo_guard(arm, body, second)
        test_rows(arm, body)
        test_modes(arm, body)
        test_hints_and_status()
        test_operator(arm)
    except Exception:
        traceback.print_exc()
        failures.append("raised")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")


if __name__ == "__main__":
    main()
