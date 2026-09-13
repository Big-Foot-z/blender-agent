"""Pure tests for the glTF input topology normalization core (G1-G5, G12, G14).

No ``bpy``: only :mod:`uv_agent.blender.topology_normalize`'s pure half is
exercised, on hand-built coordinate/face arrays that reproduce the exact shapes
the glTF importer produces (split-vertex shells, fake boundaries).
"""

from __future__ import annotations

import math

import pytest

from uv_agent.blender.topology_normalize import (
    NORMALIZATION_POLICY,
    TOPOLOGY_FIELDS,
    normalization_report,
    should_weld,
    topology_counts,
    weld_tolerance_for,
)

CUBE_COORDS = [
    (-1.0, -1.0, -1.0),
    (1.0, -1.0, -1.0),
    (1.0, 1.0, -1.0),
    (-1.0, 1.0, -1.0),
    (-1.0, -1.0, 1.0),
    (1.0, -1.0, 1.0),
    (1.0, 1.0, 1.0),
    (-1.0, 1.0, 1.0),
]
CUBE_QUADS = [
    [0, 3, 2, 1],
    [4, 5, 6, 7],
    [0, 1, 5, 4],
    [1, 2, 6, 5],
    [2, 3, 7, 6],
    [3, 0, 4, 7],
]


def _triangulated(faces):
    tris = []
    for f in faces:
        for k in range(1, len(f) - 1):
            tris.append([f[0], f[k], f[k + 1]])
    return tris


def _split_vertices(coords, faces):
    """One vertex per face corner - exactly what a flat-shaded glTF re-imports as."""
    out_coords = []
    out_faces = []
    for f in faces:
        face = []
        for vid in f:
            out_coords.append(tuple(coords[vid]))
            face.append(len(out_coords) - 1)
        out_faces.append(face)
    return out_coords, out_faces


def _translated(coords, offset):
    return [(c[0] + offset[0], c[1] + offset[1], c[2] + offset[2]) for c in coords]


# ---------------------------------------------------------------------------
# (a) genuine closed cube: nothing to repair
# ---------------------------------------------------------------------------
def test_closed_cube_has_no_boundary_no_duplicates():
    audit = topology_counts(CUBE_COORDS, CUBE_QUADS, dedupe_tolerance=1e-9)
    assert audit["vertex_count"] == 8
    assert audit["face_count"] == 6
    assert audit["edge_count"] == 12
    assert audit["boundary_edge_count"] == 0
    assert audit["non_manifold_edge_count"] == 0
    assert audit["connected_component_count"] == 1
    assert audit["duplicate_position_vertex_count"] == 0
    assert audit["duplicate_position_group_count"] == 0
    assert audit["surface_area"] == pytest.approx(24.0)
    assert audit["bbox_diagonal"] == pytest.approx(math.sqrt(12.0))
    # A quad mesh has no closed-triangle expectation.
    assert audit["expected_closed_triangle_edges"] is None
    assert audit["edge_excess"] is None
    assert should_weld("glb", audit) is False


def test_triangulated_closed_cube_edge_excess_is_zero():
    audit = topology_counts(CUBE_COORDS, _triangulated(CUBE_QUADS), dedupe_tolerance=1e-9)
    assert audit["face_count"] == 12
    assert audit["edge_count"] == 18
    assert audit["expected_closed_triangle_edges"] == 18
    assert audit["edge_excess"] == 0
    assert audit["boundary_edge_count"] == 0
    assert audit["connected_component_count"] == 1


# ---------------------------------------------------------------------------
# (b) split-vertex cube: fake boundary + fake shells (G3/G5)
# ---------------------------------------------------------------------------
def test_split_vertex_cube_is_all_fake_boundary_and_wants_a_weld():
    coords, faces = _split_vertices(CUBE_COORDS, CUBE_QUADS)
    audit = topology_counts(coords, faces, dedupe_tolerance=1e-9)
    assert audit["vertex_count"] == 24
    assert audit["face_count"] == 6
    assert audit["edge_count"] == 24
    # Every edge is used by exactly one face - the "boundary" is an artifact.
    assert audit["boundary_edge_count"] == 24
    assert audit["non_manifold_edge_count"] == 0
    assert audit["connected_component_count"] == 6
    assert audit["duplicate_position_vertex_count"] == 24
    assert audit["duplicate_position_group_count"] == 8
    assert should_weld("glb", audit) is True
    assert should_weld("gltf", audit) is True
    # Only glTF splits vertices this way; FBX/OBJ are left alone (G14 policy).
    assert should_weld("fbx", audit) is False
    assert should_weld("obj", audit) is False


# ---------------------------------------------------------------------------
# (c) genuinely separate shells must be preserved (G3/G5)
# ---------------------------------------------------------------------------
def test_two_disconnected_cubes_keep_two_components_and_are_not_welded():
    coords = list(CUBE_COORDS) + _translated(CUBE_COORDS, (100.0, 0.0, 0.0))
    faces = list(CUBE_QUADS) + [[i + 8 for i in f] for f in CUBE_QUADS]
    audit = topology_counts(coords, faces, dedupe_tolerance=1e-9)
    assert audit["vertex_count"] == 16
    assert audit["connected_component_count"] == 2
    assert audit["duplicate_position_vertex_count"] == 0
    assert audit["boundary_edge_count"] == 0
    assert should_weld("glb", audit) is False


# ---------------------------------------------------------------------------
# (d) genuine open boundary must be preserved (G3/G5)
# ---------------------------------------------------------------------------
def test_open_plane_keeps_its_perimeter_and_is_not_welded():
    coords = [(float(x), float(y), 0.0) for y in range(3) for x in range(3)]
    faces = []
    for y in range(2):
        for x in range(2):
            a = y * 3 + x
            faces.append([a, a + 1, a + 4, a + 3])
    audit = topology_counts(coords, faces, dedupe_tolerance=1e-9)
    assert audit["vertex_count"] == 9
    assert audit["face_count"] == 4
    assert audit["edge_count"] == 12
    assert audit["boundary_edge_count"] == 8  # the perimeter of a 2x2 quad grid
    assert audit["non_manifold_edge_count"] == 0
    assert audit["connected_component_count"] == 1
    assert audit["duplicate_position_vertex_count"] == 0
    assert should_weld("glb", audit) is False


# ---------------------------------------------------------------------------
# (e) genuine non-manifold must be reported, not hidden (G3/G5)
# ---------------------------------------------------------------------------
def test_non_manifold_fan_reports_one_non_manifold_edge():
    coords = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
              (0.0, 1.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, 1.0)]
    faces = [[0, 1, 2], [0, 1, 3], [0, 1, 4]]
    audit = topology_counts(coords, faces, dedupe_tolerance=1e-9)
    assert audit["non_manifold_edge_count"] == 1
    assert audit["boundary_edge_count"] == 6
    assert audit["connected_component_count"] == 1


# ---------------------------------------------------------------------------
# (f) finite-only: never substitute 0 for NaN
# ---------------------------------------------------------------------------
def test_nan_coordinate_raises_value_error():
    coords = list(CUBE_COORDS)
    coords[0] = (float("nan"), 0.0, 0.0)
    with pytest.raises(ValueError):
        topology_counts(coords, CUBE_QUADS)


# ---------------------------------------------------------------------------
# (g) report shape (G2)
# ---------------------------------------------------------------------------
def test_normalization_report_carries_every_g2_field_and_the_delta():
    pre_coords, pre_faces = _split_vertices(CUBE_COORDS, CUBE_QUADS)
    pre = topology_counts(pre_coords, pre_faces, dedupe_tolerance=1e-9)
    post = topology_counts(CUBE_COORDS, CUBE_QUADS, dedupe_tolerance=1e-9)
    weld = {"applied": True, "tolerance": 1e-6, "welded_vertex_count": 16,
            "guards": {"face_count_unchanged": True, "no_new_non_manifold": True,
                       "displacement_within_tolerance": True}}
    report = normalization_report("glb", merge_vertices_enabled=True,
                                  pre=pre, post=post, weld=weld)

    for key in TOPOLOGY_FIELDS:
        assert key in report, key
        assert report[key] == post[key]
    assert report["format"] == "glb"
    assert report["merge_vertices_enabled"] is True
    assert report["position_weld_applied"] is True
    assert report["welded_vertex_count"] == 16
    assert report["weld_tolerance"] == pytest.approx(1e-6)
    assert report["pre_normalization"] == pre
    assert report["post_normalization"] == post
    assert report["delta"] == {
        "vertex_delta": -16,
        "edge_delta": -12,
        "boundary_edge_delta": -24,
        "component_delta": -5,
        "non_manifold_delta": 0,
    }
    assert set(report["guards"]) == set(NORMALIZATION_POLICY["guards"])
    assert report["policy"] == NORMALIZATION_POLICY
    for value in (report["pre_normalization"]["surface_area"],
                  report["post_normalization"]["surface_area"],
                  report["weld_tolerance"]):
        assert math.isfinite(value)


def test_normalization_report_records_a_rejected_weld():
    audit = topology_counts(CUBE_COORDS, CUBE_QUADS, dedupe_tolerance=1e-9)
    weld = {"applied": False, "reason": "face_count_unchanged", "tolerance": 1e-6,
            "guards": {"face_count_unchanged": False, "no_new_non_manifold": True,
                       "displacement_within_tolerance": True}}
    report = normalization_report("glb", merge_vertices_enabled=True,
                                  pre=audit, post=audit, weld=weld)
    assert report["position_weld_applied"] is False
    assert report["weld_skip_reason"] == "face_count_unchanged"
    assert report["welded_vertex_count"] == 0


# ---------------------------------------------------------------------------
# (h) tolerance scaling (G12)
# ---------------------------------------------------------------------------
def test_weld_tolerance_scales_with_the_bounding_box():
    assert weld_tolerance_for(1.0) == pytest.approx(1e-7)
    assert weld_tolerance_for(1000.0) == pytest.approx(1e-4)
    assert weld_tolerance_for(0.0) == 0.0
    assert weld_tolerance_for(None) == 0.0
    assert weld_tolerance_for(2.0, factor=1e-3) == pytest.approx(2e-3)
    assert NORMALIZATION_POLICY["weld_tolerance_factor"] == pytest.approx(1e-7)


def test_tolerance_groups_near_duplicates_that_exact_matching_misses():
    coords, faces = _split_vertices(CUBE_COORDS, CUBE_QUADS)
    coords[0] = (coords[0][0] + 1e-9, coords[0][1], coords[0][2])
    exact = topology_counts(coords, faces)
    loose = topology_counts(coords, faces, dedupe_tolerance=1e-6)
    assert exact["duplicate_position_vertex_count"] == 23
    assert loose["duplicate_position_vertex_count"] == 24


# ---------------------------------------------------------------------------
# (i) determinism
# ---------------------------------------------------------------------------
def test_topology_counts_is_deterministic():
    coords, faces = _split_vertices(CUBE_COORDS, CUBE_QUADS)
    first = topology_counts(coords, faces, dedupe_tolerance=1e-9)
    second = topology_counts(coords, faces, dedupe_tolerance=1e-9)
    assert first == second
    assert list(first) == list(second)
