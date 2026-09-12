"""Gate G1 (fold-angle epsilon) and Gate G3 (real n-gon triangulation) tests.

See docs/UV_AUTOMATION_ACCEPTANCE_GATES.ko.md G1 / G3 and
docs/UV_AUTOMATION_WORK_PLAN.ko.md §4.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from uv_agent.geometry.mesh_graph import (
    FOLD_SNAP_EPS_DEG,
    MeshGraph,
    _angle_between,
    ear_clip_triangulate,
    snap_fold_angle,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _folded_quads(angle_deg: float) -> MeshGraph:
    """Two unit quads sharing the edge (0,3), folded by ``angle_deg`` degrees.

    Face A lies in the XY plane (normal +Z) and extends towards +X. Face B
    extends along ``d`` which is (-1, 0, 0) rotated by ``angle_deg`` about the
    shared edge (the Y axis), so the angle between the two face normals -- the
    dihedral angle as this codebase defines it (0 = flat) -- is exactly
    ``angle_deg`` up to floating point.
    """
    phi = math.radians(angle_deg)
    d = (-math.cos(phi), 0.0, math.sin(phi))
    v0 = (0.0, 0.0, 0.0)
    v1 = (1.0, 0.0, 0.0)
    v2 = (1.0, 1.0, 0.0)
    v3 = (0.0, 1.0, 0.0)
    v4 = (v3[0] + d[0], v3[1] + d[1], v3[2] + d[2])
    v5 = (v0[0] + d[0], v0[1] + d[1], v0[2] + d[2])
    return MeshGraph.from_faces(
        "fold", [v0, v1, v2, v3, v4, v5], [[0, 1, 2, 3], [0, 3, 4, 5]]
    )


def _shared_dihedral(graph: MeshGraph) -> float:
    e = graph.edges[graph.edge_key(0, 3)]
    assert len(e.face_ids) == 2
    return e.dihedral_angle


def _raw_dihedral(graph: MeshGraph) -> float:
    """The un-snapped angle between the two face normals."""
    n1 = np.asarray(graph.faces[0].normal)
    n2 = np.asarray(graph.faces[1].normal)
    return _angle_between(n1, n2)


def _polygon_graph(points2d) -> MeshGraph:
    verts = [(float(x), float(y), 0.0) for x, y in points2d]
    return MeshGraph.from_faces("poly", verts, [list(range(len(verts)))])


def _shoelace(points: np.ndarray) -> float:
    x, y = points[:, 0], points[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _point_in_polygon(p, poly: np.ndarray) -> bool:
    """Ray casting; points strictly inside only."""
    x, y = float(p[0]), float(p[1])
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xint = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xint:
                inside = not inside
    return inside


L_HEX = [(0.0, 0.0), (3.0, 0.0), (3.0, 1.0), (1.0, 1.0), (1.0, 3.0), (0.0, 3.0)]
ARROW_7 = [
    (0.0, 0.0),
    (1.0, -2.0),
    (2.0, 0.0),
    (1.5, 0.0),
    (1.5, 2.0),
    (0.5, 2.0),
    (0.5, 0.0),
]


# --------------------------------------------------------------------------
# (1) G1 - fold angle boundary / epsilon
# --------------------------------------------------------------------------
def test_snap_fold_angle_boundaries_unit():
    assert snap_fold_angle(89.9) == pytest.approx(89.9)
    assert snap_fold_angle(90.1) == pytest.approx(90.1)
    assert snap_fold_angle(90.0 + 5e-6) == 90.0
    assert snap_fold_angle(90.0 - 5e-6) == 90.0
    # Just outside the epsilon stays untouched.
    assert snap_fold_angle(90.0 + 2 * FOLD_SNAP_EPS_DEG) != 90.0


def test_exact_right_angle_fixture_snaps_to_90():
    graph = _folded_quads(90.0)
    raw = _raw_dihedral(graph)
    # Floating point normals may land on 89.99999999... rather than exactly 90.
    assert raw == pytest.approx(90.0, abs=FOLD_SNAP_EPS_DEG)
    assert _shared_dihedral(graph) == 90.0
    assert _shared_dihedral(graph) >= 90.0


@pytest.mark.parametrize("angle", [89.9, 90.1])
def test_non_snapped_angles_are_preserved(angle):
    graph = _folded_quads(angle)
    got = _shared_dihedral(graph)
    assert got == pytest.approx(angle, abs=1e-9)
    assert got != 90.0
    if angle < 90.0:
        assert got < 90.0
    else:
        assert got > 90.0


@pytest.mark.parametrize("angle", [90.0 + 5e-6, 90.0 - 5e-6])
def test_within_epsilon_angles_snap_to_90(angle):
    graph = _folded_quads(angle)
    assert _shared_dihedral(graph) == 90.0


# --------------------------------------------------------------------------
# (2) G3 - concave n-gons: real triangulation, no phantom triangles
# --------------------------------------------------------------------------
@pytest.mark.parametrize("poly", [L_HEX, ARROW_7], ids=["l_hexagon", "arrow_7gon"])
def test_concave_ngon_triangulation_is_inside_and_consistent(poly):
    pts = np.asarray(poly, dtype=float)
    graph = _polygon_graph(poly)
    tris = graph.face_triangles(0)

    assert len(tris) == len(poly) - 2

    poly_sign = math.copysign(1.0, _shoelace(pts))
    for l0, l1, l2 in tris:
        idx = [graph.loops[li].vertex_id for li in (l0, l1, l2)]
        a, b, c = pts[idx[0]], pts[idx[1]], pts[idx[2]]
        signed = 0.5 * ((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
        assert abs(signed) > 1e-12, "degenerate triangle emitted"
        assert math.copysign(1.0, signed) == poly_sign, "winding is not consistent"
        centroid = (a + b + c) / 3.0
        assert _point_in_polygon(centroid, pts), "phantom triangle outside the polygon"


def test_concave_ngon_fan_would_produce_a_phantom_triangle():
    """Guards the premise: the old v1 fan is genuinely wrong on this fixture."""
    pts = np.asarray(ARROW_7, dtype=float)
    outside = 0
    for i in range(1, len(pts) - 1):
        centroid = (pts[0] + pts[i] + pts[i + 1]) / 3.0
        if not _point_in_polygon(centroid, pts):
            outside += 1
    assert outside > 0


def test_face_triangles_is_cached_on_the_face():
    graph = _polygon_graph(L_HEX)
    assert graph.faces[0].triangles == []
    first = graph.face_triangles(0)
    assert graph.faces[0].triangles == first
    assert graph.face_triangles(0) is first


def test_ear_clip_handles_degenerate_polygon_without_raising():
    pts = np.array([[0.0, 0.0], [1.0, 0.0], [2.0, 0.0], [3.0, 0.0]])
    tris = ear_clip_triangulate(pts)
    assert len(tris) == 2


# --------------------------------------------------------------------------
# (3) G3 - convex quad / triangle counts
# --------------------------------------------------------------------------
def test_convex_quad_yields_two_triangles():
    graph = _polygon_graph([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])
    assert len(graph.face_triangles(0)) == 2


def test_triangle_face_yields_one_triangle():
    graph = _polygon_graph([(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)])
    tris = graph.face_triangles(0)
    assert len(tris) == 1
    assert tris == [(0, 1, 2)]


# --------------------------------------------------------------------------
# (4) G3 - triangulated area equals polygon area
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "poly",
    [
        L_HEX,
        ARROW_7,
        [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)],
        [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)],
    ],
    ids=["l_hexagon", "arrow_7gon", "quad", "triangle"],
)
def test_triangulated_area_matches_polygon_area(poly):
    pts = np.asarray(poly, dtype=float)
    graph = _polygon_graph(poly)
    total = 0.0
    for l0, l1, l2 in graph.face_triangles(0):
        idx = [graph.loops[li].vertex_id for li in (l0, l1, l2)]
        a, b, c = pts[idx[0]], pts[idx[1]], pts[idx[2]]
        total += abs(
            0.5 * ((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
        )
    assert total == pytest.approx(abs(_shoelace(pts)), abs=1e-9)


def test_ngon_triangulation_area_matches_face_area_3d():
    """The same check through the 3D face area recorded on the graph."""
    graph = _polygon_graph(L_HEX)
    total = 0.0
    for l0, l1, l2 in graph.face_triangles(0):
        p = [graph.vertex_co(graph.loops[li].vertex_id) for li in (l0, l1, l2)]
        total += 0.5 * float(np.linalg.norm(np.cross(p[1] - p[0], p[2] - p[0])))
    assert total == pytest.approx(graph.faces[0].area_3d, abs=1e-9)


# --------------------------------------------------------------------------
# compatibility: positional Face construction still works
# --------------------------------------------------------------------------
def test_face_positional_construction_still_works():
    from uv_agent.geometry.mesh_graph import Face

    f = Face(0, [0, 1, 2], [0, 1, 2], [0, 1, 2], (0.0, 0.0, 1.0), 0.5, 3)
    assert f.material_index == 3
    assert f.triangles == []
