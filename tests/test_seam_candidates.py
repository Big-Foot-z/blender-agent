"""Gate G4 — cut paths and constraints (``chart_uv_agent.constraints`` / ``.candidates``).

Blender-free: every fixture is a pure :class:`MeshGraph`. The cases mirror the G4
evidence list — mandatory/locked/protected precedence, a protected-band detour, a fully
enclosed protected region (no path), the preferred-route choice, and the deterministic
candidate ordering/budget.
"""

from __future__ import annotations

import json
import math

import pytest

from chart_uv_agent import candidates as C
from chart_uv_agent.constraints import SeamConstraints
from chart_uv_agent.fixtures import build_folded_planes
from chart_uv_agent.segmentation import flood_charts, mandatory_seam_edges, split_chart
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.io.fixtures import build_cylinder, build_grid_plane

GRID_N = 6


def _grid():
    """A flat ``GRID_N × GRID_N`` plane = one chart whose only seams are its rim."""
    mesh = build_grid_plane(GRID_N, GRID_N)

    def vid(i: int, j: int) -> int:
        return j * (GRID_N + 1) + i

    seams = mandatory_seam_edges(mesh)
    charts = flood_charts(mesh, seams)
    assert len(charts) == 1
    return mesh, vid, seams, charts


def _stretch(mesh, hot_face: int) -> dict[int, float]:
    """Artificial per-face distortion: one hot face, everything else clean."""
    fs = {f.id: 0.0 for f in mesh.faces}
    fs[hot_face] = 10.0
    return fs


# --------------------------------------------------------------- constraints


def test_constraints_precedence_on_folded_planes():
    mesh = build_folded_planes(6)
    mandatory = mandatory_seam_edges(mesh)
    fold = sorted(e.id for e in mesh.edges
                  if len(e.face_ids) == 2 and e.dihedral_angle >= 90.0)
    flat = sorted(e.id for e in mesh.edges
                  if len(e.face_ids) == 2 and e.dihedral_angle < 1.0)
    assert fold and len(flat) > 2

    protected_fold, locked_flat, plain_flat = fold[0], flat[0], flat[1]
    con = SeamConstraints.build(mesh,
                                protected={protected_fold, locked_flat, plain_flat},
                                locked={locked_flat})

    # mandatory wins over protected, and the clash is recorded (never silently honoured).
    assert protected_fold in con.mandatory
    assert protected_fold not in con.forbidden
    # a user-locked seam also beats protected.
    assert locked_flat not in con.forbidden
    # a plain protected edge is the only one that actually becomes forbidden.
    assert con.forbidden == frozenset({plain_flat})

    by_edge = {c["edge_id"]: c for c in con.conflicts}
    assert by_edge[protected_fold] == {"edge_id": protected_fold, "user_rule": "protected",
                                       "engine_rule": "mandatory_90",
                                       "resolution": "mandatory_wins"}
    assert by_edge[locked_flat] == {"edge_id": locked_flat, "user_rule": "protected",
                                    "engine_rule": "locked_seam",
                                    "resolution": "locked_wins"}

    report = con.to_report()
    assert report["mandatory_count"] == len(mandatory)
    assert report["forbidden_count"] == 1
    assert report["conflict_count"] == 2
    assert report["front_axis"] == ""
    assert report["invalid_edges"] == []


def test_constraints_invalid_edges_are_dropped_and_recorded():
    mesh = build_folded_planes(4)
    bad = mesh.edge_count + 5
    con = SeamConstraints.build(mesh, protected={0, bad}, locked={-1}, preferred={bad})
    assert con.invalid_edges == (-1, bad)
    assert bad not in con.protected and bad not in con.preferred
    assert -1 not in con.locked


def test_constraints_check_and_filter_never_silently_drop():
    mesh = build_folded_planes(4)
    flat = sorted(e.id for e in mesh.edges
                  if len(e.face_ids) == 2 and e.dihedral_angle < 1.0)
    fold = sorted(e.id for e in mesh.edges
                  if len(e.face_ids) == 2 and e.dihedral_angle >= 90.0)
    prot, free = flat[0], flat[1]
    con = SeamConstraints.build(mesh, protected={prot}, locked={flat[2]})

    added = con.check_added({prot, free})
    assert added == {"ok": False, "protected_cut": [prot], "reason": "protected_edge_cut"}
    assert con.check_added({free})["ok"] is True

    removed = con.check_removed({fold[0]})
    assert removed["ok"] is False
    assert removed["mandatory_removed"] == [fold[0]]
    assert removed["reason"] == "mandatory_seam_removed"
    removed2 = con.check_removed({flat[2]})
    assert removed2["locked_removed"] == [flat[2]]
    assert removed2["reason"] == "locked_seam_removed"
    assert con.check_removed({free})["ok"] is True

    allowed, rejected = con.filter_added({prot, free})
    assert allowed == {free} and rejected == {prot}
    assert C.edges_cut_protected(mesh, {prot, free}, con) == [prot]


def test_edge_cost_preferred_discount_locked_free_and_forbidden_infinite():
    mesh, vid, seams, _ = _grid()
    a = mesh.edge_key(vid(1, 1), vid(2, 1))
    b = mesh.edge_key(vid(2, 1), vid(3, 1))
    c = mesh.edge_key(vid(3, 1), vid(4, 1))
    base = SeamConstraints.build(mesh)
    tuned = SeamConstraints.build(mesh, preferred={a}, locked={b}, protected={c})
    assert tuned.edge_cost(mesh, a) == pytest.approx(base.edge_cost(mesh, a) * 0.25)
    assert tuned.edge_cost(mesh, b) == 0.0
    assert tuned.edge_cost(mesh, c) == float("inf")

    # a mandatory fold is never discounted by a preference.
    folded = build_folded_planes(4)
    fold = sorted(e.id for e in folded.edges
                  if len(e.face_ids) == 2 and e.dihedral_angle >= 90.0)[0]
    fcon = SeamConstraints.build(folded, preferred={fold})
    assert fcon.edge_cost(folded, fold) == SeamConstraints.build(folded).edge_cost(folded, fold)


def test_exposure_is_neutral_without_front_axis():
    mesh, vid, _, _ = _grid()
    edges = {mesh.edge_key(vid(1, 1), vid(2, 1)), mesh.edge_key(vid(2, 1), vid(3, 1))}
    neutral = SeamConstraints.build(mesh)
    assert neutral.front_axis == ""
    assert neutral.exposure_cost(mesh, edges) == 0.0

    # The grid's faces all point +Z, so a +z front sees every edge at full strength.
    facing = SeamConstraints.build(mesh, front_axis="+z")
    assert facing.exposure_cost(mesh, edges) == pytest.approx(C.seam_length(mesh, edges))
    assert SeamConstraints.build(mesh, front_axis="-z").exposure_cost(mesh, edges) == 0.0


def test_seam_length_and_bbox_diagonal():
    mesh, vid, _, _ = _grid()
    e = mesh.edge_key(vid(1, 1), vid(2, 1))
    assert C.seam_length(mesh, [e]) == pytest.approx(1.0 / GRID_N)
    assert C.seam_length(mesh, []) == 0.0
    assert C.bbox_diagonal(mesh) == pytest.approx(math.sqrt(2.0))


# ---------------------------------------------------------------- short cut


def test_short_cut_splits_the_chart_in_two_and_avoids_forbidden():
    mesh, vid, seams, charts = _grid()
    hot = 1 * GRID_N + 1                      # face (i=1, j=1)
    con = SeamConstraints.build(mesh)
    cand = C.short_cut_candidate(mesh, charts[0], seams, con,
                                 _stretch(mesh, hot), 0, top_fraction=0.001)
    assert cand is not None
    assert cand.kind == "short_cut" and cand.reason == "short_cut"
    assert cand.added_edges
    assert not cand.rejected
    assert not (set(cand.added_edges) & con.forbidden)
    assert cand.notes["region_faces"] == 1
    assert cand.notes["start_vertex"] in mesh.faces[hot].vertex_ids
    assert cand.notes["path_cost"] > 0.0
    assert cand.seam_length == pytest.approx(C.seam_length(mesh, cand.added_edges))

    after = flood_charts(mesh, set(seams) | set(cand.added_edges))
    assert len(after) == 2
    assert cand.to_dict()["added_edges"] == sorted(cand.added_edges)


def test_short_cut_detours_around_a_protected_band():
    mesh, vid, seams, charts = _grid()
    hot = 1 * GRID_N + 1
    stretch = _stretch(mesh, hot)

    baseline = C.short_cut_candidate(mesh, charts[0], seams, SeamConstraints.build(mesh),
                                     stretch, 0, top_fraction=0.001)
    assert baseline is not None

    # A protected band walling the hot region off from the two nearest boundary sides.
    band = {mesh.edge_key(vid(i, 0), vid(i, 1)) for i in range(GRID_N + 1)}
    band |= {mesh.edge_key(vid(0, j), vid(1, j)) for j in range(GRID_N + 1)}
    band -= set(seams)
    assert set(baseline.added_edges) & band      # the free route really did cross the band
    con = SeamConstraints.build(mesh, protected=band)
    assert con.forbidden == frozenset(band)

    cand = C.short_cut_candidate(mesh, charts[0], seams, con, stretch, 0, top_fraction=0.001)
    assert cand is not None                       # a detour exists and was found
    assert not (set(cand.added_edges) & con.forbidden)   # zero protected edges cut
    assert C.edges_cut_protected(mesh, cand.added_edges, con) == []
    assert set(cand.added_edges) != set(baseline.added_edges)
    assert cand.seam_length > baseline.seam_length      # the detour is the longer route
    assert len(flood_charts(mesh, set(seams) | set(cand.added_edges))) == 2


def test_fully_enclosed_protected_region_yields_no_short_cut_but_a_reported_normal_split():
    mesh = build_cylinder(segments=12, rings=3)
    seams = mandatory_seam_edges(mesh)
    charts = flood_charts(mesh, seams)
    assert len(charts) == 1

    # Pick a face on the legacy normal-split cut, then ring it with protected edges.
    _, _, split_edges = split_chart(mesh, charts[0], set(seams))
    assert split_edges
    hot = mesh.edges[sorted(split_edges)[0]].face_ids[0]
    ring = {e.id for e in mesh.edges
            if set(e.vertex_ids) & set(mesh.faces[hot].vertex_ids)} - set(seams)
    con = SeamConstraints.build(mesh, protected=ring)

    stretch = _stretch(mesh, hot)
    assert C.short_cut_candidate(mesh, charts[0], seams, con, stretch, 0,
                                 top_fraction=0.001) is None

    # The baseline split is NOT silently dropped — it comes back flagged.
    ns = C.normal_split_candidate(mesh, charts[0], seams, con, 0)
    assert ns is not None
    assert ns.kind == "normal_split"
    assert ns.rejected == "protected_edge_cut"
    assert ns.notes["protected_cut"] == sorted(set(ns.added_edges) & con.forbidden)
    assert ns.notes["protected_cut"]


# ------------------------------------------------------------ preferred path


def test_preferred_path_takes_the_preferred_route():
    mesh, vid, seams, charts = _grid()
    hot = 1 * GRID_N + 1
    stretch = _stretch(mesh, hot)

    baseline = C.short_cut_candidate(mesh, charts[0], seams, SeamConstraints.build(mesh),
                                     stretch, 0, top_fraction=0.001)
    assert baseline is not None

    preferred = {mesh.edge_key(vid(2, 1), vid(3, 1)), mesh.edge_key(vid(3, 1), vid(3, 0))}
    assert not (set(baseline.added_edges) & preferred)

    con = SeamConstraints.build(mesh, preferred=preferred)
    cand = C.preferred_path_candidate(mesh, charts[0], seams, con, stretch, 0,
                                      top_fraction=0.001)
    assert cand is not None
    assert cand.kind == "preferred_path"
    assert preferred <= set(cand.added_edges)          # the preferred route was chosen
    assert set(cand.added_edges) != set(baseline.added_edges)
    assert len(flood_charts(mesh, set(seams) | set(cand.added_edges))) == 2
    # No front axis -> visibility neutral.
    assert con.front_axis == ""
    assert cand.exposure_cost == 0.0


def test_preferred_path_is_none_without_any_preference_signal():
    mesh, _, seams, charts = _grid()
    con = SeamConstraints.build(mesh)
    assert con.region_policy is None and not con.preferred and con.front_axis == ""
    assert C.preferred_path_candidate(mesh, charts[0], seams, con,
                                      _stretch(mesh, 1 * GRID_N + 1), 0,
                                      top_fraction=0.001) is None


# ----------------------------------------------------------------- ordering


def test_generate_candidates_dedupes_leads_with_unwrap_only_and_obeys_the_budget():
    mesh, vid, seams, charts = _grid()
    hot = 1 * GRID_N + 1
    stretch = _stretch(mesh, hot)
    # A preferred edge far from the hot region -> preferred_path reproduces short_cut.
    far = {mesh.edge_key(vid(5, 5), vid(6, 5))}
    con = SeamConstraints.build(mesh, preferred=far)

    short = C.short_cut_candidate(mesh, charts[0], seams, con, stretch, 0, top_fraction=0.001)
    pref = C.preferred_path_candidate(mesh, charts[0], seams, con, stretch, 0,
                                      top_fraction=0.001)
    assert short is not None and pref is not None
    assert short.added_edges == pref.added_edges        # duplicate by construction

    cands = C.generate_candidates(mesh, charts, 0, seams, con, stretch, max_candidates=10)
    assert [c.kind for c in cands] == ["unwrap_only", "short_cut"]
    assert cands[0].added_edges == frozenset()
    assert cands[0].seam_length == 0.0 and cands[0].exposure_cost == 0.0
    assert len({c.added_edges for c in cands}) == len(cands)

    assert [c.kind for c in C.generate_candidates(mesh, charts, 0, seams, con, stretch,
                                                  max_candidates=1)] == ["unwrap_only"]
    assert C.generate_candidates(mesh, charts, 0, seams, con, stretch, max_candidates=0) == []

    # Deterministic: same inputs -> same candidates, no seed.
    again = C.generate_candidates(mesh, charts, 0, seams, con, stretch, max_candidates=10)
    assert [c.to_dict() for c in again] == [c.to_dict() for c in cands]


def test_generate_candidates_pushes_rejected_candidates_to_the_end():
    mesh = build_cylinder(segments=12, rings=3)
    seams = mandatory_seam_edges(mesh)
    charts = flood_charts(mesh, seams)
    _, _, split_edges = split_chart(mesh, charts[0], set(seams))
    hot = mesh.edges[sorted(split_edges)[0]].face_ids[0]
    ring = {e.id for e in mesh.edges
            if set(e.vertex_ids) & set(mesh.faces[hot].vertex_ids)} - set(seams)
    con = SeamConstraints.build(mesh, protected=ring)

    cands = C.generate_candidates(mesh, charts, 0, seams, con, _stretch(mesh, hot),
                                  max_candidates=10)
    assert cands[0].kind == "unwrap_only"
    assert cands[-1].kind == "normal_split"
    assert cands[-1].rejected == "protected_edge_cut"
    assert all(not c.rejected for c in cands[:-1])


def test_rank_key_orders_by_islands_then_length_then_exposure():
    a = C.rank_key(2, 1.0, 0.0)
    b = C.rank_key(2, 1.0, 5.0)
    c = C.rank_key(2, 2.0, 0.0)
    d = C.rank_key(3, 0.1, 0.0)
    assert sorted([d, c, b, a]) == [a, b, c, d]
    # Float noise never decides the order.
    assert C.rank_key(2, 1.0 + 1e-12, 0.0) == C.rank_key(2, 1.0, 0.0)


# ------------------------------------------------------- cut cost (W2 §4.3)

STRIP_NX, STRIP_NY = 12, 2


def _strip(*, split_materials: bool = False):
    """A ``12 × 2`` quad strip plane = one chart. Cutting it across the short axis gives
    two fat ``6 × 2`` halves; cutting along the long axis gives two ``12 × 1`` slivers."""
    verts = [(float(i), float(j), 0.0)
             for j in range(STRIP_NY + 1) for i in range(STRIP_NX + 1)]

    def vid(i: int, j: int) -> int:
        return j * (STRIP_NX + 1) + i

    faces = [[vid(i, j), vid(i + 1, j), vid(i + 1, j + 1), vid(i, j + 1)]
             for j in range(STRIP_NY) for i in range(STRIP_NX)]
    mats = None
    if split_materials:
        mats = [0 if j == 0 else 1 for j in range(STRIP_NY) for _ in range(STRIP_NX)]
    mesh = MeshGraph.from_faces("strip", verts, faces, material_indices=mats)
    seams = mandatory_seam_edges(mesh)
    charts = flood_charts(mesh, seams)
    assert len(charts) == 1
    return mesh, vid, seams, charts[0]


def _across_cut(mesh, vid):
    """2 edges cutting the strip across the middle -> two 6 x 2 halves."""
    return {mesh.edge_key(vid(6, j), vid(6, j + 1)) for j in range(STRIP_NY)}


def _along_cut(mesh, vid):
    """12 edges cutting along the long axis -> two 12 x 1 slivers."""
    return {mesh.edge_key(vid(i, 1), vid(i + 1, 1)) for i in range(STRIP_NX)}


def test_cost_breakdown_penalises_the_sliver_cut_over_the_fat_cut():
    mesh, vid, seams, chart = _strip()
    con = SeamConstraints.build(mesh)
    a = C.candidate_cost_breakdown(mesh, chart, seams, _across_cut(mesh, vid), con)
    b = C.candidate_cost_breakdown(mesh, chart, seams, _along_cut(mesh, vid), con)

    assert a["predicted_sub_chart_count"] == 2
    assert a["predicted_sub_chart_face_counts"] == [12, 12]
    assert b["predicted_sub_chart_count"] == 2
    assert b["predicted_sub_chart_face_counts"] == [12, 12]

    # the 12 x 1 halves are slivers; the 6 x 2 halves are not.
    assert a["sliver_creation_penalty"] == 0.0
    assert b["sliver_creation_penalty"] > 0.0
    # neither cut makes a tiny island (both halves are half the chart).
    assert a["small_island_creation_penalty"] == 0.0
    assert b["small_island_creation_penalty"] == 0.0
    # the long cut is the longer seam and the worse cut overall.
    assert b["normalized_seam_length"] > a["normalized_seam_length"]
    assert b["total"] > a["total"]
    # a flat strip has no creases, no materials, no preferences.
    assert b["concave_crease_bonus"] == 0.0
    assert b["material_boundary_bonus"] == 0.0
    assert b["user_preferred_bonus"] == 0.0
    assert b["protected_region_penalty"] == 0.0
    assert b["smooth_surface_penalty"] > 0.0        # cutting across a smooth surface
    # every reported value is JSON-safe.
    assert json.loads(json.dumps(b)) == b


def test_cost_breakdown_is_visibility_neutral_without_a_front_axis():
    mesh, vid, seams, chart = _strip()
    con = SeamConstraints.build(mesh)
    assert con.front_axis == ""
    cost = C.candidate_cost_breakdown(mesh, chart, seams, _along_cut(mesh, vid), con)
    assert cost["visible_surface_penalty"] == 0.0
    assert cost["hidden_back_bonus"] == 0.0


def test_cost_breakdown_rewards_material_boundaries_and_user_preferences():
    mesh, vid, seams, chart = _strip(split_materials=True)
    cut = _along_cut(mesh, vid)
    plain = C.candidate_cost_breakdown(mesh, chart, seams, cut,
                                       SeamConstraints.build(mesh))
    assert plain["material_boundary_bonus"] > 0.0          # the cut IS the material border
    assert plain["user_preferred_bonus"] == 0.0

    pref = sorted(cut)[:3]
    con = SeamConstraints.build(mesh, preferred=pref)
    liked = C.candidate_cost_breakdown(mesh, chart, seams, cut, con)
    assert liked["user_preferred_bonus"] > 0.0
    assert liked["total"] < plain["total"]

    # the same cut across the SHORT axis stays inside one material.
    across = C.candidate_cost_breakdown(mesh, chart, seams, _across_cut(mesh, vid),
                                        SeamConstraints.build(mesh))
    assert across["material_boundary_bonus"] == 0.0


def test_cost_breakdown_counts_protected_edges_that_mandatory_overrode():
    folded = build_folded_planes(6)
    seams = mandatory_seam_edges(folded)
    charts = flood_charts(folded, seams)
    fold = sorted(e.id for e in folded.edges
                  if len(e.face_ids) == 2 and e.dihedral_angle >= 90.0)
    con = SeamConstraints.build(folded, protected={fold[0]})
    assert fold[0] not in con.forbidden                   # mandatory won
    cost = C.candidate_cost_breakdown(folded, charts[0], seams, {fold[0]}, con)
    assert cost["protected_region_penalty"] == 1.0
    assert cost["concave_crease_bonus"] > 0.0             # a crease is a good seam line
    assert cost["smooth_surface_penalty"] == 0.0


def test_unwrap_only_cost_is_all_zero():
    cand = C.unwrap_only_candidate(3)
    assert cand.cut_reason == "distortion_repair"
    cost = cand.cost
    for key in C.COST_PENALTY_KEYS + C.COST_BONUS_KEYS:
        assert cost[key] == 0.0
    assert cost["total"] == 0.0
    assert cost["predicted_sub_chart_count"] == 1
    assert json.loads(json.dumps(cand.to_dict())) == cand.to_dict()


def test_rank_key_breaks_remaining_ties_on_total_cost():
    a = C.rank_key(2, 1.0, 0.0, 0.5)
    b = C.rank_key(2, 1.0, 0.0, 1.5)
    c = C.rank_key(2, 1.0, 5.0, 0.0)
    d = C.rank_key(2, 2.0, 0.0, 0.0)
    e = C.rank_key(3, 0.1, 0.0, 0.0)
    assert sorted([e, d, c, b, a]) == [a, b, c, d, e]
    assert len(a) == 4
    # existing 3-argument callers keep working, with total_cost = 0.
    assert C.rank_key(2, 1.0, 0.0) == (2, 1.0, 0.0, 0.0)
    assert C.rank_key(2, 1.0, 0.0, 1e-12) == C.rank_key(2, 1.0, 0.0)


def test_generated_candidates_carry_a_cut_reason_and_a_cost_breakdown():
    mesh, vid, seams, charts = _grid()
    hot = 1 * GRID_N + 1
    con = SeamConstraints.build(mesh)
    cands = C.generate_candidates(mesh, charts, 0, seams, con, _stretch(mesh, hot),
                                  max_candidates=10, cut_reason="correctness_repair")
    assert len(cands) >= 2
    keys = set(C.COST_PENALTY_KEYS + C.COST_BONUS_KEYS) | {
        "total", "predicted_sub_chart_count", "predicted_sub_chart_face_counts"}
    for cand in cands:
        assert cand.cut_reason == "correctness_repair"
        assert set(cand.cost) == keys
        d = cand.to_dict()
        assert d["cut_reason"] == "correctness_repair"
        assert d["cost"] == cand.cost
        assert json.loads(json.dumps(d)) == d

    # default reason code, and a cut candidate really predicts two sub-charts.
    default = C.generate_candidates(mesh, charts, 0, seams, con, _stretch(mesh, hot),
                                    max_candidates=10)
    assert all(c.cut_reason == "distortion_repair" for c in default)
    cut = [c for c in default if c.added_edges][0]
    assert cut.cost["predicted_sub_chart_count"] == 2
    assert cut.cost["normalized_seam_length"] > 0.0
