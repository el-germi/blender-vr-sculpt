"""Non-sculpt trigger interactions: grabbing objects and creating primitives (spatial.py has the UI's).

Each handler gets update(context, aim_pose, tip_pose, value) every frame while the trigger is
held and finish(context) on release; `changed` decides whether an undo step is pushed.
"""

import math
import time

from mathutils import Matrix, Vector

from . import generic, grid, hardsurface, primitives, snapping

TOUCH_MARGIN = 0.01  # hand-space meters around an object that still count as touching it
CLICK_DISTANCE = 0.015  # hand-space meters; a shorter drag counts as a click


def aim_direction(pose):
    # OpenXR aim poses point down local -Z.
    return (pose.to_3x3() @ Vector((0.0, 0.0, -1.0))).normalized()


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
    (once the DragGate opens; until then the press only selects it). With Snap to Surface it sits
    on the surface near it; with Snap Rotation it turns in steps (snapping.py)."""

    def __init__(self, context, obj, pose, scale=1.0):
        self.obj = obj
        self.start = obj.matrix_world.copy()
        self.pose_inv = pose.inverted_safe()
        self.gate = DragGate(context, pose)
        self.changed = False
        self.scale = scale
        self.surface = None
        primitives.make_active(context, obj)

    def update(self, context, pose, tip_pose, value):
        if not self.gate.open:
            base = self.gate.update(tip_pose)
            if base is None:
                return
            self.pose_inv = base.inverted_safe()
        settings = context.scene.vr_sculpt
        new = tip_pose @ self.pose_inv @ self.start
        placed = None
        if settings.snap_surface:
            if self.surface is None:
                self.surface = snapping.SurfaceSnap(context, self.obj, self.scale)
            placed = self.surface.place(context, new, settings.snap_angle)
        if placed is not None:
            new = placed
        else:
            if settings.snap_angle:
                loc, _rot, size = new.decompose()
                new = snapping.snap_rotation(new.to_3x3().normalized()).to_4x4() @ Matrix.Diagonal((*size, 1.0))
                new.translation = loc
            new.translation = grid.snap_if(settings, new.translation)
        if new != self.obj.matrix_world:
            self.obj.matrix_world = new
            self.changed = True

    def abandon(self):
        snapping.clear()

    def finish(self, context):
        snapping.clear()
        if self.changed:
            from . import anim
            anim.auto_key(context, self.obj)


class CreatePrimitive:
    """Drag out a box from the press point; the primitive fills it. A click makes a default size.

    With New as Cut / Union / Intersect and an object selected, the primitive is a cutter on that
    object from the start, so the cut shows while you drag (hardsurface.py)."""

    def __init__(self, context, key, tip, scale):
        settings = context.scene.vr_sculpt
        self.start = grid.snap_if(settings, tip)
        self.scale = scale
        self.target = hardsurface.cut_target(context) if settings.model_add_as != 'OBJECT' else None
        self.obj = primitives.create_from_entry(context, key, self.start)
        if self.target is not None:
            hardsurface.make_cutter(context, self.obj, self.target, settings.model_add_as, parent=False)
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
        if self.target is not None:
            # Placed in world space while dragging; from now on it moves with its target.
            hardsurface.attach(self.obj, self.target)
            hardsurface.refresh_solver(self.obj)
        # Refresh bounds now, so the new object can be grabbed right away.
        context.view_layer.update()
