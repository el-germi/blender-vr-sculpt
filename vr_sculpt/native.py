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
- Drag brushes (Grab, Snake Hook, Pose...) move the mesh by the mouse's motion on screen between the
  elements, unprojected at the stroke's depth: motion along the view is lost, and each call grabs anew
  where it starts. So they get their own dabs (NativeStroke._drag_dab, tests/native_drag_probe*.py).
"""

import time

import bpy
import numpy as np
from bpy_extras import view3d_utils
from mathutils import Vector

from . import brushes, rest, shading

# Essentials brushes the "next brush" button cycles through in native mode (the panel's quick buttons).
NATIVE_BRUSHES = ("Draw", "Clay Strips", "Smooth", "Inflate/Deflate", "Crease Sharp", "Flatten/Contrast")
_ESSENTIALS = "brushes/essentials_brushes-mesh_sculpt.blend/Brush/"

# Every essentials sculpt brush the panel's brush list offers: (name, short label), by Blender's own asset
# shelf catalog. Left out because one short call per controller sample can't drive them (see
# tests/native_drag_probe*.py): the cloth brushes that grab (Grab / Grab Planar / Grab Random Cloth,
# Bend/Twist and Stretch/Move Cloth, the boundary cloth ones), Grab Silhouette (works from the desktop
# view's silhouette), Twist (turns with the mouse around where the stroke began) and Boundary (its first
# dab writes NaN positions; unusable() refuses it).
CATALOG = (
    ("Add & Subtract", (
        ("Draw", "Draw"), ("Draw Sharp", "Sharp"), ("Clay", "Clay"), ("Clay Strips", "Strips"),
        ("Clay Thumb", "Thumb"), ("Layer", "Layer"), ("Inflate/Deflate", "Inflate"), ("Blob", "Blob"),
        ("Crease Sharp", "Crease"), ("Crease Polish", "Polish"))),
    ("Contrast", (
        ("Smooth", "Smooth"), ("Flatten/Contrast", "Flatten"), ("Fill/Deepen", "Fill"), ("Scrape/Fill", "Scrape"),
        ("Scrape Multiplane", "Multiplane"), ("Plateau", "Plateau"), ("Trim", "Trim"))),
    ("Transform", (
        ("Snake Hook", "Snake Hook"), ("Elastic Snake Hook", "Elastic Hook"), ("Pull", "Pull"), ("Grab", "Grab"),
        ("Grab 2D", "Grab 2D"), ("Elastic Grab", "Elastic Grab"), ("Pose", "Pose"), ("Thumb", "Thumb"),
        ("Nudge", "Nudge"), ("Pinch/Magnify", "Pinch"), ("Relax Slide", "Slide"),
        ("Relax Pinch", "Relax"), ("Scene Project", "Project"))),
    ("Utilities", (
        ("Mask", "Mask"), ("Face Set Paint", "Face Sets"), ("Density", "Density"),
        ("Erase Multires Displacement", "Erase Multires"), ("Smear Multires Displacement", "Smear Multires"))),
    ("Paint", (
        ("Paint Hard", "Hard"), ("Paint Soft", "Soft"), ("Paint Square", "Square"),
        ("Paint Hard Pressure", "Hard Pressure"), ("Paint Soft Pressure", "Soft Pressure"),
        ("Paint Blend", "Blend"), ("Airbrush", "Airbrush"), ("Blend Hard", "Blend Hard"),
        ("Blend Soft", "Blend Soft"), ("Blend Square", "Blend Square"), ("Blur", "Blur"), ("Smear", "Smear"),
        ("Sharpen", "Sharpen"))),
    ("Simulation", (
        ("Drag Cloth", "Drag"), ("Push Cloth", "Push"), ("Pinch Point Cloth", "Pinch Point"),
        ("Pinch Folds Cloth", "Pinch Folds"), ("Inflate Cloth", "Inflate"), ("Expand/Contract Cloth", "Expand"))),
)
CATALOG_NAMES = tuple(n for _cat, brushes_ in CATALOG for n, _short in brushes_)
SHORT = {n: short for _cat, brushes_ in CATALOG for n, short in brushes_}
CATEGORY = {n: cat for cat, brushes_ in CATALOG for n, _short in brushes_}

# Brushes that move the mesh with the mouse (sculpt_brush_type), driven by NativeStroke._drag_dab.
DRAG_TYPES = frozenset({'GRAB', 'SNAKE_HOOK', 'ELASTIC_DEFORM', 'THUMB', 'NUDGE', 'POSE'})
DRAG_CLOTH = frozenset({'DRAG', 'PINCH_POINT'})  # cloth_deform_type of the cloth brushes that drag


def unusable(brush, obj):
    """Why brush can't sculpt obj now ("" if it can). A Multires brush on a mesh without Multires crashes
    Blender 5.4 Alpha (do_displacement_eraser_brush), so these never get a dab."""
    if brush is None:
        return "No brush"
    kind = brush.sculpt_brush_type
    if kind in {'DISPLACEMENT_ERASER', 'DISPLACEMENT_SMEAR'} and not any(
            m.type == 'MULTIRES' and m.show_viewport for m in obj.modifiers):
        return f"{SHORT.get(brush.name, brush.name)} needs a Multires modifier"
    if kind == 'SIMPLIFY' and not obj.use_dynamic_topology_sculpting:
        return "Density needs dyntopo (More > Dyntopo)"
    if kind == 'BOUNDARY':  # Driven a dab per sample, it breaks the mesh (NaN positions).
        return f"{brush.name} doesn't work in VR yet"
    return ""


def is_drag(brush):
    """Whether brush moves the mesh with the hand (Grab, Snake Hook, Pose...) rather than dabbing on it."""
    if brush is None:
        return False
    kind = brush.sculpt_brush_type
    return kind in DRAG_TYPES or (kind == 'CLOTH' and brush.cloth_deform_type in DRAG_CLOTH)


# --- brush thumbnails -----------------------------------------------------------------
# The panel's brush buttons show the essentials brushes' own previews (the pictures in Blender's brush
# asset shelf). They're read from the essentials .blend into a temporary Main, so nothing is linked into
# the open file; one read per .blend (about 0.2 s), the first time a button needs one.

ESSENTIALS_FILES = {'SCULPT': "essentials_brushes-mesh_sculpt.blend",
                    'TEXTURE': "essentials_brushes-mesh_texture.blend"}
ICON_PX = 128  # previews are 146 to 256 px; drawn about 15 mm wide, this is plenty
_icon_pixels = {}  # (kind, name) -> (w, h, float32 RGBA, bottom row first) or None (no preview)
_icons = {}  # (kind, name) -> BrushIcon


def _read_previews(kind, names):
    """Read the previews of names from kind's essentials .blend into _icon_pixels."""
    import os
    t0 = time.perf_counter()
    path = os.path.join(bpy.utils.system_resource('DATAFILES', path="assets/brushes"), ESSENTIALS_FILES[kind])
    for n in names:
        _icon_pixels[(kind, n)] = None
    try:
        with bpy.data.temp_data() as temp:
            with temp.libraries.load(path, link=True) as (src, dst):
                dst.brushes = [n for n in names if n in src.brushes]
            for b in dst.brushes:
                p = b.preview if b is not None else None
                if p is None or not p.image_size[0]:
                    continue
                w, h = p.image_size
                px = np.empty(w * h * 4, np.float32)
                p.image_pixels_float.foreach_get(px)
                px = px.reshape(h, w, 4)
                step = max(1, max(w, h) // ICON_PX)
                px = np.ascontiguousarray(px[::step, ::step])
                _icon_pixels[(kind, b.name)] = (px.shape[1], px.shape[0], px.ravel())
    except Exception as ex:  # A build without the essentials, or a changed file: buttons just go without.
        print("VR Sculpt: couldn't read the brush previews:", ex, flush=True)
    print(f"VR Sculpt: brush previews from {ESSENTIALS_FILES[kind]} in {(time.perf_counter() - t0) * 1000:.0f} ms",
          flush=True)


class BrushIcon:
    """An essentials brush's preview as a GPU texture, made on first draw. Hashable, so panels can compare
    widgets that hold one (see vr_ui.Widget.image)."""

    def __init__(self, kind, name, siblings):
        self.key = (kind, name)
        self.siblings = siblings  # read together: one pass over the .blend for the whole row
        self.tex = None
        self.missing = False

    def __hash__(self):
        return hash(self.key)

    def __eq__(self, other):
        return isinstance(other, BrushIcon) and other.key == self.key

    def pixels(self):
        if self.key not in _icon_pixels:
            _read_previews(self.key[0], [n for n in self.siblings if (self.key[0], n) not in _icon_pixels])
        return _icon_pixels.get(self.key)

    def texture(self):
        if self.tex is not None or self.missing:
            return self.tex
        px = self.pixels()
        if px is None:
            self.missing = True
            return None
        import gpu
        w, h, data = px
        self.tex = gpu.types.GPUTexture((w, h), format='RGBA8', data=gpu.types.Buffer('FLOAT', data.size, data))
        return self.tex


def brush_icon(name, kind='SCULPT', siblings=NATIVE_BRUSHES):
    """The BrushIcon of essentials brush name (kind 'SCULPT' or 'TEXTURE'); siblings: the other brushes
    shown with it, read in the same pass."""
    icon = _icons.get((kind, name))
    if icon is None:
        icon = _icons[(kind, name)] = BrushIcon(kind, name, tuple(siblings) + (name,))
    return icon


def forget_icons():
    """Drop the textures (they belong to the GPU context, which goes with unregister)."""
    _icons.clear()


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

    def look(self, eye, at, ortho=False):
        r = self.rv3d
        d = at - eye
        r.view_perspective = 'ORTHO' if ortho else 'PERSP'
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


def pick_brush(context, name):
    """A brush from the panel's list: active now, and (not a quick button one) kept beside the quick buttons and
    in the B cycle."""
    activate_brush(context, name)
    if name not in NATIVE_BRUSHES:
        context.scene.vr_sculpt.native_picked = name
    return name


DEFAULT_PICKED = "Snake Hook"  # the seventh quick button until a brush is picked from the list


def picked_brush(context):
    """The brush last picked from the list (a quick button of its own), Snake Hook until one is."""
    picked = context.scene.vr_sculpt.native_picked
    return picked if picked in CATALOG_NAMES else DEFAULT_PICKED


def cycle_names(context):
    """What B cycles through: the quick buttons' brushes, the picked one included."""
    picked = picked_brush(context)
    return list(NATIVE_BRUSHES) + ([picked] if picked not in NATIVE_BRUSHES else [])


def cycle_brush(context):
    brush = sculpt_brush(context)
    current = brush.name if brush else ""
    names = cycle_names(context)
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
        shading.match_for_dyntopo(obj)
        rest.prepare(obj)
    if _run_in_view(context, bpy.ops.sculpt.dynamic_topology_toggle) is None:
        return _say("Dyntopo needs a 3D viewport in the desktop window")
    obj.data[rest.DYNTOPO] = turning_on
    rest.sync(obj)  # Off: the corners come back now.
    _dyntopo["message"] = ("", 0.0)
    return ""


def texture_restorable(context):
    """Whether refresh_texture() has anything to show: dyntopo on, and a rest.py session open on a mesh
    with corner layers (one with only materials has a session too)."""
    obj = _sculpt_object(context)
    return (obj is not None and obj.use_dynamic_topology_sculpting and rest.is_open(obj.data)
            and rest.has_corners(obj.data))


def refresh_texture(context):
    """Show the texture again mid-session: leave Sculpt Mode (Blender writes the mesh, rest.py gives the
    UVs back) and come back in, where dyntopo draws the restored mesh textured. A short freeze."""
    obj = _sculpt_object(context)
    if obj is None or not texture_restorable(context):
        return _say("Nothing to show: dyntopo is off, or the mesh has no UVs")
    t0 = time.perf_counter()

    # Undo steps for both switches and the restored mesh between them: an undo then stops on the whole
    # mesh before going into the previous session (which Blender brings back bare: rest.restore_corners).
    # Not in tests without a window: an undo mode switch with no area crashes Blender.
    undo = not bpy.app.background

    def round_trip():
        bpy.ops.object.mode_set('EXEC_DEFAULT', undo, mode='OBJECT')
        rest.sync(obj)
        rest.prepare(obj)
        if undo:
            bpy.ops.ed.undo_push(message="Show texture")
        bpy.ops.object.mode_set('EXEC_DEFAULT', undo, mode='SCULPT')  # Blender turns dyntopo back on.
        rest.sync(obj)
        return {'FINISHED'}

    if _run_in_view(context, round_trip) is None:
        return _say("Needs a 3D viewport in the desktop window")
    _dyntopo["stale"] = False
    print(f"VR Sculpt dyntopo: texture shown in {(time.perf_counter() - t0) * 1000:.0f} ms"
          f" (dyntopo {'on' if obj.use_dynamic_topology_sculpting else 'OFF'})", flush=True)
    return ""


def smooth_for_lines(context, obj):
    """The hand's lines read reflections, which flat faces don't have: each one shows a single tone. A mesh with its
    own shading and flat faces goes smooth (VR Shading is left to decide). Dyntopo keeps its own mesh, so in a
    session this leaves Sculpt Mode and comes back, like refresh_texture. True if the faces changed."""
    me = obj.data
    if shading.mode(obj) is not None or not len(me.polygons):
        return False
    smooth = np.zeros(len(me.polygons), dtype=bool)
    me.polygons.foreach_get("use_smooth", smooth)
    if smooth.all():
        return False

    def set_smooth():
        me.polygons.foreach_set("use_smooth", np.ones(len(me.polygons), dtype=bool))
        me.update()

    if obj.mode != 'SCULPT' or not obj.use_dynamic_topology_sculpting:
        set_smooth()
    else:
        undo = not bpy.app.background  # As in refresh_texture.

        def round_trip():
            bpy.ops.object.mode_set('EXEC_DEFAULT', undo, mode='OBJECT')
            rest.sync(obj)
            set_smooth()
            rest.prepare(obj)
            if undo:
                bpy.ops.ed.undo_push(message="Smooth for lines")
            bpy.ops.object.mode_set('EXEC_DEFAULT', undo, mode='SCULPT')
            rest.sync(obj)
            return {'FINISHED'}

        if _run_in_view(context, round_trip) is None:
            _say("Lines need smooth faces: needs a 3D viewport in the desktop window")
            return False
    _say("Faces set smooth for the lines")
    return True


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
        why = unusable(sculpt_brush(context), obj)
        if why:
            _say(why)  # On the panel's header, where you look in the headset.
            raise RuntimeError(why)
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
        # Drag brushes: the grabbed point (local), the vertex it is when it can be read back, and the surface
        # normal there at the start (world). The point follows the vertex, or moves by the drag times the
        # strength where Python can't see the sculpted mesh (dyntopo, Multires, a shape key).
        brush = sculpt_brush(context)
        self.drag = is_drag(brush)
        self.anchor = None
        self.anchor_vert = None
        self.anchor_nbrs = []
        self.anchor_normal = None
        self.obj = obj
        self.tracked = self.drag and _positions_live(obj)
        self.follow = (get_strength(context) or 1.0) if self.drag and not self.tracked else 1.0
        self.uses_pressure = self.drag and bool(brush.use_pressure_strength)
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
        if self.drag:
            return self._drag_update(tip_world, pressure)
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
                # undo=True: each dab is its own sculpt undo step, as Blender's dyntopo log needs (a dab
                # without one leaves the log out of step with the undo stack, and undoing crashes).
                bpy.ops.sculpt.brush_stroke('EXEC_DEFAULT', True, stroke=elems,
                                            mode='INVERT' if self.invert else 'NORMAL')
        finally:
            v.restore()
        self.last_elem = elem
        self.last_world = tip_w
        self.dabs += 1
        self.touched = len(idx)
        st.last_dab_ms = (time.perf_counter() - t0) * 1000.0
        st.last_write_ms = 0.0
        return len(idx)

    # --- drag brushes -------------------------------------------------------------
    # Blender drags by the mouse's motion between a stroke's elements, on screen, unprojected at the
    # stroke's depth, and each sculpt.brush_stroke call is a stroke of its own. So for each step of the tip:
    # - the dab starts where the grabbed point is now, not at the tip: with a strength under 1 the
    #   grabbed part lags, and grabbing anew at the tip would leave it a little further behind each time;
    # - the view is orthographic (screen motion maps linearly to the drag) and looks across the step, so
    #   all of it is on screen: along the surface normal it would be lost (pulling straight out);
    # - the lead mouse is over the grabbed vertex, so its ray hits the mesh wherever the view looks from;
    # - a step close to the start normal is two dabs at 45 degrees, so the view never looks along the surface
    #   edge-on (an open sheet's edge pulled up).

    def _drag_update(self, tip_world, pressure):
        st = self.state
        tip = st.to_local(tip_world)
        r = st.local_radius(self.radius_world)
        if self.anchor is None:
            return self._grab(tip, r)
        delta = tip - self.last_local
        if np.linalg.norm(delta) < r * self.spacing:
            return 0
        self.last_local = tip
        t0 = time.perf_counter()
        done = 0
        for part in self._drag_parts(delta):
            done += self._drag_dab(part, pressure)
        st.last_dab_ms = (time.perf_counter() - t0) * 1000.0
        st.last_write_ms = 0.0
        return done

    def _grab(self, tip, r):
        """First touch: take hold of the vertex nearest the tip (within the radius). Returns 0: no dab yet."""
        st = self.state
        idx, _w = st.select(tip, r)
        if len(idx):
            d = st.co[idx] - tip
            k = int(idx[np.argmin(np.einsum("ij,ij->i", d, d))])
        elif self.dyntopo and len(st.co):
            # The surface may have grown away from the stale snapshot (an earlier stroke's strand): hold the
            # tip itself, with the nearest snapshot normal.
            k = int(np.argmin(np.einsum("ij,ij->i", st.co - tip, st.co - tip)))
        else:
            return 0
        self.anchor = st.co[k].copy() if len(idx) else tip.copy()
        self.anchor_vert = k if self.tracked and len(idx) else None
        if self.anchor_vert is not None:
            # Its edge neighbors: one read of the edge list and a compare, not a whole adjacency.
            edges = self.obj.data.edges
            ev = np.empty(len(edges) * 2, dtype=np.int32)
            edges.foreach_get("vertices", ev)
            ev = ev.reshape(-1, 2)
            hit = (ev == k).any(axis=1)
            self.anchor_nbrs = [int(i) for i in ev[hit].ravel() if i != k]
        n = st.world_inv[:3, :3].T @ st.normals[k]
        length = np.linalg.norm(n)
        self.anchor_normal = n / length if length > 1e-12 else np.array((0.0, 0.0, 1.0))
        self.last_local = tip
        return 0

    def _drag_parts(self, delta):
        """The step as local vectors: itself, or two halves at 45 degrees to the start normal when it runs
        close along it (a view across it would look at the surface edge-on)."""
        m3 = self.state.world[:3, :3]
        d = m3 @ delta
        n = self.anchor_normal
        length = np.linalg.norm(d)
        if length < 1e-12:
            return []
        along = float(np.dot(d, n))
        if 1.0 - (along / length) ** 2 >= 0.25:  # at least 30 degrees off the normal
            return [delta]
        dn = n * along
        dt = d - dn
        side = np.cross(n, dt) if np.linalg.norm(dt) > 1e-9 * length else _orthogonal(n)
        side /= np.linalg.norm(side)
        half = abs(along) * 0.5
        inv3 = self.state.world_inv[:3, :3]
        return [inv3 @ (dn * 0.5 + side * half), inv3 @ (dn * 0.5 + dt - side * half)]

    def _drag_dab(self, part, pressure):
        """One dab dragging the grabbed point by part (local). Returns 1 if it was sent, else 0."""
        st = self.state
        context = bpy.context
        window = context.window or self.window
        view = largest_view3d(window)
        if view is None:
            self.dropped += 1
            return 0
        area, region = view
        m3, t = st.world[:3, :3], st.world[:3, 3]
        a_w = Vector(m3 @ self.anchor + t)
        step = Vector(m3 @ part)
        n = Vector(self.anchor_normal)
        m = step.normalized()
        across = n - m * n.dot(m)
        if across.length < 1e-3:
            across = m.orthogonal()
        across.normalize()
        # The ray has to hit the mesh: through a point a little way from the grabbed vertex toward its neighbors,
        # inside the faces around it (through the vertex itself it can slip past an open border). A held point
        # that isn't a vertex (dyntopo) may float above the surface, so its ray goes through a point under it.
        # Either stays within a fraction of the brush, and the view is wide enough for it: on a low-poly mesh
        # (edges longer than the brush) a quarter of the way to the neighbors fell outside the view and the dab
        # was dropped.
        if self.anchor_vert is not None:
            toward = Vector(m3 @ self._neighbor_mean() + t) - a_w
            through = a_w + toward * min(0.25, 0.2 * self.radius_world / max(toward.length, 1e-12))
        else:
            through = a_w - n * self.radius_world * 0.5
        eye = max(EYE_RADII * self.radius_world, 3.0 * step.length, 4.0 * (through - a_w).length,
                  EYE_CLIPS * area.spaces.active.clip_start)
        v = AimedView(area, region)
        try:
            v.look(a_w + across * eye, a_w, ortho=True)
            lead, start, end = v.mouse(through), v.mouse(a_w), v.mouse(a_w + step)
            if lead is None or start is None or end is None:
                self.dropped += 1
                return 0
            mouse = (lead[0] + end[0] - start[0], lead[1] + end[1] - start[1])
            base = {"name": "", "size": 50.0, "x_tilt": 0.0, "y_tilt": 0.0, "time": 0.0}
            elems = [dict(base, location=tuple(self.anchor), mouse=lead, mouse_event=lead, pressure=0.0,
                          is_start=True),
                     dict(base, location=tuple(self.anchor + part), mouse=mouse, mouse_event=mouse,
                          pressure=float(pressure), is_start=False)]
            with context.temp_override(window=window, area=area, region=region):
                if not self._can_stroke():
                    self.dropped += 1
                    return 0
                bpy.ops.sculpt.brush_stroke('EXEC_DEFAULT', True, stroke=elems, mode='NORMAL')
        finally:
            v.restore()
        if self.anchor_vert is not None:
            try:
                self.anchor = np.array(self.obj.data.vertices[self.anchor_vert].co, dtype=np.float64)
            except (ReferenceError, IndexError):
                self.anchor_vert = None
                self.anchor = self.anchor + part
        else:
            follow = self.follow * (pressure if self.uses_pressure else 1.0)
            self.anchor = self.anchor + part * follow
        self.dabs += 1
        self.touched = 1
        return 1

    def _neighbor_mean(self):
        """Where the grabbed vertex's edge neighbors are now, on average (local); the vertex if it has none."""
        verts = self.obj.data.vertices
        pts = [verts[i].co for i in self.anchor_nbrs]
        if not pts:
            return self.anchor
        return np.mean(np.array(pts, dtype=np.float64), axis=0)


def _orthogonal(n):
    """A unit vector perpendicular to n (numpy)."""
    axis = np.array((1.0, 0.0, 0.0)) if abs(n[0]) < 0.9 else np.array((0.0, 1.0, 0.0))
    o = np.cross(n, axis)
    return o / np.linalg.norm(o)


def _positions_live(obj):
    """Whether Sculpt Mode writes its dabs straight to the mesh's positions, where a vertex can be read back:
    not with dyntopo (a BMesh written at the end), Multires (its grids) or a shape key other than the basis."""
    if obj.use_dynamic_topology_sculpting:
        return False
    if any(m.type == 'MULTIRES' and m.show_viewport for m in obj.modifiers):
        return False
    keys = obj.data.shape_keys
    return keys is None or obj.active_shape_key_index == 0
