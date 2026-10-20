"""Non-sculpt trigger interactions: hand panel presses, grabbing objects, creating primitives.

Each handler gets update(context, aim_pose, tip_pose, value) every frame while the trigger is
held and finish(context) on release; `changed` decides whether an undo step is pushed.
"""

from mathutils import Vector

from . import generic, menus, primitives, vr_ui

# Widget the right trigger is holding down, for drawing.
pressed_id = None

TOUCH_MARGIN = 0.01  # hand-space meters around an object that still count as touching it
CLICK_DISTANCE = 0.015  # hand-space meters; a shorter drag counts as a click


def aim_direction(pose):
    # OpenXR aim poses point down local -Z.
    return (pose.to_3x3() @ Vector((0.0, 0.0, -1.0))).normalized()


def panel_frame(context, ss):
    """(matrix, widgets, height) of the hand panel above the left controller."""
    scale = ss.viewer_scale
    widgets, height = vr_ui.layout(menus.rows(context))
    anchor = Vector(ss.controller_grip_location_get(context, 0)) + Vector((0.0, 0.0, 0.07 * scale))
    matrix = vr_ui.panel_matrix(anchor, Vector(ss.viewer_pose_location), scale)
    return matrix, widgets, height


def panel_pointer(context, ss, pose):
    """(widgets, hit_xy, matrix, height) for a ray from pose; hit_xy is None when it misses."""
    matrix, widgets, height = panel_frame(context, ss)
    hit = vr_ui.ray_hit(matrix, pose.translation, aim_direction(pose), height, ss.viewer_scale)
    return widgets, hit, matrix, height


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
        # Rebuild the panel: it follows the left hand, and values may have changed.
        matrix, widgets, height = panel_frame(context, self.ss)
        widget = vr_ui.find(widgets, self.widget_id)
        if widget is None:
            return
        # Keep dragging even if the ray slides off the panel: use the panel plane.
        x = vr_ui.ray_plane_x(matrix, pose.translation, aim_direction(pose))
        if x is not None:
            self._set(widget, x)

    def finish(self, context):
        global pressed_id
        pressed_id = None
        generic.flush_pending(context)
        if self.changed:
            context.view_layer.update()


class MoveObject:
    """Grab an object with the controller: it follows the controller's position and rotation."""

    def __init__(self, context, obj, pose):
        self.obj = obj
        self.start = obj.matrix_world.copy()
        self.pose_inv = pose.inverted_safe()
        self.changed = False
        primitives.make_active(context, obj)

    def update(self, context, pose, tip_pose, value):
        new = tip_pose @ self.pose_inv @ self.start
        if new != self.obj.matrix_world:
            self.obj.matrix_world = new
            self.changed = True

    def finish(self, context):
        pass


class CreatePrimitive:
    """Drag out a box from the press point; the primitive fills it. A click makes a default size."""

    def __init__(self, context, key, tip, scale):
        self.start = Vector(tip)
        self.scale = scale
        self.obj = primitives.create_from_entry(context, key, self.start)
        self._fit(context, self.start)
        self.changed = True

    def _fit(self, context, tip):
        if (Vector(tip) - self.start).length < CLICK_DISTANCE * self.scale:
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
