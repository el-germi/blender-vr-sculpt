"""Experimental engine: drive Blender's own sculpt brushes from the controller tip.

Findings from tests/native_probe*.py on 5.4.0 Alpha:
- sculpt.brush_stroke honors each stroke element's 3D "location", so dabs land at
  the controller tip, even on parts hidden from the desktop view.
- The element's "mouse" must still be over the mesh in some 3D viewport, or the
  dab is silently dropped. Nobody watches the desktop view in the headset, so each
  dab points it at the tip from along the surface normal and puts it back (AimedView).
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

from . import brushes, rest

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


EYE_RADII = 4.0  # an aimed view looks at the dab from this many brush radii out...
EYE_CLIPS = 10.0  # ...and at least this many clip starts, so the surface isn't clipped


class AimedView:
    """Point a desktop 3D view at a dab and put it back afterwards (native sculpt and Paint)."""

    def __init__(self, area, region):
        self.region = region
        self.rv3d = area.spaces.active.region_3d
        r = self.rv3d
        self.saved = (r.view_location.copy(), r.view_rotation.copy(), r.view_distance, r.view_perspective)

    def look(self, eye, at):
        r = self.rv3d
        d = at - eye
        r.view_perspective = 'PERSP'
        r.view_rotation = d.to_track_quat('-Z', 'Y')
        r.view_distance = d.length
        r.view_location = at
        r.update()

    def pixels(self, at, radius):
        """radius (world) at point `at` in region pixels, in the current view."""
        side = self.rv3d.view_rotation @ Vector((1.0, 0.0, 0.0))
        a = view3d_utils.location_3d_to_region_2d(self.region, self.rv3d, at)
        b = view3d_utils.location_3d_to_region_2d(self.region, self.rv3d, at + side * radius)
        return (b - a).length if a is not None and b is not None else None

    def mouse(self, point):
        p = view3d_utils.location_3d_to_region_2d(self.region, self.rv3d, point)
        if p is None or not (0 <= p.x < self.region.width and 0 <= p.y < self.region.height):
            return None
        return (p.x, p.y)

    def restore(self):
        r = self.rv3d
        r.view_location, r.view_rotation, r.view_distance, r.view_perspective = self.saved
        r.update()


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


# --- dynamic topology ---------------------------------------------------------------
# Blender's own dyntopo: its settings are the sculpt tool settings, shown on the panel as they are.
# 'RELATIVE' detail is in pixels of the desktop view, which has nothing to do with what you see in
# the headset, so turning dyntopo on from VR switches it to a percentage of the brush. Dyntopo resets
# UVs and corner colors and scrambles shape keys; rest.py gives them back when the session ends.

DETAIL_METHODS = (('BRUSH', "Brush"), ('CONSTANT', "Constant"), ('MANUAL', "Manual"))
REFINE_METHODS = (('SUBDIVIDE', "Split"), ('COLLAPSE', "Collapse"), ('SUBDIVIDE_COLLAPSE', "Both"))
DETAIL_EDGE_MIN = 0.001  # Constant / Manual detail: edge length in meters (1 / constant_detail_resolution)
DETAIL_EDGE_MAX = 0.05
MESSAGE_SECONDS = 6.0
AUTO_TEXTURE_SECONDS = 2.0  # Auto texture: this long after the last dyntopo stroke

# stale: a dyntopo stroke has run since the texture was last shown (strokes leave the patches they
# touch without UVs on screen, inside Blender's sculpt mesh, out of Python's reach).
_dyntopo = {"message": ("", 0.0), "stale": False, "stroke_end": 0.0}


def message():
    text, when = _dyntopo["message"]
    return text if text and time.monotonic() - when < MESSAGE_SECONDS else ""


def _say(text):
    _dyntopo["message"] = (text, time.monotonic())
    print("VR Sculpt dyntopo:", text, flush=True)
    return text


def _sculpt_object(context):
    obj = context.view_layer.objects.active
    return obj if obj is not None and obj.type == 'MESH' and obj.mode == 'SCULPT' else None


def dyntopo_enabled(context):
    obj = _sculpt_object(context)
    return obj is not None and obj.use_dynamic_topology_sculpting


def _run_in_view(context, op):
    """Run a sculpt operator from any context (XR operators have no area)."""
    windows = context.window_manager.windows
    window = context.window or (windows[0] if len(windows) else None)
    view = largest_view3d(window) if window is not None else None
    if view is None:
        if not bpy.app.background:
            return None
        return op()  # Tests: no window. (Never Fill here: detail_flood_fill crashes Blender without one.)
    with context.temp_override(window=window, area=view[0], region=view[1]):
        return op()


def toggle_dyntopo(context):
    """Turn dyntopo on or off for the mesh in Sculpt Mode, keeping its UVs, corner colors and shape keys."""
    obj = _sculpt_object(context)
    if obj is None:
        return _say("Select a mesh in Sculpt Mode")
    turning_on = not obj.use_dynamic_topology_sculpting
    if turning_on:
        if any(m.type == 'MULTIRES' and m.show_viewport for m in obj.modifiers):
            return _say("Dyntopo can't sculpt Multires levels")
        sculpt = context.tool_settings.sculpt
        if sculpt.detail_type_method == 'RELATIVE':
            sculpt.detail_type_method = 'BRUSH'
        rest.prepare(obj)
    if _run_in_view(context, bpy.ops.sculpt.dynamic_topology_toggle) is None:
        return _say("Dyntopo needs a 3D viewport in the desktop window")
    obj.data[rest.DYNTOPO] = turning_on
    rest.sync(obj)  # Off: the corners come back now.
    _dyntopo["message"] = ("", 0.0)
    return ""


def texture_restorable(context):
    """Whether refresh_texture() has anything to show: dyntopo on, and a rest.py session open."""
    obj = _sculpt_object(context)
    return obj is not None and obj.use_dynamic_topology_sculpting and rest.is_open(obj.data)


def refresh_texture(context):
    """Show the texture again mid-session: leave Sculpt Mode (Blender writes the mesh, rest.py gives the
    UVs back) and come back in, where dyntopo draws the restored mesh textured. A short freeze."""
    obj = _sculpt_object(context)
    if obj is None or not texture_restorable(context):
        return _say("Nothing to show: dyntopo is off, or the mesh has no UVs")
    t0 = time.perf_counter()

    def round_trip():
        bpy.ops.object.mode_set(mode='OBJECT')
        rest.sync(obj)
        rest.prepare(obj)
        bpy.ops.object.mode_set(mode='SCULPT')  # Blender turns dyntopo back on.
        rest.sync(obj)
        return {'FINISHED'}

    if _run_in_view(context, round_trip) is None:
        return _say("Needs a 3D viewport in the desktop window")
    _dyntopo["stale"] = False
    print(f"VR Sculpt dyntopo: texture shown in {(time.perf_counter() - t0) * 1000:.0f} ms"
          f" (dyntopo {'on' if obj.use_dynamic_topology_sculpting else 'OFF'})", flush=True)
    return ""


def auto_texture(context, settings, busy):
    """From the live timer: Auto texture, once the hands have been off the trigger long enough."""
    if not (settings.dyntopo_auto_texture and _dyntopo["stale"]) or busy:
        return
    if time.monotonic() - _dyntopo["stroke_end"] < AUTO_TEXTURE_SECONDS:
        return
    _dyntopo["stale"] = False
    if texture_restorable(context):
        refresh_texture(context)


def detail_value(context):
    """The detail slider's value: a fraction of the brush for 'BRUSH', an edge length in meters for
    'CONSTANT' / 'MANUAL', None for 'RELATIVE' (desktop pixels)."""
    sculpt = context.tool_settings.sculpt
    method = sculpt.detail_type_method
    if method == 'BRUSH':
        return sculpt.detail_percent / 100.0
    if method in {'CONSTANT', 'MANUAL'}:
        return 1.0 / max(sculpt.constant_detail_resolution, 1e-6)
    return None


def set_detail_value(context, value):
    sculpt = context.tool_settings.sculpt
    if sculpt.detail_type_method == 'BRUSH':
        sculpt.detail_percent = min(max(value * 100.0, 0.5), 100.0)
    elif sculpt.detail_type_method in {'CONSTANT', 'MANUAL'}:
        sculpt.constant_detail_resolution = 1.0 / max(value, 1e-6)


def flood_fill(context):
    """Remesh the whole mesh to the constant detail (Blender's Detail Flood Fill)."""
    if not dyntopo_enabled(context):
        return _say("Turn dyntopo on first")
    if context.tool_settings.sculpt.detail_type_method not in {'CONSTANT', 'MANUAL'}:
        return _say("Fill needs Constant or Manual detail")
    if bpy.app.background:
        return _say("Fill needs the desktop window")
    if _run_in_view(context, bpy.ops.sculpt.detail_flood_fill) is None:
        return _say("Fill needs a 3D viewport in the desktop window")
    return ""


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
        # Dyntopo sculpts a BMesh and writes obj.data back only when leaving Sculpt Mode, so the
        # snapshot can be far behind the surface (see update()).
        self.dyntopo = obj.use_dynamic_topology_sculpting
        self.radius_world = radius_world
        self.invert = invert
        self.spacing = spacing
        self.last_local = None
        self.last_elem = None
        self.last_world = None
        self.dabs = 0
        self.dropped = 0
        self.touched = 0
        # The locked brush size is in object space too, so undo the object's scale.
        self.size = _SceneSize(context, self.state.local_radius(radius_world))

    def finish(self):
        self.size.restore()
        if self.dyntopo and self.dabs:
            _dyntopo["stale"] = True
            _dyntopo["stroke_end"] = time.monotonic()
        if self.dabs or self.dropped:
            print(f"VR Sculpt native stroke: {self.dabs} dabs, {self.dropped} dropped"
                  f"{' (dyntopo)' if self.dyntopo else ''}", flush=True)

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
        if len(idx):
            normal = (st.normals[idx] * w[:, None]).sum(axis=0)
        elif self.dyntopo:
            # The surface may have grown away from the stale snapshot: dab anyway, looking along the
            # snapshot's nearest vertex normal. Blender sculpts the real surface around "location",
            # and does nothing when the tip is in the air.
            normal = st.normals[int(np.argmin(np.einsum("ij,ij->i", st.co - tip, st.co - tip)))]
        else:
            self.last_elem = None
            return 0
        context = bpy.context
        window = context.window or self.window
        view = largest_view3d(window)
        if view is None:
            self.dropped += 1
            return 0
        area, region = view
        tip_w = Vector(tip_world)
        n_w = Vector(st.world_inv[:3, :3].T @ normal)
        if n_w.length < 1e-9:
            n_w = Vector((0.0, 0.0, 1.0))
        # The mouse only has to be over the mesh, as seen from the desktop view. That view can be
        # looking anywhere while you're in the headset, so each dab looks at the tip from outside,
        # along the surface normal (the mouse at the region center), and puts the view back after.
        eye = max(EYE_RADII * self.radius_world, EYE_CLIPS * area.spaces.active.clip_start)
        v = AimedView(area, region)
        try:
            v.look(tip_w + n_w.normalized() * eye, tip_w)
            mouse = v.mouse(tip_w)
            if mouse is None:
                self.dropped += 1
                return 0
            # Sculpt mode works in object space: "location" is local, not world.
            elem = {"name": "", "location": tuple(tip), "mouse": mouse, "mouse_event": mouse,
                    "pressure": float(pressure), "size": 50.0, "x_tilt": 0.0, "y_tilt": 0.0,
                    "time": 0.0, "is_start": True}
            elems = [elem]
            lead_mouse = v.mouse(self.last_world) if self.last_elem is not None else None
            if lead_mouse is not None:
                # Zero-pressure lead-in gives directional brushes a stroke direction.
                lead = dict(self.last_elem, pressure=0.0, is_start=True, mouse=lead_mouse, mouse_event=lead_mouse)
                elems = [lead, dict(elem, is_start=False)]
            with context.temp_override(window=window, area=area, region=region):
                if not self._can_stroke():
                    self.dropped += 1
                    return 0
                bpy.ops.sculpt.brush_stroke(stroke=elems, mode='INVERT' if self.invert else 'NORMAL')
        finally:
            v.restore()
        self.last_elem = elem
        self.last_world = tip_w
        self.dabs += 1
        self.touched = len(idx)
        st.last_dab_ms = (time.perf_counter() - t0) * 1000.0
        st.last_write_ms = 0.0
        return len(idx)
