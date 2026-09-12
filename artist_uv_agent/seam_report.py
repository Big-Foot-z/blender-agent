"""Seam Decision Core — reviewer-facing report (RULE_BASED_UV_SEAM_CORE_PLAN §5.3).

The plan's point is that the engine must not be a black box (§8, §13): the reviewer has to
see *why* each cut happened, how much checker distortion dropped, why the island count grew,
and whether the user's forbidden/preferred intent was honoured. This module turns the
existing ``chart_uv_agent.pipeline.run_chart_uv`` result dict (plus, optionally, the
edge-level :mod:`artist_uv_agent.seam_policy` decisions) into the exact JSON schema §5.3
specifies, ready for the Windows app's seam overlay / distortion heatmap / feedback panel.

Pure: no Blender, no mesh mutation — it only re-shapes data the pipeline already produced, so
it can be unit-tested and called from either the headless worker or a UI.
"""

from __future__ import annotations

from artist_uv_agent.seam_policy import EdgeSeamDecision


# Pipeline split-round ``reason`` → the report's compact "reason" label for an *added* seam.
_ADDED_REASON = {
    "checker_distortion": "distortion_repair",
    "worst_island_distortion": "distortion_repair",
    "distortion_split": "distortion_repair",
    "welded_fold_auxiliary": "fold_boundary_cut",
    "flipped_faces": "overlap_repair",
    "raster_overlap_self": "overlap_repair",
}


def _added_seams(result: dict) -> list[dict]:
    """The non-segmentation, non-mandatory seams the refinement loop ADDED, edge-level where
    the pipeline records the edges (RULE_BASED_UV_SEAM_CORE_PLAN §5.3). A distortion split
    records ``added_edges`` + the before/after distortion + the accepted ``improvement_ratio``
    (the §5.2 accept proof), so each cut edge becomes one entry with ``edge_id``, ``reason``,
    ``island_before``, ``distortion_before/after`` and ``improvement_ratio`` — exactly what
    the app needs to answer "why was this edge cut?". Only edges actually in the FINAL shipped
    ``result["seams"]`` are reported — a split that was reverted, lost in the ``best`` pick,
    stripped as a user-preserve edge, or left provisional/unresolved at the cap never appears,
    so the report can't claim a seam the exported UV doesn't have. Fold/overlap repairs that
    don't record per-edge ids fall back to a round-level entry (no ``edge_id``)."""
    shipped = set(result.get("seams", []))
    added: list[dict] = []
    for rec in result.get("history", []):
        if rec.get("action") != "split" or rec.get("reverted") or rec.get("unresolved"):
            continue
        reason = _ADDED_REASON.get(rec.get("reason", ""), rec.get("reason", "distortion_repair"))
        common = {
            "round": rec.get("round"),
            "reason": [reason],
            "island_before": rec.get("split_island", rec.get("worst_island")),
        }
        if "before_distortion" in rec:
            common["distortion_before"] = rec["before_distortion"]
        if "distortion_after" in rec:
            common["distortion_after"] = rec["distortion_after"]
        if "improvement_ratio" in rec:
            common["improvement_ratio"] = rec["improvement_ratio"]
        edges = rec.get("added_edges")
        if edges:
            for eid in edges:
                if eid in shipped:          # only seams the final layout actually carries
                    added.append({"edge_id": eid, **common})
        else:
            added.append(common)            # fold/overlap repair (no per-edge ids recorded)
    return added


def _conflicts(result: dict, decisions: list[EdgeSeamDecision] | None) -> list[dict]:
    """Mandatory-vs-user-forbidden clashes (§5.3 ``conflicts``). Sourced first from the
    pipeline's ``forbidden_stripped`` (edges the user forbade that were kept/removed) and the
    policy decisions' recorded ``conflict:`` reasons; mandatory always wins."""
    out: list[dict] = []
    seen: set[int] = set()
    if decisions:
        for d in decisions:
            if d.decision == "mandatory" and any(r.startswith("conflict:") for r in d.reasons):
                out.append({"edge_id": d.edge_id, "user_rule": "forbidden",
                            "engine_rule": "mandatory_90", "resolution": "mandatory_wins"})
                seen.add(d.edge_id)
    # A forbidden edge that nonetheless shipped as a seam (must be a mandatory fold) is a
    # conflict the pipeline records implicitly: it is in forbidden_edges but NOT stripped.
    forbidden = set(result.get("forbidden_edges", []))
    stripped = set(result.get("forbidden_stripped", []))
    shipped_seams = set(result.get("seams", []))
    for e in sorted(forbidden & shipped_seams - stripped - seen):
        out.append({"edge_id": e, "user_rule": "forbidden",
                    "engine_rule": "mandatory_90", "resolution": "mandatory_wins"})
    return out


def build_seam_report(result: dict, *,
                      decisions: list[EdgeSeamDecision] | None = None,
                      source_mesh: str | None = None) -> dict:
    """Assemble the §5.3 seam report from a ``run_chart_uv`` result dict.

    Required keys produced (matching the plan's schema): ``mandatory_90_edges``,
    ``mandatory_90_missing``, ``mandatory_90_uv_unsplit`` (the exported-UV audit the plan
    insists on in §10), ``initial_island_count``, ``final_island_count``,
    ``stretch_before`` / ``stretch_after``, ``added_seams`` (with reasons), and
    ``conflicts``. Extra context (distortion block, seam-type counts, conclusion, the gate
    verdict) is included so the report is self-contained for the app.

    ``decisions`` (optional) are the edge-level :class:`EdgeSeamDecision`s from
    :func:`artist_uv_agent.seam_policy.decide_edge_seams`; when given, the mandatory/candidate
    counts and conflict list are enriched from them."""
    metrics = result.get("metrics", {})
    history = result.get("history", [])
    stretch_before = None
    for rec in history:
        if "stretch" in rec:
            stretch_before = rec["stretch"]
            break
    stretch_after = metrics.get("stretch_score")

    report = {
        "engine": result.get("engine", "chart"),
        "source_mesh": source_mesh,
        "mandatory_90_edges": result.get("mandatory_90_edges",
                                         metrics.get("mandatory_90_edges", 0)),
        "mandatory_90_missing": result.get("mandatory_90_missing",
                                           metrics.get("mandatory_90_missing", 0)),
        "mandatory_90_fold_edges": result.get("mandatory_90_fold_edges",
                                              metrics.get("mandatory_90_fold_edges", 0)),
        "mandatory_90_uv_unsplit": result.get("mandatory_90_uv_unsplit",
                                              metrics.get("mandatory_90_uv_unsplit", 0)),
        "initial_island_count": result.get("initial_island_count"),
        "final_island_count": result.get("final_island_count", result.get("chart_count")),
        "stretch_before": stretch_before,
        "stretch_after": stretch_after,
        "global_checker_distortion": metrics.get("checker_distortion_score"),
        "worst_island_distortion": metrics.get("worst_island_distortion"),
        "worst_island_id": metrics.get("worst_island_id"),
        "added_seams": _added_seams(result),
        "conflicts": _conflicts(result, decisions),
        "seam_type_counts": result.get("seam_type_counts", {}),
        "forbidden_edges": result.get("forbidden_edges", []),
        "forbidden_stripped": result.get("forbidden_stripped", []),
        "pruned_auxiliary": result.get("pruned_auxiliary", 0),
        "conclusion": result.get("conclusion", ""),
        "verdict": result.get("gate").verdict if hasattr(result.get("gate"), "verdict")
        else result.get("verdict"),
    }
    for _key in ("constraints", "termination", "seam_length"):   # G4/G5 evidence blocks
        if _key in result:
            report[_key] = result[_key]
    if decisions is not None:
        counts: dict[str, int] = {}
        for d in decisions:
            counts[d.decision] = counts.get(d.decision, 0) + 1
        report["policy_decision_counts"] = counts
    # Important Region Policy block (IMPORTANT_REGION_UV_POLICY_PLAN §5.5): per-region
    # mandatory-vs-smooth seam counts + rejected protected splits. Present only when a region
    # policy ran (``run_chart_uv(region_policy=...)``); absent → baseline, no regions key.
    if result.get("region_report"):
        report["regions"] = result["region_report"]
    # User-Guided Seam block (USER_GUIDED_SEAM_UV_PIPELINE_PLAN §9): the mode + user_seams
    # summary (user/protected/mandatory counts, final seam count, auto-added, conflicts,
    # invalid edges). Present only on a user-seam run; absent → auto chart run, no mode key.
    if result.get("user_seams"):
        report["mode"] = result.get("mode", "user_seams")
        report["user_seams"] = result["user_seams"]
    # UV Layout Optimization Loop summary (UV_LAYOUT_OPTIMIZATION_LOOP_PLAN §11): compact
    # before/after of the relax/scale/rotate/pack candidate sweep. Present only when
    # ``--optimize-layout`` ran; absent → single-unwrap layout, no key.
    if result.get("layout_optimization_summary"):
        report["layout_optimization"] = result["layout_optimization_summary"]
    return report
