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
TRIAL_REASONS = ("accepted", "quality_failed", "island_count_unchanged", "exception")


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
                   initial_measurement: dict | None = None) -> dict:
    """Dissolve every seam that is not paying for itself, to a recorded end (G7/G12).

    Merge-back only runs on a layout that is ALREADY good: a failing input is reported as
    ``skipped_quality_failed`` with zero trials, because "which seams are unnecessary?" is
    not a meaningful question about a layout that does not meet the bar in the first place
    — the refinement loop's own termination reason is the story there.

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

    if not measurement.get("passed", False):
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

        after: dict | None = None
        error: str | None = None
        accept = False
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
        }

        if accept and after is not None:
            seams = trial_seams
            measurement = after
            removed_edges |= set(edges)
            accepted += 1
            record.update({
                "accepted": True,
                "reason": "accepted",
                "hard_failures": None,
                "island_count_after": int(after["island_count"]),
            })
            action = "merge"
        else:
            tried.add(edges)
            if error is not None:
                reason = "exception"
                island_count_after = None
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
    "merge_back_disabled_block",
    "removable_seam_groups",
    "run_merge_back",
]
