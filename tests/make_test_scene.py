"""Build test_scene.blend: a sculpt ball at arm's length from the VR start pose, and a physics playground
on a table to the left (a tower of cubes and some balls to knock over and throw, a pinned curtain, a
jelly, and a springy antenna).

blender --background --factory-startup --python tests/make_test_scene.py
"""

import math
import os
import sys

import bpy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import vr_sculpt  # noqa: E402  (Physics mode's setup functions)
from vr_sculpt import physics  # noqa: E402

for o in list(bpy.data.objects):
    bpy.data.objects.remove(o)

# 41k verts, 25 cm radius, at chest height, 60 cm in front of the start pose.
bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=6, radius=0.25, location=(0.0, 0.0, 1.25))
ball = bpy.context.active_object
ball.name = "SculptBall"
bpy.ops.object.shade_smooth()

# 164k verts, for the performance comparison, to the right.
bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=7, radius=0.25, location=(0.7, 0.0, 1.25))
heavy = bpy.context.active_object
heavy.name = "HeavyBall_164k"
bpy.ops.object.shade_smooth()

# The VR session starts at the scene camera (VR Scene Inspection's default base pose).
cam_data = bpy.data.cameras.new("VRStart")
cam = bpy.data.objects.new("VRStart", cam_data)
bpy.context.scene.collection.objects.link(cam)
cam.location = (0.0, -0.6, 1.45)
cam.rotation_euler = (math.radians(90), 0.0, 0.0)  # look along +Y, level
bpy.context.scene.camera = cam

light = bpy.data.objects.new("Sun", bpy.data.lights.new("Sun", 'SUN'))
light.rotation_euler = (math.radians(40), 0, math.radians(30))
bpy.context.scene.collection.objects.link(light)


# --- physics playground: a table to the left, its top at hip height ---------------------------

vr_sculpt.register()
ctx = bpy.context
scene = ctx.scene
playground = bpy.data.collections.new("Physics Playground")
scene.collection.children.link(playground)
TOP = 0.8  # the table's top
TABLE_X, TABLE_Y = -0.95, 0.15


def into_playground(obj, name):
    obj.name = name
    for c in list(obj.users_collection):
        c.objects.unlink(obj)
    playground.objects.link(obj)
    return obj


def material(name, rgb):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*rgb, 1.0)
    return mat


def colored(obj, mat):
    obj.data.materials.append(mat)
    return obj


wood = material("Table", (0.45, 0.32, 0.22))
red = material("Red", (0.8, 0.2, 0.15))
blue = material("Blue", (0.2, 0.4, 0.85))
yellow = material("Yellow", (0.9, 0.75, 0.2))
green = material("Green", (0.25, 0.75, 0.4))
cloth_mat = material("Curtain", (0.75, 0.7, 0.9))

# The table: a Static body (things land on it) with a Collision modifier (so do the curtain and jelly).
# A 20 cm slab: cloth and soft things catch on a thin one less surely.
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(TABLE_X, TABLE_Y, TOP - 0.1))
table = into_playground(colored(ctx.active_object, wood), "Table")
table.scale = (1.1, 0.7, 0.2)
bpy.ops.object.transform_apply(scale=True)

# A tower of cubes to knock down.
S = 0.08
for i in range(6):
    bpy.ops.mesh.primitive_cube_add(size=S, location=(TABLE_X - 0.3, TABLE_Y, TOP + S / 2 + i * (S + 0.002)))
    into_playground(colored(ctx.active_object, red if i % 2 else blue), f"Block {i + 1}")

# Balls to pick up and throw.
for i in range(3):
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.04, segments=16, ring_count=8,
                                         location=(TABLE_X - 0.05 + 0.1 * i, TABLE_Y - 0.2, TOP + 0.045))
    bpy.ops.object.shade_smooth()
    into_playground(colored(ctx.active_object, yellow), f"Ball {i + 1}")

# A curtain hanging over the back of the table, pinned along its top edge.
bpy.ops.mesh.primitive_grid_add(x_subdivisions=24, y_subdivisions=24, size=0.5,
                                location=(TABLE_X + 0.05, TABLE_Y + 0.25, TOP + 0.4), rotation=(math.pi / 2, 0.0, 0.0))
curtain = into_playground(colored(ctx.active_object, cloth_mat), "Curtain")

# A jelly: soft (a cloth with internal springs), it squashes and wobbles where it lands.
bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3, radius=0.07, location=(TABLE_X + 0.3, TABLE_Y - 0.05, TOP + 0.08))
bpy.ops.object.shade_smooth()
jelly = into_playground(colored(ctx.active_object, green), "Jelly")

# A springy antenna: five bones up from the table, a tube bound to them, every bone a spring.
BASE = (TABLE_X + 0.45, TABLE_Y + 0.15, TOP)
LENGTH = 0.1
bpy.ops.object.armature_add(location=BASE)
rig = into_playground(ctx.active_object, "Antenna Rig")
bpy.ops.object.mode_set(mode='EDIT')
bones = rig.data.edit_bones
parent = None
for i in range(5):
    eb = bones[0] if i == 0 else bones.new(f"Antenna.{i + 1}")
    eb.name = f"Antenna.{i + 1}"
    eb.head, eb.tail = (0.0, 0.0, i * LENGTH), (0.0, 0.0, (i + 1) * LENGTH)
    if parent is not None:
        eb.parent, eb.use_connect = parent, True
    parent = eb
bpy.ops.object.mode_set(mode='OBJECT')
bpy.ops.mesh.primitive_cylinder_add(vertices=12, radius=0.015, depth=5 * LENGTH,
                                    location=(BASE[0], BASE[1], BASE[2] + 2.5 * LENGTH))
tube = into_playground(colored(ctx.active_object, yellow), "Antenna")
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.select_all(action='SELECT')
bpy.ops.mesh.subdivide(number_cuts=19)
bpy.ops.object.mode_set(mode='OBJECT')
bpy.ops.object.shade_smooth()
bpy.ops.object.select_all(action='DESELECT')
tube.select_set(True)
rig.select_set(True)
ctx.view_layer.objects.active = rig
bpy.ops.object.parent_set(type='ARMATURE_AUTO')

# From here on, Physics mode's own setup, as the hand panel does it.
ctx.view_layer.update()
physics.set_kind(ctx, table, 'STATIC')
for obj in playground.objects:
    if obj.name.startswith(("Block", "Ball")):
        physics.set_kind(ctx, obj, 'RIGID')
        if obj.name.startswith("Ball"):
            obj.rigid_body.collision_shape = 'SPHERE'
            obj.rigid_body.mass = 0.3
            obj.rigid_body.restitution = 0.5
physics.set_kind(ctx, curtain, 'CLOTH')
physics.set_pin(curtain, physics.PIN_TOP)
physics.set_kind(ctx, jelly, 'SOFT')
first = rig.pose.bones["Antenna.1"]
first.vr_spring.stiffness, first.vr_spring.damping, first.vr_spring.gravity = 0.3, 0.15, 0.2
physics.spring_chain(rig, first)
physics.set_length(ctx, 20.0)  # A 20 s run before it starts over.
scene.frame_set(scene.frame_start)

bpy.ops.object.select_all(action='DESELECT')
ball.select_set(True)
bpy.context.view_layer.objects.active = ball

path = os.path.join(ROOT, "test_scene.blend")
bpy.ops.wm.save_as_mainfile(filepath=path)
print("saved", path)
vr_sculpt.unregister()
