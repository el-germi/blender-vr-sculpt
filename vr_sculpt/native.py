"""Experimental engine: drive Blender's own sculpt brushes from the controller tip.

Findings from tests/native_probe*.py on 5.4.0 Alpha:
- sculpt.brush_stroke honors each stroke element's 3D "location", so dabs land at
  the controller tip, even on parts hidden from the desktop view.
- The element's "mouse" must still be over the mesh in some 3D viewport, or the
  dab is silently dropped. We aim it at the mesh vertex nearest the tip.
- Its poll needs Sculpt Mode, an active brush and a brush-type tool active in the
  target viewport (sculpt_mode_and_brush_poll / paint_brush_tool_poll).
- The element's "size" is ignored. The radius comes from the unified paint
  settings (even with unified size off); with the size locked to 'SCENE',
  unprojected_size is a diameter in object space. We set it for the stroke's length.
- The element's "location" is in object space too.
- Directional brushes (Clay Strips...) do nothing with one-element strokes, so
  each call gets a zero-pressure element at the previous tip first.
- One call takes ~0.5 ms and only updates the touched part of the mesh.
"""

import time

import bpy
import numpy as np
from bpy_extras import view3d_utils
from mathutils import Vector

from . import brushes

# Essentials brushes the "next brush" button cycles through in native mode.
NATIVE_BRUSHES = ("Draw", "Clay Strips", "Smooth", "Inflate/Deflate", "Crease Sharp", "Flatten/Contrast")
_ESSENTIALS = "brushes/essentials_brushes-mesh_sculpt.blend/Brush/"


def largest_view3d(window):
    best = None
    for area in window.screen.areas:
        if area.type != 'VIEW_3D':
            continue
        region = next((r for r in area.regions if r.type == 'WINDOW'), None)
        if region and (best is None or region.width * region.height > best[1].width * best[1].height):
            best = (area, region)
    return best


def sculpt_brush(context):
    return context.tool_settings.sculpt.brush


def get_strength(context):
    """The native brush strength: the unified settings' when Unified Strength is on, else the brush's."""
    sculpt = context.tool_settings.sculpt
    ups = sculpt.unified_paint_settings if sculpt else None
    if ups is not None and ups.use_unified_strength:
        return ups.strength
    brush = sculpt.brush if sculpt else None
    return brush.strength if brush is not None else None


def set_strength(context, value):
    """Set it on both, like the size (5.4 Alpha reads some brush values from the unified settings)."""
    sculpt = context.tool_settings.sculpt
    for owner in (sculpt.unified_paint_settings, sculpt.brush) if sculpt else ():
        if owner is not None:
            owner.strength = value


def activate_brush(context, name):
    window = context.window or context.window_manager.windows[0]
    view = largest_view3d(window)
    with context.temp_override(window=window, area=view[0], region=view[1]):
        bpy.ops.brush.asset_activate(
            asset_library_type='ESSENTIALS', relative_asset_identifier=_ESSENTIALS + name)
    return name


def cycle_brush(context):
    brush = sculpt_brush(context)
    current = brush.name if brush else ""
    names = list(NATIVE_BRUSHES)
    nxt = names[(names.index(current) + 1) % len(names)] if current in names else names[0]
    return activate_brush(context, nxt)


class _SceneSize:
    """Temporarily lock the brush size to a world-space diameter, then restore it."""

    def __init__(self, context, radius_world):
        sculpt = context.tool_settings.sculpt
        # 5.4 Alpha reads the unified settings; set the brush too in case a build honors it.
        self.owners = [o for o in (sculpt.unified_paint_settings, sculpt.brush) if o is not None]
        self.saved = [(o.use_locked_size, o.unprojected_size) for o in self.owners]
        for o in self.owners:
            o.use_locked_size = 'SCENE'
            o.unprojected_size = radius_world * 2.0

    def restore(self):
        for o, (lock, size) in zip(self.owners, self.saved):
            try:
                o.use_locked_size = lock
                o.unprojected_size = size
            except ReferenceError:
                pass


class NativeStroke:
    """Same interface as brushes.Stroke, but each dab calls sculpt.brush_stroke."""

    def __init__(self, context, obj, radius_world, invert=False, spacing=0.15):
        # Don't keep `context`: XR action operators may get a temporary one that is
        # gone by the next modal event. Each dab uses bpy.context instead.
        self.window = context.window
        if largest_view3d(self.window) is None:
            raise RuntimeError("Native sculpt needs a 3D viewport in the desktop window")
        self.tried_brush_tool = False
        self.diagnosed = False
        # Snapshot of positions to find the vertex nearest the tip.
        self.state = brushes.MeshState(obj)
        self.radius_world = radius_world
        self.invert = invert
        self.spacing = spacing
        self.last_local = None
        self.last_elem = None
        self.dabs = 0
        self.dropped = 0
        self.touched = 0
        # The locked brush size is in object space too, so undo the object's scale.
        self.size = _SceneSize(context, self.state.local_radius(radius_world))

    def finish(self):
        self.size.restore()

    @staticmethod
    def _mouse_for(region, rv3d, vertex_world):
        p = view3d_utils.location_3d_to_region_2d(region, rv3d, vertex_world)
        if p is None or not (0 <= p.x < region.width and 0 <= p.y < region.height):
            return None
        return p

    def _can_stroke(self):
        """Poll sculpt.brush_stroke (call inside the override); fix the tool once if needed."""
        if bpy.ops.sculpt.brush_stroke.poll():
            return True
        if not self.tried_brush_tool:
            # The poll needs a brush-type tool active in the target viewport.
            self.tried_brush_tool = True
            try:
                bpy.ops.wm.tool_set_by_id(name="builtin.brush", space_type='VIEW_3D')
            except RuntimeError as ex:
                print("VR Sculpt native: could not switch to the Brush tool:", ex)
            if bpy.ops.sculpt.brush_stroke.poll():
                print("VR Sculpt native: switched the desktop viewport to the Brush tool", flush=True)
                return True
        if not self.diagnosed:
            self.diagnosed = True
            c = bpy.context
            obj = c.active_object
            tool = c.workspace.tools.from_space_view3d_mode(c.mode, create=False) if c.workspace else None
            brush = c.tool_settings.sculpt.brush
            print("VR Sculpt native: sculpt.brush_stroke poll failed |",
                  f"object={obj.name if obj else None} object.mode={obj.mode if obj else None}",
                  f"context.mode={c.mode} brush={brush.name if brush else None}",
                  f"area={c.area.type if c.area else None} region={c.region.type if c.region else None}",
                  f"tool={tool.idname if tool else None} use_brushes={tool.use_brushes if tool else None}",
                  f"window={c.window is not None} workspace={c.workspace.name if c.workspace else None}",
                  flush=True)
        return False

    def update(self, tip_world, pose_world=None, pressure=1.0):
        t0 = time.perf_counter()
        st = self.state
        tip = st.to_local(tip_world)
        r = st.local_radius(self.radius_world)
        if self.last_local is not None and np.linalg.norm(tip - self.last_local) < r * self.spacing:
            return 0
        self.last_local = tip

        idx, w = st.select(tip, r)
        if len(idx) == 0:
            self.last_elem = None
            return 0
        context = bpy.context
        window = context.window or self.window
        view = largest_view3d(window)
        if view is None:
            self.dropped += 1
            return 0
        area, region = view
        rv3d = area.spaces.active.region_3d
        nearest = st.co[idx[int(np.argmax(w))]]
        mouse = self._mouse_for(region, rv3d, Vector(brushes._apply_mat(st.world, nearest[None, :])[0]))
        if mouse is None:
            self.dropped += 1
            return 0

        # Sculpt mode works in object space: "location" is local, not world.
        elem = {"name": "", "location": tuple(tip), "mouse": tuple(mouse), "mouse_event": tuple(mouse),
                "pressure": float(pressure), "size": 50.0, "x_tilt": 0.0, "y_tilt": 0.0,
                "time": 0.0, "is_start": True}
        elems = [elem]
        if self.last_elem is not None:
            # Zero-pressure lead-in gives directional brushes a stroke direction.
            lead = dict(self.last_elem, pressure=0.0, is_start=True)
            elem = dict(elem, is_start=False)
            elems = [lead, elem]
        with context.temp_override(window=window, area=area, region=region):
            if not self._can_stroke():
                self.dropped += 1
                return 0
            bpy.ops.sculpt.brush_stroke(stroke=elems, mode='INVERT' if self.invert else 'NORMAL')
        self.last_elem = elem
        self.dabs += 1
        self.touched = len(idx)
        st.last_dab_ms = (time.perf_counter() - t0) * 1000.0
        st.last_write_ms = 0.0
        return len(idx)
