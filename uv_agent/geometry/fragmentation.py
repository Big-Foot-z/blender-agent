"""Island count / fragmentation measurement (Gate G7).

Gate G7 asks whether a finished UV layout is *fragmented*: too many islands, dust
islands with (near) zero area, long thin slivers, and one-or-two-face scraps that
a texture artist can never paint on. This module is the pure measurement half —
it classifies islands and reports checks; it never edits seams or UVs.

Definitions used here (all deterministic, numpy only, no randomness):

``uv_area``
    Sum of ``|signed UV triangle area|`` over the real triangulation of each face
    in the island (``MeshGraph.face_triangles``), so concave n-gons never
    contribute phantom area.

``bbox_area`` / ``aspect_ratio``
    Taken from the **PCA-oriented** bounding box of the island's UV points: the
    2x2 covariance of the points gives the principal axes, the points are
    projected onto them, and the two extents are the box sides. This makes both
    numbers rotation invariant, unlike an axis-aligned box. ``aspect_ratio`` is
    ``long / short`` with the short side clamped to ``>= 1e-9``; an island whose
    points all coincide (or which has no points at all) is degenerate and reports
    ``float('inf')``. ``inf`` is *not* JSON serialisable — converting it is the
    caller's job; the percentile metric here already skips non-finite values.

``fill_ratio``
    ``uv_area / bbox_area`` (0 when ``bbox_area <= 1e-18``) — how much of its own
    oriented box the island actually covers.

``mandatory_bounded``
    True when EVERY edge on the island boundary (a mesh edge with exactly one
    adjacent face in the island) is either a mesh boundary edge, non-manifold, or
    bends by ``>= fold_angle``. Such an island cannot be made bigger by moving a
    seam: its outline is forced by the model (R2). It is therefore still COUNTED
    in the ``tiny_island_count`` / ``sliver_island_count`` metrics, but it is
    EXEMPT from the hard failures (``zero_area``, ``below_min_area``,
    ``sliver_hard``) and is listed in ``exempt_islands`` with
    ``exempt_reason = "mandatory_bounded"``.

Classification: ``tiny := uv_area < tiny_island_uv_area``;
``sliver := aspect_ratio >= sliver_aspect_min and uv_area <= sliver_uv_area_max``.

Hard checks (``passed`` is exactly ``hard_passed``): ``zero_area_islands``,
``below_min_area_islands``, ``sliver_islands``. Quality checks (reported, never
gating): ``tiny_island_count``, ``tiny_island_area_ratio``, ``island_aspect_p95``.
"""

from __future__ import annotations

import math

import numpy as np

from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap

#: An island whose |UV area| is at or below this is "zero area" dust.
ZERO_AREA_UV_EPS = 1e-12
#: Below this oriented-box area the box is treated as having no area at all.
BBOX_AREA_EPS = 1e-18
#: Lower clamp on the short side of the oriented box when forming the aspect ratio.
MIN_BBOX_SIDE = 1e-9


def _tri_signed_area_uv(a, b, c) -> float:
    return 0.5 * float((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1]))


def _island_uv_points(mesh: MeshGraph, uvmap: UVMap, face_ids) -> np.ndarray:
    loops: list[int] = []
    for fid in face_ids:
        loops.extend(mesh.faces[fid].loop_indices)
    if not loops:
        return np.zeros((0, 2), dtype=float)
    return np.asarray(uvmap.uv[loops], dtype=float)


def _oriented_bbox(points: np.ndarray) -> tuple[float, float]:
    """Side lengths (long, short) of the PCA-oriented bounding box of ``points``."""
    if points.shape[0] == 0:
        return 0.0, 0.0
    if not np.all(np.isfinite(points)):
        return float("nan"), float("nan")
    centred = points - points.mean(axis=0)
    if points.shape[0] == 1:
        return 0.0, 0.0
    cov = centred.T @ centred
    # Symmetric 2x2 -> eigh is deterministic and returns an orthonormal basis.
    _vals, vecs = np.linalg.eigh(cov)
    proj = centred @ vecs
    extents = proj.max(axis=0) - proj.min(axis=0)
    a = float(max(extents[0], extents[1]))
    b = float(min(extents[0], extents[1]))
    return a, b


def _island_uv_area(mesh: MeshGraph, uvmap: UVMap, face_ids) -> float:
    total = 0.0
    for fid in face_ids:
        for l0, l1, l2 in mesh.face_triangles(fid):
            total += abs(
                _tri_signed_area_uv(uvmap.get(l0), uvmap.get(l1), uvmap.get(l2))
            )
    return float(total)


def _boundary_edges(mesh: MeshGraph, face_ids) -> list[int]:
    """Mesh edges with exactly ONE adjacent face inside the island, sorted by id."""
    inside = set(int(f) for f in face_ids)
    counts: dict[int, int] = {}
    for fid in sorted(inside):
        for eid in mesh.faces[fid].edge_ids:
            counts[eid] = counts.get(eid, 0) + 1
    out = []
    for eid in sorted(counts):
        e = mesh.edges[eid]
        adjacent_inside = sum(1 for f in e.face_ids if f in inside)
        if adjacent_inside == 1:
            out.append(eid)
    return out


def island_shape_rows(
    mesh: MeshGraph,
    uvmap: UVMap,
    islands,
    *,
    fold_angle: float = 90.0,
) -> list[dict]:
    """One row per island (a face-id list) describing its 3D / UV shape (Gate G7)."""
    rows: list[dict] = []
    for island_id, face_ids in enumerate(islands):
        fids = [int(f) for f in face_ids]
        points = _island_uv_points(mesh, uvmap, fids)
        long_side, short_side = _oriented_bbox(points)
        if not (math.isfinite(long_side) and math.isfinite(short_side)):
            bbox_area = float("nan")
            aspect = float("inf")
        elif long_side <= 0.0:
            bbox_area = 0.0
            aspect = float("inf")
        else:
            bbox_area = float(long_side * max(short_side, 0.0))
            aspect = float(long_side / max(short_side, MIN_BBOX_SIDE))

        uv_area = _island_uv_area(mesh, uvmap, fids)
        if math.isfinite(bbox_area) and bbox_area > BBOX_AREA_EPS:
            fill_ratio = float(uv_area / bbox_area)
        else:
            fill_ratio = 0.0

        boundary = _boundary_edges(mesh, fids)
        mandatory_bounded = bool(boundary) and all(
            mesh.edges[eid].is_boundary
            or mesh.edges[eid].is_non_manifold
            or mesh.edges[eid].dihedral_angle >= fold_angle
            for eid in boundary
        )

        rows.append(
            {
                "island_id": int(island_id),
                "face_count": len(fids),
                "area_3d": float(sum(mesh.faces[f].area_3d for f in fids)),
                "uv_area": float(uv_area),
                "bbox_area": float(bbox_area),
                "aspect_ratio": float(aspect),
                "fill_ratio": float(fill_ratio),
                "boundary_edge_count": len(boundary),
                "mandatory_bounded": bool(mandatory_bounded),
                "one_two_face": bool(len(fids) <= 2),
            }
        )
    return rows


def _p95(values: list[float]) -> float:
    finite = sorted(float(v) for v in values if math.isfinite(v))
    if not finite:
        return 0.0
    return float(np.percentile(np.asarray(finite, dtype=float), 95.0))


def _check(name: str, scope: str, value, limit, passed: bool) -> dict:
    return {
        "name": name,
        "scope": scope,
        "value": value,
        "limit": limit,
        "passed": bool(passed),
    }


def evaluate_fragmentation(
    mesh: MeshGraph,
    uvmap: UVMap,
    islands,
    seams,
    *,
    fold_angle: float = 90.0,
    min_island_uv_area: float,
    tiny_island_uv_area: float,
    tiny_island_count_max: int,
    tiny_island_area_ratio_max: float,
    sliver_aspect_min: float,
    sliver_uv_area_max: float,
    sliver_island_count_max: int,
    island_aspect_p95_max: float,
) -> dict:
    """Gate G7 island-count / fragmentation report (JSON-serialisable apart from
    ``aspect_ratio`` possibly being ``inf`` on a degenerate island)."""
    # Lazy import: keeps ``uv_agent.geometry`` free of a chart_uv_agent import at
    # module load time (the seam metric is the only thing that needs it).
    from chart_uv_agent.candidates import bbox_diagonal, seam_length

    rows = island_shape_rows(mesh, uvmap, islands, fold_angle=fold_angle)

    non_finite = any(not math.isfinite(r["uv_area"]) for r in rows)

    total_uv_area = float(sum(r["uv_area"] for r in rows))
    tiny_ids: list[int] = []
    sliver_ids: list[int] = []
    exempt_ids: list[int] = []
    zero_area_count = 0
    below_min_count = 0
    sliver_hard_count = 0
    tiny_uv_area = 0.0

    for row in rows:
        uv_area = row["uv_area"]
        exempt = bool(row["mandatory_bounded"])
        tiny = bool(uv_area < tiny_island_uv_area)
        sliver = bool(
            row["aspect_ratio"] >= sliver_aspect_min and uv_area <= sliver_uv_area_max
        )
        row["tiny"] = tiny
        row["sliver"] = sliver
        row["exempt_reason"] = "mandatory_bounded" if exempt else None
        if tiny:
            tiny_ids.append(row["island_id"])
            tiny_uv_area += uv_area
        if sliver:
            sliver_ids.append(row["island_id"])
        if exempt:
            exempt_ids.append(row["island_id"])
            continue
        if uv_area <= ZERO_AREA_UV_EPS:
            zero_area_count += 1
        if uv_area < min_island_uv_area:
            below_min_count += 1
        if sliver:
            sliver_hard_count += 1

    island_count = len(rows)
    one_two_face_count = sum(1 for r in rows if r["one_two_face"])
    tiny_area_ratio = (
        float(tiny_uv_area / total_uv_area) if total_uv_area > ZERO_AREA_UV_EPS else 0.0
    )
    aspect_p95 = _p95([r["aspect_ratio"] for r in rows])

    total_seam_length = seam_length(mesh, sorted(int(e) for e in seams))
    diagonal = bbox_diagonal(mesh)
    normalized_seam_length = (
        float(total_seam_length / diagonal) if diagonal > 0.0 else 0.0
    )

    metrics = {
        "island_count": int(island_count),
        "tiny_island_count": int(len(tiny_ids)),
        "tiny_island_area_ratio": float(tiny_area_ratio),
        "sliver_island_count": int(len(sliver_ids)),
        "one_two_face_island_count": int(one_two_face_count),
        "one_two_face_island_ratio": (
            float(one_two_face_count / island_count) if island_count else 0.0
        ),
        "island_aspect_p95": float(aspect_p95),
        "zero_area_island_count": int(zero_area_count),
        "below_min_area_island_count": int(below_min_count),
        "normalized_seam_length": float(normalized_seam_length),
        "seam_length_total": float(total_seam_length),
        "bbox_diagonal": float(diagonal),
    }

    checks = [
        _check("zero_area_islands", "hard", int(zero_area_count), 0, zero_area_count == 0),
        _check(
            "below_min_area_islands", "hard", int(below_min_count), 0, below_min_count == 0
        ),
        _check(
            "sliver_islands",
            "hard",
            int(sliver_hard_count),
            int(sliver_island_count_max),
            sliver_hard_count <= sliver_island_count_max,
        ),
        _check(
            "tiny_island_count",
            "quality",
            int(len(tiny_ids)),
            int(tiny_island_count_max),
            len(tiny_ids) <= tiny_island_count_max,
        ),
        _check(
            "tiny_island_area_ratio",
            "quality",
            float(tiny_area_ratio),
            float(tiny_island_area_ratio_max),
            tiny_area_ratio <= tiny_island_area_ratio_max,
        ),
        _check(
            "island_aspect_p95",
            "quality",
            float(aspect_p95),
            float(island_aspect_p95_max),
            aspect_p95 <= island_aspect_p95_max,
        ),
    ]

    failures = [c["name"] for c in checks if c["scope"] == "hard" and not c["passed"]]
    quality_failures = [
        c["name"] for c in checks if c["scope"] == "quality" and not c["passed"]
    ]
    hard_passed = not failures
    quality_passed = not quality_failures

    valid = not non_finite
    invalid_reasons: list[str] = [] if valid else ["non_finite_uv"]
    if not valid:
        hard_passed = False

    return {
        "gate": "G7",
        "valid": bool(valid),
        "invalid_reasons": invalid_reasons,
        "metrics": metrics,
        "checks": checks,
        "hard_passed": bool(hard_passed),
        "quality_passed": bool(quality_passed),
        "passed": bool(hard_passed),
        "failures": failures,
        "quality_failures": quality_failures,
        "islands": rows,
        "exempt_islands": exempt_ids,
        "tiny_island_ids": tiny_ids,
        "sliver_island_ids": sliver_ids,
    }


def compact_fragmentation(report: dict) -> dict:
    """The small G7 summary carried in a run record (drops the per-island rows)."""
    return {
        "metrics": report.get("metrics", {}),
        "passed": report.get("passed"),
        "hard_passed": report.get("hard_passed"),
        "quality_passed": report.get("quality_passed"),
        "failures": list(report.get("failures", [])),
        "quality_failures": list(report.get("quality_failures", [])),
        "exempt_islands": list(report.get("exempt_islands", [])),
        "tiny_island_ids": list(report.get("tiny_island_ids", [])),
        "sliver_island_ids": list(report.get("sliver_island_ids", [])),
    }
