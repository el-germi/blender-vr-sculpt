"""Background tests for the QoL tools: grid and snapping, symmetry, tape measure, scene camera,
navigation, tracking objects, the desktop screen's placement and the Tools page of the panel.

blender --background --factory-startup --python tests/test_tools.py
"""

import math
import os
import sys
import traceback

import bpy
import numpy as np
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import vr_sculpt  # noqa: E402
from vr_sculpt import (actions, brushes, camera, desktop, grid, interactions, measure, menus,  # noqa: E402
                       navigation, operators, primitives, symmetry, tracking, vr_ui)

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def close(a, b, tol=1e-4):
    return all(abs(x - y) < tol for x, y in zip(a, b))


class NavSession:
    """Fake XR session state where world poses follow the navigation, like the real one."""

    def __init__(self):
        self.navigation_location = Vector((0.0, 0.0, 0.0))
        self.navigation_rotation = Quaternion()
        self.navigation_scale = 1.0
        # Tracking space (what the headset reports): head 1.6 m up, looking along +Y.
        look = Quaternion((1, 0, 0), math.pi / 2)  # -Z (view direction) turned to +Y
        self.head = Matrix.LocRotScale(Vector((0.0, 0.0, 1.6)), look, None)
        self.hands = [Matrix.LocRotScale(Vector((-0.2, 0.3, 1.2)), look, None),
                      Matrix.LocRotScale(Vector((0.2, 0.3, 1.2)), look, None)]
        self.values = {}

    def nav(self):
        return navigation.navigation_matrix(self)

    @property
    def viewer_scale(self):
        return self.navigation_scale

    @property
    def viewer_pose_location(self):
        return (self.nav() @ self.head).translation

    @property
    def viewer_pose_rotation(self):
        return (self.nav() @ self.head).to_quaternion()

    def controller_aim_location_get(self, context, i):
        return (self.nav() @ self.hands[i]).translation

    def controller_aim_rotation_get(self, context, i):
        return (self.nav() @ self.hands[i]).to_quaternion()

    controller_grip_location_get = controller_aim_location_get
    controller_grip_rotation_get = controller_aim_rotation_get

    def action_state_get(self, context, action_set, action, user_path):
        return self.values.get((action, user_path), 0.0)

    def haptic_action_apply(self, *args):
        return True


def test_grid():
    print("== grid and snapping")
    check(close(grid.snap(Vector((0.12, -0.07, 0.26)), 0.05), (0.1, -0.05, 0.25)), "snap rounds to the grid")
    settings = bpy.context.scene.vr_sculpt
    settings.grid_snap = False
    check(close(grid.snap_if(settings, (0.12, 0, 0)), (0.12, 0, 0)), "snap_if leaves points alone with snap off")
    settings.grid_snap = True
    settings.grid_size = 0.05
    check(close(grid.snap_if(settings, (0.12, 0, 0)), (0.1, 0, 0)), "snap_if snaps with snap on")
    points, step = grid.lattice(Vector((0.01, 0.0, 0.0)), 0.05, 0.12)
    check(step == 0.05 and 20 < len(points) < 100, f"lattice lists nearby points ({len(points)}, step {step})")
    check(all(0.0 < f <= 1.0 for _p, f in points), "lattice fades with distance")
    points, step = grid.lattice(Vector((0.0, 0.0, 0.0)), 0.001, 0.12)
    check(step > 0.001 and len(points) < 600, f"a very fine grid is thinned out ({len(points)}, step {step:.3f})")

    # Model mode snapping: create and move.
    ctx = bpy.context
    menus.set_mode(ctx, 'MODEL')
    settings.primitive = "mesh.primitive_cube_add"
    h = interactions.CreatePrimitive(ctx, settings.primitive, Vector((0.012, 0.011, 0.024)), 1.0)
    h.update(ctx, Matrix(), Matrix.Translation((0.21, 0.19, 0.18)), 1.0)
    h.finish(ctx)
    check(close(h.start, (0.0, 0.0, 0.0)), f"creation starts on the grid ({tuple(h.start)})")
    check(close(h.obj.dimensions, (0.2, 0.2, 0.2), 1e-3), f"dragged box ends on the grid ({tuple(h.obj.dimensions)})")
    mv = interactions.MoveObject(ctx, h.obj, Matrix.Translation((0.0, 0.0, 0.0)))
    mv.update(ctx, Matrix(), Matrix.Translation((0.033, 0.0, 0.0)), 1.0)
    check(close(h.obj.location, (0.15, 0.1, 0.1)), f"grabbed objects move in grid steps ({tuple(h.obj.location)})")
    settings.grid_snap = False
    primitives.delete(ctx, h.obj)


def test_symmetry():
    print("== symmetry")
    ctx = bpy.context
    ctx.scene.cursor.location = (0.5, 0.0, 0.0)
    cube = primitives.create(ctx, 'CUBE', (1.0, 0.0, 0.0))
    symmetry.model_toggle(ctx, cube, 0)
    mod = cube.modifiers.get(symmetry.MODIFIER)
    check(mod is not None and tuple(mod.use_axis) == (True, False, False), "Model mirror X adds a Mirror modifier")
    check(mod.mirror_object is not None and close(mod.mirror_object.location, (0.5, 0, 0)),
          "it mirrors around the center empty, at the 3D cursor")
    ctx.view_layer.update()
    corners = [cube.matrix_world @ Vector(c) for c in cube.bound_box]
    check(min(c.x for c in corners) < 0.01, f"the mirrored copy reaches across the center ({min(c.x for c in corners):.3f})")
    symmetry.model_toggle(ctx, cube, 2)
    check(symmetry.model_axes(cube) == (True, False, True) and symmetry.model_bits(cube) == 5, "Z added")
    symmetry.model_toggle(ctx, cube, 0)
    symmetry.model_toggle(ctx, cube, 2)
    check(cube.modifiers.get(symmetry.MODIFIER) is None, "turning every axis off removes the modifier")
    symmetry.move_center(ctx, Vector((0.0, 1.0, 0.0)))
    check(close(bpy.data.objects[symmetry.CENTER].location, (0, 1, 0)) and close(ctx.scene.cursor.location, (0, 1, 0)),
          "Here moves the cursor and the center")
    primitives.delete(ctx, cube)

    settings = ctx.scene.vr_sculpt
    settings.engine = 'PYTHON'
    symmetry.sculpt_toggle(ctx, 1)
    check(symmetry.sculpt_axes(ctx) == (False, True, False) and symmetry.sculpt_bits(settings) == 2,
          "Python sculpt Y symmetry")
    symmetry.sculpt_toggle(ctx, 1)

    # Python brush with Y+Z symmetry hits all four mirrored spots.
    bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24, radius=1.0)
    ball = ctx.active_object
    st = brushes.MeshState(ball)
    before = st.co.copy()
    s = brushes.Stroke(st, "INFLATE", 0.3, 1.0, symmetry=6)
    s.update(Vector((0.3, 0.6, 0.75)), pressure=1.0)
    moved = np.linalg.norm(st.co - before, axis=1) > 1e-5
    quads = {(np.sign(c[1]), np.sign(c[2])) for c in before[moved]}
    check(len(quads) == 4, f"Y+Z symmetry dabs all four quadrants ({sorted(quads)})")
    check(not any(before[moved][:, 0] < -0.2), "and leaves -X alone")

    menus.set_engine(ctx, 'NATIVE')
    symmetry.sculpt_toggle(ctx, 2)
    check(ball.data.use_mirror_z and symmetry.sculpt_axes(ctx)[2], "native symmetry uses the mesh's own flags")
    menus.set_engine(ctx, 'PYTHON')
    bpy.data.objects.remove(ball)


def test_measure():
    print("== tape measure")
    ctx = bpy.context
    scene = ctx.scene
    settings = scene.vr_sculpt
    measure.clear(scene)
    measure.add(scene, (0, 0, 0), (0.3, 0.4, 0))
    measure.add(scene, (1, 1, 1), (1, 1, 2))
    pairs = measure.stored(scene)
    check(len(pairs) == 2 and abs((pairs[0][1] - pairs[0][0]).length - 0.5) < 1e-6, "measurements are stored")
    check(measure.label(scene, 0.5) == "50 cm", f"label uses scene units ({measure.label(scene, 0.5)!r})")
    scene.unit_settings.system = 'IMPERIAL'
    check("'" in measure.label(scene, 1.0) or '"' in measure.label(scene, 1.0),
          f"imperial label ({measure.label(scene, 1.0)!r})")
    scene.unit_settings.system = 'METRIC'
    check(measure.undo_last(scene) and len(measure.stored(scene)) == 1, "undo removes the last one")
    check(measure.clear(scene) and measure.stored(scene) == [] and not measure.clear(scene), "clear removes all")

    # Handler: grid snap, then vertex snap.
    settings.grid_snap = True
    settings.grid_size = 0.1
    m = measure.Measure(ctx, settings, Vector((0.02, 0.01, -0.03)), 1.0)
    check(m in measure.active, "a tape being dragged is drawn")
    m.update(ctx, Matrix(), Matrix.Translation((0.48, 0.0, 0.0)), 1.0)
    m.finish(ctx)
    pairs = measure.stored(scene)
    check(len(pairs) == 1 and close(pairs[0][0], (0, 0, 0)) and close(pairs[0][1], (0.5, 0, 0)),
          f"ends snap to the grid ({[tuple(p) for p in pairs[0]] if pairs else None})")
    check(m not in measure.active, "finished tapes aren't drawn as live")
    settings.grid_snap = False
    cube = primitives.create(ctx, 'CUBE', (2.0, 0.0, 0.0))
    ctx.view_layer.update()
    m = measure.Measure(ctx, settings, Vector((2.054, 0.047, 0.045)), 1.0)
    check(close(m.a, (2.05, 0.05, 0.05)), f"ends snap to a nearby vertex ({tuple(m.a)})")
    m.update(ctx, Matrix(), Matrix.Translation((2.0, 0.0, 0.3)), 1.0)
    check(close(m.b, (2.0, 0.0, 0.3)), "away from meshes, ends follow the tip")
    m.finish(ctx)
    short = measure.Measure(ctx, settings, Vector((5, 5, 5)), 1.0)
    short.finish(ctx)
    check(not short.changed and len(measure.stored(scene)) == 2, "a click without a drag adds nothing")
    primitives.delete(ctx, cube)
    measure.clear(scene)


def test_camera():
    print("== scene camera")
    ctx = bpy.context
    scene = ctx.scene
    for o in [o for o in scene.objects if o.type == 'CAMERA']:
        bpy.data.objects.remove(o)
    check(scene.camera is None, "no camera to start with")
    pose = Matrix.LocRotScale(Vector((1, 2, 3)), Quaternion((0, 0, 1), 0.5), None)
    h = camera.AimCamera(ctx, pose)
    check(scene.camera is not None and scene.camera.type == 'CAMERA', "aiming with no camera creates one")
    check(close(scene.camera.matrix_world.translation, (1, 2, 3)), "the camera is at the controller")
    pose2 = Matrix.LocRotScale(Vector((0, 0, 1)), Quaternion((1, 0, 0), 0.3), None)
    h.update(ctx, pose2, pose2, 1.0)
    h.finish(ctx)
    fwd = scene.camera.matrix_world.to_3x3() @ Vector((0, 0, -1))
    check((fwd - pose2.to_3x3() @ Vector((0, 0, -1))).length < 1e-5, "it looks where the controller points")
    first = scene.camera
    second = camera.new_camera(ctx)
    check(scene.camera == second, "a new camera becomes the scene camera")
    camera.next_camera(ctx)
    check(scene.camera == first, "next camera cycles")
    scene.render.resolution_x, scene.render.resolution_y = 1920, 1080
    w, h_px = camera.preview_size(scene)
    check(w == camera.PREVIEW_PIXELS and abs(w / h_px - 16 / 9) < 0.02, f"preview keeps the render aspect ({w}x{h_px})")
    settings = scene.vr_sculpt
    settings.mode, settings.tool, settings.camera_preview = 'TOOLS', 'MEASURE', False
    check(not camera.wanted(ctx), "no preview unless asked for")
    settings.tool = 'CAMERA'
    check(camera.wanted(ctx), "the Camera tool shows the preview")
    settings.tool, settings.camera_preview = 'MEASURE', True
    check(camera.wanted(ctx), "pinned preview shows in any tool")
    settings.camera_preview = False

    # The VR viewer is placed by the scene camera (the stock add-on's default landmark): aiming or
    # switching it must not drag you along.
    from vr_sculpt import operators
    xs = ctx.window_manager.xr_session_settings
    xs.base_pose_type = 'SCENE_CAMERA'
    running = operators.session_running
    operators.session_running = lambda context: True
    other = bpy.data.objects.new("Base", None)
    scene.collection.objects.link(other)
    try:
        cam = scene.camera
        cam.matrix_world = Matrix.LocRotScale(Vector((2, -1, 1.5)), Quaternion((0, 0, 1), 0.7) @
                                              Quaternion((1, 0, 0), 1.4), None)
        ctx.view_layer.update()
        h = camera.AimCamera(ctx, pose)
        check(xs.base_pose_type == 'CUSTOM' and close(xs.base_pose_location, (2, -1, 1.5)) and
              abs(xs.base_pose_angle - 0.7) < 1e-5, "aiming the camera you're placed by pins the base pose there")
        h.update(ctx, pose2, pose2, 1.0)
        h.finish(ctx)
        camera.next_camera(ctx)
        check(xs.base_pose_type == 'CUSTOM' and close(xs.base_pose_location, (2, -1, 1.5)),
              "moving and switching cameras leave it where it was")
        camera.release_base_pose(ctx)
        check(xs.base_pose_type == 'SCENE_CAMERA' and camera._held["type"] is None, "the session's end puts it back")
        xs.base_pose_type, xs.base_pose_object = 'OBJECT', other
        camera.AimCamera(ctx, pose).finish(ctx)
        check(xs.base_pose_type == 'OBJECT', "placed by another object, aiming a camera changes nothing")
    finally:
        operators.session_running = running
        camera.release_base_pose(ctx)
        xs.base_pose_type, xs.base_pose_object = 'SCENE_CAMERA', None
        bpy.data.objects.remove(other)


def test_navigation():
    print("== navigation")
    ctx = bpy.context
    settings = ctx.scene.vr_sculpt
    settings.nav_speed = 2.0
    ss = NavSession()
    settings.nav_mode = 'WALK'
    # Look up 30 degrees: walking still stays level.
    ss.head = ss.head @ Matrix.Rotation(math.radians(30), 4, 'X')
    head0 = ss.viewer_pose_location.copy()
    navigation.move(ss, settings, 0.0, 1.0, 1.0, 0.5)
    head1 = ss.viewer_pose_location
    check(abs(head1.z - head0.z) < 1e-6 and abs((head1 - head0).length - 1.0) < 1e-6,
          f"walk forward: 1 m level in 0.5 s at 2 m/s ({tuple(head1 - head0)})")
    check((head1 - head0).y > 0.99, "toward where you look")
    settings.nav_mode = 'FLY'
    head0 = ss.viewer_pose_location.copy()
    navigation.move(ss, settings, 0.0, 1.0, 1.0, 0.5)
    d = ss.viewer_pose_location - head0
    check(abs(d.z - 0.5) < 1e-3 and abs(d.length - 1.0) < 1e-6, f"fly forward climbs where you look ({tuple(d)})")
    check(navigation.ramp(0.2) == 0.0 and navigation.ramp(1.0) == 1.0 and 0 < navigation.ramp(0.6) < 0.5,
          "stick values ease in past the dead zone")
    head0 = ss.viewer_pose_location.copy()
    navigation.lift(ss, settings, -1.0, 1.0, 0.25)
    check(close(ss.viewer_pose_location - head0, (0, 0, -0.5)), "fly down")

    # Turning pivots around the viewer.
    ss.navigation_location = Vector((3.0, -2.0, 0.0))
    head0 = ss.viewer_pose_location.copy()
    fwd0 = ss.viewer_pose_rotation @ Vector((0, 0, -1))
    navigation.turn(ss, math.radians(90))
    head1 = ss.viewer_pose_location
    fwd1 = ss.viewer_pose_rotation @ Vector((0, 0, -1))
    check(close(head1.xy, head0.xy) and abs(head1.z - head0.z) < 1e-6, "turning keeps you in place")
    check(abs(fwd1.xy.angle(fwd0.xy) - math.radians(90)) < 1e-4, "turn rotates the view 90 degrees")
    # Positive angle = turn left (counterclockwise from above): +Y turns toward -X.
    check(fwd1.x < -0.5 * fwd1.length, f"positive turns go left ({tuple(fwd1)})")

    # World scale: speed is in hand space.
    ss2 = NavSession()
    ss2.navigation_scale = 10.0
    settings.nav_mode = 'WALK'
    h0 = ss2.viewer_pose_location.copy()
    navigation.move(ss2, settings, 1.0, 0.0, 1.0, 0.5)
    check(abs((ss2.viewer_pose_location - h0).length - 10.0) < 1e-4, "at 10x scale you move 10x as far")

    # Walking up a step: the floor follower lifts you by the step height.
    for o in list(bpy.data.objects):
        if o.type == 'MESH':
            bpy.data.objects.remove(o)
    bpy.ops.mesh.primitive_plane_add(size=20, location=(0, 0, 0))
    bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 3.0, -0.8))  # top at z=0.2, from y=2 to 4
    bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 6.0, 0.0))  # a 1 m high block further on
    ctx.view_layer.update()
    ss3 = NavSession()
    follow = navigation.FloorFollower(ctx, ss3)
    check(follow.floor is not None and abs(follow.floor) < 1e-6, f"finds the floor below ({follow.floor})")
    z0 = ss3.viewer_pose_location.z
    ss3.navigation_location = Vector((0.0, 2.5, 0.0))
    for _ in range(60):
        follow.step(ctx, ss3, 1 / 60)
    check(abs(ss3.viewer_pose_location.z - z0 - 0.2) < 0.01,
          f"stepping onto a 20 cm step lifts you 20 cm ({ss3.viewer_pose_location.z - z0:.3f})")
    ss3.navigation_location = Vector((0.0, 5.5, ss3.navigation_location.z))
    for _ in range(30):
        follow.step(ctx, ss3, 1 / 60)
    check(abs(ss3.viewer_pose_location.z - z0 - 0.2) < 0.01, "a 1 m block is too high to climb: walk through it")
    ss3.navigation_location = Vector((0.0, -1.0, ss3.navigation_location.z))
    for _ in range(10):
        follow.step(ctx, ss3, 1 / 60)
    follow.finish(ss3)
    check(abs(ss3.viewer_pose_location.z - z0) < 1e-6, "stepping down lands back on the floor")
    for o in list(bpy.data.objects):
        if o.type == 'MESH':
            bpy.data.objects.remove(o)


def test_tracking():
    print("== tracking objects")
    ctx = bpy.context
    ss = NavSession()
    ss.navigation_location = Vector((1.0, 0.0, 0.0))
    ss.values[(actions.ACTION_STROKE, actions.HANDS[1])] = 0.8
    ss.values[("nav_grab", actions.HANDS[0])] = 1.0
    ss.values[("fly_forward", actions.HANDS[0])] = 0.6
    ss.values[("fly_turnleft", actions.HANDS[1])] = -0.5  # NEGATIVE axis region: negative values
    tracking.update(ctx, ss)
    head, left, right = (bpy.data.objects[n] for n in (tracking.HEADSET, *tracking.HANDS))
    coll = bpy.data.collections[tracking.COLLECTION]
    check(all(o.name in coll.objects for o in (head, left, right)), "empties in the VR Tracking collection")
    check(close(head.matrix_world.translation, ss.viewer_pose_location), "headset empty at the viewer")
    check(close(right.matrix_world.translation, ss.controller_grip_location_get(ctx, 1)), "hand empty at the grip")
    check(abs(right["trigger"] - 0.8) < 1e-6 and abs(left["grip"] - 1.0) < 1e-6, "trigger and grip values")
    check(abs(left["stick_y"] - 0.6) < 1e-6 and abs(right["stick_x"] + 0.5) < 1e-6,
          f"stick values (left y {left['stick_y']:.2f}, right x {right['stick_x']:.2f})")
    # A driver on another object follows the trigger.
    target = bpy.data.objects.new("DrivenByTrigger", None)
    ctx.scene.collection.objects.link(target)
    fc = target.driver_add("location", 2)
    v = fc.driver.variables.new()
    v.name = "t"
    v.targets[0].id = right
    v.targets[0].data_path = '["trigger"]'
    fc.driver.expression = "t"
    ctx.view_layer.update()
    ss.values[(actions.ACTION_STROKE, actions.HANDS[1])] = 0.25
    tracking.update(ctx, ss)
    ctx.view_layer.update()
    check(abs(target.location.z - 0.25) < 1e-5, f"drivers follow the values ({target.location.z:.3f})")
    n = len(bpy.data.objects)
    tracking.update(ctx, ss)
    check(len(bpy.data.objects) == n, "updates reuse the same empties")


def test_desktop_screen():
    print("== desktop screen")
    ctx = bpy.context
    settings = ctx.scene.vr_sculpt
    settings.desktop_size = 1.0
    ss = NavSession()
    desktop.state["pose"] = None
    desktop.place_in_front(ctx, ss)
    m = desktop.world_matrix(ctx, ss)
    w, h = desktop.screen_size(ctx, 1.0)
    center = m @ Vector((0.0, h / 2, 0.0))
    head = ss.viewer_pose_location
    check(abs((center - head).y - desktop.DISTANCE) < 1e-4, f"placed in front of the viewer ({tuple(center - head)})")
    normal = (m.to_3x3() @ Vector((0, 0, 1))).normalized()
    check(normal.y < -0.99, "facing the viewer")
    origin = head
    hit = desktop.ray_hit(ctx, ss, origin, (center - origin).normalized())
    check(hit is not None and close(hit, (0.0, h / 2)), f"a ray at the center hits the center ({hit})")
    check(desktop.ray_hit(ctx, ss, origin, Vector((0, -1, 0))) is None, "a ray away misses")
    # It travels with the navigation.
    ss.navigation_location = Vector((5.0, 0.0, 0.0))
    m2 = desktop.world_matrix(ctx, ss)
    check(close(m2.translation - m.translation, (5, 0, 0)), "the screen moves with you when you fly")
    # Dragging.
    pose = Matrix.Translation(ss.viewer_pose_location)
    mv = desktop.MoveScreen(ctx, ss, pose)
    mv.update(ctx, Matrix.Translation(ss.viewer_pose_location + Vector((0, 0, 0.3))), None, 1.0)
    check(close(desktop.world_matrix(ctx, ss).translation - m2.translation, (0, 0, 0.3)), "dragging moves it")
    desktop.state["pose"] = None


class FakeOS:
    """Stands in for winput's Windows calls and records what would have been sent."""

    def __init__(self, covered=False):
        self.log = []
        self.covered = covered
        self.cursor = (0, 0)
        self.saved = {}

    def install(self):
        from vr_sculpt import winput
        names = ("find_window", "client_rect", "move", "button", "wheel", "ours_at", "ensure_foreground",
                 "press_keys", "cursor_pos", "minimized")
        self.saved = {n: getattr(winput, n) for n in names}
        self.saved["AVAILABLE"] = winput.AVAILABLE
        winput.AVAILABLE = True
        winput.find_window = lambda: 1234
        winput.client_rect = lambda hwnd: (100, 50, 1600, 900)
        winput.minimized = lambda hwnd: False

        def move(x, y):
            self.cursor = (x, y)
            self.log.append(("move", x, y))
        winput.move = move
        winput.button = lambda name, down: self.log.append(("button", name, down))
        winput.wheel = lambda n: self.log.append(("wheel", n))
        winput.ours_at = lambda hwnd, x, y: not self.covered
        winput.ensure_foreground = lambda hwnd: not self.covered
        winput.press_keys = lambda vks: self.log.append(("keys", tuple(vks)))
        winput.cursor_pos = lambda: self.cursor
        return self

    def uninstall(self):
        from vr_sculpt import winput
        for n, f in self.saved.items():
            setattr(winput, n, f)


def test_desktop_input():
    print("== desktop input (fake OS layer)")
    ctx = bpy.context
    settings = ctx.scene.vr_sculpt
    settings.show_desktop, settings.desktop_interactive, settings.desktop_size = True, True, 1.0
    settings.desktop_keyboard = False
    fake = FakeOS().install()
    try:
        ss = NavSession()
        desktop.state["pose"] = None
        desktop.place_in_front(ctx, ss)
        check(desktop.use_os(ctx) and desktop.interactive(ctx), "Windows: OS capture and mouse from VR")
        w, h = desktop.screen_size(ctx, 1.0)
        check(abs(h - 0.5625) < 1e-6, f"screen keeps the window's 16:9 shape ({h:.4f})")
        m = desktop.world_matrix(ctx, ss)

        def aim_at(local_x, local_y):
            """Point the right controller from the head at a point on the screen."""
            target = m @ Vector((local_x, local_y, 0.0))
            head = ss.viewer_pose_location
            rot = (target - head).to_track_quat('-Z', 'Y')
            ss.hands[1] = ss.nav().inverted() @ Matrix.LocRotScale(head, rot, None)
            return target

        aim_at(-w / 2 + 0.25 * w, 0.75 * h)  # a quarter in, three quarters up
        kind, uv, _p = desktop.hit(ctx, ss, *desktop._right_ray(ctx, ss))
        check(kind == 'content' and close(uv, (0.25, 0.75)), f"ray hits the content at window fractions ({uv})")
        desktop.hover(ctx, ss)
        check(fake.cursor == (100 + 400, 50 + 225), f"hover moves the cursor to that pixel ({fake.cursor})")

        class Ctx:
            def __getattr__(self, name):
                return getattr(bpy.context, name)
        c = Ctx()
        c.window_manager = type("WM", (), {"xr_session_state": ss, "windows": bpy.context.window_manager.windows})()
        settings.show_panel = False
        pose = ss.nav() @ ss.hands[1]
        handler = operators._pick_handler(c, settings, actions.HANDS[1], pose, pose, 1.0, 1.0)
        check(isinstance(handler, desktop.MouseDrag) and ("button", "LEFT", True) in fake.log,
              "right trigger on the content presses the left button")
        aim_at(-w / 2 + 0.5 * w, 0.5 * h)
        handler.update(ctx, None, None, 1.0)
        check(fake.cursor == (100 + 800, 50 + 450), f"dragging moves the cursor ({fake.cursor})")
        handler.finish(ctx)
        check(fake.log[-1] == ("button", "LEFT", False), "release lets go of the button")

        left = ss.nav() @ ss.hands[0]
        handler = operators._pick_handler(c, settings, actions.HANDS[0], left, left, 1.0, 1.0)
        check(isinstance(handler, desktop.MouseDrag) and handler.button == 'MIDDLE',
              "left trigger while the right hand points: middle button (pan)")
        handler.finish(ctx)

        fake.log.clear()
        op = type("H", (), {"execute": operators.VRSCULPT_OT_cycle_brush.execute, "left_hand": False,
                            "report": lambda *a: None})()
        op.execute(c)
        check(("button", "RIGHT", True) in fake.log and ("button", "RIGHT", False) in fake.log,
              "B while pointing: right click")
        desktop.scroll(ctx, ss, -1)
        check(fake.log[-1] == ("wheel", -1), "the stick scrolls the wheel")

        # Covered window: no click goes out.
        fake.covered = True
        fake.log.clear()
        handler = desktop.MouseDrag(ctx, ss, 'LEFT')
        check(not handler.pressed and not any(e[0] == "button" for e in fake.log), "covered window: no click")
        check("covered" in desktop.state["notice"][0], "and the title bar says why")
        handler.finish(ctx)
        fake.covered = False

        # Title bar: drag area moves the screen, buttons press.
        title = next(p for p in desktop.panels(ctx, ss) if p[0] == "title")
        _n, tm, widgets, th, tw = title
        kb = vr_ui.find(widgets, "desk_keyboard")
        target = tm @ Vector(((kb.rect[0] + kb.rect[2]) / 2, (kb.rect[1] + kb.rect[3]) / 2, 0.0))
        head = ss.viewer_pose_location
        ss.hands[1] = ss.nav().inverted() @ Matrix.LocRotScale(head, (target - head).to_track_quat('-Z', 'Y'), None)
        pose = ss.nav() @ ss.hands[1]
        handler = operators._pick_handler(c, settings, actions.HANDS[1], pose, pose, 1.0, 1.0)
        check(isinstance(handler, desktop.ButtonPress) and settings.desktop_keyboard,
              "title bar Keyboard button shows the keyboard")
        lbl = widgets[0]
        target = tm @ Vector(((lbl.rect[0] + lbl.rect[2]) / 2, (lbl.rect[1] + lbl.rect[3]) / 2, 0.0))
        ss.hands[1] = ss.nav().inverted() @ Matrix.LocRotScale(head, (target - head).to_track_quat('-Z', 'Y'), None)
        pose = ss.nav() @ ss.hands[1]
        handler = operators._pick_handler(c, settings, actions.HANDS[1], pose, pose, 1.0, 1.0)
        check(isinstance(handler, desktop.MoveScreen), "title bar label drags the screen")

        # Keyboard: sticky modifiers.
        names = [p[0] for p in desktop.panels(ctx, ss)]
        check(names == ["title", "keyboard"], f"keyboard panel under the screen ({names})")
        kb_widgets = desktop.panels(ctx, ss)[1][2]
        fake.log.clear()
        vr_ui.find(kb_widgets, "key_ctrl").on_press()
        kb_widgets = desktop.panels(ctx, ss)[1][2]
        vr_ui.find(kb_widgets, "key_z").on_press()
        check(fake.log == [("keys", (0x11, ord("Z")))], f"Ctrl then Z sends Ctrl+Z ({fake.log})")
        check(not desktop.keyboard["CTRL"], "modifiers are released after one key")
        settings.desktop_keyboard = False
    finally:
        fake.uninstall()
        desktop.state["pose"] = None
        desktop.state["notice"] = ("", 0.0)
        settings.show_desktop = False
        settings.show_panel = True


def test_tools_page():
    print("== Tools page and buttons")
    ctx = bpy.context
    settings = ctx.scene.vr_sculpt
    menus.set_mode(ctx, 'TOOLS')
    tool_ids = {'MEASURE': {"measures_show", "measure_undo", "measure_clear"},
                'CAMERA': {"camera", "camera_preview", "camera_lens"}}
    for tool in ('MEASURE', 'CAMERA'):
        settings.tool = tool
        widgets, height = menus.layout(ctx)
        ids = {w.id for w in widgets}
        need = {"mode_tools", "tool_measure", "tool_camera", "settings", "pin"} | tool_ids[tool]
        check(need <= ids, f"{tool}: page has its buttons (missing {sorted(need - ids)})")
        rail = vr_ui.rail_height(len(menus.MODES))  # The eight mode icons set the least height.
        check(height <= max(0.2, rail) + 1e-9, f"{tool}: panel height {height:.3f} m (the rail: {rail:.3f} m)")
    settings.tool = 'CAMERA'
    check(vr_ui.find(menus.layout(ctx)[0], "camera_new") is not None, "New here shows by default")
    settings.show_new_camera = False
    widgets, _ = menus.layout(ctx)
    check(vr_ui.find(widgets, "camera_new") is None and vr_ui.find(widgets, "camera_preview") is not None,
          "VR Extras can hide New here (Pin preview stays)")
    settings.show_new_camera = True
    settings.tool = 'MEASURE'

    # Grid, navigation, view and tracking live in Settings (the gear, in every mode).
    vr_ui.find(menus.layout(ctx)[0], "settings").on_press()
    widgets, height = menus.layout(ctx)
    ids = {w.id for w in widgets}
    need = {"back", "grid_show", "grid_snap", "show_desktop", "passthrough", "show_timeline", "tracking",
            "tracking_record", "nav_walk", "nav_fly", "nav_speed", "reveal_wrist", "reveal_look", "reveal_always"}
    check(need <= ids, f"Settings has the rest (missing {sorted(need - ids)})")
    check(height < 0.3, f"Settings: panel height {height:.3f} m")
    vr_ui.find(widgets, "nav_fly").on_press()
    check(settings.nav_mode == 'FLY', "Fly button")
    vr_ui.find(widgets, "grid_show").on_press()
    check(settings.grid_show, "Grid button")
    xs = ctx.window_manager.xr_session_settings
    before = xs.show_passthrough
    vr_ui.find(widgets, "passthrough").on_press()
    check(xs.show_passthrough != before, "Passthrough button toggles the session setting")
    xs.show_passthrough = before
    shown = settings.show_timeline
    vr_ui.find(widgets, "show_timeline").on_press()
    check(settings.show_timeline != shown, "Timeline button shows / hides the timeline strip")
    settings.show_timeline = shown
    vr_ui.find(menus.layout(ctx)[0], "reveal_always").on_press()
    check(settings.panel_reveal == 'ALWAYS', "Reveal: Always")
    settings.panel_reveal = 'WRIST'
    vr_ui.find(menus.layout(ctx)[0], "back").on_press()
    check(menus.sheet() is None and vr_ui.find(menus.layout(ctx)[0], "tool_measure") is not None,
          "Back returns to the Tools page")
    settings.grid_show = False
    settings.nav_mode = 'WALK'

    # B in Tools mode: removes the last measurement / switches camera.
    measure.add(ctx.scene, (0, 0, 0), (1, 0, 0))
    measure.add(ctx.scene, (0, 0, 0), (2, 0, 0))
    op = type("H", (), {"execute": operators.VRSCULPT_OT_cycle_brush.execute, "left_hand": False,
                        "report": lambda *a: None})()
    op.execute(ctx)
    check(len(measure.stored(ctx.scene)) == 1, "B removes the last measurement")
    settings.tool = 'CAMERA'
    cam = ctx.scene.camera
    op.execute(ctx)
    check(ctx.scene.camera != cam, "B switches to the next camera")
    op.left_hand = True
    op.execute(ctx)
    check(settings.mode == 'SCULPT', "Y after Tools goes back to Sculpt")
    measure.clear(ctx.scene)

    # The trigger picks the tool's handler.
    menus.set_mode(ctx, 'TOOLS')
    settings.show_panel = False
    pose = Matrix.Translation((0, 0, 1))
    settings.tool = 'MEASURE'
    hnd = operators._pick_handler(ctx, settings, actions.HANDS[1], pose, pose, 1.0, 1.0)
    check(isinstance(hnd, measure.Measure), "Measure tool: trigger measures")
    hnd.finish(ctx)
    settings.tool = 'CAMERA'
    hnd = operators._pick_handler(ctx, settings, actions.HANDS[1], pose, pose, 1.0, 1.0)
    check(isinstance(hnd, camera.AimCamera), "Camera tool: trigger aims the camera")
    settings.show_panel = True


def main():
    vr_sculpt.register()
    try:
        for t in (test_grid, test_symmetry, test_measure, test_camera, test_navigation, test_tracking,
                  test_desktop_screen, test_desktop_input, test_tools_page):
            try:
                t()
            except Exception:
                traceback.print_exc()
                failures.append(t.__name__ + " raised")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")


main()
