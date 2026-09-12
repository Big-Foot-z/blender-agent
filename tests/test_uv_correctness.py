"""Gate G1 final-UV correctness audits (no Blender required)."""

from __future__ import annotations

import math
import time

import numpy as np
import pytest

from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap
from uv_agent.geometry.uv_correctness import (
    border_gap_audit,
    bounds_audit,
    compact_correctness,
    degenerate_uv_audit,
    evaluate_correctness,
    exact_overlap_audit,
    island_gap_audit,
    orientation_audit,
    triangle_intersection_area,
)
from uv_agent.io.fixtures import build_cube, build_grid_plane


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def set_face_uv(mesh: MeshGraph, uvmap: UVMap, face_id: int, coords) -> None:
    """Assign UVs to a face's loops in polygon order."""
    for li, (u, v) in zip(mesh.faces[face_id].loop_indices, coords):
        uvmap.set(li, float(u), float(v))


def two_triangle_mesh() -> MeshGraph:
    """Two disjoint triangles (separate UV islands by construction)."""
    verts = [
        (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0),
        (3.0, 0.0, 0.0), (4.0, 0.0, 0.0), (3.0, 1.0, 0.0),
    ]
    faces = [[0, 1, 2], [3, 4, 5]]
    return MeshGraph.from_faces("two_tris", verts, faces)


def two_quad_mesh() -> MeshGraph:
    """Two disjoint quads (separate UV islands by construction)."""
    verts = [
        (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0),
        (3.0, 0.0, 0.0), (4.0, 0.0, 0.0), (4.0, 1.0, 0.0), (3.0, 1.0, 0.0),
    ]
    faces = [[0, 1, 2, 3], [4, 5, 6, 7]]
    return MeshGraph.from_faces("two_quads", verts, faces)


def square_fan_mesh() -> MeshGraph:
    """Unit square as 4 triangles around a centre vertex (one welded island)."""
    verts = [
        (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0),
        (0.5, 0.5, 0.0),
    ]
    faces = [[0, 1, 4], [1, 2, 4], [2, 3, 4], [3, 0, 4]]
    return MeshGraph.from_faces("fan", verts, faces)


def planar_uv(mesh: MeshGraph, *, scale: float = 1.0, offset: float = 0.0) -> UVMap:
    """UV = XY of the vertex, remapped from the mesh XY bounds into the tile."""
    uvmap = UVMap.for_mesh(mesh)
    xs = np.array([v.co[0] for v in mesh.vertices])
    ys = np.array([v.co[1] for v in mesh.vertices])
    spanx = max(float(xs.max() - xs.min()), 1e-12)
    spany = max(float(ys.max() - ys.min()), 1e-12)
    for loop in mesh.loops:
        co = mesh.vertices[loop.vertex_id].co
        u = (co[0] - float(xs.min())) / spanx * scale + offset
        v = (co[1] - float(ys.min())) / spany * scale + offset
        uvmap.set(loop.index, u, v)
    return uvmap


def square_uv(x0: float, y0: float, side: float):
    return [(x0, y0), (x0 + side, y0), (x0 + side, y0 + side), (x0, y0 + side)]


# ---------------------------------------------------------------------------
# 1. shared boundary: no overlap, but zero packing gap
# ---------------------------------------------------------------------------

def test_touching_islands_have_no_overlap_but_zero_gap():
    mesh = two_quad_mesh()
    uvmap = UVMap.for_mesh(mesh)
    set_face_uv(mesh, uvmap, 0, square_uv(0.1, 0.1, 0.3))
    set_face_uv(mesh, uvmap, 1, square_uv(0.4, 0.1, 0.3))  # shares the u=0.4 edge

    overlap = exact_overlap_audit(mesh, uvmap)
    assert overlap["island_count"] == 2
    assert overlap["overlap_area_total"] == pytest.approx(0.0, abs=1e-12)
    assert overlap["pair_count"] == 0
    assert overlap["passed"] is True

    gap = island_gap_audit(mesh, uvmap, texture_size_px=1024, margin_px=4)
    assert gap["min_gap_px"] == pytest.approx(0.0, abs=1e-9)
    assert gap["passed"] is False
    assert gap["closest_pair"] is not None


# ---------------------------------------------------------------------------
# 2. a known 0.01 overlap area
# ---------------------------------------------------------------------------

def _overlap_001_uvmap(mesh: MeshGraph) -> UVMap:
    """Two right triangles offset along u so the intersection area is exactly 0.01.

    Legs ``t`` at the origin and at ``(s, 0)``; the intersection is a right
    triangle with legs ``L = t - s`` and area ``L^2 / 2``."""
    t = 0.4
    leg = math.sqrt(0.02)  # L^2 / 2 == 0.01
    s = t - leg
    uvmap = UVMap.for_mesh(mesh)
    set_face_uv(mesh, uvmap, 0, [(0.0, 0.0), (t, 0.0), (0.0, t)])
    set_face_uv(mesh, uvmap, 1, [(s, 0.0), (s + t, 0.0), (s, t)])
    return uvmap


def test_overlap_area_matches_analytic_value_cross_island():
    mesh = two_triangle_mesh()
    uvmap = _overlap_001_uvmap(mesh)

    report = exact_overlap_audit(mesh, uvmap)
    assert report["overlap_area_total"] == pytest.approx(0.01, abs=1e-9)
    assert report["passed"] is False
    assert report["pair_count"] == 1
    assert report["cross_pair_count"] == 1
    assert report["self_pair_count"] == 0
    assert report["islands_source"] == "uv_connectivity"
    assert report["samples"][0]["area"] == pytest.approx(0.01, abs=1e-9)


def test_overlap_inside_one_island_counts_as_self_pair():
    mesh = two_triangle_mesh()
    uvmap = _overlap_001_uvmap(mesh)

    report = exact_overlap_audit(mesh, uvmap, [[0, 1]])
    assert report["overlap_area_total"] == pytest.approx(0.01, abs=1e-9)
    assert report["self_pair_count"] == 1
    assert report["cross_pair_count"] == 0
    assert report["islands_source"] == "provided"
    assert report["passed"] is False


# ---------------------------------------------------------------------------
# 3. analytic triangle intersection cases, both windings
# ---------------------------------------------------------------------------

def _reversed(tri):
    return [tri[2], tri[1], tri[0]]


@pytest.mark.parametrize("flip_a", [False, True])
@pytest.mark.parametrize("flip_b", [False, True])
def test_triangle_intersection_identical(flip_a, flip_b):
    tri = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)]
    a = _reversed(tri) if flip_a else tri
    b = _reversed(tri) if flip_b else tri
    assert triangle_intersection_area(a, b) == pytest.approx(0.5, abs=1e-12)


@pytest.mark.parametrize("flip_a", [False, True])
@pytest.mark.parametrize("flip_b", [False, True])
def test_triangle_intersection_containment(flip_a, flip_b):
    big = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)]
    small = [(0.1, 0.1), (0.3, 0.1), (0.1, 0.3)]
    a = _reversed(big) if flip_a else big
    b = _reversed(small) if flip_b else small
    assert triangle_intersection_area(a, b) == pytest.approx(0.02, abs=1e-12)
    assert triangle_intersection_area(b, a) == pytest.approx(0.02, abs=1e-12)


@pytest.mark.parametrize("flip_a", [False, True])
@pytest.mark.parametrize("flip_b", [False, True])
def test_triangle_intersection_half_of_unit_square(flip_a, flip_b):
    # Lower-right half of the unit square vs. the lower-left half: the overlap is
    # the triangle (0,0)-(1,0)-(0.5,0.5), area 0.25.
    lower_right = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]
    lower_left = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)]
    a = _reversed(lower_right) if flip_a else lower_right
    b = _reversed(lower_left) if flip_b else lower_left
    assert triangle_intersection_area(a, b) == pytest.approx(0.25, abs=1e-12)


def test_triangle_intersection_shared_edge_is_zero():
    a = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)]
    b = [(1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    assert triangle_intersection_area(a, b) == pytest.approx(0.0, abs=1e-15)
    assert triangle_intersection_area(_reversed(a), b) == pytest.approx(0.0, abs=1e-15)


# ---------------------------------------------------------------------------
# 4. orientation: mirrored island vs. local fold
# ---------------------------------------------------------------------------

def test_mirrored_island_is_reported_but_passes():
    mesh = build_grid_plane(nx=2, ny=2)
    uvmap = planar_uv(mesh)
    uvmap.uv[:, 0] = 1.0 - uvmap.uv[:, 0]  # mirror the whole island

    report = orientation_audit(mesh, uvmap)
    assert report["expected_orientation"] == "island_majority"
    assert report["island_count"] == 1
    assert report["local_flip_count"] == 0
    assert report["mirrored_island_count"] == 1
    assert report["mirrored_island_ids"] == [0]
    assert report["passed"] is True


def test_single_flipped_triangle_fails_orientation():
    mesh = square_fan_mesh()
    uvmap = planar_uv(mesh)
    # Push the centre vertex UV past the right edge: face 1 inverts, the rest do
    # not, and the island stays welded (UV is still per-vertex consistent).
    for loop in mesh.loops:
        if loop.vertex_id == 4:
            uvmap.set(loop.index, 1.5, 0.5)

    report = orientation_audit(mesh, uvmap)
    assert report["island_count"] == 1
    assert report["local_flip_count"] == 1
    assert report["local_flip_face_ids"] == [1]
    assert report["local_flip_area"] == pytest.approx(0.25, abs=1e-12)
    assert report["mirrored_island_count"] == 0
    assert report["passed"] is False


# ---------------------------------------------------------------------------
# 5. degenerate UV
# ---------------------------------------------------------------------------

def test_collapsed_face_is_a_uv_degenerate_failure():
    mesh = build_grid_plane(nx=2, ny=2)
    uvmap = planar_uv(mesh)
    set_face_uv(mesh, uvmap, 0, [(0.2, 0.2)] * 4)

    report = degenerate_uv_audit(mesh, uvmap)
    assert report["uv_degenerate_count"] >= 1
    assert 0 in report["uv_degenerate_face_ids"]
    assert report["input_defect_count"] == 0
    assert report["passed"] is False


def test_clean_plane_has_no_degenerate_uv():
    mesh = build_grid_plane(nx=2, ny=2)
    report = degenerate_uv_audit(mesh, planar_uv(mesh))
    assert report["uv_degenerate_count"] == 0
    assert report["passed"] is True


# ---------------------------------------------------------------------------
# 6. bounds
# ---------------------------------------------------------------------------

def test_bounds_just_outside_tolerance_fails():
    uvmap = UVMap(3)
    uvmap.set(0, 0.0, 0.0)
    uvmap.set(1, 1.0 + 2e-4, 0.5)
    uvmap.set(2, 0.5, 0.5)
    report = bounds_audit(uvmap, tol=1e-4)
    assert report["finite"] is True
    assert report["max_u"] == pytest.approx(1.0 + 2e-4)
    assert report["passed"] is False


def test_bounds_inside_tolerance_passes():
    uvmap = UVMap(3)
    uvmap.set(0, -5e-5, 0.0)
    uvmap.set(1, 1.0 + 5e-5, 0.5)
    uvmap.set(2, 0.5, 1.0)
    report = bounds_audit(uvmap, tol=1e-4)
    assert report["finite"] is True
    assert report["passed"] is True


def test_bounds_nan_is_not_finite():
    uvmap = UVMap(2)
    uvmap.set(0, 0.5, 0.5)
    uvmap.set(1, float("nan"), 0.5)
    report = bounds_audit(uvmap, tol=1e-4)
    assert report["finite"] is False
    assert report["passed"] is False


# ---------------------------------------------------------------------------
# 7. island packing gap
# ---------------------------------------------------------------------------

def _separated_islands(gap_px: float, texture_size_px: int = 1024):
    mesh = two_quad_mesh()
    uvmap = UVMap.for_mesh(mesh)
    d = gap_px / texture_size_px
    set_face_uv(mesh, uvmap, 0, square_uv(0.1, 0.1, 0.2))
    set_face_uv(mesh, uvmap, 1, square_uv(0.3 + d, 0.1, 0.2))
    return mesh, uvmap


def test_island_gap_passes_at_eight_pixels():
    mesh, uvmap = _separated_islands(8.0)
    report = island_gap_audit(mesh, uvmap, texture_size_px=1024, margin_px=4)
    assert report["min_gap_px"] == pytest.approx(8.0, abs=1e-6)
    assert report["min_gap_uv"] == pytest.approx(8.0 / 1024.0, abs=1e-9)
    assert report["passed"] is True
    assert report["closest_pair"] == {"island_a": 0, "island_b": 1} or \
        report["closest_pair"] == {"island_a": 1, "island_b": 0}


def test_island_gap_fails_at_two_pixels():
    mesh, uvmap = _separated_islands(2.0)
    report = island_gap_audit(mesh, uvmap, texture_size_px=1024, margin_px=4)
    assert report["min_gap_px"] == pytest.approx(2.0, abs=1e-6)
    assert report["passed"] is False


def test_single_island_gap_is_trivially_passed():
    mesh = build_grid_plane(nx=2, ny=2)
    report = island_gap_audit(mesh, planar_uv(mesh), texture_size_px=1024, margin_px=4)
    assert report["island_count"] == 1
    assert report["passed"] is True
    assert report["closest_pair"] is None


# ---------------------------------------------------------------------------
# 7b. tile-border gap (Gate G9: pixel padding / mip safety)
# ---------------------------------------------------------------------------

def _two_islands_at(u0a, v0a, u0b, v0b, side=0.2):
    mesh = two_quad_mesh()
    uvmap = UVMap.for_mesh(mesh)
    set_face_uv(mesh, uvmap, 0, square_uv(u0a, v0a, side))
    set_face_uv(mesh, uvmap, 1, square_uv(u0b, v0b, side))
    return mesh, uvmap


def test_border_gap_passes_when_islands_clear_every_border():
    # Both islands sit at u,v in [0.1, 0.7]: 102.4 px from the nearest border.
    mesh, uvmap = _two_islands_at(0.1, 0.1, 0.5, 0.5)
    report = border_gap_audit(mesh, uvmap, texture_size_px=1024, border_margin_px=8.0)
    assert report["island_count"] == 2
    assert report["min_gap_uv"] == pytest.approx(0.1, abs=1e-9)
    assert report["min_gap_px"] == pytest.approx(102.4, abs=1e-6)
    assert report["passed"] is True
    assert report["border_margin_px"] == 8.0
    assert report["texture_size_px"] == 1024
    assert report["closest_border"] in ("u0", "v0")
    assert report["closest_island"] is not None


def test_border_gap_fails_when_island_touches_u0():
    mesh, uvmap = _two_islands_at(0.0, 0.1, 0.5, 0.5)
    report = border_gap_audit(mesh, uvmap, texture_size_px=1024, border_margin_px=4.0)
    assert report["min_gap_px"] == pytest.approx(0.0, abs=1e-9)
    assert report["passed"] is False
    assert report["closest_border"] == "u0"
    assert report["closest_island"] is not None


def test_border_gap_two_pixels_from_v1_depends_on_the_margin():
    d = 2.0 / 1024.0
    mesh, uvmap = _two_islands_at(0.1, 1.0 - d - 0.2, 0.5, 0.1)
    strict = border_gap_audit(mesh, uvmap, texture_size_px=1024, border_margin_px=4.0)
    assert strict["min_gap_px"] == pytest.approx(2.0, abs=1e-6)
    assert strict["closest_border"] == "v1"
    assert strict["passed"] is False

    loose = border_gap_audit(mesh, uvmap, texture_size_px=1024, border_margin_px=2.0)
    assert loose["min_gap_px"] == pytest.approx(2.0, abs=1e-6)
    assert loose["passed"] is True


def test_border_gap_single_closed_island_falls_back_to_all_edges():
    # A cube is closed: no mesh-boundary edge and, with one welded island, no
    # inter-island edge either - the audit must still report a finite gap.
    mesh = build_cube()
    uvmap = UVMap.for_mesh(mesh)
    for loop in mesh.loops:
        co = mesh.vertices[loop.vertex_id].co
        uvmap.set(loop.index, 0.2 + 0.3 * (co[0] + 0.5), 0.2 + 0.3 * (co[1] + 0.5))

    report = border_gap_audit(mesh, uvmap, [list(range(len(mesh.faces)))],
                              texture_size_px=1024, border_margin_px=4.0)
    assert report["island_count"] == 1
    assert math.isfinite(report["min_gap_uv"])
    assert report["min_gap_uv"] == pytest.approx(0.2, abs=1e-9)
    assert report["min_gap_px"] == pytest.approx(204.8, abs=1e-6)
    assert report["passed"] is True
    assert report["closest_border"] in ("u0", "v0")
    assert report["closest_island"] == 0


# ---------------------------------------------------------------------------
# 8. aggregate
# ---------------------------------------------------------------------------

def test_evaluate_correctness_clean_case_passes_and_compacts():
    mesh = build_grid_plane(nx=3, ny=3)
    uvmap = planar_uv(mesh, scale=0.9, offset=0.05)

    report = evaluate_correctness(mesh, uvmap, texture_size_px=1024, margin_px=4)
    assert report["passed"] is True
    assert report["nan_triangle_count"] == 0
    assert report["island_count"] == 1
    assert report["islands_source"] == "uv_connectivity"
    assert [c["name"] for c in report["checks"]] == [
        "overlap", "orientation", "degenerate", "bounds", "island_gap",
        "border_gap"]
    assert all(c["passed"] for c in report["checks"])

    compact = compact_correctness(report)
    for key in ("passed", "checks", "overlap_area_total", "local_flip_count",
                "mirrored_island_count", "uv_degenerate_count",
                "min_island_gap_px", "bounds_ok"):
        assert key in compact
    assert compact["passed"] is True
    assert compact["bounds_ok"] is True
    assert compact["overlap_area_total"] == pytest.approx(0.0, abs=1e-12)


def test_evaluate_correctness_nan_fails_bounds_and_is_counted():
    mesh = build_grid_plane(nx=2, ny=2)
    uvmap = planar_uv(mesh)
    uvmap.set(mesh.faces[0].loop_indices[0], float("nan"), 0.5)

    report = evaluate_correctness(mesh, uvmap)
    assert report["bounds"]["finite"] is False
    assert report["passed"] is False
    assert report["nan_triangle_count"] >= 1


def test_evaluate_correctness_reports_the_border_gap_check_and_compacts_it():
    mesh = build_grid_plane(nx=3, ny=3)
    uvmap = planar_uv(mesh, scale=0.9, offset=0.05)

    report = evaluate_correctness(mesh, uvmap, texture_size_px=1024, margin_px=4,
                                  border_margin_px=8.0)
    assert report["border_margin_px"] == 8.0
    assert report["border_gap"]["min_gap_px"] == pytest.approx(51.2, abs=1e-6)
    check = next(c for c in report["checks"] if c["name"] == "border_gap")
    assert check["limit"] == 8.0
    assert check["value"] == pytest.approx(51.2, abs=1e-6)
    assert check["passed"] is True

    compact = compact_correctness(report)
    assert "min_border_gap_px" in compact
    assert compact["min_border_gap_px"] == pytest.approx(51.2, abs=1e-6)


def test_evaluate_correctness_border_margin_defaults_to_margin_px():
    mesh = build_grid_plane(nx=3, ny=3)
    uvmap = planar_uv(mesh, scale=0.9, offset=0.05)

    report = evaluate_correctness(mesh, uvmap, texture_size_px=1024, margin_px=6.0,
                                  border_margin_px=None)
    assert report["border_margin_px"] == 6.0
    assert report["border_gap"]["border_margin_px"] == 6.0
    check = next(c for c in report["checks"] if c["name"] == "border_gap")
    assert check["limit"] == 6.0


# ---------------------------------------------------------------------------
# 9. performance sanity
# ---------------------------------------------------------------------------

def test_exact_overlap_audit_scales_to_4000_triangles():
    mesh = build_grid_plane(nx=45, ny=45)  # 2025 quads -> 4050 triangles
    uvmap = planar_uv(mesh)

    start = time.perf_counter()
    report = exact_overlap_audit(mesh, uvmap)
    elapsed = time.perf_counter() - start

    assert report["overlap_area_total"] == pytest.approx(0.0, abs=1e-12)
    assert report["passed"] is True
    assert elapsed < 10.0
