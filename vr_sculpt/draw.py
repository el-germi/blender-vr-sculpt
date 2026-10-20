"""Brush cursor: three rings at each controller tip, in the headset and the desktop view."""

import math

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector

from . import hud, interactions, operators, primitives, vr_ui

COLORS = {
    'DRAW': (1.0, 0.55, 0.2),
    'SMOOTH': (0.3, 0.8, 1.0),
    'GRAB': (0.95, 0.9, 0.3),
    'INFLATE': (0.7, 0.45, 1.0),
}
INVERT_COLOR = (0.4, 0.6, 1.0)

_SEGMENTS = 32
_handles = []


def _unit_rings():
    coords = []
    for axis in range(3):
        for i in range(_SEGMENTS):
            for j in (i, i + 1):
                a = 2.0 * math.pi * j / _SEGMENTS
                p = [0.0, 0.0, 0.0]
                p[(axis + 1) % 3] = math.cos(a)
                p[(axis + 2) % 3] = math.sin(a)
                coords.append(p)
    return coords


_RINGS = _unit_rings()


def _draw():
    context = bpy.context
    if not operators.session_running(context):
        return
    try:
        draw_overlay(context, context.window_manager.xr_session_state)
    except Exception as ex:
        # A failing draw handler runs every frame; report once, not 90 times a second.
        if not _errors:
            import traceback
            traceback.print_exc()
        _errors.append(ex)


_errors = []


def draw_overlay(context, ss):
    """Brush rings at both controller tips, plus the brush card. ss: the XR session state."""
    settings = getattr(context.scene, "vr_sculpt", None)
    if settings is None or not settings.enabled or not settings.show_cursor:
        return
    scale = ss.viewer_scale
    model = settings.mode == 'MODEL'
    radius = (interactions.TOUCH_MARGIN if model else settings.radius) * scale
    offset = settings.tip_offset * scale

    native = settings.engine == 'NATIVE'
    sculpt_brush = context.tool_settings.sculpt.brush if native else None
    if native:
        brush_color = hud.NATIVE_COLORS.get(sculpt_brush.name if sculpt_brush else "", (0.9, 0.9, 0.9))
    else:
        brush_color = COLORS.get(settings.brush, (1.0, 1.0, 1.0))

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    for idx in range(2):
        pose = operators.controller_pose(ss.controller_aim_location_get(context, idx),
                                         ss.controller_aim_rotation_get(context, idx))
        tip = operators.brush_tip(pose, offset)
        color = brush_color
        if model:
            color = hud.MODEL_COLOR
            touched = primitives.touching(context, tip, interactions.TOUCH_MARGIN * scale)
            if touched is not None:
                _draw_bounds(shader, touched, (1.0, 0.85, 0.3, 0.9))
        elif idx == 0 and settings.left_inverts and (native or settings.brush in {'DRAW', 'INFLATE'}):
            color = INVERT_COLOR
        alpha = 1.0 if idx in operators.active_hands else 0.6

        m = Matrix.Translation(tip) @ pose.to_3x3().to_4x4() @ Matrix.Scale(radius, 4)
        coords = [m @ Vector(p) for p in _RINGS]
        # Stick from the controller to the tip.
        coords += [pose.translation, tip]
        batch = batch_for_shader(shader, 'LINES', {"pos": coords})
        shader.uniform_float("color", (*color, alpha))
        batch.draw(shader)
    if model:
        active = context.view_layer.objects.active
        if active is not None and active.select_get() and active.visible_get():
            _draw_bounds(shader, active, (1.0, 1.0, 1.0, 0.35))
    gpu.state.blend_set('NONE')

    if settings.show_brush_card:
        # Above the right hand, facing the headset.
        anchor = Vector(ss.controller_grip_location_get(context, 1)) + Vector((0.0, 0.0, 0.06 * scale))
        content = hud.card_content(settings, sculpt_brush, COLORS)
        hud.draw_card(anchor, Vector(ss.viewer_pose_location), scale, content)

    if settings.show_panel:
        right = operators.controller_pose(ss.controller_aim_location_get(context, 1),
                                          ss.controller_aim_rotation_get(context, 1))
        widgets, hit, matrix, height = interactions.panel_pointer(context, ss, right)
        hover = vr_ui.hit_test(widgets, *hit) if hit else None
        vr_ui.draw_panel(matrix, widgets, height, hover.id if hover else None, interactions.pressed_id)
        if hit is not None:
            vr_ui.draw_ray(right.translation, matrix @ Vector((hit[0], hit[1], 0.0)), True)


_BOX_EDGES = ((0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7))


def _draw_bounds(shader, obj, color):
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    coords = [corners[i] for edge in _BOX_EDGES for i in edge]
    batch = batch_for_shader(shader, 'LINES', {"pos": coords})
    shader.uniform_float("color", color)
    batch.draw(shader)


def register():
    for region in ('XR', 'WINDOW'):
        _handles.append((region, bpy.types.SpaceView3D.draw_handler_add(_draw, (), region, 'POST_VIEW')))


def unregister():
    for region, h in _handles:
        bpy.types.SpaceView3D.draw_handler_remove(h, region)
    _handles.clear()
    hud.clear()
