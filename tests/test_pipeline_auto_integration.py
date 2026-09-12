"""Gates G1/G2/G4/G5 — the automatic pipeline end to end, with no Blender at all.

Every run goes through :class:`tests.helpers.fake_blender_uv.FakeUnwrapBackend`, so the
no-spec automatic path, the locked/protected constraint plumbing, the user-assisted
automatic path, the legacy regression path and run-to-run determinism are all testable
off-Blender (``bpy`` is never imported).
"""

from __future__ import annotations

import json

from artist_uv_agent.user_seams import UserSeamSpec
from chart_uv_agent.fixtures import build_displaced_sphere
from chart_uv_agent.gate import ChartGateConfig
from chart_uv_agent.pipeline import run_chart_uv
from chart_uv_agent.segmentation import mandatory_seam_edges
from tests.helpers.fake_blender_uv import FakeUnwrapBackend

#: Keys the automatic result block must always carry (G1/G2 "auto 결과 블록").
AUTO_KEYS = (
    "distortion_v2", "correctness", "quality", "mandatory_audit", "quality_profile",
    "constraints", "auto_constraints", "candidate_history", "termination", "seam_length",
    "auto_passed", "uv_island_count", "islands_disagree", "seam_types",
)

#: The legacy (``use_refinement_loop=False``) round actions — the regression guard.
LEGACY_ACTIONS = {"split", "revert", "repack", "stop", "reject"}


def _sphere(monkeypatch):
    mesh = build_displaced_sphere(segments=12, rings=8)
    backend = FakeUnwrapBackend(mesh)
    obj = backend.install(monkeypatch)
    return mesh, backend, obj


def _free_edges(mesh, count: int, *, exclude=frozenset()) -> list[int]:
    """``count`` 2-face, non-mandatory edges — safe to lock or to protect."""
    mandatory = mandatory_seam_edges(mesh, fold_angle=90.0)
    out = [int(e.id) for e in mesh.edges
           if len(e.face_ids) == 2 and e.id not in mandatory and e.id not in exclude]
    assert len(out) >= count, "fixture has too few free interior edges"
    return out[:count]


# ------------------------------------------------------------------ 1. no-spec (G1/G2)


def test_no_spec_auto_run_has_every_result_block(monkeypatch):
    mesh, _backend, obj = _sphere(monkeypatch)
    result = run_chart_uv(obj, mesh, max_rounds=4,
                          budget={"max_candidates_per_round": 2})

    for key in AUTO_KEYS:
        assert key in result, key

    # The candidate log must survive plain json.dumps (no numpy, no UVMap).
    assert isinstance(result["candidate_history"], list)
    json.dumps(result["candidate_history"])

    assert result["distortion_v2"]["metric_version"] == 2
    assert result["metrics"]["metric_version"] == 2
    assert isinstance(result["termination"]["reason"], str)
    assert result["termination"]["reason"] in {
        "quality_passed", "max_rounds", "no_improving_candidate", "island_cap",
        "time_budget",
    }
    assert result["seam_length"]["total"] > 0.0

    mandatory = mandatory_seam_edges(mesh, fold_angle=90.0)
    assert mandatory, "fixture must have 90-degree fold edges"
    assert mandatory <= set(result["seams"])
    assert result["mandatory_audit"]["mandatory_90_missing"] == 0
    assert isinstance(result["auto_passed"], bool)
    # The report carries the G4/G5 evidence blocks.
    for key in ("constraints", "termination", "seam_length"):
        assert key in result["seam_report"], key


# ------------------------------------------------------- 2. locked / protected (G4)


def test_locked_seams_all_ship(monkeypatch):
    mesh, _backend, obj = _sphere(monkeypatch)
    locked = _free_edges(mesh, 5)

    result = run_chart_uv(obj, mesh, max_rounds=3, locked_seam_edges=locked,
                          budget={"max_candidates_per_round": 2})

    assert set(locked) <= set(result["seams"])
    assert result["auto_constraints"]["locked_missing"] == []
    assert result["auto_constraints"]["locked_seam_edges"] == sorted(locked)


def test_protected_edges_are_never_cut(monkeypatch):
    mesh, _backend, obj = _sphere(monkeypatch)
    protected = _free_edges(mesh, 12)

    result = run_chart_uv(obj, mesh, max_rounds=3, forbidden_edges=protected,
                          budget={"max_candidates_per_round": 2})

    forbidden = set(protected) - mandatory_seam_edges(mesh, fold_angle=90.0)
    assert set(result["seams"]) & forbidden == set()
    assert result["auto_constraints"]["protected_cut"] == []


# -------------------------------------------------- 3. user-assisted auto path (G5)


def test_user_assisted_auto_path_shares_the_core(monkeypatch):
    mesh, _backend, obj = _sphere(monkeypatch)
    free = _free_edges(mesh, 10)
    user_seams = set(free[:4])
    protected = set(free[6:])

    spec = UserSeamSpec(object=mesh.object_id, user_seam_edges=set(user_seams),
                        user_protected_edges=set(protected))
    result = run_chart_uv(obj, mesh, user_seam_spec=spec, auto_refine_user_seams=True,
                          repair_user_seams=True, enforce_user_mandatory=True,
                          gate_user_mandatory=True,
                          budget={"max_candidates_per_round": 2, "max_iterations": 2})

    assert result["mode"] == "user_seams"
    shipped = set(result["seams"])
    assert user_seams <= shipped
    assert shipped & (protected - mandatory_seam_edges(mesh, fold_angle=90.0)) == set()
    assert isinstance(result["candidate_history"], list)
    json.dumps(result["candidate_history"])
    for key in AUTO_KEYS:
        assert key in result, key
    assert result["auto_constraints"]["locked_missing"] == []
    assert result["auto_constraints"]["protected_cut"] == []


# ------------------------------------------------------------ 4. legacy regression


def test_legacy_path_is_unchanged(monkeypatch):
    mesh, _backend, obj = _sphere(monkeypatch)
    result = run_chart_uv(obj, mesh, max_rounds=4, use_refinement_loop=False)

    assert result["candidate_history"] == []
    assert result["termination"]["iterations"] == 0
    assert result["termination"]["candidates_evaluated"] == 0
    for rec in result["history"]:
        action = rec.get("action")
        if action is None:
            continue
        assert action in LEGACY_ACTIONS | {"region_protected_merge", "ok", "repair"}, action
    assert isinstance(result["metrics"]["stretch_score"], float)


# --------------------------------------------------------------- 5. determinism (G5)


def test_three_runs_are_identical(monkeypatch):
    runs = []
    for _ in range(3):
        with monkeypatch.context() as mp:
            mesh, _backend, obj = _sphere(mp)
            runs.append(run_chart_uv(obj, mesh, max_rounds=3,
                                     budget={"max_candidates_per_round": 2}))

    first = runs[0]
    for other in runs[1:]:
        assert other["seams"] == first["seams"]
        for key, value in first["metrics"].items():
            if isinstance(value, bool) or not isinstance(value, float):
                continue
            if value != value:                 # NaN compares unequal — both must be NaN
                assert other["metrics"][key] != other["metrics"][key], key
                continue
            assert abs(float(other["metrics"][key]) - value) <= 1e-6, key


# ------------------------------------------------- 6. packing margin / island gap (G1/G5)


def test_pack_margin_meets_profile_margin_px(monkeypatch):
    """G1: every pack uses a margin that satisfies margin_px @ texture_size_px, and both
    the resolved UV margin and the profile texture context are recorded on the result."""
    mesh, backend, obj = _sphere(monkeypatch)
    result = run_chart_uv(obj, mesh, max_rounds=3, margin=0.005,
                          texture_size_px=1024, margin_px=4,
                          budget={"max_candidates_per_round": 2})

    expected = max(0.005, 4 / 1024)
    assert result["pack_margin_uv"] == expected
    assert result["margin_px"] == 4
    assert result["texture_size_px"] == 1024
    assert result["quality_profile"]["margin_px"] == 4
    assert result["quality_profile"]["texture_size_px"] == 1024

    unwraps = [c for c in backend.calls if c[0] == "unwrap"]
    assert unwraps, "the run must have unwrapped at least once"
    assert all(float(c[2]) >= expected - 1e-12 for c in unwraps), \
        [float(c[2]) for c in unwraps]


def test_island_gap_failure_is_repacked_never_recut(monkeypatch):
    """G5 (packing 단독 문제로 추가 절개 0): an island-gap-ONLY correctness failure is
    resolved by re-packing wider; the shipped seam set is identical to the clean run."""
    from uv_agent.geometry import uv_correctness

    mesh, _backend, obj = _sphere(monkeypatch)
    baseline = run_chart_uv(obj, mesh, max_rounds=3, use_refinement_loop=False)

    mesh2, backend2, obj2 = _sphere(monkeypatch)
    real_audit = uv_correctness.island_gap_audit
    state = {"calls": 0}

    def failing_twice(*args, **kwargs):
        report = dict(real_audit(*args, **kwargs))
        state["calls"] += 1
        if state["calls"] <= 2:
            report.update({"passed": False, "min_gap_px": 1.0})
        else:
            report.update({"passed": True})
        return report

    monkeypatch.setattr(uv_correctness, "island_gap_audit", failing_twice)
    result = run_chart_uv(obj2, mesh2, max_rounds=3, use_refinement_loop=False)

    records = [h for h in result["history"] if h.get("stage") == "gap_repack"]
    assert len(records) == 1, result["history"]
    assert records[0]["attempts"] == 2
    assert records[0]["passed"] is True
    assert "min_gap_px" in records[0]

    # No seam was added/removed to fix the gap — the layout was only re-packed.
    assert result["seams"] == baseline["seams"]
    gap_repacks = [c for c in backend2.calls if c[0] == "repack"]
    assert len(gap_repacks) >= 2
    expected = result["pack_margin_uv"]
    assert [float(c[2]) for c in gap_repacks[-2:]] == [expected * 1.5, expected * 2.0]


# ------------------------------------------- 7. v2-only quality failure drives the loop (G4/G5)


def test_v2_quality_failure_triggers_refinement_when_v1_passes(monkeypatch):
    """G4/G5: the v1 stretch thresholds are relaxed so they cannot fire; the v2 quality
    profile still fails, and that alone must run the refinement loop (candidates really
    evaluated), never terminate with an untried 'no_improving_candidate'."""
    mesh, _backend, obj = _sphere(monkeypatch)
    config = ChartGateConfig(stretch_max=9.0, worst_island_distortion_max=9.0)

    result = run_chart_uv(obj, mesh, config=config, max_rounds=3,
                          budget={"max_candidates_per_round": 2})

    assert result["quality"]["passed"] is False, result["quality"]
    assert result["candidate_history"], result["termination"]
    assert result["termination"]["candidates_evaluated"] >= 1, result["termination"]
    assert result["termination"]["candidates_evaluated"] == len(result["candidate_history"])
    assert result["termination"]["iterations"] >= 1, result["termination"]
    assert result["termination"]["reason"] in {
        "quality_passed", "max_rounds", "no_improving_candidate", "island_cap",
        "time_budget", "no_failing_target",
    }


# ------------------------------------------------- 8. input defect diagnosis (G1)


def _with_zero_area_face(mesh):
    """``mesh`` plus one extra ZERO-AREA (collinear) triangle — an abnormal input."""
    from uv_agent.geometry.mesh_graph import MeshGraph

    coords = [tuple(v.co) for v in mesh.vertices]
    faces = [list(f.vertex_ids) for f in mesh.faces]
    base = len(coords)
    coords += [(10.0, 0.0, 0.0), (11.0, 0.0, 0.0), (12.0, 0.0, 0.0)]
    faces.append([base, base + 1, base + 2])
    return MeshGraph.from_faces(mesh.object_id, coords, faces)


def test_input_diagnostics_clean_fixture_is_ok(monkeypatch):
    mesh, _backend, obj = _sphere(monkeypatch)
    result = run_chart_uv(obj, mesh, max_rounds=2,
                          budget={"max_candidates_per_round": 2})

    diag = result["input_diagnostics"]
    assert diag["ok"] is True, diag
    assert diag["non_manifold_edge_count"] == 0
    assert diag["zero_area_face_count"] == 0
    assert diag["input_defect_triangle_count"] == 0
    assert diag["isolated_vertex_count"] == 0


def test_input_diagnostics_flags_a_zero_area_face(monkeypatch):
    from chart_uv_agent.pipeline import _input_diagnostics

    mesh = _with_zero_area_face(build_displaced_sphere(segments=12, rings=8))
    diag = _input_diagnostics(mesh, None)

    assert diag["zero_area_face_count"] >= 1, diag
    assert diag["ok"] is False, diag
