"""Brush card: a small panel above the right controller with the brush icon and name.

After the brush changes, it also shows a one-line description for a few seconds.
"""

import math
import time

import blf
import gpu
import numpy as np
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector

DESCRIPTION_SECONDS = 4.0

NATIVE_INFO = {
    "Draw": "Pushes the surface out. Left hand: pushes in",
    "Clay Strips": "Builds up flat strips, like layering clay",
    "Smooth": "Relaxes bumps and evens out the surface",
    "Inflate/Deflate": "Swells the surface outward. Left hand: shrinks",
    "Crease Sharp": "Cuts a sharp groove. Left hand: pinches a ridge",
    "Flatten/Contrast": "Flattens toward an average plane. Left hand: adds contrast",
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

DRAW_INFO = {
    'TUBE': ("Draw Tube", "Squeeze harder for thicker. B undoes the last stroke"),
    'RIBBON': ("Draw Ribbon", "Roll your wrist to twist the band. B undoes the last stroke"),
    'NOTE': ("Draw Note", "Annotation line, not geometry. B undoes the last note"),
    'EDIT': ("Edit Strokes", "Grab part of a stroke and pull it into shape"),
    'ERASE': ("Erase", "Sweep through strokes and notes to erase them"),
}

PYTHON_INFO = {
    'DRAW': ("Draw", "Pushes the surface out. Left hand: pushes in"),
    'SMOOTH': ("Smooth", "Relaxes bumps and evens out the surface"),
    'GRAB': ("Grab", "Grab the surface and move or twist it"),
    'INFLATE': ("Inflate", "Swells the surface outward. Left hand: shrinks"),
}

# Sizes in meters, in hand space (scaled by the VR viewer scale).
_ICON = 0.032
_PAD = 0.005
_TITLE_H = 0.011
_DESC_H = 0.0065
_FONT_PX = 64
_CAP = 0.72  # cap height / font size, roughly

_textures = {}
_texture_retry = {}
_last = {"key": None, "time": 0.0}


def brush_texture(brush):
    """GPU texture of a brush's asset preview, or None if it isn't loaded (yet)."""
    if brush is None:
        return None
    key = (brush.name, brush.library.filepath if brush.library else "")
    tex = _textures.get(key)
    if tex is not None:
        return tex
    # Previews can load lazily; don't re-read a missing one every frame.
    now = time.monotonic()
    if now < _texture_retry.get(key, 0.0):
        return None
    _texture_retry[key] = now + 1.0
    p = brush.preview
    if p is None:
        return None
    w, h = p.image_size
    pixels = p.image_pixels_float
    if not (w and h):
        w, h = p.icon_size
        pixels = p.icon_pixels_float
    if not (w and h) or len(pixels) != w * h * 4:
        return None
    arr = np.empty(w * h * 4, dtype=np.float32)
    pixels.foreach_get(arr)
    tex = gpu.types.GPUTexture((w, h), format='RGBA32F', data=gpu.types.Buffer('FLOAT', w * h * 4, arr.tolist()))
    _textures[key] = tex
    return tex


def card_content(settings, sculpt_brush, colors):
    """(title, description, color, texture) for the current brush."""
    if settings.mode == 'DRAW':
        from . import strokes
        tool, style = settings.draw_tool, settings.draw_style
        if tool == 'DRAW':
            title, desc = DRAW_INFO[style]
        else:
            title, desc = DRAW_INFO[tool]
        color = strokes.COLORS.get(settings.draw_color, (1.0, 1.0, 1.0)) if tool == 'DRAW' else MODEL_COLOR
        content = (title, desc, color, None)
    elif settings.mode == 'MODEL':
        from . import primitives
        title = "Add " + primitives.label(settings.primitive)
        content = (title, "Drag in empty space to create. Touch an object to grab it",
                   MODEL_COLOR, None)
    elif settings.engine == 'NATIVE':
        name = sculpt_brush.name if sculpt_brush else "No brush"
        content = (name, NATIVE_INFO.get(name, ""), NATIVE_COLORS.get(name, (0.9, 0.9, 0.9)),
                   brush_texture(sculpt_brush))
    else:
        title, desc = PYTHON_INFO.get(settings.brush, (settings.brush.title(), ""))
        content = (title, desc, colors.get(settings.brush, (1.0, 1.0, 1.0)), None)
    key = (settings.mode, settings.engine, content[0])
    if key != _last["key"]:
        _last["key"] = key
        _last["time"] = time.monotonic()
    return content


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


def draw_card(anchor, viewer, scale, content, with_description=None):
    title, desc, color, texture = content
    if with_description is None:
        with_description = show_description()
    if not desc:
        with_description = False
    m = billboard(anchor, viewer, scale)

    text_w = text_width(title, _TITLE_H)
    if with_description:
        text_w = max(text_w, text_width(desc, _DESC_H))
    width = _PAD + _ICON + _PAD + text_w + _PAD
    height = _PAD * 2 + _ICON
    x0, y0 = -width / 2.0, 0.0
    x1, y1 = x0 + width, y0 + height

    uniform = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(m)

    quad = [(x0, y0, 0), (x1, y0, 0), (x1, y1, 0), (x0, y1, 0)]
    batch = batch_for_shader(uniform, 'TRIS', {"pos": quad}, indices=((0, 1, 2), (0, 2, 3)))
    uniform.uniform_float("color", (0.06, 0.06, 0.07, 0.82))
    batch.draw(uniform)
    batch = batch_for_shader(uniform, 'LINE_LOOP', {"pos": quad})
    uniform.uniform_float("color", (*color, 0.9))
    batch.draw(uniform)

    ix0, iy0 = x0 + _PAD, y0 + _PAD
    icon = [(ix0, iy0, 0.0002), (ix0 + _ICON, iy0, 0.0002), (ix0 + _ICON, iy0 + _ICON, 0.0002), (ix0, iy0 + _ICON, 0.0002)]
    if texture is not None:
        image = gpu.shader.from_builtin('IMAGE')
        batch = batch_for_shader(image, 'TRI_FAN', {"pos": icon, "texCoord": ((0, 0), (1, 0), (1, 1), (0, 1))})
        image.uniform_sampler("image", texture)
        batch.draw(image)
    else:
        # No preview (Python brushes): a dot in the brush color, matching the cursor rings.
        cx, cy, r = ix0 + _ICON / 2.0, iy0 + _ICON / 2.0, _ICON * 0.4
        disc = [(cx, cy, 0.0002)] + [
            (cx + r * math.cos(a), cy + r * math.sin(a), 0.0002)
            for a in (2.0 * math.pi * i / 32 for i in range(33))
        ]
        batch = batch_for_shader(uniform, 'TRI_FAN', {"pos": disc})
        uniform.uniform_float("color", (*color, 1.0))
        batch.draw(uniform)
    gpu.matrix.pop()

    tx = ix0 + _ICON + _PAD
    if with_description:
        draw_text(m, tx, y0 + _PAD + _ICON * 0.52, title, _TITLE_H, (1.0, 1.0, 1.0, 1.0))
        draw_text(m, tx, y0 + _PAD + _ICON * 0.12, desc, _DESC_H, (0.78, 0.78, 0.8, 1.0))
    else:
        draw_text(m, tx, y0 + _PAD + (_ICON - _TITLE_H) / 2.0, title, _TITLE_H, (1.0, 1.0, 1.0, 1.0))
    gpu.state.blend_set('NONE')


def clear():
    _textures.clear()
    _texture_retry.clear()
