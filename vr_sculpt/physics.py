"""Physics mode: set up Blender's simulations and spring bones, and play with them by hand.

Objects (the selected mesh): Off, Rigid (an active rigid body), Static (a passive rigid body), Cloth
or Soft, each with its main settings. Soft is a closed cloth held out by internal springs: Blender's
Soft Body modifier took 260 ms a frame for a 642 vertex ball resting on a table (and sank through
it), the cloth takes 2. One made with the Soft Body modifier still reads as Soft (choosing Soft remakes
it). Rigid and Static bodies also get a Collision modifier, so cloth and soft things land on them.
It's all Blender's own simulation data (rigid body, Cloth / Collision modifiers), so the desktop's
Physics tab shows the same thing.
Simulate plays the timeline (every frame: the point caches only step frame by frame); Keep bakes
what the caches hold, so a run played with the hands plays back the same.

Spring bones (an armature): a bone with a spring keeps its animated pose as a target and its tail
is a point mass on a damped spring toward it, so it lags, overshoots and settles when its parent
moves, and gravity pulls it. Simulated here in the app timer (live.py) while VR runs or the
timeline plays, in real time, and written as the bone's local (basis) matrix on top of its pose.
What a bone was posed at is told apart from what was written here last (the base), so keyed
animation, a desktop pose and Pose mode all keep working under it. Bake keys the frame range and
turns those springs off.

Hands (togglable): the controller tips are kinematic sphere colliders ("VR Hand Collider L / R",
a passive animated rigid body plus a Collision modifier, unselectable wire spheres: a hidden object
isn't evaluated, so its shape would come out empty) that push bodies, cloth and soft bodies,
and they push spring bones too. The trigger then grabs (a rigid body turns kinematic while held and
keeps the hand's velocity when let go; a spring bone's tail follows the hand) or, with Push / Pull,
drives a force field at the hand ("VR Hand Force L / R"): the main trigger pulls, the off hand's pushes.

Probed in tests/probe_physics.py: a body turned kinematic, carried and released mid-run keeps
flying (Bullet keeps the kinematic velocity), and putting its own transform back afterwards keeps
the start of the run as it was; a field made by
forcefield_toggle only acts once its object is linked (the link rebuilds the scene's relations).
"""

import math
import time

import bmesh
import bpy
import gpu
from bpy.props import BoolProperty, FloatProperty, PointerProperty
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector

from . import anim, primitives
from . import pose as pose_mode
from .pose import find_rig

KINDS = (('NONE', "Off"), ('RIGID', "Rigid"), ('STATIC', "Static"), ('CLOTH', "Cloth"), ('SOFT', "Soft"))
SHAPES = (('CONVEX_HULL', "Hull"), ('BOX', "Box"), ('SPHERE', "Sphere"), ('MESH', "Mesh"))
SOFT_STIFFNESS = 0.5  # A new Soft's (0-1).
SOFT_SPRINGS = (0.05, 40.0)  # Stiffness 0-1 -> internal springs of 0.05 * 40 ** stiffness (0.05 to 2).

# Blender's cloth presets (scripts/presets/cloth), the settings that differ between them.
CLOTH_PRESETS = {
    "Cotton": dict(quality=5, mass=0.3, tension_stiffness=15.0, compression_stiffness=15.0, shear_stiffness=15.0,
                   bending_stiffness=0.5, tension_damping=5.0, compression_damping=5.0, shear_damping=5.0),
    "Denim": dict(quality=12, mass=1.0, tension_stiffness=40.0, compression_stiffness=40.0, shear_stiffness=40.0,
                  bending_stiffness=10.0, tension_damping=25.0, compression_damping=25.0, shear_damping=25.0),
    "Leather": dict(quality=15, mass=0.4, tension_stiffness=80.0, compression_stiffness=80.0,
                    shear_stiffness=80.0, bending_stiffness=150.0, tension_damping=25.0, compression_damping=25.0,
                    shear_damping=25.0),
    "Rubber": dict(quality=7, mass=3.0, tension_stiffness=15.0, compression_stiffness=15.0, shear_stiffness=15.0,
                   bending_stiffness=25.0, tension_damping=25.0, compression_damping=25.0, shear_damping=25.0),
    "Silk": dict(quality=5, mass=0.15, tension_stiffness=5.0, compression_stiffness=5.0, shear_stiffness=5.0,
                 bending_stiffness=0.05, tension_damping=0.0, compression_damping=0.0, shear_damping=0.0),
}
PIN_GROUP = "VR Pin"
PIN_TOP = "*top"  # the pin picker's "Top edge": makes PIN_GROUP from the highest vertices
PIN_SHARE = 0.03  # share of the mesh's height, from the top, that Top edge pins

COLLECTION = "VR Physics"
HAND_NAMES = ("VR Hand Collider L", "VR Hand Collider R")
FIELD_NAMES = ("VR Hand Force L", "VR Hand Force R")
HAND_MESH = "VR Hand Collider"
HAND_RADIUS = 0.03  # hand-space meters
FORCE_REACH = 0.6  # hand-space meters: the hand's force acts this far
REACH = 0.02  # hand-space meters around a bone that still count as touching it

# Spring bones.
SUBSTEP = 1.0 / 240.0  # seconds; the spring is stepped this finely (stiff springs stay stable)
MAX_DT = 1.0 / 20.0  # a longer gap between timer ticks (a hitch) is simulated as this long
MIN_HZ, MAX_HZ = 0.5, 8.0  # the spring's frequency at stiffness 0 and 1
SCAN_SECONDS = 0.5  # which bones have springs is looked up again this often
SAME = 1e-5  # a basis this close to the one written last is still ours

SPRING_COLOR = (0.45, 0.95, 0.75, 0.9)
FORCE_COLORS = ((1.0, 0.55, 0.3), (0.4, 0.75, 1.0))  # push (left), pull (right)


class VRSpring(bpy.types.PropertyGroup):
    """A pose bone's spring (Physics mode)."""
    enabled: BoolProperty(
        name="Spring", description="The bone lags, sways and settles when it moves, simulated live in VR and "
                                   "while the timeline plays")
    stiffness: FloatProperty(
        name="Stiffness", description="How fast the bone springs back to its pose (0: floppy, 1: tight)",
        default=0.5, min=0.0, max=1.0, subtype='FACTOR')
    damping: FloatProperty(
        name="Damping", description="How fast its swaying dies down (1: no overshoot)",
        default=0.25, min=0.0, max=1.0, subtype='FACTOR')
    gravity: FloatProperty(
        name="Gravity", description="How much the scene's gravity pulls the bone's tail",
        default=0.3, min=0.0, soft_max=2.0, subtype='FACTOR')


# --- what an object is ---------------------------------------------------------------

def _modifier(obj, kind):
    return next((m for m in obj.modifiers if m.type == kind), None) if obj is not None else None


def kind(obj):
    """'NONE', 'RIGID', 'STATIC', 'CLOTH' or 'SOFT' (a cloth with internal springs, or a Soft Body)."""
    if obj is None or obj.type != 'MESH':
        return 'NONE'
    mod = _modifier(obj, 'CLOTH')
    if mod is not None:
        return 'SOFT' if mod.settings.use_internal_springs else 'CLOTH'
    if old_soft(obj):
        return 'SOFT'
    rb = obj.rigid_body
    if rb is not None:
        return 'RIGID' if rb.type == 'ACTIVE' else 'STATIC'
    if _modifier(obj, 'COLLISION') is not None:
        return 'STATIC'
    return 'NONE'


def old_soft(obj):
    """A Soft Body modifier soft body (made on the desktop): slow, so choosing Soft remakes it as ours."""
    return _modifier(obj, 'SOFT_BODY') is not None


def _override(context, obj):
    return dict(scene=context.scene, object=obj, active_object=obj, selected_objects=[obj],
                selected_editable_objects=[obj])


def _add_rigid(context, obj, rb_type):
    if obj.rigid_body is None:
        context.view_layer.update()  # The shape is made from the evaluated mesh: a new object has none yet.
        with context.temp_override(**_override(context, obj)):
            bpy.ops.rigidbody.object_add(type=rb_type)
    obj.rigid_body.type = rb_type


def _remove_rigid(context, obj):
    if obj.rigid_body is not None:
        with context.temp_override(**_override(context, obj)):
            bpy.ops.rigidbody.object_remove()


def _ensure_modifier(obj, kind_, name):
    mod = _modifier(obj, kind_)
    return mod if mod is not None else obj.modifiers.new(name, kind_)


def _remove_modifiers(obj, kinds):
    for mod in [m for m in obj.modifiers if m.type in kinds]:
        obj.modifiers.remove(mod)


def set_kind(context, obj, new):
    """Make obj a rigid body, a static collider, cloth, a soft body, or nothing (removing the others)."""
    if obj is None or obj.type != 'MESH' or (kind(obj) == new and not old_soft(obj)):
        return
    if new not in {'RIGID', 'STATIC'}:
        _remove_rigid(context, obj)
    _remove_modifiers(obj, {'CLOTH', 'SOFT_BODY'})  # Cloth <-> Soft too: a new cloth, with its own defaults.
    if new not in {'RIGID', 'STATIC'}:
        _remove_modifiers(obj, {'COLLISION'})
    if new in {'RIGID', 'STATIC'}:
        # Blender's convex hull shape for both: things resting on a Mesh shape jitter and slide (Shape > Mesh
        # is there for hollow obstacles).
        _add_rigid(context, obj, 'ACTIVE' if new == 'RIGID' else 'PASSIVE')
        _ensure_modifier(obj, 'COLLISION', "Collision")
    elif new in {'CLOTH', 'SOFT'}:
        mod = _ensure_modifier(obj, 'CLOTH', "Cloth" if new == 'CLOTH' else "Soft")
        mod.collision_settings.use_collision = True
        if new == 'SOFT':
            mod.settings.use_internal_springs = True
            set_soft_stiffness(obj, SOFT_STIFFNESS)
    _cover_range(context)


# --- object settings ---------------------------------------------------------------

def cloth(obj):
    mod = _modifier(obj, 'CLOTH')
    return mod.settings if mod is not None else None


def cloth_collision(obj):
    mod = _modifier(obj, 'CLOTH')
    return mod.collision_settings if mod is not None else None


def cloth_preset(obj):
    """The name of the preset the cloth's settings match, or "Custom"."""
    s = cloth(obj)
    if s is None:
        return ""
    for name, values in CLOTH_PRESETS.items():
        if all(abs(getattr(s, k) - v) < 1e-3 for k, v in values.items()):
            return name
    return "Custom"


def set_cloth_preset(obj, name):
    s = cloth(obj)
    if s is None or name not in CLOTH_PRESETS:
        return
    for k, v in CLOTH_PRESETS[name].items():
        setattr(s, k, v)


def cloth_stiffness(obj):
    s = cloth(obj)
    return s.tension_stiffness if s is not None else 0.0


def set_cloth_stiffness(obj, value):
    s = cloth(obj)
    if s is not None:
        s.tension_stiffness = s.compression_stiffness = s.shear_stiffness = value


def cloth_pressure(obj):
    s = cloth(obj)
    return s.uniform_pressure_force if s is not None and s.use_pressure else 0.0


def set_cloth_pressure(obj, value):
    """Air pressure inside a closed cloth (a balloon); 0 turns it off."""
    s = cloth(obj)
    if s is not None:
        s.use_pressure = value > 1e-4
        s.uniform_pressure_force = value


def pin_options(obj):
    """[(value, label)] for the cloth's pin picker."""
    out = [("", "None"), (PIN_TOP, "Top edge")]
    if obj is not None:
        out += [(g.name, g.name) for g in obj.vertex_groups]
    return out


def pin(obj):
    s = cloth(obj)
    return s.vertex_group_mass if s is not None else ""


def set_pin(obj, value):
    """Pin the cloth to a vertex group ("" for none); PIN_TOP makes PIN_GROUP from its highest vertices
    (in world space, as it stands now) first."""
    s = cloth(obj)
    if s is None:
        return
    if value == PIN_TOP:
        mw = obj.matrix_world
        zs = [(mw @ v.co).z for v in obj.data.vertices]
        if not zs:
            return
        top, low = max(zs), min(zs)
        cut = top - max(top - low, 1e-6) * PIN_SHARE
        group = obj.vertex_groups.get(PIN_GROUP) or obj.vertex_groups.new(name=PIN_GROUP)
        group.remove(list(range(len(zs))))
        group.add([i for i, z in enumerate(zs) if z >= cut], 1.0, 'REPLACE')
        value = group.name
    s.vertex_group_mass = value


def soft_stiffness(obj):
    """0-1: the internal springs on a log scale (a Soft Body's pull)."""
    s = cloth(obj)
    if s is not None:
        low, span = SOFT_SPRINGS
        return min(max(math.log(max(s.internal_tension_stiffness, 1e-6) / low) / math.log(span), 0.0), 1.0)
    s = obj.soft_body if old_soft(obj) else None
    return s.pull if s is not None else 0.0


def set_soft_stiffness(obj, value):
    """The springs across the inside, and the skin's: three times as stiff, bending as much as the inside."""
    s = cloth(obj)
    if s is not None:
        low, span = SOFT_SPRINGS
        k = low * span ** value
        s.internal_tension_stiffness = s.internal_compression_stiffness = k
        s.tension_stiffness = s.compression_stiffness = s.shear_stiffness = 3.0 * k
        s.bending_stiffness = k
    elif old_soft(obj):
        obj.soft_body.pull = obj.soft_body.push = value


# --- the world -----------------------------------------------------------------------

G = 9.81


def gravity(context):
    """The scene's gravity in g."""
    scene = context.scene
    return scene.gravity.length / G if scene.use_gravity else 0.0


def set_gravity(context, value):
    scene = context.scene
    direction = scene.gravity.normalized() if scene.gravity.length > 1e-6 else Vector((0.0, 0.0, -1.0))
    scene.gravity = direction * (value * G)
    scene.use_gravity = True


def speed(context):
    world = context.scene.rigidbody_world
    if world is not None:
        return world.time_scale
    for obj in context.scene.objects:
        s = cloth(obj)
        if s is not None:
            return s.time_scale
    return 1.0


def set_speed(context, value):
    """Simulation speed: the rigid body world's time scale, cloth's (and Soft Bodies') speed."""
    world = context.scene.rigidbody_world
    if world is not None:
        world.time_scale = value
    for obj in context.scene.objects:
        s = cloth(obj)
        if s is not None:
            s.time_scale = value
        if old_soft(obj):
            obj.soft_body.speed = value


def caches(context):
    """Every simulation point cache in the scene: the rigid body world's, cloth's and soft bodies'."""
    out = []
    world = context.scene.rigidbody_world
    if world is not None and world.point_cache is not None:
        out.append(world.point_cache)
    for obj in context.scene.objects:
        for mod in getattr(obj, "modifiers", ()):
            if mod.type in {'CLOTH', 'SOFT_BODY'}:
                out.append(mod.point_cache)
    return out


def fps(scene):
    return scene.render.fps / max(scene.render.fps_base, 1e-6)


def length(context):
    """The simulated range in seconds: the scene's frame range."""
    scene = context.scene
    return (scene.frame_end - scene.frame_start + 1) / fps(scene)


def set_length(context, seconds):
    scene = context.scene
    scene.frame_end = max(scene.frame_start + int(round(seconds * fps(scene))) - 1, scene.frame_start + 1)
    _cover_range(context)


def _cover_range(context):
    """The caches simulate the scene's whole frame range (theirs is 1-250 when made)."""
    scene = context.scene
    for cache in caches(context):
        if cache.is_baked:
            continue
        if cache.frame_start != scene.frame_start:
            cache.frame_start = scene.frame_start
        if cache.frame_end != scene.frame_end:
            cache.frame_end = scene.frame_end


def baked(context):
    return any(c.is_baked for c in caches(context))


def simulate(context):
    """Play (from the start, if at the end) or stop. Every frame is played: a cache steps only frame by
    frame, so a dropped frame would stop the simulation."""
    scene = context.scene
    if anim.is_playing(context):
        return anim.toggle_play(context)
    _cover_range(context)
    scene.sync_mode = 'NONE'
    if scene.frame_current >= scene.frame_end or scene.frame_current < scene.frame_start:
        scene.frame_set(scene.frame_start)
    return anim.toggle_play(context)


def reset(context):
    """Stop, and back to the first frame: the simulation starts over."""
    if anim.is_playing(context):
        anim.toggle_play(context)
    scene = context.scene
    scene.frame_set(scene.frame_start)
    reset_springs(context)


def keep(context):
    """Bake what the caches hold (the run just played, hands and all), so it plays back the same."""
    if anim.is_playing(context):
        anim.toggle_play(context)
    kept = 0
    for cache in caches(context):
        if cache.is_baked:
            continue
        try:
            with context.temp_override(scene=context.scene, point_cache=cache):
                bpy.ops.ptcache.bake_from_cache()
            kept += 1
        except RuntimeError as ex:
            print("VR Physics: could not keep a cache:", ex, flush=True)
    return kept


def free(context):
    """Throw the kept run away: the simulation runs (and reacts) again."""
    with context.temp_override(scene=context.scene):
        bpy.ops.ptcache.free_bake_all()


# --- hands ---------------------------------------------------------------------------

_hands = {"tips": [None, None], "velocity": [Vector(), Vector()], "time": 0.0, "force": [0.0, 0.0]}


def hands_wanted(context):
    from . import operators
    settings = context.scene.vr_sculpt
    return settings.mode == 'PHYSICS' and settings.physics_hands and operators.session_running(context)


def _collection(context):
    coll = bpy.data.collections.get(COLLECTION)
    if coll is None:
        coll = bpy.data.collections.new(COLLECTION)
        coll.hide_render = True
    if coll.name not in context.scene.collection.children:
        context.scene.collection.children.link(coll)
    return coll


def _hand_mesh(radius):
    """The colliders' sphere, its real size in the mesh: a rigid body's shape is made once, from the object
    as it is then, so the size can't come from the object's scale (it was a 1 m ball until the hand first
    moved, and threw what it touched)."""
    me = bpy.data.meshes.get(HAND_MESH)
    if me is not None and abs(me.get("radius", 0.0) - radius) > radius * 0.02:
        for name in HAND_NAMES:  # A new size: new colliders.
            obj = bpy.data.objects.get(name)
            if obj is not None:
                bpy.data.objects.remove(obj)
        bpy.data.meshes.remove(me)
        me = None
    if me is None:
        me = bpy.data.meshes.new(HAND_MESH)
        bm = bmesh.new()
        bmesh.ops.create_icosphere(bm, subdivisions=2, radius=radius)
        bm.to_mesh(me)
        bm.free()
        me["radius"] = radius
    return me


def _hand(context, i, tip, radius):
    """Hand i's collider, made the first time (at the tip, at its size)."""
    me = _hand_mesh(radius)
    obj = bpy.data.objects.get(HAND_NAMES[i])
    if obj is None:
        obj = bpy.data.objects.new(HAND_NAMES[i], me)
        obj.display_type = 'WIRE'
        obj.hide_render = True
        obj.hide_select = True  # Not something to touch (the tip is always inside it).
        obj.matrix_world = Matrix.Translation(tip)
        _collection(context).objects.link(obj)
        col = obj.modifiers.new("Collision", 'COLLISION')
        col.settings.thickness_outer = 0.005
    if context.scene.rigidbody_world is not None and obj.rigid_body is None:
        # Only once there's a rigid body world (a hand alone mustn't make one).
        obj.matrix_world = Matrix.Translation(tip)
        _add_rigid(context, obj, 'PASSIVE')
        rb = obj.rigid_body
        rb.kinematic = True
        rb.collision_shape = 'SPHERE'
        rb.friction = 1.0
    return obj


def _field(context, i):
    """Hand i's force field (an empty), made the first time. The field is made before the empty is
    linked: the link is what makes the simulations see it."""
    obj = bpy.data.objects.get(FIELD_NAMES[i])
    if obj is None:
        obj = bpy.data.objects.new(FIELD_NAMES[i], None)
        obj.empty_display_size = 0.02
        with context.temp_override(object=obj, active_object=obj):
            bpy.ops.object.forcefield_toggle()
        f = obj.field
        f.type = 'FORCE'
        f.shape = 'POINT'
        f.strength = 0.0
        f.falloff_power = 0.0
        f.use_max_distance = True
        obj.hide_select = True
        _collection(context).objects.link(obj)
        obj.hide_set(True)  # A hidden field still acts; shown, Blender draws its reach as a big sphere.
    return obj


def remove_hands(context=None):
    """The hand colliders and fields go (Hands off, another mode, the session over)."""
    for name in HAND_NAMES + FIELD_NAMES:
        obj = bpy.data.objects.get(name)
        if obj is not None:
            bpy.data.objects.remove(obj)
    me = bpy.data.meshes.get(HAND_MESH)
    if me is not None and me.users == 0:
        bpy.data.meshes.remove(me)
    coll = bpy.data.collections.get(COLLECTION)
    if coll is not None and not coll.all_objects:
        bpy.data.collections.remove(coll)
    _hands.update(tips=[None, None], velocity=[Vector(), Vector()], force=[0.0, 0.0])


def hand_tips():
    """[(tip, velocity)] of the hands that take part right now."""
    return [(t, v) for t, v in zip(_hands["tips"], _hands["velocity"]) if t is not None]


def sync_hands(context, ss):
    """Move the colliders and fields to the controller tips (or remove them when Hands is off)."""
    if not hands_wanted(context) or ss is None:
        if any(bpy.data.objects.get(n) is not None for n in HAND_NAMES + FIELD_NAMES):
            remove_hands(context)
        _hands["tips"] = [None, None]
        return
    from . import operators
    settings = context.scene.vr_sculpt
    scale = ss.viewer_scale
    now = time.monotonic()
    dt = now - _hands["time"]
    _hands["time"] = now
    r = HAND_RADIUS * scale
    for i in range(2):
        pose = operators.controller_pose(ss.controller_aim_location_get(context, i),
                                         ss.controller_aim_rotation_get(context, i))
        tip = operators.brush_tip(pose, settings.tip_offset * scale)
        old = _hands["tips"][i]
        if old is not None and 1e-4 < dt < 0.2:
            _hands["velocity"][i] = (tip - old) / dt
        _hands["tips"][i] = tip
        hand = _hand(context, i, tip, r)
        if hand.matrix_world.translation != tip:
            hand.matrix_world = Matrix.Translation(tip)
        field = _field(context, i)
        if field.matrix_world.translation != tip:
            field.matrix_world = Matrix.Translation(tip)
        f = field.field
        reach = FORCE_REACH * scale
        if abs(f.distance_max - reach) > 1e-6:
            f.distance_max = reach
        strength = _hands["force"][i]
        if f.strength != strength:
            f.strength = strength


# --- spring bones --------------------------------------------------------------------

# (rig, bone) -> [tail, velocity] in world space.
_sim = {}
# (rig, bone) -> (basis written last, the base it was written over).
_written = {}
# (rig, bone) -> [target point (world) or None, last target, velocity]: tails held by a hand.
_held = {}
_scan = {"time": -1.0, "rigs": {}}  # rig name -> spring bone names, parents first
_clock = {"time": None, "running": False}


def has_spring(pb):
    """Whether the bone's spring is on. Reading pb.vr_spring would give every bone it's asked of its own
    (saved) settings, so the stored ones are looked at instead."""
    props = pb.bl_system_properties_get()
    group = props.get("vr_spring") if props is not None else None
    return bool(group is not None and group.get("enabled", False))


def springs(arm):
    """The rig's spring bones (pose bones), parents first."""
    if arm is None or arm.type != 'ARMATURE':
        return []
    return [pb for pb in sorted(arm.pose.bones, key=_depth) if has_spring(pb)]


def _depth(pb):
    d, p = 0, pb.parent
    while p is not None:
        d, p = d + 1, p.parent
    return d


def spring_rigs(context, refresh=False):
    """{rig name: ([spring bone names], [the bones they move, parents first])} of the scene's armatures
    with springs (looked up every SCAN_SECONDS)."""
    now = time.monotonic()
    if refresh or now - _scan["time"] > SCAN_SECONDS:
        rigs = {}
        for obj in context.scene.objects:
            if obj.type == 'ARMATURE':
                names = [pb.name for pb in springs(obj)]
                if names:
                    rigs[obj.name] = (names, [pb.name for pb in _affected(obj, names)])
        _scan.update(time=now, rigs=rigs)
    return _scan["rigs"]


def springs_changed():
    _scan["time"] = -1.0


def _same(a, b):
    return all(abs(a[i][j] - b[i][j]) < SAME for i in range(4) for j in range(4))


def base_basis(arm, pb):
    """The bone's own pose under its spring: its basis, unless that's what was written here last."""
    key = (arm.name, pb.name)
    current = pb.matrix_basis
    w = _written.get(key)
    if w is not None and _same(current, w[0]):
        return w[1].copy()
    _written.pop(key, None)
    return current.copy()


def _spring_k(stiffness):
    w = 2.0 * math.pi * (MIN_HZ + (MAX_HZ - MIN_HZ) * stiffness)
    return w * w, w


def _affected(arm, names):
    """The spring bones and their descendants (whose matrices move with them), parents first."""
    spring = set(names)
    out = []
    for pb in sorted(arm.pose.bones, key=_depth):
        p = pb
        while p is not None and p.name not in spring:
            p = p.parent
        if p is not None:
            out.append(pb)
    return out


def snapshot(arm, names, order=None):
    """What one step needs from the rig: each affected bone's base basis, and the matrices of the
    parents above them (which the springs don't move). order: _affected's names, if known."""
    bases, parents = {}, {}
    spring = set(names)
    bones = arm.pose.bones
    affected = _affected(arm, names) if order is None else [bones[n] for n in order if n in bones]
    inside = {pb.name for pb in affected}
    for pb in affected:
        bases[pb.name] = base_basis(arm, pb) if pb.name in spring else pb.matrix_basis.copy()
        if pb.parent is not None and pb.parent.name not in inside:
            parents[pb.name] = pb.parent.matrix.copy()
    return {"world": arm.matrix_world.copy(), "bases": bases, "parents": parents, "order": [pb.name for pb in affected],
            "springs": spring}


def step(context, arm, snap, dt, hands=(), radius=0.0):
    """Advance the rig's springs by dt seconds from snap. Returns {bone name: (new basis, base)} for
    the spring bones. hands: [(tip, velocity)] in world space that push the tails (radius each)."""
    scene = context.scene
    g = scene.gravity if scene.use_gravity else Vector()
    mw = snap["world"]
    mwi = mw.inverted_safe()
    news, out = {}, {}
    n = max(int(math.ceil(dt / SUBSTEP)), 1)
    h = dt / n
    for name in snap["order"]:
        pb = arm.pose.bones.get(name)
        if pb is None:
            continue
        parent = news.get(pb.parent.name) if pb.parent is not None else None
        if parent is None and pb.parent is not None:
            parent = snap["parents"].get(name, pb.parent.matrix)
        base = snap["bases"][name]
        target = pose_mode.pose_for(pb, base, parent)
        if name not in snap["springs"]:
            news[name] = target
            continue
        sp = pb.vr_spring
        head = mw @ target.translation
        tail = mw @ (target @ Vector((0.0, pb.bone.length, 0.0)))
        length = (tail - head).length
        key = (arm.name, name)
        state = _sim.get(key)
        if state is None or length < 1e-6:
            state = _sim[key] = [tail.copy(), Vector()]
        p, v = state
        k, w = _spring_k(sp.stiffness)
        c = 2.0 * sp.damping * w
        held = _held.get(key)
        for _i in range(n):
            if held is not None and held[0] is not None:
                p = held[0].copy()
                v = held[2].copy()
            else:
                a = (tail - p) * k - v * c + g * sp.gravity
                v = v + a * h
                p = p + v * h
            for tip, hv in hands:
                d = p - tip
                dist = d.length
                if dist < radius:
                    normal = d / dist if dist > 1e-9 else (p - head).normalized()
                    p = tip + normal * radius
                    into = v.dot(normal) - hv.dot(normal)
                    if into < 0.0:
                        v = v - normal * into  # Moves with the hand along the push.
            r = p - head
            if r.length > 1e-9 and length > 1e-9:
                p = head + r * (length / r.length)
                v = v - r.normalized() * v.dot(r.normalized())  # Only swinging, no stretching.
        state[0], state[1] = p, v
        want = (mwi @ p) - target.translation
        has = target.to_3x3() @ Vector((0.0, 1.0, 0.0))
        if want.length < 1e-9 or has.length < 1e-9:
            news[name] = target
            out[name] = (pose_mode.basis_for(pb, target, parent), base)
            continue
        swing = has.rotation_difference(want).to_matrix().to_4x4()
        pivot = target.translation
        new = Matrix.Translation(pivot) @ swing @ Matrix.Translation(-pivot) @ target
        news[name] = new
        out[name] = (pose_mode.basis_for(pb, new, parent), base)
    return out


def write(arm, results):
    """Set the spring bones' bases. One that wouldn't change isn't written: a settled spring then costs
    the rig (and the meshes it deforms) no evaluation."""
    for name, (basis, base) in results.items():
        pb = arm.pose.bones.get(name)
        if pb is None:
            continue
        current = pb.matrix_basis.copy()  # A copy: the property's matrix follows the bone.
        if not _same(current, basis):
            pb.matrix_basis = basis
            current = pb.matrix_basis.copy()
        _written[(arm.name, name)] = (current, base)


def tick_springs(context, hands=()):
    """App timer: step every rig's springs by the time since the last tick, in real time."""
    now = time.monotonic()
    last = _clock["time"]
    _clock.update(time=now, running=True)
    if last is None:
        return
    dt = min(now - last, MAX_DT)
    if dt <= 0.0:
        return
    ss = context.window_manager.xr_session_state
    scale = ss.viewer_scale if ss is not None else 1.0
    for name, (bones, order) in spring_rigs(context).items():
        arm = context.scene.objects.get(name)
        if arm is None or arm.mode == 'EDIT':
            continue
        write(arm, step(context, arm, snapshot(arm, bones, order), dt, hands, HAND_RADIUS * scale))


def reset_springs(context):
    """Springs at rest: every spring bone back on its own pose, the motion forgotten."""
    for (rig, bone), (written, base) in list(_written.items()):
        arm = bpy.data.objects.get(rig)
        pb = arm.pose.bones.get(bone) if arm is not None and arm.type == 'ARMATURE' else None
        if pb is not None and _same(pb.matrix_basis, written):
            pb.matrix_basis = base
    _written.clear()
    _sim.clear()
    _held.clear()
    _clock.update(time=None, running=False)


def springs_live(context):
    """Whether the springs are being simulated: in VR, or while the timeline plays."""
    from . import operators
    return (operators.session_running(context) or anim.is_playing(context)) and bool(spring_rigs(context))


def bake(context, arm):
    """Key the rig's spring bones over the frame range, simulated at the scene's frame rate (no hands),
    then turn their springs off: the keys hold the motion now. Returns the frames keyed."""
    scene = context.scene
    pbs = springs(arm)
    if not pbs:
        return 0
    names = [pb.name for pb in pbs]
    if anim.is_playing(context):
        anim.toggle_play(context)
    reset_springs(context)
    current = scene.frame_current
    frames = range(scene.frame_start, scene.frame_end + 1)
    # The pose at each frame first, then the simulation, then the keys: keying as it goes would change
    # what the next frame's pose reads.
    snaps = []
    for f in frames:
        scene.frame_set(f)
        snaps.append(snapshot(arm, names))
    dt = 1.0 / fps(scene)
    results = []
    for i, snap in enumerate(snaps):
        results.append(step(context, arm, snap, dt if i else 0.0))
    for f, res in zip(frames, results):
        for name, (basis, _base) in res.items():
            pb = arm.pose.bones[name]
            pb.matrix_basis = basis
            anim.insert_bone_key(pb, f, location=False)
    for pb in pbs:
        pb.vr_spring.enabled = False
    _sim.clear()
    _written.clear()
    springs_changed()
    scene.frame_set(current)
    anim.changed(context)
    return len(frames)


# --- which object or bone ------------------------------------------------------------

def selected(context):
    obj = context.view_layer.objects.active
    return obj if obj is not None and obj.select_get() else None


def current_rig(context):
    """The armature whose bones Physics mode shows: the selected one, or the selected mesh's rig."""
    return find_rig(selected(context))


def active_bone(arm):
    if arm is None:
        return None
    bone = arm.data.bones.active
    return arm.pose.bones.get(bone.name) if bone is not None else None


def nearest_bone(context, tip, scale, only_springs=False):
    """(rig, pose bone) within reach of tip: the current rig's bones, or (only_springs) any rig's spring
    bones; None."""
    best, best_d = None, REACH * scale
    if only_springs:
        rigs = [context.scene.objects.get(n) for n in spring_rigs(context)]
    else:
        rigs = [current_rig(context)]
    for arm in rigs:
        if arm is None or not arm.visible_get():
            continue
        for pb, head, tail in pose_mode.segments(arm):
            if only_springs and not has_spring(pb):
                continue
            d = pose_mode._closest(tip, head, tail)[0]
            if d <= best_d:
                best, best_d = (arm, pb), d
    return best


def set_spring(arm, pb, on):
    pb.vr_spring.enabled = on
    if not on:
        key = (arm.name, pb.name)
        w = _written.pop(key, None)
        if w is not None and _same(pb.matrix_basis, w[0]):
            pb.matrix_basis = w[1]
        _sim.pop(key, None)
    springs_changed()


def spring_chain(arm, pb):
    """The bone's spring settings (on) for it and every bone below it."""
    sp = pb.vr_spring
    todo = [pb]
    while todo:
        b = todo.pop()
        todo += list(b.children)
        b.vr_spring.enabled = True
        if b is not pb:
            b.vr_spring.stiffness, b.vr_spring.damping, b.vr_spring.gravity = sp.stiffness, sp.damping, sp.gravity
    springs_changed()


# --- trigger handlers ----------------------------------------------------------------

class SelectBone:
    """Trigger on a bone (setting up): its rig and the bone become the active ones."""

    def __init__(self, context, arm, pb):
        primitives.make_active(context, arm)
        arm.data.bones.active = pb.bone
        self.changed = True

    def update(self, context, pose, tip_pose, value):
        pass

    def finish(self, context):
        pass


class GrabBody:
    """Trigger on a rigid body while the simulation runs: it's kinematic (animated) and follows the hand
    while held, and flies on with the hand's velocity when let go. An active body's own transform is
    put back then, so the run still starts where it did; a static body stays where it was put."""

    def __init__(self, context, obj, tip_pose):
        self.name = obj.name
        self.base = obj.matrix_basis.copy()
        self.static = obj.rigid_body.type == 'PASSIVE'
        self.was_kinematic = obj.rigid_body.kinematic
        start = obj.matrix_world.copy()  # Where the simulation has it now, not its own transform.
        obj.rigid_body.kinematic = True
        obj.matrix_world = start
        self.offset = tip_pose.inverted_safe() @ start
        self.changed = False
        primitives.make_active(context, obj)

    def _obj(self):
        obj = bpy.data.objects.get(self.name)
        return obj if obj is not None and obj.rigid_body is not None else None

    def update(self, context, pose, tip_pose, value):
        obj = self._obj()
        if obj is not None:
            obj.matrix_world = tip_pose @ self.offset
            self.changed = self.static  # Moving an obstacle changes the setup (an undo step); a throw doesn't.

    def finish(self, context):
        obj = self._obj()
        if obj is None:
            return
        obj.rigid_body.kinematic = self.was_kinematic
        if not self.static:
            obj.matrix_basis = self.base


class GrabSpring:
    """Trigger on a spring bone (Hands on): its tail follows the hand while held, and swings on with the
    hand's velocity when let go."""

    def __init__(self, context, arm, pb, tip_pose):
        self.key = (arm.name, pb.name)
        tail = arm.matrix_world @ pb.tail
        self.offset = tail - tip_pose.translation
        self.last = tip_pose.translation.copy()
        self.time = time.monotonic()
        _held[self.key] = [tail, tail.copy(), Vector()]
        self.changed = False

    def update(self, context, pose, tip_pose, value):
        held = _held.get(self.key)
        if held is None:
            return
        point = tip_pose.translation + self.offset
        now = time.monotonic()
        dt = now - self.time
        if dt > 1e-4:
            held[2] = (point - held[1]) / dt
        held[0], held[1] = point, point.copy()
        self.time = now

    def abandon(self):
        _held.pop(self.key, None)

    def finish(self, context):
        held = _held.pop(self.key, None)
        state = _sim.get(self.key)
        if held is not None and state is not None:
            state[1] = held[2].copy()


class HandForce:
    """Push / Pull: while the trigger is held, the hand's force field pulls (main hand) or pushes (off hand)
    with the trigger's pressure. hand: the controller index (0 left, 1 right)."""

    def __init__(self, context, hand, value):
        from . import actions
        self.hand = hand
        self.sign = -1.0 if hand == actions.main_index(context) else 1.0  # A negative force attracts.
        self.changed = False
        self._set(context, value)

    def _set(self, context, value):
        settings = context.scene.vr_sculpt
        _hands["force"][self.hand] = self.sign * settings.physics_force * max(value, 0.2)

    def update(self, context, pose, tip_pose, value):
        self._set(context, value)

    def abandon(self):
        _hands["force"][self.hand] = 0.0

    def finish(self, context):
        _hands["force"][self.hand] = 0.0
        obj = bpy.data.objects.get(FIELD_NAMES[self.hand])
        if obj is not None and obj.field is not None:
            obj.field.strength = 0.0


def pick(context, settings, hand, tip_pose, scale, value):
    """Trigger press in Physics mode: the handler for what the tip is at, or None."""
    from . import interactions
    tip = tip_pose.translation
    if settings.physics_hands:
        if settings.physics_tool == 'FORCE':
            return HandForce(context, hand, value)
        hit = nearest_bone(context, tip, scale, only_springs=True)
        if hit is not None:
            return GrabSpring(context, hit[0], hit[1], tip_pose)
        obj = primitives.touching(context, tip, interactions.TOUCH_MARGIN * scale)
        if obj is None:
            return None
        if obj.rigid_body is not None and anim.is_playing(context) and not baked(context):
            return GrabBody(context, obj, tip_pose)
        return interactions.MoveObject(context, obj, tip_pose, scale)
    hit = nearest_bone(context, tip, scale)
    if hit is not None:
        return SelectBone(context, hit[0], hit[1])
    obj = primitives.touching(context, tip, interactions.TOUCH_MARGIN * scale)
    if obj is not None:
        return interactions.MoveObject(context, obj, tip_pose, scale)
    return interactions.Deselect(context)


def engaged(context, settings, tip, scale):
    from . import interactions
    if settings.physics_hands and settings.physics_tool == 'FORCE':
        return False
    return nearest_bone(context, tip, scale, only_springs=settings.physics_hands) is not None or \
        primitives.touching(context, tip, interactions.TOUCH_MARGIN * scale) is not None


# --- the app timer ---------------------------------------------------------------------

def tick(context, ss):
    """App timer (live.py): the hands in VR, and the springs while they're live. Returns whether this
    needs the fast timer."""
    sync_hands(context, ss)
    live = springs_live(context)
    if live:
        tick_springs(context, hand_tips() if hands_wanted(context) else ())
    elif _clock["running"]:
        reset_springs(context)  # Out of VR and stopped: the bones go back to their own pose.
    return live or hands_wanted(context)


def end_session(context):
    if any(bpy.data.objects.get(n) is not None for n in HAND_NAMES + FIELD_NAMES):
        remove_hands(context)


def forget_caches():
    """Undo / redo / load: the motion and the bases written are of data that's gone."""
    _sim.clear()
    _written.clear()
    _held.clear()
    _clock.update(time=None, running=False)
    springs_changed()
    _hands.update(tips=[None, None], force=[0.0, 0.0])


# --- drawing ---------------------------------------------------------------------------

def _rings(center, r, n=24):
    coords = []
    for axis in range(3):
        pts = []
        for k in range(n):
            a = 2.0 * math.pi * k / n
            c, s = math.cos(a) * r, math.sin(a) * r
            pts.append(center + Vector((c, s, 0.0) if axis == 0 else (c, 0.0, s) if axis == 1 else (0.0, c, s)))
        for k in range(n):
            coords += [pts[k], pts[(k + 1) % n]]
    return coords


def draw(context, tips, scale):
    """Physics mode in the headset: the current rig's bones (springs in green, the active one bright,
    the touched one yellow) and, with Hands on, each force while it's held."""
    settings = context.scene.vr_sculpt
    groups = {}
    arm = current_rig(context)
    if arm is not None and arm.visible_get():
        hover = set()
        for tip in tips:
            hit = nearest_bone(context, tip, scale, only_springs=settings.physics_hands)
            if hit is not None:
                hover.add(hit[1].name)
        active = arm.data.bones.active
        for pb, coords in pose_mode._bone_lines(arm):
            if pb.name in hover:
                color = pose_mode.HOVER_COLOR
            elif active is not None and pb.name == active.name:
                color = pose_mode.ACTIVE_COLOR
            elif has_spring(pb):
                color = SPRING_COLOR
            else:
                color = pose_mode.BONE_COLOR
            groups.setdefault(color, []).extend(coords)
    if settings.physics_hands:
        for i, tip in enumerate(tips):
            force = _hands["force"][i]
            if abs(force) > 1e-6:
                c = FORCE_COLORS[1 if force < 0 else 0]
                groups.setdefault((*c, 0.25), []).extend(_rings(tip, FORCE_REACH * scale))
    if not groups:
        return
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    for color, coords in groups.items():
        batch = batch_for_shader(shader, 'LINES', {"pos": coords})
        shader.uniform_float("color", color)
        batch.draw(shader)
    gpu.state.blend_set('NONE')


def register():
    bpy.utils.register_class(VRSpring)
    bpy.types.PoseBone.vr_spring = PointerProperty(type=VRSpring)


def unregister():
    remove_hands()
    del bpy.types.PoseBone.vr_spring
    bpy.utils.unregister_class(VRSpring)
