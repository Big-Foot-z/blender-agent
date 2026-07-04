"""Install + enable the Reforge bridge addon into the user's Blender prefs.

Run headless by the app (bridge plan §4.2):

    blender --background --python install_bridge.py -- /abs/path/reforge_bridge.py

Using ``bpy.ops.preferences.addon_install`` means we never have to guess the
per-version addons directory; ``save_userprefs`` persists the enable so the
bridge starts with every interactive session.
"""

from __future__ import annotations

import os
import sys

import bpy


def main() -> int:
    argv = sys.argv
    addon_path = None
    if "--" in argv:
        rest = argv[argv.index("--") + 1 :]
        if rest:
            addon_path = os.path.abspath(rest[0])
    if not addon_path or not os.path.exists(addon_path):
        print(f"install_bridge: addon file not found: {addon_path}", file=sys.stderr)
        return 2

    bpy.ops.preferences.addon_install(filepath=addon_path, overwrite=True)
    module = os.path.splitext(os.path.basename(addon_path))[0]
    bpy.ops.preferences.addon_enable(module=module)
    # The operator is ``wm.save_userpref`` (singular); keep a fallback spelling
    # in case a Blender version renames it.
    if hasattr(bpy.ops.wm, "save_userpref"):
        bpy.ops.wm.save_userpref()
    else:
        bpy.ops.wm.save_userprefs()
    print(f"install_bridge: installed and enabled '{module}'")
    return 0


sys.exit(main())
