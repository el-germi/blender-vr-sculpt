"""Paint mode: Blender's own texture paint brushes, driven from the controller.

Findings from tests/probe_texpaint*.py on 5.4.0 Alpha:
- paint.image_paint projects each dab along the 3D view: it paints where the element's
  "mouse" position looks at, with occlusion from that view. So for every dab we point the
  largest desktop 3D view at the dab (from along the surface normal for Contact, from the
  controller for Spray), paint at the region center, and put the view back right away.
- The element's "size" is the brush radius in pixels, in that view: we convert the radius
  from hand space with the view's own projection. The locked scene size isn't used.
- mode='INVERT' paints with the secondary color.
- The colors used are the unified paint settings' ones, even with Unified Color off.
- Each call is its own undo step (about 1.5 ms per dab). The add-on keeps its own per-stroke
  undo (changed tiles only, see below), so B (or Undo stroke) takes back a whole stroke.
"""

import colorsys
import time

import bpy
import numpy as np
from bpy_extras import view3d_utils
from mathutils import Vector
from mathutils.geometry import barycentric_transform, intersect_point_tri

from . import native

ESSENTIALS = "brushes/essentials_brushes-mesh_texture.blend/Brush/"
BRUSHES = ("Paint Hard", "Paint Soft", "Airbrush", "Blur", "Smear", "Erase Soft")
SHORT = {"Paint Hard": "Hard", "Paint Soft": "Soft", "Airbrush": "Airbrush", "Blur": "Blur", "Smear": "Smear",
         "Erase Soft": "Erase"}
INFO = {
    "Paint Hard": "Solid color with a crisp edge. Left hand: second color",
    "Paint Soft": "Solid color with a soft edge. Left hand: second color",
    "Airbrush": "Builds up while you hold still. Left hand: second color",
    "Blur": "Softens the texture under the brush",
    "Smear": "Drags the texture's colors along your stroke",
    "Erase Soft": "Erases to transparent (needs an image with alpha)",
}
EYE_DISTANCE = 0.25  # hand-space meters between the temporary view and the dab
SPACING = 0.2  # dab spacing as a fraction of the radius
MAX_DABS_PER_UPDATE = 24
TRIGGER_THRESHOLD = 0.3
HISTORY_BYTES = 256 * 1024 * 1024  # memory kept for undoing strokes (changed tiles only)


# --- brush and colors --------------------------------------------------------

def image_paint(context):
    return context.tool_settings.image_paint


def brush(context):
    return image_paint(context).brush


def activate_brush(context, name):
    window = context.window or context.window_manager.windows[0]
    view = native.largest_view3d(window)
    with context.temp_override(window=window, area=view[0], region=view[1]):
        bpy.ops.brush.asset_activate(asset_library_type='ESSENTIALS', relative_asset_identifier=ESSENTIALS + name)
    return name


def cycle_brush(context):
    b = brush(context)
    names = list(BRUSHES)
    current = b.name if b else ""
    return activate_brush(context, names[(names.index(current) + 1) % len(names)] if current in names else names[0])


def color_owner(context):
    """Where the paint colors are read from. 5.4 Alpha paints with the unified settings' colors
    even when Unified Color is off, so those are the source of truth."""
    return image_paint(context).unified_paint_settings


def get_color(context, secondary=False):
    owner = color_owner(context)
    return tuple(owner.secondary_color if secondary else owner.color)


def set_color(context, rgb, secondary=False):
    """Set a paint color on the unified settings and the brush (in case a build reads the brush)."""
    for owner in (color_owner(context), brush(context)):
        if owner is None:
            continue
        if secondary:
            owner.secondary_color = rgb
        else:
            owner.color = rgb


def swap_colors(context):
    a, b = get_color(context), get_color(context, True)
    set_color(context, b)
    set_color(context, a, True)


def get_hsv(context):
    return colorsys.rgb_to_hsv(*get_color(context))


def set_hsv(context, h=None, s=None, v=None):
    h0, s0, v0 = get_hsv(context)
    set_color(context, colorsys.hsv_to_rgb(h0 if h is None else h, s0 if s is None else s, v0 if v is None else v))


# --- the object and its texture ----------------------------------------------

def paint_image(obj):
    """The image Paint mode paints on: the material's active paint slot, or None."""
    if obj is None or obj.type != 'MESH':
        return None
    ip = bpy.context.tool_settings.image_paint
    if ip.mode == 'IMAGE':
        return ip.canvas
    mat = obj.active_material
    if mat is None:
        return None
    if len(mat.texture_paint_images):
        return mat.texture_paint_images[min(mat.paint_active_slot, len(mat.texture_paint_images) - 1)]
    # The slot list is only refreshed by Texture Paint mode; look at the nodes before that.
    if mat.node_tree is None:
        return None
    nodes = mat.node_tree.nodes
    active = nodes.active
    if active is not None and active.type == 'TEX_IMAGE' and active.image is not None:
        return active.image
    return next((n.image for n in nodes if n.type == 'TEX_IMAGE' and n.image is not None), None)


def ready(obj):
    return obj is not None and obj.type == 'MESH' and len(obj.data.uv_layers) > 0 and paint_image(obj) is not None


def target(context):
    """The mesh Paint mode paints on (the active object), or None."""
    obj = context.view_layer.objects.active
    if obj is None or obj.type != 'MESH' or not obj.visible_get():
        return None
    return obj


def _override(context):
    window = context.window or context.window_manager.windows[0]
    view = native.largest_view3d(window)
    if view is None:
        raise RuntimeError("Painting needs a 3D viewport in the desktop window")
    return dict(window=window, area=view[0], region=view[1])


def setup(context, obj, size=2048):
    """Give obj what texture painting needs: UVs (Smart UV Project) and an image in a paint slot."""
    from . import menus
    ov = _override(context)
    if not len(obj.data.uv_layers):
        menus.set_object_mode(context, obj, 'EDIT')
        with context.temp_override(**ov, active_object=obj, object=obj, edit_object=obj):
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.uv.smart_project(angle_limit=1.15192, island_margin=0.01)
    menus.set_object_mode(context, obj, 'TEXTURE_PAINT')
    if paint_image(obj) is None:
        with context.temp_override(**ov, active_object=obj, object=obj):
            bpy.ops.paint.add_texture_paint_slot(type='BASE_COLOR', slot_type='IMAGE', name=obj.name + " Paint",
                                                 color=(0.8, 0.8, 0.8, 1.0), width=size, height=size, alpha=True)
    return ready(obj)


def enter(context):
    """Switch the active mesh to Texture Paint mode if it's ready for painting."""
    from . import menus
    obj = target(context)
    if obj is not None and ready(obj):
        menus.set_object_mode(context, obj, 'TEXTURE_PAINT')
        if brush(context) is None:
            activate_brush(context, BRUSHES[0])
    else:
        menus.set_object_mode(context, obj, 'OBJECT')


# --- surface queries -------------------------------------------------------

def surface_near(obj, point, radius):
    """(world point, world normal, face index) of the surface closest to point, if within radius."""
    inv = obj.matrix_world.inverted_safe()
    ok, loc, normal, face = obj.closest_point_on_mesh(inv @ Vector(point))
    if not ok:
        return None
    world = obj.matrix_world @ loc
    if (world - Vector(point)).length > radius:
        return None
    n = (inv.transposed().to_3x3() @ normal).normalized()
    return world, n, face


def surface_along(obj, origin, direction):
    """(world point, world normal, face index) where a ray hits obj, or None."""
    inv = obj.matrix_world.inverted_safe()
    o = inv @ Vector(origin)
    d = (inv.to_3x3() @ Vector(direction)).normalized()
    ok, loc, normal, face = obj.ray_cast(o, d)
    if not ok:
        return None
    n = (inv.transposed().to_3x3() @ normal).normalized()
    return obj.matrix_world @ loc, n, face


def uv_at(obj, world_point, face):
    """UV of a surface point on face (active UV map)."""
    mesh = obj.data
    local = obj.matrix_world.inverted_safe() @ Vector(world_point)
    poly = mesh.polygons[face]
    uvl = mesh.uv_layers.active.data
    loops = list(poly.loop_indices)
    cos = [mesh.vertices[mesh.loops[i].vertex_index].co for i in loops]
    uvs = [uvl[i].uv.to_3d() for i in loops]
    best = None
    for k in range(1, len(loops) - 1):
        a, b, c = cos[0], cos[k], cos[k + 1]
        if intersect_point_tri(local, a, b, c) or best is None:
            best = barycentric_transform(local, a, b, c, uvs[0], uvs[k], uvs[k + 1]).xy
            if intersect_point_tri(local, a, b, c):
                break
    return best


def sample_color(obj, world_point, face):
    """The paint image's color at a surface point (RGB), or None."""
    img = paint_image(obj)
    if img is None or not img.size[0]:
        return None
    uv = uv_at(obj, world_point, face)
    w, h = img.size
    x = int(uv.x % 1.0 * w) % w
    y = int(uv.y % 1.0 * h) % h
    px = np.empty(w * h * 4, dtype=np.float32)
    img.pixels.foreach_get(px)
    i = (y * w + x) * 4
    return tuple(float(c) for c in px[i:i + 3])


# --- undo per VR stroke ----------------------------------------------------
#
# A baseline copy of the paint image (8-bit for byte images) is kept between strokes. When a
# stroke ends, the 64 x 64 tiles that changed are compared against it, and only their "before"
# pixels go into the history; then the baseline moves on. A stroke typically costs well under a
# megabyte of history, so many strokes can be undone. Undo writes those tiles back.
#
# This undo is separate from Blender's: it never pushes Blender undo steps (doing that from VR
# operators in Texture Paint mode is a likely way to upset Blender's own image undo). After a
# Blender undo or redo the baseline is re-read on the next stroke.

TILE = 64
_history = []  # [{"image", "size", "tiles": [(ty, tx, before array)], "nbytes"}], oldest first
_base = {"image": None, "size": None, "pixels": None}


def _read(img):
    """The image's pixels as (h, w, 4): uint8 for byte images, float32 for float images."""
    w, h = img.size
    px = np.empty(w * h * 4, dtype=np.float32)
    img.pixels.foreach_get(px)
    px = px.reshape(h, w, 4)
    if img.is_float:
        return px
    px *= 255.0
    np.rint(px, out=px)
    return px.astype(np.uint8)


def _write(img, arr):
    px = arr.astype(np.float32)
    if arr.dtype == np.uint8:
        px *= 1.0 / 255.0
    img.pixels.foreach_set(px.ravel())
    img.update()


def _base_matches(img):
    return (_base["pixels"] is not None and _base["image"] == img.name and _base["size"] == tuple(img.size)
            and (_base["pixels"].dtype == np.float32) == bool(img.is_float))


def ensure_base(img):
    """Make sure the baseline is the image as it is now (before a stroke paints on it)."""
    if not _base_matches(img):
        _base.update(image=img.name, size=tuple(img.size), pixels=_read(img))


def changed_tiles(before, after, tile=TILE):
    """[(ty, tx)] of the tiles where two (h, w, 4) arrays differ."""
    h, w = before.shape[:2]
    diff = np.any(before != after, axis=2)
    ny, nx = -(-h // tile), -(-w // tile)
    padded = np.zeros((ny * tile, nx * tile), dtype=bool)
    padded[:h, :w] = diff
    mask = padded.reshape(ny, tile, nx, tile).any(axis=(1, 3))
    return [tuple(t) for t in np.argwhere(mask)]


def commit(img):
    """After a stroke: record the changed tiles' old pixels, and move the baseline on."""
    if not _base_matches(img):
        return False
    before = _base["pixels"]
    after = _read(img)
    tiles = [(ty, tx, before[ty * TILE:(ty + 1) * TILE, tx * TILE:(tx + 1) * TILE].copy())
             for ty, tx in changed_tiles(before, after)]
    _base["pixels"] = after
    if not tiles:
        return False
    _history.append({"image": img.name, "size": tuple(img.size), "tiles": tiles,
                     "nbytes": sum(t[2].nbytes for t in tiles)})
    while len(_history) > 1 and sum(e["nbytes"] for e in _history) > HISTORY_BYTES:
        _history.pop(0)
    return True


def history_bytes():
    return sum(e["nbytes"] for e in _history)


def undo_last(context):
    """Put back the tiles the last VR paint stroke changed."""
    while _history:
        entry = _history.pop()
        img = bpy.data.images.get(entry["image"])
        if img is None or tuple(img.size) != entry["size"]:
            continue
        ensure_base(img)
        base = _base["pixels"]
        for ty, tx, before in entry["tiles"]:
            base[ty * TILE:ty * TILE + before.shape[0], tx * TILE:tx * TILE + before.shape[1]] = before
        _write(img, base)
        print(f"VR Paint undo: {len(entry['tiles'])} tiles restored on {img.name}, "
              f"{len(_history)} strokes left", flush=True)
        return True
    return False


def forget_history():
    _history.clear()
    _base.update(image=None, size=None, pixels=None)


@bpy.app.handlers.persistent
def _on_blender_undo(*_args):
    # Blender's undo / redo changed the image behind the baseline's back.
    _base.update(image=None, size=None, pixels=None)


def register():
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _on_blender_undo not in handlers:
            handlers.append(_on_blender_undo)


def unregister():
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _on_blender_undo in handlers:
            handlers.remove(_on_blender_undo)
    forget_history()


# --- painting --------------------------------------------------------------

class _View:
    """Point the desktop 3D view at a dab and put it back afterwards."""

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


def pressure_from(value):
    return min(max((value - TRIGGER_THRESHOLD) / (1.0 - TRIGGER_THRESHOLD), 0.05), 1.0)


class PaintStroke:
    """Trigger handler: paint while the trigger is held. Same interface as interactions' handlers."""

    def __init__(self, context, settings, obj, hand_left, scale):
        self.obj = obj
        self.reach = settings.paint_reach
        self.radius = settings.paint_radius * scale
        self.scale = scale
        self.invert = hand_left
        self.image = paint_image(obj)
        self.window = context.window or context.window_manager.windows[0]
        self.last = None  # (point, normal) of the last dab
        self.last_mouse_point = None
        self.started = False
        self.dabs = 0
        self.ms = []
        self.changed = False
        self.tried_tool = False

    def hit(self, aim_pose, tip):
        if self.reach == 'SPRAY':
            from . import interactions
            return surface_along(self.obj, aim_pose.translation, interactions.aim_direction(aim_pose))
        return surface_near(self.obj, tip, self.radius)

    def update(self, context, pose, tip_pose, value):
        h = self.hit(pose, tip_pose.translation)
        if h is None:
            self.last = None
            return
        point, normal, _face = h
        targets = [(point, normal)]
        if self.last is not None:
            gap = (point - self.last[0]).length
            step = max(self.radius * SPACING, 1e-6)
            if gap < step:
                return
            n = min(int(gap / step), MAX_DABS_PER_UPDATE)
            targets = []
            for i in range(1, n + 1):
                p = self.last[0].lerp(point, i / n)
                if self.reach == 'CONTACT':
                    near = surface_near(self.obj, p, self.radius * 2)
                    if near is not None:
                        targets.append(near[:2])
                        continue
                targets.append((p, self.last[1].lerp(normal, i / n).normalized()))
        direction = None
        if self.reach == 'SPRAY':
            from . import interactions
            direction = interactions.aim_direction(pose)
        for p, n in targets:
            self._dab(p, n, direction, pressure_from(value))

    def _dab(self, point, normal, direction, pressure):
        t0 = time.perf_counter()
        view = native.largest_view3d(self.window)
        if view is None:
            return
        area, region = view
        if not self.started:
            # Before the first dab: the baseline the stroke's undo is made from.
            self.started = True
            if self.image is not None:
                ensure_base(self.image)
        v = _View(area, region)
        try:
            eye = point - direction * EYE_DISTANCE * self.scale if direction is not None else \
                point + normal * EYE_DISTANCE * self.scale
            v.look(eye, point)
            size = v.pixels(point, self.radius)
            mouse = v.mouse(point)
            if size is None or mouse is None:
                return
            elem = {"name": "", "location": tuple(point), "mouse": mouse, "mouse_event": mouse,
                    "pressure": pressure, "size": max(size, 1.0), "x_tilt": 0.0, "y_tilt": 0.0, "time": 0.0,
                    "is_start": True}
            elems = [elem]
            if self.last is not None:
                # The previous dab as a lead-in, so Smear knows the stroke's direction.
                prev = v.mouse(self.last[0])
                if prev is not None:
                    elems = [dict(elem, mouse=prev, mouse_event=prev, pressure=0.0, location=tuple(self.last[0])),
                             dict(elem, is_start=False)]
            with bpy.context.temp_override(window=self.window, area=area, region=region):
                if not self._can_paint():
                    return
                bpy.ops.paint.image_paint(stroke=elems, mode='INVERT' if self.invert else 'NORMAL')
            self.dabs += 1
            self.changed = True
            self.last = (point, normal)
        finally:
            v.restore()
            self.ms.append((time.perf_counter() - t0) * 1000.0)

    def _can_paint(self):
        if bpy.ops.paint.image_paint.poll():
            return True
        if not self.tried_tool:
            self.tried_tool = True
            try:
                bpy.ops.wm.tool_set_by_id(name="builtin.brush", space_type='VIEW_3D')
            except RuntimeError as ex:
                print("VR Paint: could not switch to the Brush tool:", ex)
            return bpy.ops.paint.image_paint.poll()
        return False

    def finish(self, context):
        if self.ms:
            print(f"VR Paint stroke: {self.dabs} dabs, avg {sum(self.ms) / len(self.ms):.1f} ms, "
                  f"max {max(self.ms):.1f} ms", flush=True)
        if self.changed and self.image is not None:
            commit(self.image)


class PickColor:
    """Trigger handler for the Pick tool: take the texture color under the tip (or the ray)."""

    def __init__(self, context, settings, obj, tip, aim_pose, scale, secondary=False):
        self.changed = False
        if settings.paint_reach == 'SPRAY':
            from . import interactions
            h = surface_along(obj, aim_pose.translation, interactions.aim_direction(aim_pose))
        else:
            h = surface_near(obj, tip, max(settings.paint_radius * scale, 0.01 * scale))
        if h is None:
            return
        rgb = sample_color(obj, h[0], h[2])
        if rgb is not None:
            set_color(context, rgb, secondary)
            settings.paint_tool = 'PAINT'

    def update(self, context, pose, tip_pose, value):
        pass

    def finish(self, context):
        pass


def pick(context, settings, hand_left, pose, tip_pose, scale, value=1.0):
    """The trigger handler for a press in Paint mode, or None (nothing to paint)."""
    obj = target(context)
    if obj is None or not ready(obj):
        return None
    if obj.mode != 'TEXTURE_PAINT':
        enter(context)
    if settings.paint_tool == 'PICK':
        return PickColor(context, settings, obj, tip_pose.translation, pose, scale, secondary=hand_left)
    stroke = PaintStroke(context, settings, obj, hand_left, scale)
    stroke.update(context, pose, tip_pose, value)
    return stroke
