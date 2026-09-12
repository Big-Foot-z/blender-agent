"""Game shading / tangent compatibility policies (Gate G10).

A UV run must never silently change the *shading* state of an asset that a game
engine depends on: hard edges (sharp edges + smooth faces, which is how Blender
expresses a normal split) and the UV seams that the tangent basis is derived
from have to stay consistent with each other.

This module is pure (no ``bpy``): snapshots are taken from an object's mesh data
duck-typed like the fake mesh in ``tests/test_smoothing_split.py``
(``edges[i].use_edge_sharp``, ``polygons[i].use_smooth`` and optionally
``edges[i].vertices``), and the UV side is audited on a :class:`MeshGraph` plus
a ``uvmap``.

Three policies (:data:`SHADING_POLICIES`):

``preserve``
    The shading state must come out of the run byte-identical: the before/after
    snapshots must compare ``unchanged``. Used when the artist authored the hard
    edges and the UV run is only allowed to touch UVs.

``split_normals_on_uv_seams``
    The run is allowed (expected) to add sharp edges, but only such that every
    UV seam edge ends up sharp: the after snapshot's sharp keys must be a
    superset of the before sharp keys unioned with the seam edges' keys, and no
    pre-existing sharp edge may be removed. This is the
    ``split_smoothing_by_uv_islands`` contract.

``require_uv_seam_on_sharp_edges``
    Every sharp edge must be a real UV boundary: it has to be in the seam set
    *and* actually carry different UVs on both sides (a seam that Blender welds
    across still smears the tangent basis), i.e.
    ``sharp_edge_uv_audit(...)["sharp_edge_uv_unsplit"] == 0``.

On top of the policy, a caller that ran a real tangent-basis check can pass
``tangent_ok``; ``False`` is always a ``tangent_basis_failed`` failure, ``None``
means "not evaluated" and is reported as ``tangent_checked: False``.
"""

from __future__ import annotations

import hashlib
from typing import Iterable

from .mesh_graph import MeshGraph

#: The shading policies understood by :func:`evaluate_shading_policy`.
SHADING_POLICIES = (
    "preserve",
    "split_normals_on_uv_seams",
    "require_uv_seam_on_sharp_edges",
)

#: Failure codes emitted by :func:`evaluate_shading_policy`.
SHADING_FAILURE_CODES = (
    "shading_state_changed",
    "sharp_edge_not_uv_split",
    "sharp_edge_not_seam",
    "seam_not_sharp",
    "sharp_edge_removed",
    "tangent_basis_failed",
)


def _norm_pair(a, b) -> tuple[int, int]:
    a, b = int(a), int(b)
    return (a, b) if a < b else (b, a)


def _edge_key(edge, index: int):
    """Normalised key for one mesh-data edge: the vertex pair when available,
    otherwise the edge index (the fake meshes / Blender-free callers that do not
    expose ``vertices``)."""
    verts = getattr(edge, "vertices", None)
    if verts is None:
        return int(index)
    pair = list(verts)
    if len(pair) != 2:
        return int(index)
    return _norm_pair(pair[0], pair[1])


def _key_text(key) -> str:
    if isinstance(key, tuple):
        return f"{key[0]},{key[1]}"
    return str(key)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _as_key(key):
    """Re-normalise a key that may have round-tripped through JSON (list -> tuple)."""
    if isinstance(key, (list, tuple)):
        if len(key) == 2:
            return _norm_pair(key[0], key[1])
        return tuple(int(k) for k in key)
    return int(key)


def _key_set(keys: Iterable) -> set:
    return {_as_key(k) for k in (keys or [])}


def _keys_are_indices(keys: Iterable) -> bool:
    for k in keys or []:
        return not isinstance(_as_key(k), tuple)
    return False


def _sorted_keys(keys: Iterable) -> list:
    return sorted(keys, key=lambda k: (1, k[0], k[1]) if isinstance(k, tuple) else (0, k, 0))


def shading_snapshot(mesh_data) -> dict:
    """Blender-free snapshot of the shading state of an object's mesh data.

    ``mesh_data`` only needs ``edges`` (each with ``use_edge_sharp`` and
    optionally ``vertices``) and ``polygons`` (each with ``use_smooth``)."""
    edges = list(getattr(mesh_data, "edges", []) or [])
    polys = list(getattr(mesh_data, "polygons", []) or [])

    sharp_keys = []
    for i, e in enumerate(edges):
        if bool(getattr(e, "use_edge_sharp", False)):
            sharp_keys.append(_edge_key(e, getattr(e, "index", i)))
    sharp_keys = _sorted_keys(set(sharp_keys))

    smooth_flags = "".join("1" if bool(getattr(p, "use_smooth", False)) else "0" for p in polys)
    smooth_face_count = smooth_flags.count("1")

    return {
        "sharp_edge_count": len(sharp_keys),
        "smooth_face_count": smooth_face_count,
        "sharp_edge_keys": sharp_keys,
        "sharp_hash": _sha256("|".join(_key_text(k) for k in sharp_keys)),
        "smooth_hash": _sha256(smooth_flags),
        "face_count": len(polys),
        "edge_count": len(edges),
    }


def compare_shading_snapshots(before: dict, after: dict) -> dict:
    """Diff two :func:`shading_snapshot` results."""
    b = _key_set(before.get("sharp_edge_keys"))
    a = _key_set(after.get("sharp_edge_keys"))
    added = _sorted_keys(a - b)
    removed = _sorted_keys(b - a)
    delta = int(after.get("smooth_face_count", 0)) - int(before.get("smooth_face_count", 0))
    unchanged = (
        not added
        and not removed
        and before.get("smooth_hash") == after.get("smooth_hash")
        and before.get("face_count") == after.get("face_count")
        and before.get("edge_count") == after.get("edge_count")
    )
    return {
        "unchanged": bool(unchanged),
        "sharp_added": added,
        "sharp_removed": removed,
        "smooth_face_delta": delta,
    }


def sharp_edge_uv_audit(mesh: MeshGraph, uvmap, *, tol: float = 1e-5) -> dict:
    """Do the sharp edges actually split the UVs? (mirrors ``mandatory_seam_uv_audit``)

    For every 2-face edge flagged ``is_sharp``, the two faces must carry
    DIFFERENT UVs at (at least one of) the two shared vertices. An edge whose two
    faces are welded is a hard normal split with a continuous UV across it, which
    breaks the tangent basis. Boundary / non-manifold edges have a single face and
    are inherently UV boundaries, so they are not counted."""
    fv_uv: dict[tuple[int, int], tuple[float, float]] = {}
    for loop in mesh.loops:
        fv_uv[(loop.face_id, loop.vertex_id)] = uvmap.get(loop.index)

    def welded(fa: int, fb: int, verts) -> bool:
        for vid in verts:
            ua = fv_uv.get((fa, vid))
            ub = fv_uv.get((fb, vid))
            if ua is None or ub is None:
                return False
            if abs(ua[0] - ub[0]) > tol or abs(ua[1] - ub[1]) > tol:
                return False
        return True

    sharp = 0
    unsplit: list[int] = []
    for e in mesh.edges:
        if len(e.face_ids) != 2 or not e.is_sharp:
            continue
        sharp += 1
        fa, fb = e.face_ids
        if welded(fa, fb, e.vertex_ids):
            unsplit.append(e.id)
    return {
        "sharp_edge_count": sharp,
        "sharp_edge_uv_unsplit": len(unsplit),
        "unsplit_edge_ids": unsplit,
    }


def _sharp_two_face_edge_ids(mesh: MeshGraph) -> list[int]:
    return [e.id for e in mesh.edges if e.is_sharp and len(e.face_ids) == 2]


def required_seam_edges_for_policy(policy: str, mesh: MeshGraph) -> set[int]:
    """Edge ids the policy forces into the mandatory seam set.

    Only ``require_uv_seam_on_sharp_edges`` constrains seams (every 2-face sharp
    edge must be a seam); the other policies return an empty set."""
    if policy not in SHADING_POLICIES:
        raise ValueError(f"unknown shading policy: {policy!r}")
    if policy == "require_uv_seam_on_sharp_edges":
        return set(_sharp_two_face_edge_ids(mesh))
    return set()


def evaluate_shading_policy(
    policy: str,
    *,
    mesh: MeshGraph,
    uvmap,
    seams,
    before_snapshot: dict | None = None,
    after_snapshot: dict | None = None,
    tangent_ok: bool | None = None,
) -> dict:
    """Evaluate one of :data:`SHADING_POLICIES` against a finished UV run (Gate G10)."""
    if policy not in SHADING_POLICIES:
        raise ValueError(f"unknown shading policy: {policy!r}")

    seam_ids = {int(s) for s in (seams or [])}
    audit = sharp_edge_uv_audit(mesh, uvmap)

    failures: list[str] = []
    invalid_reasons: list[str] = []
    diff: dict | None = None
    if before_snapshot is not None and after_snapshot is not None:
        diff = compare_shading_snapshots(before_snapshot, after_snapshot)

    if policy == "preserve":
        if diff is None:
            invalid_reasons.append("shading_snapshot_missing")
        elif not diff["unchanged"]:
            failures.append("shading_state_changed")

    elif policy == "split_normals_on_uv_seams":
        if diff is None:
            invalid_reasons.append("shading_snapshot_missing")
        else:
            before_keys = _key_set(before_snapshot.get("sharp_edge_keys"))
            after_keys = _key_set(after_snapshot.get("sharp_edge_keys"))
            by_index = _keys_are_indices(after_snapshot.get("sharp_edge_keys")) or _keys_are_indices(
                before_snapshot.get("sharp_edge_keys")
            )
            if by_index:
                seam_keys = set(seam_ids)
            else:
                seam_keys = {_norm_pair(*mesh.edges[eid].vertex_ids) for eid in seam_ids}
            if not (before_keys | seam_keys) <= after_keys:
                failures.append("seam_not_sharp")
            if diff["sharp_removed"]:
                failures.append("sharp_edge_removed")

    elif policy == "require_uv_seam_on_sharp_edges":
        if audit["sharp_edge_uv_unsplit"] != 0:
            failures.append("sharp_edge_not_uv_split")
        if not set(_sharp_two_face_edge_ids(mesh)) <= seam_ids:
            failures.append("sharp_edge_not_seam")

    if tangent_ok is False:
        failures.append("tangent_basis_failed")

    valid = not invalid_reasons
    result = {
        "policy": policy,
        "valid": valid,
        "passed": bool(valid and not failures),
        "failures": failures,
        "invalid_reasons": invalid_reasons,
        "sharp_edge_uv_audit": audit,
        "tangent_checked": tangent_ok is not None,
        "tangent_ok": tangent_ok,
    }
    if diff is not None:
        result["snapshot_diff"] = diff
    return result
