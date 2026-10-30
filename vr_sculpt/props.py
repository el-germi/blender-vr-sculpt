import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty

from .strokes import PALETTE

BRUSH_ITEMS = (
    ('DRAW', "Draw", "Push the surface out along its normal (other hand pushes in)"),
    ('SMOOTH', "Smooth", "Relax the surface"),
    ('GRAB', "Grab", "Grab vertices and move/rotate them with the controller"),
    ('INFLATE', "Inflate", "Push each vertex along its own normal (other hand deflates)"),
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
            # Last, so files saved before Rig mode keep their mode (enum values are stored by position).
            ('RIG', "Rig", "Build an armature with the controllers, bind a mesh to it with automatic weights and "
                           "paint its weights"),
            ('PHYSICS', "Physics", "Set up rigid bodies, cloth, soft bodies and spring bones, run the simulation "
                                   "and push, pull and throw things with your hands"),
            ('RETOPO', "Retopo", "Draw new low-poly topology on the surface of the active mesh: strips, quads, "
                                 "relax and tweak, with Mirror X"),
        ),
        default='SCULPT',
    )
    # Physics mode.
    physics_hands: BoolProperty(
        name="Hands",
        description="Physics mode: the controllers take part in the simulation. They push bodies, cloth, soft "
                    "bodies and spring bones, and the trigger grabs and throws, or pushes and pulls",
    )
    physics_tool: EnumProperty(
        name="Hand Tool",
        items=(
            ('GRAB', "Grab", "The trigger grabs rigid bodies and spring bones; let go to throw"),
            ('FORCE', "Push / Pull", "The trigger is a force at your hand: the right one pulls things in, the left "
                                     "one pushes them away (squeeze harder for more)"),
        ),
        default='GRAB',
    )
    physics_force: FloatProperty(
        name="Force", description="Strength of the hand's push or pull at full trigger (a force field's strength)",
        default=300.0, min=0.0, soft_max=2000.0)
    # Retopo mode.
    retopo_tool: EnumProperty(
        name="Retopo Tool",
        items=(
            ('STRIP', "Strip", "Drag along the surface to lay a strip of quads, the size wide. Start on a border "
                               "edge to carry it on"),
            ('QUAD', "Quad", "Click to place vertices: four make a quad, then two more the next one. Drag a "
                             "border edge to pull out a quad, drag a vertex to slide it (onto another: merge)"),
            ('RELAX', "Relax", "Brush to even out the spacing of the vertices, keeping them on the surface"),
            ('TWEAK', "Tweak", "Grab the vertices in reach and slide them along the surface"),
        ),
        default='STRIP',
    )
    retopo_size: FloatProperty(
        name="Retopo Size",
        description="Strip width (and quad size), and the Relax and Tweak radius, in your hand's space (meters)",
        default=0.02, min=0.003, soft_max=0.2, subtype='DISTANCE', unit='LENGTH')
    retopo_mirror: BoolProperty(
        name="Mirror Retopo",
        description="Everything made, moved or deleted on one side of the reference's local X is mirrored to the "
                    "other; vertices near the center line stay on it")
    retopo_opacity: FloatProperty(
        name="Face Opacity", description="How much the retopo mesh's faces hide the reference in VR",
        default=0.35, min=0.0, max=1.0, subtype='FACTOR')
    retopo_relax: FloatProperty(name="Relax Strength", default=0.5, min=0.0, max=1.0, subtype='FACTOR')
    retopo_reference: PointerProperty(
        type=bpy.types.Object, name="Retopo Reference",
        description="The mesh Retopo mode draws new topology on (the active mesh when Retopo starts)",
        poll=lambda self, obj: obj.type == 'MESH')
    retopo_object: PointerProperty(
        type=bpy.types.Object, name="Retopo Mesh",
        description="The retopo mesh being edited; empty, the next stroke starts a new one",
        poll=lambda self, obj: obj.type == 'MESH')
    # Rig mode.
    rig_tool: EnumProperty(
        name="Rig Tool",
        items=(
            ('BONES', "Bones", "Each trigger press places the next joint of a bone chain. Press a joint to carry "
                               "on (branch) from it, drag it to move it. The other trigger deletes a bone"),
            ('WEIGHTS', "Weights", "Paint the active bone's weights on the bound mesh. Touch a bone to pick it"),
        ),
        default='BONES',
    )
    rig_mirror: BoolProperty(
        name="Mirror Bones", description="Bones off the rig's X = 0 plane are built and edited in .L / .R pairs")
    rig_mesh: PointerProperty(
        type=bpy.types.Object, name="Rig Mesh", description="The mesh Rig mode binds to the rig and paints weights on",
        poll=lambda self, obj: obj.type == 'MESH')
    weight_brush: EnumProperty(
        name="Weight Brush",
        items=(
            ('ADD', "Add", "Add weight (other hand subtracts)"),
            ('SUBTRACT', "Subtract", "Take weight away (other hand adds)"),
            ('SMOOTH', "Smooth", "Even out the weights with their neighbors"),
        ),
        default='ADD',
    )
    weight_radius: FloatProperty(
        name="Weight Radius", description="Weight brush radius in your hand's space (meters)",
        default=0.04, min=0.002, soft_max=0.3, subtype='DISTANCE', unit='LENGTH')
    weight_strength: FloatProperty(name="Weight Strength", default=0.5, min=0.0, max=1.0, subtype='FACTOR')
    weight_mirror: BoolProperty(
        name="Mirror Weights",
        description="Painting one side of the mesh paints the mirrored side (across its local X) on the .L / .R "
                    "counterpart's group")
    weight_show: BoolProperty(
        name="Show Weights", description="Show the active bone's weights on the mesh in VR, blue (0) to red (1)",
        default=True)
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
            ('LIGHT', "Light", "Press in empty space to place a light, aimed where you point; grab a light to move and "
                               "aim it. The other hand selects"),
            ('REFS', "Refs", "Reference images: press in empty space to place the chosen picture facing you; grab one "
                             "to move it"),
        ),
        default='MEASURE',
    )
    show_measures: BoolProperty(name="Show Measurements", description="Show tape measurements in VR", default=True)
    show_new_camera: BoolProperty(
        name="New Camera Button",
        description="Show the \"New here\" button on the hand panel's Camera tool, which adds a camera at your main hand",
        default=True,
    )
    camera_preview: BoolProperty(
        name="Camera Preview",
        description="Show what the scene camera sees next to the hand panel, in every mode",
        default=False,
    )
    # Look dev.
    light_type: EnumProperty(
        name="New Light",
        description="What the Light tool places",
        items=(
            ('POINT', "Point", "Shines every way from a point, like a bulb"),
            ('SUN', "Sun", "Parallel light from far away, along where it points; its place doesn't matter"),
            ('SPOT', "Spot", "A cone of light where it points"),
            ('AREA', "Area", "Soft light from a panel, off its front face"),
        ),
        default='POINT',
    )
    light_color: bpy.props.FloatVectorProperty(
        name="Light Color", description="Color of new lights", subtype='COLOR_GAMMA', size=3, min=0.0, max=1.0,
        default=(1.0, 1.0, 1.0))
    light_power: FloatProperty(
        name="Light Power", description="Power of new point, spot and area lights (watts)",
        default=20.0, min=0.0, soft_max=2000.0, unit='POWER')
    light_sun: FloatProperty(
        name="Sun Strength", description="Strength of new sun lights (watts per square meter)",
        default=3.0, min=0.0, soft_max=50.0)
    light_radius: FloatProperty(
        name="Light Radius", description="Radius of new point and spot lights: bigger is softer shadows",
        default=0.05, min=0.0, soft_max=1.0, subtype='DISTANCE', unit='LENGTH')
    light_area: FloatProperty(
        name="Area Size", description="Side of new area lights",
        default=0.25, min=0.001, soft_max=2.0, subtype='DISTANCE', unit='LENGTH')
    light_angle: FloatProperty(
        name="Sun Angle", description="Angular size of new sun lights: bigger is softer shadows",
        default=0.0174533, min=0.0, max=3.14159, subtype='ANGLE')
    light_spot: FloatProperty(
        name="Spot Cone", description="Cone angle of new spot lights",
        default=0.7853982, min=0.0174533, max=3.14159, subtype='ANGLE')
    # Reference images (Tools > Refs).
    refs_folder: StringProperty(
        name="Refs Folder",
        description="Folder of reference images the Refs tool lists. Empty: the .blend file's folder",
        subtype='DIR_PATH',
    )
    ref_image: StringProperty(
        name="Reference Image", description="The picture the Refs tool places", subtype='FILE_PATH')
    ref_size: FloatProperty(
        name="Reference Size", description="Longer side of new reference pictures",
        default=0.5, min=0.02, soft_max=5.0, subtype='DISTANCE', unit='LENGTH')
    refs_show: BoolProperty(
        name="Show References", description="Draw reference pictures in the headset (the desktop draws them anyway)",
        default=True)
    ref_pin: StringProperty(name="Pinned Reference", description="The reference shown beside the hand panel")
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
    nav_grab_turn: EnumProperty(
        name="Grip",
        description="What holding the world with the grips does besides moving it (both grips also zoom)",
        items=(
            ('OFF', "Move", "The world only moves with the hand, like Blender's own grab"),
            ('TURN', "Turn", "The world also turns with the wrist (or the line between both hands) about "
                             "the vertical axis; the horizon stays level"),
            ('FREE', "Free", "One hand turns the world every way the wrist does, tilting it too"),
        ),
        default='OFF',
    )
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
            "Windows: the main controller drives the real mouse while it points at the desktop screen "
            "(trigger = left button, other trigger = middle button, B = right click, main stick = wheel; "
            "Y and the left stick when left-handed)"
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
            ('PENCIL', "Pencil", "Grease Pencil stroke: renders, and animates frame by frame (with Auto Keying, "
                                 "drawing on an unkeyed frame keys a new one)"),
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
    # Smoothing (every trigger stroke: Draw, Paint, Sculpt, weights, seams, retopo strips).
    smooth_strokes: BoolProperty(
        name="Smooth Strokes",
        description="Filter hand jitter out of strokes. Off, strokes follow the controller exactly, shake and all",
        default=True,
    )
    smooth_amount: FloatProperty(
        name="Amount", description="How much jitter is smoothed away; more also lags a little behind fast moves",
        default=0.5, min=0.0, max=1.0, subtype='FACTOR')
    smooth_lag: FloatProperty(
        name="Lag",
        description="Lazy string: the stroke only follows once the tip is this far from it (hand-space meters), "
                    "for straighter lines and cleaner corners. 0 is off",
        default=0.0, min=0.0, max=0.05, subtype='DISTANCE', unit='LENGTH')
    take_smooth: BoolProperty(
        name="Smooth Takes", description="Smooth the keys of a puppet take when it ends", default=True)
    take_smooth_frames: FloatProperty(
        name="Take Smoothing", description="How widely a take's keys are smoothed, in frames",
        default=1.5, min=0.25, max=8.0)
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
            ('STACK', "Stack", "Every modifier: show, apply, remove, settings"),
            ('UV', "UV", "Seams, unwrapping and the checker / stretch view"),
            ('MATERIAL', "Material", "Base color, roughness, metallic and presets of the object's first material"),
        ),
        default='SHAPE',
    )
    uv_tool: EnumProperty(
        name="UV Tool",
        description="While on, the trigger works on the selected object's UVs instead of grabbing or creating",
        items=(
            ('NONE', "Off", "The trigger grabs and creates"),
            ('SEAM', "Seams", "Touched edges become UV seams (other hand clears them)"),
            ('FIX', "Fix", "Brushed faces unwrap again, stitched to the rest of the layout"),
        ),
        default='NONE',
    )
    uv_view: EnumProperty(
        name="UV View",
        description="What the UV page draws on the selected object in VR",
        items=(
            ('NONE', "Off", "Nothing"),
            ('CHECKER', "Checker", "A checker on the UVs: squares should stay square and even"),
            ('STRETCH', "Stretch", "Faces colored by UV stretch: blue is true to the surface, red twice off"),
        ),
        default='CHECKER',
    )
    uv_method: EnumProperty(
        name="Unwrap Method",
        description="How Unwrap, Live and Fix flatten the mesh",
        items=(
            ('ANGLE_BASED', "Angle", "Angle based: keeps angles, the usual choice"),
            ('CONFORMAL', "Conformal", "Least squares conformal: faster, a bit more stretch"),
            ('MINIMUM_STRETCH', "Min Stretch", "Spreads stretch as evenly as it can"),
        ),
        default='ANGLE_BASED',
    )
    uv_live: BoolProperty(
        name="Live Unwrap", default=True,
        description="Unwrap the mesh again each time a Seams stroke ends")
    uv_checker: IntProperty(
        name="Checker Squares", default=16, min=2, max=64,
        description="Squares per side of the UV square in the checker view")
    uv_fix_radius: FloatProperty(
        name="Fix Radius", default=0.03, min=0.005, max=0.2, unit='LENGTH', subtype='DISTANCE',
        description="Reach of the Fix brush (hand-space meters)")
    model_mark: EnumProperty(
        name="Mark Edges",
        description="While on, the trigger marks the edges it touches on the selected object instead of grabbing "
                    "or creating (other hand clears)",
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
        description="Add > Mesh entry (see catalog.py) or kit part (see kit.py) the trigger creates in Model mode",
    )
    kit_stamp: BoolProperty(
        name="Stamp",
        description="Kit parts: the trigger sweeps across surfaces placing a copy every Spacing, instead of "
                    "dropping one part",
    )
    kit_scale: FloatProperty(
        name="Part Size", description="Kit parts are placed at this times their own size",
        default=1.0, min=0.05, soft_max=4.0, subtype='FACTOR')
    kit_spacing: FloatProperty(
        name="Spacing", description="Distance between stamped kit parts (hand-space meters)",
        default=0.03, min=0.002, soft_max=0.3, subtype='DISTANCE', unit='LENGTH')
    show_panel: BoolProperty(name="Show Hand Panel", description="Menu panel above the other hand's controller", default=True)
    panel_pin: BoolProperty(
        name="Pin Hand Panel",
        description="Keep the hand panel shown, whatever the left wrist does (left stick click)",
        default=False,
    )
    panel_reveal: EnumProperty(
        name="Reveal",
        description="When the hand panel shows, unless it's pinned",
        items=(
            ('WRIST', "Wrist", "Turn the back of the panel's hand toward your face, like checking a watch, "
                               "and look at it"),
            ('LOOK', "Look", "Look at the panel's hand"),
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
    left_inverts: BoolProperty(
        name="Other Hand Inverts",
        description="The other hand's trigger (left, or right when left-handed) subtracts with Draw and Inflate",
        default=True)
    left_handed: BoolProperty(
        name="Left Handed",
        description="Swap the hands: the left controller sculpts, points and presses the panel, the panel sits over "
                    "the right one, and the face buttons, stick clicks and sticks swap jobs too",
        default=False)
    dyntopo_auto_texture: BoolProperty(
        name="Auto Texture",
        description="With dyntopo, show the texture again 2 s after the last stroke (a short freeze while it's restored)",
        default=False)
    tip_offset: FloatProperty(
        name="Tip Offset",
        description="Distance of the brush in front of the controller (meters, hand space)",
        default=0.04, min=0.0, soft_max=0.5, subtype='DISTANCE', unit='LENGTH',
    )
    show_cursor: BoolProperty(name="Show Brush Cursor", default=True)
    own_controllers: BoolProperty(
        name="Controller Models",
        description="Draw the controllers (pressed buttons move and light up) instead of Blender's, which are "
                    "gray spheres when the runtime gives no model (SteamVR): the runtime's own models once got "
                    "from it (VR Extras), else Meta's Quest Pro controllers",
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
        description="Show the mode, tool and size in a slim line above the main controller, plus a short "
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
