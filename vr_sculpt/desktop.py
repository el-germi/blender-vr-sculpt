"""Floating desktop window: the Blender window on a screen in VR (view only).

Python can't read the window's final image, but every region draws into its own buffer, and a
POST_PIXEL draw callback runs right after. There we copy the region's pixels into a texture
(throttled), and the headset draws all of them on a quad at their window positions. 3D view
regions are the exception: their callback only sees the overlay layer, so they are re-rendered
offscreen from the same view instead.

Regions only redraw when something changes, so captures stay valid between redraws; a
throttled region is tagged again later (tick()) so its last change isn't missed.

The screen's pose is kept relative to the VR navigation, so it travels with you when you
fly, walk or scale the world. Point at it in Tools mode and drag to move it.
"""

import time

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Quaternion, Vector

from . import hud

CAPTURE_INTERVAL = 0.25  # seconds between captures of one region
VIEW3D_SCALE = 0.5  # 3D views are re-rendered at this fraction of their size
DISTANCE = 0.8  # hand-space meters in front of the headset when placed
PAD = 0.008

_SPACES = ('SpaceView3D', 'SpaceOutliner', 'SpaceProperties', 'SpaceDopeSheetEditor', 'SpaceGraphEditor',
           'SpaceNLA', 'SpaceSequenceEditor', 'SpaceImageEditor', 'SpaceNodeEditor', 'SpaceTextEditor',
           'SpaceConsole', 'SpaceInfo', 'SpaceFileBrowser', 'SpacePreferences', 'SpaceClipEditor',
           'SpaceSpreadsheet')
_REGIONS = ('WINDOW', 'HEADER', 'UI', 'TOOLS', 'TOOL_HEADER', 'NAVIGATION_BAR', 'FOOTER', 'CHANNELS',
            'ASSET_SHELF', 'ASSET_SHELF_HEADER', 'TOOL_PROPS', 'EXECUTE')


class Capture:
    __slots__ = ("x", "y", "w", "h", "texture", "offscreen", "time", "order", "region")

    def __init__(self):
        self.texture = None
        self.offscreen = None
        self.time = 0.0


_captures = {}  # region pointer -> Capture
_dirty = {}  # region pointer -> region, captures skipped by the throttle
_handles = []
_errors = []
state = {"pose": None, "force": False}  # pose: screen matrix relative to the navigation (hand space)


def active(context):
    settings = getattr(context.scene, "vr_sculpt", None)
    if settings is None or not settings.show_desktop:
        return False
    if state["force"]:
        return True
    from . import operators
    return operators.session_running(context)


def target_window(context):
    windows = context.window_manager.windows
    return windows[0] if len(windows) else None


def _capture_region(context, region, area):
    key = region.as_pointer()
    cap = _captures.get(key)
    now = time.monotonic()
    if cap is not None and now - cap.time < CAPTURE_INTERVAL:
        _dirty[key] = region
        return
    if cap is None:
        cap = _captures[key] = Capture()
    cap.x, cap.y, cap.w, cap.h = region.x, region.y, region.width, region.height
    cap.order = 0 if region.type == 'WINDOW' else 1
    cap.region = region
    cap.time = now
    _dirty.pop(key, None)
    if area.type == 'VIEW_3D' and region.type == 'WINDOW':
        rv3d = context.region_data
        w, h = max(int(region.width * VIEW3D_SCALE), 8), max(int(region.height * VIEW3D_SCALE), 8)
        if cap.offscreen is None or (cap.offscreen.width, cap.offscreen.height) != (w, h):
            cap.offscreen = gpu.types.GPUOffScreen(w, h)
        cap.offscreen.draw_view3d(context.scene, context.view_layer, context.space_data, region,
                                  rv3d.view_matrix, rv3d.window_matrix, do_color_management=True)
        cap.texture = cap.offscreen.texture_color
        return
    fb = gpu.state.active_framebuffer_get()
    w, h = region.width, region.height
    # GPUTexture only takes float data, so read floats.
    buf = fb.read_color(0, 0, w, h, 4, 0, 'FLOAT')
    buf.dimensions = w * h * 4
    cap.texture = gpu.types.GPUTexture((w, h), format='RGBA16F', data=buf)


def _callback():
    context = bpy.context
    try:
        region, area = context.region, context.area
        if region is None or area is None or region.width < 2 or region.height < 2:
            return
        if not active(context) or context.window != target_window(context):
            return
        _capture_region(context, region, area)
    except Exception:
        if not _errors:
            import traceback
            traceback.print_exc()
        _errors.append(1)


def tick(context):
    """Drop captures of regions that are gone; re-tag regions whose last change was throttled."""
    if not active(context):
        if _captures:
            clear()
        return
    window = target_window(context)
    if window is None:
        return
    alive = {r.as_pointer() for a in window.screen.areas for r in a.regions}
    for key in [k for k in _captures if k not in alive]:
        del _captures[key]
    now = time.monotonic()
    for key, region in list(_dirty.items()):
        if key not in alive:
            del _dirty[key]
        elif now - _captures[key].time >= CAPTURE_INTERVAL:
            region.tag_redraw()


def request_full_capture(context):
    """Redraw every region of the window so the screen fills right away."""
    window = target_window(context)
    if window is not None:
        for area in window.screen.areas:
            area.tag_redraw()


def clear():
    for cap in _captures.values():
        if cap.offscreen is not None:
            cap.offscreen.free()
    _captures.clear()
    _dirty.clear()


# --- the screen in VR ------------------------------------------------------

def navigation_matrix(ss):
    s = ss.navigation_scale
    return Matrix.LocRotScale(Vector(ss.navigation_location), Quaternion(ss.navigation_rotation), Vector((s, s, s)))


def place_in_front(context, ss):
    """Put the screen in front of the headset, facing it, a little below eye level."""
    viewer = Vector(ss.viewer_pose_location)
    forward = Quaternion(ss.viewer_pose_rotation) @ Vector((0.0, 0.0, -1.0))
    forward.z = 0.0
    if forward.length < 1e-6:
        forward = Vector((0.0, 1.0, 0.0))
    forward.normalize()
    scale = ss.viewer_scale
    settings = context.scene.vr_sculpt
    size = screen_size(context, settings.desktop_size)
    anchor = viewer + forward * DISTANCE * scale - Vector((0.0, 0.0, (0.1 + size[1] / 2) * scale))
    world = hud.billboard(anchor, viewer - Vector((0.0, 0.0, (0.1 + size[1] / 2) * scale)), scale)
    state["pose"] = navigation_matrix(ss).inverted_safe() @ world


def world_matrix(context, ss):
    """Screen matrix in world space (hand-space meters, origin at the bottom center), or None."""
    if state["pose"] is None:
        place_in_front(context, ss)
    return navigation_matrix(ss) @ state["pose"]


def set_world_matrix(ss, matrix):
    state["pose"] = navigation_matrix(ss).inverted_safe() @ matrix


def screen_size(context, width):
    window = target_window(context)
    if window is None or window.width < 1:
        return width, width * 9 / 16
    return width, width * window.height / window.width


def ray_hit(context, ss, origin, direction):
    """Screen-space (x, y) where the ray hits the screen (frame included), or None."""
    matrix = world_matrix(context, ss)
    w, h = screen_size(context, context.scene.vr_sculpt.desktop_size)
    inv = matrix.inverted_safe()
    o = inv @ Vector(origin)
    d = inv.to_3x3() @ Vector(direction)
    if abs(d.z) < 1e-9:
        return None
    t = -o.z / d.z
    if t <= 0:
        return None
    p = o + d * t
    if -w / 2 - PAD <= p.x <= w / 2 + PAD and -PAD <= p.y <= h + PAD:
        return (p.x, p.y)
    return None


class MoveScreen:
    """Trigger handler: drag the desktop screen with the controller."""

    def __init__(self, context, ss, pose):
        self.ss = ss
        self.start = world_matrix(context, ss)
        self.pose_inv = pose.inverted_safe()
        self.changed = False

    def update(self, context, pose, tip_pose, value):
        set_world_matrix(self.ss, pose @ self.pose_inv @ self.start)

    def finish(self, context):
        pass


def draw(context, ss, hover=None):
    """The screen with every captured region at its window position."""
    window = target_window(context)
    if window is None:
        return
    matrix = world_matrix(context, ss)
    w, h = screen_size(context, context.scene.vr_sculpt.desktop_size)
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    image = gpu.shader.from_builtin('IMAGE')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(matrix)
    frame = [(-w / 2 - PAD, -PAD, 0), (w / 2 + PAD, -PAD, 0), (w / 2 + PAD, h + PAD, 0), (-w / 2 - PAD, h + PAD, 0)]
    batch = batch_for_shader(shader, 'TRIS', {"pos": frame}, indices=((0, 1, 2), (0, 2, 3)))
    shader.uniform_float("color", (0.12, 0.12, 0.13, 0.95) if hover is None else (0.2, 0.2, 0.23, 0.95))
    batch.draw(shader)
    sx, sy = w / max(window.width, 1), h / max(window.height, 1)
    for cap in sorted(_captures.values(), key=lambda c: c.order):
        if cap.texture is None:
            continue
        x0, y0 = -w / 2 + cap.x * sx, cap.y * sy
        x1, y1 = x0 + cap.w * sx, y0 + cap.h * sy
        z = 0.0002 * (1 + cap.order)
        pts = [(x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)]
        batch = batch_for_shader(image, 'TRI_FAN', {"pos": pts, "texCoord": ((0, 0), (1, 0), (1, 1), (0, 1))})
        image.uniform_sampler("image", cap.texture)
        batch.draw(image)
    if hover is not None:
        # Where the ray points: a small ring, since there's no mouse to show.
        import math
        r = 0.006
        ring = [(hover[0] + r * math.cos(a), hover[1] + r * math.sin(a), 0.001)
                for a in (2 * math.pi * i / 24 for i in range(24))]
        batch = batch_for_shader(shader, 'LINE_LOOP', {"pos": ring})
        shader.uniform_float("color", (1.0, 1.0, 1.0, 0.95))
        batch.draw(shader)
    gpu.matrix.pop()
    if not _captures:
        hud.draw_text(matrix, -w / 2 + 0.01, h / 2, "Waiting for the desktop window to redraw...", 0.012,
                      (0.85, 0.85, 0.88, 1.0))
    gpu.state.blend_set('NONE')


def register():
    for name in _SPACES:
        space = getattr(bpy.types, name, None)
        if space is None:
            continue
        for region in _REGIONS:
            try:
                _handles.append((space, region, space.draw_handler_add(_callback, (), region, 'POST_PIXEL')))
            except (TypeError, ValueError, RuntimeError):
                pass  # This space has no such region type.


def unregister():
    for space, region, h in _handles:
        space.draw_handler_remove(h, region)
    _handles.clear()
    clear()
    state["pose"] = None
