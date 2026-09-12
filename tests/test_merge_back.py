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


def _extra_seam_edges(mesh, n: int = GRID) -> list[int]:
    """A straight interior column of grid A's edges (x = 3/n, y from 0 to 1).

    Grid A's vertices are laid out ``i * (n + 1) + j``, so this is the seam that splits the
    flat z=0 half into two charts without touching the fold.
    """
    def vertex(i: int, j: int) -> int:
        return i * (n + 1) + j

    return sorted(int(mesh.edge_key(vertex(3, j), vertex(3, j + 1))) for j in range(n))


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

    result = _run(obj, mesh, seams, constraints, initial_measurement=before)

    assert result["trials"] == 0
    assert result["accepted"] == 0
    assert result["complete"] is True
    assert result["reason"] == "skipped_quality_failed"
    assert result["seams"] == seams
    assert result["history"] == []


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
