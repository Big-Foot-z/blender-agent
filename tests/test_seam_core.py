"""Seam Decision Core — pure-helper tests (RULE_BASED_UV_SEAM_CORE_PLAN §5, §12.3).

Blender-free. Covers the three new layers the plan asks for —
``artist_uv_agent.seam_policy`` (edge decisions), ``artist_uv_agent.seam_refinement``
(distortion-driven accept/stop rules) and ``artist_uv_agent.seam_report`` (the §5.3 JSON
schema) — plus the ``island_distortion_summary`` helper added to evaluation. The real
Blender unwrap loop is covered by the worker regression run.
"""

import numpy as np

from artist_uv_agent.seam_policy import (
    EdgeSeamDecision, SeamPolicyConfig, decide_edge, decide_edge_seams,
    material_boundary_edges, policy_summary,
)
from artist_uv_agent.seam_refinement import (
    accept_split, evaluate_distortion, improvement_ratio, pick_worst_island, refinement_plan,
)
from artist_uv_agent.seam_report import build_seam_report
from chart_uv_agent.fixtures import build_folded_planes
from uv_agent.geometry.evaluation import island_distortion_summary
from uv_agent.geometry.packing import pack_islands
from uv_agent.geometry.projection import project_island
from uv_agent.geometry.solution import UVMap
from uv_agent.io import fixtures
from uv_agent.planner.island_planner import plan_islands


def _flat_uvmap(mesh):
    """Exact XY planar UV for a flat grid (zero distortion)."""
    uvm = UVMap.for_mesh(mesh)
    for loop in mesh.loops:
        x, y, _ = mesh.vertex_co(loop.vertex_id)
        uvm.set(loop.index, x + 0.5, y + 0.5)
    return uvm


def _projected(mesh, projection, **plan_kw):
    plan = plan_islands(mesh, **plan_kw)
    for isl in plan.islands:
        isl.projection = projection
    uvm = UVMap.for_mesh(mesh)
    for isl in plan.islands:
        project_island(mesh, isl.face_ids, uvm, isl.projection)
    pack_islands(mesh, plan, uvm)
    return plan, uvm


# -- seam_policy (§5.1) ------------------------------------------------------

def test_fold_edge_is_mandatory():
    mesh = build_folded_planes(n=6)
    fold = next(e.id for e in mesh.edges
                if len(e.face_ids) == 2 and e.dihedral_angle >= 90.0)
    d = decide_edge(mesh, fold)
    assert d.decision == "mandatory"
    assert any("mandatory_fold" in r for r in d.reasons)


def test_mandatory_audit_splits_fold_boundary_and_non_manifold():
    """G7: ``mandatory_90_*`` is a UNION; the per-kind counts say what it is made of."""
    from chart_uv_agent.segmentation import (
        mandatory_edges_by_kind, mandatory_seam_audit, mandatory_seam_edges,
    )

    mesh = build_folded_planes(n=6)
    kinds = mandatory_edges_by_kind(mesh, fold_angle=90.0)
    union = kinds["fold"] | kinds["boundary"] | kinds["non_manifold"]
    assert union == mandatory_seam_edges(mesh, fold_angle=90.0)
    # the fixture is two open grids joined along one 90-degree fold row
    assert kinds["fold"] == {e.id for e in mesh.edges
                             if len(e.face_ids) == 2 and e.dihedral_angle >= 90.0}
    assert kinds["boundary"] == {e.id for e in mesh.edges if e.is_boundary}
    assert kinds["fold"] and kinds["boundary"]
    assert kinds["non_manifold"] == set()

    audit = mandatory_seam_audit(mesh, set(kinds["boundary"]), fold_angle=90.0)
    assert audit["mandatory_90_edges"] == len(union)
    assert audit["mandatory_fold_edges"] == len(kinds["fold"])
    assert audit["mandatory_boundary_edges"] == len(kinds["boundary"])
    assert audit["mandatory_non_manifold_edges"] == 0
    # only the boundary edges were supplied as seams
    assert audit["mandatory_fold_missing"] == len(kinds["fold"])
    assert audit["mandatory_boundary_missing"] == 0
    assert audit["mandatory_non_manifold_missing"] == 0
    assert audit["mandatory_90_missing"] == len(union - kinds["boundary"])


def test_boundary_edge_is_mandatory():
    mesh = fixtures.build_grid_plane(4, 4)
    b = next(e.id for e in mesh.edges if e.is_boundary)
    assert decide_edge(mesh, b).decision == "mandatory"


def test_forbidden_low_angle_edge_is_forbidden_but_mandatory_wins():
    mesh = build_folded_planes(n=6)
    flat = next(e.id for e in mesh.edges
                if len(e.face_ids) == 2 and e.dihedral_angle < 10.0)
    fold = next(e.id for e in mesh.edges
                if len(e.face_ids) == 2 and e.dihedral_angle >= 90.0)
    # A low-angle forbidden edge is genuinely preserved.
    assert decide_edge(mesh, flat, forbidden_edges={flat}).decision == "forbidden"
    # A ≥90° fold the user forbade still ships as mandatory, and the conflict is recorded.
    d = decide_edge(mesh, fold, forbidden_edges={fold})
    assert d.decision == "mandatory"
    assert any(r.startswith("conflict:") for r in d.reasons)


def test_preferred_edge_boosts_candidate_score():
    mesh = build_folded_planes(n=6)
    flat = next(e.id for e in mesh.edges
                if len(e.face_ids) == 2 and e.dihedral_angle < 45.0)
    base = decide_edge(mesh, flat).score
    pref = decide_edge(mesh, flat, preferred_edges={flat})
    assert pref.decision == "candidate"
    assert pref.score > base


def test_decide_edge_seams_covers_every_edge_and_summary():
    mesh = build_folded_planes(n=6)
    decisions = decide_edge_seams(mesh)
    assert len(decisions) == len(mesh.edges)
    assert all(isinstance(d, EdgeSeamDecision) for d in decisions)
    summ = policy_summary(decisions)
    # every ≥90° fold is mandatory.
    folds = {e.id for e in mesh.edges
             if len(e.face_ids) == 2 and e.dihedral_angle >= 90.0}
    assert folds.issubset(set(summ["mandatory_edges"]))


def test_visibility_bias_neutral_without_axis():
    mesh = build_folded_planes(n=6)
    flat = next(e.id for e in mesh.edges
                if len(e.face_ids) == 2 and e.dihedral_angle < 45.0)
    no_axis = decide_edge(mesh, flat).reasons
    assert "front_facing_visible" not in no_axis and "hidden_side" not in no_axis


def test_material_boundary_empty_on_single_material():
    mesh = fixtures.build_grid_plane(4, 4)
    assert material_boundary_edges(mesh) == set()


# -- seam_refinement (§5.2) --------------------------------------------------

def test_flat_plane_passes_distortion():
    mesh = fixtures.build_grid_plane(4, 4)
    uvm = _flat_uvmap(mesh)
    islands = [[f.id for f in mesh.faces]]
    v = evaluate_distortion(mesh, uvm, islands, global_threshold=0.1, island_threshold=0.1)
    assert v.passed
    assert v.worst_island_distortion < 1e-6


def test_planar_projected_tube_fails_and_picks_worst():
    mesh = fixtures.build_cylinder(16, 4)
    _, uvm = _projected(mesh, "planar", angle_threshold=45)
    islands = [[f.id for f in mesh.faces]]
    v = evaluate_distortion(mesh, uvm, islands, global_threshold=0.25, island_threshold=0.35)
    assert not v.passed
    idx, dist = pick_worst_island(mesh, uvm, islands)
    assert idx == 0 and dist > 0.35


def test_accept_split_rule():
    assert improvement_ratio(0.5, 0.25) == 0.5
    assert accept_split(0.5, 0.40, min_improvement_ratio=0.15)       # 20% > 15%
    assert not accept_split(0.5, 0.46, min_improvement_ratio=0.15)   # 8% < 15%
    # a split that makes it worse is never accepted.
    assert not accept_split(0.5, 0.6, min_improvement_ratio=0.15)


def test_refinement_plan_reports_target():
    mesh = fixtures.build_cylinder(16, 4)
    _, uvm = _projected(mesh, "planar", angle_threshold=45)
    islands = [[f.id for f in mesh.faces]]
    plan = refinement_plan(mesh, uvm, islands,
                           config=SeamPolicyConfig(distortion_threshold=0.35))
    assert plan["refine_target"] == 0
    assert plan["islands"][0]["distortion"] > 0.35
    assert plan["at_island_cap"] is False


# -- island_distortion_summary (§6.3) ----------------------------------------

def test_island_distortion_summary_shape_and_rank():
    mesh = fixtures.build_cylinder(16, 4)
    _, uvm = _projected(mesh, "planar", angle_threshold=45)
    # Two arbitrary islands so we exercise ranking.
    faces = [f.id for f in mesh.faces]
    half = len(faces) // 2
    rows = island_distortion_summary(mesh, uvm, [faces[:half], faces[half:]])
    assert len(rows) == 2
    for r in rows:
        assert {"island_id", "face_count", "area_3d", "area_uv",
                "distortion", "overlap_ratio", "rank"} <= set(r)
    # ranks are a 0..n-1 permutation, worst = rank 0.
    assert sorted(r["rank"] for r in rows) == [0, 1]
    worst = min(rows, key=lambda r: r["rank"])
    assert worst["distortion"] == max(r["distortion"] for r in rows)


# -- seam_report (§5.3) ------------------------------------------------------

def _fake_result():
    """A minimal run_chart_uv-shaped dict (no Blender)."""
    class _Gate:
        verdict = "accepted"
    return {
        # final shipped seam set: the two accepted splits (501/502, 777) + the forbidden fold
        # (3054). Edge 999 was "split" in history but LOST in the best pick → not shipped.
        "engine": "chart", "seams": [11, 501, 502, 777, 3054],
        "mandatory_90_edges": 123, "mandatory_90_missing": 0,
        "mandatory_90_fold_edges": 120, "mandatory_90_uv_unsplit": 0,
        "initial_island_count": 34, "final_island_count": 42,
        "chart_count": 42, "seam_type_counts": {"mandatory_90": 120, "distortion_split": 3},
        "forbidden_edges": [3054], "forbidden_stripped": [],
        "pruned_auxiliary": 2, "conclusion": "islands increased 34 -> 42",
        "gate": _Gate(),
        "metrics": {"stretch_score": 0.31, "checker_distortion_score": 0.31,
                    "worst_island_distortion": 0.48, "worst_island_id": 12,
                    "mandatory_90_edges": 123, "mandatory_90_missing": 0},
        "history": [
            {"round": 0, "stretch": 0.52, "action": "stop"},
            {"round": 1, "stretch": 0.41, "action": "split", "reason": "worst_island_distortion",
             "split_island": 12, "before_distortion": 0.61, "added_edges": [501, 502],
             "distortion_after": 0.40, "improvement_ratio": 0.34, "accepted": True},
            # a reverted split must NOT appear in added_seams.
            {"round": 2, "stretch": 0.40, "action": "revert", "reason": "distortion_split_reverted"},
            {"round": 3, "stretch": 0.39, "action": "split", "reason": "checker_distortion",
             "split_island": 7, "before_distortion": 0.45, "added_edges": [777],
             "distortion_after": 0.30, "improvement_ratio": 0.33, "accepted": True,
             "reverted": False},
            # accepted in history but the edge did NOT survive into the shipped seam set.
            {"round": 4, "stretch": 0.35, "action": "split", "reason": "checker_distortion",
             "split_island": 9, "before_distortion": 0.42, "added_edges": [999],
             "distortion_after": 0.31, "improvement_ratio": 0.26, "accepted": True},
            # unresolved provisional split at the cap → excluded.
            {"round": 5, "stretch": 0.33, "action": "split", "reason": "worst_island_distortion",
             "split_island": 3, "before_distortion": 0.50, "added_edges": [888],
             "accepted": False, "unresolved": True},
            {"round": 6, "stretch": 0.31, "action": "stop"},
        ],
    }


def test_seam_report_schema():
    rep = build_seam_report(_fake_result(), source_mesh="humanstatue_low")
    assert rep["mandatory_90_edges"] == 123
    assert rep["mandatory_90_missing"] == 0
    assert rep["mandatory_90_uv_unsplit"] == 0          # the §10 exported-UV audit
    assert rep["initial_island_count"] == 34
    assert rep["final_island_count"] == 42
    assert rep["stretch_before"] == 0.52
    assert rep["stretch_after"] == 0.31
    assert rep["source_mesh"] == "humanstatue_low"
    # added_seams is edge-level AND only reports edges in the FINAL shipped seam set:
    #   501/502/777 shipped → reported; 999 lost in best-pick → excluded;
    #   888 unresolved at cap → excluded; the round-2 revert contributes nothing.
    added = rep["added_seams"]
    edge_ids = {a.get("edge_id") for a in added}
    assert edge_ids == {501, 502, 777}
    e501 = next(a for a in added if a["edge_id"] == 501)
    assert e501["reason"] == ["distortion_repair"]
    assert e501["distortion_before"] == 0.61
    assert e501["distortion_after"] == 0.40
    assert e501["improvement_ratio"] == 0.34
    assert all(a.get("round") not in (2, 4, 5) for a in added)


def test_seam_report_conflict_forbidden_fold_kept():
    # 3054 was forbidden by the user but still shipped as a seam (a ≥90° fold) → conflict.
    rep = build_seam_report(_fake_result())
    assert any(c["edge_id"] == 3054 and c["resolution"] == "mandatory_wins"
               for c in rep["conflicts"])


def test_seam_report_with_policy_decisions():
    mesh = build_folded_planes(n=6)
    decisions = decide_edge_seams(mesh)
    rep = build_seam_report(_fake_result(), decisions=decisions)
    assert "policy_decision_counts" in rep
    assert rep["policy_decision_counts"].get("mandatory", 0) > 0
