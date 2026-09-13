"""Gate G7 (island count / fragmentation) tests for uv_agent.geometry.fragmentation."""

from __future__ import annotations

import math

import numpy as np

from chart_uv_agent.candidates import bbox_diagonal, seam_length
from chart_uv_agent.fixtures import build_folded_planes
from chart_uv_agent.segmentation import flood_charts, mandatory_seam_edges
from uv_agent.geometry.fragmentation import (
    compact_fragmentation,
    evaluate_fragmentation,
    island_shape_rows,
)
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap
from uv_agent.io.fixtures import build_grid_plane

#: Sliver target: aspect exactly 20 with a UV area of 0.005.
SLIVER_UV_AREA = 0.005
SLIVER_ASPECT = 20.0
STRIP_H = math.sqrt(SLIVER_UV_AREA / SLIVER_ASPECT)
STRIP_W = SLIVER_ASPECT * STRIP_H

THRESHOLDS = dict(
    min_island_uv_area=0.01,
    tiny_island_uv_area=0.01,
    tiny_island_count_max=0,
    tiny_island_area_ratio_max=0.05,
    sliver_aspect_min=8.0,
    sliver_uv_area_max=0.01,
    sliver_island_count_max=0,
    island_aspect_p95_max=6.0,
)


def _set_island_uv(mesh, uvmap, face_ids, *, sx, sy, ox=0.0, oy=0.0, x0=0.0, y0=0.0):
    """Planar XY -> UV for the given faces, scaled/offset so each island can be
    given an independent UV footprint."""
    for fid in face_ids:
        for li in mesh.faces[fid].loop_indices:
            x, y, _z = mesh.vertices[mesh.loops[li].vertex_id].co
            uvmap.set(li, ox + (x - x0) * sx, oy + (y - y0) * sy)


def _open_strip(n: int = 10) -> MeshGraph:
    """A 1 x n open quad strip in the XY unit square (every outline edge is a mesh
    boundary edge, so the whole strip is ``mandatory_bounded``)."""
    coords = []
    for i in range(n + 1):
        coords.append((i / n, 0.0, 0.0))
        coords.append((i / n, 1.0, 0.0))
    faces = [[2 * i, 2 * i + 2, 2 * i + 3, 2 * i + 1] for i in range(n)]
    return MeshGraph.from_faces("strip", coords, faces)


def _by_face_count(rows, count):
    matches = [r for r in rows if r["face_count"] == count]
    assert len(matches) == 1, f"expected one island with {count} faces, got {matches}"
    return matches[0]


# ------------------------------------------------------------------ (a)
def test_square_grid_plane_identity_uv_is_clean():
    mesh = build_grid_plane(nx=4, ny=4, size=1.0)
    uvmap = UVMap.for_mesh(mesh)
    _set_island_uv(mesh, uvmap, range(mesh.face_count), sx=1.0, sy=1.0, x0=-0.5, y0=-0.5)
    islands = [[f.id for f in mesh.faces]]

    report = evaluate_fragmentation(mesh, uvmap, islands, set(), **THRESHOLDS)

    assert report["valid"] is True
    assert report["metrics"]["island_count"] == 1
    assert report["metrics"]["tiny_island_count"] == 0
    assert report["metrics"]["sliver_island_count"] == 0
    assert report["metrics"]["zero_area_island_count"] == 0
    assert report["metrics"]["below_min_area_island_count"] == 0
    assert report["metrics"]["one_two_face_island_count"] == 0
    row = report["islands"][0]
    assert abs(row["uv_area"] - 1.0) < 1e-12
    assert abs(row["aspect_ratio"] - 1.0) < 1e-9
    assert abs(row["fill_ratio"] - 1.0) < 1e-9
    assert abs(report["metrics"]["island_aspect_p95"] - 1.0) < 1e-9
    assert report["hard_passed"] is True
    assert report["quality_passed"] is True
    assert report["passed"] is True
    assert report["failures"] == []


# ------------------------------------------------------------------ (b)
def test_mandatory_bounded_sliver_is_counted_but_exempt():
    mesh = _open_strip(10)
    uvmap = UVMap.for_mesh(mesh)
    _set_island_uv(mesh, uvmap, range(mesh.face_count), sx=STRIP_W, sy=STRIP_H)
    islands = [[f.id for f in mesh.faces]]

    rows = island_shape_rows(mesh, uvmap, islands)
    assert rows[0]["mandatory_bounded"] is True
    assert abs(rows[0]["uv_area"] - SLIVER_UV_AREA) < 1e-12
    assert abs(rows[0]["aspect_ratio"] - SLIVER_ASPECT) < 1e-9

    report = evaluate_fragmentation(mesh, uvmap, islands, set(), **THRESHOLDS)

    # Counted in the metric ...
    assert report["metrics"]["sliver_island_count"] == 1
    assert report["sliver_island_ids"] == [0]
    # ... but exempt from the hard failures.
    assert report["exempt_islands"] == [0]
    assert report["islands"][0]["exempt_reason"] == "mandatory_bounded"
    sliver_check = next(c for c in report["checks"] if c["name"] == "sliver_islands")
    assert sliver_check["scope"] == "hard"
    assert sliver_check["value"] == 0
    assert sliver_check["passed"] is True
    assert report["hard_passed"] is True
    assert report["passed"] is True
    assert "sliver_islands" not in report["failures"]


# ------------------------------------------------------------------ (c) + (e)
def _strip_split_from_plane():
    """A flat 10x4 plane cut by a NON-mandatory (flat, interior) seam into a 10-face
    sliver strip plus the 30-face remainder."""
    mesh = build_grid_plane(nx=10, ny=4, size=1.0)
    seams = set()
    strip_faces = [j * 10 + i for j in (0,) for i in range(10)]
    rest_faces = [f.id for f in mesh.faces if f.id not in set(strip_faces)]
    for i in range(10):
        f_lo = mesh.faces[i]
        f_hi = mesh.faces[10 + i]
        shared = set(f_lo.edge_ids) & set(f_hi.edge_ids)
        assert len(shared) == 1
        eid = shared.pop()
        edge = mesh.edges[eid]
        assert not edge.is_boundary and not edge.is_non_manifold
        assert edge.dihedral_angle == 0.0  # flat -> not a mandatory seam
        seams.add(eid)

    islands = flood_charts(mesh, seams)
    assert sorted(len(c) for c in islands) == [10, 30]

    uvmap = UVMap.for_mesh(mesh)
    # Strip: x spans 1.0, y spans 0.25 -> exactly the 20:1 / 0.005 sliver.
    _set_island_uv(mesh, uvmap, strip_faces, sx=STRIP_W, sy=STRIP_H / 0.25,
                   x0=-0.5, y0=-0.5)
    # Remainder: a healthy patch parked elsewhere in UV space.
    _set_island_uv(mesh, uvmap, rest_faces, sx=1.0, sy=1.0, oy=2.0, x0=-0.5, y0=-0.25)
    return mesh, uvmap, islands, seams


def test_non_mandatory_sliver_hard_fails():
    mesh, uvmap, islands, seams = _strip_split_from_plane()
    report = evaluate_fragmentation(mesh, uvmap, islands, seams, **THRESHOLDS)

    strip = _by_face_count(report["islands"], 10)
    assert strip["mandatory_bounded"] is False
    assert strip["exempt_reason"] is None
    assert abs(strip["uv_area"] - SLIVER_UV_AREA) < 1e-12
    assert abs(strip["aspect_ratio"] - SLIVER_ASPECT) < 1e-9
    assert strip["sliver"] is True

    assert report["exempt_islands"] == []
    assert report["metrics"]["sliver_island_count"] == 1
    sliver_check = next(c for c in report["checks"] if c["name"] == "sliver_islands")
    assert sliver_check["value"] == 1
    assert sliver_check["passed"] is False
    assert "sliver_islands" in report["failures"]
    assert report["hard_passed"] is False
    assert report["passed"] is False


def test_below_min_area_hard_fails_when_not_exempt():
    mesh, uvmap, islands, seams = _strip_split_from_plane()
    report = evaluate_fragmentation(mesh, uvmap, islands, seams, **THRESHOLDS)

    # uv_area 0.005 < min_island_uv_area 0.01 and the island is not mandatory bounded.
    assert report["metrics"]["below_min_area_island_count"] == 1
    check = next(c for c in report["checks"] if c["name"] == "below_min_area_islands")
    assert check["scope"] == "hard"
    assert check["limit"] == 0
    assert check["passed"] is False
    assert "below_min_area_islands" in report["failures"]
    # Tiny is a quality check, and it is reported too.
    assert report["metrics"]["tiny_island_count"] == 1
    assert "tiny_island_count" in report["quality_failures"]
    assert report["quality_passed"] is False


# ------------------------------------------------------------------ (d)
def test_aspect_ratio_is_rotation_invariant():
    mesh = _open_strip(10)
    uvmap = UVMap.for_mesh(mesh)
    _set_island_uv(mesh, uvmap, range(mesh.face_count), sx=STRIP_W, sy=STRIP_H)
    islands = [[f.id for f in mesh.faces]]
    base = island_shape_rows(mesh, uvmap, islands)[0]

    theta = math.radians(37.0)
    rot = np.array([[math.cos(theta), -math.sin(theta)],
                    [math.sin(theta), math.cos(theta)]])
    rotated = uvmap.copy()
    rotated.uv = rotated.uv @ rot.T
    turned = island_shape_rows(mesh, rotated, islands)[0]

    assert abs(turned["aspect_ratio"] - base["aspect_ratio"]) < 1e-6
    assert abs(turned["bbox_area"] - base["bbox_area"]) < 1e-12
    assert abs(turned["uv_area"] - base["uv_area"]) < 1e-12


# ------------------------------------------------------------------ (f)
def test_non_finite_uv_is_invalid_and_fails():
    mesh = _open_strip(4)
    uvmap = UVMap.for_mesh(mesh)
    _set_island_uv(mesh, uvmap, range(mesh.face_count), sx=1.0, sy=1.0)
    uvmap.uv[0] = (float("nan"), 0.0)
    islands = [[f.id for f in mesh.faces]]

    report = evaluate_fragmentation(mesh, uvmap, islands, set(), **THRESHOLDS)

    assert math.isnan(report["islands"][0]["uv_area"])
    assert report["valid"] is False
    assert report["invalid_reasons"] == ["non_finite_uv"]
    assert report["hard_passed"] is False
    assert report["passed"] is False


def test_empty_islands_pass():
    mesh = _open_strip(4)
    uvmap = UVMap.for_mesh(mesh)
    report = evaluate_fragmentation(mesh, uvmap, [], set(), **THRESHOLDS)

    assert report["valid"] is True
    assert report["metrics"]["island_count"] == 0
    assert report["metrics"]["tiny_island_count"] == 0
    assert report["metrics"]["island_aspect_p95"] == 0.0
    assert report["passed"] is True


# ------------------------------------------------------------------ (g)
def test_normalized_seam_length_matches_helpers():
    mesh = build_folded_planes(n=4)
    seams = mandatory_seam_edges(mesh)
    islands = flood_charts(mesh, seams)
    uvmap = UVMap.for_mesh(mesh)
    _set_island_uv(mesh, uvmap, range(mesh.face_count), sx=1.0, sy=1.0)

    report = evaluate_fragmentation(mesh, uvmap, islands, seams, **THRESHOLDS)

    expected_total = seam_length(mesh, sorted(seams))
    expected_diag = bbox_diagonal(mesh)
    assert abs(report["metrics"]["seam_length_total"] - expected_total) < 1e-12
    assert abs(report["metrics"]["bbox_diagonal"] - expected_diag) < 1e-12
    assert abs(
        report["metrics"]["normalized_seam_length"] - expected_total / expected_diag
    ) < 1e-12


# ------------------------------------------------------------------ (h)
def test_compact_fragmentation_keeps_listed_keys():
    mesh, uvmap, islands, seams = _strip_split_from_plane()
    report = evaluate_fragmentation(mesh, uvmap, islands, seams, **THRESHOLDS)
    compact = compact_fragmentation(report)

    assert set(compact) == {
        "metrics",
        "passed",
        "hard_passed",
        "quality_passed",
        "failures",
        "quality_failures",
        "exempt_islands",
        "tiny_island_ids",
        "sliver_island_ids",
    }
    assert compact["metrics"] == report["metrics"]
    assert compact["passed"] == report["passed"]
    assert compact["failures"] == report["failures"]
    assert compact["sliver_island_ids"] == report["sliver_island_ids"]
    assert "islands" not in compact


# ------------------------------------------------------------------ CG8 (px gate)
CG8_CAPS = dict(
    texture_size_px=1024,
    min_island_width_px=10.0,
    min_island_area_px2=100.0,
    max_island_bbox_aspect=8.0,
    max_island_perimeter_area_ratio=12.0,
    max_tiny_island_area_fraction=0.02,
)

_CG8_CHECK_NAMES = {
    "island_min_width_px",
    "island_min_area_px2",
    "island_bbox_aspect",
    "island_perimeter_area_ratio",
    "tiny_island_area_fraction",
}


def _unit_plane_uv():
    mesh = build_grid_plane(nx=4, ny=4, size=1.0)
    uvmap = UVMap.for_mesh(mesh)
    _set_island_uv(mesh, uvmap, range(mesh.face_count), sx=1.0, sy=1.0, x0=-0.5, y0=-0.5)
    return mesh, uvmap, [[f.id for f in mesh.faces]]


def test_px_metrics_on_unit_plane_at_1024():
    mesh, uvmap, islands = _unit_plane_uv()
    row = island_shape_rows(mesh, uvmap, islands, texture_size_px=1024)[0]

    assert abs(row["area_px2"] - 1024.0 ** 2) < 1e-6
    assert abs(row["min_width_px"] - 1024.0) < 1e-6
    assert abs(row["perimeter_px"] - 4096.0) < 1e-6
    assert abs(row["perimeter_area_ratio"] - 4.0) < 1e-9


def test_no_px_metrics_without_texture_size():
    mesh, uvmap, islands = _unit_plane_uv()
    row = island_shape_rows(mesh, uvmap, islands)[0]
    for key in ("area_px2", "min_width_px", "perimeter_px", "perimeter_area_ratio"):
        assert key not in row


def test_unit_plane_passes_the_px_gate():
    mesh, uvmap, islands = _unit_plane_uv()
    report = evaluate_fragmentation(
        mesh, uvmap, islands, set(), **THRESHOLDS, **CG8_CAPS
    )
    names = {c["name"] for c in report["checks"]}
    assert _CG8_CHECK_NAMES <= names
    assert report["metrics"]["texture_size_px"] == 1024
    # The whole plane is mesh bounded -> exempt, so there is no non-exempt minimum.
    assert report["islands"][0]["mandatory_bounded"] is True
    assert report["metrics"]["min_island_width_px"] is None
    assert report["metrics"]["island_area_px2_min"] is None
    assert report["hard_passed"] is True
    assert report["passed"] is True


def test_caps_none_adds_no_new_checks():
    mesh, uvmap, islands, seams = _strip_split_from_plane()
    report = evaluate_fragmentation(mesh, uvmap, islands, seams, **THRESHOLDS)
    names = {c["name"] for c in report["checks"]}
    assert not (_CG8_CHECK_NAMES & names)
    for key in ("texture_size_px", "min_island_width_px", "island_area_px2_min"):
        assert key not in report["metrics"]


def _narrow_split_strip(*, sx: float, sy: float):
    """The flat-seam split from ``_strip_split_from_plane`` with the 10-face strip
    re-parameterised to an arbitrary (narrow) UV footprint."""
    mesh, uvmap, islands, seams = _strip_split_from_plane()
    strip_faces = list(range(10))
    _set_island_uv(mesh, uvmap, strip_faces, sx=sx, sy=sy, x0=-0.5, y0=-0.5)
    return mesh, uvmap, islands, seams


def test_narrow_non_mandatory_strip_fails_min_width_px():
    # x spans 1.0 -> 0.5 long, y spans 0.25 -> 0.002 short: 2.05 px wide at 1024.
    mesh, uvmap, islands, seams = _narrow_split_strip(sx=0.5, sy=0.002 / 0.25)
    report = evaluate_fragmentation(
        mesh, uvmap, islands, seams, **THRESHOLDS, **CG8_CAPS
    )

    strip = _by_face_count(report["islands"], 10)
    assert strip["mandatory_bounded"] is False
    assert abs(strip["min_width_px"] - 2.048) < 1e-6
    assert strip["area_px2"] > 100.0  # only the width cap is under test here

    check = next(c for c in report["checks"] if c["name"] == "island_min_width_px")
    assert check["scope"] == "hard"
    assert check["value"] == 1
    assert check["limit"] == 0
    assert check["passed"] is False
    assert "island_min_width_px" in report["failures"]
    assert report["hard_passed"] is False
    assert abs(report["metrics"]["min_island_width_px"] - 2.048) < 1e-6


def test_narrow_mandatory_bounded_strip_is_exempt_from_min_width_px():
    mesh = _open_strip(10)
    uvmap = UVMap.for_mesh(mesh)
    _set_island_uv(mesh, uvmap, range(mesh.face_count), sx=0.5, sy=0.002)
    islands = [[f.id for f in mesh.faces]]

    rows = island_shape_rows(mesh, uvmap, islands, texture_size_px=1024)
    assert rows[0]["mandatory_bounded"] is True
    assert abs(rows[0]["min_width_px"] - 2.048) < 1e-6

    report = evaluate_fragmentation(
        mesh, uvmap, islands, set(), **THRESHOLDS, **CG8_CAPS
    )
    check = next(c for c in report["checks"] if c["name"] == "island_min_width_px")
    assert check["value"] == 0
    assert check["passed"] is True
    assert "island_min_width_px" not in report["failures"]
    assert report["exempt_islands"] == [0]
    # No non-exempt island -> no px minimum to report.
    assert report["metrics"]["min_island_width_px"] is None
    assert report["metrics"]["island_area_px2_min"] is None


def test_tiny_island_fails_min_area_px2():
    # 0.01 x 0.001 UV -> 1e-5 uv area -> 10.5 px^2 at 1024, below the 100 px^2 floor.
    mesh, uvmap, islands, seams = _narrow_split_strip(sx=0.01, sy=0.001 / 0.25)
    report = evaluate_fragmentation(
        mesh, uvmap, islands, seams, **THRESHOLDS, **CG8_CAPS
    )

    strip = _by_face_count(report["islands"], 10)
    assert strip["area_px2"] < 100.0
    check = next(c for c in report["checks"] if c["name"] == "island_min_area_px2")
    assert check["scope"] == "hard"
    assert check["value"] == 1
    assert check["limit"] == 0
    assert check["passed"] is False
    assert "island_min_area_px2" in report["failures"]
    assert report["metrics"]["island_area_px2_min"] < 100.0
