"""Gate G7 (+ G4 / G12) — merge-back: no quality-preserving removable seam is left behind.

Blender-free: every run goes through :class:`tests.helpers.fake_blender_uv.FakeUnwrapBackend`,
so the accept-and-revert discipline is testable without ``bpy``.

The fixture is the two 90°-folded grids of :func:`build_folded_planes`, deliberately
over-segmented: an extra straight column of NON-mandatory edges cuts one flat half in two,
so the layout starts at 3 islands where 2 would do. The G7 claim is that merge-back gives
exactly that seam back — and nothing else: the fold wall is mandatory, and a locked or
``required`` group is not even trialled.
"""

from __future__ import annotations

import numpy as np
import pytest

from chart_uv_agent import merge_back as MB
from chart_uv_agent import refinement_loop as R
from chart_uv_agent.constraints import SeamConstraints
from chart_uv_agent.fixtures import build_folded_planes
from chart_uv_agent.quality_profile import ENGINEERING_V0
from chart_uv_agent.segmentation import flood_charts, mandatory_seam_edges
from tests.helpers.fake_blender_uv import FakeUnwrapBackend

PROFILE = ENGINEERING_V0
MARGIN = 0.005
GRID = 6


def _extra_seam_edges(mesh, n: int = GRID, column: int = 3) -> list[int]:
    """A straight interior column of grid A's edges (x = ``column``/n, y from 0 to 1).

    Grid A's vertices are laid out ``i * (n + 1) + j``, so this is the seam that splits the
    flat z=0 half into two charts without touching the fold.
    """
    def vertex(i: int, j: int) -> int:
        return i * (n + 1) + j

    return sorted(int(mesh.edge_key(vertex(column, j), vertex(column, j + 1)))
                  for j in range(n))


def _fixture(monkeypatch, *, locked=(), n: int = GRID):
    """``(mesh, backend, obj, seams, constraints, mandatory, extra)`` — 3 islands."""
    mesh = build_folded_planes(n=n)
    backend = FakeUnwrapBackend(mesh)
    obj = backend.install(monkeypatch)
    mandatory = set(mandatory_seam_edges(mesh))
    extra = _extra_seam_edges(mesh, n)
    assert not (set(extra) & mandatory), "the extra cut must be freely removable"
    seams = set(mandatory) | set(extra)
    assert len(flood_charts(mesh, seams)) == 3
    constraints = SeamConstraints.build(mesh, locked=locked)
    return mesh, backend, obj, seams, constraints, mandatory, extra


def _run(obj, mesh, seams, constraints, **kwargs):
    return MB.run_merge_back(obj, mesh, seams, constraints=constraints, profile=PROFILE,
                             margin=MARGIN, **kwargs)


# ------------------------------------------------------- (a) the G7 happy path


def test_over_segmented_layout_gives_the_unnecessary_seam_back(monkeypatch):
    mesh, backend, obj, seams, constraints, mandatory, extra = _fixture(monkeypatch)

    groups = MB.removable_seam_groups(mesh, seams, constraints)
    assert len(groups) == 1
    assert groups[0]["edges"] == extra

    result = _run(obj, mesh, seams, constraints)

    assert result["enabled"] is True
    assert result["accepted"] == 1
    assert result["island_count_before"] == 3
    assert result["island_count_after"] == 2
    assert result["complete"] is True
    assert result["reason"] == "no_removable_seam"
    assert result["mode"] == "preserve"
    assert result["removable_remaining"] == 0
    assert result["removed_edges"] == extra
    assert result["seams"] == set(mandatory)
    assert mandatory <= result["seams"], "a mandatory fold is never given back"
    assert not (set(result["removed_edges"]) & mandatory)
    assert result["seam_length_after"] < result["seam_length_before"]
    assert result["normalized_seam_length_after"] < result["normalized_seam_length_before"]
    assert result["measurement"]["passed"] is True

    record = result["history"][0]
    assert record["accepted"] is True
    assert record["reason"] == "accepted"
    assert record["island_count_before"] == 3
    assert record["island_count_after"] == 2

    # The object keeps the merged layout: it is exactly a fresh unwrap of the returned seams.
    kept = obj.uv.uv.copy()
    R.unwrap_and_measure(obj, mesh, result["seams"], profile=PROFILE, margin=MARGIN,
                         stage="check")
    assert np.array_equal(obj.uv.uv, kept)


# ----------------------------------------------- (b/c/d) nothing is removable


def test_mandatory_only_seams_have_no_removable_group(monkeypatch):
    mesh, backend, obj, _seams, constraints, mandatory, _extra = _fixture(monkeypatch)

    assert MB.removable_seam_groups(mesh, mandatory, constraints) == []

    result = _run(obj, mesh, mandatory, constraints)
    assert result["trials"] == 0
    assert result["accepted"] == 0
    assert result["complete"] is True
    assert result["reason"] == "no_removable_seam"
    assert result["seams"] == set(mandatory)


def test_locked_group_is_not_removable(monkeypatch):
    mesh, backend, obj, seams, constraints, _mandatory, extra = _fixture(
        monkeypatch, locked=tuple(_extra_seam_edges(build_folded_planes(n=GRID))))

    assert set(extra) <= constraints.locked
    assert MB.removable_seam_groups(mesh, seams, constraints) == []

    result = _run(obj, mesh, seams, constraints)
    assert result["trials"] == 0
    assert result["complete"] is True
    assert result["reason"] == "no_removable_seam"
    assert result["seams"] == seams


def test_required_set_blocks_the_group(monkeypatch):
    mesh, backend, obj, seams, constraints, _mandatory, extra = _fixture(monkeypatch)
    required = frozenset(extra)

    assert MB.removable_seam_groups(mesh, seams, constraints, required=required) == []

    result = _run(obj, mesh, seams, constraints, required=required)
    assert result["trials"] == 0
    assert result["complete"] is True
    assert result["reason"] == "no_removable_seam"
    assert result["seams"] == seams


# ------------------------------------------------- (e/f) the restore discipline


def test_rejected_trial_restores_seams_and_uvs(monkeypatch):
    mesh, backend, obj, seams, constraints, _mandatory, extra = _fixture(monkeypatch)

    before = R.unwrap_and_measure(obj, mesh, seams, profile=PROFILE, margin=MARGIN,
                                  stage="merge_back")
    uv_before = obj.uv.uv.copy()

    def _failing(o, m, trial_seams, **kwargs):
        islands = flood_charts(m, {int(e) for e in trial_seams})
        return {"passed": False, "island_count": len(islands), "islands": islands,
                "hard_failures": ["anisotropy_p95"]}

    monkeypatch.setattr(MB, "unwrap_and_measure", _failing)

    result = _run(obj, mesh, seams, constraints, initial_measurement=before)

    assert result["trials"] == 1
    assert result["accepted"] == 0
    assert result["complete"] is True
    assert result["reason"] == "no_removable_seam"
    assert result["seams"] == seams
    assert result["removed_edges"] == []
    assert result["island_count_after"] == 3
    assert np.array_equal(obj.uv.uv, uv_before)

    record = result["history"][0]
    assert record["accepted"] is False
    assert record["reason"] == "quality_failed"
    assert record["edges"] == extra
    assert record["hard_failures"] == ["anisotropy_p95"]


def test_raising_trial_is_restored_and_recorded(monkeypatch):
    mesh, backend, obj, seams, constraints, _mandatory, extra = _fixture(monkeypatch)

    before = R.unwrap_and_measure(obj, mesh, seams, profile=PROFILE, margin=MARGIN,
                                  stage="merge_back")
    uv_before = obj.uv.uv.copy()

    def _raise(*args, **kwargs):
        raise RuntimeError("unwrap exploded")

    monkeypatch.setattr("chart_uv_agent.unwrap.unwrap_and_pack", _raise)

    result = _run(obj, mesh, seams, constraints, initial_measurement=before)

    assert result["trials"] == 1
    assert result["accepted"] == 0
    assert result["seams"] == seams
    assert np.array_equal(obj.uv.uv, uv_before)

    record = result["history"][0]
    assert record["accepted"] is False
    assert record["reason"] == "exception"
    assert record["island_count_after"] is None
    assert "unwrap exploded" in record["error"]


# --------------------------------------------------------- (g/h) budget / skip


def test_zero_trial_budget_terminates_incomplete_with_the_remainder(monkeypatch):
    mesh, backend, obj, seams, constraints, _mandatory, _extra = _fixture(monkeypatch)

    result = _run(obj, mesh, seams, constraints, max_trials=0)

    assert result["trials"] == 0
    assert result["accepted"] == 0
    assert result["complete"] is False
    assert result["reason"] == "trial_budget"
    assert result["removable_remaining"] == 1
    assert result["seams"] == seams


def test_failing_input_layout_is_skipped_without_a_trial(monkeypatch):
    mesh, backend, obj, seams, constraints, _mandatory, _extra = _fixture(monkeypatch)

    before = R.unwrap_and_measure(obj, mesh, seams, profile=PROFILE, margin=MARGIN,
                                  stage="merge_back")
    before = dict(before)
    before["passed"] = False

    result = _run(obj, mesh, seams, constraints, initial_measurement=before,
                  repair_mode=False)

    assert result["trials"] == 0
    assert result["accepted"] == 0
    assert result["complete"] is True
    assert result["reason"] == "skipped_quality_failed"
    assert result["mode"] == "preserve"
    assert result["seams"] == seams
    assert result["history"] == []


# ------------------------------------- (h2) a gap-only trial failure is re-packed


def test_gap_only_trial_failure_is_repacked_and_accepted(monkeypatch):
    """G9/G11: a merged layout that fails ONLY the packing gap is re-packed, not rejected."""
    mesh, _backend, obj, seams, constraints, _mandatory, extra = _fixture(monkeypatch)

    before = R.unwrap_and_measure(obj, mesh, seams, profile=PROFILE, margin=MARGIN,
                                  stage="merge_back")

    def _gap_failing(o, m, trial_seams, **kwargs):
        measurement = dict(R.unwrap_and_measure(o, m, trial_seams, profile=PROFILE,
                                                margin=MARGIN, stage="merge_back"))
        correctness = dict(measurement["correctness"])
        correctness["passed"] = False
        correctness["checks"] = [
            dict(c, passed=(False if c["name"] == "island_gap" else c["passed"]))
            for c in correctness["checks"]
        ]
        measurement["correctness"] = correctness
        measurement["hard_failures"] = ["correctness_failed"]
        measurement["passed"] = False
        return measurement

    repaired: dict = {}

    def _repack_for_gap(o, m, trial_seams, **kwargs):
        fixed = dict(R.unwrap_and_measure(o, m, trial_seams, profile=PROFILE,
                                          margin=MARGIN, stage="merge_back"))
        fixed["gap_repack"] = {"attempts": 1, "passed": True}
        repaired["seams"] = {int(e) for e in trial_seams}
        return fixed

    monkeypatch.setattr(MB, "unwrap_and_measure", _gap_failing)
    monkeypatch.setattr(MB, "repack_for_gap", _repack_for_gap)

    result = _run(obj, mesh, seams, constraints, initial_measurement=before)

    assert result["accepted"] == 1
    assert result["removed_edges"] == extra
    assert repaired["seams"] == set(seams) - set(extra)

    record = result["history"][0]
    assert record["accepted"] is True
    assert record["reason"] == "accepted"
    assert record["gap_repack"] == {"attempts": 1, "passed": True}


# ------------------------------------------------------------ (i) determinism


def test_two_runs_agree_exactly():
    """Two independent runs on fresh fixtures must produce the same trace (G12)."""
    traces = []
    seam_sets = []
    for _ in range(2):
        with pytest.MonkeyPatch.context() as mp:
            mesh, _backend, obj, seams, constraints, _mand, _extra = _fixture(mp)
            result = _run(obj, mesh, seams, constraints)
            traces.append([(tuple(r["edges"]), r["accepted"], r["reason"])
                           for r in result["history"]])
            seam_sets.append(sorted(result["seams"]))

    assert traces[0] == traces[1]
    assert seam_sets[0] == seam_sets[1]


# ---------------------------------------- (j) CG10/CG8 repair mode on a FAILING layout


def _sliver_measurement(mesh, seams, *, sliver_ids=(), hard=("fragmentation_failed",)):
    """A synthetic FAILING measurement: ``sliver_ids`` are the over-segmentation offenders.

    Only the fields the repair accept rule reads are populated — the real measurement is
    what :mod:`chart_uv_agent.refinement_loop` already has its own tests for.
    """
    islands = flood_charts(mesh, {int(e) for e in seams})
    sliver_ids = [int(i) for i in sliver_ids]
    return {
        "passed": False,
        "island_count": len(islands),
        "islands": islands,
        "hard_failures": list(hard),
        "catastrophic": {"bad_triangle_count": 0, "near_collapse_count": 0,
                         "invalid_count": 0, "bad_region_count": 0,
                         "bad_area_fraction": 0.0, "max_anisotropy": 1.0},
        "correctness": {"overlap": {"overlap_area_total": 0.0},
                        "orientation": {"local_flip_count": 0},
                        "degenerate": {"uv_degenerate_count": 0}},
        "fragmentation": {
            "metrics": {"zero_area_island_count": 0, "below_min_area_island_count": 0},
            "checks": [{"name": "sliver_islands", "scope": "hard",
                        "value": len(sliver_ids), "limit": 0,
                        "passed": not sliver_ids}],
            "tiny_island_ids": [],
            "sliver_island_ids": sliver_ids,
        },
    }


def _over_segmented_fixture(monkeypatch, n: int = GRID):
    """Grid A cut into THREE strips ⇒ 4 islands and TWO removable groups."""
    mesh = build_folded_planes(n=n)
    backend = FakeUnwrapBackend(mesh)
    obj = backend.install(monkeypatch)
    mandatory = set(mandatory_seam_edges(mesh))
    extra = (set(_extra_seam_edges(mesh, n, column=2))
             | set(_extra_seam_edges(mesh, n, column=4)))
    assert not (extra & mandatory)
    seams = set(mandatory) | extra
    assert len(flood_charts(mesh, seams)) == 4
    return mesh, obj, seams, SeamConstraints.build(mesh)


def test_repair_mode_tries_the_sliver_group_first_and_accepts(monkeypatch):
    """CG10: a FAILING over-segmented layout is repaired, sliver-touching group first."""
    mesh, obj, seams, constraints = _over_segmented_fixture(monkeypatch)

    groups = MB.removable_seam_groups(mesh, seams, constraints)
    assert len(groups) == 2, groups
    # The group the DEFAULT order would trial LAST; repair mode must promote it because it
    # touches the sliver island.
    target = groups[-1]
    sliver_island = int(target["island_b"])
    assert sliver_island not in (groups[0]["island_a"], groups[0]["island_b"])

    before = _sliver_measurement(mesh, seams, sliver_ids=[sliver_island])

    def _clean(o, m, trial_seams, **kwargs):
        return _sliver_measurement(m, trial_seams, sliver_ids=[])

    monkeypatch.setattr(MB, "unwrap_and_measure", _clean)

    result = MB.run_merge_back(obj, mesh, seams, constraints=constraints, profile=PROFILE,
                               margin=MARGIN, initial_measurement=before)

    assert result["mode"] == "repair"
    first = result["history"][0]
    assert first["edges"] == target["edges"], "the sliver group must be trialled first"
    assert first["accepted"] is True
    assert first["reason"] == "accepted_repair"
    assert first["island_count_before"] == 4
    assert first["island_count_after"] == 3
    assert result["accepted"] == 1
    assert set(result["removed_edges"]) == set(target["edges"])
    assert result["seams"] == set(seams) - set(target["edges"])
    # The second group no longer touches a sliver and buys no hard-count improvement, so
    # it is refused for exactly that reason — merge-back does not merge for merging's sake.
    assert [r["reason"] for r in result["history"][1:]] == ["repair_not_improved"]
    assert result["complete"] is True
    assert result["reason"] == "no_removable_seam"


def test_repair_mode_rejects_a_trial_whose_hard_failures_grow(monkeypatch):
    mesh, _backend, obj, seams, constraints, _mandatory, extra = _fixture(monkeypatch)
    R.unwrap_and_measure(obj, mesh, seams, profile=PROFILE, margin=MARGIN,
                         stage="merge_back")
    uv_before = obj.uv.uv.copy()

    groups = MB.removable_seam_groups(mesh, seams, constraints)
    assert len(groups) == 1
    before = _sliver_measurement(mesh, seams, sliver_ids=[int(groups[0]["island_b"])])

    def _worse(o, m, trial_seams, **kwargs):
        return _sliver_measurement(m, trial_seams, sliver_ids=[],
                                   hard=("fragmentation_failed", "correctness_failed"))

    monkeypatch.setattr(MB, "unwrap_and_measure", _worse)

    result = MB.run_merge_back(obj, mesh, seams, constraints=constraints, profile=PROFILE,
                               margin=MARGIN, initial_measurement=before)

    assert result["mode"] == "repair"
    assert result["trials"] == 1
    assert result["accepted"] == 0
    assert result["history"][0]["reason"] == "hard_failures_grew"
    assert result["history"][0]["accepted"] is False
    assert result["seams"] == seams
    assert result["removed_edges"] == []
    assert result["complete"] is True
    assert result["reason"] == "no_removable_seam"
    assert np.array_equal(obj.uv.uv, uv_before)


# ------------------------- (k) CG10/CG14: the layout recipe travels through the trials


def test_overrides_are_replayed_in_every_trial(monkeypatch):
    """CG10: every merge-back trial RE-UNWRAPS, so it must replay the repair recipe too —
    otherwise a trial judges a layout whose repair was silently thrown away."""
    mesh, backend, obj, seams, constraints, _mandatory, _extra = _fixture(monkeypatch)

    charts = flood_charts(mesh, seams)
    # The folded plane: a chart no merge-back trial on this fixture can dissolve, so the
    # recipe row stays live for the initial measurement AND for the trial.
    chart = sorted(int(f) for f in max(charts, key=len))
    override = R.override_from_variant(
        chart, {"variant_id": "slim_iter50_noflip", "method": "MINIMUM_STRETCH",
                "iterations": 50, "no_flip": True, "fill_holes": False,
                "minimize_iters": 0},
        round_index=0, region_id=0)

    backend.calls.clear()
    result = _run(obj, mesh, seams, constraints, overrides=[override])

    assert result["trials"] >= 1
    reunwraps = [c for c in backend.calls if c[0] == "reunwrap_faces"]
    # One for the initial measurement, one for each trial's own unwrap.
    assert len(reunwraps) >= 1 + int(result["trials"]), backend.calls
    assert all(c[1] == chart for c in reunwraps)
    assert all(c[3] == "MINIMUM_STRETCH" for c in reunwraps)
    assert all(c[4] == {"iterations": 50, "no_flip": True, "fill_holes": False}
               for c in reunwraps)
    assert result["stale_overrides"] == []

    # The control: without the recipe nothing is re-unwrapped at all.
    backend.calls.clear()
    _run(obj, mesh, seams, constraints)
    assert not [c for c in backend.calls if c[0] == "reunwrap_faces"]
