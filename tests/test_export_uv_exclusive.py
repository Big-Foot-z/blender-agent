"""Pure-helper test for ``make_uv_layer_exclusive`` (G9 re-read, no bpy needed).

``uv_agent/blender/export.py`` imports ``bpy`` lazily, so this layer-stripping
helper runs against a fake ``uv_layers`` collection exactly like the other pure
export tests (``tests/test_export_formats.py``). The real removal is exercised by
the Blender-gated e2e (``tests/e2e/test_uv_auto_export_perf.py``).
"""

from uv_agent.blender import export


class _Layer:
    def __init__(self, name: str):
        self.name = name


class _UVLayers(list):
    def remove(self, layer):
        list.remove(self, layer)


class _Obj:
    def __init__(self, names):
        self.data = type("_Mesh", (), {"uv_layers": _UVLayers(_Layer(n) for n in names)})()


def test_make_uv_layer_exclusive_keeps_only_the_selected_layer():
    obj = _Obj(["UVMap", "AI_UV", "UVMap.001"])
    removed = export.make_uv_layer_exclusive(obj, "AI_UV")
    assert removed == ["UVMap", "UVMap.001"]
    assert [layer.name for layer in obj.data.uv_layers] == ["AI_UV"]

    # already exclusive -> no-op; missing / empty name -> never strips the only UVs.
    assert export.make_uv_layer_exclusive(obj, "AI_UV") == []
    assert export.make_uv_layer_exclusive(obj, "NOPE") == []
    assert export.make_uv_layer_exclusive(obj, None) == []
    assert [layer.name for layer in obj.data.uv_layers] == ["AI_UV"]
