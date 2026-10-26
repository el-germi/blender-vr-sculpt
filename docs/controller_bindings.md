# Which physical button is which (deferred)

Status: not done. Written down 2026-10-26 so it isn't researched again from scratch.

## The problem

The runtime's controller models (Get From Runtime, `vr_sculpt/controller_worker.py`) give
every button its own part and key letter, from the glTF node names (`button_a`, `button_y`,
...). What they don't say is which physical button fires which of our actions.

The add-on reads Blender's actions (`controllers.read_inputs`): the trigger, the grip, "top"
(`vrsculpt_brush`, copied from the stock B / Y binding), "bottom" (`nav_reset`, A / X), the stick
and its click. Blender's stock action map has bindings for a fixed list of interaction profiles
(Touch, Index, Vive, WMR, ...). A controller not on it, such as the Steam Frame's, is remapped by
SteamVR onto one Blender does know. Which of its buttons ends up on Touch's `/input/b/click` is
SteamVR's choice, and the user can change it in SteamVR's binding UI.

So `controller_worker.assign` guesses: the face buttons play our top / bottom ones in Touch's
layout (Y / B on top, X / A below, preferring the hand's own letters), and any other buttons are
drawn but bound to nothing (role ""). That's right for Touch-like controllers. For a controller
with A, B, X and Y on one hand, B / A light up for the right hand's actions and X / Y never do,
which may not be what SteamVR actually binds. The legend letters come from the model, so they
match whichever button lights.

## What OpenXR offers

Neither `XR_EXT_render_model` nor `XR_EXT_interaction_render_model` maps nodes to input paths.
The runtime animates the nodes itself (`xrGetRenderModelStateEXT`) from the live input, which
we can't read: the models are fetched before a session, never during one (see the README's "What
we learned about controller models over OpenXR").

Core OpenXR can say what an action is bound to, which is the lead:

1. In the worker, create actions bound like Blender's stock map (the same interaction
   profiles and paths for B / Y click, A / X click, ...) and attach them.
2. After `xrSyncActions` in the FOCUSED state, `xrEnumerateBoundSourcesForAction` lists the
   input source paths each action is really bound to, on the controller in hand.
3. `xrGetInputSourceLocalizedName` (with `XR_INPUT_SOURCE_LOCALIZED_NAME_COMPONENT_BIT`) names
   each source, e.g. "A Button".
4. Match that to the glTF node names (`button_a` etc.), and store the role per part as now.

## Risks to probe first

- SteamVR keeps bindings per app (`system.generated.openxr.<app>.<exe>`), so the worker's
  binding may not be Blender's, especially after the user edits Blender's in SteamVR's UI.
  The worker would need to look like Blender to SteamVR, or read Blender's binding file
  (`Steam/config/openxr/system.generated.openxr.blender.blender.exe_*_binding.json`) instead.
- What `xrEnumerateBoundSourcesForAction` returns under SteamVR's remapping: the emulated
  profile's path (no help) or the physical one.
- Localized names may not line up with node names.
- Without the controller in hand there's nothing to test against. Start with a probe in
  `tests/` that prints bound sources and localized names for every action, as
  `tests/probe_xr_models.py` does for the models.

Cheaper fallback: let the user pick, in VR Extras, which model button plays top / bottom per
hand, saved beside the cached models.
