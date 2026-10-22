"""Add "Limits Rig" to the open file: an arm whose joints have limits, to try the VR IK and FK on.
Never saves.

- Upper: IK limits X / Z +-60 degrees, IK lock Y, and a Y rotation lock (Euler XYZ): no twist.
- Fore (the elbow): IK lock Y and Z, IK limit X 0..140 degrees: a hinge that bends one way.
- Hand: a Limit Rotation constraint (Local Space): X +-40, Y +-60, Z +-30 degrees.
- Slider: a free bone with a Limit Location constraint (Local Space): it only slides along
  itself, 0 to 15 cm.

In the headset: launch Blender with it, e.g.
blender test_scene.blend --python tests/add_ik_limits_rig.py
then in VR pick Pose > Rig: Limits Rig. Tests import build().
"""

import math

import bpy
from mathutils import Vector

NAME = "Limits Rig"
LOCATION = (-0.45, 0.05, 1.0)  # To the left of the sculpt ball, in reach from the VR start.


def build(context, location=LOCATION):
    old = bpy.data.objects.get(NAME)
    if old is not None:
        bpy.data.objects.remove(old)
    data = bpy.data.armatures.new(NAME)
    obj = bpy.data.objects.new(NAME, data)
    context.scene.collection.objects.link(obj)
    obj.location = location
    obj.show_in_front = True
    view_layer = context.view_layer
    previous = view_layer.objects.active
    view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    eb = data.edit_bones

    def bone(name, head, tail, parent=None):
        b = eb.new(name)
        b.head, b.tail = Vector(head), Vector(tail)
        b.roll = 0.0
        if parent is not None:
            b.parent = eb[parent]
            b.use_connect = (b.head - eb[parent].tail).length < 1e-6
        return b

    bone("Base", (0, 0, 0), (0, 0, 0.12))
    bone("Upper", (0, 0, 0.12), (0, 0, 0.32), "Base")
    bone("Fore", (0, 0, 0.32), (0, 0, 0.5), "Upper")
    bone("Hand", (0, 0, 0.5), (0, 0, 0.58), "Fore")
    bone("Slider", (0.15, 0, 0), (0.15, 0, 0.08))
    bpy.ops.object.mode_set(mode='OBJECT')
    view_layer.objects.active = previous

    pose = obj.pose.bones
    upper = pose["Upper"]
    upper.rotation_mode = 'XYZ'
    upper.lock_rotation[1] = True
    upper.lock_ik_y = True
    for a in "xz":
        setattr(upper, "use_ik_limit_" + a, True)
        setattr(upper, "ik_min_" + a, math.radians(-60))
        setattr(upper, "ik_max_" + a, math.radians(60))

    fore = pose["Fore"]
    fore.lock_ik_y = fore.lock_ik_z = True
    fore.use_ik_limit_x = True
    fore.ik_min_x, fore.ik_max_x = 0.0, math.radians(140)

    hand = pose["Hand"]
    con = hand.constraints.new('LIMIT_ROTATION')
    con.owner_space = 'LOCAL'
    for a, deg in (("x", 40), ("y", 60), ("z", 30)):
        setattr(con, "use_limit_" + a, True)
        setattr(con, "min_" + a, math.radians(-deg))
        setattr(con, "max_" + a, math.radians(deg))

    slider = pose["Slider"]
    con = slider.constraints.new('LIMIT_LOCATION')
    con.owner_space = 'LOCAL'
    for a in "xyz":
        setattr(con, "use_min_" + a, True)
        setattr(con, "use_max_" + a, True)
        setattr(con, "min_" + a, 0.0)
        setattr(con, "max_" + a, 0.15 if a == "y" else 0.0)
    view_layer.update()
    print(f"Added {NAME}: {', '.join(b.name for b in pose)}", flush=True)
    return obj


if __name__ == "__main__":
    build(bpy.context)
