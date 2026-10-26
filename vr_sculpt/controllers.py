"""The real controllers drawn at the hands, with what each button does right now.

Blender draws the runtime's controller model when it gets one (XR_MSFT_controller_model); SteamVR
gives none, so it falls back to gray spheres. With Controller Models on, Blender's are hidden while
the session runs and Meta's Quest Pro controllers are drawn instead (assets/controllers, made by
tools/convert_controllers.py): the buttons, trigger, grip and stick move as yours do, the way Meta's
own press animation moves them, and pressed parts light up in the accent color (the trigger by how
far it's pulled).

Beside each controller, a short legend says what the buttons do now (hints()): it fades in while
you look at that hand or hold it still, and out while you work, like the hand panel's reveal.
"""

import math
import os
import time
from collections import namedtuple

import bpy
import gpu
import numpy as np
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
                          "primitive rig_bound kit_part playing",
                  defaults=(False, False, False, False, False, True, "", "Cube", True, False, False))

NATIVE_LABELS = {"Draw": "Draw", "Clay Strips": "Clay", "Smooth": "Smooth", "Inflate/Deflate": "Inflate",
                 "Crease Sharp": "Crease", "Flatten/Contrast": "Flatten"}
NEXT_MODE = {'SCULPT': "Model", 'MODEL': "Draw", 'DRAW': "Paint", 'PAINT': "Rig", 'RIG': "Pose", 'POSE': "Physics",
             'PHYSICS': "Tools", 'TOOLS': "Sculpt"}
WEIGHT_LABELS = {'ADD': "Add weight", 'SUBTRACT': "Subtract", 'SMOOTH': "Smooth"}


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
    elif mode in {'DRAW', 'PAINT'} or (mode == 'RIG' and s.rig_tool == 'WEIGHTS'):
        right['top'] = ("undo", "Undo stroke")
    elif mode == 'RIG':
        right['top'] = ("check", "End chain")
    elif mode == 'POSE':
        right['top'] = ("swap", "IK" if s.pose_tool == 'FK' else "FK")
    elif mode == 'PHYSICS':
        right['top'] = ("stop", "Stop") if info.playing else ("play", "Simulate")
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
    elif mode == 'RIG' and s.rig_tool == 'WEIGHTS':
        right['stick_click'] = ("plus", "Brush size")
    elif mode == 'POSE':
        right['stick_click'] = ("plus", "IK chain")
    elif mode == 'PHYSICS':
        right['stick_click'] = ("hands", "Hands off" if s.physics_hands else "Hands on")

    # Grips: Blender's own navigation grab.
    left['grip'] = right['grip'] = ("here", "Move world")
    if getattr(s, "left_handed", False):
        # The roles above are the right-handed ones; the buttons and sticks swap with the hands.
        left, right = right, left
    return {'left': left, 'right': right}


def _trigger(s, info):
    mode = s.mode
    if mode == 'SCULPT':
        if s.engine == 'NATIVE':
            return ("sculpt", NATIVE_LABELS.get(info.native_brush, info.native_brush or "Sculpt"))
        return ("sculpt", s.brush.title())
    if mode == 'MODEL':
        if s.uv_tool == 'SEAM':
            return ("seam", "Mark seams")
        if s.uv_tool == 'FIX':
            return ("fix", "Fix UVs")
        if s.model_mark != 'NONE':
            return ("edit", {'HARD': "Mark hard", 'SOFT': "Mark soft"}.get(s.model_mark, "Clear marks"))
        if s.model_add_as != 'OBJECT' and info.cut_target:
            op = s.model_add_as
            return ({'DIFFERENCE': "cut", 'UNION': "union"}.get(op, "intersect"),
                    {'DIFFERENCE': "Cut", 'UNION': "Union"}.get(op, "Intersect"))
        if s.kit_stamp and info.kit_part:
            return ("stamp", "Stamp " + info.primitive)
        return ("kit" if info.kit_part else "model", "Add " + info.primitive)
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
    if mode == 'RIG':
        if s.rig_tool == 'BONES':
            return ("bones", "Place joint")
        return ("weights", WEIGHT_LABELS[s.weight_brush]) if info.rig_bound else None
    if mode == 'PHYSICS':
        if not s.physics_hands:
            return ("object", "Select, move")
        return ("force", "Pull") if s.physics_tool == 'FORCE' else ("throw", "Grab, throw")
    if mode == 'TOOLS':
        return ("camera", "Aim camera") if s.tool == 'CAMERA' else ("ruler", "Measure")
    return None


def _left_trigger(s, info, right):
    mode = s.mode
    if mode == 'SCULPT':
        inverts = s.left_inverts and (s.engine == 'NATIVE' or s.brush in {'DRAW', 'INFLATE'})
        if inverts and right is not None:
            return ("sculpt", right[1] + ", inverted")
    if mode == 'MODEL' and s.uv_tool == 'SEAM':
        return ("seam", "Clear seams")
    if mode == 'MODEL' and s.uv_tool == 'FIX':
        return right
    if mode == 'MODEL' and s.model_mark != 'NONE':
        return ("edit", "Clear marks")
    if mode == 'MODEL':
        return ("object", "Select, move")
    if mode == 'PAINT' and right is not None and s.paint_tool != 'PICK':
        return (right[0], "2nd color")
    if mode == 'PAINT' and right is not None:
        return ("eyedropper", "Pick 2nd color")
    if mode == 'RIG' and s.rig_tool == 'BONES':
        return ("trash", "Delete bone")
    if mode == 'PHYSICS' and s.physics_hands and s.physics_tool == 'FORCE':
        return ("force", "Push")
    if mode == 'RIG' and right is not None:
        from .rig import flip
        return ("weights", WEIGHT_LABELS[flip(s.weight_brush)])
    return right


def context_info(context, ss):
    """Info for hints() from the scene and the session."""
    from . import (anim, desktop, hardsurface, interactions, kit, menus, native, operators, paint, pose, presence,
                   primitives, rig)
    settings = context.scene.vr_sculpt
    main = actions.main_index(context)
    pointer = operators.controller_pose(ss.controller_aim_location_get(context, main),
                                        ss.controller_aim_rotation_get(context, main))
    on_panel = False
    if settings.show_panel and presence.alpha() > 0.5:
        on_panel = interactions.panel_pointer(context, ss, pointer)[1] is not None
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
        rig_bound=settings.mode == 'RIG' and rig.bound(rig.mesh(context), rig.target(context)),
        kit_part=kit.is_key(settings.primitive),
        playing=settings.mode == 'PHYSICS' and anim.is_playing(context),
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
    _logged["poses"] = False
    if not context.scene.vr_sculpt.own_controllers or not available():
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
    """Controller Models switched in the middle of a session."""
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


# --- the model -----------------------------------------------------------------------------

# Meta's Quest Pro controller models, converted by tools/convert_controllers.py (see NOTICE.md beside them).
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "controllers")

# The model's origin from the aim pose, in the aim frame (hand-space meters). Meta's models line up with
# the aim pose's orientation, about 3 cm from it along Z.
MODEL_OFFSET = Vector((0.0, 0.0, 0.03))

LIGHT_UP = 0.8  # the key light: from above, and from the head by this much
LIGHT_HEAD = 0.6
STICK_SINK = 0.0012  # the stick cap sinks this much when clicked (the file has no click animation)

_SHADER_SOURCES = (
    "void main() { v_nor = nor; v_uv = uv; gl_Position = mvp * vec4(pos, 1.0); }",
    """
void main() {
    vec3 base = texture(image, v_uv).rgb;
    vec3 n = normalize(v_nor);
    float key = max(dot(n, light), 0.0);
    float rim = pow(1.0 - abs(dot(n, eye)), 3.0);
    vec3 col = base * (0.7 + 1.5 * key) + vec3(0.025) + vec3(0.09) * rim;
    col = mix(col, glow.rgb, glow.a * 0.7) + glow.rgb * glow.a * 0.25;
    // As Blender's own shaders do: colors are sRGB; an sRGB target wants them linear.
    if (srgbTarget) {
        col = mix(col / 12.92, pow((col + 0.055) / 1.055, vec3(2.4)), step(0.04045, col));
    }
    frag = vec4(col, alpha);
}
""")


class Model:
    """One hand's controller: a batch per part, how the moving parts move, and the texture (made on
    first draw: it needs the GPU)."""

    def __init__(self, hand):
        data = np.load(os.path.join(ASSETS, hand + ".npz"))
        pos, part, tris = data["pos"], data["part"], data["tris"]
        self.names = [str(n) for n in data["parts"]]
        self.arrays = {"pos": pos, "nor": data["nor"], "uv": data["uv"]}
        tri_part = part[tris[:, 0]]
        self.tris = {name: tris[tri_part == i] for i, name in enumerate(self.names)}
        self.centers, self.tops = {}, {}
        for i, name in enumerate(self.names):
            sel = pos[part == i]
            if len(sel):
                c = sel.mean(axis=0)
                self.centers[name] = Vector(c)
                self.tops[name] = Vector((c[0], sel[:, 1].max(), c[2]))
        self.moves = {str(n): (Vector(p), Vector(a), float(ang), Vector(mv))
                      for n, p, a, ang, mv in zip(data["moves"], data["pivot"], data["axis"], data["angle"],
                                                  data["move"])}
        self.tex_data = data["tex"]
        self.batches = None
        self.texture = None

    def gpu_ready(self, shader):
        if self.batches is None:
            self.batches = {name: batch_for_shader(shader, 'TRIS', self.arrays, indices=t)
                            for name, t in self.tris.items() if len(t)}
        if self.texture is None:
            h, w = self.tex_data.shape[:2]
            rgba = np.ones((h, w, 4), np.float32)
            rgba[..., :3] = self.tex_data / 255.0
            self.texture = gpu.types.GPUTexture((w, h), format='RGBA8',
                                                data=gpu.types.Buffer('FLOAT', rgba.size, rgba.ravel()))

    def part_matrix(self, name, inputs):
        """Where the buttons move a part to, in the controller frame."""
        def turn(key, amount):
            pivot, axis, angle, _mv = self.moves[key]
            return Matrix.Translation(pivot) @ Matrix.Rotation(angle * amount, 4, axis) @ Matrix.Translation(-pivot)
        if name == 'stick' and 'stick_forward' in self.moves:
            sink = Matrix.Translation((0.0, -STICK_SINK if inputs.stick_click else 0.0, 0.0))
            return sink @ turn('stick_forward', inputs.stick_y) @ turn('stick_right', inputs.stick_x)
        if name in {'trigger', 'grip'} and name in self.moves:
            return turn(name, min(max(getattr(inputs, name), 0.0), 1.0))
        if name in {'top', 'bottom'} and name in self.moves and getattr(inputs, name):
            return Matrix.Translation(self.moves[name][3])
        return Matrix.Identity(4)


_models = {}
_gpu = {"shader": None}


def available():
    return all(os.path.exists(os.path.join(ASSETS, h + ".npz")) for h in ('left', 'right'))


def model(hand):
    if hand not in _models:
        _models[hand] = Model(hand)
    return _models[hand]


def _shader():
    if _gpu["shader"] is None:
        info = gpu.types.GPUShaderCreateInfo()
        info.push_constant('MAT4', "mvp")
        info.push_constant('VEC3', "light")
        info.push_constant('VEC3', "eye")
        info.push_constant('VEC4', "glow")
        info.push_constant('FLOAT', "alpha")
        info.push_constant('BOOL', "srgbTarget")  # Set by Blender when the shader is bound.
        info.sampler(0, 'FLOAT_2D', "image")
        info.vertex_in(0, 'VEC3', "pos")
        info.vertex_in(1, 'VEC3', "nor")
        info.vertex_in(2, 'VEC2', "uv")
        iface = gpu.types.GPUStageInterfaceInfo("vrs_controller_iface")
        iface.smooth('VEC3', "v_nor")
        iface.smooth('VEC2', "v_uv")
        info.vertex_out(iface)
        info.fragment_out(0, 'VEC4', "frag")
        info.vertex_source(_SHADER_SOURCES[0])
        info.fragment_source(_SHADER_SOURCES[1])
        _gpu["shader"] = gpu.shader.create_from_info(info)
    return _gpu["shader"]


def layout(hand):
    """Where the parts are, in the controller frame: the legend's leaders point here."""
    m = model(hand)
    return {'top': m.tops['top'], 'bottom': m.tops['bottom'], 'stick': m.tops['stick'],
            'stick_click': m.tops['stick'], 'trigger': m.centers['trigger'], 'grip': m.centers['grip']}


def controller_matrix(ss, context, hand_idx):
    """World matrix of a controller's model (scaled by the viewer scale): on the aim pose."""
    rot = Quaternion(ss.controller_aim_rotation_get(context, hand_idx)).to_matrix().to_4x4()
    scale = ss.viewer_scale
    m = rot @ Matrix.Scale(scale, 4)
    m.translation = Vector(ss.controller_aim_location_get(context, hand_idx)) + rot.to_3x3() @ (MODEL_OFFSET * scale)
    return m


_logged = {"poses": False}


def _log_poses(context, ss):
    """Once per session, print where each grip pose sits in its aim pose's frame: what MODEL_OFFSET is
    tuned against when a runtime places the model differently."""
    if _logged["poses"]:
        return
    out = []
    for idx, hand in enumerate(('left', 'right')):
        aim_loc = Vector(ss.controller_aim_location_get(context, idx))
        grip_loc = Vector(ss.controller_grip_location_get(context, idx))
        if aim_loc.length < 1e-6 and grip_loc.length < 1e-6:
            return  # No pose yet.
        aim = Quaternion(ss.controller_aim_rotation_get(context, idx))
        get_grip = getattr(ss, "controller_grip_rotation_get", None)
        grip = Quaternion(get_grip(context, idx)) if get_grip else aim
        scale = max(ss.viewer_scale, 1e-6)
        offset = aim.inverted() @ (grip_loc - aim_loc) / scale
        rel = (aim.inverted() @ grip).to_euler()
        out.append(f"{hand} grip in aim: offset {tuple(round(v * 1000, 1) for v in offset)} mm, rotation "
                   f"{tuple(round(math.degrees(v), 1) for v in rel)} deg")
    _logged["poses"] = True
    print("vr_sculpt controllers: " + "; ".join(out))


# --- drawing ----------------------------------------------------------------------------

def draw(context, ss, xr=True):
    """Both controllers, with their pressed parts lit."""
    settings = context.scene.vr_sculpt
    if not settings.own_controllers or not available():
        return
    if xr:
        _log_poses(context, ss)
    shader = _shader()
    head = Vector(ss.viewer_pose_location)
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('LESS_EQUAL')
    gpu.state.depth_mask_set(True)
    view_proj = gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix()
    shader.bind()
    for idx, hand in enumerate(('left', 'right')):
        mdl = model(hand)
        mdl.gpu_ready(shader)
        shader.uniform_sampler("image", mdl.texture)
        inputs = read_inputs(context, ss, idx)
        lit = {'trigger': inputs.trigger, 'grip': min(inputs.grip, 1.0), 'top': 1.0 if inputs.top else 0.0,
               'bottom': 1.0 if inputs.bottom else 0.0, 'stick': 1.0 if inputs.stick_click else 0.0}
        tilt = math.hypot(inputs.stick_x, inputs.stick_y)
        if tilt > 0.3:
            lit['stick'] = max(lit['stick'], 0.35 * min(tilt, 1.0))
        m = controller_matrix(ss, context, idx)
        to_local = m.to_3x3().inverted_safe()
        toward_head = (head - m.translation).normalized()
        shader.uniform_float("light", (to_local @ (Vector((0.0, 0.0, LIGHT_UP)) + toward_head * LIGHT_HEAD)).normalized())
        shader.uniform_float("eye", (to_local @ toward_head).normalized())
        shader.uniform_float("alpha", 1.0)
        for name, batch in mdl.batches.items():
            level = lit.get(name, 0.0)
            shader.uniform_float("mvp", view_proj @ m @ mdl.part_matrix(name, inputs))
            shader.uniform_float("glow", (*vr_ui.COL_ACCENT[:3], level if level > 0.01 else 0.0))
            batch.draw(shader)
    gpu.state.depth_mask_set(False)
    gpu.state.depth_test_set('NONE')
    gpu.state.blend_set('NONE')


LEGEND_TEXT = 0.0052
LEGEND_ROW = 0.0105
LEGEND_SIDE = 0.034  # hand-space meters from the controller to the legend


def legend_alpha(hand_idx, alpha, panel_alpha, off_idx=0):
    """A hand's legend alpha with the hand panel showing at panel_alpha: the off hand's (off_idx) gives
    way to it (the panel sits right over that hand, where its legend is)."""
    return alpha * (1.0 - panel_alpha) if hand_idx == off_idx else alpha


def draw_hints(context, ss, now=None, info=None, panel_alpha=0.0):
    """The legends beside both controllers, faded per hand."""
    settings = context.scene.vr_sculpt
    if not (settings.own_controllers and settings.controller_hints and available()):
        return
    now = time.monotonic() if now is None else now
    head = Vector(ss.viewer_pose_location)
    head_rot = getattr(ss, "viewer_pose_rotation", None)
    forward = Quaternion(head_rot).to_matrix() @ Vector((0, 0, -1)) if head_rot is not None else None
    alphas = []
    for idx in range(2):
        grip = Vector(ss.controller_grip_location_get(context, idx))
        looking = forward is None or (grip - head).normalized().dot(forward) > LOOK
        alpha = update_hints(idx, grip, ss.viewer_scale, looking, working(read_inputs(context, ss, idx)), now)
        alphas.append(legend_alpha(idx, alpha, panel_alpha, actions.off_index(context)))
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
    part_point = layout(hand)
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
    _models.clear()
    _gpu["shader"] = None
    _logged["poses"] = False
    reset_hints()


def register():
    pass


def unregister():
    restore(bpy.context)
    clear()
