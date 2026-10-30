"""Tip status: a slim line above the main controller with the mode, the tool and its size.

After the tool changes, a one-line description shows under it for a few seconds, then fades. Also
the text and billboard helpers every VR label uses.
"""

import math
import time

import blf
import gpu
from mathutils import Matrix, Vector

DESCRIPTION_SECONDS = 4.0
DESCRIPTION_FADE = 0.6  # the description's last seconds fade out
SIZE_FLASH = 0.7  # seconds the size shows in the accent after it changes (stick click)

PYTHON_COLORS = {
    'DRAW': (1.0, 0.55, 0.2),
    'SMOOTH': (0.3, 0.8, 1.0),
    'GRAB': (0.95, 0.9, 0.3),
    'INFLATE': (0.7, 0.45, 1.0),
}

NATIVE_INFO = {
    "Draw": "Pushes the surface out. Other hand: pushes in",
    "Clay Strips": "Builds up flat strips, like layering clay",
    "Smooth": "Relaxes bumps and evens out the surface",
    "Inflate/Deflate": "Swells the surface outward. Other hand: shrinks",
    "Crease Sharp": "Cuts a sharp groove. Other hand: pinches a ridge",
    "Flatten/Contrast": "Flattens toward an average plane. Other hand: adds contrast",
}

# Ring and card accent colors per native brush, so they're distinguishable at a glance.
NATIVE_COLORS = {
    "Draw": (1.0, 0.55, 0.2),
    "Clay Strips": (0.85, 0.5, 0.35),
    "Smooth": (0.3, 0.8, 1.0),
    "Inflate/Deflate": (0.7, 0.45, 1.0),
    "Crease Sharp": (1.0, 0.3, 0.45),
    "Flatten/Contrast": (0.45, 0.9, 0.45),
}

MODEL_COLOR = (0.55, 0.9, 0.85)
CUT_COLORS = {'DIFFERENCE': (1.0, 0.4, 0.35), 'UNION': (0.45, 0.9, 0.45), 'INTERSECT': (0.95, 0.75, 0.3)}
MARK_INFO = {
    'HARD': ("Mark Hard Edges", "Touch edges to make them sharp. Other hand clears"),
    'SOFT': ("Mark Soft Edges", "Touch edges to make them smooth. Other hand clears"),
    'CLEAR': ("Clear Edge Marks", "Touch edges to clear their hard or soft mark"),
}
UV_INFO = {
    'SEAM': ("Mark Seams", "Trace edges to cut the UV map along them. Other hand clears"),
    'FIX': ("Fix UVs", "Brush over stretched faces: they unwrap again, stitched to the rest"),
}
CUT_INFO = {
    'DIFFERENCE': ("Cut with", "Drag on the selected object to cut into it. Other hand selects a cutter"),
    'UNION': ("Union with", "Drag on the selected object to add to it. Other hand selects a cutter"),
    'INTERSECT': ("Intersect with", "Drag on the selected object: only the overlap stays"),
}
KIT_DROP_INFO = "Pull a part out and hold it to a surface: it sits on it"
KIT_STAMP_INFO = "Sweep across a surface: a part every Spacing"
CAMERA_COLOR = (0.6, 0.75, 1.0)
MEASURE_COLOR = (1.0, 0.82, 0.25)
POSE_COLORS = {'FK': (0.55, 0.85, 1.0), 'IK': (1.0, 0.6, 0.85)}
RECORD_COLOR = (1.0, 0.3, 0.3)
POSE_INFO = {
    'FK': ("FK Pose", "Grab a bone: it turns to follow, twist to roll. Free bones move from the head"),
    'IK': ("IK Pose", "Grab a bone: it follows your hand and its parent bones bend to reach"),
}

RIG_INFO = {
    'BONES': ("Bones", "Press to place joints. Press a joint to branch, drag it to move. B ends the chain"),
    'ADD': ("Add Weight", "Paint the bone's weights. Other hand subtracts. Touch a bone to pick it"),
    'SUBTRACT': ("Subtract Weight", "Take the bone's weights away. Other hand adds. Touch a bone to pick it"),
    'SMOOTH': ("Smooth Weights", "Even out the bone's weights. Touch a bone to pick it"),
}

PHYSICS_COLORS = {'SETUP': (0.45, 0.95, 0.75), 'GRAB': (1.0, 0.8, 0.35), 'PULL': (0.4, 0.75, 1.0),
                  'PUSH': (1.0, 0.55, 0.3)}
PHYSICS_INFO = {
    'SETUP': ("Physics", "Touch an object or a bone to set it up. B plays the simulation"),
    'GRAB': ("Grab & Throw", "Grab bodies and spring bones, let go to throw. Your hands push. B: play / stop"),
    'FORCE': ("Push & Pull", "Main trigger pulls things in, the other pushes them away. B: play / stop"),
}


def physics_color(settings, main=True):
    """The tip color in Physics mode, for the main or the off hand."""
    if not settings.physics_hands:
        return PHYSICS_COLORS['SETUP']
    if settings.physics_tool == 'FORCE':
        return PHYSICS_COLORS['PULL' if main else 'PUSH']
    return PHYSICS_COLORS['GRAB']


TOOLS_INFO = {
    'MEASURE': ("Tape Measure", "Drag to measure; ends snap to vertices. B removes the last one"),
    'CAMERA': ("Scene Camera", "Hold the trigger to aim the camera. B: next camera"),
    'LIGHT': ("Light", "Press to place a light where you point, or grab one to move and aim it. B: type"),
    'REFS': ("Reference", "Press to place the chosen picture facing you, or grab one to move it. B: face me"),
}
LIGHT_COLOR = (1.0, 0.86, 0.45)
REFS_COLOR = (0.75, 0.85, 0.95)
TOOL_COLORS = {'MEASURE': MEASURE_COLOR, 'CAMERA': CAMERA_COLOR, 'LIGHT': LIGHT_COLOR,
               'REFS': REFS_COLOR}
TOOL_ICONS = {'MEASURE': "ruler", 'CAMERA': "camera", 'LIGHT': "light", 'REFS': "image"}


def tool_color(settings, off=False):
    """A Tools mode tip's ring color; the Light tool's off hand selects, so it's the selection's blue."""
    if settings.tool == 'LIGHT' and off:
        return (0.45, 0.72, 1.0)
    return TOOL_COLORS.get(settings.tool, MEASURE_COLOR)

DRAW_INFO = {
    'TUBE': ("Draw Tube", "Round stroke mesh. B undoes the last stroke"),
    'RIBBON': ("Draw Ribbon", "Roll your wrist to twist the band. B undoes the last stroke"),
    'NOTE': ("Draw Note", "Annotation line, not geometry. B undoes the last note"),
    'PENCIL': ("Draw Pencil", "Grease Pencil, on the current frame. B undoes the last stroke"),
    'EDIT': ("Edit Strokes", "Grab part of a stroke and pull it into shape (not Pencil strokes)"),
    'ERASE': ("Erase", "Sweep through strokes, notes and pencil lines to erase them"),
}

PYTHON_INFO = {
    'DRAW': ("Draw", "Pushes the surface out. Other hand: pushes in"),
    'SMOOTH': ("Smooth", "Relaxes bumps and evens out the surface"),
    'GRAB': ("Grab", "Grab the surface and move or twist it"),
    'INFLATE': ("Inflate", "Swells the surface outward. Other hand: shrinks"),
}

_FONT_PX = 64
_CAP = 0.72  # cap height / font size, roughly

_last = {"key": None, "time": 0.0, "size": None, "size_time": -10.0}


def card_content(settings, sculpt_brush):
    """(title, description, color, icon) for the current mode and tool."""
    if settings.mode == 'DRAW':
        from . import strokes
        tool, style = settings.draw_tool, settings.draw_style
        if tool == 'DRAW':
            title, desc = DRAW_INFO[style]
        else:
            title, desc = DRAW_INFO[tool]
        color = strokes.COLORS.get(settings.draw_color, (1.0, 1.0, 1.0)) if tool == 'DRAW' else MODEL_COLOR
        icon = {'EDIT': "edit", 'ERASE': "eraser"}.get(tool, style.lower())
        content = (title, desc, color, icon)
    elif settings.mode == 'PAINT':
        from . import paint
        b = paint.brush(bpy_context())
        name = b.name if b else "No brush"
        if settings.paint_tool == 'PICK':
            content = ("Pick Color", "Touch the surface to take its color (other hand: 2nd color)",
                       paint.get_color(bpy_context()), "eyedropper")
        else:
            content = (name, paint.INFO.get(name, ""), paint.get_color(bpy_context()), "paint")
    elif settings.mode == 'POSE':
        from . import puppet
        take = puppet.state()
        if take == 'COUNTDOWN':
            content = (f"Recording in {int(puppet.countdown_left()) + 1}", "Get in position", RECORD_COLOR, "record")
        elif take is not None:
            content = ("Recording", f"Frame {bpy_context().scene.frame_current}. Press Stop rec to end",
                       RECORD_COLOR, "record")
        else:
            title, desc = POSE_INFO[settings.pose_tool]
            content = (title, desc, POSE_COLORS[settings.pose_tool], settings.pose_tool.lower())
    elif settings.mode == 'RIG':
        from . import rig
        if settings.rig_tool == 'BONES':
            title, desc = RIG_INFO['BONES']
            content = (title, desc, rig.BONES_COLOR, "bones")
        else:
            title, desc = RIG_INFO[settings.weight_brush]
            content = (title, desc, rig.BRUSH_COLORS[settings.weight_brush], "weights")
    elif settings.mode == 'PHYSICS':
        tool = settings.physics_tool if settings.physics_hands else 'SETUP'
        title, desc = PHYSICS_INFO[tool]
        content = (title, desc, physics_color(settings), {'GRAB': "throw", 'FORCE': "force"}.get(tool, "physics"))
    elif settings.mode == 'TOOLS':
        title, desc = TOOLS_INFO[settings.tool]
        if settings.tool == 'LIGHT':
            from . import lookdev
            title = lookdev.TYPE_NAMES[settings.light_type] + " Light"
        content = (title, desc, TOOL_COLORS[settings.tool], TOOL_ICONS[settings.tool])
    elif settings.mode == 'MODEL':
        from . import hardsurface, kit, primitives, shading
        op = settings.model_add_as
        if settings.uv_tool != 'NONE':
            from . import uv
            title, desc = UV_INFO[settings.uv_tool]
            content = (title, desc, uv.SEAM_COLOR if settings.uv_tool == 'SEAM' else uv.FIX_COLOR,
                       settings.uv_tool.lower())
        elif settings.model_mark != 'NONE':
            title, desc = MARK_INFO[settings.model_mark]
            color = {'HARD': shading.HARD_COLOR, 'SOFT': shading.SOFT_COLOR}.get(settings.model_mark, (1.0, 1.0, 1.0))
            content = (title, desc, color, "edit")
        elif op in CUT_INFO and hardsurface.cut_target(bpy_context()) is not None:
            verb, desc = CUT_INFO[op]
            if kit.is_key(settings.primitive):
                desc = KIT_STAMP_INFO if settings.kit_stamp else KIT_DROP_INFO
            content = (f"{verb} {primitives.label(settings.primitive)}", desc, CUT_COLORS[op],
                       {'DIFFERENCE': "cut", 'UNION': "union"}.get(op, "intersect"))
        elif kit.is_key(settings.primitive):
            verb = "Stamp" if settings.kit_stamp else "Place"
            content = (f"{verb} {primitives.label(settings.primitive)}",
                       KIT_STAMP_INFO if settings.kit_stamp else KIT_DROP_INFO + ". Other hand selects",
                       MODEL_COLOR, "stamp" if settings.kit_stamp else "kit")
        else:
            title = "Add " + primitives.label(settings.primitive)
            content = (title, "Drag to create. Other hand: touch to select and move", MODEL_COLOR, "model")
    elif settings.mode == 'RETOPO':
        from . import retopo
        content = retopo.card_content(settings)
    elif settings.engine == 'NATIVE':
        name = sculpt_brush.name if sculpt_brush else "No brush"
        content = (name, NATIVE_INFO.get(name, ""), NATIVE_COLORS.get(name, (0.9, 0.9, 0.9)), "sculpt")
    else:
        title, desc = PYTHON_INFO.get(settings.brush, (settings.brush.title(), ""))
        content = (title, desc, PYTHON_COLORS.get(settings.brush, (1.0, 1.0, 1.0)), "sculpt")
    key = (settings.mode, settings.engine, content[0])
    if key != _last["key"]:
        _last["key"] = key
        _last["time"] = time.monotonic()
    return content


def _cm(v):
    return f"{v * 100:.1f} cm" if v < 1.0 else f"{v:.2f} m"


def status_size(context, settings):
    """The size that goes with the tool: brush radius, stroke width, IK chain, lens, grid."""
    mode = settings.mode
    if mode == 'SCULPT':
        return _cm(settings.radius)
    if mode == 'RETOPO':
        from . import retopo
        return retopo.status_size(settings)
    if mode == 'PAINT':
        return _cm(settings.paint_radius)
    if mode == 'DRAW':
        return _cm(settings.draw_width) if settings.draw_tool == 'DRAW' else ""
    if mode == 'POSE':
        return f"chain {settings.pose_chain}" if settings.pose_tool == 'IK' else ""
    if mode == 'RIG':
        return _cm(settings.weight_radius) if settings.rig_tool == 'WEIGHTS' else ""
    if mode == 'PHYSICS':
        from . import anim
        if settings.physics_hands and settings.physics_tool == 'FORCE':
            return f"force {settings.physics_force:.0f}"
        return f"frame {context.scene.frame_current}" if anim.is_playing(context) else ""
    if mode == 'TOOLS':
        cam = context.scene.camera
        if settings.tool == 'CAMERA' and cam is not None and cam.data.type == 'PERSP':
            return f"{cam.data.lens:.0f} mm"
        if settings.tool == 'LIGHT':
            from . import lookdev
            return lookdev.status(context)
        if settings.tool == 'REFS':
            from . import refs
            return refs.status(context)
        return ""
    if settings.grid_snap:
        return "grid " + _cm(settings.grid_size)
    return ""


def bpy_context():
    import bpy
    return bpy.context


def show_description():
    return time.monotonic() - _last["time"] < DESCRIPTION_SECONDS


def billboard(anchor, viewer, scale):
    """Matrix at anchor whose XY plane faces the viewer, Z up kept vertical."""
    z = viewer - anchor
    if z.length < 1e-6:
        z = Vector((0.0, -1.0, 0.0))
    z.normalize()
    x = Vector((0.0, 0.0, 1.0)).cross(z)
    if x.length < 1e-6:
        x = Vector((1.0, 0.0, 0.0))
    x.normalize()
    y = z.cross(x)
    m = Matrix((x, y, z)).transposed().to_4x4()
    m.translation = anchor
    return m @ Matrix.Scale(scale, 4)


def text_width(text, height):
    blf.size(0, _FONT_PX)
    return blf.dimensions(0, text)[0] * height / (_FONT_PX * _CAP)


def draw_text(m, x, y, text, height, color):
    s = height / (_FONT_PX * _CAP)
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(m @ Matrix.Translation((x, y, 0.0005)) @ Matrix.Scale(s, 4))
    blf.size(0, _FONT_PX)
    blf.position(0, 0.0, 0.0, 0.0)
    blf.color(0, *color)
    blf.draw(0, text)
    gpu.matrix.pop()


_TAG_H = 0.011
_TAG_TEXT = 0.0052


def draw_tag(anchor, viewer, scale, text, color):
    """A small name tag facing the viewer, its bottom center at anchor (the selected object's name)."""
    from . import gfx, vr_ui
    m = billboard(anchor, viewer, scale)
    tw = text_width(text, _TAG_TEXT)
    pad = 0.003
    dot = 0.0016
    x0 = -(tw + 2 * dot + 0.002) / 2 - pad
    x1 = -x0
    cv = gfx.Canvas()
    cv.rect(x0 - 0.0005, -0.0005, x1 + 0.0005, _TAG_H + 0.0005, vr_ui.COL_EDGE, _TAG_H / 2 + 0.0005)
    cv.rect(x0, 0.0, x1, _TAG_H, vr_ui.COL_CARD, _TAG_H / 2)
    cv.disc(x0 + pad + dot, _TAG_H / 2, dot, (*color[:3], 1.0))
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(m)
    cv.draw()
    gpu.matrix.pop()
    draw_text(m, x0 + pad + 2 * dot + 0.002, (_TAG_H - _TAG_TEXT) / 2, text, _TAG_TEXT, vr_ui.COL_TEXT)
    gpu.state.blend_set('NONE')


_STATUS_H = 0.0155
_STATUS_TEXT = 0.0062
_DESC_TEXT = 0.0052


def draw_status(anchor, viewer, scale, content, size="", with_description=None):
    """The tip status: [icon] Title  size, facing the viewer, centered on anchor; the description
    below it for a few seconds after the tool changes."""
    from . import gfx, vr_ui
    title, desc, color, icon = content
    now = time.monotonic()
    if size != _last["size"]:
        if _last["size"] is not None:
            _last["size_time"] = now
        _last["size"] = size
    if with_description is None:
        left = DESCRIPTION_SECONDS - (now - _last["time"])
        desc_alpha = min(max(left / DESCRIPTION_FADE, 0.0), 1.0)
    else:
        desc_alpha = 1.0 if with_description else 0.0
    if not desc:
        desc_alpha = 0.0
    m = billboard(anchor, viewer, scale)

    pad = 0.0042
    disc = _STATUS_H / 2 - 0.0018
    title_w = text_width(title, _STATUS_TEXT)
    size_w = text_width(size, _STATUS_TEXT) if size else 0.0
    width = pad * 0.6 + 2 * disc + 0.0035 + title_w + (0.0045 + size_w if size else 0.0) + pad
    x0, y0 = -width / 2, 0.0
    x1, y1 = x0 + width, _STATUS_H
    mid = _STATUS_H / 2

    cv = gfx.Canvas()
    cv.rect(x0 - 0.0006, y0 - 0.0006, x1 + 0.0006, y1 + 0.0006, vr_ui.COL_EDGE, _STATUS_H / 2 + 0.0006)
    cv.rect(x0, y0, x1, y1, vr_ui.COL_CARD, _STATUS_H / 2)
    cx = x0 + pad * 0.6 + disc
    a = 1.0
    if color == RECORD_COLOR:
        a = 0.55 + 0.45 * math.cos(now * 2 * math.pi * 1.2) ** 2  # Recording: it breathes.
    cv.disc(cx, mid, disc, (*color[:3], a))
    light = 0.3 * color[0] + 0.55 * color[1] + 0.15 * color[2] > 0.4
    gfx.icon(cv, icon, cx, mid, disc * 1.25, (0.05, 0.05, 0.06, 0.9) if light else (1.0, 1.0, 1.0, 0.95))
    desc_w = text_width(desc, _DESC_TEXT) if desc_alpha > 0 else 0.0
    if desc_alpha > 0:
        dy1 = y0 - 0.0025
        dy0 = dy1 - (_DESC_TEXT + 0.0055)
        cv.rect(-desc_w / 2 - pad, dy0, desc_w / 2 + pad, dy1, (*vr_ui.COL_CARD[:3], 0.85 * desc_alpha),
                (dy1 - dy0) / 2)
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(m)
    cv.draw()
    gpu.matrix.pop()

    tx = cx + disc + 0.0035
    draw_text(m, tx, mid - _STATUS_TEXT / 2, title, _STATUS_TEXT, vr_ui.COL_TEXT)
    if size:
        flash = max(1.0 - (now - _last["size_time"]) / SIZE_FLASH, 0.0)
        size_color = gfx.mix(vr_ui.COL_DIM, vr_ui.COL_ACCENT, flash)
        draw_text(m, tx + title_w + 0.0045, mid - _STATUS_TEXT / 2, size, _STATUS_TEXT, size_color)
    if desc_alpha > 0:
        dmid = y0 - 0.0025 - (_DESC_TEXT + 0.0055) / 2
        draw_text(m, -desc_w / 2, dmid - _DESC_TEXT / 2, desc, _DESC_TEXT,
                  (*vr_ui.COL_DIM[:3], desc_alpha))
    gpu.state.blend_set('NONE')


def clear():
    _last.update(key=None, time=0.0, size=None, size_time=-10.0)
