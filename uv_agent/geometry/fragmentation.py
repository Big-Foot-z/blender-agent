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

CG8 (pixel-space tiny/sliver gate) is OPT IN: pass ``texture_size_px`` to get the
per-island px metrics (``area_px2``, ``min_width_px``, ``perimeter_px``,
``perimeter_area_ratio``), and pass the caps as well to get the extra checks
``island_min_width_px`` / ``island_min_area_px2`` / ``island_bbox_aspect`` /
``island_perimeter_area_ratio`` (all HARD, limit 0 non-exempt offenders) plus the
QUALITY check ``tiny_island_area_fraction``. With ``texture_size_px`` or a cap left as
``None`` nothing new is evaluated, so every existing caller is unchanged. The px
definitions, with ``T = texture_size_px``:

``area_px2``
    ``uv_area * T**2``.
``min_width_px``
    the SHORT side of the PCA-oriented box, ``* T`` — the narrowest the island gets, so
    it is what a ``margin_px`` border has to fit inside.
``perimeter_px``
    sum of the island's UV boundary segment lengths ``* T``. The boundary is the same
    edge set as ``_boundary_edges`` (mesh boundary edges plus edges that separate this
    island from another), measured on the loops of the face INSIDE the island, matching
    ``uv_correctness._island_boundary_segments``. An island with no boundary edge at all
    (a closed surface unwrapped as one piece) falls back to the perimeter of its PCA box.
``perimeter_area_ratio``
    ``perimeter_px / sqrt(area_px2)`` — scale free (4.0 for a square), 0 when the area is 0.

The CG8 exemption is the same ``mandatory_bounded`` exemption as G7: such an island is
still measured and reported, but never a hard failure.
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


def _island_uv_perimeter(
    mesh: MeshGraph,
    uvmap: UVMap,
    face_ids,
    boundary,
    fv_loop: dict[tuple[int, int], int],
) -> float:
    """UV length of the island's boundary (CG8).

    One segment per boundary edge, taken from the loops of the incident face that is
    INSIDE the island (the same curves ``uv_correctness._island_boundary_segments``
    measures gaps between). Non-finite UVs make the total non-finite.
    """
    inside = set(int(f) for f in face_ids)
    total = 0.0
    for eid in boundary:
        edge = mesh.edges[eid]
        va, vb = edge.vertex_ids
        for fid in edge.face_ids:
            if int(fid) not in inside:
                continue
            la = fv_loop.get((int(fid), int(va)))
            lb = fv_loop.get((int(fid), int(vb)))
            if la is None or lb is None:
                continue
            p = uvmap.get(la)
            q = uvmap.get(lb)
            total += math.hypot(float(q[0]) - float(p[0]), float(q[1]) - float(p[1]))
    return float(total)


def island_shape_rows(
    mesh: MeshGraph,
    uvmap: UVMap,
    islands,
    *,
    fold_angle: float = 90.0,
    texture_size_px: int | None = None,
) -> list[dict]:
    """One row per island (a face-id list) describing its 3D / UV shape (Gate G7).

    When ``texture_size_px`` is given, each row also carries the CG8 pixel metrics
    ``area_px2`` / ``min_width_px`` / ``perimeter_px`` / ``perimeter_area_ratio``.
    """
    fv_loop: dict[tuple[int, int], int] = {}
    if texture_size_px is not None:
        for loop in mesh.loops:
            fv_loop[(int(loop.face_id), int(loop.vertex_id))] = int(loop.index)

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

        px: dict = {}
        if texture_size_px is not None:
            scale = float(texture_size_px)
            area_px2 = float(uv_area) * scale * scale
            min_width_px = float(short_side) * scale
            if boundary:
                perimeter_uv = _island_uv_perimeter(
                    mesh, uvmap, fids, boundary, fv_loop
                )
            elif math.isfinite(long_side) and math.isfinite(short_side):
                perimeter_uv = 2.0 * (float(long_side) + float(short_side))
            else:
                perimeter_uv = float("nan")
            perimeter_px = float(perimeter_uv) * scale
            if math.isfinite(area_px2) and area_px2 > 0.0:
                ratio = float(perimeter_px / math.sqrt(area_px2))
            else:
                ratio = 0.0
            px = {
                "area_px2": float(area_px2),
                "min_width_px": float(min_width_px),
                "perimeter_px": float(perimeter_px),
                "perimeter_area_ratio": float(ratio),
            }

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
                **px,
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
    texture_size_px: int | None = None,
    min_island_width_px: float | None = None,
    min_island_area_px2: float | None = None,
    max_island_bbox_aspect: float | None = None,
    max_island_perimeter_area_ratio: float | None = None,
    max_tiny_island_area_fraction: float | None = None,
) -> dict:
    """Gate G7 island-count / fragmentation report (JSON-serialisable apart from
    ``aspect_ratio`` possibly being ``inf`` on a degenerate island).

    CG8 is opt in: without ``texture_size_px`` (or with the caps left ``None``) the
    report is byte-for-byte the G7 report it always was.
    """
    # Lazy import: keeps ``uv_agent.geometry`` free of a chart_uv_agent import at
    # module load time (the seam metric is the only thing that needs it).
    from chart_uv_agent.candidates import bbox_diagonal, seam_length

    rows = island_shape_rows(
        mesh, uvmap, islands, fold_angle=fold_angle, texture_size_px=texture_size_px
    )

    non_finite = any(not math.isfinite(r["uv_area"]) for r in rows)

    total_uv_area = float(sum(r["uv_area"] for r in rows))
    tiny_ids: list[int] = []
    sliver_ids: list[int] = []
    exempt_ids: list[int] = []
    zero_area_count = 0
    below_min_count = 0
    sliver_hard_count = 0
    tiny_uv_area = 0.0
    # CG8 (only populated when texture_size_px is given): non-exempt offenders.
    narrow_count = 0
    small_area_count = 0
    wide_aspect_count = 0
    ragged_count = 0
    non_exempt_widths: list[float] = []
    non_exempt_areas_px2: list[float] = []

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
        if texture_size_px is None:
            continue
        non_exempt_widths.append(float(row["min_width_px"]))
        non_exempt_areas_px2.append(float(row["area_px2"]))
        if min_island_width_px is not None and row["min_width_px"] < min_island_width_px:
            narrow_count += 1
        if min_island_area_px2 is not None and row["area_px2"] < min_island_area_px2:
            small_area_count += 1
        if max_island_bbox_aspect is not None and row["aspect_ratio"] > max_island_bbox_aspect:
            wide_aspect_count += 1
        if (
            max_island_perimeter_area_ratio is not None
            and row["perimeter_area_ratio"] > max_island_perimeter_area_ratio
        ):
            ragged_count += 1

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

    if texture_size_px is not None:
        metrics["texture_size_px"] = int(texture_size_px)
        metrics["min_island_width_px"] = (
            float(min(non_exempt_widths)) if non_exempt_widths else None
        )
        metrics["island_area_px2_min"] = (
            float(min(non_exempt_areas_px2)) if non_exempt_areas_px2 else None
        )

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

    if texture_size_px is not None:
        if min_island_width_px is not None:
            checks.append(
                _check(
                    "island_min_width_px",
                    "hard",
                    int(narrow_count),
                    0,
                    narrow_count == 0,
                )
            )
        if min_island_area_px2 is not None:
            checks.append(
                _check(
                    "island_min_area_px2",
                    "hard",
                    int(small_area_count),
                    0,
                    small_area_count == 0,
                )
            )
        if max_island_bbox_aspect is not None:
            checks.append(
                _check(
                    "island_bbox_aspect",
                    "hard",
                    int(wide_aspect_count),
                    0,
                    wide_aspect_count == 0,
                )
            )
        if max_island_perimeter_area_ratio is not None:
            checks.append(
                _check(
                    "island_perimeter_area_ratio",
                    "hard",
                    int(ragged_count),
                    0,
                    ragged_count == 0,
                )
            )
        if max_tiny_island_area_fraction is not None:
            checks.append(
                _check(
                    "tiny_island_area_fraction",
                    "quality",
                    float(tiny_area_ratio),
                    float(max_tiny_island_area_fraction),
                    tiny_area_ratio <= max_tiny_island_area_fraction,
                )
            )

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
