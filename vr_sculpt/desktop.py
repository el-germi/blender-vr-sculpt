"""Floating desktop window: the Blender window on a screen in VR, usable with the controllers.

Two ways to get the picture:
- Windows: the OS copy of the window (winput.py), on a worker thread, in strips that are only
  re-uploaded when they change. This includes menus, popups and tooltips.
- Elsewhere (view only): every region draws into its own buffer and a POST_PIXEL draw callback
  runs right after; there the region's pixels are copied into a texture (throttled). 3D views
  are re-rendered offscreen instead, since their callback only sees the overlay layer. Regions
  only redraw when something changes, so captures stay valid between redraws; a throttled
  region is tagged again later (tick()) so its last change isn't missed. Menus aren't captured.

On Windows the main controller (actions.main_index) drives the real mouse while it points at the
screen: hover, trigger = left button (drag works), the other trigger = middle button (pan / orbit),
B = right click, the main stick = scroll wheel. A virtual keyboard can be shown under the screen. Clicks
are only sent when Blender's window is the one under that point on the monitor.

The screen's pose is kept relative to the VR navigation, so it travels with you when you fly,
walk or scale the world. Drag its title bar to move it.
"""

import time

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Quaternion, Vector

from . import hud, vr_ui, winput

CAPTURE_INTERVAL = 0.25  # seconds between captures of one region (region capture)
VIEW3D_SCALE = 0.5  # 3D views are re-rendered at this fraction of their size (region capture)
DISTANCE = 0.8  # hand-space meters in front of the headset when placed
PAD = 0.008
TITLE_GAP = 0.006
KEYBOARD_WIDTH = 0.5  # hand-space meters (at most the screen width)
UPLOADS_PER_DRAW = 6  # strips turned into textures per draw call, to spread the cost
NOTICE_SECONDS = 4.0

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


_captures = {}  # region pointer -> Capture (region capture)
_dirty = {}  # region pointer -> region, captures skipped by the throttle
_os = {"capturer": None, "strips": {}, "size": (0, 0)}  # OS capture: strip index -> (texture, w, h, y0)
_handles = []
_errors = []
# pose: screen matrix relative to the navigation (hand space). force: run without a VR session (tests).
state = {"pose": None, "force": False, "notice": ("", 0.0), "cursor": None, "drag": None}
keyboard = {"SHIFT": False, "CTRL": False, "ALT": False}


def active(context):
    settings = getattr(context.scene, "vr_sculpt", None)
    if settings is None or not settings.show_desktop:
        return False
    if state["force"]:
        return True
    from . import operators
    return operators.session_running(context)


def use_os(context):
    """Whether the picture comes from the OS (Windows) instead of region captures."""
    return winput.AVAILABLE and winput.find_window() is not None


def interactive(context):
    settings = context.scene.vr_sculpt
    return settings.show_desktop and settings.desktop_interactive and use_os(context)


def target_window(context):
    windows = context.window_manager.windows
    return windows[0] if len(windows) else None


def notice(text):
    state["notice"] = (text, time.monotonic())


# --- region capture (fallback) ---------------------------------------------

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
        if not active(context) or use_os(context) or context.window != target_window(context):
            return
        _capture_region(context, region, area)
    except Exception:
        if not _errors:
            import traceback
            traceback.print_exc()
        _errors.append(1)


def tick(context):
    """Start/stop the OS capture; for region capture, drop gone regions and re-tag throttled ones.
    With the mouse driven from VR, the cursor follows the main controller's ray (hover)."""
    if not active(context):
        clear()
        return
    if use_os(context):
        _tick_os()
        if interactive(context) and state["drag"] is None:
            ss = context.window_manager.xr_session_state
            if ss is not None and not state["force"]:
                hover(context, ss)
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


def _tick_os():
    hwnd = winput.find_window()
    cap = _os["capturer"]
    if cap is not None and (cap.hwnd != hwnd or not cap.is_alive()):
        cap.stop()
        _os["capturer"] = cap = None
    if cap is None and hwnd is not None:
        cap = _os["capturer"] = winput.Capturer(hwnd)
        cap.start()


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
    if _os["capturer"] is not None:
        _os["capturer"].stop()
        _os["capturer"] = None
    _os["strips"].clear()
    _os["size"] = (0, 0)
    release_drag()


def _upload_strips():
    """Turn strips the capture thread finished into textures (needs a GPU context: draw time)."""
    cap = _os["capturer"]
    if cap is None:
        return
    if cap.size != _os["size"]:
        _os["strips"].clear()
        _os["size"] = cap.size
    for i, (buf, w, h, y0) in cap.take(UPLOADS_PER_DRAW).items():
        _os["strips"][i] = (gpu.types.GPUTexture((w, h), format='RGBA8', data=buf), w, h, y0)


# --- the screen in VR ------------------------------------------------------

def navigation_matrix(ss):
    s = ss.navigation_scale
    return Matrix.LocRotScale(Vector(ss.navigation_location), Quaternion(ss.navigation_rotation), Vector((s, s, s)))


def pixel_size(context):
    """(width, height) in pixels of what the screen shows."""
    if use_os(context):
        _x, _y, w, h = winput.client_rect(winput.find_window())
        if w > 1 and h > 1:
            return w, h
    window = target_window(context)
    if window is None or window.width < 1:
        return 16, 9
    return window.width, window.height


def screen_size(context, width):
    pw, ph = pixel_size(context)
    return width, width * ph / pw


def place_in_front(context, ss):
    """Put the screen in front of the headset, facing it, a little below eye level."""
    viewer = Vector(ss.viewer_pose_location)
    forward = Quaternion(ss.viewer_pose_rotation) @ Vector((0.0, 0.0, -1.0))
    forward.z = 0.0
    if forward.length < 1e-6:
        forward = Vector((0.0, 1.0, 0.0))
    forward.normalize()
    scale = ss.viewer_scale
    size = screen_size(context, context.scene.vr_sculpt.desktop_size)
    drop = Vector((0.0, 0.0, (0.1 + size[1] / 2) * scale))
    anchor = viewer + forward * DISTANCE * scale - drop
    state["pose"] = navigation_matrix(ss).inverted_safe() @ hud.billboard(anchor, viewer - drop, scale)


def world_matrix(context, ss):
    """Screen matrix in world space (hand-space meters, origin at the bottom center)."""
    if state["pose"] is None:
        place_in_front(context, ss)
    return navigation_matrix(ss) @ state["pose"]


def set_world_matrix(ss, matrix):
    state["pose"] = navigation_matrix(ss).inverted_safe() @ matrix


def _plane_point(matrix, origin, direction, bounded=None):
    """(x, y) where the ray crosses matrix's XY plane (in front of the origin), or None."""
    inv = matrix.inverted_safe()
    o = inv @ Vector(origin)
    d = inv.to_3x3() @ Vector(direction)
    if abs(d.z) < 1e-9:
        return None
    t = -o.z / d.z
    if t <= 0:
        return None
    p = o + d * t
    return (p.x, p.y)


def ray_hit(context, ss, origin, direction):
    """Screen-space (x, y) where the ray hits the screen (frame included), or None."""
    p = _plane_point(world_matrix(context, ss), origin, direction)
    if p is None:
        return None
    w, h = screen_size(context, context.scene.vr_sculpt.desktop_size)
    if -w / 2 - PAD <= p[0] <= w / 2 + PAD and -PAD <= p[1] <= h + PAD:
        return p
    return None


# Title bar and keyboard: small vr_ui panels attached to the screen.

def _title_rows(context):
    settings = context.scene.vr_sculpt
    text, t = state["notice"]
    if not text or time.monotonic() - t > NOTICE_SECONDS:
        text = "Blender  \u2014  drag here to move" if interactive(context) else "Blender (view only)  \u2014  drag here to move"
    row = [vr_ui.label(text)]
    if interactive(context):
        row.append(vr_ui.button("desk_keyboard", "Keyboard",
                                lambda: setattr(settings, "desktop_keyboard", not settings.desktop_keyboard),
                                active=settings.desktop_keyboard))
    row.append(vr_ui.button("desk_hide", "Hide", lambda: setattr(settings, "show_desktop", False), danger=True))
    return [row]


def _key(id, text, vks, active=False):
    return vr_ui.button("key_" + id, text, lambda: type_keys(vks), active=active)


def _modifier(name, text):
    return vr_ui.button("key_" + name.lower(), text, lambda: keyboard.__setitem__(name, not keyboard[name]),
                        active=keyboard[name])


def _keyboard_rows():
    shift = keyboard["SHIFT"]

    def letters(chars):
        return [_key(c, c.upper() if shift else c, [winput.vk_for(c)]) for c in chars]

    vk = winput.VK
    return [
        [_key("esc", "Esc", [vk["ESC"]])] + letters("1234567890") + [_key("back", "Bksp", [vk["BACK"]])],
        [_key("tab", "Tab", [vk["TAB"]])] + letters("qwertyuiop") + [_key("del", "Del", [vk["DELETE"]])],
        [_modifier("SHIFT", "Shift")] + letters("asdfghjkl") + [_key("minus", "-", [vk["MINUS"]]),
                                                               _key("enter", "Enter", [vk["RETURN"]])],
        [_modifier("CTRL", "Ctrl")] + letters("zxcvbnm") + [_key("comma", ",", [vk["COMMA"]]),
                                                            _key("period", ".", [vk["PERIOD"]]),
                                                            _key("up", "^", [vk["UP"]]),
                                                            _key("f3", "F3", [vk["F3"]])],
        [_modifier("ALT", "Alt"), _key("f2", "F2", [vk["F2"]]), _key("space", "Space", [vk["SPACE"]]),
         _key("home", "Home", [vk["HOME"]]), _key("end", "End", [vk["END"]]),
         _key("left", "<", [vk["LEFT"]]), _key("down", "v", [vk["DOWN"]]), _key("right", ">", [vk["RIGHT"]])],
    ]


def type_keys(vks):
    """Press a key with the sticky modifiers (then release them), into Blender's window."""
    hwnd = winput.find_window()
    if hwnd is None:
        return False
    if not winput.ensure_foreground(hwnd):
        notice("Click the Blender window once: it isn't the active window")
        return False
    mods = [winput.VK[m] for m in ("CTRL", "ALT", "SHIFT") if keyboard[m]]
    winput.press_keys(mods + list(vks))
    for m in keyboard:
        keyboard[m] = False
    return True


def panels(context, ss):
    """[(name, matrix, widgets, height, width)] for the title bar and (if shown) the keyboard."""
    m = world_matrix(context, ss)
    w, h = screen_size(context, context.scene.vr_sculpt.desktop_size)
    widgets, th = vr_ui.layout(_title_rows(context), w + 2 * PAD)
    out = [("title", m @ Matrix.Translation((0.0, h + PAD + TITLE_GAP, 0.0)), widgets, th, w + 2 * PAD)]
    if interactive(context) and context.scene.vr_sculpt.desktop_keyboard:
        kw = min(KEYBOARD_WIDTH, w + 2 * PAD)
        widgets, kh = vr_ui.layout(_keyboard_rows(), kw)
        out.append(("keyboard", m @ Matrix.Translation((0.0, -PAD - TITLE_GAP - kh, 0.0)), widgets, kh, kw))
    return out


def hit(context, ss, origin, direction):
    """What the ray points at: ('content', (u, v), world point), ('title' or 'keyboard', widget or None,
    world point), or None. u, v are fractions of the window (left to right, bottom to top)."""
    if not context.scene.vr_sculpt.show_desktop:
        return None
    for name, matrix, widgets, height, width in panels(context, ss):
        p = vr_ui.ray_hit(matrix, origin, direction, height, 1.0, width)
        if p is not None:
            return (name, vr_ui.hit_test(widgets, *p), matrix @ Vector((p[0], p[1], 0.0)))
    m = world_matrix(context, ss)
    p = _plane_point(m, origin, direction)
    if p is None:
        return None
    w, h = screen_size(context, context.scene.vr_sculpt.desktop_size)
    if -w / 2 <= p[0] <= w / 2 and 0.0 <= p[1] <= h:
        return ('content', ((p[0] + w / 2) / w, p[1] / h), m @ Vector((p[0], p[1], 0.0)))
    if -w / 2 - PAD <= p[0] <= w / 2 + PAD and -PAD <= p[1] <= h + PAD:
        return ('title', None, m @ Vector((p[0], p[1], 0.0)))  # The frame moves the screen too.
    return None


def content_uv(context, ss, origin, direction):
    """Window fractions where the ray crosses the screen's plane, clamped (for drags), or None."""
    p = _plane_point(world_matrix(context, ss), origin, direction)
    if p is None:
        return None
    w, h = screen_size(context, context.scene.vr_sculpt.desktop_size)
    return (min(max((p[0] + w / 2) / w, 0.0), 1.0), min(max(p[1] / h, 0.0), 1.0))


def _main_ray(context, ss):
    from . import actions, interactions, operators
    main = actions.main_index(context)
    pose = operators.controller_pose(ss.controller_aim_location_get(context, main),
                                     ss.controller_aim_rotation_get(context, main))
    return pose.translation, interactions.aim_direction(pose)


def hover(context, ss):
    """Move the real cursor to where the main controller points at the screen."""
    h = hit(context, ss, *_main_ray(context, ss))
    if h is None or h[0] != 'content':
        return
    hwnd = winput.find_window()
    x, y = winput.to_screen(hwnd, *h[1])
    if state["cursor"] != (x, y):
        state["cursor"] = (x, y)
        winput.move(x, y)


def pointing_at_content(context, ss):
    if not interactive(context):
        return False
    h = hit(context, ss, *_main_ray(context, ss))
    return h is not None and h[0] == 'content'


def release_drag():
    drag = state["drag"]
    if drag is not None and drag.pressed:
        winput.button(drag.button, False)
        drag.pressed = False
    state["drag"] = None


class MouseDrag:
    """Trigger handler: a mouse button held while the main controller's ray moves the cursor.

    The main trigger is the left button, the other trigger the middle one; both follow the
    main ray, so the other hand can pan while the main one points.
    """

    def __init__(self, context, ss, button):
        self.ss = ss
        self.button = button
        self.changed = False
        self.pressed = False
        release_drag()
        hwnd = winput.find_window()
        uv = content_uv(context, ss, *_main_ray(context, ss))
        if hwnd is None or uv is None:
            return
        x, y = winput.to_screen(hwnd, *uv)
        winput.move(x, y)
        state["cursor"] = (x, y)
        if not winput.ours_at(hwnd, x, y):
            winput.ensure_foreground(hwnd)
        if not winput.ours_at(hwnd, x, y):
            notice("Blender's window is covered on the monitor: click not sent")
            return
        winput.button(button, True)
        self.pressed = True
        state["drag"] = self

    def update(self, context, pose, tip_pose, value):
        if not self.pressed:
            return
        uv = content_uv(context, self.ss, *_main_ray(context, self.ss))
        if uv is None:
            return
        x, y = winput.to_screen(winput.find_window(), *uv)
        if state["cursor"] != (x, y):
            state["cursor"] = (x, y)
            winput.move(x, y)

    def finish(self, context):
        if state["drag"] is self:
            release_drag()

    def abandon(self):
        self.finish(None)  # Don't leave the real mouse button held down.


def right_click(context, ss):
    """B while pointing at the screen."""
    hwnd = winput.find_window()
    uv = content_uv(context, ss, *_main_ray(context, ss))
    if hwnd is None or uv is None:
        return False
    x, y = winput.to_screen(hwnd, *uv)
    winput.move(x, y)
    if not winput.ours_at(hwnd, x, y):
        winput.ensure_foreground(hwnd)
    if not winput.ours_at(hwnd, x, y):
        notice("Blender's window is covered on the monitor: click not sent")
        return False
    winput.button('RIGHT', True)
    winput.button('RIGHT', False)
    return True


def scroll(context, ss, notches):
    hwnd = winput.find_window()
    if hwnd is None:
        return
    x, y = winput.cursor_pos()
    if winput.ours_at(hwnd, x, y):
        winput.wheel(notches)


class ButtonPress:
    """Trigger handler for the title bar and keyboard buttons: they fire on press."""

    def __init__(self, widget):
        self.changed = False
        if widget is not None and widget.on_press:
            widget.on_press()

    def update(self, context, pose, tip_pose, value):
        pass

    def finish(self, context):
        pass


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


def pick(context, ss, hand, pose):
    """Trigger handler for a press aimed at the screen, or None."""
    from . import actions
    settings = context.scene.vr_sculpt
    if not settings.show_desktop:
        return None
    if actions.is_off_hand(hand, context):
        # Off-hand trigger: middle button, while the main hand points at the screen.
        if pointing_at_content(context, ss):
            return MouseDrag(context, ss, 'MIDDLE')
        return None
    from . import interactions
    h = hit(context, ss, pose.translation, interactions.aim_direction(pose))
    if h is None:
        return None
    kind, target, _point = h
    if kind == 'content':
        if interactive(context):
            return MouseDrag(context, ss, 'LEFT')
        return MoveScreen(context, ss, pose)  # View only: drag the whole screen.
    if target is not None and target.kind == 'button':
        return ButtonPress(target)
    return MoveScreen(context, ss, pose)


# --- drawing ---------------------------------------------------------------

def _textured(image, pts, tex):
    batch = batch_for_shader(image, 'TRI_FAN', {"pos": pts, "texCoord": ((0, 0), (1, 0), (1, 1), (0, 1))})
    image.uniform_sampler("image", tex)
    batch.draw(image)


def draw(context, ss, right_pose=None):
    """The screen, its title bar and keyboard, and the main controller's ray when it points at them."""
    matrix = world_matrix(context, ss)
    w, h = screen_size(context, context.scene.vr_sculpt.desktop_size)
    os_mode = use_os(context)
    if os_mode:
        _upload_strips()
    pointed = None
    if right_pose is not None:
        from . import interactions
        pointed = hit(context, ss, right_pose.translation, interactions.aim_direction(right_pose))

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    image = gpu.shader.from_builtin('IMAGE')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(matrix)
    frame = [(-w / 2 - PAD, -PAD, 0), (w / 2 + PAD, -PAD, 0), (w / 2 + PAD, h + PAD, 0), (-w / 2 - PAD, h + PAD, 0)]
    batch = batch_for_shader(shader, 'TRIS', {"pos": frame}, indices=((0, 1, 2), (0, 2, 3)))
    shader.uniform_float("color", (0.12, 0.12, 0.13, 0.95))
    batch.draw(shader)
    shown = False
    if os_mode:
        pw, ph = _os["size"]
        for _i, (tex, sw, sh, y0) in sorted(_os["strips"].items()):
            if pw < 1 or ph < 1:
                break
            y_lo, y_hi = h * y0 / ph, h * (y0 + sh) / ph
            _textured(image, [(-w / 2, y_lo, 0.0002), (w / 2, y_lo, 0.0002), (w / 2, y_hi, 0.0002),
                              (-w / 2, y_hi, 0.0002)], tex)
            shown = True
    else:
        window = target_window(context)
        sx, sy = w / max(window.width, 1), h / max(window.height, 1)
        for cap in sorted(_captures.values(), key=lambda c: c.order):
            if cap.texture is None:
                continue
            x0, y0 = -w / 2 + cap.x * sx, cap.y * sy
            x1, y1 = x0 + cap.w * sx, y0 + cap.h * sy
            z = 0.0002 * (1 + cap.order)
            _textured(image, [(x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)], cap.texture)
            shown = True
    if pointed is not None and pointed[0] == 'content':
        # Where the ray points (the OS cursor isn't in the capture).
        import math
        u, v = pointed[1]
        cx, cy, r = -w / 2 + u * w, v * h, 0.005
        ring = [(cx + r * math.cos(a), cy + r * math.sin(a), 0.001) for a in (2 * math.pi * i / 24 for i in range(24))]
        for color, rr in (((0.0, 0.0, 0.0, 0.9), 1.25), ((1.0, 1.0, 1.0, 1.0), 1.0)):
            pts = [(cx + (x - cx) * rr, cy + (y - cy) * rr, z) for x, y, z in ring]
            batch = batch_for_shader(shader, 'LINE_LOOP', {"pos": pts})
            shader.uniform_float("color", color)
            batch.draw(shader)
    gpu.matrix.pop()
    if not shown:
        hud.draw_text(matrix, -w / 2 + 0.01, h / 2, "Waiting for the desktop window...", 0.012,
                      (0.85, 0.85, 0.88, 1.0))

    from . import interactions
    for name, pm, widgets, height, width in panels(context, ss):
        hover_id = pointed[1].id if pointed is not None and pointed[0] == name and pointed[1] else None
        vr_ui.draw_panel(pm, widgets, height, hover_id, interactions.pressed_id, width=width)
    if pointed is not None and right_pose is not None:
        vr_ui.draw_ray(right_pose.translation, pointed[2], True)
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
