"""Gates G1/G2/G4/G5 — the automatic pipeline end to end, with no Blender at all.

Every run goes through :class:`tests.helpers.fake_blender_uv.FakeUnwrapBackend`, so the
no-spec automatic path, the locked/protected constraint plumbing, the user-assisted
automatic path, the legacy regression path and run-to-run determinism are all testable
off-Blender (``bpy`` is never imported).
"""

from __future__ import annotations

import json

from artist_uv_agent.user_seams import UserSeamSpec
from chart_uv_agent.fixtures import build_displaced_sphere, build_folded_planes
from chart_uv_agent.gate import ChartGateConfig
from chart_uv_agent.pipeline import run_chart_uv
from chart_uv_agent.segmentation import mandatory_seam_edges
from tests.helpers.fake_blender_uv import FakeUnwrapBackend
from uv_agent.geometry.mesh_graph import MeshGraph

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


# ------------------------------------------------- 1b. catastrophic result block (CG2/CG3)


def test_no_spec_auto_run_carries_the_catastrophic_block(monkeypatch):
    mesh, _backend, obj = _sphere(monkeypatch)
    result = run_chart_uv(obj, mesh, max_rounds=4,
                          budget={"max_candidates_per_round": 2})

    catastrophic = result["catastrophic"]
    assert isinstance(catastrophic, dict)
    assert isinstance(catastrophic["passed"], bool)

    digest = result["uv_hash"]
    assert isinstance(digest, str) and len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)

    assert len(result["face_score_raw"]) == len(mesh.faces)

    repair = result["repair"]
    assert isinstance(repair, dict)
    assert isinstance(repair["rounds"], int)

    report = result["quality_report"]
    assert isinstance(report["sections"]["catastrophic"], dict)
    assert [layer["name"] for layer in report["layers"]] == [
        "A_correctness", "B_catastrophic", "C_island_quality", "D_seam_economy",
        "E_game_production"]
    json.dumps(report)
    json.dumps(result["face_score_raw"])
    json.dumps(repair)

    for key in ("catastrophic_bad_triangles", "catastrophic_max_anisotropy",
                "catastrophic_bad_area_fraction"):
        assert key in result["metrics"], key


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
        if rec.get("stage") == "merge_back":
            # G7 runs on the legacy path too; its verdicts are its own (merge/reject/skip).
            assert action in {"merge", "reject", "skip"}, action
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

    # ``merge_back=False``: the G7 stage takes its own island-gap measurement, which would
    # consume the scripted failures below. This case is about the gap repack, nothing else.
    mesh, _backend, obj = _sphere(monkeypatch)
    baseline = run_chart_uv(obj, mesh, max_rounds=3, use_refinement_loop=False,
                            merge_back=False)

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
    result = run_chart_uv(obj2, mesh2, max_rounds=3, use_refinement_loop=False,
                          merge_back=False)

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


def test_island_cap_termination_reason(monkeypatch):
    """G5: a run stopped by the island cap must be reported as ``island_cap``, never as
    ``max_rounds`` / ``no_improving_candidate``.

    CG5: this fixture is ALSO catastrophic at the cap, so the seam-free R1 re-unwrap is
    now attempted (candidates are evaluated); only the seam-adding candidates are blocked,
    and the termination reason is unchanged."""
    mesh, _backend, obj = _sphere(monkeypatch)
    result = run_chart_uv(obj, mesh, max_rounds=3,
                          budget={"island_cap": 1, "max_candidates_per_round": 2})

    termination = result["termination"]
    assert termination["reason"] == "island_cap", termination


# --------------------------- 11. the new G7/G8/G9/G11/G15 result blocks ship (G15)


def test_no_spec_result_carries_fragmentation_texel_packing_and_quality_report(monkeypatch):
    """G7/G8/G9/G11/G15: the automatic result carries every new gate block plus the one
    self-contained ``quality_report`` document, and that document survives json.dumps."""
    mesh, _backend, obj = _sphere(monkeypatch)
    result = run_chart_uv(obj, mesh, max_rounds=3,
                          budget={"max_candidates_per_round": 2})

    for key in ("fragmentation", "texel_density", "packing", "border_inset",
                "hard_failures", "quality_failures", "quality_report"):
        assert key in result, key

    json.dumps(result["quality_report"])
    report = result["quality_report"]
    assert report["schema_version"] == 1
    assert report["hard_failures"] == result["hard_failures"]

    # ``auto_passed`` is the report's verdict AND the v1 mandatory hard gate; the report
    # never claims a pass the run did not have.
    assert result["auto_passed"] is (report["passed"] and result["auto_passed"])
    if not report["passed"]:
        assert result["auto_passed"] is False

    # The v2 metric summary carries the new numbers.
    for key in ("tiny_island_count", "sliver_island_count", "normalized_seam_length",
                "texel_density_cv", "packing_efficiency_v2", "min_border_gap_px"):
        assert key in result["metrics"], key

    # The shipped layout respects the tile padding (G9).
    border = [c for c in result["correctness"]["checks"] if c["name"] == "border_gap"]
    assert len(border) == 1 and border[0]["passed"] is True


# --------------------------- 12. merge-back (G7) + shading policy (G10) + cost (G2)


def _folded(monkeypatch, n: int = 6):
    mesh = build_folded_planes(n=n)
    backend = FakeUnwrapBackend(mesh)
    obj = backend.install(monkeypatch)
    return mesh, backend, obj


def _row_seam_chain(mesh, n: int, row: int) -> set[int]:
    """The full-width edge chain at ``y = row/n`` of grid A — a flat, non-mandatory cut
    that splits that plane in two, i.e. exactly one seam that is NOT paying for itself."""
    by_key = {tuple(sorted(e.vertex_ids)): e.id for e in mesh.edges}

    def vA(i, j):
        return i * (n + 1) + j

    chain = {by_key[tuple(sorted((vA(i, row), vA(i + 1, row))))] for i in range(n)}
    assert not (chain & mandatory_seam_edges(mesh, fold_angle=90.0))
    return chain


def _over_segment(monkeypatch, n: int = 6, row: int = 3):
    """Install a fake object whose INITIAL segmentation carries one extra flat seam."""
    import chart_uv_agent.pipeline as pipeline
    from chart_uv_agent import segmentation

    mesh, backend, obj = _folded(monkeypatch, n=n)
    extra = _row_seam_chain(mesh, n, row)
    real_segment = segmentation.segment

    def _segment(m, **kwargs):
        seg = real_segment(m, **kwargs)
        seg.seams.update(extra)
        return seg

    monkeypatch.setattr(pipeline, "segment", _segment)
    return mesh, obj, extra


def test_merge_back_dissolves_the_seam_that_is_not_paying_for_itself(monkeypatch):
    """G7: an over-segmented start must come out with the removable seam given back, the
    stage reported complete, and a merge trial in the history."""
    mesh, obj, extra = _over_segment(monkeypatch)
    mandatory = mandatory_seam_edges(mesh, fold_angle=90.0)

    result = run_chart_uv(obj, mesh, max_rounds=4,
                          budget={"max_candidates_per_round": 2})

    mb = result["merge_back"]
    assert mb["enabled"] is True
    assert mb["accepted"] >= 1, mb
    assert mb["complete"] is True
    assert mb["reason"] == "no_removable_seam"
    assert set(mb["removed_edges"]) == extra
    assert mb["island_count_after"] == mb["island_count_before"] - mb["accepted"]

    assert set(result["seams"]) == mandatory
    assert result["final_island_count"] == 2
    rows = [h for h in result["history"] if h.get("stage") == "merge_back"]
    assert rows and any(r["accepted"] for r in rows), rows
    json.dumps(mb)


def test_merge_back_repacks_a_gap_only_failure_instead_of_skipping(monkeypatch):
    """G7 + G9/G11: a merge-back input that fails ONLY the packing gap is re-packed wider,
    so the stage really runs instead of reporting ``skipped_quality_failed``."""
    from chart_uv_agent import refinement_loop
    from uv_agent.geometry import uv_correctness

    mesh, obj, extra = _over_segment(monkeypatch)

    real_audit = uv_correctness.island_gap_audit
    state = {"armed": False, "calls": 0}

    def failing_twice(*args, **kwargs):
        report = dict(real_audit(*args, **kwargs))
        if state["armed"]:
            state["calls"] += 1
            if state["calls"] <= 2:
                report.update({"passed": False, "min_gap_px": 1.0})
        return report

    # The failures are armed exactly when the merge-back stage takes its own measurement,
    # which is the situation the real-Blender runs hit (Blender's packer leaves ~4px gaps).
    real_repack_for_gap = refinement_loop.repack_for_gap

    def arming_repack_for_gap(*args, **kwargs):
        state["armed"] = True
        return real_repack_for_gap(*args, **kwargs)

    monkeypatch.setattr(uv_correctness, "island_gap_audit", failing_twice)
    monkeypatch.setattr(refinement_loop, "repack_for_gap", arming_repack_for_gap)

    result = run_chart_uv(obj, mesh, max_rounds=4,
                          budget={"max_candidates_per_round": 2})

    mb = result["merge_back"]
    assert mb["reason"] != "skipped_quality_failed", mb
    assert mb["reason"] == "no_removable_seam"
    assert mb["accepted"] >= 1
    assert set(mb["removed_edges"]) == extra
    assert extra.isdisjoint(result["seams"])

    repacks = [h for h in result["history"] if h.get("stage") == "gap_repack"]
    assert repacks and repacks[0]["attempts"] == 2 and repacks[0]["passed"] is True
    # The gap was fixed by re-packing, never by a cut.
    assert [h for h in result["history"] if h.get("stage") == "merge_back"]


def test_merge_back_disabled_keeps_the_extra_seam(monkeypatch):
    """The stage is switchable, and switching it off is REPORTED, never silent."""
    mesh, obj, extra = _over_segment(monkeypatch)

    result = run_chart_uv(obj, mesh, max_rounds=4, merge_back=False,
                          budget={"max_candidates_per_round": 2})

    mb = result["merge_back"]
    assert mb["enabled"] is False
    assert mb["reason"] == "disabled"
    assert mb["trials"] == 0 and mb["accepted"] == 0
    assert mb["removed_edges"] == []
    assert extra <= set(result["seams"])
    assert not [h for h in result["history"] if h.get("stage") == "merge_back"]


def test_shading_preserve_policy_passes_and_reaches_the_quality_report(monkeypatch):
    """G10: the default ``preserve`` policy audits the before/after shading snapshots and
    the verdict ships inside the one quality-report document."""
    mesh, _backend, obj = _folded(monkeypatch)
    result = run_chart_uv(obj, mesh, max_rounds=4,
                          budget={"max_candidates_per_round": 2})

    shading = result["shading"]
    assert shading["policy"] == "preserve"
    assert shading["passed"] is True
    assert shading["failures"] == [] and shading["invalid_reasons"] == []
    assert shading["snapshot_diff"]["unchanged"] is True
    assert "shading" in result["quality_report"]["sections"]
    json.dumps(shading)

    # No policy-required seams under ``preserve``.
    assert result["auto_constraints"]["required_seam_edges"] == []
    assert result["auto_constraints"]["required_missing"] == []
    assert result["constraints"]["required_count"] == 0


def _sharp_plane(monkeypatch, n: int = 4, row: int = 2):
    """A FLAT grid whose interior ``y = row/n`` line is authored sharp: nothing about the
    geometry forces a seam there, only the shading policy does."""
    from uv_agent.geometry.mesh_graph import MeshGraph

    coords, idx = [], {}
    for i in range(n + 1):
        for j in range(n + 1):
            idx[(i, j)] = len(coords)
            coords.append((i / n, j / n, 0.0))
    faces = [[idx[(i, j)], idx[(i + 1, j)], idx[(i + 1, j + 1)], idx[(i, j + 1)]]
             for i in range(n) for j in range(n)]
    sharp = [(idx[(i, row)], idx[(i + 1, row)]) for i in range(n)]
    mesh = MeshGraph.from_faces("sharp_plane", coords, faces, sharp_edge_keys=sharp)
    backend = FakeUnwrapBackend(mesh)
    return mesh, backend.install(monkeypatch)


def test_require_uv_seam_on_sharp_edges_forces_those_seams(monkeypatch):
    """G10: under ``require_uv_seam_on_sharp_edges`` every sharp edge is a ``required``
    constraint — it ships as a seam, is listed in the evidence block, and the policy
    passes on the shipped UVs."""
    from chart_uv_agent.quality_profile import ENGINEERING_V0

    mesh, obj = _sharp_plane(monkeypatch)
    sharp = sorted(e.id for e in mesh.edges if e.is_sharp and len(e.face_ids) == 2)
    assert sharp, "fixture must carry interior sharp edges"
    assert all(mesh.edges[e].dihedral_angle < 90.0 for e in sharp), \
        "the sharp edges must be FLAT, so only the policy can require them"

    profile = {**ENGINEERING_V0.to_dict(),
               "shading_uv_policy": "require_uv_seam_on_sharp_edges"}
    result = run_chart_uv(obj, mesh, max_rounds=4, quality_profile=profile,
                          budget={"max_candidates_per_round": 2})

    assert set(sharp) <= set(result["seams"])
    assert result["auto_constraints"]["required_seam_edges"] == sharp
    assert result["auto_constraints"]["required_missing"] == []
    assert result["constraints"]["required_count"] == len(sharp)

    shading = result["shading"]
    assert shading["policy"] == "require_uv_seam_on_sharp_edges"
    assert shading["passed"] is True, shading
    assert shading["sharp_edge_uv_audit"]["sharp_edge_uv_unsplit"] == 0
    # Merge-back must never give a required seam back.
    assert not (set(result["merge_back"]["removed_edges"]) & set(sharp))


def _without_timings(history) -> list:
    return [{k: v for k, v in dict(row).items() if k != "elapsed_s"} for row in history]


def test_two_runs_agree_on_the_seams_and_the_merge_back_history(monkeypatch):
    """G12: merge-back adds no non-determinism — two fresh runs dissolve the same seams
    through the same trials, in the same order (wall-clock timings excluded)."""
    runs = []
    for _ in range(2):
        with monkeypatch.context() as mp:
            mesh, obj, _extra = _over_segment(mp)
            runs.append(run_chart_uv(obj, mesh, max_rounds=4,
                                     budget={"max_candidates_per_round": 2}))

    first, second = runs
    assert second["seams"] == first["seams"]
    assert second["merge_back"]["removed_edges"] == first["merge_back"]["removed_edges"]
    assert _without_timings(second["merge_back"]["history"]) == \
        _without_timings(first["merge_back"]["history"])


# ------------------- 13. CG5: catastrophic repair still runs at the island cap


def _needle_grid(monkeypatch, *, inject_needle: bool, n: int = 4, persist: bool = False):
    """A flat ``n x n`` quad grid (ONE chart, no fold seams) behind the fake backend.

    With ``inject_needle`` the unwrap always collapses one UV corner of a middle face
    (the CG5 pattern from ``tests/test_catastrophic_repair.py``), so every layout the
    pipeline measures fails the hard catastrophic gate; the R1 re-unwrap repairs it.
    With ``persist`` the re-unwrap re-injects the needle too, so the catastrophic failure
    survives the whole round loop (CG6/CG13: the post-prune pass must still run).
    """
    coords = [(i / n, j / n, 0.0) for i in range(n + 1) for j in range(n + 1)]

    def vid(i: int, j: int) -> int:
        return i * (n + 1) + j

    faces = [[vid(i, j), vid(i + 1, j), vid(i + 1, j + 1), vid(i, j + 1)]
             for i in range(n) for j in range(n)]
    mesh = MeshGraph.from_faces("flat_grid", coords, faces)

    backend = FakeUnwrapBackend(mesh)
    obj = backend.install(monkeypatch)
    loops = [int(li) for li in mesh.faces[5].loop_indices]
    state = {"reunwrapped": False}

    def inject(o) -> None:
        o.uv.uv[loops[0]] = o.uv.uv[loops[1]]

    real_unwrap = backend.unwrap_and_pack
    real_reunwrap = backend.reunwrap_faces

    def unwrap_and_pack(o, seams, **kwargs):
        out = real_unwrap(o, seams, **kwargs)
        if inject_needle and (persist or not state["reunwrapped"]):
            inject(o)
        return out

    def reunwrap_faces(o, face_ids, **kwargs):
        out = real_reunwrap(o, face_ids, **kwargs)
        state["reunwrapped"] = True
        if inject_needle and persist:
            inject(o)
        return out

    monkeypatch.setattr("chart_uv_agent.unwrap.unwrap_and_pack", unwrap_and_pack)
    monkeypatch.setattr("chart_uv_agent.unwrap.reunwrap_faces", reunwrap_faces)
    return mesh, backend, obj


def test_catastrophic_repair_runs_even_at_the_island_cap(monkeypatch):
    """CG5: a catastrophic failure at the island cap must still reach the refinement
    loop — the R1 re-unwrap adds no seam, so the cap cannot forbid it."""
    mesh, _backend, obj = _needle_grid(monkeypatch, inject_needle=True)
    result = run_chart_uv(obj, mesh, max_rounds=3,
                          budget={"island_cap": 1, "max_candidates_per_round": 2})

    assert result["uv_island_count"] >= 1
    reunwraps = [c for c in result["candidate_history"] if c.get("kind") == "reunwrap"]
    assert reunwraps, result["candidate_history"]

    termination = result["termination"]
    assert (termination["reason"] != "island_cap"
            or termination["candidates_evaluated"] > 0), termination


def test_clean_run_at_the_island_cap_still_stops_without_candidates(monkeypatch):
    """The control: no catastrophic failure at the cap ⇒ the old skip is unchanged."""
    mesh, _backend, obj = _needle_grid(monkeypatch, inject_needle=False)
    result = run_chart_uv(obj, mesh, max_rounds=3,
                          budget={"island_cap": 1, "max_candidates_per_round": 2})

    termination = result["termination"]
    assert termination["candidates_evaluated"] == 0, termination
    assert termination["reason"] in {"island_cap", "quality_passed"}, termination
    assert not [c for c in result["candidate_history"] if c.get("kind") == "reunwrap"]


# ------- 14. CG5/CG6/CG13: an accepted R1 keeps the round loop going; post-prune pass


def _round_rows(result) -> list:
    """The per-round records of the pipeline loop (they all carry a gate ``verdict``)."""
    return [h for h in result["history"] if "verdict" in h]


def test_accepted_reunwrap_without_a_seam_change_continues_the_round_loop(monkeypatch):
    """CG5: an accepted R1 repair changes the LAYOUT, not the seam set — the round must
    still count as changed, or the loop stops one repair too early."""
    mesh, _backend, obj = _needle_grid(monkeypatch, inject_needle=True)

    result = run_chart_uv(obj, mesh, max_rounds=4,
                          budget={"max_candidates_per_round": 2})

    rounds = _round_rows(result)
    reunwrap_rounds = [h for h in rounds if h.get("action") == "reunwrap"]
    assert reunwrap_rounds, rounds
    row = reunwrap_rounds[0]
    assert row["reason"] == "catastrophic_repair"
    assert "refinement_added_edges" not in row, "an R1 repair adds no seam"
    # The loop did NOT stop on that round: a later round exists, and it is not the
    # "nothing left to try" break.
    assert row["round"] < rounds[-1]["round"], rounds
    assert row is not rounds[-1]


def test_post_prune_catastrophic_pass_re_enters_the_repair_loop(monkeypatch):
    """CG6/CG13: when the loop stops with the catastrophic gate still red, the settled
    (pruned) seam set is re-measured and the repair loop is entered once more."""
    mesh, _backend, obj = _needle_grid(monkeypatch, inject_needle=True, persist=True)

    result = run_chart_uv(obj, mesh, max_rounds=2,
                          budget={"max_candidates_per_round": 2})

    rows = [h for h in result["history"] if h.get("stage") == "post_prune_catastrophic"]
    assert rows, [h.get("stage") for h in result["history"]]
    row = rows[0]
    assert isinstance(row["reason"], str) and row["reason"]
    assert int(row["rounds"]) >= 0
    assert int(row["bad_triangles_before"]) >= 1
    assert int(row["bad_triangles_after"]) >= 0
    # The pass only makes sense below the island cap — pruning is what buys that headroom.
    assert result["final_island_count"] < int(result["termination"]["budget"]["island_cap"])
    json.dumps(row)


def test_clean_run_has_no_post_prune_catastrophic_row(monkeypatch):
    """The control: a layout whose catastrophic gate is green never re-enters the loop."""
    mesh, _backend, obj = _needle_grid(monkeypatch, inject_needle=False)

    result = run_chart_uv(obj, mesh, max_rounds=3,
                          budget={"max_candidates_per_round": 2})

    assert not [h for h in result["history"] if h.get("stage") == "post_prune_catastrophic"]
