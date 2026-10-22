"""The dock: a belt of tool tokens around you at waist height.

Each token is a little 3D tool (a clay ball, a cube, a pen, a paint brush, a bone, a tape measure, a
camera) on a socket. Reach in with a controller tip and pull the trigger to take it: that's the mode
and tool. Its socket stays empty while you hold it, and the tool's name rides above your right hand.
Touch the empty socket to put it back (an empty hand grabs and moves objects). Above the token you
reach for, its variants fan out (brushes, shapes, stroke styles, ...); the last slot is the drawer.

The belt follows you lazily: it stays put while you reach into it and catches up (gliding) once you
walk or turn away, so it's always where your hands expect it.
"""

import math
import time

from mathutils import Matrix, Quaternion, Vector

from . import hud, shapes, strokes

DROP = 0.52  # hand-space meters below the headset
RADIUS = 0.4  # hand-space meters from your body axis to the tokens
ARC = math.radians(104.0)  # the belt's span in front of you
TOKEN_SIZE = 0.017  # hand-space meters, a token's half size
REACH = 0.034  # hand-space meters from a token's center that still take it
WAKE = 0.3  # hand-space meters: a tip this close wakes the dock (labels, trays, full color)
FOLLOW_DISTANCE = 0.4  # hand-space meters the headset may move before the belt catches up
FOLLOW_ANGLE = math.radians(55.0)  # how far you may turn before the belt turns with you
GLIDE = 5.0  # 1/seconds: how fast it catches up

SOCKET_COLOR = (0.75, 0.8, 0.9)
EMPTY_COLOR = (1.0, 0.85, 0.35)


class Token:
    def __init__(self, id, label, mode, tool=None, color=(0.8, 0.8, 0.8)):
        self.id, self.label, self.mode, self.tool, self.color = id, label, mode, tool, color

    def held(self, settings):
        if self.mode is None:
            return False
        return settings.mode == self.mode and (self.tool is None or settings.tool == self.tool)


TOKENS = (
    Token("sculpt", "Sculpt", 'SCULPT', color=(0.86, 0.56, 0.42)),
    Token("model", "Model", 'MODEL', color=hud.MODEL_COLOR),
    Token("draw", "Draw", 'DRAW', color=(0.2, 0.21, 0.24)),
    Token("paint", "Paint", 'PAINT', color=(0.62, 0.42, 0.25)),
    Token("pose", "Pose", 'POSE', color=hud.POSE_COLORS['FK']),
    Token("measure", "Measure", 'TOOLS', 'MEASURE', color=hud.MEASURE_COLOR),
    Token("camera", "Camera", 'TOOLS', 'CAMERA', color=(0.25, 0.26, 0.29)),
    Token("drawer", "More", None, color=(0.7, 0.72, 0.78)),
)


def find(id):
    return next((t for t in TOKENS if t.id == id), None)


def held_token(settings):
    return next((t for t in TOKENS if t.held(settings)), None)


# --- where the belt is -------------------------------------------------------------

_state = {"origin": None, "yaw": 0.0, "goal": None, "t": 0.0, "scale": None}


def reset():
    _state.update(origin=None, goal=None, scale=None)


def viewer_yaw(ss):
    """The headset's heading (radians about Z, 0 = looking along +Y)."""
    rot = getattr(ss, "viewer_pose_rotation", None)
    if rot is None:
        return 0.0
    f = Quaternion(rot) @ Vector((0.0, 0.0, -1.0))
    if math.hypot(f.x, f.y) < 1e-4:  # Looking straight down or up: use the head's up vector.
        f = Quaternion(rot) @ Vector((0.0, 1.0, 0.0))
    return math.atan2(-f.x, f.y)


def _angle_diff(a, b):
    return (a - b + math.pi) % (2 * math.pi) - math.pi


def frame(ss, tips=()):
    """(origin, yaw, scale) of the belt: origin on your body axis at waist height. It follows the
    headset lazily, and not at all while a tip is in it."""
    scale = ss.viewer_scale
    head = Vector(ss.viewer_pose_location)
    target = head - Vector((0.0, 0.0, DROP * scale))
    yaw = viewer_yaw(ss)
    now = time.monotonic()
    st = _state
    if st["origin"] is None or st["scale"] is None or abs(st["scale"] - scale) > 1e-6:
        st.update(origin=target, yaw=yaw, goal=None, t=now, scale=scale)
        return st["origin"].copy(), st["yaw"], scale
    reaching = any(_near_slots(st["origin"], st["yaw"], scale, tip) for tip in tips)
    flat = Vector((target.x - st["origin"].x, target.y - st["origin"].y))
    if not reaching and (flat.length > FOLLOW_DISTANCE * scale or abs(target.z - st["origin"].z) > 0.25 * scale
                         or abs(_angle_diff(yaw, st["yaw"])) > FOLLOW_ANGLE):
        st["goal"] = (target, yaw)
    dt = min(max(now - st["t"], 0.0), 0.1)
    st["t"] = now
    if st["goal"] is not None and not reaching:
        g_origin, g_yaw = st["goal"]
        k = 1.0 - math.exp(-GLIDE * dt)
        st["origin"] = st["origin"].lerp(g_origin, k)
        st["yaw"] = st["yaw"] + _angle_diff(g_yaw, st["yaw"]) * k
        if (st["origin"] - g_origin).length < 0.005 * scale and abs(_angle_diff(g_yaw, st["yaw"])) < 0.01:
            st["origin"], st["yaw"], st["goal"] = g_origin, g_yaw, None
    return st["origin"].copy(), st["yaw"], scale


def _slot_angles():
    n = len(TOKENS)
    return [ARC * (0.5 - i / (n - 1)) for i in range(n)]  # left to right as you face it


def _direction(yaw, angle):
    a = yaw + angle
    return Vector((-math.sin(a), math.cos(a), 0.0))


def slots_at(origin, yaw, scale):
    """[(token, world position)] for a belt frame."""
    return [(t, origin + _direction(yaw, a) * RADIUS * scale) for t, a in zip(TOKENS, _slot_angles())]


def slots(ss, tips=()):
    origin, yaw, scale = frame(ss, tips)
    return slots_at(origin, yaw, scale)


def _near_slots(origin, yaw, scale, tip):
    return any((Vector(tip) - p).length < WAKE * scale for _t, p in slots_at(origin, yaw, scale))


def awake(ss, tips):
    """How far the dock has woken up for these tips: 1 with a tip in it, fading out to 0 at WAKE."""
    scale = ss.viewer_scale
    best = None
    for _t, p in slots(ss, tips):
        for tip in tips:
            d = (Vector(tip) - p).length / scale
            best = d if best is None else min(best, d)
    if best is None:
        return 0.0
    return min(max((WAKE - best) / (WAKE * 0.5), 0.0), 1.0)


def token_at(ss, tip, tips=()):
    """(token, position) whose reach contains tip, nearest first, or None."""
    scale = ss.viewer_scale
    best = None
    for token, p in slots(ss, tips or (tip,)):
        d = (Vector(tip) - p).length
        if d <= REACH * scale and (best is None or d < best[0]):
            best = (d, token, p)
    return (best[1], best[2]) if best else None


def token_matrix(position, viewer, scale, spin=0.0, size=TOKEN_SIZE):
    """A token's local frame: Z up, -Y toward the viewer, spinning about Z by `spin`."""
    f = Vector(viewer) - Vector(position)
    f.z = 0.0
    if f.length < 1e-6:
        f = Vector((0.0, -1.0, 0.0))
    rot = f.normalized().to_track_quat('-Y', 'Z') @ Quaternion((0.0, 0.0, 1.0), spin)
    return shapes.matrix_at(position, rot, size * scale)


# --- token shapes ---------------------------------------------------------------------

def _tilted(mesh, angle=math.radians(-40.0), offset=(0.0, 0.0, 0.0)):
    return shapes.transformed(mesh, Matrix.Translation(offset) @ Matrix.Rotation(angle, 4, 'Y'))


def _parts(token_id):
    S = shapes
    if token_id == "sculpt":
        return [S.Part(S.transformed(S.sphere(10, 18), Matrix.Scale(0.85, 4)), None)]
    if token_id == "model":
        return [S.Part(S.transformed(S.box(0.62, 0.62, 0.62), Matrix.Rotation(math.radians(30), 4, 'Z')), None)]
    if token_id == "draw":
        return [
            S.Part(_tilted(S.cylinder(0.2, 1.3, 14)), (0.16, 0.17, 0.2)),
            S.Part(_tilted(S.cylinder(0.21, 0.18, 14), offset=(0.42, 0.0, 0.5)), (0.75, 0.77, 0.82)),
            S.Part(_tilted(S.cylinder(0.2, 0.42, 14, r_top=0.0), offset=(-0.54, 0.0, -0.65)), None),
        ]
    if token_id == "paint":
        return [
            S.Part(_tilted(S.cylinder(0.13, 1.1, 12), offset=(0.25, 0.0, 0.3)), (0.62, 0.42, 0.25)),
            S.Part(_tilted(S.cylinder(0.19, 0.3, 12), offset=(-0.17, 0.0, -0.2)), (0.78, 0.8, 0.84)),
            S.Part(_tilted(S.cylinder(0.22, 0.5, 12, r_top=0.04), offset=(-0.42, 0.0, -0.5)), None),
        ]
    if token_id == "pose":
        bone = S.transformed(S.octahedron(1.7, 0.34, 0.22), Matrix.Translation((0.0, 0.0, -0.85)))
        rot = Matrix.Rotation(math.radians(35), 4, 'Y')
        return [
            S.Part(S.transformed(bone, rot), None),
            S.Part(S.transformed(S.sphere(6, 10), rot @ Matrix.Translation((0, 0, -0.85)) @ Matrix.Scale(0.22, 4)),
                   (0.92, 0.94, 1.0)),
            S.Part(S.transformed(S.sphere(6, 10), rot @ Matrix.Translation((0, 0, 0.85)) @ Matrix.Scale(0.16, 4)),
                   (0.92, 0.94, 1.0)),
        ]
    if token_id == "measure":
        face = Matrix.Rotation(math.radians(90), 4, 'X')  # The puck faces the viewer.
        return [
            S.Part(S.transformed(S.cylinder(0.78, 0.42, 20), face), None),
            S.Part(S.transformed(S.cylinder(0.3, 0.46, 14), face), (0.18, 0.18, 0.2)),
            S.Part(S.transformed(S.box(0.38, 0.06, 0.12), Matrix.Translation((0.9, 0.0, -0.55))), (0.95, 0.95, 0.9)),
        ]
    if token_id == "camera":
        return [
            S.Part(S.box(0.75, 0.42, 0.5), None),
            S.Part(S.transformed(S.cylinder(0.3, 0.5, 16), Matrix.Translation((0.0, -0.62, 0.0)) @
                                 Matrix.Rotation(math.radians(90), 4, 'X')), (0.12, 0.12, 0.14)),
            S.Part(S.transformed(S.cylinder(0.2, 0.04, 16), Matrix.Translation((0.0, -0.88, 0.0)) @
                                 Matrix.Rotation(math.radians(90), 4, 'X')), (0.35, 0.6, 1.0)),
            S.Part(S.transformed(S.box(0.16, 0.12, 0.1), Matrix.Translation((0.45, 0, 0.6))),
                   (0.9, 0.3, 0.3)),
        ]
    # The drawer: three bars.
    return [S.Part(S.transformed(S.box(0.75, 0.22, 0.11), Matrix.Translation((0.0, 0.0, z))), None)
            for z in (-0.42, 0.0, 0.42)]


_parts_cache = {}


def parts(token_id):
    out = _parts_cache.get(token_id)
    if out is None:
        out = _parts_cache[token_id] = _parts(token_id)
    return out


def tint(context, token):
    """The token's main color: the current brush, stroke or paint color where it has one."""
    settings = context.scene.vr_sculpt
    if token.id == "draw":
        return strokes.COLORS.get(settings.draw_color, (1.0, 1.0, 1.0))
    if token.id == "paint":
        from . import paint
        try:
            return paint.get_color(context)
        except AttributeError:
            return (0.9, 0.3, 0.3)
    if token.id == "pose":
        return hud.POSE_COLORS[settings.pose_tool]
    if token.id == "sculpt" and settings.mode == 'SCULPT':
        from . import draw as vr_draw, native
        if settings.engine == 'NATIVE':
            b = native.sculpt_brush(context)
            return hud.NATIVE_COLORS.get(b.name if b else "", token.color)
        return vr_draw.COLORS.get(settings.brush, token.color)
    return token.color


_socket = {}


def _socket_parts():
    if "ring" not in _socket:
        _socket["ring"] = [shapes.Part(shapes.torus(1.0, 0.06, 28, 6), None)]
        _socket["disc"] = [shapes.Part(shapes.cylinder(1.0, 0.05, 28), None)]
    return _socket["ring"], _socket["disc"]


def draw(context, ss, tips, hovered=(), spin_t=None):
    """The tokens on their sockets. hovered: ids of tokens a tip is in (they glow and turn)."""
    settings = context.scene.vr_sculpt
    viewer = Vector(ss.viewer_pose_location)
    scale = ss.viewer_scale
    wake = awake(ss, tips)
    alpha = 0.5 + 0.5 * wake
    t = time.monotonic() if spin_t is None else spin_t
    ring, disc = _socket_parts()
    labels = []
    shapes.begin_solid()
    for token, p in slots(ss, tips):
        held = token.held(settings)
        light = shapes.light_from(viewer, p)
        socket_m = shapes.matrix_at(p - Vector((0.0, 0.0, TOKEN_SIZE * 1.25 * scale)), Quaternion(),
                                    TOKEN_SIZE * 1.2 * scale)
        if held:
            # Its socket is empty and lit: it's in your hand. Touch it to put the tool back.
            shapes.draw_shape("socket_ring", ring, socket_m, light, alpha, 0.35 if token.id in hovered else 0.1,
                              EMPTY_COLOR)
        else:
            shapes.draw_shape("socket_disc", disc, socket_m, light, alpha * 0.7, 0.0, (0.2, 0.21, 0.24))
            shapes.draw_shape("socket_ring", ring, socket_m, light, alpha * 0.8, 0.0, SOCKET_COLOR)
            hot = token.id in hovered
            spin = (t * 1.6) % (2 * math.pi) if hot else 0.0
            size = TOKEN_SIZE * (1.22 if hot else 1.0)
            m = token_matrix(p, viewer, scale, spin, size)
            shapes.draw_shape("token_" + token.id, parts(token.id), m, light, alpha, 0.25 if hot else 0.0,
                              tint(context, token))
        if wake > 0.05 or held:
            labels.append((token, p, held))
    shapes.end_solid()
    from . import chips
    for token, p, held in labels:
        m = hud.billboard(p + Vector((0.0, 0.0, -TOKEN_SIZE * 2.6 * scale)), viewer, scale)
        color = (*EMPTY_COLOR, max(wake, 0.6)) if held else (1.0, 1.0, 1.0, wake)
        chips.draw_label(m, 0.0, 0.0, token.label, 0.0075, color)


def clear():
    _parts_cache.clear()
    _socket.clear()
