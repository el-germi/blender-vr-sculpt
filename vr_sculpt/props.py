import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty, StringProperty

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
        ),
        default='SCULPT',
    )
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
    primitive: StringProperty(
        name="Add",
        default="mesh.primitive_cube_add",
        description="Add > Mesh entry the trigger creates in Model mode (see catalog.py)",
    )
    show_panel: BoolProperty(name="Show Hand Panel", description="Menu panel above the left controller", default=True)
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
    grab_rotate: BoolProperty(name="Grab Rotates", description="Grab follows controller rotation, not only position", default=True)
    left_inverts: BoolProperty(name="Left Hand Inverts", description="Left trigger subtracts with Draw and Inflate", default=True)
    tip_offset: FloatProperty(
        name="Tip Offset",
        description="Distance of the brush in front of the controller (meters, hand space)",
        default=0.04, min=0.0, soft_max=0.5, subtype='DISTANCE', unit='LENGTH',
    )
    show_cursor: BoolProperty(name="Show Brush Cursor", default=True)
    show_brush_card: BoolProperty(
        name="Show Brush Card",
        description="Show the brush icon and name above the right controller, plus a short description after switching",
        default=True,
    )


def register():
    bpy.utils.register_class(VRSculptSettings)
    bpy.types.Scene.vr_sculpt = PointerProperty(type=VRSculptSettings)


def unregister():
    del bpy.types.Scene.vr_sculpt
    bpy.utils.unregister_class(VRSculptSettings)
