"""Catastrophic-distortion repair: R1 same-seam re-unwrap, then R2 relief seam (CG5/CG6).

The catastrophic gate (:mod:`uv_agent.geometry.catastrophic_distortion`) answers "is this
layout broken". This module answers "what do we try, in what order, and when may we keep
it" — and the ORDER is the point (plan §7, gate CG5):

``R1``  re-unwrap the SAME faces with a different solver setting
        (:data:`~chart_uv_agent.unwrap.UNWRAP_VARIANTS`). Zero new seams. A needle caused
        by a solver landing badly is not a reason to cut the model.
``R2``  only when every R1 variant failed: cut a RELIEF seam from the bad region out to
        the chart boundary. This is the first thing that changes the seam set, and it is
        deliberately the last thing tried.

Acceptance (plan §6, gate CG6) is stricter than the ordinary distortion accept: a repair
may not trade one broken triangle for another, so EVERY catastrophic counter must hold or
improve, the hard anisotropy maximum may not rise, and the targeted region itself must
either clear or improve by ``min_improvement_ratio``.

Pure orchestration: Blender is reached only through :mod:`chart_uv_agent.unwrap` module
attributes resolved at call time, so this whole module runs off-Blender against
``tests.helpers.fake_blender_uv.FakeUnwrapBackend``. ``bpy`` is never imported here.
"""

from __future__ import annotations

import math

from chart_uv_agent.candidates import (
    SeamCandidate,
    _make,
    _splits_in_two,
    _two_leg_cut,
    candidate_cost_breakdown,
    normal_split_candidate,
)
from chart_uv_agent.constraints import edge_length
from chart_uv_agent.quality_profile import QualityProfile
from chart_uv_agent.segmentation import _chart_boundary_vertices
from chart_uv_agent.unwrap import UNWRAP_VARIANTS
from uv_agent.geometry.catastrophic_distortion import catastrophic_counters
from uv_agent.geometry.mesh_graph import MeshGraph

#: The R1 (same-seam) re-unwrap variants, in the order they are tried. Shared with the
#: unwrap module on purpose — one list, never a second copy that can drift.
R1_VARIANTS: tuple[dict, ...] = UNWRAP_VARIANTS

#: WHY every candidate this module produces exists (W2 §4.3 reason code).
CUT_REASON = "catastrophic_repair"

#: Dihedral (degrees) at/above which an edge is a crease the relief cut prefers to follow.
CREASE_ANGLE = 45.0
#: Weight of the crease preference in the ``relief_crease`` path cost.
CREASE_WEIGHT = 4.0
#: Multiplier applied to the ``relief_no_fragment`` cost when the cut is predicted to
#: create a sliver / dust sub-chart — big enough that such a cut always loses to a clean one.
FRAGMENTATION_PENALTY = 100.0

#: Path kinds this module proposes, in production order.
RELIEF_REASONS = ("relief_min_cost", "relief_crease", "relief_no_fragment", "normal_split")


# --------------------------------------------------------------- R1 re-unwrap


def reunwrap_candidates(target: dict, profile: QualityProfile) -> list[dict]:
    """The R1 candidate specs for ``target`` — same seams, different solver settings.

    At most ``profile.catastrophic_reunwrap_variants`` of :data:`R1_VARIANTS`, in order.
    ``minimize_iters`` is forced to 0 for SLIM (``MINIMUM_STRETCH``): ``minimize_stretch``
    is not locally injective and would re-introduce exactly the folds R1 is repairing.
    """
    count = max(0, int(getattr(profile, "catastrophic_reunwrap_variants", 0) or 0))
    out: list[dict] = []
    for variant in R1_VARIANTS[:count]:
        method = str(variant.get("method", "MINIMUM_STRETCH"))
        minimize = (0 if method == "MINIMUM_STRETCH"
                    else int(variant.get("minimize_iters", 0) or 0))
        out.append({
            "kind": "reunwrap",
            "variant_id": str(variant.get("id", method)),
            "method": method,
            "iterations": variant.get("iterations"),
            "no_flip": bool(variant.get("no_flip", False)),
            "fill_holes": bool(variant.get("fill_holes", False)),
            "minimize_iters": minimize,
            "added_edges": frozenset(),
            "region_id": target.get("region_id"),
            "target_island": int(target.get("island_id", -1)),
            "cut_reason": CUT_REASON,
        })
    return out


def apply_reunwrap_variant(obj, mesh: MeshGraph, island_faces, variant: dict, *,
                           margin: float) -> int:
    """Re-unwrap ``island_faces`` with ``variant``'s solver settings, then re-pack (R1).

    The seam set is NOT touched — that is the whole contract of R1. Returns the face count
    handed to the backend."""
    from chart_uv_agent import unwrap as unwrap_mod

    faces = sorted(int(f) for f in island_faces)
    method = str(variant.get("method", "MINIMUM_STRETCH"))
    minimize = (0 if method == "MINIMUM_STRETCH"
                else int(variant.get("minimize_iters", 0) or 0))
    unwrap_mod.reunwrap_faces(
        obj, faces,
        method=method,
        minimize_iters=minimize,
        iterations=variant.get("iterations"),
        no_flip=bool(variant.get("no_flip", False)),
        fill_holes=bool(variant.get("fill_holes", False)),
    )
    unwrap_mod.repack(obj, margin=float(margin))
    return len(faces)


# ------------------------------------------------------------- R2 relief seam


def _chart_of_region(charts, target_island: int, region: set[int]) -> tuple[int, set[int]]:
    """The chart the bad region actually lives in.

    ``target_island`` is trusted when it really contains the region; otherwise the chart
    with the largest overlap wins, so a stale island id from a previous round can never
    silently send the cut into the wrong chart."""
    charts = [set(int(f) for f in c) for c in charts]
    if 0 <= int(target_island) < len(charts) and region & charts[int(target_island)]:
        return int(target_island), charts[int(target_island)]
    best = (-1, -1, set())
    for index, faces in enumerate(charts):
        overlap = len(faces & region)
        if overlap > best[0]:
            best = (overlap, index, faces)
    if best[0] <= 0:
        return int(target_island), set()
    return int(best[1]), set(best[2])


def _predicted_fragmentation(cost: dict) -> bool:
    """Does the itemised cut cost predict a new sliver / dust sub-chart (CG6)?"""
    return bool(float(cost.get("sliver_creation_penalty", 0.0)) > 0.0
                or float(cost.get("small_island_creation_penalty", 0.0)) > 0.0)


def relief_rank_key(cand: SeamCandidate) -> tuple:
    """Deterministic order for relief candidates of equal standing (CG5).

    Fewest predicted sub-charts → not predicted to fragment → shortest normalised cut →
    least exposed → cheapest total cost → the path kind's own name as the final tie-break,
    so two calls on the same mesh always produce the same list."""
    cost = dict(cand.cost or {})
    return (
        int(cost.get("predicted_sub_chart_count", 1 << 30)),
        1 if bool(cand.notes.get("predicted_fragmentation")) else 0,
        round(float(cost.get("normalized_seam_length", 0.0)), 9),
        round(float(cand.exposure_cost), 9),
        round(float(cost.get("total", 0.0)), 9),
        str(cand.reason),
    )


def relief_seam_candidates(mesh: MeshGraph, charts, target_island: int, seams,
                           constraints, region_faces, *,
                           max_candidates: int = 4) -> list[SeamCandidate]:
    """Relief cuts from the BAD REGION out to the chart boundary (R2, plan §7).

    Unlike :func:`chart_uv_agent.candidates.short_cut_candidate`, the cut is seeded from
    the catastrophic region, NOT from the chart's top-stretch faces: the whole point of a
    relief seam is to let the broken place open up, and the broken place is rarely the
    place with the worst average stretch.

    Four path kinds are tried — cheapest cut, crease-preferring cut, fragmentation-averse
    cut, and the legacy normal split as a baseline. Candidates with identical
    ``added_edges`` are de-duplicated (first wins), constraint-rejected ones are pushed to
    the END (kept for the history, never chosen ahead of a valid one), and the list is
    truncated to ``max_candidates``.
    """
    if int(max_candidates) <= 0:
        return []
    region = {int(f) for f in region_faces}
    island_id, chart_faces = _chart_of_region(charts, target_island, region)
    if len(chart_faces) < 2:
        return []
    seam_set = {int(e) for e in seams}
    region = region & chart_faces
    if not region:
        return []
    bverts = _chart_boundary_vertices(mesh, chart_faces, seam_set)
    if not bverts:
        return []

    inf = float("inf")

    def base_cost(eid: int) -> float:
        return constraints.edge_cost(mesh, eid)

    def crease_cost(eid: int) -> float:
        base = base_cost(eid)
        if base == inf or base != base:
            return base
        dihedral = float(mesh.edges[eid].dihedral_angle)
        return base + CREASE_WEIGHT * edge_length(mesh, eid) * max(
            0.0, 1.0 - dihedral / CREASE_ANGLE)

    def cut_for(cost_fn):
        found = _two_leg_cut(mesh, chart_faces, seam_set, sorted(region), bverts, cost_fn)
        if found is None:
            return None
        cut, cost, start = found
        if not _splits_in_two(mesh, chart_faces, seam_set, cut):
            return None
        return cut, float(cost), int(start)

    produced: list[SeamCandidate] = []

    def emit(reason: str, cut: set[int], path_cost: float, start: int,
             *, fragmentation_aware: bool = False) -> None:
        notes = {"region_faces": len(region), "path_cost": float(path_cost),
                 "start_vertex": int(start), "relief": True}
        if fragmentation_aware:
            breakdown = candidate_cost_breakdown(mesh, chart_faces, seam_set, cut,
                                                 constraints)
            if _predicted_fragmentation(breakdown):
                notes["predicted_fragmentation"] = True
                notes["fragmentation_penalty"] = float(FRAGMENTATION_PENALTY)
        cand = _make(mesh, "short_cut", cut, island_id, constraints, reason, notes,
                     chart_faces=chart_faces, seams=seam_set, cut_reason=CUT_REASON)
        if notes.get("predicted_fragmentation"):
            # The penalty lives in the RANKING copy of the cost, never in the itemised
            # breakdown a reviewer reads — the breakdown must stay the measured truth.
            cand.cost["total"] = float(cand.cost.get("total", 0.0)) * FRAGMENTATION_PENALTY
        produced.append(cand)

    found = cut_for(base_cost)
    if found is not None:
        emit("relief_min_cost", *found)
    found = cut_for(crease_cost)
    if found is not None:
        emit("relief_crease", *found)
    found = cut_for(base_cost)
    if found is not None:
        emit("relief_no_fragment", *found, fragmentation_aware=True)

    baseline = normal_split_candidate(mesh, chart_faces, seam_set, constraints, island_id,
                                      cut_reason=CUT_REASON)
    if baseline is not None:
        produced.append(baseline)

    seen: set[frozenset[int]] = set()
    unique: list[SeamCandidate] = []
    for cand in produced:
        if not cand.added_edges or cand.added_edges in seen:
            continue
        seen.add(cand.added_edges)
        unique.append(cand)

    usable = sorted((c for c in unique if not c.rejected), key=relief_rank_key)
    blocked = sorted((c for c in unique if c.rejected), key=relief_rank_key)
    return (usable + blocked)[:int(max_candidates)]


# --------------------------------------------------------------- acceptance


def _counters(measurement: dict) -> tuple:
    return catastrophic_counters((measurement or {}).get("catastrophic") or {})


def _correctness_counters(measurement: dict) -> tuple[float, int, int]:
    corr = (measurement or {}).get("correctness") or {}
    return (
        float((corr.get("overlap") or {}).get("overlap_area_total", 0.0)),
        int((corr.get("orientation") or {}).get("local_flip_count", 0)),
        int((corr.get("degenerate") or {}).get("uv_degenerate_count", 0)),
    )


def _hard_max(measurement: dict) -> float:
    """``max_anisotropy`` of the catastrophic report; ``0.0`` when nothing was measurable.

    ``None`` here means "no finite anisotropy anywhere", which is the *absence* of a hard
    maximum, not an infinite one — treating it as 0.0 keeps "did the ceiling rise" honest.
    """
    value = ((measurement or {}).get("catastrophic") or {}).get("max_anisotropy")
    if value is None:
        return 0.0
    v = float(value)
    return v if math.isfinite(v) else 0.0


#: Relative slack when comparing the FLOAT catastrophic counters. Counts are compared
#: exactly; ``bad_area_fraction`` / ``max_anisotropy`` are re-derived from a fresh unwrap,
#: so last-bit float noise must never read as "the repair made it worse".
COUNTER_TOLERANCE = 1e-9


def _counters_not_worse(after: tuple, before: tuple) -> bool:
    """No component of the counter tuple got worse (integers exactly, floats with slack)."""
    for a, b in zip(after, before):
        if isinstance(a, int) and isinstance(b, int):
            if a > b:
                return False
            continue
        av, bv = float(a), float(b)
        if av > bv + COUNTER_TOLERANCE * max(1.0, abs(bv)):
            return False
    return True


def _region_score(region) -> float:
    if isinstance(region, dict):
        return float(region.get("score", float("nan")))
    try:
        return float(region)
    except (TypeError, ValueError):
        return float("nan")


def _region_bad_triangles(region) -> int | None:
    if isinstance(region, dict) and "bad_triangle_count" in region:
        return int(region.get("bad_triangle_count") or 0)
    return None


def accept_catastrophic_candidate(profile: QualityProfile, *, before: dict, after: dict,
                                  target_faces, mesh: MeshGraph,
                                  correctness_ok: bool, constraints_ok: bool,
                                  mandatory_ok: bool, fragmentation_ok: bool,
                                  island_cap_ok: bool, regression_ok: bool,
                                  region_before, region_after) -> dict:
    """Decide whether a catastrophic repair candidate is kept (plan §6 / gate CG6).

    ALL of the following must hold — the first one that does not names the rejection:

    ``correctness_regression`` / ``constraint_violation`` / ``mandatory_seam_violation``
        the ordinary hard invariants, checked first because nothing else matters if the
        layout stopped being a valid layout;
    ``catastrophic_not_improved``
        no component of :func:`catastrophic_counters` may get WORSE, and
        ``bad_triangle_count`` must strictly decrease or reach 0 — a repair that leaves
        the same number of broken triangles is not a repair. ``bad_area_fraction`` is held
        to the same decrease-or-zero rule;
    ``hard_max_increased``
        the catastrophic ``max_anisotropy`` may not rise (never trade a fixed needle for a
        new one somewhere else);
    ``correctness_counter_increased``
        local flips / overlap area / degenerate UV triangles may not increase;
    ``fragmentation_limit_exceeded`` / ``island_cap_reached`` / ``regression_budget_exceeded``
        the G6/G5 budgets the ordinary loop already enforces;
    ``insufficient_region_improvement``
        the TARGET region must clear (0 bad triangles after ⇒ ``region_cleared``) or
        improve by at least ``profile.min_improvement_ratio``.

    ``region_before`` / ``region_after`` are the per-region sub-reports (a dict with
    ``score`` / ``bad_triangle_count``) or plain scores. ``mesh`` and ``target_faces`` are
    accepted so a caller never has to re-derive the scope this verdict describes; the
    verdict itself is a pure function of the numbers handed in.
    """
    b_score = _region_score(region_before)
    a_score = _region_score(region_after)
    measurable = (math.isfinite(b_score) and math.isfinite(a_score) and b_score > 1e-12)
    ratio = ((b_score - a_score) / b_score) if measurable else 0.0

    b_counters = _counters(before)
    a_counters = _counters(after)
    counters_ok = _counters_not_worse(a_counters, b_counters)
    b_bad = int(b_counters[0])
    a_bad = int(a_counters[0])
    bad_triangles_ok = bool(a_bad == 0 or a_bad < b_bad)
    b_area = float((before or {}).get("catastrophic", {}).get("bad_area_fraction", 0.0))
    a_area = float((after or {}).get("catastrophic", {}).get("bad_area_fraction", 0.0))
    bad_area_ok = bool(a_area <= 0.0 or a_area < b_area - 1e-15)
    hard_max_ok = bool(_hard_max(after) <= _hard_max(before) + 1e-12)

    b_overlap, b_flip, b_degen = _correctness_counters(before)
    a_overlap, a_flip, a_degen = _correctness_counters(after)
    counts_ok = bool(a_overlap <= b_overlap + 1e-12 and a_flip <= b_flip
                     and a_degen <= b_degen)

    after_bad = _region_bad_triangles(region_after)
    region_cleared = bool(after_bad == 0)
    region_improved = bool(measurable and a_score <= b_score * (
        1.0 - float(profile.min_improvement_ratio)) + 1e-15)

    checks = {
        "correctness_ok": bool(correctness_ok),
        "constraints_ok": bool(constraints_ok),
        "mandatory_ok": bool(mandatory_ok),
        "counters_ok": bool(counters_ok),
        "bad_triangles_ok": bool(bad_triangles_ok),
        "bad_area_ok": bool(bad_area_ok),
        "hard_max_ok": bool(hard_max_ok),
        "correctness_counts_ok": bool(counts_ok),
        "fragmentation_ok": bool(fragmentation_ok),
        "island_cap_ok": bool(island_cap_ok),
        "regression_ok": bool(regression_ok),
        "region_cleared": region_cleared,
        "region_improved": region_improved,
        "counters_before": list(b_counters),
        "counters_after": list(a_counters),
    }

    order = (
        (correctness_ok, "correctness_regression"),
        (constraints_ok, "constraint_violation"),
        (mandatory_ok, "mandatory_seam_violation"),
        # ``max_anisotropy`` is also the last component of ``catastrophic_counters``, so a
        # risen hard maximum would otherwise be reported as the vaguer
        # ``catastrophic_not_improved``; the specific reason is checked first.
        (hard_max_ok, "hard_max_increased"),
        (counters_ok and bad_triangles_ok and bad_area_ok, "catastrophic_not_improved"),
        (counts_ok, "correctness_counter_increased"),
        (fragmentation_ok, "fragmentation_limit_exceeded"),
        (island_cap_ok, "island_cap_reached"),
        (regression_ok, "regression_budget_exceeded"),
    )
    for ok, reason in order:
        if not ok:
            return {"accepted": False, "reason": reason,
                    "improvement_ratio": float(ratio), "checks": checks}

    if region_cleared:
        return {"accepted": True, "reason": "region_cleared",
                "improvement_ratio": float(ratio), "checks": checks}
    if region_improved:
        return {"accepted": True, "reason": "region_improved",
                "improvement_ratio": float(ratio), "checks": checks}
    return {"accepted": False, "reason": "insufficient_region_improvement",
            "improvement_ratio": float(ratio), "checks": checks}


__all__ = [
    "CUT_REASON",
    "R1_VARIANTS",
    "RELIEF_REASONS",
    "accept_catastrophic_candidate",
    "apply_reunwrap_variant",
    "relief_rank_key",
    "relief_seam_candidates",
    "reunwrap_candidates",
]
