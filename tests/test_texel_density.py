"""Gate G8 (texel density) tests for :mod:`uv_agent.geometry.texel_density`."""

from __future__ import annotations

import math

import numpy as np
import pytest

from chart_uv_agent.fixtures import build_folded_planes
from chart_uv_agent.segmentation import flood_charts, mandatory_seam_edges
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap
from uv_agent.geometry.texel_density import (
    compact_texel_density,
    evaluate_texel_density,
)

TEX = 1024
TOL = 1e-9


def _folded_case():
    """Two-island folded-plane fixture with an exact per-plane planar unwrap.

    Grid A (z = 0, the unit square in xy) gets ``(x, y)``; grid B (y = 1, rising in
    +z) gets ``(x, 1 + z)``. Both islands then have ``uv_area == area_3d == 1``, so
    both densities are exactly ``texture_size_px``."""
    mesh = build_folded_planes(n=2)
    islands = flood_charts(mesh, mandatory_seam_edges(mesh))
    assert len(islands) == 2

    uvmap = UVMap.for_mesh(mesh)
    for face in mesh.faces:
        flat = all(abs(mesh.vertex_co(vid)[2]) < 1e-12 for vid in face.vertex_ids)
        for li in face.loop_indices:
            x, y, z = mesh.vertex_co(mesh.loops[li].vertex_id)
            if flat:
                uvmap.set(li, float(x), float(y))
            else:
                uvmap.set(li, float(x), 1.0 + float(z))

    # island_id of the grid-A island (the one holding face 0, a z = 0 quad).
    a_id = next(i for i, fids in enumerate(islands) if 0 in fids)
    return mesh, uvmap, islands, a_id, 1 - a_id


def _evaluate(mesh, uvmap, islands, **kw):
    params = dict(
        texture_size_px=TEX, cv_max=0.05, outlier_tolerance=0.1, outlier_count_max=0
    )
    params.update(kw)
    return evaluate_texel_density(mesh, uvmap, islands, **params)


def _scale_island(mesh, uvmap, islands, island_id, s):
    for fid in islands[island_id]:
        for li in mesh.faces[fid].loop_indices:
            u, v = uvmap.get(li)
            uvmap.set(li, u * s, v * s)


# (a) equal density on both islands -------------------------------------------------
def test_uniform_two_island_layout_passes_with_zero_cv():
    mesh, uvmap, islands, a_id, b_id = _folded_case()
    rep = _evaluate(mesh, uvmap, islands)

    assert rep["valid"] is True
    assert rep["passed"] is True
    assert rep["failures"] == []
    assert rep["unmeasurable_island_count"] == 0
    assert rep["intentional_weighting_applied"] is False
    assert rep["density_mean"] == pytest.approx(TEX, abs=1e-6)
    assert rep["density_cv"] == pytest.approx(0.0, abs=TOL)
    assert rep["outlier_island_ids"] == []
    for row in rep["islands"]:
        assert row["area_3d"] == pytest.approx(1.0, abs=1e-12)
        assert row["uv_area"] == pytest.approx(1.0, abs=1e-12)
        assert row["density"] == pytest.approx(TEX, abs=1e-6)
        assert row["relative_density"] == pytest.approx(1.0, abs=TOL)
        assert row["outlier"] is False


# (b) one island scaled 2x ----------------------------------------------------------
def test_one_island_scaled_twice_is_an_outlier():
    mesh, uvmap, islands, a_id, b_id = _folded_case()
    _scale_island(mesh, uvmap, islands, b_id, 2.0)
    rep = _evaluate(mesh, uvmap, islands)

    rows = {r["island_id"]: r for r in rep["islands"]}
    # Densities: A = TEX, B = 2 * TEX (uv_area x4 under the square root).
    assert rows[b_id]["density"] / rows[a_id]["density"] == pytest.approx(2.0, abs=TOL)
    # The mean is 3D-area weighted over two equal-area islands -> 1.5 * TEX, so the
    # relative densities are 2/3 and 4/3 (their ratio is the 2x).
    assert rep["density_mean"] == pytest.approx(1.5 * TEX, rel=1e-12)
    assert rows[a_id]["relative_density"] == pytest.approx(2.0 / 3.0, abs=TOL)
    assert rows[b_id]["relative_density"] == pytest.approx(4.0 / 3.0, abs=TOL)
    assert rows[b_id]["outlier"] is True
    assert b_id in rep["outlier_island_ids"]
    assert rep["outlier_count"] == 2
    assert "texel_density_outliers" in rep["failures"]
    assert rep["passed"] is False


# (c) uniform scale of ALL UVs is invariant -----------------------------------------
def test_uniform_uv_scale_leaves_cv_relative_and_outliers_unchanged():
    mesh, uvmap, islands, a_id, b_id = _folded_case()
    _scale_island(mesh, uvmap, islands, b_id, 2.0)
    before = _evaluate(mesh, uvmap, islands)

    s = 0.37
    uvmap.uv *= s
    after = _evaluate(mesh, uvmap, islands)

    assert after["density_mean"] == pytest.approx(before["density_mean"] * s, rel=1e-12)
    assert after["density_cv"] == pytest.approx(before["density_cv"], abs=TOL)
    assert after["outlier_island_ids"] == before["outlier_island_ids"]
    assert after["outlier_count"] == before["outlier_count"]
    assert after["failures"] == before["failures"]
    rb = {r["island_id"]: r for r in before["islands"]}
    ra = {r["island_id"]: r for r in after["islands"]}
    for iid in rb:
        assert ra[iid]["density"] == pytest.approx(rb[iid]["density"] * s, rel=1e-12)
        assert ra[iid]["relative_density"] == pytest.approx(
            rb[iid]["relative_density"], abs=TOL
        )
        assert ra[iid]["outlier"] == rb[iid]["outlier"]


# (d) intentional weighting ---------------------------------------------------------
def test_intentional_weighting_accepts_the_denser_island():
    mesh, uvmap, islands, a_id, b_id = _folded_case()
    _scale_island(mesh, uvmap, islands, b_id, 2.0)
    rep = _evaluate(mesh, uvmap, islands, intentional_weighting={b_id: 2.0})

    assert rep["intentional_weighting_applied"] is True
    assert rep["intentional_weighting"] == {b_id: 2.0}
    assert rep["outlier_island_ids"] == []
    assert rep["outlier_count"] == 0
    assert "texel_density_outliers" not in rep["failures"]
    # cv is not a uniformity failure once the weighting is declared.
    assert rep["passed"] is True or rep["failures"] == ["texel_density_cv"]


# (e) NaN UV ------------------------------------------------------------------------
def test_non_finite_uv_makes_the_report_invalid():
    mesh, uvmap, islands, a_id, b_id = _folded_case()
    uvmap.set(mesh.faces[islands[a_id][0]].loop_indices[0], float("nan"), 0.0)
    rep = _evaluate(mesh, uvmap, islands)

    assert rep["valid"] is False
    assert rep["passed"] is False
    assert "texel_density_finite" in rep["failures"]


# (f) zero-area 3D island -----------------------------------------------------------
def test_zero_area_island_is_unmeasurable_and_does_not_fail():
    # One unit quad plus a fully collinear (zero area) quad, as two explicit islands.
    mesh = MeshGraph.from_faces(
        "degenerate_pair",
        [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0),
         (2.0, 0.0, 0.0), (3.0, 0.0, 0.0), (4.0, 0.0, 0.0), (5.0, 0.0, 0.0)],
        [[0, 1, 2, 3], [4, 5, 6, 7]],
    )
    uvmap = UVMap.for_mesh(mesh)
    for face in mesh.faces:
        for li in face.loop_indices:
            x, y, _z = mesh.vertex_co(mesh.loops[li].vertex_id)
            uvmap.set(li, float(x), float(y))

    rep = _evaluate(mesh, uvmap, [[0], [1]])

    assert rep["unmeasurable_island_count"] == 1
    rows = {r["island_id"]: r for r in rep["islands"]}
    assert rows[1]["density"] is None
    assert rows[1]["relative_density"] is None
    assert rows[1]["outlier"] is False
    assert rows[0]["density"] == pytest.approx(TEX, abs=1e-6)
    assert rep["density_cv"] == pytest.approx(0.0, abs=TOL)   # < 2 measurable islands
    assert rep["density_mean"] == pytest.approx(TEX, abs=1e-6)
    assert rep["passed"] is True
    assert rep["failures"] == []


# (g) compact ------------------------------------------------------------------------
def test_compact_keeps_the_summary_keys():
    mesh, uvmap, islands, a_id, b_id = _folded_case()
    rep = _evaluate(mesh, uvmap, islands)
    compact = compact_texel_density(rep)

    assert set(compact) == {
        "passed", "valid", "failures", "density_mean", "density_cv",
        "outlier_count", "outlier_island_ids", "unmeasurable_island_count",
        "intentional_weighting_applied",
    }
    assert compact["passed"] is True
    assert compact["valid"] is True
    assert compact["failures"] == []
    assert compact["density_cv"] == pytest.approx(0.0, abs=TOL)
    assert compact["outlier_count"] == 0
    assert compact["outlier_island_ids"] == []
    assert compact["unmeasurable_island_count"] == 0
    assert compact["intentional_weighting_applied"] is False
    assert math.isfinite(float(compact["density_mean"]))
    assert np.isfinite(rep["density_cv"])
