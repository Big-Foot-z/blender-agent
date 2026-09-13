"""Mesh identity fingerprints and edge correspondence (gates G0 / G1).

G0 requires evidence that UV work never touched the approved low-poly: the
vertex coordinates, the topology and the per-face material assignment must be
bit-identical before and after. G1 adds that when an artifact is re-read the
edge IDs may be renumbered, so the "same geometric edge" has to be recovered
through a fingerprint / correspondence table rather than through raw IDs.

This module is pure (``numpy`` allowed, no ``bpy``). :func:`fingerprint_from_arrays`
is a byte-for-byte copy of the serialization used by
``tests/e2e/fixtures/build_fixture_models.py`` so the worker recomputes exactly
the digest recorded in the fixture manifest; ``tests/test_mesh_identity.py``
cross-checks the two implementations against each other.
"""

from __future__ import annotations

import hashlib
import os
from typing import Iterable, Sequence

# ---------------------------------------------------------------------------
# Fingerprint — must stay byte-identical to the fixture builder's algorithm.
# ---------------------------------------------------------------------------
_COORD_NDIGITS = 6
_COORD_EPS = 0.5 * 10 ** (-_COORD_NDIGITS)


def _fmt_coord(value: float) -> str:
    """Round to 6 decimals and collapse -0.0 / +0.0 onto one spelling."""
    v = round(float(value), _COORD_NDIGITS)
    if abs(v) < _COORD_EPS:
        v = 0.0
    return f"{v:.{_COORD_NDIGITS}f}"


def _canonical_face(face) -> tuple:
    """Rotate a face loop to start at its lowest vertex index, keeping winding."""
    idx = [int(i) for i in face]
    if not idx:
        return ()
    start = idx.index(min(idx))
    return tuple(idx[start:] + idx[:start])


def fingerprint_from_arrays(coords, faces, mats) -> str:
    """SHA-256 over a deterministic serialization of geometry + material ids.

    ``coords``: iterable of ``(x, y, z)``; ``faces``: iterable of vertex-index
    lists; ``mats``: per-face material index (same order/length as ``faces``).
    Coordinates are rounded to 6 decimals so float noise below that does not
    change the digest; faces are canonicalized (loop rotated to its lowest
    vertex index, then the face list sorted) so a different polygon storage
    order does not change it either.
    """
    coords = list(coords)
    faces = [list(f) for f in faces]
    mats = list(mats)
    if len(mats) != len(faces):
        raise ValueError(f"mats/faces length mismatch: {len(mats)} vs {len(faces)}")
    parts = [f"nv {len(coords)}", f"nf {len(faces)}"]
    for c in coords:
        parts.append("v " + " ".join(_fmt_coord(x) for x in c))
    rows = sorted((_canonical_face(f), int(m)) for f, m in zip(faces, mats))
    for face, mat in rows:
        parts.append("f " + " ".join(str(i) for i in face) + " m" + str(mat))
    blob = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# UV hash (CG7 rollback baseline / CG0 evidence): exact, unrounded UV identity.
# ---------------------------------------------------------------------------
def uv_hash_from_array(arr) -> str:
    """SHA-256 of ``len:`` + the float64 little-endian bytes of ``arr``.

    Unlike :func:`fingerprint_from_arrays` this is *exact*: no rounding, so a
    1e-12 nudge of a single UV changes the digest (a rollback that restored a
    "visually identical" layout is still a different layout). NaN bytes are
    hashed as they are, so a NaN UV never hashes equal to a finite one. The
    loop-count prefix keeps two different-length arrays with the same byte tail
    apart. Deterministic across processes (no ``hash()``, no dict order).
    """
    import numpy as np  # local: keeps the module importable without numpy

    a = np.ascontiguousarray(np.asarray(arr, dtype="<f8"))
    n = int(a.shape[0]) if a.ndim else 0
    h = hashlib.sha256()
    h.update(f"{n}:".encode("utf-8"))
    h.update(a.tobytes(order="C"))
    return h.hexdigest()


def uv_hash(uvmap) -> str:
    """:func:`uv_hash_from_array` adapter for a :class:`UVMap` (``uvmap.uv``)."""
    return uv_hash_from_array(uvmap.uv)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# MeshGraph adapters
# ---------------------------------------------------------------------------
def _mesh_arrays(mesh) -> tuple[list, list, list]:
    coords = [tuple(v.co) for v in mesh.vertices]
    faces = [list(f.vertex_ids) for f in mesh.faces]
    mats = [int(getattr(f, "material_index", 0) or 0) for f in mesh.faces]
    return coords, faces, mats


def mesh_fingerprint(mesh) -> str:
    """:func:`fingerprint_from_arrays` adapter for a :class:`MeshGraph`."""
    coords, faces, mats = _mesh_arrays(mesh)
    return fingerprint_from_arrays(coords, faces, mats)


def material_indices_hash(mats: Iterable[int]) -> str:
    blob = "\n".join(str(int(m)) for m in mats).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def mesh_identity(mesh, *, model_path: str | None = None) -> dict:
    """Full G0 identity record for a mesh (plus the source file SHA-256)."""
    coords, faces, mats = _mesh_arrays(mesh)
    model_sha256 = None
    if model_path and os.path.isfile(model_path):
        model_sha256 = sha256_file(model_path)
    return {
        "fingerprint": fingerprint_from_arrays(coords, faces, mats),
        "vertex_count": len(mesh.vertices),
        "edge_count": len(mesh.edges),
        "face_count": len(mesh.faces),
        "loop_count": len(mesh.loops),
        "material_indices_hash": material_indices_hash(mats),
        "model_sha256": model_sha256,
    }


_IDENTITY_KEYS = ("fingerprint", "vertex_count", "edge_count", "face_count", "loop_count")


def compare_identity(before: dict, after: dict) -> dict:
    """Before/after diff shaped like the summary ``mesh_identity`` block."""
    differences = [k for k in _IDENTITY_KEYS if before.get(k) != after.get(k)]
    for extra in ("material_indices_hash", "model_sha256"):
        if before.get(extra) != after.get(extra):
            differences.append(extra)
    return {
        "unchanged": not differences,
        "before_sha256": before.get("fingerprint"),
        "after_sha256": after.get("fingerprint"),
        "vertex_count": after.get("vertex_count", before.get("vertex_count")),
        "face_count": after.get("face_count", before.get("face_count")),
        "loop_count": after.get("loop_count", before.get("loop_count")),
        "differences": differences,
    }


# ---------------------------------------------------------------------------
# Edge correspondence (G1: edge IDs may be renumbered on artifact re-read)
# ---------------------------------------------------------------------------
def edge_geometry_key(mesh, edge_id: int, ndigits: int = 6) -> tuple:
    """Geometry-only identity of an edge: its two rounded endpoints, sorted."""
    a, b = mesh.edges[edge_id].vertex_ids
    ends = []
    for vid in (a, b):
        co = mesh.vertices[vid].co
        ends.append(tuple(_round_coord(c, ndigits) for c in co))
    return tuple(sorted(ends))


def _round_coord(value: float, ndigits: int) -> float:
    v = round(float(value), ndigits)
    if abs(v) < 0.5 * 10 ** (-ndigits):
        v = 0.0
    return v


def edge_correspondence(mesh_a, mesh_b, ndigits: int = 6) -> dict:
    """Match edges of ``mesh_a`` to ``mesh_b`` by geometry, not by edge id."""
    keys_b: dict[tuple, list[int]] = {}
    for e in mesh_b.edges:
        keys_b.setdefault(edge_geometry_key(mesh_b, e.id, ndigits), []).append(e.id)

    a_to_b: dict[int, int] = {}
    used_b: set[int] = set()
    unmatched_a: list[int] = []
    for e in mesh_a.edges:
        key = edge_geometry_key(mesh_a, e.id, ndigits)
        bucket = keys_b.get(key)
        target = None
        if bucket:
            for cand in bucket:
                if cand not in used_b:
                    target = cand
                    break
        if target is None:
            unmatched_a.append(e.id)
            continue
        a_to_b[e.id] = target
        used_b.add(target)

    unmatched_b = [e.id for e in mesh_b.edges if e.id not in used_b]
    identical_ids = all(k == v for k, v in a_to_b.items()) and not unmatched_a and not unmatched_b
    return {
        "a_to_b": a_to_b,
        "unmatched_a": unmatched_a,
        "unmatched_b": unmatched_b,
        "identical_ids": bool(identical_ids),
    }


def remap_edge_ids(edge_ids: Sequence[int], correspondence: dict) -> tuple[list[int], list[int]]:
    """Translate ``edge_ids`` (mesh A numbering) into mesh B numbering."""
    a_to_b = correspondence.get("a_to_b", {})
    mapped: list[int] = []
    unmatched: list[int] = []
    for eid in edge_ids:
        eid = int(eid)
        if eid in a_to_b:
            mapped.append(int(a_to_b[eid]))
        else:
            unmatched.append(eid)
    return mapped, unmatched
