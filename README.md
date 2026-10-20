# VR Sculpt for Blender (prototype)

Sculpt, model, draw and measure with VR controllers inside Blender's own VR session. Pure Python
extension, no Blender fork. Built and tested on **Blender 5.4.0 Alpha** with a
Meta Quest over OpenXR.

![Brush rings and the brush card, native engine](docs/overlay_native.png)

## Features

- **Trigger to sculpt**, pressure-sensitive. The left hand does the opposite
  (push in, deflate, ...) where that makes sense.
- **Two engines**
  - **Python brushes** (Object Mode): this add-on's own numpy brushes:
    Draw, Smooth, Grab (follows controller rotation), Inflate. X / Y / Z
    symmetry, works on moved, rotated and scaled objects.
  - **Native (experimental)** (Sculpt Mode): drives Blender's real sculpt
    brushes from the controller tip: Draw, Clay Strips, Smooth,
    Inflate/Deflate, Crease Sharp, Flatten/Contrast.
- **Brush cursor in the headset**: rings at each controller tip, colored per
  brush (blue when that hand inverts).
- **Brush card** above the right controller: brush icon and name, plus a
  one-line description for a few seconds after switching.
- **Haptics** on contact with the surface.
- One undo step per stroke (Python engine).
- **Model mode**: drag out any Add > Mesh primitive in empty space, grab
  objects to move and rotate them, and edit the primitive's settings
  afterwards. Primitives stay editable until you sculpt them.
- **Every Add > Mesh entry, including extensions.** The list is read from
  Blender's own Add > Mesh menu, so primitives that extensions add there show
  up too: with Extra Mesh Objects that's 42 entries (gears, pipe joints,
  gemstones, torus knots, rocks, ...). Their settings panels are built from
  the operators' own properties.
- **Draw mode**: freehand 3D strokes that become real meshes, like Gravity
  Sketch. Tube (round, never twists) and Ribbon (flat, follows your wrist's
  roll) strokes, plus Note strokes that are Blender annotations. Thickness
  from trigger pressure, from hand speed (fast = thin or fast = thick;
  averaged over ~0.3 s, eased up to 2 m/s, changing gradually along the
  stroke), Taper (thin tips ramping to full width over a share of the
  stroke's length), or fixed. Edit pulls part of a stroke into a new shape; Erase sweeps
  strokes and notes away. Mirror on X / Y / Z around the 3D cursor (editing
  one side updates the other), a stabilizer, 8 colors, and B to undo the last
  stroke.
- **Tools mode**:
  - **Tape measure**: drag to measure. Ends snap to nearby mesh vertices (or
    the grid), labels use the scene's units, and measurements stay until you
    remove them (B removes the last one).
  - **Scene camera**: hold the trigger and the camera follows your controller
    (it looks where you point), with a live **camera preview** above your
    hand. Switch or add cameras and set the lens from the panel. The preview
    can also be pinned next to the hand panel in every mode.
- **3D grid and snapping**: a lattice of points around the controller tips,
  in scene units. Snap to Grid snaps new primitives, grabbed objects and tape
  ends.
- **Symmetry**: Model mode mirrors the selected object on X / Y / Z with a
  live Mirror modifier around a "VR Mirror Center" empty (Here puts it at
  your left hand). Sculpt mode has X / Y / Z symmetry for both engines (the
  native engine uses the mesh's own symmetry flags).
- **Walk and Fly navigation** on the thumbsticks, switchable while in VR.
  Walk keeps you level, follows floors, slopes and stairs under you (but not
  tables or walls), and snap-turns. Fly goes where you look, with up/down and
  smooth turning. The speed is in hand space, so it scales with the world.
- **Tracking objects**: "VR Headset", "VR Hand Left" and "VR Hand Right"
  empties in a "VR Tracking" collection follow the headset and controllers,
  with the trigger, grip and stick values as custom properties, for parenting
  and drivers. Record keyframes them on every frame while the timeline plays.
- **Floating desktop window**: the whole Blender window on a screen in VR,
  view only. It travels with you when you move; point at it in Tools mode
  and drag to move it.
- **Passthrough** toggle (Blender's own; needs a runtime that supports it).
- **Hand panel** above the left controller: Sculpt / Model / Draw / Tools
  tabs, engine and brush buttons, radius and strength sliders, primitive
  settings, Duplicate and Delete. Point at it with the right controller and
  pull the trigger.

![Brush cards](docs/brush_cards.png)

## Controls

| Input | Sculpt mode | Model mode | Draw mode | Tools mode |
|---|---|---|---|---|
| Right trigger | Sculpt | Drag in empty space: create. Inside an object: grab | Draw / Edit / Erase | Measure / aim the camera. Pointing at the desktop screen: move it |
| Left trigger | Sculpt, inverted for Draw / Inflate (configurable) | Same as right | Same as right | Same as right |
| Right trigger, pointing at the hand panel | Press buttons, drag sliders | Same | Same | Same |
| Y (left) | Next mode (Sculpt, Model, Draw, Tools) | Same | Same | Same |
| B (right) | Next brush | Next primitive in the Add list | Undo the last stroke | Remove the last measurement / next camera |
| Left stick | Walk / fly (move) | Same | Same | Same |
| Right stick | Walk: snap turn. Fly: up/down, turn | Same | Same | Same |
| Left stick click | Show / hide the hand panel | Same | Same | Same |
| Right stick click | Next radius (1, 2, 4, 8, 16 cm) | Same | Same | Same |
| Grip, A / X | Blender's stock VR navigation (unchanged) | Same | Same | Same |

Trigger teleport and the stock stick navigation are replaced while "Sculpt
With Triggers" is on. The setting applies when the VR session starts.

## Install

Needs Blender 5.4+ with the bundled **VR Scene Inspection** add-on enabled
and a working OpenXR runtime.

```bash
blender --background --factory-startup --command extension build --source-dir vr_sculpt --output-dir dist
```

```bash
blender --background --command extension install-file -r user_default -e dist/vr_sculpt-0.1.0.zip
```

Then in the 3D View: **Sidebar (N) > VR > Start VR Session**. The **VR
Authoring** panel in the same tab has the engine, brush, radius, strength, tip
offset and display options; **VR Extras** has the grid, navigation, camera, desktop
window, passthrough and tracking settings (including hiding the hand panel's
"New here" camera button).

- Python engine: select a mesh in **Object Mode**.
- Native engine: select a mesh in **Sculpt Mode** and keep it visible in the
  desktop 3D viewport (see below for why).

## How it works

- **Controller input.** OpenXR action sets can't be added once a session has
  started, and Blender syncs a single action set. So a
  `xr_session_start_pre` handler, inserted before the stock add-on's, edits
  the stock `blender_default` action map: it copies the teleport trigger
  bindings for every controller profile into a `vrsculpt_stroke` action
  (modal operator), and copies B/Y and stick-click bindings for brush and
  radius cycling ([actions.py](vr_sculpt/actions.py)).
- **Strokes.** [operators.py](vr_sculpt/operators.py) runs a modal operator
  per hand. XR modal events arrive every frame while the trigger is held; a
  90 Hz timer that polls the controller pose is a fallback. Errors end the
  stroke cleanly instead of leaving a timer running.
- **Python engine** ([brushes.py](vr_sculpt/brushes.py)): vertex positions in
  numpy, a hash grid for brush queries, CSR adjacency for smoothing, one
  `foreach_set` + `mesh.update()` per frame.
- **Native engine** ([native.py](vr_sculpt/native.py)): calls
  `bpy.ops.sculpt.brush_stroke` once per dab with a stroke element at the
  controller tip.
- **Model mode** ([interactions.py](vr_sculpt/interactions.py),
  [primitives.py](vr_sculpt/primitives.py)): the same trigger operator picks
  a handler per press: hand panel, grab, or create. Primitive settings are a
  property group on the object whose update callbacks rebuild the mesh with
  bmesh.
- **Draw mode** ([strokes.py](vr_sculpt/strokes.py), [drawing.py](vr_sculpt/drawing.py)):
  each stroke object keeps its raw samples (position, width, the
  controller's side vector) as an ID property in object space. The mesh is
  rebuilt from them with numpy (Catmull-Rom smoothing, tube rings or ribbon
  strips, mirrored copies) and written with `foreach_set`, so edits and
  mirroring stay live. Long strokes rebuild a lighter preview while the
  trigger is held (under 8 ms per frame for a 1200-sample mirrored tube) and
  the full mesh on release. Notes are 3D annotation strokes, one layer per
  color.
- **Stroke orientation**: each sample's frame normal is carried along the
  path (a rotation-minimizing frame), never taken straight from the
  controller. Projecting the controller's side axis across the stroke breaks
  down when you draw along that axis (the usual sideways stroke) and flips
  180 degrees, which made bow-ties and knots. Tubes are twist-free; ribbons
  turn toward the controller only when its side axis clearly stands across
  the stroke (judged over the last 2 cm, so jitter doesn't count), at most
  half a turn per 10 cm, and never flip.
- **Hand panel** ([vr_ui.py](vr_sculpt/vr_ui.py), [menus.py](vr_sculpt/menus.py)):
  rows of buttons, sliders and selects on a card that faces the headset; the
  right controller's aim ray is intersected with the card's plane. A select
  opens a picker page (categories first, then a paged grid).
- **Primitive catalog** ([catalog.py](vr_sculpt/catalog.py)): runs the Add >
  Mesh menu's draw functions (including ones extensions appended) against a
  recording layout, collecting each `layout.operator(...)` with its preset
  properties and following `layout.menu(...)` into submenus.
- **Operator primitives** ([generic.py](vr_sculpt/generic.py)): the object
  stores its menu entry and the settings changed in VR; a change re-runs the
  operator and swaps the new mesh in. Drag-to-create scales length settings,
  then corrects with a uniform object scale so the shape fills the box
  exactly. Generators slower than 40 ms (e.g. Wall Factory) regenerate on
  slider release instead of every frame.
- **Floating desktop window** ([desktop.py](vr_sculpt/desktop.py)): Python
  can't read the window's final image, but every region draws into its own
  buffer and a `POST_PIXEL` draw callback runs right after, on every editor
  type. There the region's pixels are read (`read_color`, about 0.5-2 ms per
  UI region, at most 4 times a second per region) into a texture, and the
  headset draws all of them at their window positions. Regions only redraw
  when something changes, so the captures stay current between redraws. 3D
  views are the exception: their callback only sees the overlay layer, so
  they are re-rendered offscreen from the same view at half resolution.
- **Camera preview** ([camera.py](vr_sculpt/camera.py)):
  `GPUOffScreen.draw_view3d` from the scene camera, which needs a 3D view's
  draw callback: it renders while the largest desktop 3D view redraws (a
  timer asks for 20 redraws a second while a preview is shown), and only the
  headset displays it.
- **Navigation** ([navigation.py](vr_sculpt/navigation.py)): the stock fly
  actions keep their bindings but run `vrsculpt.navigate`, which moves
  `XrSessionState.navigation_location` / `navigation_rotation` itself, so
  Walk / Fly is read live instead of being baked into the action map. Walk
  casts a ray down from the headset and follows changes in the surface
  height (the VR tracking origin isn't the real floor, so it keeps your
  height rather than putting you on the surface).
- **Tracking objects** ([tracking.py](vr_sculpt/tracking.py)): an app timer
  ([live.py](vr_sculpt/live.py)) sets the empties' matrices from the session
  state at 90 Hz and reads the input values with `action_state_get`; custom
  property changes are followed by `update_tag()` so drivers re-run.
- **Drawing in the headset** ([draw.py](vr_sculpt/draw.py),
  [hud.py](vr_sculpt/hud.py)): `SpaceView3D` draw handlers on region type
  `'XR'` (headset) and `'WINDOW'` (desktop). The session's
  `show_custom_overlays` is turned on at session start. Text is drawn in 3D
  with `blf` under a `gpu.matrix` transform; brush icons come from the brush
  asset previews.

### What we learned about `sculpt.brush_stroke` (5.4.0 Alpha)

Found with the probes in [tests/](tests/):

- Each stroke element's **`location` is used as the dab position**, so dabs
  land at the controller tip, including on parts hidden from the desktop
  view. It is in **object space**, not world space.
- The element's **`mouse` must still be over the mesh** in a 3D viewport, or
  the dab is silently dropped. The add-on aims it at the mesh vertex nearest
  the tip.
- The element's **`size` is ignored**. The radius comes from the unified paint
  settings, even with unified size off. With the size locked to `'SCENE'`,
  `unprojected_size` is a **diameter in object space**.
- The poll needs Sculpt Mode, an active brush and a **brush-type tool active
  in the target viewport**.
- **Directional brushes** (Clay Strips, ...) do nothing with one-element
  strokes. Each call gets a zero-pressure element at the previous tip first.
- **XR action operators get a temporary context**: don't keep `context`
  across modal events. Use `bpy.context` each time.
- Python `Event` objects have no `timer` attribute.

## Performance

Measured on the test machine.

| Mesh | Python engine, per frame | Native engine, per dab |
|---|---|---|
| 41k verts | ~0.2 ms math, ~5 ms with viewport redraw | ~0.5 ms |
| 164k verts | ~0.5 ms math, ~8 ms with viewport redraw | — |
| 655k verts | ~1.3 ms math (background, no redraw) | — |

For the Python engine, the limit is Blender rebuilding the whole mesh for
display after each update. The native engine only updates the touched part.

## Tests

Background (no window):

```bash
blender --background --factory-startup --python tests/test_brushes.py
```

```bash
blender --background --factory-startup --python tests/test_modal.py
```

```bash
blender --background --factory-startup --python tests/test_model.py
```

```bash
blender --background --factory-startup --python tests/test_generic.py
```

```bash
blender --background --factory-startup --python tests/test_draw.py
```

```bash
blender --background --factory-startup --python tests/test_tools.py
```

`test_generic.py` enables Extra Mesh Objects for its own run (factory
settings, so your preferences aren't touched) and checks all 42 entries.

Windowed (opens Blender briefly, writes `tests/*_result.txt`):
`native_engine.py`, `native_modal.py`, `native_transform.py`. The
`overlay_preview.py`, `panel_preview.py`, `hud_preview.py` and
`tools_preview.py` scripts render the overlay to PNG; `probe_capture.py`
checks region capture and offscreen camera rendering.
Run the overlay one with `test_scene.blend` loaded; create that file with
`tests/make_test_scene.py`.

## Status and limits

- Prototype. Only tested on Blender 5.4.0 Alpha with Meta Quest controllers.
- The native engine's undo granularity in VR hasn't been checked.
- Creation is aligned to the world axes. Only Cube and Cylinder (our own
  versions) size non-uniformly to the drag box; other primitives keep their
  proportions.
- Geometry-node asset primitives in Add > Mesh aren't listed yet.
- The desktop window in VR is view only: Python can't send clicks or key
  presses to Blender's UI. Menus and popups aren't captured (they are
  temporary regions), and nothing updates while the Blender window is
  minimized. The camera preview needs a visible desktop 3D view too.
- Passthrough is Blender's own `show_passthrough`; whether it shows depends
  on the OpenXR runtime.
- Hand tracking isn't possible from an add-on: Blender's VR layer doesn't
  enable OpenXR's hand tracking or hand interaction extensions, so drawing
  uses controllers.
- Blender plans to move VR out of the add-on list into core (issue #161834),
  which may change how action maps are set up.

## License

GPL-3.0-or-later, see [LICENSE](LICENSE).
