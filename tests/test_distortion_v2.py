"""Gate G3 — analytical accuracy of the v2 distortion metrics.

Every case here is a closed-form fixture with a hand-computed expected value and the
tolerance from the G3 table, so a regression in
:mod:`uv_agent.geometry.distortion_v2` shows up as a number, not as a vibe. No Blender.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from chart_uv_agent.quality_profile import ENGINEERING_V0, evaluate_quality
from uv_agent.geometry.distortion_v2 import (
    METRIC_VERSION,
    compact_distortion_v2,
    evaluate_distortion_v2,
    per_face_anisotropy,
    triangle_singular_values,
)
from uv_agent.geometry.evaluation import _tri_signed_area_uv, _tris_from_face
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap
from uv_agent.io.fixtures import build_grid_plane

ANISO_TOL = 1e-6
AREA_TOL = 1e-9


def identity_uv(mesh: MeshGraph) -> UVMap:
    """UV = XY of the (planar) mesh: conformal and equiareal by construction."""
    uvmap = UVMap.for_mesh(mesh)
    for loop in mesh.loops:
        x, y, _ = mesh.vertices[loop.vertex_id].co
        uvmap.set(loop.index, x, y)
    return uvmap


def affine_uv(mesh: MeshGraph, a: float, b: float, c: float, d: float,
              tx: float = 0.0, ty: float = 0.0) -> UVMap:
    """UV = [[a, b], [c, d]] @ XY + (tx, ty)."""
    uvmap = UVMap.for_mesh(mesh)
    for loop in mesh.loops:
        x, y, _ = mesh.vertices[loop.vertex_id].co
        uvmap.set(loop.index, a * x + b * y + tx, c * x + d * y + ty)
    return uvmap


def l_shape_mesh() -> MeshGraph:
    """One concave hexagon (L), wound so that a NAIVE FAN from vertex 0 produces a
    flipped phantom triangle — the failure mode ``face_triangles`` must not have."""
    verts = [
        (2.0, 1.0, 0.0),  # reflex-adjacent start: fanning from here leaves the polygon
        (1.0, 1.0, 0.0),  # the reflex corner
        (1.0, 2.0, 0.0),
        (0.0, 2.0, 0.0),
        (0.0, 0.0, 0.0),
        (2.0, 0.0, 0.0),
    ]
    return MeshGraph.from_faces("lshape", verts, [[0, 1, 2, 3, 4, 5]])


# --- 1. plane, conformal and equiareal --------------------------------------------


def test_identity_uv_is_isotropic_and_area_preserving():
    mesh = build_grid_plane(4, 4)
    report = evaluate_distortion_v2(mesh, identity_uv(mesh))

    assert report["metric_version"] == METRIC_VERSION == 2
    assert report["island_source"] == "uv_connectivity"
    assert report["valid"] is True
    g = report["global"]
    for key in ("anisotropy_mean", "anisotropy_p95", "anisotropy_max"):
        assert g[key] == pytest.approx(1.0, abs=ANISO_TOL)
    for key in ("area_stretch_mean", "area_stretch_p95", "area_stretch_max"):
        assert abs(g[key]) <= AREA_TOL
    assert g["exceed_area_fraction"] == 0.0
    assert g["triangle_count"] == 2 * mesh.face_count
    assert len(report["islands"]) == 1


# --- 2. uniform scale / rotation / translation ------------------------------------


def test_similarity_transform_of_uv_changes_nothing():
    mesh = build_grid_plane(4, 4)
    base = evaluate_distortion_v2(mesh, identity_uv(mesh))

    t = math.radians(37.0)
    s = 3.0
    moved = affine_uv(
        mesh,
        s * math.cos(t), -s * math.sin(t),
        s * math.sin(t), s * math.cos(t),
        tx=1.25, ty=-0.75,
    )
    after = evaluate_distortion_v2(mesh, moved)

    for key in ("anisotropy_mean", "anisotropy_p95", "anisotropy_max"):
        assert abs(after["global"][key] - base["global"][key]) <= ANISO_TOL
    assert abs(after["global"]["area_stretch_mean"] - base["global"]["area_stretch_mean"]) <= AREA_TOL


# --- 3. 4x wide / 0.25x tall ------------------------------------------------------


def test_axis_aligned_stretch_is_anisotropy_sixteen():
    mesh = build_grid_plane(4, 4)
    report = evaluate_distortion_v2(mesh, affine_uv(mesh, 4.0, 0.0, 0.0, 0.25))

    g = report["global"]
    assert report["valid"] is True
    for key in ("anisotropy_mean", "anisotropy_p95", "anisotropy_max"):
        assert g[key] == pytest.approx(16.0, abs=ANISO_TOL)
    # det = 1 -> the global normalisation leaves area distortion at zero.
    assert abs(g["area_stretch_mean"]) <= AREA_TOL
    assert abs(g["area_stretch_max"]) <= AREA_TOL
    assert g["exceed_area_fraction"] == pytest.approx(1.0, abs=AREA_TOL)


# --- 4. area-preserving shear -----------------------------------------------------


def test_area_preserving_shear_has_zero_area_distortion_but_anisotropy():
    mesh = build_grid_plane(4, 4)
    k = 0.5
    report = evaluate_distortion_v2(mesh, affine_uv(mesh, 1.0, k, 0.0, 1.0))

    expected = ((k * k + 2.0) + k * math.sqrt(k * k + 4.0)) / 2.0
    g = report["global"]
    assert abs(g["area_stretch_mean"]) <= AREA_TOL
    assert abs(g["area_stretch_max"]) <= AREA_TOL
    assert g["anisotropy_max"] > 1.0
    for key in ("anisotropy_mean", "anisotropy_p95", "anisotropy_max"):
        assert g[key] == pytest.approx(expected, abs=ANISO_TOL)

    # Same number straight out of the per-triangle primitive.
    s1, s2, status = triangle_singular_values(
        (0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0), (1, 0), (k, 1)
    )
    assert status == "ok"
    assert s1 / s2 == pytest.approx(expected, abs=ANISO_TOL)


# --- 5. local UV collapse ---------------------------------------------------------


def test_local_uv_collapse_is_reported_and_fails_quality():
    mesh = build_grid_plane(4, 4)
    uvmap = identity_uv(mesh)
    collapsed_fid = 0
    for li in mesh.faces[collapsed_fid].loop_indices:
        uvmap.set(li, 0.125, -0.125)

    report = evaluate_distortion_v2(mesh, uvmap)
    deg = report["degenerate_triangles"]

    assert report["valid"] is True  # measurable; the failure is a quality failure
    assert deg["uv_degenerate_count"] == len(mesh.face_triangles(collapsed_fid)) == 2
    assert deg["invalid_count"] == 0
    assert deg["input_defect_count"] == 0
    assert collapsed_fid in deg["uv_degenerate_face_ids"]

    # The collapsed triangles are excluded from the statistics, not folded in as zeros.
    g = report["global"]
    assert g["triangle_count"] == 2 * mesh.face_count - 2
    assert g["anisotropy_mean"] == pytest.approx(1.0, abs=ANISO_TOL)
    assert g["anisotropy_max"] == pytest.approx(1.0, abs=ANISO_TOL)
    assert abs(g["area_stretch_mean"]) <= AREA_TOL

    # A zero-scoring report must NOT pass the profile.
    verdict = evaluate_quality(ENGINEERING_V0, report)
    assert verdict["valid"] is True
    assert verdict["passed"] is False
    assert "uv_degenerate_triangles" in verdict["failures"]

    assert math.isnan(per_face_anisotropy(mesh, uvmap)[collapsed_fid])


# --- 6. tiny bad patch ------------------------------------------------------------


def test_tiny_bad_patch_survives_the_global_mean():
    mesh = build_grid_plane(10, 10)  # 100 faces, total 3D area 1.0
    assert mesh.face_count == 100
    uvmap = identity_uv(mesh)

    bad_fid = 55
    loops = mesh.faces[bad_fid].loop_indices
    cu = sum(uvmap.get(li)[0] for li in loops) / len(loops)
    cv = sum(uvmap.get(li)[1] for li in loops) / len(loops)
    for li in loops:
        u, v = uvmap.get(li)
        uvmap.set(li, cu + (u - cu) * 2.0, cv + (v - cv) * 0.5)  # det = 1, ratio = 4

    all_faces = [f.id for f in mesh.faces]
    report = evaluate_distortion_v2(
        mesh, uvmap, [all_faces], regions={"patch": [bad_fid]}
    )
    assert report["island_source"] == "caller"

    g = report["global"]
    assert g["anisotropy_max"] == pytest.approx(4.0, abs=ANISO_TOL)
    # Area-weighted over 100 equal faces: 99 at 1.0 and one at 4.0.
    assert g["anisotropy_mean"] == pytest.approx((99 * 1.0 + 1 * 4.0) / 100.0, abs=1e-6)
    assert g["anisotropy_mean"] < 1.1  # the global mean alone would hide it
    assert g["anisotropy_p95"] == pytest.approx(1.0, abs=ANISO_TOL)

    island = report["islands"][0]
    face_area_fraction = mesh.faces[bad_fid].area_3d / g["area_3d"]
    assert face_area_fraction == pytest.approx(0.01, abs=AREA_TOL)
    assert island["exceed_area_fraction"] == pytest.approx(face_area_fraction, abs=AREA_TOL)
    assert report["worst_island_id"] == 0

    region = report["regions"]["patch"]
    assert region["anisotropy_p95"] == pytest.approx(4.0, abs=ANISO_TOL)
    assert region["anisotropy_max"] == pytest.approx(4.0, abs=ANISO_TOL)
    assert region["exceed_area_fraction"] == pytest.approx(1.0, abs=AREA_TOL)

    compact = compact_distortion_v2(report, max_islands=1)
    assert compact["metric_version"] == 2
    assert compact["island_count"] == 1
    assert compact["islands"][0]["island_id"] == 0
    assert compact["worst_island_id"] == 0


# --- 7. concave n-gon -------------------------------------------------------------


def test_concave_ngon_uses_real_triangulation_not_a_fan():
    mesh = l_shape_mesh()
    uvmap = identity_uv(mesh)
    report = evaluate_distortion_v2(mesh, uvmap)

    assert report["triangle_count"] == 4  # n - 2, no phantom triangle
    assert report["valid"] is True
    g = report["global"]
    assert g["area_3d"] == pytest.approx(3.0, abs=1e-12)  # the true L area
    for key in ("anisotropy_mean", "anisotropy_p95", "anisotropy_max"):
        assert g[key] == pytest.approx(1.0, abs=ANISO_TOL)
    assert abs(g["area_stretch_mean"]) <= AREA_TOL

    # Contrast: v1's fan leaves the polygon here, so its absolute UV areas over-count
    # (and one triangle is flipped) while the real triangulation matches the polygon.
    fan = [
        _tri_signed_area_uv(uvmap.get(l0), uvmap.get(l1), uvmap.get(l2))
        for l0, l1, l2 in _tris_from_face(mesh.faces[0].loop_indices)
    ]
    assert sum(abs(a) for a in fan) == pytest.approx(4.0, abs=1e-12)
    assert sum(abs(a) for a in fan) != pytest.approx(3.0, abs=1e-6)
    assert any(a < 0 for a in fan)


# --- 8. independent re-derivation of the weighted mean / p95 ----------------------


def test_area_weighted_mean_and_p95_match_a_hand_computation():
    verts = [
        (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0),  # area 1
        (2.0, 0.0, 0.0), (5.0, 0.0, 0.0), (5.0, 1.0, 0.0), (2.0, 1.0, 0.0),  # area 3
    ]
    mesh = MeshGraph.from_faces("two_quads", verts, [[0, 1, 2, 3], [4, 5, 6, 7]])
    uvmap = UVMap.for_mesh(mesh)
    for loop in mesh.loops:
        x, y, _ = mesh.vertices[loop.vertex_id].co
        # Face 0 conformal (ratio 1); face 1 stretched 2x in u (ratio 2).
        sx = 1.0 if loop.face_id == 0 else 2.0
        uvmap.set(loop.index, x * sx, y)

    report = evaluate_distortion_v2(mesh, uvmap)
    g = report["global"]
    assert g["area_3d"] == pytest.approx(4.0, abs=1e-12)
    assert g["anisotropy_mean"] == pytest.approx((1.0 * 1 + 2.0 * 3) / 4.0, abs=AREA_TOL)
    assert g["anisotropy_mean"] == pytest.approx(1.75, abs=AREA_TOL)
    assert g["anisotropy_p95"] == pytest.approx(2.0, abs=AREA_TOL)
    assert g["anisotropy_max"] == pytest.approx(2.0, abs=ANISO_TOL)

    per_face = per_face_anisotropy(mesh, uvmap)
    assert per_face[0] == pytest.approx(1.0, abs=ANISO_TOL)
    assert per_face[1] == pytest.approx(2.0, abs=ANISO_TOL)


# --- 9. NaN injection -------------------------------------------------------------


def test_nan_in_the_uv_map_makes_the_report_invalid():
    mesh = build_grid_plane(4, 4)
    uvmap = identity_uv(mesh)
    uvmap.uv[3] = (float("nan"), 0.0)

    report = evaluate_distortion_v2(mesh, uvmap)
    assert report["valid"] is False
    assert report["degenerate_triangles"]["invalid_count"] >= 1
    assert np.isnan(per_face_anisotropy(mesh, uvmap)[mesh.loops[3].face_id])

    verdict = evaluate_quality(ENGINEERING_V0, report)
    assert verdict["valid"] is False
    assert verdict["passed"] is False


# --- 10. the flat summary block (Gate G4) -----------------------------------------

SUMMARY_KEYS = {
    "metric_version",
    "global_area_stretch_mean",
    "global_area_stretch_p95",
    "global_anisotropy_p95",
    "global_anisotropy_max",
    "worst_island_id",
    "worst_island_area_stretch_p95",
    "worst_island_anisotropy_p95",
    "worst_island_anisotropy_max",
    "bad_area_ratio",
    "bad_area_threshold",
    "summary_valid",
}


def test_summary_block_mirrors_the_global_and_worst_island_rows():
    mesh = build_grid_plane(4, 4)
    report = evaluate_distortion_v2(mesh, identity_uv(mesh))

    s = report["summary"]
    assert set(s) == SUMMARY_KEYS
    assert s["metric_version"] == METRIC_VERSION == 2

    g = report["global"]
    assert s["global_anisotropy_p95"] == g["anisotropy_p95"]
    assert s["global_anisotropy_max"] == g["anisotropy_max"]
    assert s["global_area_stretch_mean"] == g["area_stretch_mean"]
    assert s["global_area_stretch_p95"] == g["area_stretch_p95"]
    assert s["bad_area_ratio"] == g["exceed_area_fraction"]
    assert s["bad_area_threshold"] == report["exceed_basis_anisotropy"]

    worst = report["islands"][report["worst_island_id"]]
    assert s["worst_island_id"] == report["worst_island_id"]
    assert s["worst_island_anisotropy_p95"] == worst["anisotropy_p95"]
    assert s["worst_island_anisotropy_max"] == worst["anisotropy_max"]
    assert s["worst_island_area_stretch_p95"] == worst["area_stretch_p95"]

    assert s["summary_valid"] is True


def test_summary_block_exposes_a_tiny_bad_patch():
    mesh = build_grid_plane(10, 10)
    uvmap = identity_uv(mesh)

    bad_fid = 55
    loops = mesh.faces[bad_fid].loop_indices
    cu = sum(uvmap.get(li)[0] for li in loops) / len(loops)
    cv = sum(uvmap.get(li)[1] for li in loops) / len(loops)
    for li in loops:
        u, v = uvmap.get(li)
        uvmap.set(li, cu + (u - cu) * 2.0, cv + (v - cv) * 0.5)  # det = 1, ratio = 4

    all_faces = [f.id for f in mesh.faces]
    report = evaluate_distortion_v2(mesh, uvmap, [all_faces])

    s = report["summary"]
    assert s["summary_valid"] is True
    assert s["bad_area_ratio"] > 0.0
    assert s["bad_area_ratio"] == report["global"]["exceed_area_fraction"]
    assert s["worst_island_anisotropy_max"] > report["global"]["anisotropy_mean"]
    assert s["worst_island_anisotropy_max"] == pytest.approx(4.0, abs=ANISO_TOL)


def test_compact_report_keeps_the_summary_block_unchanged():
    mesh = build_grid_plane(4, 4)
    report = evaluate_distortion_v2(mesh, identity_uv(mesh))

    compact = compact_distortion_v2(report)
    assert compact["summary"] == report["summary"]


def test_summary_valid_is_false_when_the_uv_map_has_a_nan():
    mesh = build_grid_plane(4, 4)
    uvmap = identity_uv(mesh)
    uvmap.uv[3] = (float("nan"), 0.0)

    report = evaluate_distortion_v2(mesh, uvmap)
    assert report["valid"] is False
    assert set(report["summary"]) == SUMMARY_KEYS
    assert report["summary"]["summary_valid"] is False
