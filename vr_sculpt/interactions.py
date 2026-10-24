"""Non-sculpt trigger interactions: hand panel presses, grabbing objects, creating primitives.

Each handler gets update(context, aim_pose, tip_pose, value) every frame while the trigger is
held and finish(context) on release; `changed` decides whether an undo step is pushed.
"""

import math
import time

import bmesh
import bpy
from mathutils import Matrix, Vector

from . import generic, grid, hardsurface, kit, menus, presence, primitives, snapping, vr_ui

# Widget the right trigger is holding down, for drawing.
pressed_id = None

TOUCH_MARGIN = 0.01  # hand-space meters around an object that still count as touching it
CLICK_DISTANCE = 0.015  # hand-space meters; a shorter drag counts as a click


def aim_direction(pose):
    # OpenXR aim poses point down local -Z.
    return (pose.to_3x3() @ Vector((0.0, 0.0, -1.0))).normalized()


TIMELINE_WIDTH = 0.2  # hand-space meters
TIMELINE_GAP = 0.006


def panel_frame(context, ss):
    """(matrix, widgets, height) of the hand panel above the left controller."""
    scale = ss.viewer_scale
    widgets, height = menus.layout(context)
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


def panel_pointer(context, ss, pose, frames=None):
    """(widgets, hit_xy, matrix, height) for a ray from pose at the hand panel or the timeline strip;
    hit_xy is None when it misses both (then the hand panel's frame is returned)."""
    frames = frames or panel_frames(context, ss)
    for matrix, widgets, height, width in frames:
        hit = vr_ui.ray_hit(matrix, pose.translation, aim_direction(pose), height, ss.viewer_scale, width)
        if hit is not None:
            return widgets, hit, matrix, height
    matrix, widgets, height, _width = frames[0]
    return widgets, None, matrix, height


_hover = {"id": None}


def note_hover(context, ss, widget_id):
    """A light tick on the right hand when the ray moves onto a new widget."""
    if widget_id != _hover["id"]:
        _hover["id"] = widget_id
        if widget_id is not None:
            presence.tick(context, ss, 1, 0.08, 0.006)


DETENTS = 10  # slider ticks along the track (integer sliders with few values tick on each)
REPEAT_DELAY = 0.4  # seconds a stepper press waits before repeating
REPEAT_EVERY = 0.09


def _detent(widget, value):
    span = widget.max - widget.min
    if span <= 0:
        return 0
    if widget.integer and span <= 30:
        return int(round(value))
    return int(min(max((value - widget.min) / span, 0.0), 1.0) * DETENTS + 1e-6)


class UIPress:
    """Trigger pressed while pointing at the hand panel. Buttons fire on press; sliders drag;
    steppers step (and repeat while held)."""

    def __init__(self, context, ss, widget, hit):
        global pressed_id
        self.ss = ss
        self.widget_id = widget.id if widget else None
        self.kind = widget.kind if widget else None
        self.changed = False
        self.step = 0
        self.t0 = time.monotonic()
        self.repeats = 0
        pressed_id = self.widget_id
        if widget is None:
            return
        vr_ui.pulse(widget.id)
        if widget.kind == 'slider':
            self.detent = _detent(widget, widget.value)
            self._set(context, widget, hit[0])
        elif widget.kind == 'stepper':
            self.step = widget.step_at(hit[0])
            self._step(widget)
        elif widget.on_press:
            widget.on_press()
            self.changed = True

    def _set(self, context, widget, x):
        value = widget.value_at(x)
        if value != widget.value:
            widget.on_change(value)
            self.changed = True
            detent = _detent(widget, value)
            if detent != self.detent:
                self.detent = detent
                presence.tick(context, self.ss, 1, 0.22, 0.005)

    def _step(self, widget):
        value = widget.stepped(self.step) if self.step else None
        if value is not None:
            widget.on_change(value)
            self.changed = True

    def _find(self, context):
        # Rebuild the panels: they follow the left hand, and values may have changed.
        for m, widgets, _height, _width in panel_frames(context, self.ss):
            widget = vr_ui.find(widgets, self.widget_id)
            if widget is not None:
                return widget, m
        return None, None

    def update(self, context, pose, tip_pose, value):
        if self.kind == 'stepper' and self.step:
            held = time.monotonic() - self.t0 - REPEAT_DELAY
            due = int(held / REPEAT_EVERY) + 1 if held >= 0 else 0
            if due > self.repeats:
                self.repeats = due
                widget, _m = self._find(context)
                if widget is not None:
                    self._step(widget)
                    vr_ui.pulse(widget.id)
                    presence.tick(context, self.ss, 1, 0.18, 0.005)
            return
        if self.kind != 'slider':
            return
        widget, matrix = self._find(context)
        if widget is None:
            return
        # Keep dragging even if the ray slides off the panel: use the panel plane.
        x = vr_ui.ray_plane_x(matrix, pose.translation, aim_direction(pose))
        if x is not None:
            self._set(context, widget, x)

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
        self.snap = None  # snap to surfaces: None follows Snap to Surface
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
        if settings.snap_surface if self.snap is None else self.snap:
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


class Deselect:
    """Left trigger in empty space (Model mode): nothing is selected any more, so what the right trigger
    makes next is an object of its own, not a cutter on the old selection."""

    def __init__(self, context):
        self.changed = any(o is not None and o.select_get() for o in context.view_layer.objects)
        primitives.make_active(context, None)

    def update(self, context, pose, tip_pose, value):
        pass

    def finish(self, context):
        pass


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


def _cut_target(context, asset):
    """What a new kit part cuts with New as Cut / Union / Intersect (collection instances can't cut)."""
    if context.scene.vr_sculpt.model_add_as == 'OBJECT' or asset.id_type != 'OBJECT':
        return None
    return hardsurface.cut_target(context)


class PlaceAsset(MoveObject):
    """Drop a kit part: it appears at the tip and follows the hand like a grabbed object, settling on
    any surface in reach whatever Snap to Surface says (parts are made to sit on things). With New as
    Cut / Union / Intersect and an object selected, it's a cutter on that object from the start."""

    def __init__(self, context, asset, tip_pose, scale):
        settings = context.scene.vr_sculpt
        self.target = _cut_target(context, asset)
        obj = kit.place(context, asset, settings.kit_scale)
        if self.target is not None:
            hardsurface.make_cutter(context, obj, self.target, settings.model_add_as, parent=False)
        # The part hangs from the tip by its anchor (bottom center, or the center for a cutter).
        rs = obj.matrix_world.to_3x3()
        obj.matrix_world = Matrix.Translation(tip_pose.translation - rs @ snapping.anchor(obj)) @ rs.to_4x4()
        super().__init__(context, obj, tip_pose, scale)
        self.snap = True
        self.gate.open = True
        self.update(context, tip_pose, tip_pose, 1.0)
        self.changed = True

    def finish(self, context):
        snapping.clear()
        if self.target is not None:
            # Placed in world space while carried; from now on it moves with its target.
            hardsurface.attach(self.obj, self.target)
            hardsurface.refresh_solver(self.obj)
        context.view_layer.update()


def surface_frame(normal, along):
    """Rotation with Z on normal and X along the given direction (its part across the surface)."""
    z = Vector(normal).normalized()
    x = Vector(along) - z * Vector(along).dot(z)
    if x.length < 1e-6:
        x = Vector((1.0, 0.0, 0.0)) if abs(z.x) < 0.9 else Vector((0.0, 1.0, 0.0))
        x -= z * x.dot(z)
    x.normalize()
    return Matrix((x, z.cross(x), z)).transposed()


STAMPS_PER_UPDATE = 16


class StampAssets:
    """Sweep across surfaces to place a kit part every Spacing, each standing on the surface and
    turned along the sweep, like a brush (bolts along a seam, greebles across a hull). Nothing is
    placed away from surfaces. Copies share their mesh. With New as Cut / Union / Intersect and an
    object selected, the stamps make one cutter on it together: one Boolean however many there are."""

    def __init__(self, context, asset, tip_pose, scale):
        settings = context.scene.vr_sculpt
        self.asset = asset
        self.size = settings.kit_scale
        self.spacing = settings.kit_spacing * scale
        self.target = _cut_target(context, asset)
        self.operation = settings.model_add_as
        self.surface = snapping.SurfaceSnap(context, None, scale, target=self.target)
        self.first = None  # the first stamp: later ones share its data (or join its mesh, as a cutter)
        self.first_matrix = None
        self.rs = None  # the part's own rotation and scale, times Part Size
        self.anchor = None  # object-space point that lands on the surface
        self.part_mesh = None  # cutter stamps: the part's mesh, added to the cutter at each stamp
        self.bm = None
        self.last = None
        self.count = 0
        self.changed = False
        self.update(context, tip_pose, tip_pose, 1.0)

    def _start(self, context):
        part = kit.place(context, self.asset, self.size, select=False)
        context.view_layer.update()
        self.rs = part.matrix_world.to_3x3()
        if self.target is None:
            self.first = part
            self.anchor = snapping.anchor(part)
            return
        lo, hi = primitives.local_bounds(part)
        self.anchor = (lo + hi) / 2  # Cutters sink halfway in.
        self.part_mesh = part.data
        bpy.data.objects.remove(part)
        mesh = self.part_mesh.copy()
        mesh.name = self.asset.name + " Stamps"
        self.first = bpy.data.objects.new(mesh.name, mesh)
        context.collection.objects.link(self.first)
        hardsurface.make_cutter(context, self.first, self.target, self.operation, parent=False)
        self.bm = bmesh.new()
        self.bm.from_mesh(mesh)

    def _stamp(self, context, point, normal, along):
        if self.first is None:
            self._start(context)
        full = surface_frame(normal, along) @ self.rs
        matrix = Matrix.Translation(point - full @ self.anchor) @ full.to_4x4()
        if self.count == 0:
            self.first.matrix_world = matrix
            self.first_matrix = matrix
        elif self.bm is None:
            obj = self.first.copy()  # Shares the mesh (or the instanced collection).
            for coll in self.first.users_collection:
                coll.objects.link(obj)
            obj.matrix_world = matrix
        else:
            n = len(self.bm.verts)
            self.bm.from_mesh(self.part_mesh)  # Adds to what's there.
            self.bm.verts.ensure_lookup_table()
            bmesh.ops.transform(self.bm, matrix=self.first_matrix.inverted_safe() @ matrix, verts=self.bm.verts[n:])
            self.bm.to_mesh(self.first.data)
            self.first.data.update()
        self.count += 1
        self.changed = True

    def update(self, context, pose, tip_pose, value):
        hit = self.surface.nearest(context, tip_pose.translation)
        if hit is None:
            return
        point, normal = hit
        stamped = False
        if self.last is None:
            self._stamp(context, point, normal, tip_pose.to_3x3() @ Vector((1.0, 0.0, 0.0)))
            self.last = point
            stamped = True
        # Stamps go exactly Spacing apart along the sweep, however fast it moves (a fast one gets several).
        for _ in range(STAMPS_PER_UPDATE):
            step = point - self.last
            if step.length < self.spacing - 1e-9:
                break
            hit = self.surface.nearest(context, self.last + step.normalized() * self.spacing)
            if hit is None or (hit[0] - self.last).length < 1e-6:
                break
            self._stamp(context, hit[0], hit[1], step)
            self.last = hit[0]
            stamped = True
        ss = context.window_manager.xr_session_state
        if stamped and ss is not None:
            presence.tick(context, ss, 1, 0.3, 0.01)

    def _free(self):
        if self.bm is not None:
            self.bm.free()
            self.bm = None

    def abandon(self):
        self._free()

    def finish(self, context):
        self._free()
        if self.part_mesh is not None and self.part_mesh.users == 0:
            bpy.data.meshes.remove(self.part_mesh)
        if self.target is not None and self.first is not None:
            hardsurface.attach(self.first, self.target)
            hardsurface.refresh_solver(self.first)
        context.view_layer.update()


def pick_kit(context, settings, tip_pose, scale):
    """The trigger's handler for the current kit part: stamp or drop."""
    asset = kit.find(settings.primitive)
    if asset is None:
        raise RuntimeError(f"Kit part '{kit.label(settings.primitive)}' is gone (was its file moved?)")
    if settings.kit_stamp:
        return StampAssets(context, asset, tip_pose, scale)
    return PlaceAsset(context, asset, tip_pose, scale)
