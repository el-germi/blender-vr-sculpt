import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty

from .strokes import PALETTE

BRUSH_ITEMS = (
    ('DRAW', "Draw", "Push the surface out along its normal (left hand pushes in)"),
    ('SMOOTH', "Smooth", "Relax the surface"),
    ('GRAB', "Grab", "Grab vertices and move/rotate them with the controller"),
    ('INFLATE', "Inflate", "Push each vertex along its own normal (left hand deflates)"),
)

RADIUS_STEPS = (0.01, 0.02, 0.04, 0.08, 0.16)


class VRSculptSettings(bpy.types.PropertyGroup):
    enabled: BoolProperty(
        name="Sculpt With Triggers",
        description=(
            "Replace trigger teleport with sculpting while a VR session runs. "
            "Applied when the VR session starts"
        ),
        default=True,
    )
    mode: EnumProperty(
        name="Mode",
        items=(
            ('SCULPT', "Sculpt", "Trigger sculpts the active mesh"),
            ('MODEL', "Model", "Trigger creates primitives in empty space and grabs objects it touches"),
            ('DRAW', "Draw", "Trigger draws freehand 3D strokes"),
            ('PAINT', "Paint", "Trigger paints the active mesh's texture with Blender's texture paint brushes"),
            ('POSE', "Pose", "Trigger poses the bones of the active armature (FK or IK); puppeteer bones with "
                             "your hands and record takes"),
            ('TOOLS', "Tools", "Tape measure and scene camera, plus grid, desktop window and navigation options"),
        ),
        default='SCULPT',
    )
    # Paint mode.
    paint_tool: EnumProperty(
        name="Paint Tool",
        items=(
            ('PAINT', "Paint", "Paint with the active texture paint brush"),
            ('PICK', "Pick Color", "Take the texture's color under the tip (then back to painting)"),
        ),
        default='PAINT',
    )
    paint_reach: EnumProperty(
        name="Reach",
        items=(
            ('CONTACT', "Contact", "Paint where the tip touches the surface, like a brush"),
            ('SPRAY', "Spray", "Paint where the controller points, at any distance, like a spray can"),
        ),
        default='CONTACT',
    )
    paint_radius: FloatProperty(
        name="Paint Radius",
        description="Paint brush radius in your hand's space (meters). Stays the same size when you scale the world",
        default=0.02, min=0.001, soft_max=0.2, subtype='DISTANCE', unit='LENGTH',
    )
    paint_texture_size: EnumProperty(
        name="Texture Size",
        description="Size of the image made by Set Up for Painting",
        items=(('1024', "1K", "1024 x 1024"), ('2048', "2K", "2048 x 2048"), ('4096', "4K", "4096 x 4096")),
        default='2048',
    )
    # Pose mode.
    pose_tool: EnumProperty(
        name="Pose Tool",
        items=(
            ('FK', "FK", "The grabbed bone turns about its head to follow the tip; twist the controller to roll it. "
                         "A free bone grabbed near its head moves instead"),
            ('IK', "IK", "The grabbed bone follows the controller and its parent bones bend to reach"),
        ),
        default='FK',
    )
    pose_chain: IntProperty(
        name="IK Chain", description="Parent bones that bend to reach in IK (for a hand: forearm and upper arm = 2)",
        default=2, min=1, max=8)
    pose_mirror: BoolProperty(
        name="Mirror Pose", description="Posing a .L/.R bone poses its counterpart the same way, mirrored across X")
    pose_show_bones: BoolProperty(
        name="Show Bones", description="Draw the posed rig's bones on top of everything in VR", default=True)
    # Tools mode.
    tool: EnumProperty(
        name="Tool",
        items=(
            ('MEASURE', "Measure", "Drag with the trigger to measure distances"),
            ('CAMERA', "Camera", "Hold the trigger to aim the scene camera with your controller"),
        ),
        default='MEASURE',
    )
    show_measures: BoolProperty(name="Show Measurements", description="Show tape measurements in VR", default=True)
    show_new_camera: BoolProperty(
        name="New Camera Button",
        description="Show the \"New here\" button on the hand panel's Camera tool, which adds a camera at your right hand",
        default=True,
    )
    camera_preview: BoolProperty(
        name="Camera Preview",
        description="Show what the scene camera sees next to the hand panel, in every mode",
        default=False,
    )
    # Animation.
    show_timeline: BoolProperty(
        name="Timeline", description="Timeline, keying and playback controls at the bottom of the hand panel",
        default=True,
    )
    show_paths: BoolProperty(
        name="Motion Paths",
        description="Show the selected objects' motion paths in VR; grab a key marker to move that key",
    )
    onion: BoolProperty(name="Onion Skin", description="Show see-through copies of the selection at other frames")
    onion_mode: EnumProperty(
        name="Onion Frames",
        items=(
            ('KEYS', "Keyframes", "Ghosts at the previous and next keyframes"),
            ('FRAMES', "Frame Step", "Ghosts every few frames before and after the current one"),
        ),
        default='KEYS',
    )
    onion_count: IntProperty(name="Onion Count", description="Ghosts on each side", default=1, min=1, max=4)
    onion_step: IntProperty(name="Onion Step", description="Frames between ghosts (Frame Step)", default=5, min=1,
                            soft_max=24)
    # 3D grid.
    grid_show: BoolProperty(name="Show Grid", description="Show a 3D grid of points around the controller tips")
    grid_snap: BoolProperty(
        name="Snap to Grid",
        description="Snap new primitives, grabbed objects and tape measure ends to the 3D grid",
    )
    grid_size: FloatProperty(
        name="Grid Size", description="Spacing of the 3D grid (scene units)",
        default=0.05, min=0.001, soft_max=1.0, subtype='DISTANCE', unit='LENGTH')
    # Navigation (thumbsticks).
    nav_mode: EnumProperty(
        name="Navigation",
        items=(
            ('WALK', "Walk", "Left stick walks on the ground (follows floors and stairs), right stick snap-turns"),
            ('FLY', "Fly", "Left stick flies where you look, right stick goes up/down and turns smoothly"),
        ),
        default='WALK',
    )
    nav_speed: FloatProperty(
        name="Move Speed", description="Thumbstick movement speed at full tilt (meters per second, hand space)",
        default=1.5, min=0.1, soft_max=10.0, unit='VELOCITY')
    nav_turn: FloatProperty(
        name="Snap Turn", description="Angle of one snap turn in Walk mode",
        default=0.5235988, min=0.0872665, max=1.5707963, subtype='ANGLE')
    # Tracking objects.
    tracking: BoolProperty(
        name="Tracking Objects",
        description=(
            "Keep the empties 'VR Headset', 'VR Hand Left' and 'VR Hand Right' at the tracked poses "
            "(with trigger, grip and stick values as custom properties), for parenting and drivers"
        ),
    )
    tracking_record: BoolProperty(
        name="Record Tracking",
        description="While the timeline plays, keyframe the tracking objects on every frame",
    )
    # Floating desktop window.
    show_desktop: BoolProperty(
        name="Desktop Window",
        description="Show the Blender window on a floating screen in VR (view only)",
    )
    desktop_interactive: BoolProperty(
        name="Use From VR",
        description=(
            "Windows: the right controller drives the real mouse while it points at the desktop screen "
            "(trigger = left button, left trigger = middle button, B = right click, right stick = wheel)"
        ),
        default=True,
    )
    desktop_keyboard: BoolProperty(name="Keyboard", description="Show a virtual keyboard under the desktop screen")
    desktop_size: FloatProperty(
        name="Desktop Size", description="Width of the floating desktop screen (meters, hand space)",
        default=0.9, min=0.2, soft_max=3.0, subtype='DISTANCE', unit='LENGTH')
    # Draw mode.
    draw_tool: EnumProperty(
        name="Tool",
        items=(
            ('DRAW', "Draw", "Draw strokes"),
            ('EDIT', "Edit", "Pull part of a stroke into a new shape"),
            ('ERASE', "Erase", "Erase the strokes and notes the tip touches"),
        ),
        default='DRAW',
    )
    draw_style: EnumProperty(
        name="Stroke",
        items=(
            ('TUBE', "Tube", "Round tube mesh; trigger pressure sets the thickness"),
            ('RIBBON', "Ribbon", "Flat band mesh that follows your wrist's roll"),
            ('NOTE', "Note", "Annotation line (not geometry), shown in the headset and on the desktop"),
        ),
        default='TUBE',
    )
    draw_color: EnumProperty(
        name="Color",
        items=[(ident, name, "") for ident, name, _rgb in PALETTE],
        default='WHITE',
    )
    draw_width: FloatProperty(
        name="Width", description="Stroke width in your hand's space (meters)",
        default=0.008, min=0.0005, soft_max=0.1, subtype='DISTANCE', unit='LENGTH')
    draw_smoothing: FloatProperty(
        name="Smoothing", description="Stabilizer: how much hand jitter is filtered out",
        default=0.5, min=0.0, max=0.95, subtype='FACTOR')
    draw_thickness: EnumProperty(
        name="Thickness",
        description="What varies the stroke's width along its length",
        items=(
            ('PRESSURE', "Pressure", "Squeeze the trigger harder for a thicker stroke"),
            ('SPEED_THIN', "Speed: fast = thin", "Faster hand movement draws thinner, like a calligraphy brush"),
            ('SPEED_THICK', "Speed: fast = thick", "Faster hand movement draws thicker"),
            ('TAPER', "Taper", "Thin at both ends, ramping smoothly to full width; "
                               "the ramps are a share of the stroke's length"),
            ('FIXED', "Fixed", "Always the Width setting"),
        ),
        default='PRESSURE',
    )
    draw_taper: FloatProperty(
        name="Taper Length",
        description="Share of the stroke's length each end takes to ramp up to full width",
        default=0.3, min=0.05, max=0.5, subtype='FACTOR')
    mirror_x: BoolProperty(name="Mirror X", description="Mirror strokes across the 3D cursor's X plane")
    mirror_y: BoolProperty(name="Mirror Y", description="Mirror strokes across the 3D cursor's Y plane")
    mirror_z: BoolProperty(name="Mirror Z", description="Mirror strokes across the 3D cursor's Z plane")
    model_add_as: EnumProperty(
        name="New As",
        description="What a primitive dragged out in Model mode becomes",
        items=(
            ('OBJECT', "Object", "A new object"),
            ('DIFFERENCE', "Cut", "A cutter that cuts its shape out of the selected object (live Boolean)"),
            ('UNION', "Union", "A cutter whose shape is added to the selected object (live Boolean)"),
            ('INTERSECT', "Intersect", "A cutter that keeps only where it overlaps the selected object (live Boolean)"),
        ),
        default='OBJECT',
    )
    model_page: EnumProperty(
        name="Page",
        description="Which settings of the selected object the hand panel shows",
        items=(
            ('SHAPE', "Shape", "Primitive settings, mirror and solidify"),
            ('BOOLEAN', "Cuts", "Cutters and booleans"),
            ('FINISH', "Finish", "Bevel and smooth shading"),
            ('ARRAY', "Array", "Linear and radial arrays"),
        ),
        default='SHAPE',
    )
    model_mark: EnumProperty(
        name="Mark Edges",
        description="While on, the trigger marks the edges it touches on the selected object instead of grabbing "
                    "or creating (left hand clears)",
        items=(
            ('NONE', "Off", "The trigger grabs and creates"),
            ('HARD', "Hard", "Touched edges shade sharp, even below the Auto angle"),
            ('SOFT', "Soft", "Touched edges shade smooth, even above the Auto angle"),
            ('CLEAR', "Clear", "Touched edges go back to following the shading mode"),
        ),
        default='NONE',
    )
    snap_surface: BoolProperty(
        name="Snap to Surface",
        description="A grabbed object near another mesh sits on its surface, turned to its normal "
                    "(a cutter sinks halfway in)",
    )
    snap_angle: BoolProperty(
        name="Snap Rotation", description="Grabbed objects turn in 15 degree steps")
    primitive: StringProperty(
        name="Add",
        default="mesh.primitive_cube_add",
        description="Add > Mesh entry the trigger creates in Model mode (see catalog.py)",
    )
    show_panel: BoolProperty(name="Show Hand Panel", description="Menu panel above the left controller", default=True)
    panel_pin: BoolProperty(
        name="Pin Hand Panel",
        description="Keep the hand panel shown, whatever the left wrist does (left stick click)",
        default=False,
    )
    panel_reveal: EnumProperty(
        name="Reveal",
        description="When the hand panel shows, unless it's pinned",
        items=(
            ('WRIST', "Wrist", "Turn the back of your left hand toward your face, like checking a watch, "
                               "and look at it"),
            ('LOOK', "Look", "Look at your left hand"),
            ('ALWAYS', "Always", "Always shown"),
        ),
        default='WRIST',
    )
    engine: EnumProperty(
        name="Engine",
        items=(
            ('PYTHON', "Python Brushes", "This add-on's own numpy brushes, in Object Mode"),
            ('NATIVE', "Native (Experimental)",
             "Blender's own active sculpt brush, in Sculpt Mode. "
             "Keep the object visible in the desktop 3D viewport"),
        ),
        default='PYTHON',
    )
    brush: EnumProperty(name="Brush", items=BRUSH_ITEMS, default='DRAW')
    radius: FloatProperty(
        name="Radius",
        description="Brush radius in your hand's space (meters). Stays the same size when you scale the world",
        default=0.04, min=0.002, soft_max=0.3, subtype='DISTANCE', unit='LENGTH',
    )
    strength: FloatProperty(name="Strength", default=0.5, min=0.0, max=2.0, subtype='FACTOR')
    symmetry_x: BoolProperty(name="X Symmetry", description="Mirror strokes across the object's local X axis")
    symmetry_y: BoolProperty(name="Y Symmetry", description="Mirror strokes across the object's local Y axis")
    symmetry_z: BoolProperty(name="Z Symmetry", description="Mirror strokes across the object's local Z axis")
    grab_rotate: BoolProperty(name="Grab Rotates", description="Grab follows controller rotation, not only position", default=True)
    grab_deadzone: FloatProperty(
        name="Grab Threshold",
        description=(
            "How far the hand has to move (meters, hand space) before a grabbed object or bone starts to follow, "
            "so a trigger press meant to select doesn't nudge it. Turning the controller past a few degrees "
            "counts too"
        ),
        default=0.015, min=0.0, soft_max=0.1, subtype='DISTANCE', unit='LENGTH')
    grab_delay: FloatProperty(
        name="Grab Delay",
        description=(
            "Holding the trigger this long (seconds) also starts the move, for small precise moves. "
            "0 = only the threshold"
        ),
        default=0.35, min=0.0, soft_max=2.0, subtype='TIME_ABSOLUTE', unit='TIME_ABSOLUTE')
    left_inverts: BoolProperty(name="Left Hand Inverts", description="Left trigger subtracts with Draw and Inflate", default=True)
    tip_offset: FloatProperty(
        name="Tip Offset",
        description="Distance of the brush in front of the controller (meters, hand space)",
        default=0.04, min=0.0, soft_max=0.5, subtype='DISTANCE', unit='LENGTH',
    )
    show_cursor: BoolProperty(name="Show Brush Cursor", default=True)
    own_controllers: BoolProperty(
        name="Stylized Controllers",
        description="Draw this add-on's controllers (pressed buttons light up) instead of Blender's, which "
                    "are gray spheres when the runtime gives no model (SteamVR)",
        default=True,
        update=lambda self, context: _controllers_option(self, context),
    )
    controller_hints: BoolProperty(
        name="Button Hints",
        description="Beside each controller, what its buttons do right now (shows when you look at the hand "
                    "or hold it still)",
        default=True,
    )
    show_brush_card: BoolProperty(
        name="Show Tip Status",
        description="Show the mode, tool and size in a slim line above the right controller, plus a short "
                    "description after switching",
        default=True,
    )


def _controllers_option(settings, context):
    from . import controllers
    controllers.on_option(settings, context)


def register():
    bpy.utils.register_class(VRSculptSettings)
    bpy.types.Scene.vr_sculpt = PointerProperty(type=VRSculptSettings)


def unregister():
    del bpy.types.Scene.vr_sculpt
    bpy.utils.unregister_class(VRSculptSettings)
