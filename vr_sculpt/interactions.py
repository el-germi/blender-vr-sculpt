"""Non-sculpt trigger interactions: hand panel presses, grabbing objects, creating primitives.

Each handler gets update(context, aim_pose, tip_pose, value) every frame while the trigger is
held and finish(context) on release; `changed` decides whether an undo step is pushed.
"""

import math
import time

from mathutils import Matrix, Vector

from . import generic, grid, menus, primitives, vr_ui

# Widget the right trigger is holding down, for drawing.
pressed_id = None

TOUCH_MARGIN = 0.01  # hand-space meters around an object that still count as touching it
CLICK_DISTANCE = 0.015  # hand-space meters; a shorter drag counts as a click


def aim_direction(pose):
    # OpenXR aim poses point down local -Z.
    return (pose.to_3x3() @ Vector((0.0, 0.0, -1.0))).normalized()


TIMELINE_WIDTH = 0.22  # hand-space meters
TIMELINE_GAP = 0.008


def panel_frame(context, ss):
    """(matrix, widgets, height) of the hand panel above the left controller."""
    scale = ss.viewer_scale
    widgets, height = vr_ui.layout(menus.rows(context))
    anchor = Vector(ss.controller_grip_location_get(context, 0)) + Vector((0.0, 0.0, 0.07 * scale))
    matrix = vr_ui.panel_matrix(anchor, Vector(ss.viewer_pose_location), scale)
    return matrix, widgets, height


def panel_frames(context, ss):
    """[(matrix, widgets, height, width)]: the hand panel, plus the timeline strip on its right."""
    matrix, widgets, height = panel_frame(context, ss)
    out = [(matrix, widgets, height, vr_ui.WIDTH)]
    settings = context.scene.vr_sculpt
    if settings.show_timeline and not menus.picker_open():
        tw, th = vr_ui.layout(menus.timeline_rows(context, settings), TIMELINE_WIDTH)
        offset = vr_ui.WIDTH / 2 + TIMELINE_GAP + TIMELINE_WIDTH / 2
        out.append((matrix @ Matrix.Translation((offset, 0.0, 0.0)), tw, th, TIMELINE_WIDTH))
    return out


def panel_pointer(context, ss, pose):
    """(widgets, hit_xy, matrix, height) for a ray from pose at the hand panel or the timeline strip;
    hit_xy is None when it misses both (then the hand panel's frame is returned)."""
    frames = panel_frames(context, ss)
    for matrix, widgets, height, width in frames:
        hit = vr_ui.ray_hit(matrix, pose.translation, aim_direction(pose), height, ss.viewer_scale, width)
        if hit is not None:
            return widgets, hit, matrix, height
    matrix, widgets, height, _width = frames[0]
    return widgets, None, matrix, height


class UIPress:
    """Trigger pressed while pointing at the hand panel. Buttons fire on press; sliders drag."""

    def __init__(self, context, ss, widget, hit):
        global pressed_id
        self.ss = ss
        self.widget_id = widget.id if widget else None
        self.kind = widget.kind if widget else None
        self.changed = False
        pressed_id = self.widget_id
        if widget is None:
            return
        if widget.kind in {'button', 'select'} and widget.on_press:
            widget.on_press()
            self.changed = True
        elif widget.kind == 'slider':
            self._set(widget, hit[0])

    def _set(self, widget, x):
        value = widget.value_at(x)
        if value != widget.value:
            widget.on_change(value)
            self.changed = True

    def update(self, context, pose, tip_pose, value):
        if self.kind != 'slider':
            return
        # Rebuild the panels: they follow the left hand, and values may have changed.
        widget = matrix = None
        for m, widgets, _height, _width in panel_frames(context, self.ss):
            widget = vr_ui.find(widgets, self.widget_id)
            if widget is not None:
                matrix = m
                break
        if widget is None:
            return
        # Keep dragging even if the ray slides off the panel: use the panel plane.
        x = vr_ui.ray_plane_x(matrix, pose.translation, aim_direction(pose))
        if x is not None:
            self._set(widget, x)

    def abandon(self):
        global pressed_id
        pressed_id = None

    def finish(self, context):
        global pressed_id
        pressed_id = None
        generic.flush_pending(context)
        if self.changed:
            context.view_layer.update()


GATE_ANGLE = math.radians(8.0)  # turning the controller this far also starts a grab's move


class DragGate:
    """Keeps a grab still until the hand clearly means to move it: it moved past the Grab Threshold,
    turned past GATE_ANGLE, or the trigger was held for the Grab Delay. Selecting needs a trigger
    press, and without this the jitter of that press nudged what it selected.

    The move then counts from the last pose that was still inside the threshold, so nothing jumps
    when it starts and a fast move isn't lost.
    """

    def __init__(self, context, pose):
        settings = context.scene.vr_sculpt
        ss = context.window_manager.xr_session_state
        self.distance = settings.grab_deadzone * (ss.viewer_scale if ss is not None else 1.0)
        self.delay = settings.grab_delay
        self.start = pose.copy()
        self.inside = pose.copy()
        self.t0 = time.monotonic()
        self.open = False

    def update(self, pose):
        """The pose the move counts from, on the update that opens the gate; otherwise None."""
        if self.open:
            return None
        moved = (pose.translation - self.start.translation).length > self.distance
        turned = self.start.to_quaternion().rotation_difference(pose.to_quaternion()).angle > GATE_ANGLE
        if moved or turned:
            self.open = True
            return self.inside
        if self.delay > 0.0 and time.monotonic() - self.t0 >= self.delay:
            self.open = True
            return pose.copy()
        self.inside = pose.copy()
        return None


class MoveObject:
    """Grab an object with the controller: it follows the controller's position and rotation
    (once the DragGate opens; until then the press only selects it)."""

    def __init__(self, context, obj, pose):
        self.obj = obj
        self.start = obj.matrix_world.copy()
        self.pose_inv = pose.inverted_safe()
        self.gate = DragGate(context, pose)
        self.changed = False
        primitives.make_active(context, obj)

    def update(self, context, pose, tip_pose, value):
        if not self.gate.open:
            base = self.gate.update(tip_pose)
            if base is None:
                return
            self.pose_inv = base.inverted_safe()
        new = tip_pose @ self.pose_inv @ self.start
        new.translation = grid.snap_if(context.scene.vr_sculpt, new.translation)
        if new != self.obj.matrix_world:
            self.obj.matrix_world = new
            self.changed = True

    def finish(self, context):
        if self.changed:
            from . import anim
            anim.auto_key(context, self.obj)


class CreatePrimitive:
    """Drag out a box from the press point; the primitive fills it. A click makes a default size."""

    def __init__(self, context, key, tip, scale):
        self.start = grid.snap_if(context.scene.vr_sculpt, tip)
        self.scale = scale
        self.obj = primitives.create_from_entry(context, key, self.start)
        self._fit(context, self.start)
        self.changed = True

    def _fit(self, context, tip):
        tip = grid.snap_if(context.scene.vr_sculpt, tip)
        if (tip - self.start).length < CLICK_DISTANCE * self.scale:
            half = Vector((primitives.DEFAULT_SIZE * self.scale / 2,) * 3)
            a, b = self.start - half, self.start + half
        else:
            a, b = self.start, Vector(tip)
        prim = self.obj.vr_prim
        if prim.kind == 'OPERATOR':
            # Uniform: the operator's own proportions are kept, the largest side fills the box.
            generic.fit(context, self.obj, a, b, 0.005 * self.scale)
        else:
            self.obj.location = primitives.fit_box(prim, a, b)

    def update(self, context, pose, tip_pose, value):
        self._fit(context, tip_pose.translation)

    def finish(self, context):
        # Refresh bounds now, so the new object can be grabbed right away.
        context.view_layer.update()
