"""Gate G5 (+ G4) — candidate accept / full restore / budget termination.

Blender-free: every run goes through :class:`tests.helpers.fake_blender_uv.FakeUnwrapBackend`,
so the loop's accept-and-revert discipline is testable without ``bpy``. The cases mirror the
G5 evidence list — an improvement-free candidate, an exception candidate and a worsening
candidate must all leave the seam set AND the UV coordinates byte-identical; the four budget
exits must each name themselves; three identical runs must agree to 1e-6.
"""

from __future__ import annotations

import numpy as np
import pytest

from chart_uv_agent import refinement_loop as R
from chart_uv_agent import segmentation
from chart_uv_agent.candidates import SeamCandidate
from chart_uv_agent.constraints import SeamConstraints
from chart_uv_agent.fixtures import build_displaced_sphere, build_folded_planes
from chart_uv_agent.quality_profile import ENGINEERING_V0
from chart_uv_agent.segmentation import mandatory_seam_edges
from tests.helpers.fake_blender_uv import FakeUnwrapBackend

PROFILE = ENGINEERING_V0


def _sphere(monkeypatch):
    mesh = build_displaced_sphere(segments=12, rings=8)
    backend = FakeUnwrapBackend(mesh)
    obj = backend.install(monkeypatch)
    seams = set(segmentation.segment(mesh).seams)
    return mesh, backend, obj, seams, SeamConstraints.build(mesh)


def _interior_edge(mesh, seams) -> int:
    """One 2-face, non-mandatory, non-seam edge — a cut that changes nothing."""
    mandatory = mandatory_seam_edges(mesh)
    for e in mesh.edges:
        if len(e.face_ids) == 2 and e.id not in mandatory and e.id not in seams:
            return int(e.id)
    raise AssertionError("fixture has no free interior edge")


def _fixed_candidates(cand):
    def _gen(mesh, charts, target_island, seams, constraints, face_stretch, *, max_candidates):
        return [cand]
    return _gen


def _unwrap_calls(backend) -> int:
    return sum(1 for c in backend.calls if c[0] == "unwrap")


# ------------------------------------------------------------------ 1. restore (G5)


@pytest.mark.parametrize("sabotage", ["none", "exception", "worse"])
def test_rejected_candidate_restores_seams_and_uvs(monkeypatch, sabotage):
    mesh, backend, obj, seams, constraints = _sphere(monkeypatch)
    edge = _interior_edge(mesh, seams)

    # Measure the clean starting layout FIRST, so the sabotage below cannot corrupt the
    # "before" the candidate is judged against.
    before = R.unwrap_and_measure(obj, mesh, seams, profile=PROFILE, margin=0.005,
                                  stage="refinement")
    uv_before = obj.uv.uv.copy()
    seams_before = set(seams)

    if sabotage == "none":
        # An extra interior edge that does not split anything: no improvement at all.
        cand = SeamCandidate(kind="short_cut", added_edges=frozenset({edge}),
                             target_island=0, reason="no_improvement",
                             seam_length=0.0, exposure_cost=0.0)
        expected = {"insufficient_improvement"}
    elif sabotage == "exception":
        cand = SeamCandidate(kind="unwrap_only", added_edges=frozenset(),
                             target_island=0, reason="boom",
                             seam_length=0.0, exposure_cost=0.0)

        def _raise(*args, **kwargs):
            raise RuntimeError("unwrap exploded")

        monkeypatch.setattr("chart_uv_agent.unwrap.unwrap_and_pack", _raise)
        expected = {"candidate_exception"}
    else:
        cand = SeamCandidate(kind="unwrap_only", added_edges=frozenset(),
                             target_island=0, reason="worse",
                             seam_length=0.0, exposure_cost=0.0)
        real = backend.unwrap_and_pack

        def _collapse(o, s, **kwargs):
            out = real(o, s, **kwargs)
            o.uv.uv *= 0.001            # every island piled on the origin -> overlap
            return out

        monkeypatch.setattr("chart_uv_agent.unwrap.unwrap_and_pack", _collapse)
        expected = {"correctness_regression"}

    monkeypatch.setattr(R, "generate_candidates", _fixed_candidates(cand))

    result = R.run_refinement(obj, mesh, seams, constraints=constraints, profile=PROFILE,
                              budget={"max_iterations": 1}, margin=0.005,
                              initial_measurement=before)

    assert result["candidate_history"], "the candidate must be recorded, not dropped"
    for record in result["candidate_history"]:
        assert record["accepted"] is False
        assert record["reason"] in expected

    assert result["seams"] == seams_before
    assert result["distortion_seams"] == set()
    assert np.array_equal(obj.uv.uv, uv_before)
    assert result["rejected_regions"], "a region with no usable candidate is recorded"


# ------------------------------------------------------------------ 2. acceptance


def test_failing_input_accepts_or_rejects_explicitly(monkeypatch):
    mesh, backend, obj, seams, constraints = _sphere(monkeypatch)
    result = R.run_refinement(obj, mesh, seams, constraints=constraints, profile=PROFILE,
                              budget={"max_iterations": 3}, margin=0.005)

    assert isinstance(result["termination"]["reason"], str)
    assert result["termination"]["budget"]["seed"] == PROFILE.seed

    accepted = [c for c in result["candidate_history"] if c["accepted"]]
    if accepted:
        splits = [h for h in result["history"] if h["action"] in ("split", "unwrap_only")]
        assert splits, "an accepted candidate must produce a history entry"
        applied = {e for h in splits for e in h["added_edges"]}
        assert applied <= result["seams"]
    else:
        assert [h for h in result["history"] if h["action"] == "reject_region"]


# ------------------------------------------------------------------ 3. budgets


def _termination(obj, mesh, seams, constraints, before, **budget):
    return R.run_refinement(obj, mesh, seams, constraints=constraints, profile=PROFILE,
                            budget=budget, margin=0.005, initial_measurement=before,
                            clock=lambda: 0.0)


def test_budget_terminations(monkeypatch):
    mesh, backend, obj, seams, constraints = _sphere(monkeypatch)
    before = R.unwrap_and_measure(obj, mesh, seams, profile=PROFILE, margin=0.005,
                                  stage="refinement")
    assert before["passed"] is False, "fixture must fail quality for a budget test"

    cases = [
        ({"max_iterations": 0}, "max_iterations"),
        ({"time_budget_s": 0.0}, "time_budget"),
        ({"island_cap": 1}, "island_cap"),
    ]
    for budget, expected in cases:
        result = _termination(obj, mesh, seams, constraints, before, **budget)
        assert result["termination"]["reason"] == expected, budget
        assert result["measurement"] is before
        assert result["seams"] == set(seams)
        assert result["termination"]["budget"]["max_iterations"] == budget.get(
            "max_iterations", PROFILE.max_iterations)


# --------------------------------------------------------------- 4. determinism


def test_three_runs_are_identical(monkeypatch):
    runs = []
    for _ in range(3):
        with monkeypatch.context() as mp:
            mesh, _backend, obj, seams, constraints = _sphere(mp)
            result = R.run_refinement(obj, mesh, seams, constraints=constraints,
                                      profile=PROFILE, budget={"max_iterations": 2},
                                      margin=0.005)
            runs.append((set(result["seams"]),
                         dict(result["measurement"]["distortion_v2"]["global"]),
                         result["termination"]["reason"]))

    first_seams, first_global, first_reason = runs[0]
    for seams_i, global_i, reason_i in runs[1:]:
        assert seams_i == first_seams
        assert reason_i == first_reason
        for key, value in first_global.items():
            assert abs(float(global_i[key]) - float(value)) <= 1e-6, key


# --------------------------------------------------------------- 5. constraints (G4)


def test_protected_candidate_is_never_unwrapped(monkeypatch):
    mesh, backend, obj, seams, constraints = _sphere(monkeypatch)
    edge = _interior_edge(mesh, seams)
    protected = SeamConstraints.build(mesh, protected={edge})
    assert edge in protected.forbidden

    before = R.unwrap_and_measure(obj, mesh, seams, profile=PROFILE, margin=0.005,
                                  stage="refinement")
    cand = SeamCandidate(kind="short_cut", added_edges=frozenset({edge}), target_island=0,
                         reason="protected", seam_length=0.0, exposure_cost=0.0)
    monkeypatch.setattr(R, "generate_candidates", _fixed_candidates(cand))

    unwraps_before = _unwrap_calls(backend)
    result = R.run_refinement(obj, mesh, seams, constraints=protected, profile=PROFILE,
                              budget={"max_iterations": 1}, margin=0.005,
                              initial_measurement=before)

    assert [c["reason"] for c in result["candidate_history"]] == ["constraint_violation"]
    assert result["candidate_history"][0]["protected_cut"] == [edge]
    assert _unwrap_calls(backend) == unwraps_before
    assert result["seams"] == set(seams)


# ---------------------------------------------------- 6. no cut for packing alone (G5)


def test_passing_input_cuts_nothing(monkeypatch):
    mesh = build_folded_planes(n=4)
    backend = FakeUnwrapBackend(mesh)
    obj = backend.install(monkeypatch)
    seams = set(segmentation.segment(mesh).seams)
    constraints = SeamConstraints.build(mesh)

    result = R.run_refinement(obj, mesh, seams, constraints=constraints, profile=PROFILE,
                              budget={"max_iterations": 4}, margin=0.005)

    assert result["termination"]["reason"] == "quality_passed"
    assert result["termination"]["candidates_evaluated"] == 0
    assert result["termination"]["iterations"] == 0
    assert result["seams"] == set(seams)
    assert result["distortion_seams"] == set()
    assert result["candidate_history"] == []
    assert result["passed"] is True


# ------------------------------------------------------------- 7. seam length report


def test_seam_length_report_splits_and_sums():
    mesh = build_folded_planes(n=4)
    seams = set(segmentation.segment(mesh).seams)
    mandatory = mandatory_seam_edges(mesh)
    user = {sorted(seams - mandatory)[0]} if (seams - mandatory) else set()

    report = R.seam_length_report(mesh, seams, mandatory=mandatory, user=user,
                                  distortion_seams=set())

    assert set(report) == {
        "mandatory", "user", "auxiliary", "total", "bbox_diagonal",
        "auxiliary_normalized", "mandatory_edge_count", "user_edge_count",
        "auxiliary_edge_count",
    }
    assert report["mandatory_edge_count"] == len(mandatory & seams)
    assert report["user_edge_count"] == len(user)
    assert (report["mandatory_edge_count"] + report["user_edge_count"]
            + report["auxiliary_edge_count"]) == len(seams)
    assert report["mandatory"] + report["user"] + report["auxiliary"] == pytest.approx(
        report["total"], abs=1e-9)
    assert report["bbox_diagonal"] > 0.0
    assert report["auxiliary_normalized"] == pytest.approx(
        report["auxiliary"] / report["bbox_diagonal"], abs=1e-12)
