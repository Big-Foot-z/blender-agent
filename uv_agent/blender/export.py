"""Blender production-asset export (MVP 5 plan §5, §11, Session B).

Exports the MVP 3 accepted ``selected_uv.blend`` object to FBX / OBJ / GLB / GLTF
with its UVs, applying the user's export options on a **duplicate** object so the
source ``selected_uv_model`` is never mutated (plan §11 "destructive modifiers는
duplicate object에만 적용한다", §15 "selected UV blend 오염 위험").

``bpy`` is imported lazily inside the functions that need it, so the pure helpers
(:func:`obj_axis_enum`, :data:`EXPORT_DISPATCH`) import and unit-test without
Blender. Each format exports independently and returns a structured ok/error so a
single format failure becomes a ``partial`` export, never an app crash (plan §5).

Export contract (plan §5.1 options):

- ``selected_uv_layer``  activate this UV layer before export (or keep active).
- ``apply_scale``        bake the object's scale into the duplicate's mesh.
- ``triangulate``        apply a Triangulate modifier to the duplicate.
- ``include_materials``  keep / drop the duplicate's materials.
- ``include_normals``    export smoothing / normals.
- ``copy_textures``      best-effort embed/copy textures (warning only).
- ``axis_forward`` / ``axis_up``  forward/up axis for FBX + OBJ.
"""

from __future__ import annotations

import os

# FBX-style axis token (e.g. ``-Z``) -> ``wm.obj_export`` forward/up enum. The
# OBJ exporter uses ``NEGATIVE_*`` where the FBX exporter uses ``-*`` (plan §5).
_OBJ_AXIS = {
    "X": "X", "Y": "Y", "Z": "Z",
    "-X": "NEGATIVE_X", "-Y": "NEGATIVE_Y", "-Z": "NEGATIVE_Z",
}
# Default axes match the contract defaults (plan §5.1): forward -Z, up Y.
DEFAULT_AXIS_FORWARD = "-Z"
DEFAULT_AXIS_UP = "Y"

# Internal names so cleanup / detection is deterministic.
_EXPORT_DUP_SUFFIX = "_AI_Export"
_TRI_MODIFIER = "AI_Export_Triangulate"


def obj_axis_enum(axis: str | None, default: str = "Y") -> str:
    """Map an FBX-style axis token (``-Z`` / ``Y``) to a ``wm.obj_export`` enum.

    Pure (no Blender). Unknown tokens fall back to ``default`` mapped through the
    same table, so a bad option can never raise inside the OBJ exporter.
    """
    key = str(axis or default).upper().replace(" ", "")
    return _OBJ_AXIS.get(key, _OBJ_AXIS.get(str(default).upper(), "Y"))


# ---------------------------------------------------------------------------
# Object / UV preparation (plan §5 — never mutate the source object)
# ---------------------------------------------------------------------------
def activate_only(bpy, obj) -> None:
    """Make ``obj`` the sole selected + active object in OBJECT mode."""
    try:
        if getattr(bpy.context, "object", None) is not None and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
    except RuntimeError:
        pass
    for o in bpy.context.view_layer.objects:
        if o is not None:
            o.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def resolve_export_object(bpy, object_name: str | None):
    """Resolve the named mesh, or the first mesh object (plan §11 resolve object)."""
    obj = bpy.data.objects.get(object_name) if object_name else None
    if obj is None or obj.type != "MESH":
        obj = next((o for o in bpy.data.objects if o.type == "MESH"), None)
    return obj


def make_uv_layer_exclusive(obj, layer_name: str | None) -> list[str]:
    """Remove every UV layer except ``layer_name``; return the removed names.

    Pure enough to unit-test with a fake ``uv_layers`` collection (only ``__iter__``,
    ``.name`` and ``.remove(layer)`` are used).

    Why: FBX / glTF importers keep EVERY UV layer and re-activate the FIRST one, so
    an asset exported with two layers (original ``UVMap`` + optimized ``AI_UV``)
    re-reads with the ORIGINAL UV active — the G9 re-read audit then measures the
    wrong map (observed: fbx ``mandatory_90_uv_unsplit`` = 68). Shipping exactly one
    UV layer makes the exported file unambiguous for any consumer.

    In-memory only: the caller must never save the source ``selected_uv.blend`` back
    (plan §11, §15), so dropping layers here cannot damage the accepted asset.
    """
    removed: list[str] = []
    uv_layers = getattr(obj.data, "uv_layers", None)
    if uv_layers is None or not layer_name:
        return removed
    doomed = [layer for layer in uv_layers if layer.name != layer_name]
    if len(doomed) >= len(list(uv_layers)):
        # ``layer_name`` is not present — never strip the object's only UVs.
        return removed
    for layer in doomed:
        name = layer.name
        try:
            uv_layers.remove(layer)
        except (RuntimeError, TypeError, ValueError):  # pragma: no cover - Blender build dependent
            continue
        removed.append(name)
    return removed


def set_active_uv_layer(obj, layer_name: str | None, *,
                        exclusive: bool = False) -> tuple[str | None, list[str]]:
    """Activate ``layer_name`` (and mark it active-for-render); keep active if None.

    Returns ``(active_uv_layer_name, warnings)``. A requested-but-missing layer is
    a warning, not a failure — the object's existing active layer still exports
    (plan §7 tolerance). Returns ``(None, [...])`` when the object has no UVs.

    CRITICAL: the OBJ exporter (and glTF's primary TEXCOORD_0) writes the UV map
    flagged ``active_render``, NOT ``uv_layers.active``. So we ALWAYS mark the
    active layer as ``active_render`` here — even on the keep-active (``None``)
    path. Otherwise an asset that still carries a leftover original UV layer flagged
    for render (e.g. the source ``UVChannel_1`` next to the optimized ``AI_UV``)
    silently ships the WRONG, un-optimized UVs while the manifest/preview report the
    active one (MVP3 existing-UV repack follow-up — preview ≠ exported OBJ bug).

    ``exclusive=True`` additionally drops every other UV layer (see
    :func:`make_uv_layer_exclusive`) and reports the removed names as warnings.
    Default ``False`` keeps the historical behaviour untouched.
    """
    warnings: list[str] = []
    uv_layers = obj.data.uv_layers
    if len(uv_layers) == 0:
        warnings.append("object has no UV layers")
        return None, warnings
    if layer_name:
        layer = uv_layers.get(layer_name)
        if layer is None:
            warnings.append(
                f"requested UV layer {layer_name!r} not found; "
                f"exporting active layer {uv_layers.active.name!r}")
        else:
            uv_layers.active = layer
    active = uv_layers.active
    # Sync render layer with the active layer so the exporter writes exactly the UV
    # we activated / previewed (see the CRITICAL note above).
    if active is not None:
        try:
            active.active_render = True
        except (AttributeError, RuntimeError):  # pragma: no cover - Blender build dependent
            pass
    active_name = active.name if active is not None else None
    if exclusive and active_name:
        removed = make_uv_layer_exclusive(obj, active_name)
        if removed:
            warnings.append(f"removed non-exported UV layers: {removed}")
    return active_name, warnings


def build_export_object(bpy, obj, options: dict):
    """Duplicate ``obj`` and apply destructive export options to the COPY only.

    Applies scale (bake transform), triangulation (modifier), and material
    stripping per ``options`` — all on a fresh duplicate with its own mesh data,
    so the source ``selected_uv.blend`` object is never modified (plan §11, §15).
    Returns the duplicate object; caller must :func:`cleanup_export_object` it.
    """
    dup = obj.copy()
    dup.data = obj.data.copy()
    dup.name = obj.name + _EXPORT_DUP_SUFFIX
    bpy.context.collection.objects.link(dup)
    activate_only(bpy, dup)

    if bool(options.get("apply_scale", True)):
        try:
            bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        except RuntimeError as exc:  # pragma: no cover - Blender op
            print(f"export: apply scale skipped ({exc})", flush=True)

    if bool(options.get("triangulate", False)):
        mod = dup.modifiers.new(_TRI_MODIFIER, "TRIANGULATE")
        try:
            bpy.ops.object.modifier_apply(modifier=mod.name)
        except RuntimeError as exc:  # pragma: no cover - Blender op
            print(f"export: triangulate skipped ({exc})", flush=True)

    if not bool(options.get("include_materials", True)):
        dup.data.materials.clear()

    activate_only(bpy, dup)
    return dup


def cleanup_export_object(bpy, dup) -> None:
    """Remove the export duplicate + its mesh data (leave the source untouched)."""
    if dup is None:
        return
    mesh = dup.data
    try:
        bpy.data.objects.remove(dup, do_unlink=True)
    except (ReferenceError, RuntimeError):  # pragma: no cover
        pass
    try:
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh, do_unlink=True)
    except (ReferenceError, RuntimeError):  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# Per-format exporters (plan §5, §11 export operators)
# ---------------------------------------------------------------------------
def export_fbx(bpy, path: str, options: dict) -> None:
    """FBX export of the active selection (``bpy.ops.export_scene.fbx``, plan §11)."""
    bpy.ops.export_scene.fbx(
        filepath=os.path.abspath(path),
        use_selection=True,
        object_types={"MESH"},
        use_mesh_modifiers=True,
        mesh_smooth_type="FACE" if bool(options.get("include_normals", True)) else "OFF",
        use_custom_props=False,
        path_mode="COPY" if bool(options.get("copy_textures", False)) else "AUTO",
        embed_textures=bool(options.get("copy_textures", False)),
        bake_space_transform=False,
        # Scale was already baked onto the duplicate (build_export_object).
        apply_scale_options="FBX_SCALE_NONE",
        axis_forward=str(options.get("axis_forward", DEFAULT_AXIS_FORWARD)),
        axis_up=str(options.get("axis_up", DEFAULT_AXIS_UP)),
    )


def export_obj(bpy, path: str, options: dict) -> None:
    """OBJ export (``bpy.ops.wm.obj_export``, fallback ``export_scene.obj``; plan §11)."""
    if hasattr(bpy.ops.wm, "obj_export"):
        bpy.ops.wm.obj_export(
            filepath=os.path.abspath(path),
            export_selected_objects=True,
            export_normals=bool(options.get("include_normals", True)),
            export_uv=True,
            export_materials=bool(options.get("include_materials", True)),
            export_triangulated_mesh=False,  # already applied on the duplicate
            forward_axis=obj_axis_enum(options.get("axis_forward"), DEFAULT_AXIS_FORWARD),
            up_axis=obj_axis_enum(options.get("axis_up"), DEFAULT_AXIS_UP),
            path_mode="COPY" if bool(options.get("copy_textures", False)) else "AUTO",
        )
    else:  # pragma: no cover - legacy Blender
        bpy.ops.export_scene.obj(
            filepath=os.path.abspath(path),
            use_selection=True,
            use_normals=bool(options.get("include_normals", True)),
            use_uvs=True,
            use_materials=bool(options.get("include_materials", True)),
            use_triangles=False,
        )


def export_gltf(bpy, path: str, options: dict, *, binary: bool) -> None:
    """glTF/GLB export (``bpy.ops.export_scene.gltf``, plan §11).

    ``binary`` selects GLB (single embedded file) vs GLTF_SEPARATE. Textures are
    embedded automatically for GLB; ``copy_textures`` is best-effort here (plan §5).
    """
    bpy.ops.export_scene.gltf(
        filepath=os.path.abspath(path),
        export_format="GLB" if binary else "GLTF_SEPARATE",
        use_selection=True,
        export_apply=False,  # modifiers/scale already applied on the duplicate
        export_normals=bool(options.get("include_normals", True)),
        export_materials="EXPORT" if bool(options.get("include_materials", True)) else "NONE",
        export_texcoords=True,
        export_yup=True,
    )


# format -> (exporter callable, kwargs). ``glb``/``gltf`` share one exporter.
EXPORT_DISPATCH: dict[str, str] = {"fbx": "fbx", "obj": "obj", "glb": "glb", "gltf": "gltf"}


def export_one(bpy, fmt: str, path: str, options: dict) -> dict:
    """Export ONE format; return ``{"ok": bool, "path"|"error": ...}`` (plan §5).

    Never raises: a failing exporter is captured as a structured error so the
    worker can record a ``failed_formats`` entry and still ship the others.
    """
    try:
        if fmt == "fbx":
            export_fbx(bpy, path, options)
        elif fmt == "obj":
            export_obj(bpy, path, options)
        elif fmt == "glb":
            export_gltf(bpy, path, options, binary=True)
        elif fmt == "gltf":
            export_gltf(bpy, path, options, binary=False)
        else:
            return {"ok": False, "error": {"code": "unsupported_format",
                                           "message": f"unsupported export format: {fmt!r}"}}
    except Exception as exc:  # noqa: BLE001 - any exporter failure is structured
        return {"ok": False, "error": {"code": "export_failed",
                                       "message": f"Blender {fmt.upper()} export failed: {exc}"}}
    if not os.path.exists(path):
        return {"ok": False, "error": {"code": "export_failed",
                                       "message": f"{fmt.upper()} export wrote no file"}}
    return {"ok": True, "path": path}
