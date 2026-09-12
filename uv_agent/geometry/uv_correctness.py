"""Final-UV correctness audits (Gate G1).

Plan §4 separates *distortion* (how much the surface is stretched) from
*correctness* (is the layout a valid, injective, in-bounds packing at all).
This module owns the correctness half:

* exact triangle/triangle **overlap** area (not a raster screening ratio),
* **orientation** (local folds vs. a deliberately mirrored island),
* **degenerate** UV triangles (separated from degenerate *input* faces),
* UV **bounds**,
* island **packing gap** against a ``margin_px`` / ``texture_size_px`` profile,
* island-to-**tile border** gap (pixel padding / mip safety, Gate G9).

Gate G1 requires the overlap number to come from a broad phase plus real
triangle intersection areas, and requires a shared boundary (islands that merely
touch) to be distinguishable from a genuine overlap. Sutherland-Hodgman convex
clipping gives exactly that: a shared edge or vertex clips down to a zero-area
polygon.

No ``bpy`` here - everything runs off :class:`MeshGraph` / :class:`UVMap` so the
audits are testable without Blender and reusable from the worker.
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np

from uv_agent.geometry.evaluation import uv_islands_from_uvmap
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap

__all__ = [
    "triangle_intersection_area",
    "exact_overlap_audit",
    "orientation_audit",
    "degenerate_uv_audit",
    "bounds_audit",
    "island_gap_audit",
    "border_gap_audit",
    "evaluate_correctness",
    "compact_correctness",
]

#: How many offending items each audit reports back (reports stay small).
MAX_SAMPLES = 20
MAX_FACE_IDS = 50


# ---------------------------------------------------------------------------
# Triangle intersection
# ---------------------------------------------------------------------------

def _signed_area_poly(poly: Sequence[Sequence[float]]) -> float:
    n = len(poly)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return 0.5 * s


def _as_ccw(tri) -> list[tuple[float, float]]:
    p = [(float(tri[0][0]), float(tri[0][1])),
         (float(tri[1][0]), float(tri[1][1])),
         (float(tri[2][0]), float(tri[2][1]))]
    if _signed_area_poly(p) < 0.0:
        p.reverse()
    return p


def triangle_intersection_area(t1, t2) -> float:
    """Area of the intersection of two UV triangles (each ``3x2``).

    Winding-agnostic: both inputs are normalised to counter-clockwise first, so
    a clockwise triangle is not silently treated as empty. Triangles that only
    touch (shared edge or shared vertex) intersect in a degenerate polygon and
    return ``0.0`` - Gate G1 requires that distinction.

    Implemented by Sutherland-Hodgman clipping of ``t1`` against the three
    half-planes of ``t2``; both are convex, so the clipped polygon is exactly
    the intersection."""
    subject = _as_ccw(t1)
    clip = _as_ccw(t2)
    if _signed_area_poly(subject) <= 0.0 or _signed_area_poly(clip) <= 0.0:
        return 0.0

    output = subject
    for i in range(3):
        ax, ay = clip[i]
        bx, by = clip[(i + 1) % 3]
        ex, ey = bx - ax, by - ay
        if not output:
            return 0.0
        inp = output
        output = []
        sx, sy = inp[-1]
        s_side = ex * (sy - ay) - ey * (sx - ax)
        for px, py in inp:
            p_side = ex * (py - ay) - ey * (px - ax)
            if p_side >= 0.0:
                if s_side < 0.0:
                    t = s_side / (s_side - p_side)
                    output.append((sx + t * (px - sx), sy + t * (py - sy)))
                output.append((px, py))
            elif s_side >= 0.0:
                t = s_side / (s_side - p_side)
                output.append((sx + t * (px - sx), sy + t * (py - sy)))
            sx, sy, s_side = px, py, p_side

    if len(output) < 3:
        return 0.0
    return abs(_signed_area_poly(output))


# ---------------------------------------------------------------------------
# Shared triangle gathering
# ---------------------------------------------------------------------------

def _island_of_face(mesh: MeshGraph, uvmap: UVMap, islands) -> tuple[dict[int, int], int, str]:
    if islands is None:
        islands = uv_islands_from_uvmap(mesh, uvmap)
        source = "uv_connectivity"
    else:
        islands = [list(g) for g in islands]
        source = "provided"
    lookup: dict[int, int] = {}
    for idx, group in enumerate(islands):
        for fid in group:
            lookup[int(fid)] = idx
    return lookup, len(islands), source


def _gather_uv_triangles(mesh: MeshGraph, uvmap: UVMap):
    """``(tris, face_ids, nan_count)`` - one entry per real triangle.

    Triangles come from :meth:`MeshGraph.face_triangles` (Blender's own
    triangulation when available), never a naive fan, so concave n-gons do not
    contribute phantom triangles. Triangles with a non-finite UV are dropped and
    counted; ``bounds_audit`` is what turns NaN into a failure."""
    tris: list[np.ndarray] = []
    face_ids: list[int] = []
    nan_count = 0
    for f in mesh.faces:
        for tri in mesh.face_triangles(f.id):
            uv = np.array([uvmap.get(tri[0]), uvmap.get(tri[1]), uvmap.get(tri[2])], dtype=float)
            if not np.all(np.isfinite(uv)):
                nan_count += 1
                continue
            tris.append(uv)
            face_ids.append(f.id)
    return tris, face_ids, nan_count


def _uniform_grid_pairs(boxes: np.ndarray, grid: int) -> set[tuple[int, int]]:
    """Broad phase: bucket AABBs into a uniform ``grid x grid`` lattice over the
    overall bounds and return the candidate index pairs sharing a cell."""
    n = int(boxes.shape[0])
    pairs: set[tuple[int, int]] = set()
    if n < 2:
        return pairs
    lo = boxes[:, :2].min(axis=0)
    hi = boxes[:, 2:].max(axis=0)
    span = np.maximum(hi - lo, 1e-12)
    g = max(1, int(grid))
    cell = span / g

    buckets: dict[tuple[int, int], list[int]] = {}
    for i in range(n):
        i0 = int(np.clip((boxes[i, 0] - lo[0]) / cell[0], 0, g - 1))
        i1 = int(np.clip((boxes[i, 2] - lo[0]) / cell[0], 0, g - 1))
        j0 = int(np.clip((boxes[i, 1] - lo[1]) / cell[1], 0, g - 1))
        j1 = int(np.clip((boxes[i, 3] - lo[1]) / cell[1], 0, g - 1))
        for ci in range(i0, i1 + 1):
            for cj in range(j0, j1 + 1):
                buckets.setdefault((ci, cj), []).append(i)

    for members in buckets.values():
        m = len(members)
        if m < 2:
            continue
        for a in range(m):
            ia = members[a]
            for b in range(a + 1, m):
                ib = members[b]
                pairs.add((ia, ib) if ia < ib else (ib, ia))
    return pairs


# ---------------------------------------------------------------------------
# Audits
# ---------------------------------------------------------------------------

def exact_overlap_audit(mesh: MeshGraph, uvmap: UVMap, islands=None, *,
                        area_tol_total: float = 1e-8,
                        pair_area_eps: float = 1e-12,
                        grid: int = 128) -> dict:
    """Total positive-area UV triangle overlap (Gate G1 overlap row).

    Areas are in normalised UV units, i.e. the whole 0-1 tile has area 1, which
    is the unit the ``<= 1e-8`` numeric tolerance is written in. Triangles of the
    SAME face are excluded (they share the triangulation edge by construction);
    so are boundary touches, which clip to zero area."""
    lookup, island_count, source = _island_of_face(mesh, uvmap, islands)
    tris, face_ids, _nan = _gather_uv_triangles(mesh, uvmap)

    n = len(tris)
    total = 0.0
    pair_count = 0
    self_pair = 0
    cross_pair = 0
    samples: list[dict] = []

    if n >= 2:
        boxes = np.empty((n, 4), dtype=float)
        for i, t in enumerate(tris):
            boxes[i, 0] = t[:, 0].min()
            boxes[i, 1] = t[:, 1].min()
            boxes[i, 2] = t[:, 0].max()
            boxes[i, 3] = t[:, 1].max()

        for ia, ib in _uniform_grid_pairs(boxes, grid):
            fa, fb = face_ids[ia], face_ids[ib]
            if fa == fb:
                continue
            if (boxes[ia, 2] < boxes[ib, 0] or boxes[ib, 2] < boxes[ia, 0]
                    or boxes[ia, 3] < boxes[ib, 1] or boxes[ib, 3] < boxes[ia, 1]):
                continue
            area = triangle_intersection_area(tris[ia], tris[ib])
            if area <= pair_area_eps:
                continue
            total += area
            pair_count += 1
            isl_a = lookup.get(fa, -1)
            isl_b = lookup.get(fb, -1)
            if isl_a == isl_b:
                self_pair += 1
            else:
                cross_pair += 1
            if len(samples) < MAX_SAMPLES:
                samples.append({
                    "face_a": int(fa), "face_b": int(fb),
                    "island_a": int(isl_a), "island_b": int(isl_b),
                    "area": float(area),
                })

    return {
        "overlap_area_total": float(total),
        "passed": bool(total <= area_tol_total),
        "pair_count": int(pair_count),
        "self_pair_count": int(self_pair),
        "cross_pair_count": int(cross_pair),
        "samples": samples,
        "islands_source": source,
        "island_count": int(island_count),
        "area_tol_total": float(area_tol_total),
    }


def _tri_signed_area(t: np.ndarray) -> float:
    return 0.5 * float((t[1, 0] - t[0, 0]) * (t[2, 1] - t[0, 1])
                       - (t[2, 0] - t[0, 0]) * (t[1, 1] - t[0, 1]))


def orientation_audit(mesh: MeshGraph, uvmap: UVMap, islands=None) -> dict:
    """Local UV folds vs. mirrored islands (plan §4 / Gate G1 policy D4).

    The expected orientation of an island is decided by its own **area-weighted
    sign majority** (the sign of the summed signed area), not by a global
    convention. A triangle whose sign opposes its island's is a genuine local
    fold and fails the gate; an island whose total is negative is a *mirrored*
    island, which is reported but does not fail - mirroring the whole chart is a
    legal (if unusual) layout, while a fold is always a texturing bug."""
    lookup, island_count, source = _island_of_face(mesh, uvmap, islands)
    tris, face_ids, _nan = _gather_uv_triangles(mesh, uvmap)

    areas = [_tri_signed_area(t) for t in tris]
    per_island: dict[int, float] = {}
    for a, fid in zip(areas, face_ids):
        isl = lookup.get(fid, -1)
        per_island[isl] = per_island.get(isl, 0.0) + a

    flip_faces: list[int] = []
    flip_count = 0
    flip_area = 0.0
    for a, fid in zip(areas, face_ids):
        if a == 0.0:
            continue
        expected = per_island.get(lookup.get(fid, -1), 0.0)
        if expected == 0.0:
            continue
        if (a > 0.0) != (expected > 0.0):
            flip_count += 1
            flip_area += abs(a)
            if len(flip_faces) < MAX_FACE_IDS and int(fid) not in flip_faces:
                flip_faces.append(int(fid))

    mirrored = sorted(int(k) for k, v in per_island.items() if v < 0.0)
    return {
        "expected_orientation": "island_majority",
        "local_flip_count": int(flip_count),
        "local_flip_face_ids": flip_faces,
        "local_flip_area": float(flip_area),
        "mirrored_island_ids": mirrored,
        "mirrored_island_count": len(mirrored),
        "passed": bool(flip_count == 0),
        "islands_source": source,
        "island_count": int(island_count),
    }


def degenerate_uv_audit(mesh: MeshGraph, uvmap: UVMap, *, eps: float = 1e-12) -> dict:
    """Zero-area UV triangles (Gate G1 degenerate row).

    Plan §4: a degenerate triangle must never be silently skipped, and a
    *defective input* must not be blamed on the unwrapper. A triangle whose 3D
    area is also zero is an input defect and is reported separately; a triangle
    with real 3D area that collapsed in UV is a correctness failure."""
    uv_bad: list[int] = []
    uv_count = 0
    defect_count = 0
    for f in mesh.faces:
        for tri in mesh.face_triangles(f.id):
            uv = np.array([uvmap.get(tri[0]), uvmap.get(tri[1]), uvmap.get(tri[2])], dtype=float)
            if not np.all(np.isfinite(uv)):
                continue
            if abs(_tri_signed_area(uv)) > eps:
                continue
            p = np.array([mesh.vertex_co(mesh.loops[li].vertex_id) for li in tri], dtype=float)
            area3d = 0.5 * float(np.linalg.norm(np.cross(p[1] - p[0], p[2] - p[0])))
            if area3d <= eps:
                defect_count += 1
                continue
            uv_count += 1
            if len(uv_bad) < MAX_FACE_IDS and int(f.id) not in uv_bad:
                uv_bad.append(int(f.id))

    return {
        "uv_degenerate_count": int(uv_count),
        "uv_degenerate_face_ids": uv_bad,
        "input_defect_count": int(defect_count),
        "passed": bool(uv_count == 0),
        "eps": float(eps),
    }


def bounds_audit(uvmap: UVMap, *, tol: float = 1e-4) -> dict:
    """UVs are finite and inside the 0-1 tile within ``tol`` (Gate G1 bounds row)."""
    uv = np.asarray(uvmap.uv, dtype=float)
    if uv.size == 0:
        return {"finite": True, "min_u": 0.0, "min_v": 0.0, "max_u": 0.0, "max_v": 0.0,
                "passed": True, "tol": float(tol)}
    finite = bool(np.all(np.isfinite(uv)))
    mask = np.isfinite(uv[:, 0]) & np.isfinite(uv[:, 1])
    if mask.any():
        good = uv[mask]
        min_u, min_v = float(good[:, 0].min()), float(good[:, 1].min())
        max_u, max_v = float(good[:, 0].max()), float(good[:, 1].max())
    else:
        min_u = min_v = max_u = max_v = math.nan
    inside = (finite and min_u >= -tol and min_v >= -tol
              and max_u <= 1.0 + tol and max_v <= 1.0 + tol)
    return {
        "finite": finite,
        "min_u": min_u, "min_v": min_v, "max_u": max_u, "max_v": max_v,
        "passed": bool(inside),
        "tol": float(tol),
    }


def _segment_distance(p1, q1, p2, q2) -> float:
    """Shortest distance between two 2D segments (0 when they touch or cross)."""
    d1 = q1 - p1
    d2 = q2 - p2
    r = p1 - p2
    a = float(d1 @ d1)
    e = float(d2 @ d2)
    f = float(d2 @ r)
    if a <= 1e-24 and e <= 1e-24:
        return float(np.linalg.norm(p1 - p2))
    if a <= 1e-24:
        s, t = 0.0, min(1.0, max(0.0, f / e))
    else:
        c = float(d1 @ r)
        if e <= 1e-24:
            t, s = 0.0, min(1.0, max(0.0, -c / a))
        else:
            b = float(d1 @ d2)
            denom = a * e - b * b
            s = min(1.0, max(0.0, (b * f - c * e) / denom)) if denom > 1e-24 else 0.0
            t = (b * s + f) / e
            if t < 0.0:
                t = 0.0
                s = min(1.0, max(0.0, -c / a))
            elif t > 1.0:
                t = 1.0
                s = min(1.0, max(0.0, (b - c) / a))
    return float(np.linalg.norm((p1 + d1 * s) - (p2 + d2 * t)))


def _island_boundary_segments(mesh: MeshGraph, uvmap: UVMap, lookup: dict[int, int]):
    """UV segments on island boundaries: every edge that is a mesh boundary
    (one face) or that separates two different islands contributes the UV line
    of each incident face. These are the curves that must stay ``margin_px``
    apart after packing."""
    fv_loop: dict[tuple[int, int], int] = {}
    for loop in mesh.loops:
        fv_loop[(loop.face_id, loop.vertex_id)] = loop.index

    segs: list[tuple[int, np.ndarray, np.ndarray]] = []
    for e in mesh.edges:
        fids = e.face_ids
        if len(fids) == 2 and lookup.get(fids[0], -1) == lookup.get(fids[1], -1):
            continue
        va, vb = e.vertex_ids
        for fid in fids:
            la = fv_loop.get((fid, va))
            lb = fv_loop.get((fid, vb))
            if la is None or lb is None:
                continue
            p = np.array(uvmap.get(la), dtype=float)
            q = np.array(uvmap.get(lb), dtype=float)
            if not (np.all(np.isfinite(p)) and np.all(np.isfinite(q))):
                continue
            segs.append((lookup.get(fid, -1), p, q))
    return segs


def island_gap_audit(mesh: MeshGraph, uvmap: UVMap, islands=None, *,
                     texture_size_px: int = 1024, margin_px: float = 4.0,
                     grid: int = 128) -> dict:
    """Packing gap between distinct islands (Gate G1 packing margin row).

    The gap is the minimum segment-to-segment distance between the boundary
    curves of two DIFFERENT islands, found with a uniform-grid broad phase and
    an expanding ring search so the reported minimum is exact, not a
    cell-quantised approximation. Islands that overlap or merely touch give 0,
    which is why a shared boundary passes the overlap audit but fails here."""
    lookup, island_count, source = _island_of_face(mesh, uvmap, islands)
    limit = float(margin_px) - 1e-6

    def _trivial() -> dict:
        return {
            "min_gap_uv": float("inf"), "min_gap_px": float("inf"),
            "margin_px": float(margin_px), "texture_size_px": int(texture_size_px),
            "passed": True, "closest_pair": None,
            "islands_source": source, "island_count": int(island_count),
        }

    if island_count <= 1:
        return _trivial()

    segs = _island_boundary_segments(mesh, uvmap, lookup)
    n = len(segs)
    if n < 2:
        return _trivial()

    boxes = np.empty((n, 4), dtype=float)
    for i, (_isl, p, q) in enumerate(segs):
        boxes[i, 0] = min(p[0], q[0])
        boxes[i, 1] = min(p[1], q[1])
        boxes[i, 2] = max(p[0], q[0])
        boxes[i, 3] = max(p[1], q[1])

    lo = boxes[:, :2].min(axis=0)
    hi = boxes[:, 2:].max(axis=0)
    span = np.maximum(hi - lo, 1e-12)
    g = max(1, int(grid))
    cell = span / g
    cell_min = float(min(cell[0], cell[1]))

    cells: list[tuple[int, int, int, int]] = []
    buckets: dict[tuple[int, int], list[int]] = {}
    for i in range(n):
        i0 = int(np.clip((boxes[i, 0] - lo[0]) / cell[0], 0, g - 1))
        i1 = int(np.clip((boxes[i, 2] - lo[0]) / cell[0], 0, g - 1))
        j0 = int(np.clip((boxes[i, 1] - lo[1]) / cell[1], 0, g - 1))
        j1 = int(np.clip((boxes[i, 3] - lo[1]) / cell[1], 0, g - 1))
        cells.append((i0, i1, j0, j1))
        for ci in range(i0, i1 + 1):
            for cj in range(j0, j1 + 1):
                buckets.setdefault((ci, cj), []).append(i)

    best = float("inf")
    best_pair: tuple[int, int] | None = None

    for i in range(n):
        isl_i, p1, q1 = segs[i]
        i0, i1, j0, j1 = cells[i]
        local_best = float("inf")
        local_pair: tuple[int, int] | None = None
        r = 0
        while True:
            # Cells at Chebyshev ring ``r`` around this segment's cell box.
            for ci in range(i0 - r, i1 + r + 1):
                if not (0 <= ci < g):
                    continue
                for cj in range(j0 - r, j1 + r + 1):
                    if not (0 <= cj < g):
                        continue
                    if r > 0 and (i0 - r < ci < i1 + r) and (j0 - r < cj < j1 + r):
                        continue
                    for j in buckets.get((ci, cj), ()):
                        if j == i:
                            continue
                        isl_j, p2, q2 = segs[j]
                        if isl_j == isl_i:
                            continue
                        d = _segment_distance(p1, q1, p2, q2)
                        if d < local_best:
                            local_best = d
                            local_pair = (isl_i, isl_j)
            # Anything first reachable beyond ring r is at least (r-1)*cell away.
            if r >= 1 and local_best <= (r - 1) * cell_min:
                break
            if (i0 - r) <= 0 and (j0 - r) <= 0 and (i1 + r) >= g - 1 and (j1 + r) >= g - 1:
                break
            r += 1
        if local_best < best:
            best = local_best
            best_pair = local_pair

    min_gap_px = best * float(texture_size_px)
    closest = None
    if best_pair is not None:
        closest = {"island_a": int(best_pair[0]), "island_b": int(best_pair[1])}
    return {
        "min_gap_uv": float(best),
        "min_gap_px": float(min_gap_px),
        "margin_px": float(margin_px),
        "texture_size_px": int(texture_size_px),
        "passed": bool(min_gap_px >= limit),
        "closest_pair": closest,
        "islands_source": source,
        "island_count": int(island_count),
    }


def _all_edge_segments(mesh: MeshGraph, uvmap: UVMap, lookup: dict[int, int]):
    """Every edge's UV line, per incident face - the fallback used when a mesh
    has no island boundary at all (a closed, single-island mesh). Without it the
    border audit would be vacuous exactly for the meshes that need it most."""
    fv_loop: dict[tuple[int, int], int] = {}
    for loop in mesh.loops:
        fv_loop[(loop.face_id, loop.vertex_id)] = loop.index

    segs: list[tuple[int, np.ndarray, np.ndarray]] = []
    for e in mesh.edges:
        va, vb = e.vertex_ids
        for fid in e.face_ids:
            la = fv_loop.get((fid, va))
            lb = fv_loop.get((fid, vb))
            if la is None or lb is None:
                continue
            p = np.array(uvmap.get(la), dtype=float)
            q = np.array(uvmap.get(lb), dtype=float)
            if not (np.all(np.isfinite(p)) and np.all(np.isfinite(q))):
                continue
            segs.append((lookup.get(fid, -1), p, q))
    return segs


def border_gap_audit(mesh: MeshGraph, uvmap: UVMap, islands=None, *,
                     texture_size_px: int = 1024,
                     border_margin_px: float = 4.0) -> dict:
    """Minimum island-to-tile-border gap (Gate G9, pixel padding / mip safety).

    Texture filtering and mip generation both sample *outside* a texel, so an
    island that runs right up to ``u=0``/``u=1``/``v=0``/``v=1`` bleeds across the
    tile seam at the coarser mips. The gap measured here is the distance from any
    island boundary segment (the same curves :func:`island_gap_audit` uses) to
    each of the four tile edges, exactly: for a segment ``p-q`` the distance to
    the line ``u=0`` is ``min(p.u, q.u)``, and symmetrically for the others. A
    point outside the tile yields a negative gap and therefore fails.

    A closed single-island mesh has no boundary segments at all; rather than
    report a vacuous ``inf``, the audit then falls back to every edge's UV line,
    which bounds the island just as well for this purpose."""
    lookup, island_count, source = _island_of_face(mesh, uvmap, islands)
    limit = float(border_margin_px) - 1e-6

    segs = _island_boundary_segments(mesh, uvmap, lookup)
    if not segs:
        segs = _all_edge_segments(mesh, uvmap, lookup)

    best = float("inf")
    best_island: int | None = None
    best_border: str | None = None

    for isl, p, q in segs:
        for name, d in (
            ("u0", min(float(p[0]), float(q[0]))),
            ("u1", min(1.0 - float(p[0]), 1.0 - float(q[0]))),
            ("v0", min(float(p[1]), float(q[1]))),
            ("v1", min(1.0 - float(p[1]), 1.0 - float(q[1]))),
        ):
            if d < best:
                best = d
                best_island = int(isl)
                best_border = name

    min_gap_px = best * float(texture_size_px)
    return {
        "min_gap_uv": float(best),
        "min_gap_px": float(min_gap_px),
        "border_margin_px": float(border_margin_px),
        "texture_size_px": int(texture_size_px),
        "passed": bool(min_gap_px >= limit),
        "closest_island": best_island,
        "closest_border": best_border,
        "islands_source": source,
        "island_count": int(island_count),
    }


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

def evaluate_correctness(mesh: MeshGraph, uvmap: UVMap, islands=None, *,
                         texture_size_px: int = 1024, margin_px: float = 4.0,
                         border_margin_px: float | None = None,
                         bounds_tol: float = 1e-4,
                         overlap_area_tol: float = 1e-8) -> dict:
    """Run the Gate G1/G9 correctness audits and combine their verdicts.

    ``border_margin_px`` defaults to ``margin_px``: the padding a tile border
    needs is the same padding two islands need unless a profile says otherwise.

    NaN handling follows plan §4: a non-finite UV makes ``bounds`` fail (so the
    whole report fails and can never be laundered into ``accepted``), while the
    geometric audits skip those triangles instead of producing garbage areas -
    the count is surfaced as ``nan_triangle_count`` rather than dropped."""
    if border_margin_px is None:
        border_margin_px = margin_px
    lookup, island_count, source = _island_of_face(mesh, uvmap, islands)
    resolved: list[list[int]] = [[] for _ in range(island_count)]
    for fid, isl in lookup.items():
        if 0 <= isl < island_count:
            resolved[isl].append(fid)

    _tris, _fids, nan_count = _gather_uv_triangles(mesh, uvmap)

    overlap = exact_overlap_audit(mesh, uvmap, resolved, area_tol_total=overlap_area_tol)
    orientation = orientation_audit(mesh, uvmap, resolved)
    degenerate = degenerate_uv_audit(mesh, uvmap)
    bounds = bounds_audit(uvmap, tol=bounds_tol)
    gap = island_gap_audit(mesh, uvmap, resolved,
                           texture_size_px=texture_size_px, margin_px=margin_px)
    border = border_gap_audit(mesh, uvmap, resolved,
                              texture_size_px=texture_size_px,
                              border_margin_px=border_margin_px)

    for part in (overlap, orientation, gap, border):
        part["islands_source"] = source

    checks = [
        {"name": "overlap", "passed": overlap["passed"],
         "value": overlap["overlap_area_total"], "limit": float(overlap_area_tol),
         "detail": f"{overlap['pair_count']} overlapping triangle pairs "
                   f"(self={overlap['self_pair_count']}, cross={overlap['cross_pair_count']})"},
        {"name": "orientation", "passed": orientation["passed"],
         "value": orientation["local_flip_count"], "limit": 0,
         "detail": f"{orientation['mirrored_island_count']} mirrored island(s) (reported only)"},
        {"name": "degenerate", "passed": degenerate["passed"],
         "value": degenerate["uv_degenerate_count"], "limit": 0,
         "detail": f"{degenerate['input_defect_count']} input-defect triangle(s)"},
        {"name": "bounds", "passed": bounds["passed"],
         "value": [bounds["min_u"], bounds["min_v"], bounds["max_u"], bounds["max_v"]],
         "limit": [-float(bounds_tol), 1.0 + float(bounds_tol)],
         "detail": "finite" if bounds["finite"] else "non-finite UV present"},
        {"name": "island_gap", "passed": gap["passed"],
         "value": gap["min_gap_px"], "limit": float(margin_px),
         "detail": f"texture_size_px={texture_size_px}"},
        {"name": "border_gap", "passed": border["passed"],
         "value": border["min_gap_px"], "limit": float(border_margin_px),
         "detail": f"closest_border={border['closest_border']} "
                   f"texture_size_px={texture_size_px}"},
    ]

    return {
        "passed": all(bool(c["passed"]) for c in checks),
        "nan_triangle_count": int(nan_count),
        "checks": checks,
        "overlap": overlap,
        "orientation": orientation,
        "degenerate": degenerate,
        "bounds": bounds,
        "island_gap": gap,
        "border_gap": border,
        "island_count": int(island_count),
        "islands_source": source,
        "texture_size_px": int(texture_size_px),
        "margin_px": float(margin_px),
        "border_margin_px": float(border_margin_px),
    }


def compact_correctness(report: dict) -> dict:
    """Small summary of :func:`evaluate_correctness` for job reports / JSON."""
    return {
        "passed": bool(report.get("passed", False)),
        "checks": [
            {"name": c["name"], "passed": bool(c["passed"]),
             "value": c["value"], "limit": c["limit"]}
            for c in report.get("checks", [])
        ],
        "overlap_area_total": float(report.get("overlap", {}).get("overlap_area_total", 0.0)),
        "local_flip_count": int(report.get("orientation", {}).get("local_flip_count", 0)),
        "mirrored_island_count": int(report.get("orientation", {}).get("mirrored_island_count", 0)),
        "uv_degenerate_count": int(report.get("degenerate", {}).get("uv_degenerate_count", 0)),
        "min_island_gap_px": float(report.get("island_gap", {}).get("min_gap_px", float("inf"))),
        "min_border_gap_px": float(report.get("border_gap", {}).get("min_gap_px", float("inf"))),
        "bounds_ok": bool(report.get("bounds", {}).get("passed", False)),
    }
