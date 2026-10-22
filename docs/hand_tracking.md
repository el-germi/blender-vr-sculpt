# Hand tracking for puppeteering: findings (deferred)

Goal: puppeteer rigs with tracked hands (finger joints), not just controller poses. Shelved
for now (2026-10-22) as not worth the effort yet. These are the findings so far.

## What the headset side gives us

- **Steam Link (Quest → SteamVR) forwards hand tracking to the PC** in two forms:
  SteamVR Skeletal Input (OpenVR's IVRInput), and the OpenXR extension
  `XR_EXT_hand_tracking` (26 joints per hand, each with a pose and a radius). It's in the
  stable releases of Steam Link and SteamVR, after a public beta.
- An app gets the joints only if it asks for them. Hands don't stand in for controllers
  on their own: SteamVR shows a pinch pointer in its own interface, and controller-based
  games need per-game gesture bindings.
- One reported caveat: Meta's own runtime (Quest Link) has kept hand tracking behind
  developer mode. Steam Link is the better-documented path.

## Why the add-on can't read them today

- An OpenXR app has to enable an extension when it creates its session. Blender's VR
  layer (`GHOST_Xr`, C++) doesn't request `XR_EXT_hand_tracking` (or
  `XR_EXT_hand_interaction`), and its Python API only exposes action-map inputs
  (controller buttons, values and poses).
- An add-on can't change which extensions Blender requests or call OpenXR itself, and a
  second OpenXR session next to Blender's isn't an option.

## Options, cheapest first

1. **Probe what Blender already gets with hands only.** Start a VR session, put the
   controllers down and use hands, then log the controller aim and grip poses and the
   trigger value. If SteamVR maps a pinch to the trigger and gives a hand pose, we'd have
   an aim ray and a pinch with no code changes (no finger joints, though). This hasn't been
   tried yet, and the documentation doesn't say what SteamVR's OpenXR side reports.
2. **A helper process using OpenVR** (Windows and SteamVR only). It would read Skeletal
   Input (IVRInput, skeletal bone data) as an overlay or background app, outside Blender,
   and stream the joints to the add-on locally (e.g. a socket on 127.0.0.1). The add-on
   would then map the joints onto finger bones the same way puppet bindings drive bones
   (`puppet.py` / `pose.Follow`).
   This is the realistic way to get full finger data without touching Blender, but it's a
   moderate project. Still to check: whether a background OpenVR app may read skeletal
   input while Blender holds the scene, and how much latency it adds.
3. **Patch Blender:** enable `XR_EXT_hand_tracking` in GHOST_Xr and expose the joints to
   Python (e.g. on `XrSessionState`). This is the proper fix, but it needs a custom Blender
   build or an upstream contribution.

## Sources

- [UploadVR: Steam Link Quest hand tracking (OpenXR + SteamVR Input)](https://www.uploadvr.com/steam-link-quest-hand-tracking-openxr-steamvr-input/)
- [Road to VR: Valve releases hand-tracking passthrough for Steam Link](https://www.roadtovr.com/valve-steam-link-quest-hand-tracking-2-8-release/)
- [SteamDB: SteamVR patch notes](https://steamdb.info/patchnotes/15400572)
- [Godot docs: OpenXR hand tracking (SteamVR / Steam Link notes)](https://docs.godotengine.org/en/stable/tutorials/xr/openxr_hand_tracking.html)
- [Khronos forum: extensions supported by Meta's runtime](https://community.khronos.org/t/supported-extensions-by-meta/112150)
