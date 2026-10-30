"""VR overlay: brush rings at the controller tips, tip status, hand panel, grid, measurements,
plus (headset only) the desktop window screen and camera previews."""

import math

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector

from . import (actions, camera, controllers, desktop, drawing, grid, hardsurface, hud, interactions, kit, measure,
               menus, onion, operators, paint, paths, physics, presence, primitives, puppet, refs, retopo, rig,
               shading, snapping, strokes, symmetry, uv, vr_ui)
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
    from . import lookdev
    lookdev.draw(context, ss, tips, scale)  # The Light tool's gizmos (the headset doesn't draw lights).
    refs.draw(context, ss, tips, scale, xr)  # Reference pictures (the headset doesn't draw empties).
    onion.draw(context)
    if settings.show_paths and xr:  # The desktop view draws Blender's own paths.
        paths.draw(context, scale)
    if settings.mode == 'POSE' and settings.pose_show_bones:
        pose_mode.draw(context, tips, scale)
    if settings.mode == 'RIG':
        rig.draw(context, actions.by_role(tips, context), scale)
    if settings.mode == 'RETOPO':
        retopo.draw(context, actions.by_role(tips, context), actions.by_role(poses, context), scale)
    if settings.mode == 'PHYSICS':
        physics.draw(context, tips, scale)
    puppet.draw(context, scale)
    if xr and settings.show_desktop:
        desktop.draw(context, ss, poses[1])
    if settings.show_cursor:
        _draw_cursors(context, ss, settings, xr)
    controllers.draw_hints(context, ss, panel_alpha=presence.alpha())


def _draw_cursors(context, ss, settings, xr):
    scale = ss.viewer_scale
    model = settings.mode == 'MODEL'
    sketch = settings.mode == 'DRAW'
    tools = settings.mode == 'TOOLS'
    posing = settings.mode == 'POSE'
    painting = settings.mode == 'PAINT'
    rigging = settings.mode == 'RIG'
    weights = rigging and settings.rig_tool == 'WEIGHTS'
    simulating = settings.mode == 'PHYSICS'
    if painting:
        _draw_paint_cursors(context, ss, settings)
    if sketch:
        radius = {'EDIT': drawing.EDIT_RADIUS, 'ERASE': drawing.ERASE_MARGIN}.get(
            settings.draw_tool, settings.draw_width / 2) * scale
    else:
        marking = model and (settings.model_mark != 'NONE' or settings.uv_tool == 'SEAM')
        fixing = model and settings.uv_tool == 'FIX'
        # Stamping, the ring is half the spacing: stamps go where neighboring rings would touch.
        stamping = model and settings.kit_stamp and kit.is_key(settings.primitive)
        radius = (settings.weight_radius if weights else pose_mode.REACH if posing else rig.REACH if rigging else
                  physics.HAND_RADIUS if simulating and settings.physics_hands else
                  interactions.TOUCH_MARGIN if simulating else settings.uv_fix_radius if fixing else
                  shading.MARK_REACH if marking else settings.kit_spacing / 2 if stamping else
                  interactions.TOUCH_MARGIN if model or tools else
                  settings.paint_radius if painting else settings.radius) * scale
    if settings.mode == 'RETOPO':
        radius = retopo.ring_radius(settings) * scale
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
    off = actions.off_index(context)
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
        elif model and settings.uv_tool != 'NONE':
            color = uv.SEAM_COLOR if settings.uv_tool == 'SEAM' else uv.FIX_COLOR
            if idx == off and settings.uv_tool == 'SEAM':
                color = (1.0, 1.0, 1.0)  # The off hand clears.
        elif model and settings.model_mark != 'NONE':
            color = {'HARD': shading.HARD_COLOR, 'SOFT': shading.SOFT_COLOR}.get(settings.model_mark, (1.0, 1.0, 1.0))
            if idx == off:
                color = (1.0, 1.0, 1.0)  # The off hand clears.
        elif model and idx == off:
            # The off-hand trigger selects: the ring is the selection's color, and what it would select is boxed.
            color = SELECT_COLOR
            touched = primitives.touching(context, tip, interactions.TOUCH_MARGIN * scale)
            if touched is not None:
                _draw_bounds(shader, touched, (1.0, 0.85, 0.3, 0.9))
        elif model:
            color = (hud.CUT_COLORS.get(settings.model_add_as, hud.MODEL_COLOR)
                     if hardsurface.cut_target(context) is not None else hud.MODEL_COLOR)
        elif tools:
            color = hud.tool_color(settings, idx == off)
        elif posing:
            color = hud.POSE_COLORS[settings.pose_tool]
        elif weights:
            brush = rig.flip(settings.weight_brush) if idx == off else settings.weight_brush
            color = rig.BRUSH_COLORS[brush]
        elif rigging:
            color = rig.DELETE_COLOR if idx == off else rig.BONES_COLOR
        elif simulating:
            color = hud.physics_color(settings, idx != off)
            if not settings.physics_hands or settings.physics_tool == 'GRAB':
                touched = primitives.touching(context, tip, interactions.TOUCH_MARGIN * scale)
                if touched is not None:
                    _draw_bounds(shader, touched, (1.0, 0.85, 0.3, 0.9))
        elif settings.mode == 'RETOPO':
            color = retopo.ring_color(settings, idx == off)
        elif painting:
            color = paint.get_color(context, secondary=idx == off)
        elif idx == off and settings.left_inverts and (native or settings.brush in {'DRAW', 'INFLATE'}):
            color = INVERT_COLOR
        alpha = 1.0 if idx in operators.active_hands else 0.6

        m = Matrix.Translation(tip) @ pose.to_3x3().to_4x4() @ Matrix.Scale(radius, 4)
        coords = [m @ Vector(p) for p in _RINGS]
        # Stick from the controller to the tip.
        coords += [pose.translation, tip]
        batch = batch_for_shader(shader, 'LINES', {"pos": coords})
        shader.uniform_float("color", (*color, alpha))
        batch.draw(shader)
    selected = None
    if simulating:
        active = physics.selected(context)
        if active is not None and active.visible_get():
            selected = active
            _draw_selected(shader, active)
    if model:
        active = context.view_layer.objects.active
        if active is not None and active.select_get() and active.visible_get():
            selected = active
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
            _draw_selected(shader, active)  # Last, over the cut boxes.
        snapping.draw(shader, scale)
        if settings.model_page == 'FINISH' or settings.model_mark != 'NONE':
            tips = [operators.brush_tip(operators.controller_pose(ss.controller_aim_location_get(context, i),
                                                                  ss.controller_aim_rotation_get(context, i)), offset)
                    for i in range(2)]
            shading.draw(shader, context, tips, scale, settings.model_mark != 'NONE')
        if uv.page_shown(context):
            tips = [operators.brush_tip(operators.controller_pose(ss.controller_aim_location_get(context, i),
                                                                  ss.controller_aim_rotation_get(context, i)), offset)
                    for i in range(2)]
            uv.draw(shader, context, tips, scale)
    if sketch:
        _draw_mirror_planes(shader, drawing.mirror_bits(settings), context.scene.cursor.location, scale)
    if rigging and settings.rig_tool == 'BONES' and settings.rig_mirror:
        arm = rig.target(context)
        if arm is not None:
            _draw_mirror_planes(shader, 1, arm.matrix_world.translation, scale)
    gpu.state.blend_set('NONE')

    viewer = Vector(ss.viewer_pose_location)
    if selected is not None:
        # Its name above it, so you know what the off-hand trigger picked.
        top = max(_box(selected), key=lambda c: c.z)
        center = sum(_box(selected), Vector()) / 8
        hud.draw_tag(Vector((center.x, center.y, top.z)) + Vector((0.0, 0.0, 0.012 * scale)), viewer, scale,
                     selected.name, SELECT_COLOR)
    camera_tool = tools and settings.tool == 'CAMERA'
    if settings.show_brush_card:
        # The tip status: a slim line above the main hand, facing the headset.
        anchor = Vector(ss.controller_grip_location_get(context, actions.main_index(context))) + Vector((0.0, 0.0, 0.05 * scale))
        content = hud.card_content(settings, sculpt_brush)
        hud.draw_status(anchor, viewer, scale, content, hud.status_size(context, settings))
    if xr and camera_tool and context.scene.camera is not None:
        # A viewfinder above the status: the controller is the camera.
        anchor = Vector(ss.controller_grip_location_get(context, actions.main_index(context))) + \
            Vector((0.0, 0.0, 0.075 * scale))
        camera.draw_preview(hud.billboard(anchor, viewer, scale), context.scene)
    _draw_panel(context, ss, settings, xr)


def _draw_panel(context, ss, settings, xr):
    """The hand panel and timeline, faded in and out by presence.py; hidden, its ghost."""
    if not settings.show_panel:
        return
    main = actions.main_index(context)
    aim = operators.controller_pose(ss.controller_aim_location_get(context, main),
                                    ss.controller_aim_rotation_get(context, main))
    frames = interactions.panel_frames(context, ss)
    widgets, hit, hit_matrix, _h = interactions.panel_pointer(context, ss, aim, frames)
    was_shown = presence.shown(context, ss)
    ghost = not was_shown and vr_ui.ghost_hit(frames[0][0], aim.translation, interactions.aim_direction(aim))
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
    faded = alpha * presence.dim()  # see-through while the off hand holds something
    for matrix, panel_widgets, height, width in frames:
        pointer = hit if hit is not None and shown and matrix is hit_matrix else None
        vr_ui.draw_panel(vr_ui.lift(matrix, alpha), panel_widgets, height, hover.id if hover else None,
                         interactions.pressed_id, width=width, alpha=faded, pointer=pointer)
    if hit is not None and shown:
        vr_ui.draw_ray(aim.translation, hit_matrix @ Vector((hit[0], hit[1], 0.0)), True)
    camera_tool = settings.mode == 'TOOLS' and settings.tool == 'CAMERA'
    if xr and settings.camera_preview and context.scene.camera is not None and faded > 0.5 and not camera_tool:
        # Pinned to the left of the hand panel.
        width = camera.PREVIEW_WIDTH
        camera.draw_preview(frames[0][0], context.scene, width, x=-vr_ui.WIDTH / 2 - 0.006 - width)
    if xr and settings.ref_pin and faded > 0.5:
        refs.draw_pin(context, frames[0][0])  # Right of the hand panel.


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
        secondary = idx == actions.off_index(context)
        color = (1.0, 1.0, 1.0) if settings.paint_tool == 'PICK' else paint.get_color(context, secondary=secondary)
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


SELECT_COLOR = (0.45, 0.72, 1.0)  # the selection's box, name tag and the left (selecting) ring
_BOX_EDGES = ((0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7))


def _box(obj):
    """obj's bounds as 8 world corners, in bound_box order."""
    lo, hi = primitives.local_bounds(obj)
    box = [(lo.x, lo.y, lo.z), (lo.x, lo.y, hi.z), (lo.x, hi.y, hi.z), (lo.x, hi.y, lo.z),
           (hi.x, lo.y, lo.z), (hi.x, lo.y, hi.z), (hi.x, hi.y, hi.z), (hi.x, hi.y, lo.z)]
    return [obj.matrix_world @ Vector(c) for c in box]


def _draw_selected(shader, obj):
    """The selected object: a faint box with bright corner brackets, easy to tell from the touch box."""
    corners = _box(obj)
    coords = [corners[i] for edge in _BOX_EDGES for i in edge]
    shader.uniform_float("color", (*SELECT_COLOR, 0.3))
    batch_for_shader(shader, 'LINES', {"pos": coords}).draw(shader)
    brackets = []
    for a, b in _BOX_EDGES:
        p, q = corners[a], corners[b]
        k = min(0.25, 0.03 / max((q - p).length, 1e-9))  # a quarter of the edge, at most 3 cm
        brackets += [p, p.lerp(q, k), q, q.lerp(p, k)]
    shader.uniform_float("color", (*SELECT_COLOR, 1.0))
    batch_for_shader(shader, 'LINES', {"pos": brackets}).draw(shader)


def _draw_bounds(shader, obj, color):
    corners = _box(obj)
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
