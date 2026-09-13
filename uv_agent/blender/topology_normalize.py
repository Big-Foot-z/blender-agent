"""glTF/GLB input topology normalization (gates G1-G5, G12, G14).

The glTF importer stores one vertex per (position, normal, uv) corner, so a
flat-shaded asset comes back with every face disconnected: 6 "shells", every
edge a boundary edge, 24 co-located vertices instead of 8. Measuring islands,
folds or texel density on such a mesh measures the *shading* split, not the
model. Blender 5.1's ``import_scene.gltf(merge_vertices=True)`` fixes part of
it, but it "cannot combine verts with different normals" - hence the position
weld fallback below (G12).

Layout:

* a **pure core** (numpy only, no ``bpy``) that measures topology
  (:func:`topology_counts`), owns the single normalization policy
  (:data:`NORMALIZATION_POLICY`, :func:`should_weld`, :func:`weld_tolerance_for`)
  and shapes the report (:func:`normalization_report`);
* **Blender adapters** (:func:`import_model`, :func:`topology_audit_object`,
  :func:`weld_by_position`, :func:`normalize_topology`) that import ``bpy`` /
  ``bmesh`` lazily inside the function body.

The same policy object is used by the import path and by the export re-read
audit (G14): there is exactly one definition of "when do we weld and how far".
"""

from __future__ import annotations

import os

#: Weld distance as a fraction of the mesh bounding-box diagonal (G12).
WELD_TOLERANCE_FACTOR = 1e-7

#: The ONE normalization policy shared by import and export re-read (G14).
NORMALIZATION_POLICY = {
    "merge_vertices": True,
    "weld_tolerance_factor": WELD_TOLERANCE_FACTOR,
    "weld_when": "glb_gltf_with_duplicate_position_boundary",
    "guards": [
        "face_count_unchanged",
        "no_new_non_manifold",
        "displacement_within_tolerance",
    ],
}

#: Every G2 audit field, in report order.
TOPOLOGY_FIELDS = (
    "vertex_count",
    "edge_count",
    "face_count",
    "connected_component_count",
    "boundary_edge_count",
    "non_manifold_edge_count",
    "duplicate_position_vertex_count",
)


# ---------------------------------------------------------------------------
# Pure core - measurement
# ---------------------------------------------------------------------------
class _UnionFind:
    """Deterministic union-find (path compression, union by size)."""

    def __init__(self, n: int) -> None:
        self._parent = list(range(n))
        self._size = [1] * n

    def find(self, a: int) -> int:
        parent = self._parent
        root = a
        while parent[root] != root:
            root = parent[root]
        while parent[a] != root:
            parent[a], a = root, parent[a]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._size[ra] < self._size[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        self._size[ra] += self._size[rb]

    def groups(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = {}
        for i in range(len(self._parent)):
            out.setdefault(self.find(i), []).append(i)
        return out


def _as_coords(coords):
    """``(N, 3)`` float array; raises on non-finite input (never 0-substitute)."""
    import numpy as np  # noqa: PLC0415 - local so the module imports without numpy

    arr = np.asarray(list(coords) if not hasattr(coords, "shape") else coords, dtype=float)
    if arr.size == 0:
        return np.zeros((0, 3), dtype=float)
    if arr.ndim != 2 or arr.shape[1] != 3:
        raise ValueError(f"coords must be (N, 3), got {arr.shape}")
    if not bool(np.all(np.isfinite(arr))):
        raise ValueError("coords contain non-finite values (NaN/inf)")
    return arr


def _as_faces(faces, vertex_count: int) -> list[list[int]]:
    out: list[list[int]] = []
    for f in faces:
        idx = [int(i) for i in f]
        for i in idx:
            if i < 0 or i >= vertex_count:
                raise ValueError(f"face vertex index {i} out of range (0..{vertex_count - 1})")
        out.append(idx)
    return out


def _edge_face_counts(faces: list[list[int]]) -> dict[tuple[int, int], list[int]]:
    """``(lo, hi) -> [face index, ...]`` for every polygon edge."""
    edges: dict[tuple[int, int], list[int]] = {}
    for fi, verts in enumerate(faces):
        n = len(verts)
        if n < 2:
            continue
        for k in range(n):
            a = verts[k]
            b = verts[(k + 1) % n]
            if a == b:
                continue
            key = (a, b) if a < b else (b, a)
            edges.setdefault(key, []).append(fi)
    return edges


def _face_components(faces: list[list[int]], edges: dict[tuple[int, int], list[int]]) -> int:
    if not faces:
        return 0
    uf = _UnionFind(len(faces))
    for face_ids in edges.values():
        first = face_ids[0]
        for other in face_ids[1:]:
            uf.union(first, other)
    return len({uf.find(i) for i in range(len(faces))})


def _duplicate_position_groups(coords, *, ndigits, tolerance) -> list[list[int]]:
    """Groups of vertex indices sharing a position (exact, rounded or within tol)."""
    n = int(coords.shape[0])
    if n == 0:
        return []

    if tolerance is None:
        buckets: dict[tuple, list[int]] = {}
        for i in range(n):
            row = coords[i]
            if ndigits is None:
                key = (float(row[0]), float(row[1]), float(row[2]))
            else:
                key = tuple(round(float(c) + 0.0, int(ndigits)) + 0.0 for c in row)
            buckets.setdefault(key, []).append(i)
        return [v for v in buckets.values() if len(v) > 1]

    tol = float(tolerance)
    if tol <= 0.0:
        return _duplicate_position_groups(coords, ndigits=ndigits, tolerance=None)

    cells: dict[tuple[int, int, int], list[int]] = {}
    keys: list[tuple[int, int, int]] = []
    import math  # noqa: PLC0415

    for i in range(n):
        row = coords[i]
        key = (int(math.floor(float(row[0]) / tol)),
               int(math.floor(float(row[1]) / tol)),
               int(math.floor(float(row[2]) / tol)))
        keys.append(key)
        cells.setdefault(key, []).append(i)

    uf = _UnionFind(n)
    tol_sq = tol * tol
    for i in range(n):
        cx, cy, cz = keys[i]
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for j in cells.get((cx + dx, cy + dy, cz + dz), ()):  # noqa: E501
                        if j <= i:
                            continue
                        d = coords[i] - coords[j]
                        if float(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]) <= tol_sq:
                            uf.union(i, j)
    return [sorted(g) for g in uf.groups().values() if len(g) > 1]


def _surface_area(coords, faces: list[list[int]]) -> float:
    import numpy as np  # noqa: PLC0415

    total = 0.0
    for verts in faces:
        if len(verts) < 3:
            continue
        p0 = coords[verts[0]]
        for k in range(1, len(verts) - 1):
            a = coords[verts[k]] - p0
            b = coords[verts[k + 1]] - p0
            total += 0.5 * float(np.linalg.norm(np.cross(a, b)))
    return float(total)


def topology_counts(coords, faces, *, ndigits: int | None = None,
                    dedupe_tolerance: float | None = None) -> dict:
    """Measure the topology of a raw ``(coords, faces)`` mesh (G2 audit fields).

    ``coords`` is an iterable of ``(x, y, z)`` rows, ``faces`` an iterable of
    vertex-index lists (any polygon size). Deterministic and finite-only: a NaN
    or inf coordinate raises :class:`ValueError` instead of being substituted.

    Returns ``vertex_count``, ``edge_count``, ``face_count``,
    ``connected_component_count`` (faces joined through shared edges),
    ``boundary_edge_count`` (1 face), ``non_manifold_edge_count`` (> 2 faces),
    ``duplicate_position_vertex_count`` / ``duplicate_position_group_count``
    (co-located vertices; exact, rounded to ``ndigits``, or within
    ``dedupe_tolerance``), ``bbox_diagonal``, ``surface_area``,
    ``expected_closed_triangle_edges`` (``3F/2`` when every face is a triangle,
    else ``None``) and ``edge_excess`` (``E - expected``, else ``None``).
    """
    import numpy as np  # noqa: PLC0415

    pts = _as_coords(coords)
    face_list = _as_faces(faces, int(pts.shape[0]))
    edges = _edge_face_counts(face_list)

    boundary = sum(1 for f in edges.values() if len(f) == 1)
    non_manifold = sum(1 for f in edges.values() if len(f) > 2)
    groups = _duplicate_position_groups(pts, ndigits=ndigits, tolerance=dedupe_tolerance)

    if pts.shape[0]:
        diag = float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0)))
    else:
        diag = 0.0

    all_tris = bool(face_list) and all(len(f) == 3 for f in face_list)
    expected = None
    excess = None
    if all_tris:
        raw = 3.0 * len(face_list) / 2.0
        expected = int(raw) if float(raw).is_integer() else float(raw)
        excess = len(edges) - expected

    return {
        "vertex_count": int(pts.shape[0]),
        "edge_count": int(len(edges)),
        "face_count": int(len(face_list)),
        "connected_component_count": int(_face_components(face_list, edges)),
        "boundary_edge_count": int(boundary),
        "non_manifold_edge_count": int(non_manifold),
        "duplicate_position_vertex_count": int(sum(len(g) for g in groups)),
        "duplicate_position_group_count": int(len(groups)),
        "bbox_diagonal": diag,
        "surface_area": _surface_area(pts, face_list),
        "expected_closed_triangle_edges": expected,
        "edge_excess": excess,
    }


# ---------------------------------------------------------------------------
# Pure core - policy (G14: shared by import and export re-read)
# ---------------------------------------------------------------------------
def weld_tolerance_for(bbox_diagonal: float, *, factor: float = WELD_TOLERANCE_FACTOR) -> float:
    """Scale-relative weld distance: ``bbox_diagonal * factor`` (0.0 for a point)."""
    diag = float(bbox_diagonal or 0.0)
    if diag <= 0.0:
        return 0.0
    return diag * float(factor)


def should_weld(fmt: str, audit: dict) -> bool:
    """Does this format + audit need the position weld fallback (G12)?

    Only glTF/GLB split vertices on shading, and only a mesh that actually has
    co-located vertices AND boundary edges is a fake-boundary suspect (G3/G5:
    a genuine open shell without duplicates must be left alone).
    """
    if str(fmt).lower() not in ("glb", "gltf"):
        return False
    return bool(int(audit.get("duplicate_position_vertex_count", 0)) > 0
                and int(audit.get("boundary_edge_count", 0)) > 0)


def _delta(pre: dict, post: dict) -> dict:
    def d(key: str) -> int:
        return int(post.get(key, 0)) - int(pre.get(key, 0))

    return {
        "vertex_delta": d("vertex_count"),
        "edge_delta": d("edge_count"),
        "boundary_edge_delta": d("boundary_edge_count"),
        "component_delta": d("connected_component_count"),
        "non_manifold_delta": d("non_manifold_edge_count"),
    }


def normalization_report(fmt: str, *, merge_vertices_enabled: bool, pre: dict, post: dict,
                         weld: dict | None) -> dict:
    """Plan §5/§9 normalization report: pre/post audits, delta, guards, policy.

    Every G2 field is repeated at the TOP level (post-normalization values) so a
    consumer never has to know the nesting to answer "how many boundary edges
    does the mesh we actually measured have?".
    """
    weld = weld or {}
    applied = bool(weld.get("applied", False))
    report = {
        "format": str(fmt),
        "merge_vertices_enabled": bool(merge_vertices_enabled),
        "position_weld_applied": applied,
        "weld_tolerance": float(weld.get("tolerance", 0.0) or 0.0),
        "welded_vertex_count": int(weld.get("welded_vertex_count", 0) or 0),
        "pre_normalization": dict(pre),
        "post_normalization": dict(post),
        "delta": _delta(pre, post),
        "guards": dict(weld.get("guards") or {}),
        "policy": dict(NORMALIZATION_POLICY),
    }
    if weld.get("reason"):
        report["weld_skip_reason"] = str(weld["reason"])
    for key in TOPOLOGY_FIELDS:
        report[key] = int(post.get(key, 0))
    return report


# ---------------------------------------------------------------------------
# Blender adapters (bpy / bmesh imported lazily inside the body)
# ---------------------------------------------------------------------------
_GLTF_FORMATS = ("glb", "gltf")


def format_from_path(path: str) -> str:
    """Lower-case extension without the dot (``"glb"``, ``"fbx"``, ``"blend"``…)."""
    return os.path.splitext(str(path))[1].lower().lstrip(".")


def mesh_arrays_from_object(obj):
    """``(coords ndarray (N,3), faces list[list[int]])`` from a Blender object."""
    import numpy as np  # noqa: PLC0415

    mesh = obj.data
    n = len(mesh.vertices)
    coords = np.empty(n * 3, dtype=float)
    mesh.vertices.foreach_get("co", coords)
    coords = coords.reshape((n, 3))
    faces = [[int(i) for i in p.vertices] for p in mesh.polygons]
    return coords, faces


def topology_audit_object(obj, *, tolerance: float | None = None) -> dict:
    """:func:`topology_counts` for a Blender object's evaluated mesh data."""
    coords, faces = mesh_arrays_from_object(obj)
    return topology_counts(coords, faces, dedupe_tolerance=tolerance)


def import_model(bpy, path: str, *, merge_vertices: bool = True) -> dict:
    """The ONE model import helper (G1/G14). Does NOT reset the scene.

    ``.blend`` is opened, ``.fbx`` / ``.obj`` use their importers, and
    ``.glb`` / ``.gltf`` pass ``merge_vertices`` to ``import_scene.gltf``. A
    Blender build without that argument is RECORDED (``merge_vertices_supported``
    False), never silently ignored - the position weld fallback is what makes
    such a build safe (G12).

    Returns ``{"format", "merge_vertices_requested", "merge_vertices_supported",
    "merge_vertices_enabled"}``.
    """
    fmt = format_from_path(path)
    abs_path = os.path.abspath(path)
    supported = None
    enabled = False

    if fmt == "blend":
        bpy.ops.wm.open_mainfile(filepath=abs_path)
    elif fmt == "fbx":
        bpy.ops.import_scene.fbx(filepath=abs_path)
    elif fmt == "obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=abs_path)
        else:  # pragma: no cover - legacy Blender
            bpy.ops.import_scene.obj(filepath=abs_path)
    elif fmt in _GLTF_FORMATS:
        try:
            bpy.ops.import_scene.gltf(filepath=abs_path, merge_vertices=bool(merge_vertices))
            supported = True
            enabled = bool(merge_vertices)
        except TypeError:  # pragma: no cover - older Blender without the argument
            bpy.ops.import_scene.gltf(filepath=abs_path)
            supported = False
            enabled = False
    else:
        raise ValueError(f"unsupported model format: {fmt or '(none)'}")

    return {
        "format": fmt,
        "merge_vertices_requested": bool(merge_vertices) if fmt in _GLTF_FORMATS else False,
        "merge_vertices_supported": supported,
        "merge_vertices_enabled": bool(enabled),
    }


def _max_nearest_displacement(pre_coords, post_coords, tolerance: float):
    """``(max_displacement, all_within)``: how far did surviving vertices move?

    ``remove_doubles`` keeps the first vertex of each merged group, so every
    surviving position should already exist in the pre set; this measures it
    instead of assuming it. ``all_within`` is False as soon as one pre vertex has
    no post vertex within ``tolerance``.
    """
    import math  # noqa: PLC0415

    n_pre = int(pre_coords.shape[0])
    if n_pre == 0 or int(post_coords.shape[0]) == 0:
        return 0.0, True

    tol = float(tolerance)
    if tol <= 0.0:
        seen = {(float(r[0]), float(r[1]), float(r[2])) for r in post_coords}
        for r in pre_coords:
            if (float(r[0]), float(r[1]), float(r[2])) not in seen:
                return float("inf"), False
        return 0.0, True

    cells: dict[tuple[int, int, int], list[int]] = {}
    for j in range(int(post_coords.shape[0])):
        r = post_coords[j]
        key = (int(math.floor(float(r[0]) / tol)),
               int(math.floor(float(r[1]) / tol)),
               int(math.floor(float(r[2]) / tol)))
        cells.setdefault(key, []).append(j)

    worst = 0.0
    for i in range(n_pre):
        r = pre_coords[i]
        cx = int(math.floor(float(r[0]) / tol))
        cy = int(math.floor(float(r[1]) / tol))
        cz = int(math.floor(float(r[2]) / tol))
        best = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for j in cells.get((cx + dx, cy + dy, cz + dz), ()):
                        d = r - post_coords[j]
                        dist = math.sqrt(float(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]))
                        if best is None or dist < best:
                            best = dist
        if best is None or best > tol:
            return (float(best) if best is not None else float("inf")), False
        worst = max(worst, best)
    return float(worst), True


def weld_by_position(bpy, obj, *, tolerance: float) -> dict:
    """Weld co-located vertices by POSITION only, behind three guards (G12).

    The weld is computed on a bmesh COPY first. It is written back to the mesh
    only when the face count is unchanged, the non-manifold edge count did not
    grow, and no surviving vertex moved further than ``tolerance``. On a guard
    failure nothing is written and ``{"applied": False, "reason": <guard>}`` is
    returned - a normalization that would damage geometry is evidence, not an
    exception (G4).
    """
    import bmesh  # noqa: PLC0415 - lazy: keeps the pure half importable
    import numpy as np  # noqa: PLC0415

    mesh = obj.data
    pre_coords, _pre_faces = mesh_arrays_from_object(obj)
    faces_before = len(mesh.polygons)
    verts_before = len(mesh.vertices)

    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        nm_before = sum(1 for e in bm.edges if len(e.link_faces) > 2)
        bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=float(tolerance))
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        faces_after = len(bm.faces)
        verts_after = len(bm.verts)
        nm_after = sum(1 for e in bm.edges if len(e.link_faces) > 2)
        post_coords = np.asarray([tuple(v.co) for v in bm.verts], dtype=float)
        if post_coords.size == 0:
            post_coords = np.zeros((0, 3), dtype=float)
        max_disp, within = _max_nearest_displacement(pre_coords, post_coords, float(tolerance))

        guards = {
            "face_count_unchanged": bool(faces_after == faces_before),
            "no_new_non_manifold": bool(nm_after <= nm_before),
            "displacement_within_tolerance": bool(within),
        }
        failed = [name for name, ok in guards.items() if not ok]
        result = {
            "applied": False,
            "tolerance": float(tolerance),
            "welded_vertex_count": int(verts_before - verts_after),
            "vertices_before": int(verts_before),
            "vertices_after": int(verts_after),
            "faces_before": int(faces_before),
            "faces_after": int(faces_after),
            "non_manifold_before": int(nm_before),
            "non_manifold_after": int(nm_after),
            "max_displacement": float(max_disp),
            "guards": guards,
        }
        if failed:
            result["reason"] = failed[0]
            return result
        bm.to_mesh(mesh)
    finally:
        bm.free()
    mesh.update()
    result["applied"] = True
    return result


def normalize_topology(bpy, obj, *, fmt: str, merge_info: dict | None = None,
                       tolerance_factor: float = WELD_TOLERANCE_FACTOR) -> dict:
    """Audit → (conditionally) weld → re-audit one imported object (G2/G3/G12).

    Never raises for a guard failure: a rejected weld comes back as
    ``position_weld_applied`` False with ``weld_skip_reason``.
    """
    merge_info = dict(merge_info or {})
    bbox = topology_audit_object(obj, tolerance=None)["bbox_diagonal"]
    tolerance = weld_tolerance_for(bbox, factor=tolerance_factor)
    pre = topology_audit_object(obj, tolerance=tolerance)

    weld: dict | None = None
    if should_weld(fmt, pre):
        weld = weld_by_position(bpy, obj, tolerance=tolerance)
        if not weld.get("applied"):
            weld.setdefault("tolerance", float(tolerance))

    post = topology_audit_object(obj, tolerance=tolerance) if weld and weld.get("applied") \
        else dict(pre)

    report = normalization_report(
        fmt, merge_vertices_enabled=bool(merge_info.get("merge_vertices_enabled", False)),
        pre=pre, post=post, weld=weld)
    report["weld_tolerance"] = float(weld["tolerance"]) if weld else float(tolerance)
    report["weld_considered"] = bool(weld is not None)
    report["merge_vertices_supported"] = merge_info.get("merge_vertices_supported")
    report["merge_vertices_requested"] = bool(merge_info.get("merge_vertices_requested", False))
    report["applied_weld"] = weld
    return report
