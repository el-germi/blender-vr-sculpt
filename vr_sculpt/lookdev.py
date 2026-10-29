"""Look dev in VR: lights (the Tools mode Light tool), the VR view's look (its shading, matcaps, studio
HDRIs, and a World built from an HDRI) and materials (Model mode's Material page).

Lights are Blender's own light objects. The headset doesn't draw light objects (the session's
show_object_extras is off), so they get gizmos drawn with gpu: a disc with the type's icon, plus the
aim line, the spot cone or the area panel. The VR view's look is the session's own View3DShading
(window_manager.xr_session_settings.shading), not the desktop's. Its studio_light is one dynamic enum
that means the studio light, the matcap or the HDRI depending on the shading type and light, so
choosing one switches to the right type for a moment. Previews of matcaps and HDRIs are read with
OpenImageIO (bundled with Blender) into numpy, never through bpy.data, so building them makes no data.
Materials are the object's first material's Principled BSDF; a shared material is copied first.
"""

import math
import time

import bpy
import numpy as np
from mathutils import Color, Matrix, Vector

from . import gfx, grid, hud, primitives, vr_ui

# --- lights ---------------------------------------------------------------------

LIGHT_TYPES = (('POINT', "Point", "light_point"), ('SUN', "Sun", "light_sun"), ('SPOT', "Spot", "light_spot"),
               ('AREA', "Area", "light_area"))
LIGHT_ICONS = {ident: icon for ident, _name, icon in LIGHT_TYPES}
TYPE_NAMES = {ident: name for ident, name, _icon in LIGHT_TYPES}

# Light colors to pick from (as shown, not linear): warm to cool whites, then a few gels.
LIGHT_PALETTE = (
    ('CANDLE', "Candle", (1.0, 0.62, 0.32)),
    ('WARM', "Warm", (1.0, 0.82, 0.62)),
    ('WHITE', "White", (1.0, 1.0, 1.0)),
    ('COOL', "Cool", (0.76, 0.86, 1.0)),
    ('RED', "Red", (1.0, 0.27, 0.22)),
    ('GREEN', "Green", (0.38, 1.0, 0.45)),
    ('BLUE', "Blue", (0.3, 0.5, 1.0)),
    ('MAGENTA', "Magenta", (0.92, 0.36, 1.0)),
)

REACH = 0.03  # hand-space meters: a tip this close to a light grabs or selects it
GIZMO_R = 0.009  # hand-space meters: the gizmo's disc
AIM_LENGTH = 0.08  # hand-space meters: the aim line and the spot cone's length
SELECT_COLOR = (0.45, 0.72, 1.0)  # like the Model selection's
TOUCH_COLOR = (1.0, 0.85, 0.3)  # like Model's "what the tip would select" box
POWER_RANGE = (0.1, 5000.0)  # watts, the Power slider (logarithmic)
SUN_RANGE = (0.05, 50.0)  # W/m^2, the sun's Strength slider (logarithmic)
KELVIN_RANGE = (1000.0, 12000.0)

# Where each type keeps its size, and the setting new lights of that type take it from.
_SIZE_PROP = {'POINT': "shadow_soft_size", 'SPOT': "shadow_soft_size", 'AREA': "size", 'SUN': "angle"}
_SIZE_DEFAULT = {'POINT': "light_radius", 'SPOT': "light_radius", 'AREA': "light_area", 'SUN': "light_angle"}


def to_linear(rgb):
    """A color as shown (sRGB) to scene linear, as lights and shaders store it."""
    return tuple(Color(rgb[:3]).from_srgb_to_scene_linear())


def to_display(rgb):
    return tuple(Color(rgb[:3]).from_scene_linear_to_srgb())


def lights(context):
    """The visible light objects."""
    return [o for o in context.view_layer.objects if o is not None and o.type == 'LIGHT' and o.visible_get()]


def selected_light(context):
    obj = context.view_layer.objects.active
    return obj if obj is not None and obj.type == 'LIGHT' and obj.select_get() else None


def light_at(context, point, scale):
    """The selectable light nearest point within REACH (hand space), or None."""
    best, best_d = None, REACH * scale
    for obj in lights(context):
        if obj.hide_select:
            continue
        d = (obj.matrix_world.translation - Vector(point)).length
        if d <= best_d:
            best, best_d = obj, d
    return best


def kind_of(context, obj):
    """The type the Light page shows: the selected light's, or the type new lights get."""
    return obj.data.type if obj is not None else context.scene.vr_sculpt.light_type


def next_type(settings):
    ids = [i for i, _n, _ic in LIGHT_TYPES]
    settings.light_type = ids[(ids.index(settings.light_type) + 1) % len(ids)]


def next_type_name(kind):
    ids = [i for i, _n, _ic in LIGHT_TYPES]
    return TYPE_NAMES[ids[(ids.index(kind) + 1) % len(ids)]] if kind in ids else "Point"


def light_get(context, obj, what):
    """A value the Light page shows: of obj, or for new lights when obj is None. what: 'color' (as shown),
    'power' (W, a sun's W/m^2), 'size' (radius, area side, sun angle), 'cone', 'blend', 'radius',
    'shadow', 'kelvin' (temperature on), 'temperature'."""
    settings = context.scene.vr_sculpt
    kind = kind_of(context, obj)
    if obj is None:
        if what == 'color':
            return tuple(settings.light_color)
        if what == 'power':
            return settings.light_sun if kind == 'SUN' else settings.light_power
        if what == 'size':
            return getattr(settings, _SIZE_DEFAULT[kind])
        if what == 'cone':
            return settings.light_spot
        if what == 'radius':
            return settings.light_radius
        return None
    d = obj.data
    if what == 'color':
        return to_display(d.color)
    if what == 'power':
        return d.energy
    if what == 'size':
        return getattr(d, _SIZE_PROP[d.type])
    if what == 'cone':
        return getattr(d, "spot_size", settings.light_spot)
    if what == 'blend':
        return getattr(d, "spot_blend", 0.15)
    if what == 'radius':
        return d.shadow_soft_size
    if what == 'shadow':
        return d.use_shadow
    if what == 'kelvin':
        return d.use_temperature
    if what == 'temperature':
        return d.temperature
    return None


def light_set(context, obj, what, value):
    """Set a Light page value on obj (see light_get). The setting new lights take it from follows, so the
    next light of the type carries on from this one; with obj None only that setting changes."""
    settings = context.scene.vr_sculpt
    kind = kind_of(context, obj)
    if what == 'color':
        settings.light_color = value[:3]
    elif what == 'power':
        setattr(settings, "light_sun" if kind == 'SUN' else "light_power", value)
    elif what == 'size':
        setattr(settings, _SIZE_DEFAULT[kind], value)
    elif what == 'cone':
        settings.light_spot = value
    elif what == 'radius':
        settings.light_radius = value
    if obj is None:
        return
    d = obj.data
    if what == 'color':
        d.color = to_linear(value)
    elif what == 'power':
        d.energy = value
    elif what == 'size':
        setattr(d, _SIZE_PROP[d.type], value)
        if d.type == 'AREA' and d.shape in {'SQUARE', 'DISK'}:
            d.size_y = value
    elif what == 'cone' and d.type == 'SPOT':
        d.spot_size = value
    elif what == 'blend' and d.type == 'SPOT':
        d.spot_blend = value
    elif what == 'radius':
        d.shadow_soft_size = value
    elif what == 'shadow':
        d.use_shadow = bool(value)
    elif what == 'kelvin':
        d.use_temperature = bool(value)
    elif what == 'temperature':
        d.temperature = value
        d.use_temperature = True


def _aim(pose, location, scale=(1.0, 1.0, 1.0)):
    """A light's matrix at location, turned like the controller (lights shine down -Z, as aim poses point)."""
    m = pose.to_3x3().normalized().to_4x4() @ Matrix.Diagonal((*scale, 1.0))
    m.translation = location
    return m


def new_light(context, kind, matrix):
    """A light of kind with the settings for new lights, at matrix, selected. Material Preview in the VR view
    shows scene lights from then on (it doesn't by default, so the light would seem to do nothing)."""
    settings = context.scene.vr_sculpt
    data = bpy.data.lights.new("VR " + TYPE_NAMES[kind], kind)
    data.color = to_linear(settings.light_color)
    data.energy = settings.light_sun if kind == 'SUN' else settings.light_power
    setattr(data, _SIZE_PROP[kind], getattr(settings, _SIZE_DEFAULT[kind]))
    if kind == 'AREA':
        data.size_y = data.size
    if kind == 'SPOT':
        data.spot_size = settings.light_spot
    obj = bpy.data.objects.new(data.name, data)
    context.scene.collection.objects.link(obj)
    obj.matrix_world = matrix
    primitives.make_active(context, obj)
    shading(context).use_scene_lights = True
    return obj


def delete_light(context, obj):
    data = obj.data
    bpy.data.objects.remove(obj)
    if data is not None and data.users == 0:
        bpy.data.lights.remove(data)


def duplicate_light(context, obj):
    return primitives.duplicate(context, obj)


def choose_light(context, name):
    """The lights picker's answer: select that light ("" selects nothing: the page edits new lights)."""
    obj = context.scene.objects.get(name) if name else None
    primitives.make_active(context, obj if obj is not None and obj.type == 'LIGHT' else None)


class PlaceLight:
    """Main trigger in empty space: a new light of the New type at the tip, aimed where the controller points.
    It follows the controller (place and aim) until the trigger lets go."""

    def __init__(self, context, pose, tip_pose):
        settings = context.scene.vr_sculpt
        self.obj = new_light(context, settings.light_type,
                             _aim(pose, grid.snap_if(settings, tip_pose.translation)))
        self.changed = True

    def update(self, context, pose, tip_pose, value):
        self.obj.matrix_world = _aim(pose, grid.snap_if(context.scene.vr_sculpt, tip_pose.translation))

    def finish(self, context):
        context.view_layer.update()
        from . import anim
        anim.auto_key(context, self.obj)


class GrabLight:
    """Main trigger on a light: it's selected, and once the hand clearly moves (interactions.DragGate) it
    moves with the tip and aims where the controller points, like the camera tool's camera."""

    def __init__(self, context, obj, tip_pose):
        from . import interactions
        self.obj = obj
        self.scale = obj.matrix_world.to_scale()
        self.offset = obj.matrix_world.translation - tip_pose.translation
        self.gate = interactions.DragGate(context, tip_pose)
        self.changed = False
        primitives.make_active(context, obj)

    def update(self, context, pose, tip_pose, value):
        if not self.gate.open:
            base = self.gate.update(tip_pose)
            if base is None:
                return
            self.offset = self.obj.matrix_world.translation - base.translation
        where = grid.snap_if(context.scene.vr_sculpt, tip_pose.translation + self.offset)
        new = _aim(pose, where, self.scale)
        if new != self.obj.matrix_world:
            self.obj.matrix_world = new
            self.changed = True

    def finish(self, context):
        if self.changed:
            context.view_layer.update()
            from . import anim
            anim.auto_key(context, self.obj)


def pick(context, settings, off, pose, tip_pose, scale):
    """The Light tool's trigger handler. Main hand: grab the light in reach, else place a new one. Off hand
    (as in Model mode): select the light in reach (hold to move it, keeping its aim), else select nothing."""
    from . import interactions
    obj = light_at(context, tip_pose.translation, scale)
    if off:
        if obj is None:
            return interactions.Deselect(context)
        handler = interactions.MoveObject(context, obj, tip_pose, scale)
        handler.snap = False  # A light doesn't sit on surfaces.
        return handler
    if obj is not None:
        return GrabLight(context, obj, tip_pose)
    return PlaceLight(context, pose, tip_pose)


def watts(v):
    if v >= 1000.0:
        return f"{v / 1000:.1f} kW"
    return f"{v:.0f} W" if v >= 10.0 else f"{v:.1f} W" if v >= 1.0 else f"{v:.2f} W"


def strength_text(v):
    return f"{v:.1f}" if v < 10.0 else f"{v:.0f}"


def power_text(kind, v):
    return strength_text(v) + " W/m\u00b2" if kind == 'SUN' else watts(v)


def status(context):
    """The tip status's size for the Light tool: the power of the selected light, or of new ones."""
    obj = selected_light(context)
    return power_text(kind_of(context, obj), light_get(context, obj, 'power'))


def gizmo_color(obj):
    """The light's color as shown, at full brightness (temperature included)."""
    d = obj.data
    lin = list(d.color)
    if d.use_temperature:
        lin = [a * b for a, b in zip(lin, d.temperature_color)]
    m = max(lin)
    if m <= 1e-6:
        return (1.0, 1.0, 1.0)
    return to_display([min(c / m, 1.0) for c in lin])


def _ring(center, u, v, r, n=24):
    pts = [center + (u * math.cos(2 * math.pi * k / n) + v * math.sin(2 * math.pi * k / n)) * r for k in range(n)]
    return [p for k in range(n) for p in (pts[k], pts[(k + 1) % n])]


def gizmo_lines(obj, scale):
    """World-space line segments (pairs of points) of a light's gizmo: a sun's direction, a spot's cone, an
    area light's panel, and the aim line of each."""
    d = obj.data
    mw = obj.matrix_world
    loc = mw.translation
    rot = mw.to_3x3().normalized()
    fwd = rot @ Vector((0.0, 0.0, -1.0))
    side, up = rot @ Vector((1.0, 0.0, 0.0)), rot @ Vector((0.0, 1.0, 0.0))
    length = AIM_LENGTH * scale
    start = loc + fwd * GIZMO_R * scale
    if d.type == 'SUN':
        end = loc + fwd * length * 1.6
        head = 0.012 * scale
        return [start, end, end, end - fwd * head + side * head * 0.5, end, end - fwd * head - side * head * 0.5,
                end, end - fwd * head + up * head * 0.5, end, end - fwd * head - up * head * 0.5]
    if d.type == 'SPOT':
        half = min(d.spot_size / 2.0, math.radians(80.0))
        r = length * math.tan(half)
        base = loc + fwd * length
        out = [start, base]
        for a in (0.0, 0.5 * math.pi, math.pi, 1.5 * math.pi):
            out += [loc, base + (side * math.cos(a) + up * math.sin(a)) * r]
        return out + _ring(base, side, up, r)
    if d.type == 'AREA':
        sx = d.size / 2.0
        sy = (d.size_y if d.shape in {'RECTANGLE', 'ELLIPSE'} else d.size) / 2.0
        if d.shape in {'DISK', 'ELLIPSE'}:
            pts = [mw @ Vector((sx * math.cos(2 * math.pi * k / 24), sy * math.sin(2 * math.pi * k / 24), 0.0))
                   for k in range(24)]
        else:
            pts = [mw @ Vector(c) for c in ((-sx, -sy, 0.0), (sx, -sy, 0.0), (sx, sy, 0.0), (-sx, sy, 0.0))]
        out = [p for k in range(len(pts)) for p in (pts[k], pts[(k + 1) % len(pts)])]
        return out + [start, loc + fwd * length]
    return []


def draw(context, ss, tips, scale):
    """The lights' gizmos, while the Light tool is on (the headset doesn't show light objects)."""
    settings = getattr(context.scene, "vr_sculpt", None)
    if settings is None or settings.mode != 'TOOLS' or settings.tool != 'LIGHT':
        return
    objs = lights(context)
    if not objs:
        return
    import gpu
    from gpu_extras.batch import batch_for_shader
    viewer = Vector(ss.viewer_pose_location)
    active = selected_light(context)
    near = set()
    for tip in tips:
        obj = light_at(context, tip, scale)
        if obj is not None:
            near.add(obj.name)
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    colors = {obj.name: gizmo_color(obj) for obj in objs}
    for obj in objs:
        coords = gizmo_lines(obj, scale)
        if coords:
            hot = obj == active or obj.name in near
            shader.uniform_float("color", (*colors[obj.name], 0.95 if hot else 0.55))
            batch_for_shader(shader, 'LINES', {"pos": coords}).draw(shader)
    for obj in objs:
        ring = SELECT_COLOR if obj == active else TOUCH_COLOR if obj.name in near else (1.0, 1.0, 1.0)
        cv = gfx.Canvas()
        cv.disc(0.0, 0.0, GIZMO_R + 0.0014, (*ring, 1.0 if ring != (1.0, 1.0, 1.0) else 0.35))
        cv.disc(0.0, 0.0, GIZMO_R, vr_ui.COL_CARD)
        gfx.icon(cv, LIGHT_ICONS.get(obj.data.type, "light"), 0.0, 0.0, GIZMO_R * 1.45, (*colors[obj.name], 1.0))
        gpu.matrix.push()
        gpu.matrix.multiply_matrix(hud.billboard(obj.matrix_world.translation, viewer, scale))
        cv.draw()
        gpu.matrix.pop()
    gpu.state.blend_set('NONE')
    if active is not None and active.visible_get():
        top = active.matrix_world.translation + Vector((0.0, 0.0, (GIZMO_R + 0.005) * scale))
        hud.draw_tag(top, viewer, scale, active.name, SELECT_COLOR)


# --- the VR view's look -------------------------------------------------------------

VIEW_TYPES = (('SOLID', "Solid"), ('MATERIAL', "Material"), ('RENDERED', "Render"))
SOLID_LIGHTS = (('STUDIO', "Studio"), ('MATCAP', "Matcap"), ('FLAT', "Flat"))
COLOR_TYPES = (('MATERIAL', "Material"), ('OBJECT', "Object"), ('RANDOM', "Random"))
WORLD_NAME = "VR Look"
WORLD_KEY = "vrs_look"  # ID property on a World built by Use as world: the HDRI it was built from
MAPPING = "VR Mapping"
BACKGROUND = "VR Background"
ENVIRONMENT = "VR Environment"


def shading(context):
    """The VR view's shading (the session's own, not the desktop's)."""
    return context.window_manager.xr_session_settings.shading


def set_view(context, kind):
    shading(context).type = kind


def studio_lights(context, kind):
    """Blender's studio lights of kind: 'STUDIO' (Solid lighting), 'MATCAP' or 'WORLD' (HDRIs)."""
    return [sl for sl in context.preferences.studio_lights if sl.type == kind]


def _studio(kind, name):
    return next((sl for sl in bpy.context.preferences.studio_lights if sl.type == kind and sl.name == name), None)


def pretty(name):
    """'basic_bright.exr' -> 'Basic bright'."""
    base = name.rsplit(".", 1)[0] if name.lower().endswith((".exr", ".sl", ".hdr", ".png", ".jpg")) else name
    base = base.replace("_", " ")
    return base[:1].upper() + base[1:]


def look_kind(context):
    """What the shading's studio_light means right now: 'STUDIO', 'MATCAP', 'WORLD' or None (flat, wireframe)."""
    sh = shading(context)
    if sh.type in {'MATERIAL', 'RENDERED'}:
        return 'WORLD'
    if sh.type == 'SOLID' and sh.light in {'STUDIO', 'MATCAP'}:
        return sh.light
    return None


def current(context, kind):
    """The studio light, matcap or HDRI chosen for the VR view. Reads only what the shading means right now
    (the others would need a type switch); "" otherwise."""
    return shading(context).studio_light if look_kind(context) == kind else ""


def set_studio(context, kind, name):
    """Choose a studio light, matcap ('STUDIO' / 'MATCAP': Solid's lighting becomes that) or HDRI ('WORLD'). The
    shading's studio_light is one property whose meaning follows the type and light, so the type is switched
    for the moment it takes, then put back."""
    sh = shading(context)
    kept = sh.type
    if kind == 'WORLD':
        if sh.type not in {'MATERIAL', 'RENDERED'}:
            sh.type = 'MATERIAL'
    else:
        sh.type = 'SOLID'
        sh.light = kind
    try:
        sh.studio_light = name
    finally:
        sh.type = kept


def hdri_name(context):
    """The HDRI Material Preview uses in the VR view, whatever the shading is now."""
    sh = shading(context)
    if sh.type in {'MATERIAL', 'RENDERED'}:
        return sh.studio_light
    kept = sh.type
    sh.type = 'MATERIAL'
    try:
        return sh.studio_light
    finally:
        sh.type = kept


def scene_world_on(context):
    """Whether the VR view shows the scene's world (Rendered: by default) rather than the HDRI."""
    sh = shading(context)
    return sh.use_scene_world_render if sh.type == 'RENDERED' else sh.use_scene_world


def set_scene_world(context, on):
    sh = shading(context)
    if sh.type == 'RENDERED':
        sh.use_scene_world_render = on
    else:
        sh.use_scene_world = on


def scene_lights_on(context):
    sh = shading(context)
    return sh.use_scene_lights_render if sh.type == 'RENDERED' else sh.use_scene_lights


def set_scene_lights(context, on):
    sh = shading(context)
    if sh.type == 'RENDERED':
        sh.use_scene_lights_render = on
    else:
        sh.use_scene_lights = on


def our_world(context):
    """The scene's world if Use as world built it, else None."""
    w = context.scene.world
    return w if w is not None and w.get(WORLD_KEY) else None


def _hdri_image(sl):
    """The HDRI as an image, packed into the file: the bundled ones live in Blender's own folder, which
    changes with every version."""
    img = next((i for i in bpy.data.images if i.get(WORLD_KEY) == sl.name), None)
    if img is None:
        img = bpy.data.images.load(sl.path, check_existing=False)
        img.name = sl.name
        img[WORLD_KEY] = sl.name
        try:
            img.pack()
        except RuntimeError:
            pass  # Kept as a path then.
    return img


def _build_world(world, sl, rotation, strength):
    """Environment Texture of the HDRI, turned by a Mapping about Z (the same angle as Material Preview's
    rotation), into a Background of that strength (the same as Material Preview's intensity)."""
    if world.node_tree is None:
        world.use_nodes = True
    nt = world.node_tree
    nt.nodes.clear()
    coord = nt.nodes.new("ShaderNodeTexCoord")
    coord.location = (-900, 0)
    mapping = nt.nodes.new("ShaderNodeMapping")
    mapping.name = MAPPING
    mapping.location = (-700, 0)
    env = nt.nodes.new("ShaderNodeTexEnvironment")
    env.name = ENVIRONMENT
    env.location = (-500, 0)
    env.image = _hdri_image(sl)
    bg = nt.nodes.new("ShaderNodeBackground")
    bg.name = BACKGROUND
    bg.location = (-200, 0)
    out = nt.nodes.new("ShaderNodeOutputWorld")
    out.location = (0, 0)
    nt.links.new(coord.outputs["Generated"], mapping.inputs["Vector"])
    nt.links.new(mapping.outputs["Vector"], env.inputs["Vector"])
    nt.links.new(env.outputs["Color"], bg.inputs["Color"])
    nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
    mapping.inputs["Rotation"].default_value = (0.0, 0.0, rotation)
    bg.inputs["Strength"].default_value = strength
    world[WORLD_KEY] = sl.name


def use_as_world(context):
    """Build a World from the VR view's HDRI, rotation and strength, and make it the scene's, so Rendered
    and final renders get the same light. A world built before is rebuilt; another world it replaces gets
    a fake user, so it isn't lost on save."""
    sh = shading(context)
    sl = _studio('WORLD', hdri_name(context))
    if sl is None or not sl.path:
        return None
    world = our_world(context)
    if world is None:
        old = context.scene.world
        if old is not None:
            old.use_fake_user = True
        world = bpy.data.worlds.new(WORLD_NAME)
    _build_world(world, sl, sh.studiolight_rotate_z, sh.studiolight_intensity)
    context.scene.world = world
    return world


def _world_node(world, name):
    nt = world.node_tree if world is not None else None
    return nt.nodes.get(name) if nt is not None else None


def world_background(world):
    """Our Background node, or the first Background linked into the world's output."""
    node = _world_node(world, BACKGROUND)
    if node is not None or world is None or world.node_tree is None:
        return node
    for link in world.node_tree.links:
        if link.to_node.type == 'OUTPUT_WORLD' and link.from_node.type == 'BACKGROUND':
            return link.from_node
    return None


def world_value(context, what):
    """'rotation', 'strength' or 'hdri' of the scene's world (rotation and HDRI only for ours)."""
    world = context.scene.world
    if what == 'strength':
        bg = world_background(world)
        return bg.inputs["Strength"].default_value if bg is not None else None
    if world is None or not world.get(WORLD_KEY):
        return None
    if what == 'hdri':
        return world.get(WORLD_KEY)
    mapping = _world_node(world, MAPPING)
    return mapping.inputs["Rotation"].default_value[2] if mapping is not None else None


def set_world_value(context, what, value):
    """Set the world's rotation or strength; for our world, the VR view's HDRI follows, so the two match."""
    world = context.scene.world
    if what == 'strength':
        bg = world_background(world)
        if bg is not None:
            bg.inputs["Strength"].default_value = value
        if our_world(context) is not None:
            shading(context).studiolight_intensity = value
    elif what == 'rotation' and our_world(context) is not None:
        mapping = _world_node(world, MAPPING)
        if mapping is not None:
            mapping.inputs["Rotation"].default_value[2] = value
        shading(context).studiolight_rotate_z = value


def choose_hdri(context, name):
    """The HDRI picker's answer: the VR view's HDRI, and our world's too while the view shows the world."""
    set_studio(context, 'WORLD', name)
    world = our_world(context)
    if world is not None and scene_world_on(context):
        sl = _studio('WORLD', name)
        env = _world_node(world, ENVIRONMENT)
        if sl is not None and sl.path and env is not None:
            env.image = _hdri_image(sl)
            world[WORLD_KEY] = name


def in_world(context):
    """Whether the scene's world is ours and matches the VR view's HDRI, rotation and strength."""
    world = our_world(context)
    sh = shading(context)
    if world is None or sh.type not in {'MATERIAL', 'RENDERED'}:
        return False
    rotation, strength = world_value(context, 'rotation'), world_value(context, 'strength')
    return (world.get(WORLD_KEY) == sh.studio_light and rotation is not None and strength is not None and
            abs(rotation - sh.studiolight_rotate_z) < 1e-4 and abs(strength - sh.studiolight_intensity) < 1e-4)


def engine_name(context):
    engine = context.scene.render.engine
    return {'BLENDER_EEVEE': "EEVEE", 'BLENDER_EEVEE_NEXT': "EEVEE", 'CYCLES': "Cycles",
            'BLENDER_WORKBENCH': "Workbench"}.get(engine, engine.title())


# --- previews of studio lights, matcaps and HDRIs ---------------------------------------

PREVIEW_PX = 64
PREVIEWS_PER_FRAME = 2
_previews = {}  # (kind, name) -> Preview
_clock = [0.0, 0]
_errors = []


class Preview:
    """A studio light's, matcap's or HDRI's preview for a panel tile, made into a GPU texture on first draw (a
    few per frame). Hashable by (kind, name), so panels can compare widgets holding one."""

    def __init__(self, kind, name):
        self.key = (kind, name)
        self.pixels = None
        self.tex = None
        self.missing = False

    def __hash__(self):
        return hash(self.key)

    def __eq__(self, other):
        return isinstance(other, Preview) and other.key == self.key

    def compute(self):
        """The pixels (computed now if they weren't), or None."""
        if self.pixels is None and not self.missing:
            try:
                self.pixels = preview_pixels(*self.key)
            except Exception:
                if not _errors:
                    import traceback
                    traceback.print_exc()
                _errors.append(1)
                self.pixels = None
            self.missing = self.pixels is None
        return self.pixels

    def texture(self):
        if self.tex is not None or self.missing:
            return self.tex
        if self.pixels is None and not _budget():
            return None
        px = self.compute()
        if px is None:
            return None
        import gpu
        flat = np.ascontiguousarray(px, np.float32).ravel()
        self.tex = gpu.types.GPUTexture((px.shape[1], px.shape[0]), format='RGBA8',
                                        data=gpu.types.Buffer('FLOAT', flat.size, flat))
        return self.tex


def _budget():
    now = time.monotonic()
    if now - _clock[0] > 0.008:  # A new frame (both eyes draw within a few milliseconds).
        _clock[0], _clock[1] = now, 0
    if _clock[1] >= PREVIEWS_PER_FRAME:
        return False
    _clock[1] += 1
    return True


def preview(kind, name):
    """The tile image for a studio light, matcap or HDRI, or None (no preview: the tile shows an icon)."""
    p = _previews.get((kind, name))
    if p is None:
        p = _previews[(kind, name)] = Preview(kind, name)
    return None if p.missing else p


def prepare(kind, names):
    """Work out the previews of names now (a tiles page opening), so drawing only uploads them."""
    for name in names:
        p = preview(kind, name)
        if p is not None:
            p.compute()


def clear():
    _previews.clear()


def _srgb(c):
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1.0 / 2.4) - 0.055)


def _shrink(px, h, w):
    """px (H, W, C) at (h, w): box averages when the sizes divide, else nearest samples."""
    big_h, big_w = px.shape[:2]
    if big_h % h == 0 and big_w % w == 0:
        return px.reshape(h, big_h // h, w, big_w // w, -1).mean(axis=(1, 3))
    ys = ((np.arange(h) + 0.5) * big_h / h).astype(int)
    xs = ((np.arange(w) + 0.5) * big_w / w).astype(int)
    return px[ys][:, xs]


def _read(path, max_width=1024):
    """(float32 (H, W, C) top row first, channel names) of an image file, with OpenImageIO; (None, ())
    when it can't be read."""
    try:
        import OpenImageIO as oiio
    except ImportError:
        return None, ()
    buf = oiio.ImageBuf(path)
    if buf.has_error:
        return None, ()
    spec = buf.spec()
    if spec.width > max_width:  # A big user HDRI: shrink it before reading it all.
        h = max(int(spec.height * max_width / spec.width), 1)
        buf = oiio.ImageBufAlgo.resize(buf, roi=oiio.ROI(0, max_width, 0, h, 0, 1, 0, spec.nchannels))
    px = buf.get_pixels(oiio.FLOAT)
    if px is None or np.ndim(px) != 3:
        return None, ()
    return np.asarray(px, np.float32), tuple(spec.channelnames)


def _ball(n):
    """Unit-sphere normals seen from the front, n x n, bottom row first: (x, y, z, alpha)."""
    t = (np.arange(n) + 0.5) / n * 2.0 - 1.0
    x, y = np.meshgrid(t, t)
    r = np.sqrt(x * x + y * y)
    z = np.sqrt(np.clip(1.0 - r * r, 0.0, 1.0))
    alpha = np.clip((1.0 - r) * n / 2.0, 0.0, 1.0)  # A one-pixel soft edge.
    return x, y, z, alpha


def _matcap_pixels(px, names):
    """A matcap is a ball already: diffuse (on Solid's gray) plus specular, cut to its disc."""
    rgb = px[..., :3]
    if px.shape[2] >= 7 and any("specular" in n.lower() for n in names[4:7]):
        rgb = 0.8 * rgb + px[..., 4:7]
    small = _shrink(np.ascontiguousarray(rgb[::-1]), PREVIEW_PX, PREVIEW_PX)
    _x, _y, _z, alpha = _ball(PREVIEW_PX)
    return np.dstack([_srgb(small), alpha]).astype(np.float32)


def _world_pixels(px):
    """An HDRI as a mirror ball (the way Blender previews them), tone mapped."""
    env = px[..., :3]
    if env.shape[1] >= 512 and env.shape[1] % 256 == 0 and env.shape[0] % 128 == 0:
        env = _shrink(env, 128, 256)
    x, y, z, alpha = _ball(PREVIEW_PX)
    # The view ray (0, 0, -1) reflected off the ball, then from view space (looking along +Y, Z up) to world.
    rx, ry, rz = 2.0 * z * x, 2.0 * z * y, 2.0 * z * z - 1.0
    dx, dy, dz = rx, -rz, ry
    u = ((math.pi - np.arctan2(dy, dx)) / (2.0 * math.pi)) % 1.0
    v = 0.5 + np.arcsin(np.clip(dz, -1.0, 1.0)) / math.pi
    h, w = env.shape[:2]
    col = env[np.clip(((1.0 - v) * h).astype(int), 0, h - 1), np.clip((u * w).astype(int), 0, w - 1)]
    col = 1.0 - np.exp(-col)
    return np.dstack([_srgb(col), alpha]).astype(np.float32)


def _studio_pixels(sl):
    """A gray ball lit by a studio light's solid lights (wrapped diffuse plus a highlight), as Solid shades."""
    x, y, z, alpha = _ball(PREVIEW_PX)
    base = 0.8
    col = np.zeros(x.shape + (3,)) + np.array(sl.light_ambient)[None, None, :] * base
    for light in sl.solid_lights:
        if not light.use:
            continue
        d = Vector(light.direction)
        if d.length < 1e-6:
            continue
        d.normalize()
        wrap = light.smooth
        diffuse = np.clip((x * d.x + y * d.y + z * d.z + wrap) / (1.0 + wrap), 0.0, 1.0)
        col += base * diffuse[..., None] * np.array(light.diffuse_color)[None, None, :]
        half = (d + Vector((0.0, 0.0, 1.0))).normalized()
        spec = np.clip(x * half.x + y * half.y + z * half.z, 0.0, 1.0) ** (8.0 + 120.0 * (1.0 - wrap))
        col += spec[..., None] * np.array(light.specular_color)[None, None, :]
    return np.dstack([_srgb(col), alpha]).astype(np.float32)


def preview_pixels(kind, name):
    """(PREVIEW_PX, PREVIEW_PX, 4) float32 RGBA, bottom row first, as shown; None without one."""
    sl = _studio(kind, name)
    if sl is None:
        return None
    if kind == 'STUDIO':
        return _studio_pixels(sl)
    if not sl.path:
        return None
    px, names = _read(sl.path)
    if px is None:
        return None
    return _matcap_pixels(px, names) if kind == 'MATCAP' else _world_pixels(px)


# --- materials (Model mode's Material page) ----------------------------------------------

# (id, name, base color as shown or None to keep the object's, Principled inputs). Inputs a preset doesn't
# name go back to RESET, so presets don't leak into each other (glass, glow).
PRESETS = (
    ('PLASTIC', "Plastic", None, {"Roughness": 0.3}),
    ('RUBBER', "Rubber", None, {"Roughness": 0.85}),
    ('CLAY', "Clay", (0.72, 0.5, 0.4), {"Roughness": 0.95}),
    ('CERAMIC', "Ceramic", (0.93, 0.93, 0.9), {"Roughness": 0.1, "Coat Weight": 1.0}),
    ('METAL', "Metal", (0.8, 0.8, 0.8), {"Metallic": 1.0, "Roughness": 0.35}),
    ('CHROME', "Chrome", (0.97, 0.97, 0.97), {"Metallic": 1.0, "Roughness": 0.04}),
    ('GOLD', "Gold", (1.0, 0.86, 0.57), {"Metallic": 1.0, "Roughness": 0.2}),
    ('COPPER', "Copper", (0.98, 0.82, 0.76), {"Metallic": 1.0, "Roughness": 0.25}),
    ('GLASS', "Glass", (1.0, 1.0, 1.0), {"Roughness": 0.0, "Transmission Weight": 1.0, "IOR": 1.45}),
    ('EMISSIVE', "Emissive", None, {"Roughness": 0.5, "Emission Strength": 5.0}),
)
RESET = {"Metallic": 0.0, "Roughness": 0.5, "Transmission Weight": 0.0, "Emission Strength": 0.0,
         "Coat Weight": 0.0, "IOR": 1.5}
INPUTS = {'metallic': "Metallic", 'roughness': "Roughness", 'emission': "Emission Strength",
          'transmission': "Transmission Weight"}
GLASS_ALPHA = 0.7  # how see-through full transmission makes the viewport display color (Solid view)


def can_have_material(obj):
    return obj is not None and obj.data is not None and hasattr(obj.data, "materials")


def material_of(obj):
    """The object's first material, or None."""
    if not can_have_material(obj) or not obj.material_slots:
        return None
    return obj.material_slots[0].material


def principled(mat):
    """The Principled BSDF feeding the material's output (or its first one), or None."""
    nt = mat.node_tree if mat is not None else None
    if nt is None:
        return None
    for link in nt.links:
        if (link.to_node.type == 'OUTPUT_MATERIAL' and link.to_node.is_active_output and
                link.to_socket.name == "Surface" and link.from_node.type == 'BSDF_PRINCIPLED'):
            return link.from_node
    return next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)


def is_shared(obj):
    """Whether editing the first material would change other objects too: other users, a mesh it rides on
    that others share, or a material from a library."""
    mat = material_of(obj)
    if mat is None:
        return False
    users = mat.users - (1 if mat.use_fake_user else 0)
    slot = obj.material_slots[0]
    return users > 1 or not mat.is_editable or (slot.link == 'DATA' and obj.data.users > 1)


def _new_material(name):
    mat = bpy.data.materials.new(name or "Material")
    if mat.node_tree is None:
        mat.use_nodes = True
    sync_viewport(mat)
    return mat


def own_material(context, obj):
    """The object's first material, made its own to edit: a new Principled one if it has none, a copy if it's
    shared (like Stack's Apply on a shared mesh). A mesh other objects share keeps being shared: the copy goes
    in an object-linked slot."""
    if not can_have_material(obj):
        return None
    if not obj.material_slots:
        mat = _new_material(obj.name)
        obj.data.materials.append(mat)
        return mat
    slot = obj.material_slots[0]
    if slot.material is None:
        if slot.link == 'DATA' and obj.data.users > 1:
            slot.link = 'OBJECT'
        slot.material = _new_material(obj.name)
        return slot.material
    if is_shared(obj):
        copy = slot.material.copy()
        if slot.link == 'DATA' and obj.data.users > 1:
            slot.link = 'OBJECT'
        slot.material = copy
    return slot.material


def assign_material(context, obj, name):
    """The materials picker's answer: that material in the first slot ("" a new one)."""
    if not can_have_material(obj):
        return None
    mat = bpy.data.materials.get(name) if name else _new_material(obj.name)
    if mat is None:
        return None
    if not obj.material_slots:
        obj.data.materials.append(mat)
        return mat
    slot = obj.material_slots[0]
    if slot.link == 'DATA' and obj.data.users > 1:
        slot.link = 'OBJECT'  # Only this object changes, as the page promises.
    slot.material = mat
    return mat


def material_names():
    return sorted(m.name for m in bpy.data.materials if not m.is_grease_pencil)


def base_linked(mat):
    """Whether the base color comes from a node (a painted texture): the color swatches can't change it."""
    bsdf = principled(mat)
    base = bsdf.inputs.get("Base Color") if bsdf is not None else None
    return base is not None and base.is_linked


def material_value(obj, what):
    """'color' (as shown), 'metallic', 'roughness', 'emission' or 'transmission' of the first material; the
    defaults of a new material without one."""
    bsdf = principled(material_of(obj))
    if what == 'color':
        if bsdf is None:
            return to_display((0.8, 0.8, 0.8))
        return to_display(bsdf.inputs["Base Color"].default_value[:3])
    if bsdf is None:
        return RESET.get(INPUTS[what], 0.0)
    inp = bsdf.inputs.get(INPUTS[what])
    return inp.default_value if inp is not None else 0.0


def _set_emission_color(bsdf):
    """A glowing material glows in its own base color."""
    em = bsdf.inputs.get("Emission Color")
    if em is not None and not em.is_linked:
        em.default_value = tuple(bsdf.inputs["Base Color"].default_value)


def _glass(mat, transmission):
    mat.use_raytrace_refraction = transmission > 0.0  # EEVEE refracts through it.


def material_set(context, obj, what, value):
    """Set a value of the first material (made the object's own first), keeping the viewport display in sync."""
    mat = own_material(context, obj)
    bsdf = principled(mat)
    if bsdf is None:
        return
    if what == 'color':
        base = bsdf.inputs["Base Color"]
        if not base.is_linked:
            base.default_value = (*to_linear(value), 1.0)
    else:
        inp = bsdf.inputs.get(INPUTS[what])
        if inp is not None and not inp.is_linked:
            inp.default_value = value
        if what == 'transmission':
            _glass(mat, value)
    if material_value(obj, 'emission') > 0.0:
        _set_emission_color(bsdf)
    sync_viewport(mat, bsdf)


def apply_preset(context, obj, ident):
    entry = next((p for p in PRESETS if p[0] == ident), None)
    if entry is None:
        return
    mat = own_material(context, obj)
    bsdf = principled(mat)
    if bsdf is None:
        return
    values = dict(RESET)
    values.update(entry[3])
    for name, value in values.items():
        inp = bsdf.inputs.get(name)
        if inp is not None and not inp.is_linked:
            inp.default_value = value
    base = bsdf.inputs["Base Color"]
    if entry[2] is not None and not base.is_linked:
        base.default_value = (*to_linear(entry[2]), 1.0)
    if values["Emission Strength"] > 0.0:
        _set_emission_color(bsdf)
    _glass(mat, values["Transmission Weight"])
    sync_viewport(mat, bsdf)


def preset_name(mat):
    """The preset the material's values match, "Custom" or "-" (no Principled BSDF)."""
    bsdf = principled(mat)
    if bsdf is None:
        return "-"
    color = to_display(bsdf.inputs["Base Color"].default_value[:3])
    for _ident, name, rgb, inputs in PRESETS:
        values = dict(RESET)
        values.update(inputs)
        same = all(bsdf.inputs.get(k) is None or abs(bsdf.inputs[k].default_value - v) < 1e-3
                   for k, v in values.items())
        if same and (rgb is None or all(abs(a - b) < 2e-3 for a, b in zip(color, rgb))):
            return name
    return "Custom"


def sync_viewport(mat, bsdf=None):
    """The viewport display color, metallic and roughness from the Principled BSDF, so Solid shading (and the
    headset in Solid) shows the material; glass is see-through there too."""
    bsdf = bsdf or principled(mat)
    if bsdf is None:
        return
    base = bsdf.inputs.get("Base Color")
    rgb = tuple(base.default_value[:3]) if base is not None and not base.is_linked else tuple(mat.diffuse_color[:3])
    t = bsdf.inputs.get("Transmission Weight")
    t = t.default_value if t is not None else 0.0
    mat.diffuse_color = (*rgb, 1.0 - GLASS_ALPHA * min(max(t, 0.0), 1.0))
    mat.metallic = bsdf.inputs["Metallic"].default_value
    mat.roughness = bsdf.inputs["Roughness"].default_value


def register():
    pass


def unregister():
    clear()  # GPU textures go before Blender's GPU teardown.
