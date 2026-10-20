"""Probe Blender's pose-mode Python API: bone selection storage, local/pose conversion, auto IK,
XR armature display settings, bone motion paths and keying. Writes tests/probe_pose_result.txt.

blender --background --factory-startup --python tests/probe_pose.py
"""

import os
import sys
import time
import traceback

import bpy
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
LOG = os.path.join(ROOT, "tests", "probe_pose_result.txt")
lines = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    lines.append(s)


def main():
    from test_anim import make_rig
    ctx = bpy.context
    body, rig = make_rig(ctx)
    log("version", bpy.app.version_string)
    pb = rig.pose.bones["B1"]
    log("PoseBone attrs with select:", [a for a in dir(pb) if "sel" in a.lower()])
    log("Bone attrs with select:", [a for a in dir(pb.bone) if "sel" in a.lower()])
    log("Bone has convert_local_to_pose:", hasattr(pb.bone, "convert_local_to_pose"))
    log("Pose attrs:", [a for a in dir(rig.pose) if not a.startswith("_")])
    log("Armature data attrs:", [a for a in dir(rig.data) if not a.startswith("_")])
    xs = bpy.types.XrSessionSettings.bl_rna.properties.keys()
    log("XrSessionSettings props:", [p for p in xs if "show" in p])
    log("rig.show_in_front", rig.show_in_front)

    # pose mode
    ctx.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='POSE')
    log("mode", rig.mode)
    try:
        log("pb.select", pb.select)
        pb.select = True
        log("pb.select after set", pb.select, "bone.select", getattr(pb.bone, "select", "n/a"))
    except Exception as ex:
        log("pb.select error", ex)
    log("rig.data.bones.active", rig.data.bones.active)
    rig.data.bones.active = pb.bone
    log("active after", rig.data.bones.active.name)
    log("ctx.selected_pose_bones", getattr(ctx, "selected_pose_bones", "n/a"))

    # conversion round trip
    ctx.scene.frame_set(20)
    m = pb.matrix.copy()
    parent = pb.parent
    basis = pb.bone.convert_local_to_pose(m, pb.bone.matrix_local, parent_matrix=parent.matrix,
                                          parent_matrix_local=parent.bone.matrix_local, invert=True)
    log("basis from conversion:", [round(x, 4) for x in basis.to_quaternion()], "actual:",
        [round(x, 4) for x in pb.matrix_basis.to_quaternion()])

    # write cost
    t = time.perf_counter()
    for i in range(100):
        pb.rotation_euler.x = 0.01 * i
        ctx.view_layer.update()
    log("rotate + view_layer.update ms", (time.perf_counter() - t) * 10)

    # keyframe group
    pb.keyframe_insert("rotation_euler", frame=30, group=pb.name)
    from bpy_extras import anim_utils
    ad = rig.animation_data
    bag = anim_utils.action_get_channelbag_for_slot(ad.action, ad.action_slot)
    log("fcurves", [(fc.data_path, fc.array_index, fc.group.name if fc.group else None) for fc in bag.fcurves])

    # bone motion paths
    try:
        mp = rig.pose.animation_visualization.motion_path
        log("pose motion path settings", mp.type, mp.frame_start, mp.frame_end, mp.bake_location)
        with ctx.temp_override(object=rig, active_object=rig, selected_pose_bones=[pb]):
            bpy.ops.pose.paths_calculate(display_type='RANGE', range='SCENE', bake_location='TAILS')
        log("pb.motion_path", pb.motion_path, len(pb.motion_path.points) if pb.motion_path else 0)
        if pb.motion_path:
            log("first point", tuple(pb.motion_path.points[0].co), "frame_start", pb.motion_path.frame_start)
    except Exception as ex:
        log("paths error", ex)
        traceback.print_exc()

    # convert_space for comparison
    try:
        log("convert_space POSE->LOCAL ok:", rig.convert_space(pose_bone=pb, matrix=pb.matrix, from_space='POSE',
                                                              to_space='LOCAL').to_quaternion())
    except Exception as ex:
        log("convert_space error", ex)
    log("pose.ik_solver", getattr(rig.pose, "ik_solver", None), "use_auto_ik", getattr(rig.pose, "use_auto_ik", None))
    bpy.ops.object.mode_set(mode='OBJECT')
    log("object mode, pb.select still", getattr(pb, "select", None))


try:
    main()
except Exception:
    log(traceback.format_exc())
with open(LOG, "w") as f:
    f.write("\n".join(lines) + "\n")
