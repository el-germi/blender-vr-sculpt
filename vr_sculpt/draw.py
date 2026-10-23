"""VR overlay: brush rings at the controller tips, tip status, hand panel, grid, measurements,
plus (headset only) the desktop window screen and camera previews."""

import math

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector

from . import (camera, controllers, desktop, drawing, grid, hardsurface, hud, interactions, measure, menus, onion,
               operators, paint, paths, presence, primitives, puppet, shading, snapping, strokes, symmetry, vr_ui)
from . import pose as pose_mode  # "pose" is a controller pose below

COLORS = hud.PYTHON_COLORS
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


def _draw(xr):
    context = bpy.context
    if not operators.session_running(context):
        return
    try:
        draw_overlay(context, context.window_manager.xr_session_state, xr)
    except Exception as ex:
        # A failing draw handler runs every frame; report once, not 90 times a second.
        if not _errors:
            import traceback
            traceback.print_exc()
        _errors.append(ex)


_errors = []


def draw_overlay(context, ss, xr=True):
    """Everything the add-on draws in VR. ss: the XR session state; xr: drawing the headset view
    (the desktop view skips the desktop screen and camera previews)."""
    settings = getattr(context.scene, "vr_sculpt", None)
    if settings is None or not settings.enabled:
        return
    scale = ss.viewer_scale
    poses = [operators.controller_pose(ss.controller_aim_location_get(context, i),
                                       ss.controller_aim_rotation_get(context, i)) for i in range(2)]
    tips = [operators.brush_tip(p, settings.tip_offset * scale) for p in poses]
    controllers.draw(context, ss, xr)
    if settings.grid_show:
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('NONE')
        grid.draw(settings, tips, scale)
        gpu.state.blend_set('NONE')
    measure.draw(context, ss, scale)
    onion.draw(context)
    if settings.show_paths and xr:  # The desktop view draws Blender's own paths.
        paths.draw(context, scale)
    if settings.mode == 'POSE' and settings.pose_show_bones:
        pose_mode.draw(context, tips, scale)
    puppet.draw(context, scale)
    if xr and settings.show_desktop:
        desktop.draw(context, ss, poses[1])
    if settings.show_cursor:
        _draw_cursors(context, ss, settings, xr)
    controllers.draw_hints(context, ss)


def _draw_cursors(context, ss, settings, xr):
    scale = ss.viewer_scale
    model = settings.mode == 'MODEL'
    sketch = settings.mode == 'DRAW'
    tools = settings.mode == 'TOOLS'
    posing = settings.mode == 'POSE'
    painting = settings.mode == 'PAINT'
    if painting:
        _draw_paint_cursors(context, ss, settings)
    if sketch:
        radius = {'EDIT': drawing.EDIT_RADIUS, 'ERASE': drawing.ERASE_MARGIN}.get(
            settings.draw_tool, settings.draw_width / 2) * scale
    else:
        marking = model and settings.model_mark != 'NONE'
        radius = (pose_mode.REACH if posing else shading.MARK_REACH if marking else
                  interactions.TOUCH_MARGIN if model or tools else
                  settings.paint_radius if painting else settings.radius) * scale
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
        if sketch:
            color = {'ERASE': ERASE_COLOR, 'EDIT': EDIT_COLOR}.get(
                settings.draw_tool, strokes.COLORS.get(settings.draw_color, (1.0, 1.0, 1.0)))
            if settings.draw_tool in {'EDIT', 'ERASE'}:
                reach = (drawing.EDIT_RADIUS if settings.draw_tool == 'EDIT' else drawing.ERASE_MARGIN) * scale
                near = strokes.nearest(context, tip, reach)
                if near is not None:
                    _draw_bounds(shader, near, (*color, 0.9))
        elif model and settings.model_mark != 'NONE':
            color = {'HARD': shading.HARD_COLOR, 'SOFT': shading.SOFT_COLOR}.get(settings.model_mark, (1.0, 1.0, 1.0))
            if idx == 0:
                color = (1.0, 1.0, 1.0)  # The left hand clears.
        elif model:
            color = hud.CUT_COLORS.get(settings.model_add_as, hud.MODEL_COLOR)                 if hardsurface.cut_target(context) is not None else hud.MODEL_COLOR
            touched = primitives.touching(context, tip, interactions.TOUCH_MARGIN * scale)
            if touched is not None:
                _draw_bounds(shader, touched, (1.0, 0.85, 0.3, 0.9))
        elif tools:
            color = hud.CAMERA_COLOR if settings.tool == 'CAMERA' else measure.COLOR
        elif posing:
            color = hud.POSE_COLORS[settings.pose_tool]
        elif painting:
            color = paint.get_color(context, secondary=idx == 0)
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
            target = hardsurface.cut_target(context)
            if target is not None and hardsurface.cutters_visible(context):
                # The cuts of the object being worked on (cutters are wireframes, easy to lose).
                for mod in hardsurface.booleans(target):
                    if mod.object is not None and mod.object.visible_get():
                        c = hud.CUT_COLORS.get(mod.operation, hud.MODEL_COLOR)
                        _draw_bounds(shader, mod.object, (*c, 0.9 if mod.object == active else 0.45))
            bits = symmetry.model_bits(active)
            mod = active.modifiers.get(symmetry.MODIFIER)
            if bits and mod is not None:
                center = mod.mirror_object.matrix_world.translation if mod.mirror_object else \
                    active.matrix_world.translation
                _draw_mirror_planes(shader, bits, center, scale)
        snapping.draw(shader, scale)
        if settings.model_page == 'FINISH' or settings.model_mark != 'NONE':
            tips = [operators.brush_tip(operators.controller_pose(ss.controller_aim_location_get(context, i),
                                                                  ss.controller_aim_rotation_get(context, i)), offset)
                    for i in range(2)]
            shading.draw(shader, context, tips, scale, settings.model_mark != 'NONE')
    if sketch:
        _draw_mirror_planes(shader, drawing.mirror_bits(settings), context.scene.cursor.location, scale)
    gpu.state.blend_set('NONE')

    viewer = Vector(ss.viewer_pose_location)
    camera_tool = tools and settings.tool == 'CAMERA'
    if settings.show_brush_card:
        # The tip status: a slim line above the right hand, facing the headset.
        anchor = Vector(ss.controller_grip_location_get(context, 1)) + Vector((0.0, 0.0, 0.05 * scale))
        content = hud.card_content(settings, sculpt_brush)
        hud.draw_status(anchor, viewer, scale, content, hud.status_size(context, settings))
    if xr and camera_tool and context.scene.camera is not None:
        # A viewfinder above the status: the controller is the camera.
        anchor = Vector(ss.controller_grip_location_get(context, 1)) + Vector((0.0, 0.0, 0.075 * scale))
        camera.draw_preview(hud.billboard(anchor, viewer, scale), context.scene)
    _draw_panel(context, ss, settings, xr)


def _draw_panel(context, ss, settings, xr):
    """The hand panel and timeline, faded in and out by presence.py; hidden, its ghost."""
    if not settings.show_panel:
        return
    right = operators.controller_pose(ss.controller_aim_location_get(context, 1),
                                      ss.controller_aim_rotation_get(context, 1))
    frames = interactions.panel_frames(context, ss)
    widgets, hit, hit_matrix, _h = interactions.panel_pointer(context, ss, right, frames)
    was_shown = presence.shown(context, ss)
    ghost = not was_shown and vr_ui.ghost_hit(frames[0][0], right.translation, interactions.aim_direction(right))
    alpha = presence.update(context, ss, using=(hit is not None and was_shown) or ghost or
                            interactions.pressed_id is not None)
    if alpha < 0.98:
        icon = menus.MODE_ICONS.get(settings.mode, "sculpt")
        vr_ui.draw_ghost(frames[0][0], icon, 1.0 - alpha, ghost)
    if alpha <= 0.01:
        if xr:
            interactions.note_hover(context, ss, None)
        return
    shown = presence.shown(context, ss)
    hover = vr_ui.hit_test(widgets, *hit) if hit is not None and shown else None
    if xr:
        interactions.note_hover(context, ss, hover.id if hover else None)
    for matrix, panel_widgets, height, width in frames:
        pointer = hit if hit is not None and shown and matrix is hit_matrix else None
        vr_ui.draw_panel(vr_ui.lift(matrix, alpha), panel_widgets, height, hover.id if hover else None,
                         interactions.pressed_id, width=width, alpha=alpha, pointer=pointer)
    if hit is not None and shown:
        vr_ui.draw_ray(right.translation, hit_matrix @ Vector((hit[0], hit[1], 0.0)), True)
    camera_tool = settings.mode == 'TOOLS' and settings.tool == 'CAMERA'
    if xr and settings.camera_preview and context.scene.camera is not None and alpha > 0.5 and not camera_tool:
        # Pinned to the left of the hand panel.
        width = camera.PREVIEW_WIDTH
        camera.draw_preview(frames[0][0], context.scene, width, x=-vr_ui.WIDTH / 2 - 0.006 - width)


def _draw_paint_cursors(context, ss, settings):
    """Where the paint lands: a ring on the surface (Contact: near the tip; Spray: along the ray)."""
    obj = paint.target(context)
    if obj is None or not paint.ready(obj):
        return
    scale = ss.viewer_scale
    radius = settings.paint_radius * scale
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    for idx in range(2):
        pose = operators.controller_pose(ss.controller_aim_location_get(context, idx),
                                         ss.controller_aim_rotation_get(context, idx))
        if settings.paint_reach == 'SPRAY':
            h = paint.surface_along(obj, pose.translation, interactions.aim_direction(pose))
        else:
            h = paint.surface_near(obj, operators.brush_tip(pose, settings.tip_offset * scale), radius)
        if h is None:
            continue
        point, normal, _face = h
        rot = normal.to_track_quat('Z', 'Y').to_matrix().to_4x4()
        m = Matrix.Translation(point + normal * 0.0005 * scale) @ rot @ Matrix.Scale(radius, 4)
        ring = [m @ Vector((math.cos(a), math.sin(a), 0.0)) for a in
                (2 * math.pi * i / _SEGMENTS for i in range(_SEGMENTS))]
        coords = [p for i in range(_SEGMENTS) for p in (ring[i], ring[(i + 1) % _SEGMENTS])]
        if settings.paint_reach == 'SPRAY':
            coords += [pose.translation, point]
        color = (1.0, 1.0, 1.0) if settings.paint_tool == 'PICK' else paint.get_color(context, secondary=idx == 0)
        batch = batch_for_shader(shader, 'LINES', {"pos": coords})
        shader.uniform_float("color", (*color, 0.95))
        batch.draw(shader)
    gpu.state.blend_set('NONE')


ERASE_COLOR = (1.0, 0.3, 0.3)
EDIT_COLOR = (1.0, 0.85, 0.3)
MIRROR_COLORS = ((1.0, 0.35, 0.35), (0.45, 0.9, 0.35), (0.35, 0.55, 1.0))  # X, Y, Z like Blender's axes


def _draw_mirror_planes(shader, bits, center, scale, half=0.3, lines=6):
    """A light grid on each mirror plane in bits (1 = X, 2 = Y, 4 = Z), through center."""
    for axis in range(3):
        if not bits & (1 << axis):
            continue
        u, v = [(a, b) for a, b in ((1, 2), (0, 2), (0, 1))][axis]
        h = half * scale
        coords = []
        for i in range(lines + 1):
            f = -h + 2 * h * i / lines
            for a, b in ((f, -h), (f, h)), ((-h, f), (h, f)):
                p1, p2 = Vector(center), Vector(center)
                p1[u] += a[0]
                p1[v] += a[1]
                p2[u] += b[0]
                p2[v] += b[1]
                coords += [p1, p2]
        batch = batch_for_shader(shader, 'LINES', {"pos": coords})
        shader.uniform_float("color", (*MIRROR_COLORS[axis], 0.35))
        batch.draw(shader)


_BOX_EDGES = ((0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7))


def _draw_bounds(shader, obj, color):
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    coords = [corners[i] for edge in _BOX_EDGES for i in edge]
    batch = batch_for_shader(shader, 'LINES', {"pos": coords})
    shader.uniform_float("color", color)
    batch.draw(shader)


def register():
    for region in ('XR', 'WINDOW'):
        _handles.append((region, bpy.types.SpaceView3D.draw_handler_add(_draw, (region == 'XR',), region,
                                                                        'POST_VIEW')))


def unregister():
    for region, h in _handles:
        bpy.types.SpaceView3D.draw_handler_remove(h, region)
    _handles.clear()
    hud.clear()
    controllers.clear()
