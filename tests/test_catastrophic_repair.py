"""Gates CG5 / CG6 / CG13 — catastrophic repair ordering, acceptance and rollback.

Blender-free: every run goes through :class:`tests.helpers.fake_blender_uv.FakeUnwrapBackend`
with two thin wrappers that INJECT a catastrophic defect (one UV loop collapsed onto its
neighbour ⇒ a near-collapsed / needle triangle) so the repair loop has something real to
repair. The cases mirror the CG5/CG6 evidence list:

* R1 (same-seam re-unwrap) is tried FIRST and, when it works, the seam count grows by 0;
* R2 (relief seam) is only reached once every R1 variant has failed;
* every rejected trial restores the seams AND the exact ``uv_hash``;
* the island cap blocks the CUT, never the re-unwrap.
"""

from __future__ import annotations

import numpy as np
import pytest

from chart_uv_agent import catastrophic_repair as CR
from chart_uv_agent import refinement_loop as R
from chart_uv_agent import segmentation
from chart_uv_agent.constraints import SeamConstraints
from chart_uv_agent.fixtures import build_displaced_sphere
from chart_uv_agent.quality_profile import ENGINEERING_V0
from tests.helpers.fake_blender_uv import FakeUnwrapBackend
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.mesh_identity import uv_hash

PROFILE = ENGINEERING_V0

#: A face in the middle of the flat 4x4 grid chart (face index = i*4 + j).
NEEDLE_FACE = 5


# ------------------------------------------------------------------ fixtures


def _needle_backend(monkeypatch, mesh, *, fix_on_reunwrap: bool, face_id: int = NEEDLE_FACE):
    """A fake backend whose unwrap always lands ONE collapsed UV corner on ``face_id``.

    ``fix_on_reunwrap`` is the whole experiment: when True the R1 re-unwrap repairs the
    corner (so R1 must win with zero new seams), when False it re-injects it (so R1 must
    be rejected and the loop must fall through to R2).
    """
    backend = FakeUnwrapBackend(mesh)
    obj = backend.install(monkeypatch)
    loops = [int(li) for li in mesh.faces[int(face_id)].loop_indices]
    state = {"reunwrapped": False}

    def inject(o) -> None:
        # Collapse loop 0 onto loop 1: the quad's first triangle loses its area, which is
        # a near-collapse / needle in UV space and a degenerate triangle in correctness.
        o.uv.uv[loops[0]] = o.uv.uv[loops[1]]

    real_unwrap = backend.unwrap_and_pack
    real_reunwrap = backend.reunwrap_faces

    def unwrap_and_pack(o, seams, **kwargs):
        out = real_unwrap(o, seams, **kwargs)
        if not (state["reunwrapped"] and fix_on_reunwrap):
            inject(o)
        return out

    def reunwrap_faces(o, face_ids, **kwargs):
        out = real_reunwrap(o, face_ids, **kwargs)
        state["reunwrapped"] = True
        if not fix_on_reunwrap:
            inject(o)
        return out

    monkeypatch.setattr("chart_uv_agent.unwrap.unwrap_and_pack", unwrap_and_pack)
    monkeypatch.setattr("chart_uv_agent.unwrap.reunwrap_faces", reunwrap_faces)
    return backend, obj, state


def _grid_mesh(n: int = 4) -> MeshGraph:
    """One flat ``n x n`` quad grid: a SINGLE chart with zero mandatory seams.

    Single-chart on purpose — the fake backend's ``reunwrap_faces`` re-projects an island
    without re-normalising texel density across islands, so a second chart would make an
    R1 re-unwrap look like a (real, but irrelevant) texel-density regression and drown out
    the ordering behaviour under test."""
    coords = [(i / n, j / n, 0.0) for i in range(n + 1) for j in range(n + 1)]

    def vid(i: int, j: int) -> int:
        return i * (n + 1) + j

    faces = [[vid(i, j), vid(i + 1, j), vid(i + 1, j + 1), vid(i, j + 1)]
             for i in range(n) for j in range(n)]
    return MeshGraph.from_faces("flat_grid", coords, faces)


def _planes(monkeypatch, *, fix_on_reunwrap: bool):
    mesh = _grid_mesh(4)
    backend, obj, state = _needle_backend(monkeypatch, mesh, fix_on_reunwrap=fix_on_reunwrap)
    # Boundary edges are mandatory seams by definition; they do not split the grid.
    seams = set(segmentation.mandatory_seam_edges(mesh))
    assert len(segmentation.flood_charts(mesh, seams)) == 1
    return mesh, backend, obj, seams, SeamConstraints.build(mesh), state


# --------------------------------------------- (a) R1 wins with zero new seams (CG5)


def test_reunwrap_repair_wins_without_adding_a_single_seam(monkeypatch):
    mesh, _backend, obj, seams, constraints, _state = _planes(monkeypatch,
                                                              fix_on_reunwrap=True)
    before = R.unwrap_and_measure(obj, mesh, seams, profile=PROFILE, margin=0.005,
                                  stage="refinement")
    assert "catastrophic_failed" in before["hard_failures"], before["hard_failures"]

    target = R.select_target(before, set(), PROFILE)
    assert target is not None and target["kind"] == "catastrophic_repair"
    assert target["metric"] == R.CATASTROPHIC_METRIC
    assert NEEDLE_FACE in target["faces"]

    result = R.run_refinement(obj, mesh, seams, constraints=constraints, profile=PROFILE,
                              budget={"max_iterations": 1}, margin=0.005,
                              initial_measurement=before)

    # CG5 "seam 증가 0": the repair that works is the one that changes no seam at all.
    assert result["seams"] == set(seams)
    assert result["distortion_seams"] == set()

    accepted = [c for c in result["candidate_history"] if c.get("accepted")]
    assert accepted, result["candidate_history"]
    assert accepted[0]["kind"] == "reunwrap"
    assert accepted[0]["cut_reason"] == "catastrophic_repair"
    assert accepted[0]["variant_id"] == CR.R1_VARIANTS[0]["id"]
    assert accepted[0]["reason"] in ("region_cleared", "region_improved")

    rows = [h for h in result["history"] if h.get("cut_reason") == "catastrophic_repair"]
    assert rows and rows[0]["action"] == "reunwrap"
    assert rows[0]["added_edges"] == []
    assert rows[0]["bad_triangles_before"] > 0
    assert rows[0]["bad_triangles_after"] == 0
    assert rows[0]["bad_area_after"] <= rows[0]["bad_area_before"]
    assert result["termination"]["catastrophic_rounds"] == 1

    final = result["measurement"]
    assert "catastrophic_failed" not in final["hard_failures"], final["hard_failures"]


# ------------------------- (b) R1 fails -> R2 relief candidates, every trial restored


def test_failed_reunwrap_falls_through_to_relief_seams_and_restores(monkeypatch):
    mesh, _backend, obj, seams, constraints, _state = _planes(monkeypatch,
                                                              fix_on_reunwrap=False)
    before = R.unwrap_and_measure(obj, mesh, seams, profile=PROFILE, margin=0.005,
                                  stage="refinement")
    assert "catastrophic_failed" in before["hard_failures"]
    uv_before = obj.uv.uv.copy()
    hash_before = uv_hash(obj.uv)

    result = R.run_refinement(obj, mesh, seams, constraints=constraints, profile=PROFILE,
                              budget={"max_iterations": 1}, margin=0.005,
                              initial_measurement=before)

    records = [c for c in result["candidate_history"]
               if c.get("cut_reason") == "catastrophic_repair"]
    assert records
    r1 = [c for c in records if c["kind"] == "reunwrap"]
    r2 = [c for c in records if c["kind"] != "reunwrap"]

    assert r1, "R1 must be tried first"
    assert len(r1) == PROFILE.catastrophic_reunwrap_variants
    for record in r1:
        assert record["accepted"] is False
        assert record["reason"] == "catastrophic_not_improved", record["reason"]

    # CG5 ordering: R2 exists ONLY because every R1 variant failed.
    assert r2, "relief candidates must be generated once R1 is exhausted"
    for record in r2:
        assert record["cut_reason"] == "catastrophic_repair"
        assert record["candidate"]["added_edges"]

    # CG7: nothing survived a rejected trial — seams and the exact UV digest are back.
    assert not [c for c in records if c.get("accepted")], "no candidate can pass here"
    assert result["seams"] == set(seams)
    assert np.array_equal(obj.uv.uv, uv_before)
    assert uv_hash(obj.uv) == hash_before
    assert result["rejected_regions"]


# ------------------------------------------- (c) acceptance rules, one per rejection


def _measurement(*, bad_triangles: int, near_collapse: int = 0, invalid: int = 0,
                 bad_regions: int = 0, bad_area: float = 0.0, max_aniso: float = 1.0,
                 overlap: float = 0.0, flip: int = 0, degenerate: int = 0) -> dict:
    return {
        "catastrophic": {
            "bad_triangle_count": int(bad_triangles),
            "near_collapse_count": int(near_collapse),
            "invalid_count": int(invalid),
            "bad_region_count": int(bad_regions),
            "bad_area_fraction": float(bad_area),
            "max_anisotropy": float(max_aniso),
        },
        "correctness": {
            "overlap": {"overlap_area_total": float(overlap)},
            "orientation": {"local_flip_count": int(flip)},
            "degenerate": {"uv_degenerate_count": int(degenerate)},
        },
    }


BROKEN = _measurement(bad_triangles=4, near_collapse=2, bad_regions=1, bad_area=0.02,
                      max_aniso=20.0)
REPAIRED = _measurement(bad_triangles=0, max_aniso=2.0)
REGION_BEFORE = {"score": 20.0, "bad_triangle_count": 4}
REGION_CLEAR = {"score": 2.0, "bad_triangle_count": 0}


def _accept(**overrides) -> dict:
    kwargs = dict(before=BROKEN, after=REPAIRED, target_faces=frozenset({1, 2}),
                  mesh=None, correctness_ok=True, constraints_ok=True, mandatory_ok=True,
                  fragmentation_ok=True, island_cap_ok=True, regression_ok=True,
                  region_before=REGION_BEFORE, region_after=REGION_CLEAR)
    kwargs.update(overrides)
    return CR.accept_catastrophic_candidate(PROFILE, **kwargs)


def test_a_clean_repair_is_accepted():
    verdict = _accept()
    assert verdict["accepted"] is True
    assert verdict["reason"] == "region_cleared"
    assert verdict["checks"]["hard_max_ok"] is True
    assert verdict["improvement_ratio"] == pytest.approx(0.9)


@pytest.mark.parametrize("overrides,expected", [
    ({"correctness_ok": False}, "correctness_regression"),
    ({"constraints_ok": False}, "constraint_violation"),
    ({"mandatory_ok": False}, "mandatory_seam_violation"),
    # The hard anisotropy ceiling rose: a fixed needle traded for a new one.
    ({"after": _measurement(bad_triangles=0, max_aniso=25.0)}, "hard_max_increased"),
    # Same number of broken triangles after ⇒ not a repair.
    ({"after": _measurement(bad_triangles=4, near_collapse=2, bad_regions=1,
                            bad_area=0.02, max_aniso=20.0),
      "region_after": {"score": 20.0, "bad_triangle_count": 4}},
     "catastrophic_not_improved"),
    # A brand-new local flip.
    ({"after": _measurement(bad_triangles=0, max_aniso=2.0, flip=2)},
     "correctness_counter_increased"),
    ({"fragmentation_ok": False}, "fragmentation_limit_exceeded"),
    ({"island_cap_ok": False}, "island_cap_reached"),
    ({"regression_ok": False}, "regression_budget_exceeded"),
    # Region still holds broken triangles and barely moved (2.5% < 15%).
    ({"after": _measurement(bad_triangles=2, near_collapse=1, bad_regions=1,
                            bad_area=0.01, max_aniso=19.5),
      "region_after": {"score": 19.5, "bad_triangle_count": 2}},
     "insufficient_region_improvement"),
])
def test_each_rejection_rule_names_itself(overrides, expected):
    verdict = _accept(**overrides)
    assert verdict["accepted"] is False
    assert verdict["reason"] == expected
    assert set(verdict) == {"accepted", "reason", "improvement_ratio", "checks"}


def test_region_that_only_improves_enough_is_accepted():
    verdict = _accept(after=_measurement(bad_triangles=1, bad_area=0.001, max_aniso=8.0),
                      region_after={"score": 8.0, "bad_triangle_count": 1})
    assert verdict["accepted"] is True
    assert verdict["reason"] == "region_improved"


# ------------------------------------------ (d) relief candidates: shape + determinism


def _sphere_region(monkeypatch):
    mesh = build_displaced_sphere(segments=12, rings=8)
    backend = FakeUnwrapBackend(mesh)
    backend.install(monkeypatch)
    seams = set(segmentation.segment(mesh).seams)
    charts = segmentation.flood_charts(mesh, seams)
    island = max(range(len(charts)), key=lambda i: (len(charts[i]), -i))
    faces = sorted(charts[island])
    middle = faces[len(faces) // 2: len(faces) // 2 + 3]
    return mesh, seams, charts, island, middle, SeamConstraints.build(mesh)


def test_relief_candidates_cut_from_the_region_and_rank_deterministically(monkeypatch):
    mesh, seams, charts, island, region, constraints = _sphere_region(monkeypatch)

    first = CR.relief_seam_candidates(mesh, charts, island, seams, constraints, region,
                                      max_candidates=4)
    second = CR.relief_seam_candidates(mesh, charts, island, seams, constraints, region,
                                       max_candidates=4)

    assert len(first) >= 2, [c.reason for c in first]
    for cand in first:
        assert cand.cut_reason == "catastrophic_repair"
        assert cand.reason in CR.RELIEF_REASONS
        assert cand.added_edges, "a relief candidate must actually cut something"
        assert "total" in cand.cost

    # Deterministic across two calls: same order, same edges, same ranking keys.
    assert [c.reason for c in first] == [c.reason for c in second]
    assert [sorted(c.added_edges) for c in first] == [sorted(c.added_edges) for c in second]
    assert [CR.relief_rank_key(c) for c in first] == [CR.relief_rank_key(c) for c in second]

    # The crease-preferring path either produces its own cut or dedupes onto an earlier
    # one — both are legal, but it may never appear twice with the same edge set.
    edge_sets = [frozenset(c.added_edges) for c in first]
    assert len(edge_sets) == len(set(edge_sets))

    # The ranking is sorted by its own key, so "first" really is the best candidate.
    keys = [CR.relief_rank_key(c) for c in first if not c.rejected]
    assert keys == sorted(keys)


def test_reunwrap_candidate_specs_follow_the_profile_budget():
    target = {"island_id": 0, "region_id": 3, "faces": frozenset({1})}
    specs = CR.reunwrap_candidates(target, PROFILE)

    assert len(specs) == PROFILE.catastrophic_reunwrap_variants
    assert [s["variant_id"] for s in specs] == [
        v["id"] for v in CR.R1_VARIANTS[:PROFILE.catastrophic_reunwrap_variants]]
    for spec in specs:
        assert spec["kind"] == "reunwrap"
        assert spec["added_edges"] == frozenset()
        assert spec["region_id"] == 3
        if spec["method"] == "MINIMUM_STRETCH":
            # minimize_stretch is not locally injective; it must never run for SLIM.
            assert spec["minimize_iters"] == 0


# ------------------------------------------- (e) the island cap blocks R2, never R1


def test_island_cap_blocks_the_relief_cut_but_not_the_reunwrap(monkeypatch):
    mesh, backend, obj, seams, constraints, _state = _planes(monkeypatch,
                                                             fix_on_reunwrap=False)
    before = R.unwrap_and_measure(obj, mesh, seams, profile=PROFILE, margin=0.005,
                                  stage="refinement")
    cap = int(before["island_count"])

    reunwraps_before = sum(1 for c in backend.calls if c[0] == "reunwrap_faces")
    result = R.run_refinement(obj, mesh, seams, constraints=constraints, profile=PROFILE,
                              budget={"max_iterations": 2, "island_cap": cap},
                              margin=0.005, initial_measurement=before)

    records = [c for c in result["candidate_history"]
               if c.get("cut_reason") == "catastrophic_repair"]
    r1 = [c for c in records if c["kind"] == "reunwrap"]
    blocked = [c for c in records if c.get("reason") == "island_cap_reached"]

    # R1 adds no seam, so the cap may not stop it.
    assert r1, "the cap must not block the same-seam re-unwrap"
    assert sum(1 for c in backend.calls if c[0] == "reunwrap_faces") > reunwraps_before
    # R2 would cut, so it is recorded as blocked WITHOUT ever being unwrapped.
    assert blocked, records
    for record in blocked:
        assert record["kind"] != "reunwrap"
        assert record["after_measurement"] is None
    assert result["termination"]["reason"] == "island_cap"
    assert result["seams"] == set(seams)
