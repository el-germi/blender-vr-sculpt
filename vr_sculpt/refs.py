"""Reference images (Tools mode, Refs tool): pictures to look at while you work, placed around you.

A reference is Blender's own image empty (Add > Image > Reference does the same on the desktop), so
the desktop shows them as usual and any image empty counts, including ones set up there. The headset
doesn't draw empties (its view has object extras off), so draw() draws every visible one in VR as a
textured quad.

Images come from the references folder (Settings: Refs folder; empty: the .blend file's folder) and
from the references already in the file, as tiles with thumbnails (read with OpenImageIO through lookdev's
preview cache, so listing them makes no image data). The trigger places the chosen image at the tip,
upright and facing you, or grabs one in reach (it follows the hand, turning with it). One reference
can be pinned beside the hand panel, so it comes along.
"""

import os
import time

import bpy
import numpy as np
from mathutils import Matrix, Vector

from . import hud, primitives, vr_ui

EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".tga", ".exr", ".hdr"}
MAX_FILES = 500
REACH = 0.02  # hand-space meters in front of or behind an image that still touch it
PIN_WIDTH = 0.12  # hand-space meters: the pinned picture beside the panel
SELECT_COLOR = (0.45, 0.72, 1.0)
TOUCH_COLOR = (1.0, 0.85, 0.3)

_listing = {"folder": None, "stamp": 0.0, "files": []}
_textures = {}  # image name -> GPUTexture


def forget_caches():
    """Undo / redo / load: the images the textures came from may be gone."""
    _textures.clear()


# --- what's a reference, and where -------------------------------------------------

def is_ref(obj):
    return (obj is not None and obj.type == 'EMPTY' and obj.empty_display_type == 'IMAGE'
            and obj.data is not None)


def refs(context):
    return [o for o in context.view_layer.objects if is_ref(o) and o.visible_get()]


def selected_ref(context):
    obj = context.view_layer.objects.active
    return obj if is_ref(obj) and obj.select_get() else None


def aspect(image):
    """(x, y) of an image empty's quad per unit of display size: the longer side is 1."""
    w, h = image.size[:] if image is not None else (0, 0)
    if w <= 0 or h <= 0:
        return 1.0, 1.0
    return (1.0, h / w) if w >= h else (w / h, 1.0)


def local_rect(obj):
    """(x0, y0, x1, y1) of the picture in the empty's own space, as Blender draws it."""
    ax, ay = aspect(obj.data)
    size = obj.empty_display_size
    w, h = size * ax, size * ay
    ox, oy = obj.empty_image_offset
    return ox * w, oy * h, (ox + 1.0) * w, (oy + 1.0) * h


def corners(obj):
    x0, y0, x1, y1 = local_rect(obj)
    mw = obj.matrix_world
    return [mw @ Vector((x, y, 0.0)) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]


def ref_at(context, point, scale):
    """The reference whose picture the point touches (within REACH of its plane), the nearest one; or None."""
    point = Vector(point)
    best, best_d = None, REACH * scale
    for obj in refs(context):
        if obj.hide_select:
            continue
        mw = obj.matrix_world
        normal = (mw.to_3x3() @ Vector((0.0, 0.0, 1.0))).normalized()
        d = abs((point - mw.translation).dot(normal))
        if d > best_d:
            continue
        local = mw.inverted_safe() @ point
        x0, y0, x1, y1 = local_rect(obj)
        if x0 <= local.x <= x1 and y0 <= local.y <= y1:
            best, best_d = obj, d
    return best


def facing(location, viewer):
    """A matrix at location, the picture upright (world Z) and turned toward viewer."""
    location = Vector(location)
    toward = Vector(viewer) - location if viewer is not None else Vector((0.0, -1.0, 0.0))
    toward.z = 0.0
    if toward.length < 1e-6:
        toward = Vector((0.0, -1.0, 0.0))
    z = toward.normalized()
    x = Vector((0.0, 0.0, 1.0)).cross(z).normalized()
    y = z.cross(x)
    m = Matrix((x, y, z)).transposed().to_4x4()
    m.translation = location
    return m


def viewer(context):
    ss = context.window_manager.xr_session_state
    return Vector(ss.viewer_pose_location) if ss is not None else None


# --- the images to choose from ------------------------------------------------------

def folder(context):
    """The references folder, absolute ("" when it's unset and the file isn't saved)."""
    path = context.scene.vr_sculpt.refs_folder
    if path:
        return bpy.path.abspath(path)
    return os.path.dirname(bpy.data.filepath) if bpy.data.filepath else ""


def files(context):
    """Image files in the references folder, sorted, listed again when the folder changes (or every 5 s)."""
    where = folder(context)
    now = time.monotonic()
    if where != _listing["folder"] or now - _listing["stamp"] > 5.0:
        found = []
        if where and os.path.isdir(where):
            try:
                names = sorted(os.listdir(where), key=str.lower)
            except OSError:
                names = []
            for name in names:
                if os.path.splitext(name)[1].lower() in EXTENSIONS:
                    found.append(os.path.join(where, name))
                    if len(found) >= MAX_FILES:
                        break
        _listing.update(folder=where, stamp=now, files=found)
    return list(_listing["files"])


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


def choices(context):
    """Paths to choose from: the folder's images, then the pictures of references already in the file that
    aren't there (not every image in the file: textures aren't references)."""
    out = files(context)
    seen = {_norm(p) for p in out}
    used = {o.data for o in bpy.data.objects if is_ref(o)}
    for img in used:
        if img.source != 'FILE' or not img.filepath:
            continue
        p = bpy.path.abspath(img.filepath, library=img.library)
        if _norm(p) not in seen and os.path.splitext(p)[1].lower() in EXTENSIONS:
            seen.add(_norm(p))
            out.append(p)
    return out


def label(path):
    return os.path.splitext(os.path.basename(path))[0] if path else ""


def image_for(path):
    """The image for a file: one already loaded from it, else loaded now."""
    target = _norm(path)
    for img in bpy.data.images:
        if img.filepath and _norm(bpy.path.abspath(img.filepath, library=img.library)) == target:
            return img
    return bpy.data.images.load(path, check_existing=True)


def thumbnail(path):
    """(64, 64, 4) float32 RGBA as shown, bottom row first, the picture fitted in; None if unreadable."""
    from . import lookdev
    px, _names = lookdev._read(path, max_width=256)
    if px is None:
        return None
    if px.shape[2] == 1:
        px = np.repeat(px, 3, axis=2)
    rgb = px[..., :3]
    if os.path.splitext(path)[1].lower() in {".exr", ".hdr"}:
        rgb = lookdev._srgb(rgb)  # Linear files; the rest are stored as shown.
    alpha = px[..., 3:4] if px.shape[2] >= 4 else np.ones(px.shape[:2] + (1,), np.float32)
    rgba = np.concatenate([np.clip(rgb, 0.0, 1.0), alpha], axis=2)[::-1]  # bottom row first
    n = lookdev.PREVIEW_PX
    h, w = rgba.shape[:2]
    if w >= h:
        tw, th = n, max(int(round(n * h / w)), 1)
    else:
        tw, th = max(int(round(n * w / h)), 1), n
    small = lookdev._shrink(rgba, th, tw)
    out = np.zeros((n, n, 4), np.float32)
    y, x = (n - th) // 2, (n - tw) // 2
    out[y:y + th, x:x + tw] = small
    return out


# --- making, changing ------------------------------------------------------------------

def new_ref(context, path, matrix, size):
    """A reference image empty for path at matrix, size meters on its longer side, selected."""
    img = image_for(path)
    obj = bpy.data.objects.new(label(path) or "Reference", None)
    obj.empty_display_type = 'IMAGE'
    obj.data = img
    obj.empty_display_size = size
    obj.empty_image_offset = (-0.5, -0.5)
    obj.empty_image_depth = 'DEFAULT'
    obj.empty_image_side = 'DOUBLE_SIDED'
    obj.show_empty_image_orthographic = True
    obj.show_empty_image_perspective = True
    obj.show_empty_image_only_axis_aligned = False
    context.scene.collection.objects.link(obj)
    obj.matrix_world = matrix
    primitives.make_active(context, obj)
    return obj


def delete_ref(context, obj):
    settings = context.scene.vr_sculpt
    if settings.ref_pin == obj.name:
        settings.ref_pin = ""
    bpy.data.objects.remove(obj)


def face_me(context, obj):
    """Turn a reference to face the headset (upright), where it is."""
    if obj is None:
        return False
    size = obj.matrix_world.to_scale()
    m = facing(obj.matrix_world.translation, viewer(context)) @ Matrix.Diagonal((*size, 1.0))
    obj.matrix_world = m
    return True


def opacity(obj):
    return obj.color[3] if obj.use_empty_image_alpha else 1.0


def set_opacity(obj, value):
    obj.use_empty_image_alpha = value < 0.999
    obj.color[3] = value


def pinned(context):
    name = context.scene.vr_sculpt.ref_pin
    obj = context.scene.objects.get(name) if name else None
    return obj if is_ref(obj) else None


def toggle_pin(context, obj):
    settings = context.scene.vr_sculpt
    settings.ref_pin = "" if obj is None or settings.ref_pin == obj.name else obj.name


# --- trigger handlers --------------------------------------------------------------------

class PlaceRef:
    """Main trigger in empty space: the chosen image at the tip, upright and facing you. It follows the tip
    until the trigger lets go."""

    def __init__(self, context, settings, tip_pose):
        self.obj = new_ref(context, settings.ref_image, facing(tip_pose.translation, viewer(context)),
                           settings.ref_size)
        self.changed = True

    def update(self, context, pose, tip_pose, value):
        self.obj.matrix_world = facing(tip_pose.translation, viewer(context))

    def finish(self, context):
        context.view_layer.update()


def pick(context, settings, off, pose, tip_pose, scale):
    """The Refs tool's trigger handler. Either hand on a picture grabs it (it follows the hand, turning with
    it); the main hand in empty space places the chosen image; the off hand there selects nothing."""
    from . import interactions
    obj = ref_at(context, tip_pose.translation, scale)
    if obj is not None:
        handler = interactions.MoveObject(context, obj, tip_pose, scale)
        handler.snap = False  # A picture floats; it doesn't sit on surfaces.
        return handler
    if off or not settings.ref_image or not os.path.isfile(settings.ref_image):
        return interactions.Deselect(context)
    return PlaceRef(context, settings, tip_pose)


# --- drawing ---------------------------------------------------------------------------

def texture(image):
    """A GPU texture of an image (cached by name), or None if it can't be had."""
    tex = _textures.get(image.name)
    if tex is None:
        import gpu
        try:
            tex = gpu.texture.from_image(image)
        except Exception:
            return None
        _textures[image.name] = tex
    return tex


def _quad(points):
    from gpu_extras.batch import batch_for_shader
    import gpu
    shader = gpu.shader.from_builtin('IMAGE_COLOR')
    batch = batch_for_shader(shader, 'TRI_FAN', {"pos": [p[:] for p in points],
                                                 "texCoord": ((0, 0), (1, 0), (1, 1), (0, 1))})
    return shader, batch


def draw(context, ss, tips, scale, xr=True):
    """Every visible reference as a picture in the headset (the desktop draws them itself), and with the Refs
    tool on, an outline: blue selected, yellow in reach of a tip."""
    settings = getattr(context.scene, "vr_sculpt", None)
    if settings is None:
        return
    objs = refs(context)
    if not objs:
        return
    import gpu
    from gpu_extras.batch import batch_for_shader
    if xr and settings.refs_show:
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.depth_mask_set(False)
        for obj in objs:
            tex = texture(obj.data)
            if tex is None:
                continue
            shader, batch = _quad(corners(obj))
            shader.uniform_sampler("image", tex)
            shader.uniform_float("color", (1.0, 1.0, 1.0, opacity(obj)))
            batch.draw(shader)
        gpu.state.depth_mask_set(True)
    if settings.mode != 'TOOLS' or settings.tool != 'REFS':
        gpu.state.blend_set('NONE')
        return
    active = selected_ref(context)
    near = {o.name for o in (ref_at(context, t, scale) for t in tips) if o is not None}
    line = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    for obj in objs:
        color = SELECT_COLOR if obj == active else TOUCH_COLOR if obj.name in near else None
        if color is None:
            continue
        line.uniform_float("color", (*color, 0.95))
        batch_for_shader(line, 'LINE_LOOP', {"pos": [p[:] for p in corners(obj)]}).draw(line)
    gpu.state.blend_set('NONE')
    if active is not None and ss is not None:
        c = corners(active)
        top = (c[2] + c[3]) / 2 + Vector((0.0, 0.0, 0.01 * scale))
        hud.draw_tag(top, Vector(ss.viewer_pose_location), scale, active.name, SELECT_COLOR)


def draw_pin(context, panel_matrix):
    """The pinned reference beside the hand panel (right of it), PIN_WIDTH wide."""
    obj = pinned(context)
    if obj is None:
        return
    tex = texture(obj.data)
    if tex is None:
        return
    import gpu
    from gpu_extras.batch import batch_for_shader
    ax, ay = aspect(obj.data)
    width = PIN_WIDTH
    height = width * ay / ax
    x0 = vr_ui.WIDTH / 2 + 0.006
    y0 = 0.0
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(panel_matrix)
    pad = 0.002
    frame = gpu.shader.from_builtin('UNIFORM_COLOR')
    bg = [(x0 - pad, y0 - pad, 0), (x0 + width + pad, y0 - pad, 0), (x0 + width + pad, y0 + height + pad, 0),
          (x0 - pad, y0 + height + pad, 0)]
    frame.uniform_float("color", (0.06, 0.06, 0.07, 0.88))
    batch_for_shader(frame, 'TRIS', {"pos": bg}, indices=((0, 1, 2), (0, 2, 3))).draw(frame)
    shader, batch = _quad([Vector((x0, y0, 0.0002)), Vector((x0 + width, y0, 0.0002)),
                           Vector((x0 + width, y0 + height, 0.0002)), Vector((x0, y0 + height, 0.0002))])
    shader.uniform_sampler("image", tex)
    shader.uniform_float("color", (1.0, 1.0, 1.0, 1.0))
    batch.draw(shader)
    gpu.matrix.pop()
    gpu.state.blend_set('NONE')


def status(context):
    """The tip status's size for the Refs tool: the selected picture's size, or new ones'."""
    obj = selected_ref(context)
    size = obj.empty_display_size * max(obj.matrix_world.to_scale()) if obj else context.scene.vr_sculpt.ref_size
    return f"{size * 100:.0f} cm" if size < 1.0 else f"{size:.2f} m"
