"""Phase U2 + U3 — per-chart Blender unwrap + packing (chart-UV plan §6, §7).

Given the chart seam set from U1, mark it on the mesh and run one angle-based unwrap
(charts unwrap independently because they are bounded by seams), tighten area-stretch
per chart with ``minimize_stretch``, normalise texel density across charts with
``average_islands_scale``, and pack with Blender's CONCAVE packer (rotation on). Then
read the per-loop UVs back into a :class:`UVMap` and detect any chart that still flipped
so the pipeline can re-split it (U2.2). Only runs inside Blender (``bpy`` lazy).

Reuses the organic engine's Blender helpers (seam marking, UV read-back, island-plan
recovery, metric assembly) rather than forking them.
"""

from __future__ import annotations

from uv_agent.blender.organic_unwrap import (  # reuse, do not fork
    AI_UV_LAYER, _activate, island_plan_from_seams, mark_seams, read_uvmap,
)
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap


def unwrap_and_pack(
    obj,
    seams,
    *,
    margin: float = 0.02,
    method: str = "MINIMUM_STRETCH",
    minimize_iters: int = 0,
    pack_shape: str = "CONCAVE",
    rotate: bool = True,
    average_scale: bool = True,
    layer_name: str = AI_UV_LAYER,
) -> int:
    """Mark ``seams``, unwrap, density-normalise, and pack (U2.1/U2.3/U3.1). The default
    method is **SLIM (``MINIMUM_STRETCH``)** — it is locally injective, so charts do not
    self-fold (the §5d correctness fix; ABF folds and the raster gate caught it). We do
    NOT run the separate ``minimize_stretch`` op for SLIM: it is not injective and would
    re-introduce folds. Returns the seam count marked; UVs land in ``layer_name``.

    ``rotate`` / ``average_scale`` / ``minimize_iters`` are the levers the UV Layout
    Optimization Loop (UV_LAYOUT_OPTIMIZATION_LOOP_PLAN §8) sweeps across candidates; the
    defaults reproduce the prior single-unwrap behaviour exactly (existing callers
    unchanged)."""
    import bpy

    mesh = obj.data
    if layer_name not in mesh.uv_layers:
        mesh.uv_layers.new(name=layer_name)
    mesh.uv_layers.active = mesh.uv_layers[layer_name]
    marked = mark_seams(obj, seams)

    _activate(bpy, obj)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.select_all(action="SELECT")
        bpy.ops.uv.unwrap(method=method, margin=margin)
        # minimize_stretch only for ABF (it is non-injective; never for SLIM).
        if minimize_iters and method == "ANGLE_BASED":
            try:
                bpy.ops.uv.minimize_stretch(iterations=int(minimize_iters))
            except RuntimeError:
                pass
        if average_scale:
            try:
                bpy.ops.uv.average_islands_scale()
            except RuntimeError:
                pass
        _pack(bpy, margin, pack_shape, rotate=rotate)
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
    mesh.update()
    return marked


def write_uvmap(obj, mesh: MeshGraph, uvmap: UVMap, *, layer_name: str = AI_UV_LAYER) -> None:
    """Write ``uvmap`` back into ``layer_name`` — the inverse of :func:`read_uvmap` (G5).

    Restoring a rejected candidate needs the UV coordinates put back exactly as they were,
    so this is the write half of the snapshot pair: the layer is created and made active
    when missing, and the per-loop UVs are pushed with one ``foreach_set`` in the same
    loop order :func:`read_uvmap` reads (loop index i <-> ``obj.data.loops[i]``). Loops
    beyond ``uvmap``'s length keep their current UV rather than being zeroed.
    """
    import numpy as np

    mesh_data = obj.data
    if layer_name not in mesh_data.uv_layers:
        mesh_data.uv_layers.new(name=layer_name)
    mesh_data.uv_layers.active = mesh_data.uv_layers[layer_name]
    data = mesh_data.uv_layers[layer_name].data
    flat = np.empty(len(data) * 2, dtype=np.float64)
    data.foreach_get("uv", flat)
    flat = flat.reshape(-1, 2)
    source = np.asarray(uvmap.uv, dtype=np.float64)
    n = min(len(flat), len(source))
    flat[:n] = source[:n]
    data.foreach_set("uv", flat.reshape(-1))
    mesh_data.update()


def repack(obj, *, margin: float = 0.02, pack_shape: str = "CONCAVE", rotate: bool = True,
           layer_name: str = AI_UV_LAYER) -> None:
    """U3.2 packing retune — re-pack the existing UVs (e.g. with a smaller margin or a
    different shape method) without re-unwrapping."""
    import bpy

    obj.data.uv_layers.active = obj.data.uv_layers[layer_name]
    _activate(bpy, obj)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.select_all(action="SELECT")
        _pack(bpy, margin, pack_shape, rotate=rotate)
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
    obj.data.update()


def _pack(bpy, margin: float, shape: str, rotate: bool = True) -> None:
    """Blender 5 CONCAVE packer with graceful degradation across signature drift."""
    for kwargs in (
        {"rotate": rotate, "margin": margin, "shape_method": shape},
        {"rotate": rotate, "margin": margin},
        {"margin": margin},
    ):
        try:
            bpy.ops.uv.pack_islands(**kwargs)
            return
        except TypeError:
            continue


def reunwrap_faces(obj, face_ids, *, method: str = "MINIMUM_STRETCH", minimize_iters: int = 0,
                   margin: float = 0.001, layer_name: str = AI_UV_LAYER) -> int:
    """Re-unwrap ONLY ``face_ids`` (the self-folding charts) with the locally-injective
    SLIM (``MINIMUM_STRETCH``) method, leaving other charts as-is (§5d correctness fix —
    SLIM removes self-folds without splitting). Returns the face count re-unwrapped; the
    caller re-packs. ``minimize_stretch`` is skipped for SLIM (non-injective)."""
    import bmesh
    import bpy

    obj.data.uv_layers.active = obj.data.uv_layers[layer_name]
    _activate(bpy, obj)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        bpy.ops.mesh.select_all(action="DESELECT")
        fs = set(face_ids)
        for f in bm.faces:
            f.select = f.index in fs
        bmesh.update_edit_mesh(obj.data)
        bpy.ops.uv.unwrap(method=method, margin=margin)
        if minimize_iters:
            try:
                bpy.ops.uv.minimize_stretch(iterations=int(minimize_iters))
            except RuntimeError:
                pass
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
    obj.data.update()
    return len(face_ids)


def pack_subset(obj, face_ids, *, margin: float = 0.01, pack_shape: str = "AABB",
                layer_name: str = AI_UV_LAYER) -> int:
    """Pack ONLY ``face_ids``' UV islands, leaving every other chart where it is — the
    transfer engine's local last-resort for a stubborn colliding pair (it must not global-
    repack, which would discard the reference-guided placement). Returns the face count."""
    import bmesh
    import bpy

    obj.data.uv_layers.active = obj.data.uv_layers[layer_name]
    _activate(bpy, obj)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        bpy.ops.mesh.select_all(action="DESELECT")
        fs = set(face_ids)
        for f in bm.faces:
            f.select = f.index in fs
        bmesh.update_edit_mesh(obj.data)
        bpy.ops.uv.select_all(action="SELECT")
        _pack(bpy, margin, pack_shape)
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
    obj.data.update()
    return len(face_ids)


def flipped_faces(mesh: MeshGraph, uvmap: UVMap) -> list[int]:
    """Face ids with negative signed UV area (a flip/fold — U2.2). A chart containing
    flips is not a valid embedding and must be re-split."""
    import numpy as np

    out: list[int] = []
    for f in mesh.faces:
        li = f.loop_indices
        area = 0.0
        for i in range(1, len(li) - 1):
            a = uvmap.get(li[0]); b = uvmap.get(li[i]); c = uvmap.get(li[i + 1])
            area += 0.5 * ((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1]))
        if area < -1e-12:
            out.append(f.id)
    return out


__all__ = ["unwrap_and_pack", "repack", "write_uvmap", "pack_subset", "flipped_faces",
           "island_plan_from_seams", "read_uvmap"]
