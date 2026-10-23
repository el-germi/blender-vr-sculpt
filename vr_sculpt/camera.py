"""Scene camera in VR: aim it with the controller and preview what it sees.

The preview is rendered offscreen with GPUOffScreen.draw_view3d, which needs a 3D viewport's
draw callback, so it is rendered while the largest desktop 3D view redraws (live.py asks for
those redraws) and only shown in the headset.
"""

import time

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector

from . import hud, native

PREVIEW_FPS = 20.0
PREVIEW_PIXELS = 480  # width of the offscreen image
PREVIEW_WIDTH = 0.12  # hand-space meters

_offscreen = None
_rendered = {"time": 0.0, "camera": "", "ok": False}
_handle = None


def cameras(scene):
    return [o for o in scene.objects if o.type == 'CAMERA']


def new_camera(context, pose=None):
    """A new camera at pose (an aim pose: looking down its -Z), made the scene camera."""
    hold_base_pose(context)
    data = bpy.data.cameras.new("VR Camera")
    obj = bpy.data.objects.new("VR Camera", data)
    context.scene.collection.objects.link(obj)
    if pose is not None:
        obj.matrix_world = pose.normalized()
    context.scene.camera = obj
    return obj


def set_camera(context, obj):
    hold_base_pose(context)
    context.scene.camera = obj


def next_camera(context):
    scene = context.scene
    cams = cameras(scene)
    if not cams:
        return None
    i = cams.index(scene.camera) if scene.camera in cams else -1
    set_camera(context, cams[(i + 1) % len(cams)])
    return scene.camera


# --- the VR base pose ------------------------------------------------------------
#
# Blender places the viewer relative to the session's base pose, worked out again every frame, and the
# stock VR add-on's default landmark makes that the scene camera. Moving or switching the scene camera
# then moves you with it: aiming it, the viewer follows the controller, which moves the camera again,
# and you're thrown around. So before the camera tool moves or switches a camera you're placed by, the
# base pose is pinned where it is (Custom, same spot and heading); it goes back as it was when the
# session ends.

_held = {"type": None}


def _placed_by(context, obj):
    """The object the base pose follows, if it's obj (any camera for None: the scene camera's)."""
    xs = context.window_manager.xr_session_settings
    if xs.base_pose_type == 'SCENE_CAMERA':
        source = context.scene.camera
    elif xs.base_pose_type == 'OBJECT':
        source = xs.base_pose_object
    else:
        return None
    return source if source is not None and (obj is None or obj == source) else None


def hold_base_pose(context, obj=None):
    """In VR, pin the base pose where it is if it follows obj (None: if it follows the scene camera)."""
    from . import operators
    if not operators.session_running(context):
        return
    source = _placed_by(context, obj)
    if source is None:
        return
    xs = context.window_manager.xr_session_settings
    if _held["type"] is None:
        _held.update(type=xs.base_pose_type, object=xs.base_pose_object, location=tuple(xs.base_pose_location),
                     angle=xs.base_pose_angle)
    mw = source.matrix_world
    # Blender only keeps an object's turn about Z (it's leveled with the floor), like a Custom pose's angle.
    xs.base_pose_location = mw.translation
    xs.base_pose_angle = mw.to_euler('XYZ').z
    xs.base_pose_type = 'CUSTOM'


def release_base_pose(context):
    """Put the base pose back as it was (session over, add-on off)."""
    if _held["type"] is None:
        return
    try:
        xs = context.window_manager.xr_session_settings
    except AttributeError:
        return  # No window manager (e.g. quitting); keep it for next time.
    xs.base_pose_type = _held["type"]
    xs.base_pose_object = _held["object"]
    xs.base_pose_location = _held["location"]
    xs.base_pose_angle = _held["angle"]
    _held["type"] = None


class AimCamera:
    """Trigger handler: the scene camera follows the controller (it looks where the controller points)."""

    def __init__(self, context, pose):
        hold_base_pose(context, context.scene.camera)
        cam = context.scene.camera
        if cam is None:
            cam = new_camera(context, pose)
        self.cam = cam
        self.scale = cam.matrix_world.to_scale()
        self.changed = True
        self.update(context, pose, pose, 1.0)

    def update(self, context, pose, tip_pose, value):
        self.cam.matrix_world = pose.normalized() @ Matrix.Diagonal(self.scale).to_4x4()

    def finish(self, context):
        context.view_layer.update()
        from . import anim
        anim.auto_key(context, self.cam)


# --- preview ---------------------------------------------------------------

def wanted(context):
    """Whether the headset shows a camera preview right now."""
    settings = getattr(context.scene, "vr_sculpt", None)
    if settings is None or context.scene.camera is None:
        return False
    return settings.camera_preview or (settings.mode == 'TOOLS' and settings.tool == 'CAMERA')


def preview_size(scene):
    r = scene.render
    aspect = (r.resolution_x * r.pixel_aspect_x) / max(r.resolution_y * r.pixel_aspect_y, 1e-6)
    w = PREVIEW_PIXELS
    return w, max(int(round(w / max(aspect, 1e-3))), 8)


def host_region(context):
    """The desktop 3D view region that renders the preview: the largest one in the first window."""
    windows = context.window_manager.windows
    if not len(windows):
        return None
    view = native.largest_view3d(windows[0])
    return view[1] if view else None


def render(context, force=False):
    """Render the scene camera into the offscreen buffer. Call from a 3D view draw callback."""
    global _offscreen
    scene = context.scene
    cam = scene.camera
    if cam is None or context.space_data is None or context.region is None:
        return False
    now = time.monotonic()
    if not force and now - _rendered["time"] < 1.0 / PREVIEW_FPS:
        return False
    w, h = preview_size(scene)
    if _offscreen is None or (_offscreen.width, _offscreen.height) != (w, h):
        _offscreen = gpu.types.GPUOffScreen(w, h)
    depsgraph = context.evaluated_depsgraph_get()
    cam_eval = cam.evaluated_get(depsgraph)
    view = cam_eval.matrix_world.normalized().inverted()
    r = scene.render
    proj = cam_eval.calc_matrix_camera(depsgraph, x=w, y=h, scale_x=r.pixel_aspect_x, scale_y=r.pixel_aspect_y)
    _offscreen.draw_view3d(scene, context.view_layer, context.space_data, context.region, view, proj,
                           do_color_management=True)
    _rendered.update(time=now, camera=cam.name, ok=True)
    return True


def _render_callback():
    context = bpy.context
    try:
        from . import operators
        if not operators.session_running(context) or not wanted(context):
            return
        host = host_region(context)
        if host is None or context.region is None or context.region.as_pointer() != host.as_pointer():
            return
        render(context)
    except Exception:
        if not _errors:
            import traceback
            traceback.print_exc()
        _errors.append(1)


_errors = []


def texture():
    return _offscreen.texture_color if _offscreen is not None and _rendered["ok"] else None


def caption(scene):
    cam = scene.camera
    if cam is None:
        return "No camera"
    lens = f"  {cam.data.lens:.0f} mm" if cam.data.type == 'PERSP' else ""
    return cam.name + lens


def draw_preview(matrix, scene, width=PREVIEW_WIDTH, x=None, y=0.0):
    """The preview as a framed picture in matrix's XY plane (hand-space meters); x defaults to centered."""
    w, h = preview_size(scene)
    height = width * h / w
    x0 = -width / 2 if x is None else x
    y0 = y + 0.012  # room for the caption below
    x1, y1 = x0 + width, y0 + height
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(matrix)
    pad = 0.002
    bg = [(x0 - pad, y - pad, 0), (x1 + pad, y - pad, 0), (x1 + pad, y1 + pad, 0), (x0 - pad, y1 + pad, 0)]
    batch = batch_for_shader(shader, 'TRIS', {"pos": bg}, indices=((0, 1, 2), (0, 2, 3)))
    shader.uniform_float("color", (0.06, 0.06, 0.07, 0.88))
    batch.draw(shader)
    pic = [(x0, y0, 0.0002), (x1, y0, 0.0002), (x1, y1, 0.0002), (x0, y1, 0.0002)]
    tex = texture()
    if tex is not None:
        image = gpu.shader.from_builtin('IMAGE')
        batch = batch_for_shader(image, 'TRI_FAN', {"pos": pic, "texCoord": ((0, 0), (1, 0), (1, 1), (0, 1))})
        image.uniform_sampler("image", tex)
        batch.draw(image)
    batch = batch_for_shader(shader, 'LINE_LOOP', {"pos": pic})
    shader.uniform_float("color", (0.95, 0.95, 0.95, 0.9))
    batch.draw(shader)
    gpu.matrix.pop()
    text = caption(scene) if tex is not None else "Waiting for the desktop 3D view..."
    hud.draw_text(matrix, x0, y + 0.003, text, 0.0065, (0.85, 0.85, 0.88, 1.0))
    gpu.state.blend_set('NONE')
    return height + 0.012


def register():
    global _handle
    _handle = bpy.types.SpaceView3D.draw_handler_add(_render_callback, (), 'WINDOW', 'POST_PIXEL')


def unregister():
    global _handle, _offscreen
    release_base_pose(bpy.context)
    if _handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle, 'WINDOW')
        _handle = None
    if _offscreen is not None:
        _offscreen.free()
        _offscreen = None
    _rendered.update(time=0.0, ok=False)
