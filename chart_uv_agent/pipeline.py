"""Phase U4 — chart-UV pipeline orchestration + refinement loop (chart-UV plan §8).

U1 segments → U2 unwraps + packs → U4 gates; on a hard-gate miss the loop refines and
retries (max ~6 rounds, monotonic-best like A4): flipped charts are re-split, the
worst-stretch chart is split, a packing miss is retuned then (if needed) the largest
chart split. No silent shipping — a hard-gate failure after the loop returns the best
attempt marked ``failed``. The Smart-UV fallback is never produced (hard gate).

The segmentation/split decisions are pure (U1); the unwrap/pack/measure steps are
Blender. ``run_chart_uv`` runs inside Blender; ``_chart_metrics`` and the refinement
helpers are import-safe so they can be unit-tested without ``bpy``.
"""

from __future__ import annotations

import dataclasses
import time

from chart_uv_agent import refinement_loop
from chart_uv_agent.gate import ChartGateConfig, ChartGateResult, evaluate_chart_gate
from chart_uv_agent.segmentation import (
    constrained_split_chart, flood_charts, segment, split_chart,
)
from uv_agent.geometry.evaluation import (
    estimate_vt_count, evaluate_uv_solution, per_face_stretch,
    raster_overlap_diagnosis, relative_small_island_ratio, uv_bounds_ok,
)
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap

#: The loop-termination reasons the automatic path may report (G5).
TERMINATION_REASONS = ("quality_passed", "max_rounds", "no_improving_candidate",
                       "island_cap", "time_budget", "no_failing_target")


def _chart_metrics(mesh: MeshGraph, uvmap: UVMap, evaluation, *, fallback_used: bool = False) -> dict:
    """Flat metric dict for :func:`evaluate_chart_gate`."""
    vt = estimate_vt_count(mesh, uvmap)
    return {
        "overlap_ratio": evaluation.overlap_ratio,
        "stretch_score": evaluation.stretch_score,
        "packing_efficiency": evaluation.packing_efficiency,
        "island_count": evaluation.island_count,
        "small_island_ratio": evaluation.small_island_ratio,
        "texel_density_variance": evaluation.texel_density_variance,
        "vt_v_ratio": vt / max(1, mesh.vertex_count),
        "uv_bounds_ok": uv_bounds_ok(uvmap),
        "fallback_used": fallback_used,
        "vt_count": vt,
    }


def _jsonable(value):
    """Recursively coerce numpy scalars/arrays and sets to plain JSON types (G5 evidence:
    ``candidate_history`` must survive ``json.dumps`` without a custom encoder)."""
    import numpy as np

    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        try:
            return [_jsonable(v) for v in sorted(value)]
        except TypeError:
            return [_jsonable(v) for v in sorted(value, key=repr)]
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return [_jsonable(v) for v in value.tolist()]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, bool) or value is None or isinstance(value, (int, float, str)):
        return value
    return str(value)


def _serialize_candidate_history(candidate_history) -> list:
    """JSON-serialisable candidate trial log: the heavyweight ``after_measurement`` (it
    carries a UVMap and numpy arrays) is dropped, everything else is coerced."""
    out: list[dict] = []
    for rec in candidate_history or ():
        row = {k: v for k, v in dict(rec).items() if k != "after_measurement"}
        out.append(_jsonable(row))
    return out


def _resolve_profile(quality_profile=None, *, texture_size_px=None, margin_px=None):
    """Frozen quality profile for this run, with the caller's texture context folded in."""
    from chart_uv_agent.quality_profile import load_quality_profile

    profile = load_quality_profile(quality_profile)
    replace: dict = {}
    if texture_size_px is not None:
        replace["texture_size_px"] = int(texture_size_px)
    if margin_px is not None:
        replace["margin_px"] = int(margin_px)
    if replace:
        profile = dataclasses.replace(profile, **replace)
    return profile


def _resolve_budget(profile, budget=None, *, seed=None) -> dict:
    from chart_uv_agent.refinement_loop import resolve_budget

    overrides = dict(budget or {})
    if seed is not None:
        overrides["seed"] = int(seed)
    return resolve_budget(profile, overrides)


def _termination_block(records, *, budget: dict, elapsed: float, passed: bool,
                       exhausted_rounds: bool, candidate_count: int | None = None,
                       island_count: int | None = None,
                       island_cap: int | None = None) -> dict:
    """Roll the per-round :func:`run_refinement` terminations up into ONE explicit
    termination for the whole pipeline (G5: 종료 이유 + 실제 사용량 기록).

    ``candidates_evaluated`` is the LENGTH of the shipped ``candidate_history`` when it is
    known, so the reported count and the evidence log can never disagree. When the loop
    evaluated no candidate at all the reason must be the precise one (``max_rounds`` /
    ``no_failing_target``), never the catch-all ``no_improving_candidate``."""
    reasons = [str(r.get("reason", "")) for r in records]
    evaluated = int(sum(int(r.get("candidates_evaluated", 0)) for r in records))
    if candidate_count is not None:
        evaluated = int(candidate_count)
    if passed:
        reason = "quality_passed"
    elif "time_budget" in reasons:
        reason = "time_budget"
    elif "island_cap" in reasons:
        reason = "island_cap"
    elif (evaluated == 0 and island_count is not None and island_cap is not None
          and int(island_count) >= int(island_cap)):
        # The island cap was already reached before any candidate could be proposed, so the
        # refinement loop never ran. The honest reason is the cap, not "max_rounds" and not
        # the catch-all "no_improving_candidate" (G5: 종료 이유 정확성).
        reason = "island_cap"
    elif exhausted_rounds:
        reason = "max_rounds"
    elif evaluated == 0 and "no_failing_target" in reasons:
        reason = "no_failing_target"
    else:
        reason = "no_improving_candidate"
    return {
        "reason": reason,
        "iterations": int(sum(int(r.get("iterations", 0)) for r in records)),
        "candidates_evaluated": evaluated,
        "elapsed_s": float(elapsed),
        "budget": dict(budget),
        "round_reasons": reasons,
    }


#: History rows a catastrophic repair round writes (CG2/CG5).
_CATASTROPHIC_CUT_REASON = "catastrophic_repair"

#: The history-row keys a compact repair row keeps (the full row stays in ``history``).
_REPAIR_ROW_KEYS = (
    "round", "action", "reason", "region_id", "target_island", "before", "after",
    "improvement_ratio", "added_edges", "candidate_kind", "candidates_evaluated",
    "bad_triangles_before", "bad_triangles_after", "bad_area_before", "bad_area_after",
    "island_count_before", "island_count_after",
)


def build_repair_summary(history, termination_records) -> dict:
    """What the catastrophic repair loop tried and what it actually bought (CG2/CG13).

    Pure: scans the refinement ``history`` rows tagged ``cut_reason ==
    "catastrophic_repair"`` plus the per-round ``run_refinement`` termination records.
    ``rounds`` prefers the loop's own ``catastrophic_rounds`` counter and falls back to the
    number of catastrophic history rows; ``reason`` is the LAST catastrophic round reason
    (``None`` when no catastrophic round ran)."""
    rows = [dict(r) for r in (history or ())
            if str((r or {}).get("cut_reason", "")) == _CATASTROPHIC_CUT_REASON]
    records = list(termination_records or ())

    rounds = int(sum(int((rec or {}).get("catastrophic_rounds", 0) or 0) for rec in records))
    if not rounds:
        rounds = len(rows)

    reason = None
    for rec in records:
        for entry in ((rec or {}).get("round_reasons") or ()):
            if str((entry or {}).get("kind", "")) == _CATASTROPHIC_CUT_REASON:
                reason = str((entry or {}).get("reason")) if entry.get("reason") is not None else None

    first = rows[0] if rows else {}
    last = rows[-1] if rows else {}

    def _last_present(key):
        for row in reversed(rows):
            if row.get(key) is not None:
                return row.get(key)
        return None

    def _first_present(key):
        for row in rows:
            if row.get(key) is not None:
                return row.get(key)
        return None

    return {
        "rounds": int(rounds),
        "reunwrap_accepted": sum(1 for r in rows if str(r.get("action")) == "reunwrap"),
        "relief_accepted": sum(1 for r in rows if str(r.get("action")) == "split"),
        "rejected": sum(1 for r in rows if str(r.get("action")) == "reject_region"),
        "reason": reason,
        "bad_triangles_before": first.get("bad_triangles_before"),
        "bad_triangles_after": last.get("bad_triangles_after"),
        "bad_area_before": first.get("bad_area_before"),
        "bad_area_after": last.get("bad_area_after"),
        "island_count_before": _first_present("island_count_before"),
        "island_count_after": _last_present("island_count_after"),
        "rows": [{k: row[k] for k in _REPAIR_ROW_KEYS if k in row} for row in rows],
    }


def _auto_constraints_block(constraints, final_seams: set[int], forbidden: set[int]) -> dict:
    """G4 evidence block: what the run was told to protect and what actually shipped."""
    locked = set(constraints.locked)
    required = set(getattr(constraints, "required", ()) or ())
    protected = set(constraints.protected)
    protected_cut = sorted(set(final_seams) & set(forbidden))
    mandatory_cut = sorted(protected & set(constraints.mandatory) & set(final_seams))
    conflicts = [dict(c) for c in constraints.conflicts]
    return {
        "locked_seam_edges": sorted(locked),
        "locked_missing": sorted(locked - set(final_seams)),
        "required_seam_edges": sorted(required),
        "required_missing": sorted(required - set(final_seams)),
        "protected_edges": sorted(protected),
        "protected_cut": protected_cut,
        "conflicts": conflicts,
        "conflicts_unresolved": bool(conflicts and mandatory_cut),
    }


_MANDATORY_GATE_CHECKS = ("mandatory_90_missing", "mandatory_90_uv_unsplit")


def _mandatory_gate_ok(gate) -> bool:
    """True when no MANDATORY hard-gate check failed (other failures don't count here)."""
    return not any(c.name in _MANDATORY_GATE_CHECKS for c in getattr(gate, "failures", ()))


def _v2_result_block(obj, mesh: MeshGraph, final_seams: set[int], *, profile, regions,
                     constraints, forbidden: set[int], mandatory: set[int],
                     distortion_seams: set[int], candidate_history, termination_records,
                     budget: dict, elapsed: float, gate,
                     exhausted_rounds: bool,
                     island_cap: int | None = None,
                     merge_back: dict | None = None,
                     shading: dict | None = None,
                     history=None, unwrap_overrides=()) -> tuple[dict, dict]:
    """Final v2 measurement of the SHIPPED UV plus the G1/G2/G4/G5 report blocks.

    Measures the layout already on ``obj`` (never unwraps), so the numbers describe exactly
    what the run shipped."""
    from chart_uv_agent.quality_report import build_quality_report
    from chart_uv_agent.refinement_loop import measure_layout, seam_length_report

    measurement = measure_layout(obj, mesh, final_seams, profile=profile, stage="final",
                                 regions=regions)
    # The whole v2 report (not the compact view); the two non-serialisable measurement
    # entries (``uvmap`` / ``face_anisotropy``) never enter the result.
    reportable = {k: v for k, v in measurement.items()
                  if k not in ("uvmap", "face_anisotropy")}
    distortion_v2 = reportable["distortion_v2"]
    passed = bool(measurement["passed"])
    repair = build_repair_summary(history, termination_records)
    # CG14/CG0: how many UV-only repairs the shipped layout is replaying.
    repair["overrides"] = len(list(unwrap_overrides or ()))
    block = {
        "distortion_v2": distortion_v2,
        "correctness": measurement["correctness"],
        # CG2/CG3: the hard catastrophic verdict travels with the shipped result, together
        # with the digest of the UVs it was measured on and the raw per-face score the
        # heat map must be rendered from (CG4).
        "catastrophic": measurement["catastrophic"],
        "uv_hash": measurement["uv_hash"],
        "face_score_raw": _jsonable(measurement.get("face_score_raw")),
        "repair": _jsonable(repair),
        "quality": measurement["quality"],
        "mandatory_audit": measurement["mandatory_audit"],
        "fragmentation": measurement["fragmentation"],
        "texel_density": measurement["texel_density"],
        "packing": measurement["packing"],
        "border_inset": measurement["border_inset"],
        "hard_failures": list(measurement["hard_failures"]),
        "quality_failures": list(measurement["quality_failures"]),
        "quality_report": build_quality_report(measurement, profile,
                                               merge_back=merge_back, shading=shading,
                                               repair=repair),
        "uv_island_count": int(measurement["uv_island_count"]),
        "islands_disagree": bool(measurement["islands_disagree"]),
        "quality_profile": profile.to_dict(),
        "constraints": constraints.to_report(),
        "auto_constraints": _auto_constraints_block(constraints, final_seams, forbidden),
        "candidate_history": _serialize_candidate_history(candidate_history),
        "termination": _termination_block(termination_records, budget=budget,
                                          elapsed=elapsed, passed=passed,
                                          exhausted_rounds=exhausted_rounds,
                                          candidate_count=len(candidate_history or ()),
                                          island_count=len(flood_charts(mesh, final_seams)),
                                          island_cap=island_cap),
        "seam_length": seam_length_report(mesh, final_seams, mandatory=mandatory,
                                          user=constraints.locked,
                                          distortion_seams=distortion_seams),
        # G7/G10: an incomplete merge-back or a failing shading policy is a REAL failure of
        # the automatic path, not a remark — ``auto_passed`` may never claim a pass then.
        "auto_passed": bool(passed and _mandatory_gate_ok(gate)
                            and (merge_back is None
                                 or bool(merge_back.get("complete", True)))
                            and (shading is None
                                 or bool(shading.get("passed", True)))),
    }
    return block, measurement


def _input_diagnostics(mesh: MeshGraph, distortion_v2: dict | None) -> dict:
    """G1 (topology/입력): diagnose an abnormal INPUT mesh explicitly, so a run that could
    not be evaluated is never silently reported as clean. Counts only — the gate decides."""
    non_manifold = sum(1 for e in mesh.edges if bool(getattr(e, "is_non_manifold", False)))
    zero_area = sum(1 for f in mesh.faces if float(f.area_3d) <= 1e-12)
    defect = int(((distortion_v2 or {}).get("degenerate_triangles") or {})
                 .get("input_defect_count", 0) or 0)
    used: set[int] = set()
    for f in mesh.faces:
        used.update(int(v) for v in f.vertex_ids)
    isolated = int(max(0, int(mesh.vertex_count) - len(used)))
    out = {
        "non_manifold_edge_count": int(non_manifold),
        "zero_area_face_count": int(zero_area),
        "input_defect_triangle_count": defect,
        "isolated_vertex_count": isolated,
    }
    out["ok"] = not any(out[k] for k in
                        ("non_manifold_edge_count", "zero_area_face_count",
                         "input_defect_triangle_count", "isolated_vertex_count"))
    return out


def _apply_v2_metrics(metrics: dict, measurement: dict) -> None:
    """Add the v2 SUMMARY keys to the existing metric dict; v1 key meanings are untouched."""
    glob = (measurement.get("distortion_v2") or {}).get("global") or {}
    corr = measurement.get("correctness") or {}
    metrics["anisotropy_p95"] = float(glob.get("anisotropy_p95", float("nan")))
    metrics["anisotropy_max"] = float(glob.get("anisotropy_max", float("nan")))
    metrics["area_stretch_mean_v2"] = float(glob.get("area_stretch_mean", float("nan")))
    metrics["exceed_area_fraction"] = float(glob.get("exceed_area_fraction", float("nan")))
    metrics["overlap_area_total"] = float((corr.get("overlap") or {})
                                          .get("overlap_area_total", 0.0))
    metrics["local_flip_count"] = int((corr.get("orientation") or {})
                                      .get("local_flip_count", 0))
    frag = (measurement.get("fragmentation") or {}).get("metrics") or {}
    metrics["tiny_island_count"] = int(frag.get("tiny_island_count", 0) or 0)
    metrics["sliver_island_count"] = int(frag.get("sliver_island_count", 0) or 0)
    metrics["normalized_seam_length"] = float(frag.get("normalized_seam_length", 0.0) or 0.0)
    metrics["texel_density_cv"] = float((measurement.get("texel_density") or {})
                                        .get("density_cv", float("nan")))
    metrics["packing_efficiency_v2"] = float((measurement.get("packing") or {})
                                             .get("efficiency", float("nan")))
    metrics["min_border_gap_px"] = float((corr.get("border_gap") or {})
                                         .get("min_gap_px", float("nan")))
    cat = measurement.get("catastrophic") or {}
    metrics["catastrophic_bad_triangles"] = int(cat.get("bad_triangle_count", 0) or 0)
    metrics["catastrophic_max_anisotropy"] = float(cat.get("max_anisotropy", float("nan"))
                                                   if cat.get("max_anisotropy") is not None
                                                   else float("nan"))
    metrics["catastrophic_bad_area_fraction"] = float(
        cat.get("bad_area_fraction", 0.0) or 0.0)
    metrics["metric_version"] = 2


#: Re-pack margin multipliers tried when the ONLY failing correctness check is the island
#: gap (G1 packing 간격 / G5 "packing 단독 문제로 추가 절개 0").
#: The policy itself lives in :mod:`chart_uv_agent.refinement_loop` so merge-back can reuse
#: it; this name is kept as the pipeline's alias.
_GAP_REPACK_FACTORS = refinement_loop.GAP_REPACK_FACTORS


def _pack_margin_for(profile, margin: float | None) -> float:
    """The UV-space pack margin that satisfies the profile's ``margin_px`` @ ``texture_size_px``
    (G1: packing 간격은 texture_size 에 대한 margin_px 를 충족해야 하고 둘 다 profile 에 기록)."""
    floor = float(profile.margin_px) / float(max(1, profile.texture_size_px))
    return max(float(margin or 0.0), floor)


#: The correctness checks a pure RE-PACK (never a cut) can repair: island-to-island
#: spacing and tile-border padding are both placement problems (G9/G11).
_REPACK_ONLY_CHECKS = refinement_loop._REPACK_ONLY_CHECKS


def _island_gap_only_failure(correctness: dict) -> bool:
    """True when the failing correctness checks are ONLY the placement ones.

    Thin wrapper over :func:`chart_uv_agent.refinement_loop.gap_only_failure`, which owns
    the policy so merge-back can apply exactly the same rule."""
    return refinement_loop.gap_only_failure(correctness)


def _gap_repack_pass(obj, mesh, final_seams, *, profile, regions, pack_margin: float,
                     history: list) -> None:
    """G1/G5: if the shipped layout fails ONLY the placement checks, re-pack with a
    progressively larger margin (≤3 attempts). The seam set is NEVER touched — a packing
    gap is not a reason to cut. Delegates to
    :func:`chart_uv_agent.refinement_loop.repack_for_gap`."""
    refinement_loop.repack_for_gap(obj, mesh, final_seams, profile=profile,
                                   pack_margin=pack_margin, regions=regions,
                                   stage="final", history=history)


def _charts(mesh: MeshGraph, seams: set[int]) -> tuple[list[list[int]], dict[int, int]]:
    charts = flood_charts(mesh, seams)
    face_chart = {f: cid for cid, fs in enumerate(charts) for f in fs}
    return charts, face_chart


def _split_flipped_charts(mesh, seams, face_chart, charts, flipped, forbidden=frozenset(),
                          rejected=None) -> set[int]:
    """Split every chart that contains a flipped face (U2.2).

    The split runs under the shared protected-edge law (G4): a cut that would traverse a
    protected edge is REJECTED whole, and the offending edge ids are appended to
    ``rejected`` (never silently filtered)."""
    new: set[int] = set()
    for cid in sorted({face_chart[f] for f in flipped if f in face_chart}):
        _, _, ns, rej = constrained_split_chart(mesh, charts[cid], seams, forbidden)
        new.update(ns)
        if rej and rejected is not None:
            rejected.extend(rej)
    return new


def _worst_stretch_chart(mesh, charts, face_stretch):
    return max(charts, key=lambda fs: _chart_distortion(mesh, fs, face_stretch))


def _chart_distortion(mesh, face_ids, face_stretch) -> float:
    """Area-weighted mean per-face stretch over a chart — the checker/stretch distortion
    of one island (MINIMAL_DISTORTION_UV_PLAN §M1). Area-weighting (not a raw sum) makes
    the worst-island pick scale-invariant, so a big low-distortion chart never out-ranks a
    small badly-stretched one."""
    area = sum(mesh.faces[f].area_3d for f in face_ids)
    if area <= 1e-12:
        return 0.0
    return sum(face_stretch[f] * mesh.faces[f].area_3d for f in face_ids) / area


def _distortion_report(mesh, uvmap, charts) -> dict:
    """Checker-distortion summary for the report (MINIMAL_DISTORTION_UV_PLAN §M1). Aliases
    the existing area-stretch metric under checker-distortion names so a reviewer can tie
    the number to the visual checker render. ``charts`` is a list of face-id lists."""
    fstr = per_face_stretch(mesh, uvmap)
    worst_id, worst_val, worst_n = -1, 0.0, 0
    for cid, fs in enumerate(charts):
        d = _chart_distortion(mesh, fs, fstr)
        if d > worst_val:
            worst_id, worst_val, worst_n = cid, d, len(fs)
    # Global checker distortion: same area-weighted mean over all faces (== stretch_score
    # family) so the headline number and the per-island worst share one scale.
    glob = _chart_distortion(mesh, range(len(mesh.faces)), fstr)
    return {"checker_distortion_score": round(float(glob), 6),
            "worst_island_distortion": round(float(worst_val), 6),
            "worst_island_id": worst_id, "worst_face_count": worst_n}


def _audit_metrics(mesh, seams) -> dict:
    """R2 seam-SET audit metrics for the gate (MINIMAL_DISTORTION_UV_PLAN §M2)."""
    from chart_uv_agent.segmentation import mandatory_seam_audit
    a = mandatory_seam_audit(mesh, set(seams), fold_angle=90.0)
    return {"mandatory_90_edges": a["mandatory_90_edges"],
            "mandatory_90_missing": a["mandatory_90_missing"]}


def _uv_audit_metrics(mesh, uvmap) -> dict:
    """R2 UV-LEVEL audit metrics — measured on the actual UVMap, not the seam set, so a
    fold that welds in the exported UV is caught (MINIMAL_DISTORTION_UV_PLAN §M2)."""
    from uv_agent.geometry.evaluation import mandatory_seam_uv_audit
    a = mandatory_seam_uv_audit(mesh, uvmap, fold_angle=90.0)
    return {"mandatory_90_fold_edges": a["mandatory_90_fold_edges"],
            "mandatory_90_uv_unsplit": a["mandatory_90_uv_unsplit"],
            "uv_unsplit_edge_ids": a["uv_unsplit_edge_ids"]}


def run_chart_uv(obj, mesh: MeshGraph, *, config: ChartGateConfig | None = None,
                 cone_limit: float = 150.0, max_rounds: int = 24,
                 margin: float | None = 0.005,
                 shape_passes: bool = False, forbidden_edges=None, region_policy=None,
                 prune_auxiliary: bool = True, min_improvement_ratio: float = 0.15,
                 user_seam_spec=None, auto_refine_user_seams: bool = False,
                 repair_user_seams: bool = True, enforce_user_mandatory: bool = True,
                 gate_user_mandatory: bool = True, optimize_layout: bool = False,
                 layout_optimization_config=None, constraints=None, quality_profile=None,
                 budget=None, locked_seam_edges=None, preferred_edges=None,
                 front_axis: str = "", regions=None, texture_size_px=None, margin_px=None,
                 seed=None, use_refinement_loop: bool = True,
                 shading_snapshot_before: dict | None = None,
                 merge_back: bool | None = None) -> dict:
    """Run U1→U4 on ``obj`` (in Blender). Returns the gate, metrics, seam set, chart
    count, and per-round history; leaves the object holding the best layout.

    ``forbidden_edges`` (a set of mesh edge ids the user wants PRESERVED, e.g. a Blender
    selection) are never traversed by the fold-repair cut paths and are stripped from the
    shipped seam set. ``prune_auxiliary`` (default on) runs a post-pass that removes
    low-angle auxiliary fold-repair seams whose removal keeps every hard gate green.

    Minimal-distortion mode (MINIMAL_DISTORTION_UV_PLAN, default): start from the fewest
    charts compatible with mandatory 90° seams + disk topology, then split ONE worst
    island per round only while overlap, the GLOBAL checker distortion, OR the WORST
    single island's distortion exceeds the gate (so a layout that passes the global mean
    but hides one badly-stretched island is still split). One split per round, hence
    ``max_rounds`` is generous (the safety cap ``island_count_max`` bounds the total).
    Island count is never raised for chart-shape aesthetics. The convexity-driven shape
    repair / tail round are OFF by default (``shape_passes=False``) — they split charts to
    chase convexity, which the plan forbids; convexity is now an advisory report only.
    Set ``shape_passes=True`` to restore the legacy U1.6/U1.7 shape rounds.

    Distortion-split accept/revert (RULE_BASED_UV_SEAM_CORE_PLAN §5.2): a checker-distortion
    split is PROVISIONAL — the next round measures the split region's new distortion and the
    seam is KEPT only if it cut that region's distortion by ≥ ``min_improvement_ratio`` (or
    the layout now passes the gate). A split that doesn't pay for its extra island is reverted
    and that region is not retried, so the island count never rises for a negligible gain.

    Important Region Policy (IMPORTANT_REGION_UV_POLICY_PLAN, optional, off when
    ``region_policy is None`` — identical baseline behaviour): an
    :class:`artist_uv_agent.region_policy.RegionPolicy` protecting artist-important regions
    (face front, hands, logo). Precedence is mandatory 90° > region protection > distortion
    split: a distortion split whose new seam edges would cut a protected *smooth* (<90°) edge
    is REJECTED before it is committed (§5.4 post-split reject — ``split_chart`` is NOT
    rewritten); that island is then left alone, and the reject is recorded in the history and
    the ``seam_report`` ``regions`` block. Mandatory ≥90° folds are never protected, so they
    still ship.

    Merge-back (G7) and the shading policy (G10) close the automatic path. After the
    refinement loop and the prune pass settle, :func:`chart_uv_agent.merge_back.run_merge_back`
    proves that no remaining seam can be dissolved without losing quality (pass
    ``merge_back=False`` to switch the stage off; ``None`` follows
    ``profile.merge_back_enabled``). The profile's ``shading_uv_policy`` then decides what
    happens to the shading state: ``split_normals_on_uv_seams`` marks the final seam set
    sharp, ``require_uv_seam_on_sharp_edges`` forces every sharp edge into the seam set as
    a ``required`` constraint, and ``preserve`` demands the before/after shading snapshots
    compare unchanged. ``shading_snapshot_before`` lets the caller (the worker) supply the
    pre-run snapshot; when it is omitted the pipeline takes its own before touching
    anything."""
    from chart_uv_agent.unwrap import (
        flipped_faces, island_plan_from_seams, read_uvmap, repack, unwrap_and_pack,
    )

    from chart_uv_agent.segmentation import (
        flood_charts, mandatory_seam_edges, split_welded_folds,
    )
    from chart_uv_agent.shape import measure_charts
    from chart_uv_agent.shape_repair import repair_shapes, tail_round

    from chart_uv_agent import refinement_loop
    from chart_uv_agent.constraints import SeamConstraints
    from chart_uv_agent.merge_back import merge_back_disabled_block, run_merge_back
    from uv_agent.blender.apply import apply_smoothing_split_by_edges
    from uv_agent.geometry.shading_policy import (
        evaluate_shading_policy, required_seam_edges_for_policy, shading_snapshot,
    )

    config = config or ChartGateConfig()
    # G10: the BEFORE shading snapshot has to predate every mutation of this run, so it is
    # taken here (unless the caller already supplied one).
    def _has_shading_data(o) -> bool:
        data = getattr(o, "data", None)
        return hasattr(data, "edges") and hasattr(data, "polygons")

    shading_before = shading_snapshot_before
    if shading_before is None and _has_shading_data(obj):
        shading_before = shading_snapshot(obj.data)
    started_at = time.monotonic()
    profile = _resolve_profile(quality_profile, texture_size_px=texture_size_px,
                               margin_px=margin_px)
    # G1: every pack in this run uses a margin that satisfies the profile's margin_px @
    # texture_size_px (the caller's ``margin`` is only a floor, never a ceiling).
    pack_margin = _pack_margin_for(profile, margin)
    budget = _resolve_budget(profile, budget, seed=seed)
    candidate_history: list[dict] = []
    termination_records: list[dict] = []

    # USER-GUIDED SEAM mode (USER_GUIDED_SEAM_UV_PIPELINE_PLAN §8.2). When the caller supplies a
    # user seam spec, the user's seams are the authoritative source of truth: the auto chart
    # solver is bypassed entirely (so the no-spec path below is byte-for-byte unchanged — plan
    # §12 success criterion #1) and a dedicated report-only path runs instead.
    if user_seam_spec is not None:
        return _run_user_seam_uv(obj, mesh, user_seam_spec, config=config,
                                 base_forbidden=set(forbidden_edges or ()), margin=pack_margin,
                                 auto_refine=auto_refine_user_seams,
                                 repair_user_seams=repair_user_seams,
                                 enforce_mandatory=enforce_user_mandatory,
                                 gate_mandatory=gate_user_mandatory,
                                 optimize_layout=optimize_layout,
                                 layout_optimization_config=layout_optimization_config,
                                 constraints=constraints, quality_profile=profile,
                                 budget=budget, regions=regions, front_axis=front_axis,
                                 preferred_edges=preferred_edges,
                                 texture_size_px=texture_size_px, margin_px=margin_px,
                                 started_at=started_at)

    # G4: ONE constraint object governs every cut path of this run (initial segmentation,
    # distortion refinement, overlap repair, welded-fold repair, prune).
    # G10: the shading policy may FORCE edges into the seam set (every sharp edge must be a
    # UV boundary under ``require_uv_seam_on_sharp_edges``). They enter the one constraint
    # object as ``required`` so every cut/prune/merge stage honours them by construction.
    shading_policy = str(profile.shading_uv_policy)
    required = required_seam_edges_for_policy(shading_policy, mesh)
    if constraints is None:
        constraints = SeamConstraints.build(
            mesh, locked=locked_seam_edges or (), protected=forbidden_edges or (),
            preferred=preferred_edges or (), region_policy=region_policy,
            front_axis=front_axis, required=required)
    forbidden = set(constraints.forbidden)

    aux_seams: set[int] = set()       # welded-fold-auxiliary seams (prune candidates)
    overlap_seams: set[int] = set()   # added by flip / raster overlap repair
    distortion_seams: set[int] = set()  # added by the checker-distortion split
    seg = segment(mesh, cone_limit=cone_limit, max_charts=config.island_count_max,
                  locked_seams=constraints.locked, forbidden=forbidden)
    seams = set(seg.seams)
    # Region-aware FACE RECOVERY (REGION_AWARE_FACE_UV_RECOVERY_PLAN §6.3): the FRONT-stage
    # lever — dissolve face_front_core interior smooth boundaries the segmentation created, so
    # the face front stays a coherent island (mandatory folds + disk + a bounded normal cone
    # keep hard correctness and stop the v1 distortion blow-up). Off unless a face_recovery
    # region policy is supplied; the experimental post-split reject path is NOT used here.
    region_merge = {"removed": [], "merges": 0, "history": []}
    region_mode = getattr(region_policy, "mode", None) if region_policy is not None else None
    if region_policy is not None and region_mode == "face_recovery":
        from artist_uv_agent.region_policy import region_protected_merge
        region_merge = region_protected_merge(mesh, seams, region_policy, fold_angle=90.0)
    # M3 minimal segmentation: the convexity-driven shape repair / tail round inflate the
    # island count for shape (forbidden by the plan), so they are OFF by default. When
    # enabled (legacy), repair runs before U2 and the tail round proves worst-decile charts.
    if shape_passes:
        repair = repair_shapes(mesh, seams, convexity_min=0.92, max_charts=config.island_count_max)
        tail = tail_round(mesh, seams, convexity_bar=config.convexity_p10_min,
                          max_charts=config.island_count_max)
    else:
        repair = {"history": []}
        tail = {"history": [], "stuck": []}
    stuck_charts = tail["stuck"]
    mandatory = mandatory_seam_edges(mesh, fold_angle=90.0)
    history: list[dict] = [{"stage": "segment", **seg.history[-1]},
                           {"stage": "region_protected_merge", "action": "region_protected_merge",
                            "enabled": region_mode == "face_recovery",
                            "merges": region_merge["merges"],
                            "removed_core_smooth_edges": len(region_merge["removed"]),
                            "rounds": region_merge["history"]},
                           {"stage": "shape_repair", "enabled": shape_passes, "rounds": repair["history"]},
                           {"stage": "tail_round", "enabled": shape_passes,
                            "rounds": tail["history"], "stuck": tail["stuck"]}]
    best = None
    # CG14/CG0: the layout recipe — every accepted R1 re-unwrap is recorded here and
    # replayed after every later ``unwrap_and_pack``, so a UV-only repair survives the
    # prune / merge-back / post-prune stages instead of being silently discarded.
    unwrap_overrides: list = []
    repacked = False
    raster_margin_bumped = False
    bbox_packed = False
    pending_split = None              # provisional distortion split awaiting an accept/revert
    rejected_regions: set[frozenset] = set()  # face sets a reverted split proved not worth cutting
    rounds_exhausted = False          # the loop used every allowed round (G5 termination)

    for rnd in range(max_rounds):
        unwrap_and_pack(obj, seams, margin=pack_margin)
        refinement_loop.apply_unwrap_overrides(obj, mesh, seams, unwrap_overrides,
                                               margin=pack_margin)
        uvmap = read_uvmap(obj, mesh)
        plan = island_plan_from_seams(mesh, seams)
        ev = evaluate_uv_solution(mesh, plan, uvmap)
        charts, face_chart = _charts(mesh, seams)
        metrics = _chart_metrics(mesh, uvmap, ev)
        metrics["small_island_ratio"] = relative_small_island_ratio(mesh, plan, uvmap)
        metrics.update(_shape_metrics(mesh, seams, mandatory))
        metrics.update(_audit_metrics(mesh, seams))            # R2: mandatory_90_missing (set)
        metrics.update(_uv_audit_metrics(mesh, uvmap))         # R2: mandatory_90_uv_unsplit (UV)
        metrics.update(_distortion_report(mesh, uvmap, charts))  # checker distortion names
        # TRUE raster overlap (correctness round) + per-chart attribution for repair.
        diag = raster_overlap_diagnosis(mesh, uvmap, face_chart)
        metrics["raster_overlap_ratio"] = diag["raster_overlap_ratio"]
        gate = evaluate_chart_gate(metrics, config=config)
        rec = {"round": rnd, "charts": ev.island_count, "stretch": round(ev.stretch_score, 4),
               "checker_distortion": metrics["checker_distortion_score"],
               "worst_island": metrics["worst_island_id"],
               "worst_island_distortion": metrics["worst_island_distortion"],
               "overlap": round(ev.overlap_ratio, 5), "raster": diag["raster_overlap_ratio"],
               "self": diag["self_overlap_ratio"], "cross": diag["cross_overlap_ratio"],
               "packing": round(ev.packing_efficiency, 4),
               "mandatory_90_missing": metrics["mandatory_90_missing"],
               "mandatory_90_uv_unsplit": metrics["mandatory_90_uv_unsplit"],
               "verdict": gate.verdict, "fails": [c.name for c in gate.failures],
               "action": "stop", "reason": ""}
        history.append(rec)

        # Resolve last round's PROVISIONAL distortion split (§5.2 accept/revert). Measure the
        # split region's distortion now; keep the seam only if it improved enough OR the gate
        # now passes, else revert it and never retry that region. Done BEFORE the best-update
        # so a reverted (kept-out) seam set is what competes to be ``best``.
        if pending_split is not None:
            from artist_uv_agent.seam_refinement import improvement_ratio
            after = _chart_distortion(mesh, pending_split["faces"], per_face_stretch(mesh, uvmap))
            imp = improvement_ratio(pending_split["before"], after)
            psrec = pending_split["rec"]
            psrec["distortion_after"] = round(float(after), 6)
            psrec["improvement_ratio"] = round(float(imp), 4)
            if imp >= min_improvement_ratio or gate.passed:
                psrec["accepted"] = True
                pending_split = None
            else:
                seams -= pending_split["added"]
                distortion_seams -= pending_split["added"]
                rejected_regions.add(pending_split["faces"])
                psrec["accepted"] = False
                psrec["reverted"] = True
                rec["action"] = "revert"
                rec["reason"] = "distortion_split_reverted"
                rec["reverted_improvement_ratio"] = round(float(imp), 4)
                pending_split = None
                continue   # re-measure the reverted seam set next round

        if best is None or _better(metrics, gate, best):
            best = {"seams": set(seams), "metrics": metrics, "gate": gate, "ev": ev}

        # v2 measurement of the layout this round already holds (G4/G5): the v1 gate alone
        # cannot see a quality-profile / correctness / mandatory failure, so a run that fails
        # the profile must still enter the refinement loop. ``measure_layout`` never unwraps;
        # it is resolved lazily so a round that needs no v2 answer pays nothing.
        v2_cache: dict = {}

        def _round_v2(_cache=v2_cache, _seams=seams):
            if "m" not in _cache:
                _cache["m"] = refinement_loop.measure_layout(
                    obj, mesh, _seams, profile=profile, stage="round", regions=regions)
            return _cache["m"]

        if gate.passed:
            if not use_refinement_loop or _round_v2()["passed"]:
                rec["reason"] = "all hard gates pass (distortion/overlap/bounds/seams)"
                break
            rec["v2_passed"] = False
            rec["reason"] = "v1 gates pass but the v2 quality profile does not"

        changed = False

        # (0) R2 first: a fold welded in the ACTUAL UV → make it a chart boundary with a
        # LOCAL minimum-cost cut (routes along creases, avoids low-angle / forbidden edges)
        # rather than a broad chart-wide split. Targeted at the welded folds only.
        if metrics["mandatory_90_uv_unsplit"] > 0:
            r = split_welded_folds(mesh, seams, metrics["uv_unsplit_edge_ids"],
                                   forbidden=forbidden, region_policy=region_policy)
            if r["added"] or r["local_cuts"] or r["fallback"]:
                aux_seams |= r["added"]
                changed = True
                rec["action"] = "split"; rec["reason"] = "welded_fold_auxiliary"
                rec["fold_local_cuts"] = r["local_cuts"]; rec["fold_fallback"] = r["fallback"]
            if r["blocked"]:
                # A protected region with no legal cut path — recorded, never swallowed (G4).
                rec["fold_blocked"] = r["blocked"]
                rec["fold_blocked_edge_ids"] = sorted(r["blocked_edge_ids"])
                history.append({"round": rnd, "stage": "welded_fold", "action": "reject",
                                "reason": "protected_region_reject",
                                "protected_edges_cut": [],
                                "blocked_edge_ids": sorted(r["blocked_edge_ids"])})

        # (1) Flipped UV faces → re-split the folding charts (overlap correctness).
        flips = flipped_faces(mesh, uvmap)
        if not changed and flips:
            rejected_edges: list[int] = []
            ns = _split_flipped_charts(mesh, seams, face_chart, charts, flips, forbidden,
                                       rejected_edges)
            if rejected_edges:
                history.append({"round": rnd, "stage": "flipped_faces", "action": "reject",
                                "reason": "protected_region_reject",
                                "protected_edges_cut": sorted(set(rejected_edges))})
            if ns:
                seams.update(ns); overlap_seams |= set(ns); changed = True
                rec["action"] = "split"; rec["reason"] = "flipped_faces"

        # (2) Raster-overlap repair: (b) inter-chart invasion → margin bump then AABB
        # re-pack (no split); (a) self-intersection → split the folding charts.
        if not changed and metrics["raster_overlap_ratio"] > config.raster_overlap_max:
            if diag["cross_charts"] and not bbox_packed:
                if not raster_margin_bumped:
                    repack(obj, margin=min(0.05, pack_margin * 4)); raster_margin_bumped = True
                else:
                    repack(obj, margin=pack_margin, pack_shape="AABB"); bbox_packed = True
                changed = True
                rec["action"] = "repack"; rec["reason"] = "raster_overlap_cross_invasion"
            else:
                cap = config.island_count_max
                for cid in diag["self_charts"]:
                    if cid < len(charts) and len(charts[cid]) >= 2 * 5 \
                            and len(_charts(mesh, seams)[0]) < cap:
                        _, _, ns, rej = constrained_split_chart(mesh, charts[cid], seams,
                                                                forbidden)
                        if rej:
                            history.append({"round": rnd, "stage": "raster_overlap_self",
                                            "action": "reject",
                                            "reason": "protected_region_reject",
                                            "protected_edges_cut": sorted(rej)})
                        if ns:
                            seams.update(ns); overlap_seams |= set(ns); changed = True
                if changed:
                    rec["action"] = "split"; rec["reason"] = "raster_overlap_self"

        # (3) Checker/stretch distortion over threshold → split exactly the ONE worst
        # island (MINIMAL_DISTORTION_UV_PLAN §M4: one split, one recorded reason). The
        # trigger is GLOBAL mean stretch OR the WORST single island — a layout whose global
        # mean passes but whose worst island is badly stretched still splits that island.
        global_over = metrics["stretch_score"] > config.stretch_max
        worst_over = metrics["worst_island_distortion"] > config.worst_island_distortion_max
        # Don't open a PROVISIONAL split on the last allowed round — there'd be no next round
        # to measure its improvement, so it could neither be accepted nor reverted (§5.2).
        # REFINEMENT LOOP (UV_AUTOMATION §5, G4/G5): one refinement round = one island,
        # candidates proposed/measured/accepted-or-fully-restored by the shared decision
        # core, so the no-spec path and the user-assisted path obey the same rules. The
        # legacy in-place split + provisional accept/revert is kept for regression runs
        # (``use_refinement_loop=False``).
        # The v2 trigger is OR-ed with the v1 one: a v2 quality / correctness / mandatory
        # failure is a refinement target too, so a run that fails the frozen profile can
        # never terminate with "no candidate was ever evaluated" (G4/G5).
        v2_needed = False
        # CG5: a CATASTROPHIC failure must still be repaired when the island cap is already
        # reached — the R1 re-unwrap adds no seam, so the cap cannot forbid it. Resolved off
        # the already-cached round measurement, only while ``v2_needed`` is evaluated.
        cat_failed = False
        if use_refinement_loop and not changed:
            v2_needed = not _round_v2()["passed"]
            rec["v2_passed"] = not v2_needed
            cat_failed = bool(((_round_v2().get("catastrophic") or {}).get("passed") is False))
        # The cap that actually applies is the STRICTER of the gate cap and the refinement
        # budget's ``island_cap`` — refining past the budget cap would produce islands the
        # refinement loop itself would refuse (G5).
        refine_island_cap = min(int(config.island_count_max), int(budget["island_cap"]))
        if use_refinement_loop and not changed and (global_over or worst_over or v2_needed) \
                and (len(charts) < refine_island_cap or cat_failed):
            before_seams = set(seams)
            if len(charts) >= refine_island_cap:
                # Entered ONLY because of the catastrophic failure (CG5). ``run_refinement``
                # itself blocks the seam-adding candidates at the cap, so the same budget is
                # passed through unchanged.
                rec["refinement_at_cap"] = True
            ref = refinement_loop.run_refinement(
                obj, mesh, seams, constraints=constraints, profile=profile,
                budget={**budget, "max_iterations": 1}, margin=pack_margin, regions=regions,
                history=history, candidate_history=candidate_history,
                initial_measurement=_round_v2(), overrides=unwrap_overrides)
            termination_records.append(ref["termination"])
            seams = set(ref["seams"])
            if seams != before_seams:
                distortion_seams |= set(ref["distortion_seams"])
                changed = True
                rec["action"] = "split"
                rec["reason"] = "refinement_loop"
                rec["refinement_added_edges"] = sorted(seams - before_seams)
                rec["refinement_reason"] = ref["termination"]["reason"]
            else:
                rec["refinement_reason"] = ref["termination"]["reason"]
                # CG5: an accepted R1 re-unwrap repairs the LAYOUT without touching a single
                # seam, so a seam-set comparison alone reads it as "nothing happened" and the
                # round loop stops one repair too early. The UV digest is what actually moved,
                # so it is what decides whether this round changed anything.
                if str((ref.get("measurement") or {}).get("uv_hash", "")) \
                        != str(_round_v2().get("uv_hash", "")):
                    changed = True
                    rec["action"] = "reunwrap"
                    rec["reason"] = _CATASTROPHIC_CUT_REASON
        elif use_refinement_loop and not changed and (global_over or worst_over or v2_needed) \
                and len(charts) >= refine_island_cap and not cat_failed:
            # Refinement was skipped because the island cap is already reached — recorded so
            # the round history says WHY nothing was refined (G5).
            if not rec.get("reason"):
                rec["reason"] = "island_cap_reached"

        if not use_refinement_loop and not changed and (global_over or worst_over) \
                and len(charts) < config.island_count_max and rnd < max_rounds - 1:
            fstr = per_face_stretch(mesh, uvmap)
            # Pick the worst chart NOT already proved not-worth-splitting (a reverted region).
            ranked = sorted(((cid, fs) for cid, fs in enumerate(charts)
                             if frozenset(fs) not in rejected_regions),
                            key=lambda cf: _chart_distortion(mesh, cf[1], fstr), reverse=True)
            worst_available = _chart_distortion(mesh, ranked[0][1], fstr) if ranked else 0.0
            # When the ONLY thing over the bar is a worst island we can no longer split (it is
            # protected/rejected) and the best AVAILABLE island is already under the per-island
            # threshold, splitting it cannot help — stop honestly rather than inflate the island
            # count on already-fine islands (IMPORTANT_REGION_UV_POLICY_PLAN §11.3; also fixes a
            # latent thrash after a §5.2 revert). No effect on a clean run: there ranked[0] IS
            # the global worst, so ``worst_available`` exceeds the threshold and the split fires.
            if ranked and not global_over and worst_available <= config.worst_island_distortion_max:
                ranked = []
                rec["reason"] = "worst_island_unsplittable_best_effort"
            if ranked:
                worst_cid, worst = ranked[0]
                before = _chart_distortion(mesh, worst, fstr)
                _, _, ns = split_chart(mesh, worst, seams)
                # EXPERIMENTAL post-split reject (mode="post_split_reject" only, off by default —
                # REGION_AWARE_FACE_UV_RECOVERY_PLAN §2.1: the v1 approach did NOT reduce face
                # seams). Reject a split that would cut a protected smooth edge; the island is
                # marked rejected and the next round re-measures the unchanged seam set. The
                # default face_recovery mode handles the face up front (region merge) instead.
                protected_cut = (region_policy.protected_cut(ns)
                                 if (ns and region_policy and region_mode == "post_split_reject")
                                 else set())
                if protected_cut:
                    rejected_regions.add(frozenset(worst))
                    rec["action"] = "reject"
                    rec["reason"] = "protected_region_reject"
                    rec["region"] = (region_policy.region_names_for(protected_cut) or [None])[0]
                    rec["protected_edges_cut"] = sorted(protected_cut)
                    rec["split_island"] = worst_cid
                    changed = True   # re-measure the (unchanged) seam set next round
                elif ns:
                    seams.update(ns); distortion_seams |= set(ns); changed = True
                    rec["action"] = "split"
                    rec["reason"] = "checker_distortion" if global_over else "worst_island_distortion"
                    rec["split_island"] = worst_cid
                    rec["before_distortion"] = round(float(before), 6)
                    rec["added_edges"] = sorted(ns)
                    # Provisional — next round measures the region and accepts/reverts (§5.2).
                    pending_split = {"faces": frozenset(worst), "before": before,
                                     "added": set(ns), "rec": rec}

        # (4) Packing is ADVISORY — never split for it. A single tighter-margin re-pack is
        # the only packing action (more, smaller charts pack *worse*, so splitting is
        # counterproductive). Only reached when a hard gate other than packing still fails.
        if not changed and metrics["packing_efficiency"] < config.packing_min and not repacked:
            repack(obj, margin=pack_margin * 0.5)
            repacked = True
            changed = True  # re-measure next round without changing seams
            rec["action"] = "repack"; rec["reason"] = "packing_retune"

        if not changed:
            rec["reason"] = "no further refinement available (best-effort)"
            break
    else:
        rounds_exhausted = True

    # A distortion split that never got a measurement round (loop exhausted) is left honestly
    # UNRESOLVED — its improvement is unproved, so the report excludes it (the guard above
    # normally prevents this; this is belt-and-suspenders). The shipped seam set is still the
    # ``best`` pick, re-measured by the correctness/measure passes below.
    if pending_split is not None:
        psrec = pending_split["rec"]
        psrec["accepted"] = False
        psrec["unresolved"] = True
        psrec["note"] = "distortion_split_unresolved (no round left to measure improvement)"
        pending_split = None

    # Correctness round: eliminate TRUE (raster) overlap. Owns the final UV; may raise the
    # chart count / stretch (CONFORMAL) — reported as regression. Pre-correctness metrics
    # are captured for the before/after table.
    final_seams = set(best["seams"])
    pre = dict(best["metrics"])
    correctness = correctness_pass(obj, mesh, final_seams, config, margin=pack_margin,
                                   forbidden=forbidden, reject_history=history)
    final_seams |= mandatory
    # G10: a policy-required seam ships exactly like a mandatory fold.
    final_seams |= set(constraints.required)

    def measure():
        """Unwrap+pack the current ``final_seams`` and return (metrics, gate, ev). Owns the
        object's shipped UV — every seam edit here is followed by exactly one re-unwrap."""
        unwrap_and_pack(obj, final_seams, margin=pack_margin)
        refinement_loop.apply_unwrap_overrides(obj, mesh, final_seams, unwrap_overrides,
                                               margin=pack_margin)
        uvm = read_uvmap(obj, mesh)
        pl = island_plan_from_seams(mesh, final_seams)
        e = evaluate_uv_solution(mesh, pl, uvm)
        chs, _fc = _charts(mesh, final_seams)
        m = _chart_metrics(mesh, uvm, e)
        m["small_island_ratio"] = relative_small_island_ratio(mesh, pl, uvm)
        m.update(_shape_metrics(mesh, final_seams, mandatory))
        m.update(_audit_metrics(mesh, final_seams))
        m.update(_uv_audit_metrics(mesh, uvm))            # UV-level R2 on the SHIPPED uvmap
        m.update(_distortion_report(mesh, uvm, chs))
        fcr = {f: i for i, fs in enumerate(flood_charts(mesh, final_seams)) for f in fs}
        m["raster_overlap_ratio"] = raster_overlap_diagnosis(mesh, uvm, fcr)["raster_overlap_ratio"]
        return m, evaluate_chart_gate(m, config=config), e

    # R2 (§M2): cut every welded fold with a LOCAL min-cost cut, re-unwrap, until none weld.
    metrics, gate, ev = measure()
    for _ in range(8):
        if metrics["mandatory_90_uv_unsplit"] == 0:
            break
        r = split_welded_folds(mesh, final_seams, metrics["uv_unsplit_edge_ids"],
                               forbidden=forbidden, region_policy=region_policy)
        if not (r["added"] or r["local_cuts"] or r["fallback"]):
            break  # cannot cut further — surfaced honestly by the hard gate
        aux_seams |= r["added"]
        metrics, gate, ev = measure()

    # User preserve: a non-mandatory forbidden edge must never ship as a seam. The cut paths
    # already avoid them; strip any that an earlier (VSA fallback / segmentation) pass placed.
    strip = {e for e in forbidden if e in final_seams and mesh.edges[e].dihedral_angle < 90.0}
    if strip:
        final_seams -= strip
        aux_seams -= strip
        metrics, gate, ev = measure()

    # Pruning: drop low-angle welded-fold-auxiliary seams whose removal keeps EVERY hard gate
    # green (fewer needless cuts, lower vt/v). One at a time, flattest first; revert on fail.
    pruned: list[int] = []
    if prune_auxiliary and gate.passed:
        # A user-LOCKED or policy-REQUIRED seam is never a prune candidate
        # (G4: 사용자 seam lock 제거 0 / G10: shading-required seam 제거 0).
        cand = sorted((e for e in aux_seams if e in final_seams
                       and e not in constraints.never_removed
                       and mesh.edges[e].dihedral_angle < 90.0),
                      key=lambda e: mesh.edges[e].dihedral_angle)[:_PRUNE_CAP]
        pruned = _prune_seams(final_seams, cand, lambda: measure()[1].passed)
        aux_seams -= set(pruned)
        metrics, gate, ev = measure()     # ship the kept seam set

    # ------------------------------------------ post-prune catastrophic pass (CG6/CG13)
    # The round loop can be forced to stop at the island cap while a catastrophic region is
    # still broken; the prune pass then GIVES islands back (the statue case: 80 islands at
    # the cap → 37 after pruning). So once the seam set has settled, re-measure and, if the
    # hard catastrophic gate is still red, re-enter the repair loop with the headroom that
    # pruning just bought — instead of shipping the broken regions untouched.
    if use_refinement_loop:
        post_prune = refinement_loop.measure_layout(obj, mesh, final_seams, profile=profile,
                                                    stage="post_prune", regions=regions)
        if (post_prune.get("catastrophic") or {}).get("passed") is False:
            ref2 = refinement_loop.run_refinement(
                obj, mesh, final_seams, constraints=constraints, profile=profile,
                budget={**budget,
                        "max_iterations": int(profile.catastrophic_repair_max_rounds)},
                margin=pack_margin, regions=regions, history=history,
                candidate_history=candidate_history, initial_measurement=post_prune,
                overrides=unwrap_overrides)
            termination_records.append(ref2["termination"])
            final_seams = set(ref2["seams"])
            distortion_seams |= set(ref2["distortion_seams"])
            history.append({
                "stage": "post_prune_catastrophic",
                "rounds": int(ref2["termination"]["iterations"]),
                "reason": str(ref2["termination"]["reason"]),
                "bad_triangles_before": int(((post_prune.get("catastrophic") or {})
                                             .get("bad_triangle_count", 0)) or 0),
                "bad_triangles_after": int((((ref2.get("measurement") or {})
                                             .get("catastrophic") or {})
                                            .get("bad_triangle_count", 0)) or 0),
            })

    # ----------------------------------------------------------- merge-back (G7)
    # The refinement loop only ever ADDS cuts, so before the layout ships the run must
    # prove that no remaining seam can be dissolved without losing quality — or record
    # that the budget ran out. mandatory / user-locked / shading-required seams are never
    # even trialled (G4/G10).
    merge_back_enabled = (bool(profile.merge_back_enabled) if merge_back is None
                          else bool(merge_back))
    if merge_back_enabled:
        # G9/G11 before G7: a layout that fails ONLY on packing gaps is re-packed wider
        # first, so merge-back judges the layout it can actually ship instead of skipping
        # itself with ``skipped_quality_failed`` over a placement defect.
        mb_measure = refinement_loop.repack_for_gap(
            obj, mesh, final_seams, profile=profile, pack_margin=pack_margin,
            regions=regions, history=history)
        mb = run_merge_back(
            obj, mesh, final_seams, constraints=constraints, profile=profile,
            margin=pack_margin, regions=regions, required=constraints.required,
            history=history, initial_measurement=mb_measure, repair_mode=True,
            overrides=unwrap_overrides,
            time_budget_s=max(0.0, float(budget["time_budget_s"])
                              - (time.monotonic() - started_at)))
        mb_removed = {int(e) for e in mb["removed_edges"]}
        final_seams = set(mb["seams"])
        aux_seams -= mb_removed
        overlap_seams -= mb_removed
        distortion_seams -= mb_removed
        # One re-unwrap of the SHIPPED seam set so the v1 metrics describe the merged
        # layout — ONLY when merge-back actually changed it. When nothing was accepted the
        # seam set is untouched, so a re-unwrap would only cost time and throw away the
        # gap re-pack that was just applied; the v1 metrics stay as they were.
        if int(mb["accepted"]) > 0:
            metrics, gate, ev = measure()
    else:
        mb = merge_back_disabled_block()
        mb_removed = set()
    merge_back_block = _jsonable({k: v for k, v in mb.items()
                                  if k not in ("seams", "measurement")})
    merge_back_block["removed_edges"] = sorted(mb_removed)

    # ------------------------------------------------------- shading policy (G10)
    # The seam set is final from here on. ``split_normals_on_uv_seams`` is the one policy
    # that WRITES shading state (every UV seam becomes a sharp edge); the other two only
    # audit. The audit runs on the shipped UVs, so a seam Blender welds across is caught.
    if shading_policy == "split_normals_on_uv_seams" and hasattr(getattr(obj, "data", None),
                                                                 "edges"):
        apply_smoothing_split_by_edges(obj, sorted(final_seams))
    shading_after = shading_snapshot(obj.data) if _has_shading_data(obj) else None
    shading_block = _jsonable(evaluate_shading_policy(
        shading_policy, mesh=mesh, uvmap=read_uvmap(obj, mesh), seams=final_seams,
        before_snapshot=shading_before, after_snapshot=shading_after))

    seam_types = _classify_seams(mesh, final_seams, aux_seams, overlap_seams,
                                 distortion_seams, forbidden)
    distortion = {k: metrics[k] for k in ("checker_distortion_score", "worst_island_distortion",
                                          "worst_island_id", "worst_face_count")}
    initial_islands = history[0].get("charts", ev.island_count)
    conclusion = _island_conclusion(initial_islands, ev.island_count, history, metrics, config)
    result = {
        "engine": "chart", "seams": sorted(final_seams), "chart_count": ev.island_count,
        "metrics": metrics, "gate": gate, "gate_config": config.to_dict(),
        "history": history, "rounds": len(history) - 1,
        "distortion": distortion, "conclusion": conclusion,
        "mandatory_90_edges": metrics["mandatory_90_edges"],
        "mandatory_90_missing": metrics["mandatory_90_missing"],
        "mandatory_90_fold_edges": metrics["mandatory_90_fold_edges"],
        "mandatory_90_uv_unsplit": metrics["mandatory_90_uv_unsplit"],
        "initial_island_count": initial_islands, "final_island_count": ev.island_count,
        "seam_type_counts": _count_types(seam_types), "pruned_auxiliary": len(pruned),
        "forbidden_edges": sorted(forbidden), "forbidden_stripped": sorted(strip),
        "shape_repair": repair["history"], "tail_round": tail["history"],
        "correctness": correctness["history"],
        "metrics_before_correctness": {k: pre.get(k) for k in
            ("raster_overlap_ratio", "stretch_score", "packing_efficiency",
             "convexity_p10", "island_count")},
        "stuck_charts": stuck_charts, "shippable": shippable_with_stuck(gate, stuck_charts),
        "pack_margin_uv": float(pack_margin), "margin_px": int(profile.margin_px),
        "texture_size_px": int(profile.texture_size_px),
        "merge_back": merge_back_block, "shading": shading_block,
        # CG14/CG0: the reproducible layout recipe the shipped UVs were built with.
        "unwrap_overrides": _jsonable(unwrap_overrides),
    }
    # G1/G5: an island-gap-ONLY correctness failure is repaired by re-packing wider, never
    # by cutting. Runs before the final v2 measurement so the report describes the re-pack.
    _gap_repack_pass(obj, mesh, final_seams, profile=profile, regions=regions,
                     pack_margin=pack_margin, history=history)
    # v2 FINAL measurement of the shipped UV + the G1/G4/G5 evidence blocks. Measured after
    # every seam edit, so it describes exactly the layout the object now holds.
    v2_block, v2_measurement = _v2_result_block(
        obj, mesh, final_seams, profile=profile, regions=regions, constraints=constraints,
        forbidden=forbidden, mandatory=mandatory, distortion_seams=distortion_seams,
        candidate_history=candidate_history, termination_records=termination_records,
        budget=budget, elapsed=time.monotonic() - started_at, gate=gate,
        exhausted_rounds=rounds_exhausted,
        island_cap=min(int(config.island_count_max), int(budget["island_cap"])),
        merge_back=merge_back_block, shading=shading_block, history=history,
        unwrap_overrides=unwrap_overrides)
    result.update(v2_block)
    # G1 (topology/입력): the input-defect diagnosis ships with every automatic result.
    result["input_diagnostics"] = _input_diagnostics(mesh, v2_block.get("distortion_v2"))
    result["correctness_rounds"] = correctness["history"]
    _apply_v2_metrics(metrics, v2_measurement)
    result["seam_types"] = {int(k): v for k, v in seam_types.items()}
    # Important Region Policy report (IMPORTANT_REGION_UV_POLICY_PLAN §5.5) — the mandatory-
    # vs-smooth seam split per protected region + the rejected-split records, built from the
    # SHIPPED seams. Only present when a region policy was supplied (else baseline behaviour).
    if region_policy is not None:
        from artist_uv_agent.region_policy import build_region_report, region_boundary_audit
        result["region_report"] = build_region_report(region_policy, mesh, final_seams, history)
        result["region_audit"] = region_boundary_audit(mesh, final_seams, region_policy)
        result["region_mode"] = region_mode
        result["region_protected_merges"] = region_merge["merges"]

    # Reviewer-facing Seam Decision Core report (RULE_BASED_UV_SEAM_CORE_PLAN §5.3) — pure,
    # built from the result above so the app gets the seam reasons / distortion / conflicts.
    from artist_uv_agent.seam_report import build_seam_report
    result["seam_report"] = build_seam_report(result, source_mesh=getattr(mesh, "object_id", None))
    return result


def _run_user_seam_uv(obj, mesh: MeshGraph, spec, *, config: ChartGateConfig,
                      base_forbidden: set[int], margin: float, auto_refine: bool,
                      repair_user_seams: bool, enforce_mandatory: bool,
                      gate_mandatory: bool, optimize_layout: bool = False,
                      layout_optimization_config=None, constraints=None,
                      quality_profile=None, budget=None, regions=None,
                      front_axis: str = "", preferred_edges=None, texture_size_px=None,
                      margin_px=None, started_at=None) -> dict:
    """User-guided seam UV path (USER_GUIDED_SEAM_UV_PIPELINE_PLAN §8.2). The user's seam spec
    is the source of truth: the initial seam set is ``mandatory_90 ∪ user_seam_edges`` and the
    non-mandatory ``user_protected_edges`` are forwarded as forbidden (never cut). The app does
    NOT add seams of its own — it unwraps/packs the user's seams, then HONESTLY reports
    distortion / overlap / mandatory audits (plan §10: report first, don't auto-fix). The only
    seams the engine may add are mandatory-fold boundary cuts (a ≥90° fold welded in the actual
    UV must become a chart boundary — mandatory wins, plan §7) and, ONLY when
    ``auto_refine=True``, distortion splits — each tracked separately as ``auto_added_seams``.

    Pure-ish: this owns the object's UV (each seam edit is followed by one re-unwrap) but adds
    no auto chart segmentation, so the no-spec ``run_chart_uv`` path is untouched (plan §13)."""
    from chart_uv_agent.unwrap import (
        island_plan_from_seams, read_uvmap, unwrap_and_pack,
    )
    from chart_uv_agent.segmentation import flood_charts, split_welded_folds
    from artist_uv_agent.user_seams import build_user_seam_set
    from chart_uv_agent import refinement_loop
    from chart_uv_agent.constraints import SeamConstraints

    started_at = time.monotonic() if started_at is None else float(started_at)
    profile = _resolve_profile(quality_profile, texture_size_px=texture_size_px,
                               margin_px=margin_px)
    # G1: same rule as the no-spec path — the pack margin must satisfy the profile's
    # margin_px @ texture_size_px; the caller's ``margin`` is only a floor.
    pack_margin = _pack_margin_for(profile, margin)
    budget = _resolve_budget(profile, budget)
    candidate_history: list[dict] = []
    termination_records: list[dict] = []

    usr = build_user_seam_set(mesh, spec)
    # G5: the user-assisted automatic path uses the SAME constraint object / accept rules
    # as the no-spec path — the user's seams are locked, their protected edges protected.
    if constraints is None:
        constraints = SeamConstraints.build(
            mesh, locked=usr.user_seam_edges, protected=usr.user_protected_edges,
            preferred=preferred_edges or (), front_axis=front_axis)
    mandatory = set(usr.mandatory_edges) if enforce_mandatory else set()
    if enforce_mandatory:
        forbidden = set(base_forbidden) | usr.forbidden_edges
        final_seams = set(usr.initial_seams)
    else:
        # Strict user/reference mode: use exactly the user's seam intent, without adding
        # mandatory folds. Protected edges still matter only for optional auto-refine.
        forbidden = set(base_forbidden) | (usr.user_protected_edges - usr.user_seam_edges)
        final_seams = set(usr.user_seam_edges)
    aux_seams: set[int] = set()         # mandatory-fold boundary cuts (auto, but mandatory-driven)
    distortion_seams: set[int] = set()  # auto_refine distortion splits (auto_added)
    history: list[dict] = [{"stage": "user_seams", "mode": "user_seams",
                            "user_seam_edges": len(usr.user_seam_edges),
                            "user_protected_edges": len(usr.user_protected_edges),
                            "mandatory_90_edges": len(mandatory),
                            "forbidden_edges": len(forbidden),
                            "conflicts": usr.conflicts, "invalid_edges": usr.invalid_edges,
                            "auto_refine": auto_refine,
                            "repair_user_seams": repair_user_seams,
                            "enforce_mandatory": enforce_mandatory,
                            "gate_mandatory": gate_mandatory}]

    def _eval_current():
        """Measure the object's CURRENT UV against ``final_seams`` (no re-unwrap)."""
        uvm = read_uvmap(obj, mesh)
        pl = island_plan_from_seams(mesh, final_seams)
        e = evaluate_uv_solution(mesh, pl, uvm)
        chs, _fc = _charts(mesh, final_seams)
        m = _chart_metrics(mesh, uvm, e)
        m["small_island_ratio"] = relative_small_island_ratio(mesh, pl, uvm)
        m.update(_shape_metrics(mesh, final_seams, mandatory))
        m.update(_audit_metrics(mesh, final_seams))
        m.update(_uv_audit_metrics(mesh, uvm))
        m.update(_distortion_report(mesh, uvm, chs))
        fcr = {f: i for i, fs in enumerate(flood_charts(mesh, final_seams)) for f in fs}
        m["raster_overlap_ratio"] = raster_overlap_diagnosis(mesh, uvm, fcr)["raster_overlap_ratio"]
        g = evaluate_chart_gate(m, config=config)
        if not gate_mandatory:
            g = _without_mandatory_gate_checks(g)
        return m, g, e

    def measure():
        """Unwrap+pack the current ``final_seams`` and measure — owns the shipped UV."""
        unwrap_and_pack(obj, final_seams, margin=pack_margin)
        return _eval_current()

    metrics, gate, ev = measure()
    initial_islands = ev.island_count

    # Mandatory ≥90° folds welded in the ACTUAL UV → cut to a chart boundary (mandatory wins,
    # plan §7 + §12: mandatory_90_uv_unsplit == 0). The LOCAL min-cost cut avoids forbidden
    # (protected) edges. These are mandatory-driven, NOT "auto seam"; still counted under
    # ``auto_added_seams`` honestly (they're not user/mandatory-fold edges themselves).
    if repair_user_seams and enforce_mandatory:
        for _ in range(8):
            if metrics["mandatory_90_uv_unsplit"] == 0:
                break
            r = split_welded_folds(mesh, final_seams, metrics["uv_unsplit_edge_ids"], forbidden=forbidden)
            if not (r["added"] or r["local_cuts"] or r["fallback"]):
                break  # cannot cut further — surfaced honestly by the hard gate
            aux_seams |= r["added"]
            history.append({"stage": "mandatory_fold_cut", "added": sorted(r["added"]),
                            "local_cuts": r["local_cuts"], "fallback": r["fallback"]})
            metrics, gate, ev = measure()
    elif gate_mandatory and metrics["mandatory_90_uv_unsplit"] != 0:
        history.append({"stage": "mandatory_fold_report_only",
                        "uv_unsplit": metrics["mandatory_90_uv_unsplit"],
                        "edge_ids": metrics.get("uv_unsplit_edge_ids", [])})

    # OPTIONAL auto distortion refine (plan §10, ``auto_refine_user_seams=true`` only). Split the
    # worst island while distortion exceeds the gate; never cut a protected edge (precedence:
    # protected > auto). Every added seam is tracked as an ``auto_added`` seam and reported.
    # The refinement loop is the SHARED decision core (G5: "no-spec 와 사용자 보조 자동 경로가
    # 같은 채택/revert 규칙"): propose → apply → measure → accept or fully restore, under the
    # same constraints/profile/budget the no-spec path uses.
    if auto_refine:
        before_seams = set(final_seams)
        ref = refinement_loop.run_refinement(
            obj, mesh, final_seams, constraints=constraints, profile=profile,
            budget=budget, margin=pack_margin, regions=regions, history=history,
            candidate_history=candidate_history)
        termination_records.append(ref["termination"])
        final_seams = set(ref["seams"])
        distortion_seams |= set(ref["distortion_seams"])
        history.append({"stage": "auto_refine", "action": "refinement_loop",
                        "added_edges": sorted(final_seams - before_seams),
                        "termination": ref["termination"]["reason"],
                        "iterations": ref["termination"]["iterations"],
                        "candidates_evaluated": ref["termination"]["candidates_evaluated"],
                        "auto_added": bool(final_seams - before_seams)})
        metrics, gate, ev = measure()

    # UV LAYOUT OPTIMIZATION LOOP (UV_LAYOUT_OPTIMIZATION_LOOP_PLAN §9.2). The seam set is now
    # FINAL — this never adds/removes a seam. It sweeps relax/scale/rotate/pack candidates on
    # the SAME ``final_seams``, scores each (checker distortion + texel density + overlap +
    # packing), and applies the best (or keeps the baseline if no candidate clearly wins).
    # Mandatory-90 audits are report-only here (plan §3.1) — already so when gate_mandatory off.
    layout_opt = None
    if optimize_layout:
        from chart_uv_agent.layout_optimization import (
            LayoutOptimizationConfig, run_layout_optimization,
        )
        lo_cfg = layout_optimization_config or LayoutOptimizationConfig(
            enabled=True, mode="user_reference")

        def _apply_spec(sp, *, promote_margin: bool = False):
            """Unwrap+pack ``final_seams`` per ``sp``; for a custom backend, follow with the
            island-level density-normalize/orient + MaxRects/shelf re-pack (MVP3 §2 Goal B,
            §5). Never touches the seam set.

            ``promote_margin`` (the FINAL application only — candidate comparison must keep
            each candidate's own margin) raises the spec margin to the G1 pack margin."""
            sp = dict(sp)
            if promote_margin:
                sp["margin"] = max(sp["margin"], pack_margin)
            unwrap_and_pack(obj, final_seams, margin=sp["margin"], method=sp["unwrap_method"],
                            minimize_iters=sp["minimize_iters"], pack_shape=sp["pack_shape"],
                            rotate=sp["rotate"], average_scale=sp["average_scale"])
            if sp.get("pack_backend", "blender") != "blender":
                from chart_uv_agent.island_layout import repack_uv_islands_custom
                repack_uv_islands_custom(
                    obj, mesh, seams=final_seams, padding=sp["margin"],
                    algorithm=sp["pack_backend"], allow_rotate=sp["rotate"],
                    density_normalize=sp.get("density_normalize", True),
                    orient_long_islands=sp.get("orient_long_islands", False))

        def _measure_candidate(cand_spec):
            _apply_spec(cand_spec)
            return _eval_current()[0]

        layout_opt = run_layout_optimization(_measure_candidate, dict(metrics), lo_cfg,
                                             mode="user_reference")
        _apply_spec(layout_opt.selected_spec, promote_margin=True)
        metrics, gate, ev = _eval_current()   # the shipped (best) layout
        history.append({"stage": "layout_optimization",
                        "selected_candidate_id": layout_opt.selected_candidate_id,
                        "kept_baseline": layout_opt.kept_baseline,
                        "candidate_count": len(layout_opt.candidates),
                        "pack_backend": layout_opt.selected_spec.get("pack_backend", "blender"),
                        "score_before": round(float(layout_opt.score_before), 6),
                        "score_after": round(float(layout_opt.score_after), 6),
                        "verdict": layout_opt.verdict()})

    seam_types = _classify_user_seams(mesh, final_seams, usr, aux_seams, distortion_seams,
                                      fold_angle=spec.mandatory_fold_angle,
                                      classify_mandatory=enforce_mandatory)
    # auto_added = every shipped seam that is NEITHER a user seam NOR a mandatory fold edge
    # (= the mandatory-fold path cuts + any auto_refine splits). 0 when the user spec already
    # separates every fold and no refine ran (plan §12: auto_added_seams default 0).
    auto_added = len(final_seams - usr.user_seam_edges - mandatory)
    distortion = {k: metrics[k] for k in ("checker_distortion_score", "worst_island_distortion",
                                          "worst_island_id", "worst_face_count")}
    user_block = {"mode": "user_seams",
                  **usr.report(final_seams=final_seams, auto_added=auto_added)}
    if not enforce_mandatory:
        user_block["mandatory_rule_enabled"] = False
        user_block["mandatory_90_edges"] = 0
    if not gate_mandatory:
        user_block["mandatory_gate_enabled"] = False
    conclusion = _user_seam_conclusion(usr, metrics, config, auto_added, auto_refine,
                                       include_mandatory_diagnostics=gate_mandatory)
    result = {
        "engine": "chart", "mode": "user_seams", "seams": sorted(final_seams),
        "chart_count": ev.island_count, "metrics": metrics, "gate": gate,
        "gate_config": config.to_dict(), "history": history, "rounds": len(history) - 1,
        "distortion": distortion, "conclusion": conclusion,
        "mandatory_90_edges": metrics["mandatory_90_edges"],
        "mandatory_90_missing": metrics["mandatory_90_missing"],
        "mandatory_90_fold_edges": metrics["mandatory_90_fold_edges"],
        "mandatory_90_uv_unsplit": metrics["mandatory_90_uv_unsplit"],
        "initial_island_count": initial_islands, "final_island_count": ev.island_count,
        "seam_type_counts": _count_types(seam_types), "pruned_auxiliary": 0,
        "forbidden_edges": sorted(forbidden), "forbidden_stripped": [],
        "shape_repair": [], "tail_round": [], "correctness": [],
        "metrics_before_correctness": {}, "stuck_charts": [],
        "shippable": shippable_with_stuck(gate, []),
        "user_seams": user_block,
        "pack_margin_uv": float(pack_margin), "margin_px": int(profile.margin_px),
        "texture_size_px": int(profile.texture_size_px),
    }
    # G1/G5: island-gap-ONLY failures are re-packed (after the layout-optimization candidate
    # has been applied), never cut.
    _gap_repack_pass(obj, mesh, final_seams, profile=profile, regions=regions,
                     pack_margin=pack_margin, history=history)
    # Same v2 final measurement + evidence blocks as the no-spec path (G1/G4/G5).
    v2_block, v2_measurement = _v2_result_block(
        obj, mesh, final_seams, profile=profile, regions=regions, constraints=constraints,
        forbidden=forbidden, mandatory=mandatory, distortion_seams=distortion_seams,
        candidate_history=candidate_history, termination_records=termination_records,
        budget=budget, elapsed=time.monotonic() - started_at, gate=gate,
        exhausted_rounds=False,
        island_cap=min(int(config.island_count_max), int(budget["island_cap"])),
        history=history)
    result.update(v2_block)
    # Same G1 input-defect diagnosis as the no-spec path.
    result["input_diagnostics"] = _input_diagnostics(mesh, v2_block.get("distortion_v2"))
    result["correctness_rounds"] = []
    _apply_v2_metrics(metrics, v2_measurement)
    result["seam_types"] = {int(k): v for k, v in seam_types.items()}
    if layout_opt is not None:
        result["layout_optimization"] = layout_opt.report()
        result["layout_optimization_summary"] = layout_opt.summary()
    from artist_uv_agent.seam_report import build_seam_report
    result["seam_report"] = build_seam_report(result, source_mesh=getattr(mesh, "object_id", None))
    return result


def _classify_user_seams(mesh, seams, usr, aux_seams, distortion_seams, *,
                         fold_angle: float = 90.0, classify_mandatory: bool = True) -> dict:
    """Tag each shipped user-mode seam by origin. Precedence (plan §7): a ≥``fold_angle`` fold
    is always ``mandatory_90``; then user seams, then auto distortion splits, then the
    mandatory-fold boundary cuts; anything else is a leftover ``auto`` seam (should not occur)."""
    out: dict[int, str] = {}
    for e in seams:
        if classify_mandatory and (mesh.edges[e].dihedral_angle >= fold_angle or e in usr.mandatory_edges):
            out[e] = "mandatory_90"
        elif e in usr.user_seam_edges:
            out[e] = "user_seam"
        elif e in distortion_seams:
            out[e] = "distortion_split"
        elif e in aux_seams:
            out[e] = "mandatory_fold_auxiliary"
        else:
            out[e] = "auto"
    return out


def _without_mandatory_gate_checks(gate: ChartGateResult) -> ChartGateResult:
    """User/reference seam mode can be run with mandatory fold rules disabled. Keep the
    measurements in the report, but remove them from the blocking gate decision."""
    return ChartGateResult(
        checks=[c for c in gate.checks
                if c.name not in {"mandatory_90_missing", "mandatory_90_uv_unsplit"}]
    )


def _user_seam_conclusion(usr, metrics, config, auto_added: int, auto_refine: bool, *,
                          include_mandatory_diagnostics: bool = True) -> str:
    """One-line honest verdict for user-seam mode (plan §10 quality feedback)."""
    parts: list[str] = []
    if usr.conflicts:
        parts.append(f"{len(usr.conflicts)} protected/mandatory conflict(s) (mandatory wins)")
    if usr.invalid_edges:
        parts.append(f"{len(usr.invalid_edges)} invalid edge id(s) ignored")
    if include_mandatory_diagnostics:
        unsplit = int(metrics.get("mandatory_90_uv_unsplit", 0))
        missing = int(metrics.get("mandatory_90_missing", 0))
        if missing:
            parts.append(f"{missing} mandatory ≥90° fold(s) MISSING from the seam set")
        if unsplit:
            parts.append(f"{unsplit} mandatory fold(s) still WELD in the UV")
    if metrics.get("raster_overlap_ratio", 0.0) > config.raster_overlap_max:
        parts.append(f"raster overlap {metrics['raster_overlap_ratio']:.4f} over "
                     f"{config.raster_overlap_max}")
    if metrics.get("worst_island_distortion", 0.0) > config.worst_island_distortion_max:
        parts.append(f"worst island distortion {metrics['worst_island_distortion']:.3f} over "
                     f"{config.worst_island_distortion_max}")
    if auto_added:
        kind = "auto distortion + mandatory-fold" if auto_refine else "mandatory-fold"
        parts.append(f"{auto_added} {kind} seam(s) added")
    head = "user-seam mode: applied the user's seam plan"
    if not parts:
        return f"{head}; all hard gates pass with no auto-added seams."
    return f"{head}; " + "; ".join(parts) + " (reported, not auto-fixed)."


_PRUNE_CAP = 80  # max auxiliary-seam prune attempts (each a re-unwrap) — runtime bound


def _prune_seams(final_seams: set, candidates, accept) -> list:
    """Greedily remove each candidate seam, KEEPING the removal iff ``accept()`` (a no-arg
    callable that re-measures the now-current ``final_seams`` and returns True when every hard
    gate still holds) passes; otherwise put it back. Mutates ``final_seams`` in place and
    returns the list of edges actually removed (MINIMAL_DISTORTION_UV_PLAN follow-up §5)."""
    removed: list[int] = []
    for e in candidates:
        if e not in final_seams:
            continue
        final_seams.discard(e)
        if accept():
            removed.append(e)
        else:
            final_seams.add(e)            # revert — this seam is load-bearing
    return removed


def _classify_seams(mesh, seams, aux_seams, overlap_seams, distortion_seams, forbidden,
                    *, fold_angle: float = 90.0) -> dict:
    """Tag each shipped seam with its origin/type so a post-pass can tell which seams are
    safe to reconsider (only ``welded_fold_auxiliary``). Precedence: a ≥90° fold is always
    ``mandatory_90`` regardless of which pass first added it."""
    out: dict[int, str] = {}
    for e in seams:
        if mesh.edges[e].dihedral_angle >= fold_angle:
            out[e] = "mandatory_90"                 # mandatory wins, even if also forbidden
        elif e in forbidden:
            out[e] = "user_forbidden"               # should not occur after the strip pass
        elif e in aux_seams:
            out[e] = "welded_fold_auxiliary"
        elif e in distortion_seams:
            out[e] = "distortion_split"
        elif e in overlap_seams:
            out[e] = "overlap_repair"
        else:
            out[e] = "segmentation"
    return out


def _count_types(seam_types: dict) -> dict:
    out: dict[str, int] = {}
    for t in seam_types.values():
        out[t] = out.get(t, 0) + 1
    return out


def _island_conclusion(initial: int, final: int, history, metrics, config) -> str:
    """The plan's required one-line verdict (MINIMAL_DISTORTION_UV_PLAN §M6): state whether
    the island count grew and why, and — per the user's per-island rule — explicitly
    distinguish "global distortion passed but the worst island still failed" (a best-effort
    ship where the loop ran out of splits) from a clean pass."""
    split_reasons = sorted({h.get("reason") for h in history
                            if h.get("action") == "split" and h.get("reason")})
    g_stretch = metrics.get("stretch_score", 0.0)
    worst = metrics.get("worst_island_distortion", 0.0)
    worst_id = metrics.get("worst_island_id", -1)
    global_ok = g_stretch <= config.stretch_max
    worst_ok = worst <= config.worst_island_distortion_max

    unsplit = int(metrics.get("mandatory_90_uv_unsplit", 0))
    if unsplit > 0:
        return (f"best-effort: {unsplit} of {metrics.get('mandatory_90_fold_edges', '?')} "
                f"≥90° folds still WELD in the UV (could not be cut to a chart boundary); "
                f"islands {initial}→{final}.")
    if global_ok and not worst_ok:
        # The user's exact concern: headline metric passes, one island is still distorted.
        return (f"best-effort: GLOBAL checker distortion passed ({g_stretch:.3f} ≤ "
                f"{config.stretch_max}) but WORST island {worst_id} distortion "
                f"{worst:.3f} exceeds the per-island threshold "
                f"{config.worst_island_distortion_max} (islands {initial}→{final}; "
                f"out of splits at the island cap or no-progress).")
    grow = ", ".join(split_reasons) or "distortion/overlap"
    if final > initial:
        return (f"islands increased from {initial} to {final} because checker distortion "
                f"or overlap exceeded threshold ({grow}); final worst-island distortion "
                f"{worst:.3f} ≤ {config.worst_island_distortion_max}.")
    return (f"islands stayed at {final} because both global ({g_stretch:.3f}) and "
            f"worst-island ({worst:.3f}) checker distortion were within threshold.")


def correctness_pass(obj, mesh, seams, config, *, max_rounds=4, margin=0.005, hard_cap=60,
                     forbidden=frozenset(), reject_history=None):
    """Correctness round (§5d, SLIM-driven). Owns the FINAL UV. The main unwrap already uses
    SLIM (``MINIMUM_STRETCH``, locally injective), so self-folds are gone up front. This
    pass is the safety net: re-SLIM the still-folding charts in isolation, and ONLY a chart
    that still self-overlaps after that (rare boundary self-overlap) is split and re-SLIMed —
    split is the exception, never the driver, so the chart count stays in the cap.
    Cross-invasion (the packer prevents it; 0 in practice) → margin bump + AABB re-pack.
    Mutates ``seams``; returns the per-round history + final diagnosis."""
    from chart_uv_agent.segmentation import flood_charts, split_chart
    from chart_uv_agent.unwrap import read_uvmap, reunwrap_faces, repack, unwrap_and_pack
    from uv_agent.geometry.evaluation import raster_overlap_diagnosis

    def diag():
        uvmap = read_uvmap(obj, mesh)
        fc = {f: i for i, fs in enumerate(flood_charts(mesh, seams)) for f in fs}
        return raster_overlap_diagnosis(mesh, uvmap, fc)

    unwrap_and_pack(obj, seams, margin=margin)             # SLIM
    history: list[dict] = []
    bumped = False
    for rnd in range(max_rounds):
        d = diag()
        history.append({"round": rnd, "raster": d["raster_overlap_ratio"],
                        "self": d["self_overlap_ratio"], "cross": d["cross_overlap_ratio"],
                        "charts": len(flood_charts(mesh, seams)),
                        "action": "ok" if d["raster_overlap_ratio"] <= config.raster_overlap_max else "repair"})
        if d["raster_overlap_ratio"] <= config.raster_overlap_max:
            break
        if d["cross_charts"] and not bumped:               # inter-chart invasion (rare)
            repack(obj, margin=min(0.05, margin * 4), pack_shape="AABB"); bumped = True
            continue
        charts = flood_charts(mesh, seams)
        fold = {f for c in d["self_charts"] if c < len(charts) for f in charts[c]}
        if fold:
            reunwrap_faces(obj, fold, method="MINIMUM_STRETCH", margin=0.001)  # SLIM, isolated
            repack(obj, margin=margin)
        d2 = diag()
        if d2["raster_overlap_ratio"] > config.raster_overlap_max:
            # Exception path: SLIM still leaves a fold (boundary self-overlap) → split it.
            charts = flood_charts(mesh, seams)
            n = len(charts)
            for cid in d2["self_charts"]:
                if cid < len(charts) and len(charts[cid]) >= 10 and n < hard_cap:
                    # G4: the same protected-edge law as every other cut path.
                    _, _, ns, rej = constrained_split_chart(mesh, charts[cid], seams,
                                                            forbidden)
                    if rej:
                        rec = {"stage": "correctness", "round": rnd, "action": "reject",
                               "reason": "protected_region_reject",
                               "protected_edges_cut": sorted(rej)}
                        (reject_history if reject_history is not None else history).append(rec)
                    if ns:
                        seams.update(ns); n += 1
            unwrap_and_pack(obj, seams, margin=margin)      # SLIM
    final = diag()
    return {"history": history, "final": final, "raster_overlap_ratio": final["raster_overlap_ratio"],
            "splits": len(flood_charts(mesh, seams))}


def _shape_metrics(mesh, seams, mandatory) -> dict:
    """U1.6 shape metrics for the gate (convexity / smoothness / tendrils)."""
    from chart_uv_agent.segmentation import flood_charts
    from chart_uv_agent.shape import measure_charts

    sh = measure_charts(mesh, flood_charts(mesh, set(seams)), set(seams), mandatory)
    return {"convexity_mean": sh["convexity_mean"], "convexity_p10": sh["convexity_p10"],
            "boundary_smoothness_mean": sh["boundary_smoothness_mean"],
            "tendril_count": sh["tendril_count"]}


def shippable_with_stuck(gate, stuck_charts) -> bool:
    """Ship if the gate passes. Under the minimal-distortion gate convexity is ADVISORY
    (never a hard failure), so this is normally just ``gate.passed``. The legacy §5c branch
    — ship when the ONLY hard failure is the convexity_p10 tail and the tail loop proved
    the below-bar charts stuck — is retained for ``shape_passes=True`` runs that still
    surface convexity_p10 as a (now advisory) signal."""
    if gate.passed:
        return True
    fails = [c.name for c in gate.failures]
    return fails == ["convexity_p10"] and len(stuck_charts) > 0


def _better(metrics, gate, best) -> bool:
    """Prefer a passing gate; among equals, fewer hard fails, then lower TRUE overlap
    (correctness first), then lower stretch."""
    if gate.passed != best["gate"].passed:
        return gate.passed
    if len(gate.failures) != len(best["gate"].failures):
        return len(gate.failures) < len(best["gate"].failures)
    ro, bro = metrics.get("raster_overlap_ratio", 0.0), best["metrics"].get("raster_overlap_ratio", 0.0)
    if abs(ro - bro) > 1e-6:
        return ro < bro
    return metrics["stretch_score"] < best["metrics"]["stretch_score"]
