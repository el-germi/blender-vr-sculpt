"""Stylized controllers drawn at the hands, in the hand panel's visual style, with what each button
does right now.

Blender draws the runtime's controller model when it gets one (XR_MSFT_controller_model); SteamVR
gives none, so it falls back to gray spheres. With Stylized Controllers on, Blender's are hidden
while the session runs and these are drawn instead: a Touch-like body, tracking ring, thumbstick,
face buttons, trigger and grip, built here from simple geometry (no model files). Pressed parts
light up in the accent color (the trigger by how far it's pulled) and the stick tilts with yours.

Beside each controller, a short legend says what the buttons do now (hints()): it fades in while
you look at that hand or hold it still, and out while you work, like the hand panel's reveal.
"""

import math
import time
from collections import namedtuple

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Quaternion, Vector

from . import actions, gfx, hud, vr_ui

# --- what the buttons do -----------------------------------------------------------

BUTTONS = ('top', 'bottom', 'stick', 'stick_click', 'trigger', 'grip')  # legend order, top to bottom
KEY_NAMES = {
    'left': {'top': "Y", 'bottom': "X", 'stick': "Stick", 'stick_click': "Click", 'trigger': "Trigger",
             'grip': "Grip"},
    'right': {'top': "B", 'bottom': "A", 'stick': "Stick", 'stick_click': "Click", 'trigger': "Trigger",
              'grip': "Grip"},
}

# What hints() needs besides the settings. Gathered by context_info() in VR; tests build it by hand.
Info = namedtuple("Info", "ray_on_panel ray_on_desktop picker_open cut_target has_rig paint_ready native_brush "
                          "primitive", defaults=(False, False, False, False, False, True, "", "Cube"))

NATIVE_LABELS = {"Draw": "Draw", "Clay Strips": "Clay", "Smooth": "Smooth", "Inflate/Deflate": "Inflate",
                 "Crease Sharp": "Crease", "Flatten/Contrast": "Flatten"}
NEXT_MODE = {'SCULPT': "Model", 'MODEL': "Draw", 'DRAW': "Paint", 'PAINT': "Pose", 'POSE': "Tools",
             'TOOLS': "Sculpt"}


def hints(s, info):
    """{'left': {button: (icon, label) or None}, 'right': {...}}: what each button does right now, for
    settings s (mode, tool, ...) and info (where the ray is, the selection, ...). Pure: no Blender data."""
    right = {b: None for b in BUTTONS}
    left = {b: None for b in BUTTONS}
    mode = s.mode

    # Triggers.
    trigger = _trigger(s, info)
    right['trigger'] = trigger
    left['trigger'] = _left_trigger(s, info, trigger)
    if info.picker_open and info.ray_on_panel:
        right['trigger'] = ("check", "Choose")
    elif info.ray_on_panel:
        right['trigger'] = ("cursor", "Press")
    elif info.ray_on_desktop:
        right['trigger'] = ("cursor", "Click")
        left['trigger'] = ("cursor", "Middle click")

    # Face buttons. Y: next mode; B: per mode (cycle_brush in operators.py); A / X: Blender's reset view.
    left['top'] = ("chevron_right", "Next: " + NEXT_MODE.get(mode, "Sculpt"))
    if info.ray_on_desktop:
        right['top'] = ("cursor", "Right click")
    elif mode == 'SCULPT':
        right['top'] = ("chevron_right", "Next brush")
    elif mode == 'MODEL':
        right['top'] = ("chevron_right", "Next shape")
    elif mode in {'DRAW', 'PAINT'}:
        right['top'] = ("undo", "Undo stroke")
    elif mode == 'POSE':
        right['top'] = ("swap", "IK" if s.pose_tool == 'FK' else "FK")
    elif mode == 'TOOLS':
        right['top'] = ("chevron_right", "Next camera") if s.tool == 'CAMERA' else ("undo", "Undo measure")
    left['bottom'] = right['bottom'] = ("reset", "Reset view")

    # Sticks.
    walk = s.nav_mode == 'WALK'
    left['stick'] = ("walk", "Walk") if walk else ("fly", "Fly")
    right['stick'] = ("cursor", "Scroll") if info.ray_on_desktop else \
        (("reset", "Turn") if walk else ("fly", "Up, down, turn"))
    left['stick_click'] = ("pinned", "Unpin panel") if s.panel_pin else ("pin", "Pin panel")
    if mode == 'SCULPT':
        right['stick_click'] = ("plus", "Brush size")
    elif mode == 'PAINT':
        right['stick_click'] = ("plus", "Paint size")
    elif mode == 'POSE':
        right['stick_click'] = ("plus", "IK chain")

    # Grips: Blender's own navigation grab.
    left['grip'] = right['grip'] = ("here", "Move world")
    return {'left': left, 'right': right}


def _trigger(s, info):
    mode = s.mode
    if mode == 'SCULPT':
        if s.engine == 'NATIVE':
            return ("sculpt", NATIVE_LABELS.get(info.native_brush, info.native_brush or "Sculpt"))
        return ("sculpt", s.brush.title())
    if mode == 'MODEL':
        if s.model_mark != 'NONE':
            return ("edit", {'HARD': "Mark hard", 'SOFT': "Mark soft"}.get(s.model_mark, "Clear marks"))
        if s.model_add_as != 'OBJECT' and info.cut_target:
            op = s.model_add_as
            return ({'DIFFERENCE': "cut", 'UNION': "union"}.get(op, "intersect"),
                    {'DIFFERENCE': "Cut", 'UNION': "Union"}.get(op, "Intersect"))
        return ("model", "Add " + info.primitive + ", grab")
    if mode == 'DRAW':
        if s.draw_tool == 'EDIT':
            return ("edit", "Edit stroke")
        if s.draw_tool == 'ERASE':
            return ("eraser", "Erase")
        return (s.draw_style.lower(), s.draw_style.title())
    if mode == 'PAINT':
        if not info.paint_ready:
            return None
        if s.paint_tool == 'PICK':
            return ("eyedropper", "Pick color")
        return ("spray", "Spray") if s.paint_reach == 'SPRAY' else ("paint", "Paint")
    if mode == 'POSE':
        return ("bones", "Pose " + s.pose_tool) if info.has_rig else ("cursor", "Move object")
    if mode == 'TOOLS':
        return ("camera", "Aim camera") if s.tool == 'CAMERA' else ("ruler", "Measure")
    return None


def _left_trigger(s, info, right):
    mode = s.mode
    if mode == 'SCULPT':
        inverts = s.left_inverts and (s.engine == 'NATIVE' or s.brush in {'DRAW', 'INFLATE'})
        if inverts and right is not None:
            return ("sculpt", right[1] + ", inverted")
    if mode == 'MODEL' and s.model_mark != 'NONE':
        return ("edit", "Clear marks")
    if mode == 'PAINT' and right is not None and s.paint_tool != 'PICK':
        return (right[0], "2nd color")
    if mode == 'PAINT' and right is not None:
        return ("eyedropper", "Pick 2nd color")
    return right


def context_info(context, ss):
    """Info for hints() from the scene and the session."""
    from . import desktop, hardsurface, interactions, menus, native, operators, paint, pose, presence, primitives
    settings = context.scene.vr_sculpt
    right = operators.controller_pose(ss.controller_aim_location_get(context, 1),
                                      ss.controller_aim_rotation_get(context, 1))
    on_panel = False
    if settings.show_panel and presence.alpha() > 0.5:
        on_panel = interactions.panel_pointer(context, ss, right)[1] is not None
    brush = native.sculpt_brush(context) if settings.engine == 'NATIVE' else None
    obj = paint.target(context) if settings.mode == 'PAINT' else None
    return Info(
        ray_on_panel=on_panel,
        ray_on_desktop=not on_panel and desktop.pointing_at_content(context, ss),
        picker_open=menus.picker_open(),
        cut_target=settings.mode == 'MODEL' and hardsurface.cut_target(context) is not None,
        has_rig=settings.mode == 'POSE' and pose.rig(context) is not None,
        paint_ready=obj is not None and paint.ready(obj),
        native_brush=brush.name if brush else "",
        primitive=primitives.label(settings.primitive),
    )


# --- reading the buttons -----------------------------------------------------------

# (our action, the stock one it was copied from): the stock name is read when sculpting is off.
_TRIGGER = (actions.ACTION_STROKE, "teleport")
_TOP = (actions.ACTION_BRUSH, "viewfinder_apply_action_up")
_STICK_CLICK = (actions.ACTION_RADIUS, "viewfinder_swap_hands")
_STICK_AXES = {  # hand -> ((+x, -x), (+y, -y)) actions
    0: (("fly_right", "fly_left"), ("fly_forward", "fly_back")),
    1: (("fly_turnright", "fly_turnleft"), ("fly_up", "fly_down")),
}


def _state(context, ss, names, hand_idx):
    for name in names:
        try:
            v = ss.action_state_get(context, actions.ACTIONMAP, name, actions.HANDS[hand_idx])
        except Exception:
            continue
        if v is None:
            continue
        return float(v if isinstance(v, (int, float)) else v[0])
    return 0.0


def _axis(context, ss, pos, neg, hand_idx):
    a = _state(context, ss, (pos,), hand_idx)
    b = _state(context, ss, (neg,), hand_idx)
    # Each half of the axis is its own action; depending on the runtime both read the signed axis, or
    # each its own half as a magnitude.
    v = a if a == b else abs(a) - abs(b)
    return max(-1.0, min(1.0, v))


Inputs = namedtuple("Inputs", "trigger grip top bottom stick_click stick_x stick_y")


def read_inputs(context, ss, hand_idx):
    (xp, xn), (yp, yn) = _STICK_AXES[hand_idx]
    return Inputs(
        trigger=_state(context, ss, _TRIGGER, hand_idx),
        grip=_state(context, ss, ("nav_grab",), hand_idx),
        top=_state(context, ss, _TOP, hand_idx) > 0.3,
        bottom=_state(context, ss, ("nav_reset",), hand_idx) > 0.3,
        stick_click=_state(context, ss, _STICK_CLICK, hand_idx) > 0.3,
        stick_x=_axis(context, ss, xp, xn, hand_idx),
        stick_y=_axis(context, ss, yp, yn, hand_idx),
    )


def working(inputs):
    """Whether a hand is busy: hints fade while it is."""
    return (inputs.trigger > 0.1 or inputs.grip > 0.3 or inputs.top or inputs.bottom or inputs.stick_click or
            abs(inputs.stick_x) > 0.3 or abs(inputs.stick_y) > 0.3)


# --- when the hints show -----------------------------------------------------------------

LOOK = math.cos(math.radians(28))  # the hand within 28 degrees of where you look
STILL_SPEED = 0.12  # hand-space meters per second
STILL_TIME = 1.2  # seconds held still before the hints show
HINT_IN = 0.18  # fade time constants, seconds
HINT_OUT = 0.08

_hands = [{"pos": None, "time": None, "still": 0.0, "alpha": 0.0} for _ in range(2)]


def hint_target(looking, still_for, busy):
    """Whether a hand's hints want to show."""
    return not busy and (looking or still_for >= STILL_TIME)


def update_hints(hand_idx, pos, scale, looking, busy, now):
    """Advance a hand's hint fade; returns its alpha."""
    h = _hands[hand_idx]
    dt = 0.0 if h["time"] is None else max(now - h["time"], 0.0)
    if h["pos"] is not None and dt > 0:
        speed = (Vector(pos) - h["pos"]).length / dt / max(scale, 1e-6)
        h["still"] = h["still"] + dt if speed < STILL_SPEED else 0.0
    h["pos"], h["time"] = Vector(pos), now
    target = 1.0 if hint_target(looking, h["still"], busy) else 0.0
    h["alpha"] = target + (h["alpha"] - target) * math.exp(-dt / (HINT_IN if target else HINT_OUT))
    if abs(h["alpha"] - target) < 0.004:
        h["alpha"] = target
    return h["alpha"]


def reset_hints():
    for h in _hands:
        h.update(pos=None, time=None, still=0.0, alpha=0.0)


# --- Blender's controllers off while ours are on -------------------------------------

_saved = {"show": None}  # Blender's show_controllers before the session hid them


def on_session_start(context):
    """Hide Blender's controller drawing (runs in the session start handler)."""
    if not context.scene.vr_sculpt.own_controllers:
        return
    xs = context.window_manager.xr_session_settings
    if _saved["show"] is None:
        _saved["show"] = xs.show_controllers
    xs.show_controllers = False


def restore(context):
    """Put Blender's controller drawing back as it was (session over, add-on off, option off)."""
    if _saved["show"] is None:
        return
    try:
        context.window_manager.xr_session_settings.show_controllers = _saved["show"]
    except AttributeError:
        return  # No window manager (e.g. quitting); keep the saved value for next time.
    _saved["show"] = None


def on_option(settings, context):
    """Stylized Controllers switched in the middle of a session."""
    from . import operators
    if not operators.session_running(context):
        return
    if settings.own_controllers:
        on_session_start(context)
    else:
        restore(context)


def tick(context, running):
    """From the app timer: restore Blender's controllers once the session is over."""
    if not running:
        restore(context)


# --- geometry -------------------------------------------------------------------------

# The controller's own frame: origin at the face's center, +Y out of the face, -Z forward (toward the
# ring), +X right: the aim pose's axes, centered above the grip. The handle leans back from it.
FACE_TILT = math.radians(0.0)
FACE_ABOVE_GRIP = 0.042  # hand-space meters from the grip pose to the face's center, along +Y

LIGHT = Vector((-0.35, 1.0, 0.45)).normalized()

COL_BODY = (0.17, 0.178, 0.2)
COL_FACE = (0.23, 0.24, 0.27)
COL_RING = (0.2, 0.21, 0.24)
COL_BUTTON = (0.42, 0.44, 0.49)
COL_STICK = (0.34, 0.355, 0.4)


def _shade(color, normal):
    k = 0.32 + 0.68 * max(normal.dot(LIGHT), 0.0) + 0.1 * max(-normal.z, 0.0)
    return (min(color[0] * k * 1.6, 1.0), min(color[1] * k * 1.6, 1.0), min(color[2] * k * 1.6, 1.0), 1.0)


def _loft(rings, centers, cap_bottom=True, cap_top=True):
    """Triangles (a, b, c, outward normal) joining rings of equal length; centers: each ring's center."""
    tris = []
    for (r0, c0), (r1, c1) in zip(zip(rings, centers), zip(rings[1:], centers[1:])):
        n = len(r0)
        for i in range(n):
            j = (i + 1) % n
            a, b, c, d = r0[i], r0[j], r1[j], r1[i]
            mid = (a + b + c + d) / 4
            out = (mid - (c0 + c1) / 2)
            normal = (b - a).cross(d - a).normalized()
            if normal.dot(out) < 0:
                normal = -normal
            tris += [(a, b, c, normal), (a, c, d, normal)]
    for ring, center, on, sign in ((rings[0], centers[0], cap_bottom, -1), (rings[-1], centers[-1], cap_top, 1)):
        if not on:
            continue
        axis = (centers[-1] - centers[0]).normalized() * sign
        for i in range(len(ring)):
            tris.append((center, ring[i], ring[(i + 1) % len(ring)], axis))
    return tris


def _ellipse(center, rx, rz, n=18, y_axis=Vector((0, 1, 0))):
    """A ring of points around center in the plane across y_axis (rx along X, rz along the rest)."""
    x = Vector((1, 0, 0))
    z = x.cross(y_axis).normalized()
    x = y_axis.cross(z).normalized()
    return [center + x * (rx * math.cos(2 * math.pi * i / n)) + z * (rz * math.sin(2 * math.pi * i / n))
            for i in range(n)]


def _cylinder(base, radius, height, n=14, axis=Vector((0, 1, 0))):
    top = base + axis * height
    return _loft([_ellipse(base, radius, radius, n, axis), _ellipse(top, radius, radius, n, axis)], [base, top])


def _box(center, size, rot=None):
    hx, hy, hz = size[0] / 2, size[1] / 2, size[2] / 2
    m = rot or Matrix.Identity(3)
    corners = [center + m @ Vector((sx * hx, sy * hy, sz * hz)) for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
    faces = ((0, 1, 3, 2, (-1, 0, 0)), (4, 6, 7, 5, (1, 0, 0)), (0, 4, 5, 1, (0, -1, 0)), (2, 3, 7, 6, (0, 1, 0)),
             (0, 2, 6, 4, (0, 0, -1)), (1, 5, 7, 3, (0, 0, 1)))
    tris = []
    for a, b, c, d, n in faces:
        normal = m @ Vector(n)
        tris += [(corners[a], corners[b], corners[c], normal), (corners[a], corners[c], corners[d], normal)]
    return tris


def _torus(center, normal, radius, tube, n=30, m=6):
    normal = normal.normalized()
    e1 = normal.orthogonal().normalized()
    e2 = normal.cross(e1)
    tris = []

    def point(u, v):
        radial = e1 * math.cos(u) + e2 * math.sin(u)
        return center + radial * (radius + tube * math.cos(v)) + normal * (tube * math.sin(v)), \
            radial * math.cos(v) + normal * math.sin(v)
    for i in range(n):
        for k in range(m):
            u0, u1 = 2 * math.pi * i / n, 2 * math.pi * (i + 1) / n
            v0, v1 = 2 * math.pi * k / m, 2 * math.pi * (k + 1) / m
            (a, na), (c, nc) = point(u0, v0), point(u1, v1)
            b, d = point(u1, v0)[0], point(u0, v1)[0]
            normal_q = (na + nc).normalized()
            tris += [(a, b, c, normal_q), (a, c, d, normal_q)]
    return tris


# Where the parts are, per hand ('left' / 'right'), in the controller frame: name -> (point, pivot).
def layout(hand):
    inward = 1.0 if hand == 'left' else -1.0  # toward the other hand
    return {
        'stick': Vector((-inward * 0.011, 0.004, -0.002)),
        'top': Vector((inward * 0.012, 0.004, -0.013)),
        'bottom': Vector((inward * 0.013, 0.004, 0.004)),
        'trigger': Vector((0.0, -0.017, -0.026)),
        'grip': Vector((inward * 0.016, -0.045, 0.004)),
        'ring': Vector((-inward * 0.01, 0.012, -0.03)),
    }


def parts(hand):
    """{part: list of (a, b, c, normal)} for one hand, in the controller frame (meters)."""
    p = layout(hand)
    inward = 1.0 if hand == 'left' else -1.0
    out = {}
    # Handle: a tapered, slightly backward-leaning oval.
    centers, rings = [], []
    for y, r in ((-0.002, 0.021), (-0.02, 0.0195), (-0.045, 0.0185), (-0.07, 0.0175), (-0.092, 0.0158),
                 (-0.103, 0.011)):
        c = Vector((0.0, y, 0.006 - 0.42 * y))
        centers.append(c)
        rings.append(_ellipse(c, r * 0.92, r, 18))
    out['body'] = _loft(rings, centers, cap_top=False)
    # Face plate: a flat oval slab on top.
    c0, c1 = Vector((0, -0.004, -0.004)), Vector((0, 0.004, -0.004))
    out['face'] = _loft([_ellipse(c0, 0.03, 0.028, 22), _ellipse(c1, 0.029, 0.027, 22)], [c0, c1])
    # Tracking ring: a halo arching over the front of the face, leaning out.
    out['ring'] = _torus(p['ring'], Vector((-inward * 0.45, 0.35, -0.82)), 0.036, 0.0034)
    out['top'] = _cylinder(p['top'], 0.0058, 0.003)
    out['bottom'] = _cylinder(p['bottom'], 0.0058, 0.003)
    # Stick: a base, then the moving part (shaft and cap) around its pivot.
    out['stick_base'] = _cylinder(p['stick'] - Vector((0, 0.001, 0)), 0.0105, 0.0016)
    out['stick'] = _cylinder(p['stick'], 0.003, 0.008, 10) + _cylinder(p['stick'] + Vector((0, 0.007, 0)), 0.009,
                                                                       0.004)
    trig_rot = Matrix.Rotation(math.radians(-25), 3, 'X')
    out['trigger'] = _box(p['trigger'], (0.015, 0.024, 0.008), trig_rot)
    out['grip'] = _box(p['grip'], (0.007, 0.034, 0.016), Matrix.Rotation(math.radians(12), 3, 'X'))
    return out


PART_COLORS = {'body': COL_BODY, 'face': COL_FACE, 'ring': COL_RING, 'top': COL_BUTTON, 'bottom': COL_BUTTON,
               'stick_base': COL_RING, 'stick': COL_STICK, 'trigger': COL_BUTTON, 'grip': COL_BUTTON}

_batches = {}  # (hand, part) -> (shaded batch, plain batch)


def _part_batches(hand, name, tris):
    key = (hand, name)
    b = _batches.get(key)
    if b is None:
        pos, col = [], []
        color = PART_COLORS[name]
        for a, bb, c, n in tris:
            pos += [a[:], bb[:], c[:]]
            col += [_shade(color, n)] * 3
        smooth = gpu.shader.from_builtin('SMOOTH_COLOR')
        plain = gpu.shader.from_builtin('UNIFORM_COLOR')
        b = _batches[key] = (batch_for_shader(smooth, 'TRIS', {"pos": pos, "color": col}),
                             batch_for_shader(plain, 'TRIS', {"pos": pos}))
    return b


_parts = {}


def _hand_parts(hand):
    if hand not in _parts:
        _parts[hand] = parts(hand)
    return _parts[hand]


def controller_matrix(ss, context, hand_idx):
    """World matrix of a controller's frame (scaled by the viewer scale)."""
    aim = Quaternion(ss.controller_aim_rotation_get(context, hand_idx)).to_matrix().to_4x4()
    rot = aim @ Matrix.Rotation(FACE_TILT, 4, 'X')
    scale = ss.viewer_scale
    grip = Vector(ss.controller_grip_location_get(context, hand_idx))
    m = rot @ Matrix.Scale(scale, 4)
    m.translation = grip + rot.to_3x3() @ Vector((0.0, FACE_ABOVE_GRIP * scale, 0.0))
    return m


# --- drawing ----------------------------------------------------------------------------

def draw(context, ss, xr=True):
    """Both controllers, with their pressed parts lit."""
    settings = context.scene.vr_sculpt
    if not settings.own_controllers:
        return
    smooth = gpu.shader.from_builtin('SMOOTH_COLOR')
    plain = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('LESS_EQUAL')
    gpu.state.depth_mask_set(True)
    for idx, hand in enumerate(('left', 'right')):
        inputs = read_inputs(context, ss, idx)
        lit = {'trigger': inputs.trigger, 'grip': min(inputs.grip, 1.0), 'top': 1.0 if inputs.top else 0.0,
               'bottom': 1.0 if inputs.bottom else 0.0, 'stick': 1.0 if inputs.stick_click else 0.0}
        tilt = math.hypot(inputs.stick_x, inputs.stick_y)
        if tilt > 0.3:
            lit['stick'] = max(lit['stick'], 0.35 * min(tilt, 1.0))
        m = controller_matrix(ss, context, idx)
        where = layout(hand)
        for name, tris in _hand_parts(hand).items():
            local = Matrix.Identity(4)
            if name == 'stick':
                # Tilts with your stick (about 20 degrees at full), and sinks a little when clicked.
                pivot = where['stick']
                tilt_m = Matrix.Rotation(math.radians(20) * inputs.stick_x, 4, 'Z').inverted() @ \
                    Matrix.Rotation(math.radians(20) * inputs.stick_y, 4, 'X').inverted()
                sink = Vector((0.0, -0.0012 if inputs.stick_click else 0.0, 0.0))
                local = Matrix.Translation(pivot + sink) @ tilt_m @ Matrix.Translation(-pivot)
            elif name == 'trigger':
                pivot = where['trigger'] + Vector((0, 0.012, 0))
                local = Matrix.Translation(pivot) @ Matrix.Rotation(math.radians(14) * inputs.trigger, 4, 'X') @ \
                    Matrix.Translation(-pivot)
            elif name in {'top', 'bottom'} and lit[name]:
                local = Matrix.Translation((0, -0.0015, 0))
            shaded, flat = _part_batches(hand, name, tris)
            gpu.matrix.push()
            gpu.matrix.multiply_matrix(m @ local)
            shaded.draw(smooth)
            level = lit.get(name, 0.0)
            if level > 0.01:
                plain.uniform_float("color", (*vr_ui.COL_ACCENT[:3], 0.25 + 0.65 * level))
                flat.draw(plain)
            gpu.matrix.pop()
    gpu.state.depth_mask_set(False)
    gpu.state.depth_test_set('NONE')
    gpu.state.blend_set('NONE')


LEGEND_TEXT = 0.0052
LEGEND_ROW = 0.0105
LEGEND_SIDE = 0.034  # hand-space meters from the controller to the legend


def draw_hints(context, ss, now=None, info=None):
    """The legends beside both controllers, faded per hand."""
    settings = context.scene.vr_sculpt
    if not (settings.own_controllers and settings.controller_hints):
        return
    now = time.monotonic() if now is None else now
    head = Vector(ss.viewer_pose_location)
    head_rot = getattr(ss, "viewer_pose_rotation", None)
    forward = Quaternion(head_rot).to_matrix() @ Vector((0, 0, -1)) if head_rot is not None else None
    alphas = []
    for idx in range(2):
        grip = Vector(ss.controller_grip_location_get(context, idx))
        looking = forward is None or (grip - head).normalized().dot(forward) > LOOK
        alphas.append(update_hints(idx, grip, ss.viewer_scale, looking, working(read_inputs(context, ss, idx)), now))
    if max(alphas) <= 0.01:
        return
    table = hints(settings, info or context_info(context, ss))
    for idx, hand in enumerate(('left', 'right')):
        if alphas[idx] > 0.01:
            _draw_legend(context, ss, idx, hand, table[hand], alphas[idx], head)


def _draw_legend(context, ss, idx, hand, rows, alpha, head):
    scale = ss.viewer_scale
    m = controller_matrix(ss, context, idx)
    rows = [(b, rows[b]) for b in BUTTONS if rows.get(b)]
    if not rows:
        return
    side = -1.0 if hand == 'left' else 1.0
    anchor = m @ Vector((0.0, 0.01, 0.0))
    board = hud.billboard(anchor, head, scale)
    keys = KEY_NAMES[hand]
    key_w = max(hud.text_width(keys[b], LEGEND_TEXT * 0.85) for b, _h in rows) + 0.003
    text_w = max(hud.text_width(label, LEGEND_TEXT) for _b, (_i, label) in rows)
    width = 0.004 + key_w + LEGEND_TEXT * 1.4 + 0.002 + text_w + 0.004
    height = len(rows) * LEGEND_ROW + 0.004
    x0 = LEGEND_SIDE if side > 0 else -LEGEND_SIDE - width
    top = height / 2
    cv = gfx.Canvas()
    cv.rect(x0, top - height, x0 + width, top, (*vr_ui.COL_CARD[:3], 0.72), 0.004)
    texts = []
    leaders = []
    where = layout(hand)
    part_point = {'top': where['top'], 'bottom': where['bottom'], 'stick': where['stick'] + Vector((0, 0.011, 0)),
                  'stick_click': where['stick'] + Vector((0, 0.011, 0)), 'trigger': where['trigger'],
                  'grip': where['grip']}
    inv = board.inverted_safe()
    for i, (button, (icon, label)) in enumerate(rows):
        y = top - 0.002 - LEGEND_ROW * (i + 0.5)
        kx = x0 + 0.004
        texts.append((kx, y, keys[button], LEGEND_TEXT * 0.85, vr_ui.COL_FAINT))
        ix = kx + key_w + LEGEND_TEXT * 0.7
        gfx.icon(cv, icon, ix, y, LEGEND_TEXT * 1.4, vr_ui.COL_ACCENT)
        texts.append((ix + LEGEND_TEXT * 0.7 + 0.002, y, label, LEGEND_TEXT, vr_ui.COL_TEXT))
        # A faint leader from the row's inner end to the part, in the legend's plane.
        end = inv @ (m @ part_point[button])
        start = (x0 if side > 0 else x0 + width, y)
        leaders.append((start, (end.x, end.y)))
    for start, end in leaders:
        cv.line(start, end, 0.0003, (1.0, 1.0, 1.0, 0.18), caps=False)
        cv.disc(end[0], end[1], 0.0009, (1.0, 1.0, 1.0, 0.45))
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(board)
    cv.draw(alpha)
    gpu.matrix.pop()
    for x, y, text, h, color in texts:
        hud.draw_text(board, x, y - h / 2, text, h, (*color[:3], color[3] * alpha))
    gpu.state.blend_set('NONE')


def clear():
    _batches.clear()
    _parts.clear()
    reset_hints()


def register():
    pass


def unregister():
    restore(bpy.context)
    clear()
