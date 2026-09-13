"""Merge-back: give every *unnecessary* seam back to the mesh (Gate G7 / G12).

The refinement loop only ever *adds* cuts: each round splits an island to buy distortion
headroom, and a cut that was worth making in round 3 may be pointless by round 9 once a
neighbouring island has been re-charted. G7 is the closing argument for that — after the
loop settles, the run must prove that **no seam can be removed without losing quality**:

    quality-preserving removable seam count == 0, or the budget is exhausted with a record.

So this module walks the *adjacent island pairs* of the current chart layout, and for each
pair tries to dissolve the whole shared seam boundary at once. A trial is accepted only if
the merged layout still passes the FULL measurement (distortion + correctness + mandatory
audit + whatever else :func:`~chart_uv_agent.refinement_loop.measure_layout` checks) *and*
the island count really went down by exactly one — a "merge" that does not merge is a
rejected trial, not a silent success.

Never negotiable (G4): ``constraints.mandatory`` (≥ 90° folds, boundary / non-manifold
edges), ``constraints.locked`` (explicit user seams) and the caller's ``required`` set
(e.g. shading-required seams) are not removable, so a group containing any of them is
never even trialled.

Restore discipline is the refinement loop's (G5): every trial is bracketed by
:func:`~chart_uv_agent.refinement_loop.take_snapshot` /
:func:`~chart_uv_agent.refinement_loop.restore_snapshot`, and the restore runs on an
unconditional ``finally``-style path — a rejected merge and a merge that *raises* both
leave the object with exactly the UVs and seams it had before the trial.

Determinism (G12): no randomness anywhere. Groups are ordered by
``(-shared_length, island_a, island_b)`` with the length rounded to 9 decimals, so float
noise never decides the order, and a rejected group is remembered by its edge set (island
ids renumber after a merge, edge ids do not).

Blender is reached ONLY through the refinement-loop helpers, which resolve
:mod:`chart_uv_agent.unwrap` as a *module attribute* at call time — so this module runs
off-Blender against ``tests.helpers.fake_blender_uv.FakeUnwrapBackend``. ``bpy`` is never
imported here.
"""

from __future__ import annotations

import time

from chart_uv_agent.candidates import bbox_diagonal, seam_length
from chart_uv_agent.catastrophic_repair import _counters_not_worse, catastrophic_counters
from chart_uv_agent.quality_profile import QualityProfile
from chart_uv_agent.refinement_loop import (
    gap_only_failure,
    repack_for_gap,
    restore_snapshot,
    take_snapshot,
    unwrap_and_measure,
)
from chart_uv_agent.segmentation import flood_charts
from uv_agent.geometry.mesh_graph import MeshGraph

#: Trial verdicts, as they appear in a history record's ``reason``.
TRIAL_REASONS = ("accepted", "quality_failed", "island_count_unchanged", "exception",
                 "accepted_repair", "repair_not_improved", "hard_failures_grew",
                 "catastrophic_worse", "correctness_worse", "fragmentation_worse")

#: The fragmentation HARD checks a merge must never make worse in repair mode. The two
#: count metrics live in ``fragmentation["metrics"]``; the rest are ``checks`` rows that
#: only exist when the profile supplies the matching pixel cap (CG8).
_FRAG_HARD_METRICS = ("zero_area_island_count", "below_min_area_island_count")
_FRAG_HARD_CHECKS = ("sliver_islands", "island_min_width_px", "island_min_area_px2",
                     "island_bbox_aspect", "island_perimeter_area_ratio")

#: Absolute slack on the overlap AREA comparison (a re-pack moves islands, so the total is
#: re-derived from a fresh unwrap and last-bit noise must not read as a regression).
_OVERLAP_TOLERANCE = 1e-12


def _fragmentation_hard_counts(measurement: dict) -> dict:
    """The non-exempt fragmentation HARD offender counts of one measurement (CG8/CG10)."""
    report = (measurement or {}).get("fragmentation") or {}
    metrics = report.get("metrics") or {}
    out: dict[str, float] = {}
    for key in _FRAG_HARD_METRICS:
        out[key] = float(metrics.get(key, 0) or 0)
    for check in report.get("checks") or ():
        name = str((check or {}).get("name", ""))
        if name in _FRAG_HARD_CHECKS and str(check.get("scope")) == "hard":
            out[name] = float(check.get("value", 0) or 0)
    return out


def _fragmentation_verdict(before: dict, after: dict) -> tuple[bool, bool]:
    """``(nothing got worse, something got strictly better)`` over the shared hard counts."""
    better = False
    for key, before_value in before.items():
        if key not in after:
            continue                      # the check is not part of this profile
        if after[key] > before_value:
            return False, False
        if after[key] < before_value:
            better = True
    return True, better


def _correctness_counters(measurement: dict) -> tuple[float, int, int]:
    """``(overlap area, local flips, UV-degenerate triangles)`` — lower is better."""
    corr = (measurement or {}).get("correctness") or {}
    return (
        float((corr.get("overlap") or {}).get("overlap_area_total", 0.0) or 0.0),
        int((corr.get("orientation") or {}).get("local_flip_count", 0) or 0),
        int((corr.get("degenerate") or {}).get("uv_degenerate_count", 0) or 0),
    )


def _correctness_not_worse(before: dict, after: dict) -> bool:
    b_area, b_flips, b_degen = _correctness_counters(before)
    a_area, a_flips, a_degen = _correctness_counters(after)
    return (a_area <= b_area + _OVERLAP_TOLERANCE
            and a_flips <= b_flips and a_degen <= b_degen)


def _broken_island_ids(measurement: dict) -> set[int]:
    """The island ids the fragmentation report already calls tiny or sliver."""
    report = (measurement or {}).get("fragmentation") or {}
    out: set[int] = set()
    for key in ("tiny_island_ids", "sliver_island_ids"):
        out.update(int(i) for i in (report.get(key) or ()))
    return out


def repair_trial_verdict(before: dict, after: dict, *, before_count: int,
                         touches_broken: bool) -> str:
    """Why a merge trial on an ALREADY-FAILING layout is accepted or not (CG10/CG8).

    A failing layout cannot be asked "does this merge still pass?" — nothing passes. The
    question is instead "does dissolving this seam make the layout *less* broken without
    breaking anything else?", so a merge is kept iff the island count really dropped by
    one, no hard failure / catastrophic counter / correctness counter / fragmentation hard
    count got worse, and either a fragmentation hard count got strictly better or the pair
    that was merged contained one of the tiny / sliver islands the report complains about.

    ``mandatory``, ``texel density`` and the shading-required seams need no separate test:
    a regression in any of them shows up as a new entry in ``hard_failures`` (and a
    shading-required group is never trialled at all).

    Returns ``"accepted_repair"`` or the name of the first rule that refused.
    """
    if int(after.get("island_count", -1)) != int(before_count) - 1:
        return "island_count_unchanged"
    if not set(after.get("hard_failures") or ()) <= set(before.get("hard_failures") or ()):
        return "hard_failures_grew"
    if not _counters_not_worse(catastrophic_counters(after.get("catastrophic") or {}),
                               catastrophic_counters(before.get("catastrophic") or {})):
        return "catastrophic_worse"
    if not _correctness_not_worse(before, after):
        return "correctness_worse"
    not_worse, improved = _fragmentation_verdict(_fragmentation_hard_counts(before),
                                                 _fragmentation_hard_counts(after))
    if not not_worse:
        return "fragmentation_worse"
    if not (improved or touches_broken):
        return "repair_not_improved"
    return "accepted_repair"


# ----------------------------------------------------------- removable groups


def removable_seam_groups(mesh: MeshGraph, seams, constraints, *,
                          required=frozenset()) -> list[dict]:
    """The shared seam boundaries that MAY be dissolved, best candidate first (G7).

    One row per adjacent island pair ``(a < b)`` of ``flood_charts(mesh, seams)``: its
    ``edges`` are the seam edges with one face in ``a`` and the other in ``b`` — the whole
    wall between the two islands, because removing only part of it would not merge them.

    A group is removable iff **none** of its edges is mandatory, user-locked or in
    ``required``; the mandatory / locked test is delegated to
    :meth:`~chart_uv_agent.constraints.SeamConstraints.check_removed` so this module and
    the cut stages share one definition of "protected" (G4).

    Deterministic order: longest shared boundary first (the biggest seam-length win),
    then ``island_a``, then ``island_b``.
    """
    seam_set = {int(e) for e in seams}
    required_set = {int(e) for e in required}
    islands = flood_charts(mesh, seam_set)

    island_of: dict[int, int] = {}
    for index, faces in enumerate(islands):
        for fid in faces:
            island_of[int(fid)] = index

    shared: dict[tuple[int, int], list[int]] = {}
    for eid in sorted(seam_set):
        if not (0 <= eid < mesh.edge_count):
            continue
        face_ids = [int(f) for f in mesh.edges[eid].face_ids]
        if len(face_ids) != 2:
            continue                      # boundary / non-manifold: no pair to merge
        a = island_of.get(face_ids[0])
        b = island_of.get(face_ids[1])
        if a is None or b is None or a == b:
            continue                      # an interior seam inside one island
        shared.setdefault((min(a, b), max(a, b)), []).append(eid)

    rows: list[dict] = []
    for (island_a, island_b), edges in shared.items():
        edges = sorted(edges)
        if not constraints.check_removed(edges)["ok"]:
            continue                      # mandatory fold or user-locked seam
        if required_set.intersection(edges):
            continue                      # e.g. a shading-required seam
        rows.append({
            "island_a": int(island_a),
            "island_b": int(island_b),
            "edges": edges,
            "shared_length": seam_length(mesh, edges),
        })

    rows.sort(key=lambda row: (-round(float(row["shared_length"]), 9),
                               row["island_a"], row["island_b"]))
    return rows


# -------------------------------------------------------------------- runner


def merge_back_disabled_block() -> dict:
    """The ``merge_back`` report block for a run with merge-back switched off."""
    return {
        "enabled": False,
        "complete": True,
        "reason": "disabled",
        "trials": 0,
        "accepted": 0,
        "removable_remaining": None,
        "history": [],
    }


def run_merge_back(obj, mesh: MeshGraph, seams, *, constraints,
                   profile: QualityProfile, margin: float, regions: dict | None = None,
                   required=frozenset(), max_trials: int | None = None,
                   clock=time.monotonic, time_budget_s: float | None = None,
                   history: list | None = None,
                   initial_measurement: dict | None = None,
                   repair_mode: bool = True) -> dict:
    """Dissolve every seam that is not paying for itself, to a recorded end (G7/G12).

    Two modes, decided by the INPUT measurement:

    ``preserve`` (the input already passes)
        the historical behaviour, byte for byte: accept a merge iff the merged layout still
        passes the full measurement AND the island count dropped by exactly one.

    ``repair`` (the input FAILS and ``repair_mode`` is on)
        over-segmentation is one of the things that *makes* a layout fail — a wall of tiny
        and sliver islands is exactly what merge-back exists to dissolve — so a failing
        input is no longer skipped. Groups touching a tiny / sliver island are trialled
        first and a merge is kept under :func:`repair_trial_verdict`: strictly less broken,
        nothing else worse. With ``repair_mode=False`` a failing input is still reported as
        ``skipped_quality_failed`` with zero trials.

    Otherwise each iteration takes the best untried removable group and trials it. Accept
    iff the merged layout passes the full measurement AND the island count dropped by
    exactly one; on accept the object keeps the merged layout and the loop continues on the
    new (renumbered) island graph. On reject or exception the object is restored exactly and
    the group is remembered as tried-and-rejected, so the loop always makes progress.

    Termination is always explicit: ``no_removable_seam`` (the G7 target — nothing is
    removable any more, ``complete`` True), ``trial_budget`` or ``time_budget``
    (``complete`` False, with the whole trial record kept), or ``skipped_quality_failed``.
    """
    started = clock()
    history = history if history is not None else []
    seams = {int(e) for e in seams}
    required = frozenset(int(e) for e in required)
    limit = int(profile.merge_back_max_trials if max_trials is None else max_trials)
    budget_s = None if time_budget_s is None else float(time_budget_s)

    measurement = initial_measurement
    if measurement is None:
        measurement = unwrap_and_measure(obj, mesh, seams, profile=profile, margin=margin,
                                         stage="merge_back", regions=regions)
        # G9/G11: a layout that fails ONLY on packing gaps is a PLACEMENT defect. Re-pack
        # it wider before deciding this stage has nothing to reason about.
        if (not measurement.get("passed", False)
                and gap_only_failure(measurement.get("correctness") or {})):
            measurement = repack_for_gap(obj, mesh, seams, profile=profile,
                                         pack_margin=margin, regions=regions,
                                         stage="merge_back_gap_repack")

    diagonal = bbox_diagonal(mesh)
    seam_length_before = seam_length(mesh, seams)
    island_count_before = int(measurement["island_count"])
    # CG10: a FAILING input is a repair job, not a reason to walk away — unless the caller
    # switched the repair mode off.
    mode = "preserve" if measurement.get("passed", False) else "repair"

    records: list[dict] = []
    removed_edges: set[int] = set()
    tried: set[frozenset[int]] = set()
    trials = 0
    accepted = 0

    def result(reason: str, complete: bool, remaining) -> dict:
        after_length = seam_length(mesh, seams)
        return {
            "enabled": True,
            "complete": bool(complete),
            "reason": str(reason),
            "mode": str(mode),
            "trials": int(trials),
            "accepted": int(accepted),
            "island_count_before": island_count_before,
            "island_count_after": int(measurement["island_count"]),
            "seam_length_before": float(seam_length_before),
            "seam_length_after": float(after_length),
            "normalized_seam_length_before": (float(seam_length_before / diagonal)
                                              if diagonal > 0.0 else 0.0),
            "normalized_seam_length_after": (float(after_length / diagonal)
                                             if diagonal > 0.0 else 0.0),
            "removable_remaining": remaining,
            "removed_edges": sorted(removed_edges),
            "history": records,
            "seams": set(seams),
            "measurement": measurement,
            "elapsed_s": float(clock() - started),
        }

    if mode == "repair" and not repair_mode:
        mode = "preserve"
        groups = removable_seam_groups(mesh, seams, constraints, required=required)
        history.append({
            "stage": "merge_back",
            "action": "skip",
            "reason": "skipped_quality_failed",
            "trial": 0,
            "edges": [],
            "accepted": False,
            "island_count_before": island_count_before,
            "island_count_after": island_count_before,
        })
        return result("skipped_quality_failed", True, len(groups))

    while True:
        groups = removable_seam_groups(mesh, seams, constraints, required=required)
        untried = [g for g in groups if frozenset(g["edges"]) not in tried]
        broken_islands = _broken_island_ids(measurement) if mode == "repair" else set()
        if mode == "repair":
            # CG10: in repair mode the point is to dissolve the islands the report is
            # actually complaining about, so a group touching a tiny / sliver island is
            # trialled before the merely-longest boundary. Same deterministic tiebreak.
            untried.sort(key=lambda row: (
                not ({int(row["island_a"]), int(row["island_b"])} & broken_islands),
                -round(float(row["shared_length"]), 9),
                int(row["island_a"]), int(row["island_b"])))

        if not untried:
            return result("no_removable_seam", True, 0)
        if trials >= limit:
            return result("trial_budget", False, len(untried))
        if budget_s is not None and float(clock() - started) >= budget_s:
            return result("time_budget", False, len(untried))

        group = untried[0]
        edges = frozenset(int(e) for e in group["edges"])
        before_count = int(measurement["island_count"])
        trial_started = clock()
        snapshot = take_snapshot(obj, mesh, seams)
        trial_seams = set(seams) - set(edges)

        touches_broken = bool({int(group["island_a"]), int(group["island_b"])}
                              & broken_islands)

        after: dict | None = None
        error: str | None = None
        accept = False
        verdict: str | None = None
        try:
            after = unwrap_and_measure(obj, mesh, trial_seams, profile=profile,
                                       margin=margin, stage="merge_back", regions=regions)
            # A merged layout that only fails the packing gaps is re-packed, never cut
            # back apart (G9/G11) — the accept rule below is unchanged.
            if (not after.get("passed", False)
                    and gap_only_failure(after.get("correctness") or {})):
                after = repack_for_gap(obj, mesh, trial_seams, profile=profile,
                                       pack_margin=margin, regions=regions,
                                       stage="merge_back_gap_repack")
            if mode == "repair":
                verdict = repair_trial_verdict(measurement, after,
                                               before_count=before_count,
                                               touches_broken=touches_broken)
                accept = verdict == "accepted_repair"
            else:
                accept = bool(after.get("passed", False)) and (
                    int(after["island_count"]) == before_count - 1)
        except Exception as exc:                               # noqa: BLE001 — reported
            error = str(exc)
            accept = False
        finally:
            # G5: restore on EVERY non-accepting path — rejected or raised.
            if not accept:
                restore_snapshot(obj, mesh, snapshot)

        trials += 1
        record: dict = {
            "trial": int(trials),
            "island_a": int(group["island_a"]),
            "island_b": int(group["island_b"]),
            "edges": sorted(edges),
            "shared_length": float(group["shared_length"]),
            "island_count_before": before_count,
            "gap_repack": (after or {}).get("gap_repack"),
            "mode": str(mode),
        }

        if accept and after is not None:
            seams = trial_seams
            measurement = after
            removed_edges |= set(edges)
            accepted += 1
            record.update({
                "accepted": True,
                "reason": "accepted" if mode == "preserve" else "accepted_repair",
                "hard_failures": (None if mode == "preserve"
                                  else list(after.get("hard_failures") or ())),
                "island_count_after": int(after["island_count"]),
            })
            action = "merge"
        else:
            tried.add(edges)
            if error is not None:
                reason = "exception"
                island_count_after = None
            elif verdict is not None:
                reason = verdict
                island_count_after = int(after["island_count"])
            elif not after.get("passed", False):
                reason = "quality_failed"
                island_count_after = int(after["island_count"])
            else:
                reason = "island_count_unchanged"
                island_count_after = int(after["island_count"])
            record.update({
                "accepted": False,
                "reason": reason,
                "hard_failures": (after or {}).get("hard_failures"),
                "island_count_after": island_count_after,
            })
            if error is not None:
                record["error"] = error
            action = "reject"

        record["elapsed_s"] = float(clock() - trial_started)
        records.append(record)
        history.append({
            "stage": "merge_back",
            "action": action,
            "reason": record["reason"],
            "trial": int(trials),
            "island_a": record["island_a"],
            "island_b": record["island_b"],
            "edges": record["edges"],
            "shared_length": record["shared_length"],
            "accepted": bool(record["accepted"]),
            "island_count_before": record["island_count_before"],
            "island_count_after": record["island_count_after"],
        })


__all__ = [
    "TRIAL_REASONS",
    "repair_trial_verdict",
    "merge_back_disabled_block",
    "removable_seam_groups",
    "run_merge_back",
]
