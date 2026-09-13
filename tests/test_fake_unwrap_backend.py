"""G4/G5 test infrastructure — run the chart-UV pipeline with no Blender at all.

These tests prove the deterministic fake unwrap backend
(:mod:`tests.helpers.fake_blender_uv`) can drive :func:`chart_uv_agent.pipeline.run_chart_uv`
end to end, so candidate accept/revert and restore behaviour becomes testable off-Blender.
"""

from __future__ import annotations

import math

from artist_uv_agent.user_seams import UserSeamSpec
from chart_uv_agent.fixtures import build_displaced_sphere, build_folded_planes
from chart_uv_agent.pipeline import run_chart_uv
from chart_uv_agent.segmentation import mandatory_seam_edges
from tests.helpers.fake_blender_uv import FakeUnwrapBackend


def _run_folded(monkeypatch, **kwargs):
    mesh = build_folded_planes(n=4)
    backend = FakeUnwrapBackend(mesh)
    obj = backend.install(monkeypatch)
    return mesh, backend, run_chart_uv(obj, mesh, **kwargs)


def test_folded_planes_runs_without_blender(monkeypatch):
    mesh, _backend, result = _run_folded(monkeypatch)
    assert isinstance(result, dict)
    mandatory = mandatory_seam_edges(mesh, fold_angle=90.0)
    assert mandatory, "fixture must have 90-degree fold edges"
    assert mandatory <= set(result["seams"])
    assert result["metrics"]["mandatory_90_missing"] == 0
    assert result["metrics"]["uv_bounds_ok"] is True


def test_backend_is_deterministic(monkeypatch):
    _m1, _b1, r1 = _run_folded(monkeypatch)
    _m2, _b2, r2 = _run_folded(monkeypatch)
    assert r1["seams"] == r2["seams"]
    for key, v1 in r1["metrics"].items():
        v2 = r2["metrics"][key]
        if isinstance(v1, float) and not isinstance(v1, bool):
            assert math.isclose(v1, v2, rel_tol=0.0, abs_tol=1e-9), key


def test_user_seam_strict_path_runs(monkeypatch):
    mesh = build_folded_planes(n=4)
    backend = FakeUnwrapBackend(mesh)
    obj = backend.install(monkeypatch)
    spec = UserSeamSpec(object=mesh.object_id,
                        user_seam_edges=set(mandatory_seam_edges(mesh, fold_angle=90.0)))
    result = run_chart_uv(obj, mesh, user_seam_spec=spec,
                          auto_refine_user_seams=False, repair_user_seams=False,
                          enforce_user_mandatory=False, gate_user_mandatory=False,
                          optimize_layout=False)
    assert result["mode"] == "user_seams"
    assert result["user_seams"]["auto_added_seams"] == 0


def test_unwrap_calls_are_recorded(monkeypatch):
    _mesh, backend, _result = _run_folded(monkeypatch)
    assert sum(1 for c in backend.calls if c[0] == "unwrap") >= 1


def test_displaced_sphere_completes(monkeypatch):
    mesh = build_displaced_sphere(segments=12, rings=8)
    backend = FakeUnwrapBackend(mesh)
    obj = backend.install(monkeypatch)
    result = run_chart_uv(obj, mesh, max_rounds=6)
    for key in ("seams", "metrics", "gate", "history", "chart_count", "conclusion"):
        assert key in result


def test_unwrap_variant_kwargs_are_accepted_and_recorded(monkeypatch):
    """CG5 — the same-seam re-unwrap variants must be expressible through the backend
    interface: the three Blender 5.1 solver kwargs are accepted and recorded verbatim."""
    mesh = build_folded_planes(n=4)
    backend = FakeUnwrapBackend(mesh)
    obj = backend.install(monkeypatch)
    seams = mandatory_seam_edges(mesh, fold_angle=90.0)

    backend.unwrap_and_pack(obj, seams, iterations=50, no_flip=True, fill_holes=True)
    unwrap_call = [c for c in backend.calls if c[0] == "unwrap"][-1]
    assert unwrap_call[4] == {"iterations": 50, "no_flip": True, "fill_holes": True}

    backend.reunwrap_faces(obj, [0], iterations=30, no_flip=True)
    re_call = [c for c in backend.calls if c[0] == "reunwrap_faces"][-1]
    assert re_call[4] == {"iterations": 30, "no_flip": True, "fill_holes": False}

    # defaults stay the pre-existing behaviour (nothing requested)
    backend.unwrap_and_pack(obj, seams)
    assert [c for c in backend.calls if c[0] == "unwrap"][-1][4] == {
        "iterations": None, "no_flip": False, "fill_holes": False}


def test_unwrap_variants_table_is_well_formed():
    """Pure check on the CG5 variant table (no Blender, no fake backend)."""
    from chart_uv_agent.unwrap import UNWRAP_VARIANTS

    assert len(UNWRAP_VARIANTS) >= 2
    ids = [v["id"] for v in UNWRAP_VARIANTS]
    assert len(ids) == len(set(ids)), f"duplicate variant ids: {ids}"
    allowed = {"CONFORMAL", "ANGLE_BASED", "MINIMUM_STRETCH"}
    for v in UNWRAP_VARIANTS:
        assert v["method"] in allowed, v
        assert isinstance(v["id"], str) and v["id"]
