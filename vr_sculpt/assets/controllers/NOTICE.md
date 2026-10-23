# Controller models

`left.npz` and `right.npz` are Meta's Quest Pro controller models and textures, from Meta's
"Oculus Controller Art" download (https://developer.oculus.com/downloads/package/oculus-controller-art),
converted by `tools/convert_controllers.py` (mesh split by button, press motions, texture reduced to
1024 px with its ambient occlusion baked in).

They are Meta's work, not covered by this add-on's GPL license. They are used, as Meta's terms for
the controller art allow, only to show the physical controllers the user is holding, mirroring the
real ones. Don't reuse them for anything else.
