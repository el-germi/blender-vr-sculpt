"""Build test_scene.blend: a sculpt ball at arm's length from the VR start pose.

blender --background --factory-startup --python tests/make_test_scene.py
"""

import math
import os

import bpy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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

bpy.ops.object.select_all(action='DESELECT')
ball.select_set(True)
bpy.context.view_layer.objects.active = ball

path = os.path.join(ROOT, "test_scene.blend")
bpy.ops.wm.save_as_mainfile(filepath=path)
print("saved", path)
