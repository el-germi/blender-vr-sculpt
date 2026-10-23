# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

VR Sculpt is a pure-Python Blender extension (`vr_sculpt/`, no Blender fork) for sculpting, modeling,
drawing, painting, posing, puppeteering and measuring with VR controllers inside Blender's own OpenXR
session. It targets **Blender 5.4.0 Alpha** with a Meta Quest. README.md is the real design doc: its
"How it works" section explains each module, and "What we learned about `sculpt.brush_stroke` /
`paint.image_paint`" records API behavior found by the `tests/probe_*` scripts. Read the relevant part
before changing a subsystem. `docs/ui_panel.md` maps the hand panel.

## Commands

The Blender used here is
`D:\Descargas D\BlenderLauncher\daily\blender-5.4.0-alpha+daily.3f4216e1bad1\blender.exe`
(shown as `blender` below). Run everything from the repo root.

Build and install the extension (only needed for headset runs; tests import from source):

```bash
blender --background --factory-startup --command extension build --source-dir vr_sculpt --output-dir dist
blender --background --command extension install-file -r user_default -e dist/vr_sculpt-0.1.0.zip
```

Background tests, one file per area (`test_brushes`, `test_modal`, `test_model`, `test_generic`,
`test_draw`, `test_tools`, `test_paint`, `test_anim`, `test_pose`, `test_ik_limits`,
`test_hard_surface`, `test_ui`):

```bash
blender --background --factory-startup --python tests/test_tools.py
```

- There is no test runner or pytest. Each script puts the repo root on `sys.path`, calls
  `vr_sculpt.register()`, runs its `test_*` functions from a tuple in `main()`, prints `ok` / `FAIL`
  lines and ends with `RESULT: ALL PASSED` or `RESULT: FAILED [...]`. Blender's exit code doesn't
  reflect failures: check the `RESULT:` line. To run one test, edit the tuple in `main()` temporarily.
- New tests follow the same pattern (`check(cond, msg)`, fake XR session objects like `NavSession`).
  XR state is faked in tests; nothing needs a headset.
- `test_generic.py` enables Extra Mesh Objects for its own run and checks all its Add > Mesh entries.

Windowed scripts (`*_preview.py`, `native_*.py`, `probe_capture.py`) open Blender, write
`tests/*_result.txt` and/or render `tests/*.png`. They need a .blend argument, or the splash screen
covers the render:

```bash
blender --background --factory-startup --python tests/make_test_scene.py
blender --factory-startup test_scene.blend --python tests/panel_preview.py
```

These scripts register the add-on by hand, so they must unregister it (and remove their own draw
handlers) before quitting, or Blender crashes on exit freeing GPU offscreens (see `quit_cleanly()` in
`tests/panel_preview.py`).

Headset testing is done by the user: rebuild, reinstall, then relaunch `test_scene.blend` with
stdout/stderr redirected to `logs/blender_stdout.log` and read that log afterwards. Only ever close
the Blender window titled `test_scene` (with unsaved changes its title starts with `* `); never touch
other Blender windows.

## Source conventions

- Keep `.py` files pure ASCII; write non-ASCII UI text as `\u` escapes. Don't rewrite sources with
  PowerShell 5.1 `Get-Content`/`Set-Content` (it adds a BOM and garbles non-ASCII).
- A new module must be added to both import lists in `vr_sculpt/__init__.py` (the `importlib.reload`
  branch too), and to `_modules` if it has `register()` / `unregister()`. `_modules` order is the
  registration order (`props` first).
- All add-on settings live on one property group, `bpy.context.scene.vr_sculpt`
  (`VRSculptSettings` in `props.py`); per-object state (primitive settings, stroke samples) is
  property groups / ID properties on the objects, so .blend files stay plain Blender data.

## Architecture

**Input plumbing** (`actions.py`). Action sets can't be added after a session starts and Blender
syncs only one, so an `xr_session_start_pre` handler, inserted before the stock VR Scene Inspection
add-on's, patches the stock `blender_default` action map: trigger bindings become the
`vrsculpt.stroke` modal operator, B/Y/stick clicks get cycling actions, and fly actions run
`vrsculpt.navigate`. Changes to bindings only apply at the next session start.

**The trigger operator** (`operators.py`, `VRSCULPT_OT_stroke`). One modal undo operator per hand
press. `_pick_handler()` is the central dispatch: in priority order it picks hand panel press, desktop
screen, motion path key, edge marking, then a per-mode handler (`interactions.MoveObject` /
`CreatePrimitive`, `drawing.pick`, `paint.pick`, `pose.pick`, `measure.Measure`,
`camera.AimCamera`). `None` means sculpt with the active engine (`brushes.py` numpy engine in Object
Mode, or `native.py` driving `sculpt.brush_stroke` in Sculpt Mode). A handler is a plain class with
`update(context, pose, tip_pose, value)`, `finish(context)` and a `changed` flag; `changed` decides
whether the operator returns FINISHED (one undo step) or CANCELLED. New trigger behavior = a new
handler class plus a branch in `_pick_handler`.

- XR action operators get a temporary context: don't keep `context` across modal events; use
  `bpy.context` each time.
- Undo guard: undo/redo/load replace Blender's data, so `undo_*` / `redo_*` / `load_post` handlers
  bump a counter (`_on_data_swap`) and a held trigger operator that sees it change lets go without
  touching what it kept. Handlers that cache Blender data (meshes, F-curves, objects) must be safe
  under this, and module caches holding Blender data are cleared there too.

**Per-frame work outside the operator.** `live.py` is the one persistent app timer (90 Hz while
something needs it, 4 Hz idle) that drives tracking empties, desktop captures, camera preview
redraws, puppet bindings and takes, motion paths and onion skin. Add continuous updates there rather
than registering new timers. `draw.py` / `hud.py` register `SpaceView3D` draw handlers on region
`'XR'` (headset) and `'WINDOW'` (desktop); everything in the headset (brush rings, controller models,
panels, motion paths, ghosts, desktop screen) is drawn with `gpu`/`blf` from there (`actions.py`
turns on the session's `show_custom_overlays` at start so the XR region runs them).

**Hand panel UI** (`vr_ui.py` widgets + hit testing, `menus.py` per-mode page contents, `gfx.py`
batched triangle drawing and icons, `presence.py` wrist-turn reveal rule, `interactions.UIPress`
press handling). The panel is a card above the left controller; the right aim ray is intersected
with its plane. Its batch is cached in panel space and only rebuilt when a widget looks different,
so keep widget state comparable and avoid per-frame allocation there. Animations are time-based.

**Model mode / hard surface.** `primitives.py` (own Cube/Cylinder etc., rebuilt with bmesh from
update callbacks), `catalog.py` (reads Blender's Add > Mesh menu, including extensions, via a
recording layout) and `generic.py` (re-runs a menu operator to regenerate). Booleans, bevel, array,
solidify are modifiers managed by `hardsurface.py`, which enforces a fixed order for our modifiers;
a cutter is identified by the Boolean modifiers pointing at it. `shading.py` owns the
"VR Shading" Geometry Nodes group and the `sharp_edge` / `vrs_soft` edge marks.

**Other modes**: `strokes.py` (mesh from stored samples, rotation-minimizing frames) +
`drawing.py` for Draw; `paint.py` paints by temporarily aiming the largest desktop 3D view at each
dab; `pose.py` (FK/IK via CCD, limits) + `puppet.py` (bindings, recorded takes via
`frame_change_post`); `anim.py`, `paths.py`, `onion.py` for the timeline strip, motion paths and
mesh onion skin (5.x slotted actions: F-curves come from the slot's channelbag via
`bpy_extras.anim_utils`); `desktop.py` + `winput.py` for the floating desktop window (GDI capture
and `SendInput` on Windows, region read-back elsewhere).

## Branches

Features are developed on `feature/*` branches and merged to `main` by PR on
github.com/el-germi/blender-vr-sculpt. The `feature/ui-panel`, `feature/ui-radial` and
`feature/ui-spatial` branches are the three UI reworks compared in the headset; `ui-panel` is the
one merged.
