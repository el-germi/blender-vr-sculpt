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
- Each call is its own undo step. Most of a call's cost is setting up the projection for the whole
  mesh, so all the dabs of a frame go into one call (one stroke element each). The add-on keeps its own per-stroke
  undo (changed tiles only, see below), so B (or Undo stroke) takes back a whole stroke.
"""

import colorsys
import time

import bpy
import numpy as np
from mathutils import Color, Vector
from mathutils.interpolate import poly_3d_calc

from . import native

ESSENTIALS = "brushes/essentials_brushes-mesh_texture.blend/Brush/"
BRUSHES = ("Paint Hard", "Paint Soft", "Airbrush", "Blur", "Smear", "Erase Soft")
SHORT = {"Paint Hard": "Hard", "Paint Soft": "Soft", "Airbrush": "Airbrush", "Blur": "Blur", "Smear": "Smear",
         "Erase Soft": "Erase"}
INFO = {
    "Paint Hard": "Solid color with a crisp edge. Other hand: second color",
    "Paint Soft": "Solid color with a soft edge. Other hand: second color",
    "Airbrush": "Builds up while you hold still. Other hand: second color",
    "Blur": "Softens the texture under the brush",
    "Smear": "Drags the texture's colors along your stroke",
    "Erase Soft": "Erases to transparent (needs an image with alpha)",
}
EYE_DISTANCE = 0.25  # hand-space meters between the temporary view and the dab, at least
# Projection painting skips faces that reach far outside the view (a big quad painted from close up
# only took paint near its corners), so the view backs off until the face under the dab fits.
SPACING = 0.2  # dab spacing as a fraction of the radius
MAX_DABS_PER_UPDATE = 48  # per frame; they share one paint call
MIN_CALL_INTERVAL = 0.009  # seconds: at most one paint call per VR frame (90 Hz)
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
    """Give obj what texture painting needs: UVs (Smart UV Project) and an image in a paint slot.

    An editable VR primitive or stroke becomes a plain mesh first: rebuilding it from its settings
    would throw the UVs (and so the painting) away, like sculpting does."""
    from . import menus, primitives, strokes
    primitives.forget(obj)
    strokes.forget(obj)
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


def evaluated_mesh(obj):
    """The mesh ray casts and closest-point queries land on (modifiers applied): their face indices are its."""
    me = obj.evaluated_get(bpy.context.view_layer.depsgraph).data
    return me if isinstance(me, bpy.types.Mesh) else obj.data


def _corner_weights(mesh, local, face):
    """(loop indices, weights) of a point on a face, for interpolating corner or point data."""
    poly = mesh.polygons[face]
    loops = list(poly.loop_indices)
    weights = poly_3d_calc([mesh.vertices[mesh.loops[i].vertex_index].co for i in loops], local)
    return loops, weights


def uv_at(obj, world_point, face, mesh=None, uv_map=""):
    """UV of a surface point on face (uv_map, or the active UV map), or None."""
    mesh = mesh or evaluated_mesh(obj)
    layer = (mesh.uv_layers.get(uv_map) if uv_map else None) or mesh.uv_layers.active
    if layer is None or not 0 <= face < len(mesh.polygons):
        return None
    loops, weights = _corner_weights(mesh, obj.matrix_world.inverted_safe() @ Vector(world_point), face)
    uv = Vector((0.0, 0.0))
    for i, w in zip(loops, weights):
        uv += layer.data[i].uv * w
    return uv


def sample_color(obj, world_point, face, fresh=True):
    """The paint image's color at a surface point (RGB, scene linear like the brush's), or None."""
    found = image_color(paint_image(obj), uv_at(obj, world_point, face), fresh)
    return found[0] if found is not None else None


# --- picking colors ----------------------------------------------------------
#
# Pick takes the color under the hand from any visible mesh (the texture, color attribute or color its
# material shows), any reference picture, or the picture pinned beside the hand panel. Colors come out
# scene linear, as the brush keeps them: painting turns them back into the image's sRGB.

PICK_REACH = 0.01  # hand-space meters: Contact picks at least this far from the tip
PIN_RING = 0.004  # hand-space meters: the preview ring on the pinned picture
PIXEL_IMAGES = 4  # images whose pixels are kept for the live preview
CLEAR = 0.1  # a picture's alpha below this is see-through: Pick looks past it
_pixels = {}  # image name -> ((size, is_float), pixels as _read() gives them), most recent last


def forget_pixels():
    _pixels.clear()


def _image_pixels(img, fresh):
    """img's pixels as _read() gives them. Kept for the preview; fresh (a press) reads them again."""
    if not fresh and _base_matches(img):
        return _base["pixels"]  # The paint image, as its last VR stroke left it.
    key = (tuple(img.size), bool(img.is_float))
    held = _pixels.pop(img.name, None)
    if fresh or held is None or held[0] != key:
        held = (key, _read(img))
    _pixels[img.name] = held
    while len(_pixels) > PIXEL_IMAGES:
        _pixels.pop(next(iter(_pixels)))
    return held[1]


def image_color(img, uv, fresh=False):
    """(scene linear RGB, alpha) of an image's pixel at uv (nearest, repeating), or None."""
    if img is None or uv is None:
        return None
    w, h = img.size[:]
    if w <= 0 or h <= 0:
        return None
    px = _image_pixels(img, fresh)
    c = px[int(uv[1] % 1.0 * h) % h, int(uv[0] % 1.0 * w) % w].astype(np.float64)
    if px.dtype == np.uint8:
        c /= 255.0
    rgb = tuple(float(v) for v in c[:3])
    if not img.is_float and not img.colorspace_settings.is_data:
        rgb = tuple(Color(rgb).from_srgb_to_scene_linear())  # Stored as shown.
    return rgb, float(c[3])


def _attribute_color(mesh, name, loops, weights):
    """A color attribute's (scene linear) color at a point, from its corners or points; or None."""
    attrs = mesh.color_attributes
    attr = attrs.get(name) if name else attrs.get(attrs.active_color_name or "")
    if attr is None or attr.domain not in {'CORNER', 'POINT'}:
        return None
    index = loops if attr.domain == 'CORNER' else [mesh.loops[i].vertex_index for i in loops]
    rgb = Vector((0.0, 0.0, 0.0))
    for i, w in zip(index, weights):
        rgb += Vector(attr.data[i].color[:3]) * w
    return tuple(rgb)


def _upstream(socket, kinds):
    """The nearest node of one of these types feeding socket (breadth first, not into groups), or None."""
    queue = [link.from_node for link in socket.links]
    seen = set()
    while queue and len(seen) < 64:
        node = queue.pop(0)
        if node.name in seen:
            continue
        seen.add(node.name)
        if node.type in kinds:
            return node
        queue += [link.from_node for inp in node.inputs for link in inp.links]
    return None


def _base_color_socket(tree):
    for kind, name in (('BSDF_PRINCIPLED', "Base Color"), ('BSDF_DIFFUSE', "Color"), ('EMISSION', "Color")):
        node = next((n for n in tree.nodes if n.type == kind), None)
        if node is not None and name in node.inputs:
            return node.inputs[name]
    return None


def _material_color(obj, mesh, face, loops, weights, point, fresh):
    """The color obj's material shows at a point: the image, color attribute or color behind its base
    color (the nearest one going back from it), else the material's viewport color."""
    slots = obj.material_slots
    index = mesh.polygons[face].material_index
    mat = slots[index].material if 0 <= index < len(slots) else None
    if mat is None:
        return _attribute_color(mesh, "", loops, weights)
    socket = _base_color_socket(mat.node_tree) if mat.node_tree is not None else None
    if socket is None:
        return tuple(mat.diffuse_color[:3])
    if not socket.is_linked:
        return tuple(socket.default_value[:3])
    node = _upstream(socket, {'TEX_IMAGE', 'VERTEX_COLOR', 'ATTRIBUTE', 'RGB'})
    if node is None:
        return tuple(mat.diffuse_color[:3])
    if node.type == 'TEX_IMAGE':
        uv_node = _upstream(node.inputs["Vector"], {'UVMAP'})
        found = image_color(node.image, uv_at(obj, point, face, mesh, uv_node.uv_map if uv_node else ""), fresh)
        return found[0] if found is not None else tuple(mat.diffuse_color[:3])
    if node.type == 'RGB':
        return tuple(node.outputs[0].default_value[:3])
    name = node.layer_name if node.type == 'VERTEX_COLOR' else node.attribute_name
    rgb = _attribute_color(mesh, name, loops, weights)
    return rgb if rgb is not None else tuple(mat.diffuse_color[:3])


def mesh_color(context, obj, point, face, fresh=False):
    """The color a mesh shows at a surface point (scene linear), or None. The mesh being painted gives its
    paint image's color."""
    mesh = evaluated_mesh(obj)
    if not 0 <= face < len(mesh.polygons):
        return None
    if obj == target(context) and ready(obj):
        rgb = sample_color(obj, point, face, fresh)
        if rgb is not None:
            return rgb
    loops, weights = _corner_weights(mesh, obj.matrix_world.inverted_safe() @ Vector(point), face)
    return _material_color(obj, mesh, face, loops, weights, point, fresh)


def _box(obj):
    """World-space (min, max) corners of obj's bounding box."""
    mw = obj.matrix_world
    pts = np.array([(mw @ Vector(c))[:] for c in obj.bound_box])
    return pts.min(axis=0), pts.max(axis=0)


def _box_near(obj, point, radius):
    lo, hi = _box(obj)
    p = np.array(point[:])
    return float(np.linalg.norm(p - np.clip(p, lo, hi))) <= radius


def _box_on_ray(obj, origin, direction):
    lo, hi = _box(obj)
    o, d = np.array(origin[:]), np.array(direction[:])
    with np.errstate(divide='ignore', invalid='ignore'):
        a, b = (lo - o) / d, (hi - o) / d
    t0 = np.nanmax(np.minimum(a, b))
    t1 = np.nanmin(np.maximum(a, b))
    return t1 >= max(t0, 0.0)


def pick_meshes(context):
    """The meshes Pick can take a color from: visible and drawn solid."""
    return [o for o in context.view_layer.objects
            if o is not None and o.type == 'MESH' and o.visible_get() and o.display_type not in {'WIRE', 'BOUNDS'}]


def _ref_point(obj, local, fresh):
    """(RGB, alpha) of a reference picture at a point in the empty's own space, or None off the picture."""
    from . import refs
    x0, y0, x1, y1 = refs.local_rect(obj)
    if x1 - x0 <= 0 or y1 - y0 <= 0 or not (x0 <= local.x <= x1 and y0 <= local.y <= y1):
        return None
    return image_color(obj.data, ((local.x - x0) / (x1 - x0), (local.y - y0) / (y1 - y0)), fresh)


def _pick_refs(context):
    from . import refs
    return refs.refs(context) if context.scene.vr_sculpt.refs_show else []


def _facing(normal, toward):
    return normal if normal.dot(toward) >= 0.0 else -normal


def pick_near(context, point, radius, fresh=False):
    """(world point, world normal, RGB) of the nearest surface within radius that has a color: a mesh's or
    a reference picture's (not where it's see-through); or None."""
    point = Vector(point)
    best, best_d = None, radius
    for obj in pick_meshes(context):
        if not _box_near(obj, point, best_d):
            continue
        h = surface_near(obj, point, best_d)
        if h is None:
            continue
        rgb = mesh_color(context, obj, h[0], h[2], fresh)
        if rgb is not None:
            best, best_d = (h[0], h[1], rgb), (h[0] - point).length
    for obj in _pick_refs(context):
        mw = obj.matrix_world
        normal = (mw.to_3x3() @ Vector((0.0, 0.0, 1.0))).normalized()
        d = (point - mw.translation).dot(normal)
        if abs(d) > best_d:
            continue
        found = _ref_point(obj, mw.inverted_safe() @ point, fresh)
        if found is not None and found[1] >= CLEAR:
            best, best_d = (point - normal * d, _facing(normal, normal * d), found[0]), abs(d)
    return best


def pick_along(context, origin, direction, fresh=False):
    """(world point, world normal, RGB) where a ray first meets a mesh or a reference picture (looking past
    see-through parts of pictures), or None."""
    origin, direction = Vector(origin), Vector(direction).normalized()
    best, best_t = None, float("inf")
    for obj in pick_meshes(context):
        if not _box_on_ray(obj, origin, direction):
            continue
        h = surface_along(obj, origin, direction)
        if h is None or (h[0] - origin).length >= best_t:
            continue
        rgb = mesh_color(context, obj, h[0], h[2], fresh)
        if rgb is not None:
            best, best_t = (h[0], h[1], rgb), (h[0] - origin).length
    for obj in _pick_refs(context):
        mw = obj.matrix_world
        normal = (mw.to_3x3() @ Vector((0.0, 0.0, 1.0))).normalized()
        denom = direction.dot(normal)
        if abs(denom) < 1e-9:
            continue
        t = (mw.translation - origin).dot(normal) / denom
        if not 0.0 < t < best_t:
            continue
        hit = origin + direction * t
        found = _ref_point(obj, mw.inverted_safe() @ hit, fresh)
        if found is not None and found[1] >= CLEAR:
            best, best_t = (hit, _facing(normal, -direction), found[0]), t
    return best


def pick_pin(context, aim_pose, fresh=False):
    """(world point, world normal, RGB) where a ray from aim_pose meets the picture pinned beside the hand
    panel (while the panel shows), or None."""
    from . import interactions, presence, refs
    ss = context.window_manager.xr_session_state
    obj = refs.pinned(context)
    if obj is None or ss is None or not context.scene.vr_sculpt.show_panel:
        return None
    # A press asks like a panel press does; the preview only looks (shown() moves the panel's fade on).
    if not (presence.shown(context, ss) if fresh else presence.alpha() > 0.5):
        return None
    return pin_color(obj, interactions.panel_matrix(context, ss), aim_pose, fresh)


def pin_color(obj, matrix, aim_pose, fresh=False):
    """(world point, world normal, RGB) where a ray from aim_pose meets obj's picture pinned beside a hand
    panel at matrix, or None."""
    from . import interactions, refs
    inv = matrix.inverted_safe()
    o = inv @ aim_pose.translation
    d = inv.to_3x3() @ interactions.aim_direction(aim_pose)
    if abs(d.z) < 1e-9 or -o.z / d.z <= 0.0:
        return None
    p = o + d * (-o.z / d.z)
    x0, y0, x1, y1 = refs.pin_rect(obj)
    if not (x0 <= p.x <= x1 and y0 <= p.y <= y1):
        return None
    found = image_color(obj.data, ((p.x - x0) / (x1 - x0), (p.y - y0) / (y1 - y0)), fresh)
    if found is None:
        return None
    normal = (matrix.to_3x3() @ Vector((0.0, 0.0, 1.0))).normalized()
    return matrix @ Vector((p.x, p.y, 0.0)), normal, found[0]


def pick_at(context, settings, aim_pose, tip, scale, main_hand=True, fresh=False):
    """(world point, world normal, RGB, on the pin) Pick takes for a hand: the pinned picture it points at
    (main hand), else along its ray (Spray) or near its tip (Contact); or None."""
    found = pick_pin(context, aim_pose, fresh) if main_hand else None
    if found is not None:
        return (*found, True)
    if settings.paint_reach == 'SPRAY':
        from . import interactions
        found = pick_along(context, aim_pose.translation, interactions.aim_direction(aim_pose), fresh)
    else:
        found = pick_near(context, tip, max(settings.paint_radius, PICK_REACH) * scale, fresh)
    return (*found, False) if found is not None else None


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
    px += 0.5  # Rounds on the truncating cast below (values are 0..1 for byte images).
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
    # Compare whole pixels as integers: much faster than comparing channels and reducing.
    if before.dtype == np.uint8:
        diff = before.view(np.uint32).reshape(h, w) != after.view(np.uint32).reshape(h, w)
    else:
        diff = (before.view(np.uint64).reshape(h, w, 2) != after.view(np.uint64).reshape(h, w, 2)).any(axis=2)
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
    _pixels.clear()


def register():
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _on_blender_undo not in handlers:
            handlers.append(_on_blender_undo)


def unregister():
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _on_blender_undo in handlers:
            handlers.remove(_on_blender_undo)
    forget_history()
    forget_pixels()


# --- painting --------------------------------------------------------------

def pressure_from(value):
    return min(max((value - TRIGGER_THRESHOLD) / (1.0 - TRIGGER_THRESHOLD), 0.05), 1.0)


class PaintStroke:
    """Trigger handler: paint while the trigger is held. Same interface as interactions' handlers."""
    smooth = True  # The trigger operator stabilizes the tip (smoothing.py).

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
        self.tan_half = None
        self.last_call = 0.0
        self.pending = None
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
        # Updates come from XR events and a fallback timer, often both in one frame. One paint call
        # per frame is enough: the next update fills the gap with interpolated dabs.
        if self.last is not None and time.perf_counter() - self.last_call < MIN_CALL_INTERVAL:
            self.pending = (pose, tip_pose, value)  # Painted on release if nothing comes after it.
            return
        self.pending = None
        h = self.hit(pose, tip_pose.translation)
        if h is None:
            self.last = None
            return
        point, normal, face = h
        targets = [(point, normal, face)]
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
                        targets.append(near)
                        continue
                targets.append((p, self.last[1].lerp(normal, i / n).normalized(), face))
        direction = None
        if self.reach == 'SPRAY':
            from . import interactions
            direction = interactions.aim_direction(pose)
        self._paint(targets, direction, pressure_from(value))

    def eye_distance(self, face, point, tan_half):
        """How far the temporary view has to be for the face under the dab to fit in it."""
        d = EYE_DISTANCE * self.scale
        if tan_half <= 0.0:
            return d
        mesh = self.obj.data
        mw = self.obj.matrix_world
        if face is not None and 0 <= face < len(mesh.polygons):
            extent = max((mw @ mesh.vertices[v].co - point).length for v in mesh.polygons[face].vertices)
        else:
            extent = max(self.obj.dimensions)  # E.g. a modifier changed the faces: be safe.
        return max(d, (extent + self.radius) / tan_half)

    @staticmethod
    def _element(point, mouse, pressure, size, is_start):
        return {"name": "", "location": tuple(point), "mouse": mouse, "mouse_event": mouse, "pressure": pressure,
                "size": max(size, 1.0), "x_tilt": 0.0, "y_tilt": 0.0, "time": 0.0, "is_start": is_start}

    def _paint(self, targets, direction, pressure):
        """Paint one update's dabs with a single image_paint call, from one view that sees them all.

        Each call sets up projection painting for the whole mesh first, which is most of its cost
        (about 2 ms on a 10k-vertex mesh, 4.6 ms on 41k), so one call per frame instead of one per
        dab is up to ~10x cheaper when the hand moves fast."""
        if not targets:
            return
        t0 = time.perf_counter()
        self.last_call = t0
        view = native.largest_view3d(self.window)
        if view is None:
            return
        area, region = view
        if not self.started:
            # Before the first dab: the baseline the stroke's undo is made from.
            self.started = True
            if self.image is not None:
                ensure_base(self.image)
        center = sum((p for p, _n, _f in targets), Vector()) / len(targets)
        if direction is not None:
            back = -direction
        else:
            back = sum((n for _p, n, _f in targets), Vector())
            back = back.normalized() if back.length > 1e-9 else targets[-1][1]
        v = native.AimedView(area, region)
        try:
            v.look(center + back * EYE_DISTANCE * self.scale, center)
            if self.tan_half is None:
                # Half the narrower field of view, from the view's projection (lens and region shape).
                wm = v.rv3d.window_matrix
                self.tan_half = min(1.0 / wm[0][0], 1.0 / wm[1][1]) if wm[0][0] and wm[1][1] else 0.0
            dist = max(self.eye_distance(f, center, self.tan_half) for _p, _n, f in targets)
            if self.tan_half > 0.0:
                spread = max((p - center).length for p, _n, _f in targets)
                dist = max(dist, (spread + self.radius) / self.tan_half)
            if dist > EYE_DISTANCE * self.scale + 1e-6:
                v.look(center + back * dist, center)
            elems = []
            for p, _n, _f in targets:
                mouse, size = v.mouse(p), v.pixels(p, self.radius)
                if mouse is not None and size is not None:
                    elems.append(self._element(p, mouse, pressure, size, not elems))
            if not elems:
                return
            if self.last is not None:
                # The previous dab as a lead-in, so Smear knows the stroke's direction.
                prev = v.mouse(self.last[0])
                if prev is not None:
                    elems[0]["is_start"] = False
                    elems.insert(0, self._element(self.last[0], prev, 0.0, elems[0]["size"], True))
            with bpy.context.temp_override(window=self.window, area=area, region=region):
                if not self._can_paint():
                    return
                bpy.ops.paint.image_paint(stroke=elems, mode='INVERT' if self.invert else 'NORMAL')
            self.dabs += len(targets)
            self.changed = True
            self.last = (targets[-1][0], targets[-1][1])
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
        if self.pending is not None:
            pose, tip_pose, value = self.pending
            self.last_call = 0.0
            self.update(context, pose, tip_pose, value)
        if self.ms:
            print(f"VR Paint stroke: {self.dabs} dabs in {len(self.ms)} calls, avg {sum(self.ms) / len(self.ms):.1f} ms "
                  f"per call, max {max(self.ms):.1f} ms", flush=True)
        if self.changed and self.image is not None:
            commit(self.image)


class PickColor:
    """Trigger handler for the Pick tool: take the color under the tip (or the ray), from any mesh or picture."""

    def __init__(self, context, settings, tip, aim_pose, scale, secondary=False):
        self.changed = False
        found = pick_at(context, settings, aim_pose, tip, scale, main_hand=not secondary, fresh=True)
        if found is not None:
            set_color(context, found[2], secondary)
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
        return PickColor(context, settings, tip_pose.translation, pose, scale, secondary=hand_left)
    stroke = PaintStroke(context, settings, obj, hand_left, scale)
    stroke.update(context, pose, tip_pose, value)
    return stroke
