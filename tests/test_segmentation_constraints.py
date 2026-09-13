"""G4 — the same user constraints (mandatory seams, user seam lock, protected edges) are
verified on EVERY segmentation path: initial split, disk-ification, absorb, merge,
straightening and welded-fold repair. Pure Python, no Blender."""

from chart_uv_agent.fixtures import (
    build_capsule_with_spikes, build_displaced_sphere, build_folded_planes,
)
from chart_uv_agent.segmentation import (
    constrained_split_chart, flood_charts, mandatory_seam_edges, seam_set_audit, segment,
    split_chart, split_welded_folds,
)


def _interior_edges(mesh, seams):
    return [e.id for e in mesh.edges if len(e.face_ids) == 2 and e.id not in seams]


# -- regression: the constrained arguments default to the old behaviour ------------------


def test_default_args_reproduce_unconstrained_segmentation():
    for mesh in (build_displaced_sphere(), build_capsule_with_spikes()):
        base = segment(mesh, cone_limit=50.0)
        same = segment(mesh, cone_limit=50.0, locked_seams=set(), forbidden=set())
        assert same.seams == base.seams
        assert same.face_chart == base.face_chart


# -- user seam lock: never removed by any pass -------------------------------------------


def test_locked_seams_survive_every_pass():
    mesh = build_displaced_sphere()
    mandatory = mandatory_seam_edges(mesh)
    locked = set(_interior_edges(mesh, mandatory)[:5])
    assert len(locked) == 5

    seg = segment(mesh, cone_limit=50.0, locked_seams=locked)
    assert locked.issubset(seg.seams)                       # split/absorb/merge/straighten
    audit = seam_set_audit(mesh, seg.seams, locked=locked)
    assert audit["locked_missing"] == []
    assert audit["mandatory_90_missing"] == 0
    final = [h for h in seg.history if h["stage"] == "final"][0]
    assert final["locked_missing"] == 0
    assert [h for h in seg.history if h["stage"] == "initial"][0]["locked_seams"] == 5


def test_absorb_merge_straighten_never_dissolve_a_locked_wall():
    # A 3-face patch walled entirely by locked seams is an absorb target (< 5 faces), but the
    # lock wins: absorb/merge/straighten never dissolve the wall nor move a face across it.
    mesh = build_displaced_sphere()
    adj = mesh.face_adjacency()
    small = {0}
    for nb, _ in adj[0]:
        if len(small) < 3:
            small.add(nb)
    locked = {eid for f in small for nb, eid in adj[f] if nb not in small}

    seg = segment(mesh, cone_limit=50.0, locked_seams=locked)
    assert locked.issubset(seg.seams)
    # No chart straddles the locked wall: the patch is an exact union of whole charts, so
    # nothing was absorbed/merged/straightened across it.
    for fs in seg.charts.values():
        inside = set(fs) & small
        assert not inside or set(fs) <= small
    assert seam_set_audit(mesh, seg.seams, locked=locked)["locked_missing"] == []


# -- protected (forbidden) edges: never cut ----------------------------------------------


def test_forbidden_edges_are_never_introduced_as_seams():
    mesh = build_displaced_sphere()
    mandatory = mandatory_seam_edges(mesh)
    # A protected band: every non-mandatory interior edge in the upper hemisphere.
    forbidden = {e.id for e in mesh.edges
                 if len(e.face_ids) == 2 and e.id not in mandatory
                 and all(mesh.vertices[v].co[2] > 0.0 for v in e.vertex_ids)}
    assert forbidden

    seg = segment(mesh, cone_limit=50.0, forbidden=forbidden)
    assert (set(seg.seams) & forbidden) - mandatory == set()
    audit = seam_set_audit(mesh, seg.seams, forbidden=forbidden)
    assert audit["forbidden_in_seams"] == []
    assert audit["mandatory_90_missing"] == 0                # R2 still wins

    stages = {h["stage"]: h for h in seg.history}
    for st in ("split", "diskify"):
        assert isinstance(stages[st]["rejected_forbidden"], int)
        assert stages[st]["rejected_forbidden"] >= 0
    assert stages["final"]["forbidden_in_seams"] == 0


def test_constrained_split_chart_rejects_the_whole_cut():
    mesh = build_displaced_sphere()
    seams = set(mandatory_seam_edges(mesh))
    chart = max(flood_charts(mesh, seams), key=len)
    _, _, baseline = split_chart(mesh, chart, seams)
    assert baseline

    # Unconstrained: identical to split_chart, nothing rejected.
    ga, gb, ns, rejected = constrained_split_chart(mesh, chart, seams, set())
    assert ns == baseline and rejected == []
    assert ga and gb

    # One protected edge on the cut rejects the ENTIRE cut (no silent partial filtering).
    forbidden = {baseline[len(baseline) // 2]}
    _, _, ns2, rejected2 = constrained_split_chart(mesh, chart, seams, forbidden)
    assert ns2 == []
    assert rejected2 == sorted(forbidden)


# -- welded-fold repair: blocked instead of a silent partial cut -------------------------


def test_split_welded_folds_reports_blocked_when_no_legal_cut_exists():
    mesh = build_folded_planes(n=6)
    folds = [e.id for e in mesh.edges if len(e.face_ids) == 2 and e.dihedral_angle >= 89.0]
    assert len(folds) >= 3
    target = folds[len(folds) // 2]
    # Seam everything except ONE fold edge, so that fold is welded inside a single chart...
    seams = {e.id for e in mesh.edges if e.is_boundary} | (set(folds) - {target})
    # ...and protect every remaining cuttable edge, so neither the local cut nor the VSA
    # fallback has a legal path.
    forbidden = set(_interior_edges(mesh, seams))
    before = set(seams)

    r = split_welded_folds(mesh, seams, [target], forbidden=forbidden)
    assert r["blocked"] >= 1
    assert target in r["blocked_edge_ids"]
    assert r["added"] == set()
    assert seams == before                       # fully reverted, no partial cut left behind
    assert r["local_cuts"] == 0 and r["fallback"] == 0


def test_split_welded_folds_keeps_existing_keys_and_counts_unblocked_runs():
    mesh = build_capsule_with_spikes(n_spikes=5, spike_len=1.8)
    from chart_uv_agent.segmentation import interior_fold_edges
    seams = mandatory_seam_edges(mesh)
    welded = interior_fold_edges(mesh, seams)[:3]
    assert welded
    r = split_welded_folds(mesh, seams, welded)
    for k in ("added", "local_cuts", "fallback", "blocked", "blocked_edge_ids"):
        assert k in r
    assert r["blocked"] == 0 and r["blocked_edge_ids"] == []
    assert r["local_cuts"] + r["fallback"] >= 1


# -- shading-required seams: an ENGINE rule that is never removed (G4/G10) ---------------


def _required_constraints(mesh, required):
    from chart_uv_agent.constraints import SeamConstraints
    return SeamConstraints.build(mesh, required=required)


def test_required_seams_are_never_removable():
    mesh = build_folded_planes(n=6)
    mandatory = mandatory_seam_edges(mesh)
    required = set(_interior_edges(mesh, mandatory)[:3])
    assert len(required) == 3

    con = _required_constraints(mesh, required)
    assert con.required == frozenset(required)
    # ``mandatory`` stays the DIHEDRAL set — a required edge never leaks into it.
    assert not (con.mandatory & con.required)
    assert con.never_removed == frozenset(con.mandatory | con.locked | con.required)

    removed = con.check_removed(sorted(required))
    assert removed["ok"] is False
    assert removed["required_removed"] == sorted(required)
    assert removed["reason"] == "required_seam_removed"
    # mandatory and locked still outrank the required reason.
    both = con.check_removed(sorted(required) + sorted(con.mandatory)[:1])
    assert both["reason"] == "mandatory_seam_removed"


def test_required_seam_is_free_to_cut_and_never_forbidden():
    mesh = build_folded_planes(n=6)
    mandatory = mandatory_seam_edges(mesh)
    free = _interior_edges(mesh, mandatory)
    required = {free[0]}
    protected = {free[0], free[1]}

    from chart_uv_agent.constraints import SeamConstraints
    con = SeamConstraints.build(mesh, protected=protected, required=required)

    # protected ∩ required → the required (engine) rule wins, and it is RECORDED.
    assert free[0] not in con.forbidden
    assert free[1] in con.forbidden
    conflicts = [c for c in con.conflicts if c["engine_rule"] == "shading_required"]
    assert conflicts == [{"edge_id": free[0], "user_rule": "protected",
                          "engine_rule": "shading_required",
                          "resolution": "required_wins"}]
    # A required edge is already a seam, so routing "through" it costs nothing.
    assert con.edge_cost(mesh, free[0]) == 0.0
    assert con.to_report()["required_count"] == 1
