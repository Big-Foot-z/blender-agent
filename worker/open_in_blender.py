"""Open a model file in an interactive Blender session (bridge plan §3.1).

Launched by the app as ``blender --python open_in_blender.py -- /abs/model.fbx``
when the working model is an FBX (a .blend is opened directly as the file
argument instead). Runs in the interactive UI (NOT --background): resets to an
empty scene, imports the model, and frames it.

A script file (not ``--python-expr``) so Windows argument quoting is a
non-issue and packaging ships it under ``pysrc/`` (plan §3.1).
"""

from __future__ import annotations

import os
import sys

import bpy


def _model_path() -> str | None:
    argv = sys.argv
    if "--" in argv:
        rest = argv[argv.index("--") + 1 :]
        if rest:
            return os.path.abspath(rest[0])
    return None


def main() -> None:
    path = _model_path()
    if not path or not os.path.exists(path):
        print(f"open_in_blender: model not found: {path}", file=sys.stderr)
        return

    # Fresh empty scene (drops the default cube/camera/light of the startup file).
    bpy.ops.wm.read_homefile(use_empty=True)

    ext = os.path.splitext(path)[1].lower()
    if ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    else:
        print(f"open_in_blender: unsupported extension: {ext}", file=sys.stderr)
        return

    # Tag imports so the bridge addon can find/replace them on refresh (plan §4.1).
    for obj in bpy.context.selected_objects:
        obj["reforge_source"] = path

    print(f"open_in_blender: imported {path}")


main()
