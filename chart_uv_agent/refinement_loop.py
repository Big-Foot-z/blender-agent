"""Distortion refinement loop: propose → apply → measure → accept / fully restore (§5).

Gates: **G5** (candidate acceptance, complete restore after a reject / exception, explicit
budget termination with a recorded reason, run-to-run determinism) and **G4** (the same
:class:`~chart_uv_agent.constraints.SeamConstraints` object that governs the initial
segmentation also governs every refinement cut, one distortion island per round, and a
correctness repair carries its own ``reason`` in the history).

The loop owns no policy of its own — it *composes* the decision cores that already exist:

``chart_uv_agent.candidates``      what cuts are even possible for an island
``chart_uv_agent.constraints``     whether a cut is allowed at all
``chart_uv_agent.quality_profile`` whether the measured result is good enough
``uv_agent.geometry.distortion_v2`` / ``uv_correctness``  the measurements themselves

Blender is reached ONLY through :mod:`chart_uv_agent.unwrap`, and always as a *module
attribute* (``unwrap_mod.unwrap_and_pack(...)``) resolved at call time, so the whole loop
runs off-Blender against ``tests.helpers.fake_blender_uv.FakeUnwrapBackend``. ``bpy`` is
never imported here.

Restore discipline (G5): every candidate trial is bracketed by
:func:`take_snapshot` / :func:`restore_snapshot`, and the restore runs in a ``finally``-
style unconditional path — a rejected candidate, a worse candidate and a candidate that
*raises* all leave the object with exactly the UVs and seams it had before the round.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from chart_uv_agent.candidates import (
    bbox_diagonal,
    generate_candidates,
    rank_key,
    seam_length,
)
from chart_uv_agent.quality_profile import (
    QualityProfile,
    accept_candidate,
    evaluate_quality,
    regression_within_budget,
)
from chart_uv_agent.segmentation import flood_charts, mandatory_seam_audit
from uv_agent.blender.organic_unwrap import AI_UV_LAYER, mark_seams
from uv_agent.geometry.distortion_v2 import evaluate_distortion_v2, per_face_anisotropy
from uv_agent.geometry.evaluation import mandatory_seam_uv_audit, uv_islands_from_uvmap
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap
from uv_agent.geometry.uv_correctness import evaluate_correctness

#: Budget keys resolved from the frozen profile, with the coercion each one needs.
_BUDGET_KEYS: tuple[tuple[str, type], ...] = (
    ("max_iterations", int),
    ("max_candidates_per_round", int),
    ("time_budget_s", float),
    ("island_cap", int),
    ("min_improvement_ratio", float),
    ("seed", int),
)

#: Which failing distortion metric a round goes after first (§5 ordering).
METRIC_PRIORITY: tuple[str, ...] = (
    "anisotropy_p95",
    "area_stretch_mean",
    "exceed_area_fraction",
    "anisotropy_max",
)

#: The regression target name used when the round is repairing correctness, not distortion.
CORRECTNESS_METRIC = "overlap_area_total"


# --------------------------------------------------------------------- budget


def resolve_budget(profile: QualityProfile, overrides: dict | None = None) -> dict:
    """The round budget: profile defaults with explicit non-``None`` overrides on top (G5).

    A key whose override is ``None`` falls back to the frozen profile — "not specified" and
    "specified as 0" are different things, and 0 is a legal budget (an immediate
    ``max_iterations`` termination is a valid, reportable outcome).
    """
    overrides = dict(overrides or {})
    out: dict = {}
    for key, cast in _BUDGET_KEYS:
        value = overrides.get(key)
        out[key] = cast(getattr(profile, key)) if value is None else cast(value)
    return out


# ------------------------------------------------------------------ snapshot


@dataclass
class UvSnapshot:
    """Everything one candidate trial may mutate (G5 "완전 복원")."""

    seams: frozenset[int]
    uvmap: UVMap
    layer_name: str = AI_UV_LAYER


def take_snapshot(obj, mesh: MeshGraph, seams) -> UvSnapshot:
    """Copy the current seam set + UV coordinates before a candidate is applied."""
    from chart_uv_agent import unwrap as unwrap_mod

    uvmap = unwrap_mod.read_uvmap(obj, mesh)
    return UvSnapshot(seams=frozenset(int(e) for e in seams), uvmap=uvmap.copy(),
                      layer_name=AI_UV_LAYER)


def restore_snapshot(obj, mesh: MeshGraph, snap: UvSnapshot) -> set[int]:
    """Put the UVs, the active UV layer and the marked seams back exactly (G5).

    ``mark_seams`` is only meaningful on a real Blender mesh; an off-Blender stand-in has
    no ``data.edges``, so the seam state there lives purely in the returned set.
    """
    from chart_uv_agent import unwrap as unwrap_mod

    unwrap_mod.write_uvmap(obj, mesh, snap.uvmap, layer_name=snap.layer_name)
    if hasattr(getattr(obj, "data", None), "edges"):
        mark_seams(obj, set(snap.seams))
    return set(snap.seams)


# --------------------------------------------------------------- measurement


def measure_layout(obj, mesh: MeshGraph, seams, *, profile: QualityProfile,
                   stage: str, regions: dict | None = None) -> dict:
    """Measure the UVs that are ALREADY on ``obj`` — this never unwraps (G3/G5).

    Islands come from the seam flood (``flood_charts``), which is what the loop reasons
    about, but the UV-connectivity island count is measured too and any disagreement is
    surfaced as ``islands_disagree`` rather than being papered over: a seam set that does
    not match the layout actually written is a real defect, not a rounding detail.
    """
    from chart_uv_agent import unwrap as unwrap_mod

    seams = {int(e) for e in seams}
    uvmap = unwrap_mod.read_uvmap(obj, mesh)
    islands = flood_charts(mesh, seams)
    uv_islands = uv_islands_from_uvmap(mesh, uvmap)

    distortion = evaluate_distortion_v2(
        mesh, uvmap, islands, stage=stage,
        exceed_basis=profile.exceed_basis_anisotropy, regions=regions,
    )
    correctness = evaluate_correctness(
        mesh, uvmap, islands,
        texture_size_px=profile.texture_size_px, margin_px=profile.margin_px,
    )
    quality = evaluate_quality(profile, distortion)

    seam_audit = mandatory_seam_audit(mesh, seams)
    uv_audit = mandatory_seam_uv_audit(mesh, uvmap)
    mandatory_audit = {
        "mandatory_90_edges": int(seam_audit["mandatory_90_edges"]),
        "mandatory_90_missing": int(seam_audit["mandatory_90_missing"]),
        "mandatory_90_fold_edges": int(uv_audit["mandatory_90_fold_edges"]),
        "mandatory_90_uv_unsplit": int(uv_audit["mandatory_90_uv_unsplit"]),
        "uv_unsplit_edge_ids": list(uv_audit["uv_unsplit_edge_ids"]),
    }

    face_aniso = per_face_anisotropy(mesh, uvmap)
    face_aniso = np.nan_to_num(np.asarray(face_aniso, dtype=float), nan=0.0,
                               posinf=0.0, neginf=0.0)

    passed = bool(
        quality.get("passed", False)
        and correctness.get("passed", False)
        and mandatory_audit["mandatory_90_missing"] == 0
        and mandatory_audit["mandatory_90_uv_unsplit"] == 0
    )

    return {
        "uvmap": uvmap,
        "islands": islands,
        "island_count": len(islands),
        "uv_island_count": len(uv_islands),
        "islands_disagree": bool(len(islands) != len(uv_islands)),
        "distortion_v2": distortion,
        "correctness": correctness,
        "quality": quality,
        "mandatory_audit": mandatory_audit,
        "passed": passed,
        "face_anisotropy": face_aniso,
    }


def unwrap_and_measure(obj, mesh: MeshGraph, seams, *, profile: QualityProfile,
                       margin: float, stage: str, regions: dict | None = None) -> dict:
    """Apply ``seams``, unwrap/pack, then measure the result."""
    from chart_uv_agent import unwrap as unwrap_mod

    seams = {int(e) for e in seams}
    unwrap_mod.unwrap_and_pack(obj, seams, margin=margin)
    return measure_layout(obj, mesh, seams, profile=profile, stage=stage, regions=regions)


def _region_metric(mesh: MeshGraph, uvmap: UVMap, faces, metric: str,
                   profile: QualityProfile) -> float:
    """The v2 value of ``metric`` over exactly ``faces``, measured as its own scope.

    The target island of a round is a FACE SET, not an island id: after a cut the island
    ids renumber, so comparing ``before`` and ``after`` by island id would silently compare
    two different regions. Measuring the same face set on both layouts is what makes the
    15% improvement number mean anything (G5).
    """
    face_ids = sorted(int(f) for f in faces)
    if not face_ids:
        return float("nan")
    report = evaluate_distortion_v2(
        mesh, uvmap, [face_ids], stage="candidate",
        exceed_basis=profile.exceed_basis_anisotropy,
    )
    rows = report.get("islands") or []
    if not rows:
        return float("nan")
    return float(rows[0].get(metric, float("nan")))


def _correctness_value(measurement: dict) -> float:
    """The single number a ``correctness_repair`` round tries to drive down."""
    corr = measurement.get("correctness") or {}
    overlap = float((corr.get("overlap") or {}).get("overlap_area_total", 0.0))
    flip = float((corr.get("orientation") or {}).get("local_flip_area", 0.0))
    return overlap + flip


def _correctness_counters(measurement: dict) -> tuple[float, int, int]:
    corr = measurement.get("correctness") or {}
    return (
        float((corr.get("overlap") or {}).get("overlap_area_total", 0.0)),
        int((corr.get("orientation") or {}).get("local_flip_count", 0)),
        int((corr.get("degenerate") or {}).get("uv_degenerate_count", 0)),
    )


# ------------------------------------------------------------ target picking


def select_target(measurement: dict, rejected_regions: set, profile: QualityProfile):
    """The ONE island this round works on, or ``None`` when there is nothing to do (G4).

    Priority: a failing per-island distortion check (worst value of the highest-priority
    failing metric) → a failing global check, blamed on its worst island → a correctness
    repair. Exactly one island per round, and a ``correctness_repair`` is tagged as such so
    the history can tell it apart from a distortion split.

    A face set already in ``rejected_regions`` (a region whose candidates all failed once)
    is skipped, so the loop moves on instead of re-cutting the same place forever.
    """
    islands = measurement.get("islands") or []
    rejected = {frozenset(int(f) for f in r) for r in (rejected_regions or ())}

    def faces_of(island_id: int) -> frozenset[int]:
        if 0 <= island_id < len(islands):
            return frozenset(int(f) for f in islands[island_id])
        return frozenset()

    def target(kind: str, island_id: int, metric: str, before: float):
        faces = faces_of(island_id)
        if not faces or faces in rejected:
            return None
        return {"kind": kind, "island_id": int(island_id), "faces": faces,
                "metric": metric, "before": float(before)}

    quality = measurement.get("quality") or {}
    failed = [c for c in (quality.get("checks") or []) if not c.get("passed", True)]

    # (a) failing per-island checks, highest-priority metric first, worst island first.
    island_failed = [c for c in failed if c.get("scope") == "island"]
    for metric in METRIC_PRIORITY:
        rows = sorted((c for c in island_failed if c.get("name") == metric),
                      key=lambda c: (-float(c.get("value", 0.0)), int(c.get("island_id", 0))))
        for check in rows:
            found = target("distortion", int(check.get("island_id", 0)), metric,
                           float(check.get("value", 0.0)))
            if found is not None:
                return found

    # (b) only global checks fail — blame the island with the worst value of that metric.
    global_failed = {c.get("name") for c in failed if c.get("scope") == "global"}
    if global_failed:
        rows = (measurement.get("distortion_v2") or {}).get("islands") or []
        metrics = [m for m in METRIC_PRIORITY if m in global_failed] or ["anisotropy_p95"]
        for metric in metrics:
            ordered = sorted(rows, key=lambda r: (-float(r.get(metric, 0.0)),
                                                  int(r.get("island_id", 0))))
            for row in ordered:
                found = target("distortion", int(row.get("island_id", 0)), metric,
                               float(row.get(metric, 0.0)))
                if found is not None:
                    return found

    # (c) distortion is satisfied (or exhausted) but the layout is not a valid packing.
    correctness = measurement.get("correctness") or {}
    if not correctness.get("passed", True):
        before = _correctness_value(measurement)
        island_ids: list[int] = []
        for sample in (correctness.get("overlap") or {}).get("samples") or []:
            for key in ("island_a", "island_b"):
                value = sample.get(key)
                if isinstance(value, int) and value >= 0 and value not in island_ids:
                    island_ids.append(int(value))
        flip_faces = (correctness.get("orientation") or {}).get("local_flip_face_ids") or []
        if flip_faces:
            lookup: dict[int, int] = {}
            for index, faces in enumerate(islands):
                for fid in faces:
                    lookup[int(fid)] = index
            for fid in flip_faces:
                index = lookup.get(int(fid))
                if index is not None and index not in island_ids:
                    island_ids.append(index)
        for island_id in island_ids:
            found = target("correctness_repair", island_id, CORRECTNESS_METRIC, before)
            if found is not None:
                return found

    # (d) nothing left to target.
    return None


# --------------------------------------------------------- candidate trial


def evaluate_candidate(obj, mesh: MeshGraph, seams, cand, *, target: dict, before: dict,
                       constraints, profile: QualityProfile, budget: dict, margin: float,
                       regions: dict | None = None) -> dict:
    """Trial ``cand``: apply, unwrap, measure, and judge it against the profile (G5).

    This function NEVER restores — the caller owns the snapshot, because the restore must
    also happen on the paths this function cannot reach (a raise inside the backend, a
    cancel between calls). A constraint violation short-circuits before any unwrap: an
    illegal cut is not worth a measurement, and G4 counts "protected edge 절개" at zero,
    not "measured then discarded".
    """
    started = time.monotonic()
    record: dict = {
        "candidate": cand.to_dict(),
        "kind": cand.kind,
        "target_island": int(target.get("island_id", -1)),
        "target_metric": str(target.get("metric", "")),
        "aux_length": seam_length(mesh, cand.added_edges),
        "exposure": float(cand.exposure_cost),
    }

    check = constraints.check_added(cand.added_edges)
    if not check["ok"]:
        record.update({
            "accepted": False,
            "reason": "constraint_violation",
            "improvement_ratio": 0.0,
            "protected_cut": list(check["protected_cut"]),
            "after_measurement": None,
            "island_count_after": None,
            "elapsed_s": time.monotonic() - started,
        })
        return record

    try:
        trial_seams = {int(e) for e in seams} | {int(e) for e in cand.added_edges}
        after = unwrap_and_measure(obj, mesh, trial_seams, profile=profile, margin=margin,
                                   stage="candidate", regions=regions)

        if target.get("kind") == "correctness_repair":
            metric = CORRECTNESS_METRIC
            before_value = _correctness_value(before)
            after_value = _correctness_value(after)
        else:
            metric = str(target.get("metric") or "anisotropy_p95")
            before_value = _region_metric(mesh, before["uvmap"], target["faces"], metric, profile)
            after_value = _region_metric(mesh, after["uvmap"], target["faces"], metric, profile)

        # Correctness may not get worse. A run that was ALREADY broken is allowed to stay
        # broken while a distortion split makes progress, but never to deteriorate.
        if after["correctness"].get("passed", False):
            correctness_ok = True
        elif before["correctness"].get("passed", False):
            correctness_ok = False
        else:
            b_overlap, b_flip, b_degen = _correctness_counters(before)
            a_overlap, a_flip, a_degen = _correctness_counters(after)
            correctness_ok = (a_overlap <= b_overlap + 1e-12
                              and a_flip <= b_flip
                              and a_degen <= b_degen)

        regression = regression_within_budget(
            profile,
            (before.get("distortion_v2") or {}).get("global") or {},
            (after.get("distortion_v2") or {}).get("global") or {},
            target=metric,
        )

        mandatory_ok = (
            after["mandatory_audit"]["mandatory_90_missing"] == 0
            and after["mandatory_audit"]["mandatory_90_uv_unsplit"]
            <= before["mandatory_audit"]["mandatory_90_uv_unsplit"]
        )

        verdict = accept_candidate(
            profile,
            target_before=before_value,
            target_after=after_value,
            quality_after_passed=bool(after["passed"]),
            correctness_ok=bool(correctness_ok and mandatory_ok),
            constraints_ok=True,
            regression_ok=bool(regression["ok"]),
        )
        record.update(verdict)
        record.update({
            "after_measurement": after,
            "island_count_after": int(after["island_count"]),
            "target_before": float(before_value),
            "target_after": float(after_value),
            "correctness_ok": bool(correctness_ok),
            "mandatory_ok": bool(mandatory_ok),
            "regression_ok": bool(regression["ok"]),
            "regression_violations": list(regression["violations"]),
            "quality_after_passed": bool(after["passed"]),
            "elapsed_s": time.monotonic() - started,
        })
        return record
    except Exception as exc:                                   # noqa: BLE001 — reported
        record.update({
            "accepted": False,
            "reason": "candidate_exception",
            "error": str(exc),
            "improvement_ratio": 0.0,
            "after_measurement": None,
            "island_count_after": None,
            "elapsed_s": time.monotonic() - started,
        })
        return record


def _choice_key(record: dict, cand) -> tuple:
    """Deterministic tie-break for candidates of equal standing (§5 / G4)."""
    island_count = record.get("island_count_after")
    if island_count is None:
        island_count = 1 << 30
    return rank_key(island_count, record.get("aux_length", 0.0), record.get("exposure", 0.0))


# --------------------------------------------------------------------- loop


def run_refinement(obj, mesh: MeshGraph, seams: set[int], *, constraints,
                   profile: QualityProfile, budget: dict | None = None,
                   margin: float = 0.005, regions: dict | None = None,
                   history: list | None = None, candidate_history: list | None = None,
                   clock=time.monotonic, initial_measurement: dict | None = None) -> dict:
    """Run the distortion refinement loop to a recorded termination (G4/G5).

    One round = pick one island → generate its candidates → trial each (restoring after
    every single one) → keep at most ONE. A candidate that already meets the full quality
    bar wins on ``rank_key`` (fewest islands, then shortest auxiliary seam, then least
    visible); with no such candidate the best *accepted* partial improvement wins. When no
    candidate is accepted at all the region is marked rejected and the loop moves on, so a
    stubborn island can never consume the whole budget.

    Termination is always explicit — ``quality_passed``, ``max_iterations``,
    ``time_budget``, ``island_cap``, ``no_failing_target`` or ``no_improving_candidate`` —
    and the returned ``measurement`` is always the measurement of the returned ``seams``.
    """
    budget = resolve_budget(profile, budget)
    history = history if history is not None else []
    candidate_history = candidate_history if candidate_history is not None else []
    seams = {int(e) for e in seams}
    distortion_seams: set[int] = set()
    rejected_regions: set[frozenset] = set()

    started = clock()
    iterations = 0
    candidates_evaluated = 0

    measurement = initial_measurement
    if measurement is None:
        measurement = unwrap_and_measure(obj, mesh, seams, profile=profile, margin=margin,
                                         stage="refinement", regions=regions)

    reason = "no_failing_target"
    while True:
        if measurement["passed"]:
            reason = "quality_passed"
            break
        if iterations >= budget["max_iterations"]:
            reason = "max_iterations"
            break
        if float(clock() - started) >= budget["time_budget_s"]:
            reason = "time_budget"
            break
        if measurement["island_count"] >= budget["island_cap"]:
            reason = "island_cap"
            break

        target = select_target(measurement, rejected_regions, profile)
        if target is None:
            reason = "no_improving_candidate" if rejected_regions else "no_failing_target"
            break

        snapshot = take_snapshot(obj, mesh, seams)
        cands = generate_candidates(
            mesh, measurement["islands"], target["island_id"], seams, constraints,
            measurement["face_anisotropy"],
            max_candidates=budget["max_candidates_per_round"],
        )

        trials: list[tuple] = []
        for cand in cands:
            if cand.rejected:
                candidate_history.append({
                    "round": iterations,
                    "candidate": cand.to_dict(),
                    "kind": cand.kind,
                    "target_island": int(target["island_id"]),
                    "target_metric": str(target["metric"]),
                    "accepted": False,
                    "reason": str(cand.rejected),
                    "improvement_ratio": 0.0,
                    "after_measurement": None,
                    "island_count_after": None,
                    "aux_length": seam_length(mesh, cand.added_edges),
                    "exposure": float(cand.exposure_cost),
                    "elapsed_s": 0.0,
                })
                continue
            try:
                record = evaluate_candidate(
                    obj, mesh, seams, cand, target=target, before=measurement,
                    constraints=constraints, profile=profile, budget=budget,
                    margin=margin, regions=regions,
                )
            finally:
                # G5: restore on EVERY path — accepted, rejected, or raised.
                restore_snapshot(obj, mesh, snapshot)
            record["round"] = iterations
            candidates_evaluated += 1
            candidate_history.append(record)
            trials.append((cand, record))

        accepted = [(c, r) for c, r in trials if r.get("accepted")]
        passing = [(c, r) for c, r in accepted if r.get("quality_after_passed")]
        chosen = None
        if passing:
            chosen = min(passing, key=lambda pair: _choice_key(pair[1], pair[0]))
        elif accepted:
            chosen = max(
                accepted,
                key=lambda pair: (round(float(pair[1].get("improvement_ratio", 0.0)), 9),
                                  tuple(-v for v in _choice_key(pair[1], pair[0]))),
            )

        if chosen is None:
            rejected_regions.add(target["faces"])
            history.append({
                "round": iterations,
                "stage": "refinement",
                "action": "reject_region",
                "reason": target["kind"],
                "target_island": int(target["island_id"]),
                "target_metric": str(target["metric"]),
                "before": float(target["before"]),
                "after": float(target["before"]),
                "improvement_ratio": 0.0,
                "added_edges": [],
                "candidate_kind": None,
                "candidates_evaluated": len(trials),
            })
            iterations += 1
            continue

        cand, record = chosen
        added = sorted(int(e) for e in cand.added_edges)
        seams |= set(added)
        distortion_seams |= set(added)
        measurement = unwrap_and_measure(obj, mesh, seams, profile=profile, margin=margin,
                                         stage="refinement", regions=regions)
        history.append({
            "round": iterations,
            "stage": "refinement",
            "action": "split" if added else "unwrap_only",
            "reason": target["kind"],
            "target_island": int(target["island_id"]),
            "target_metric": str(target["metric"]),
            "before": float(record.get("target_before", target["before"])),
            "after": float(record.get("target_after", target["before"])),
            "improvement_ratio": float(record.get("improvement_ratio", 0.0)),
            "added_edges": added,
            "candidate_kind": cand.kind,
            "candidates_evaluated": len(trials),
        })
        iterations += 1

    return {
        "seams": set(seams),
        "distortion_seams": set(distortion_seams),
        "measurement": measurement,
        "termination": {
            "reason": reason,
            "iterations": int(iterations),
            "candidates_evaluated": int(candidates_evaluated),
            "elapsed_s": float(clock() - started),
            "budget": dict(budget),
        },
        "rejected_regions": [sorted(int(f) for f in region)
                             for region in sorted(rejected_regions, key=sorted)],
        "history": history,
        "candidate_history": candidate_history,
        "passed": bool(measurement["passed"]),
    }


# ------------------------------------------------------------------ reports


def seam_length_report(mesh: MeshGraph, seams, *, mandatory, user, distortion_seams) -> dict:
    """Seam length split by WHO asked for it (G8 evidence).

    ``auxiliary`` is what the engine added on its own — ``seams − mandatory − user`` — and
    is the number a reviewer judges the loop by; it is also reported normalised by the
    model's bounding-box diagonal so it is comparable across models of different scale.
    ``distortion_seams`` is accepted for call-site symmetry: those edges are already inside
    ``seams`` and are counted in ``auxiliary``.
    """
    seam_set = {int(e) for e in seams}
    mandatory_set = {int(e) for e in mandatory} & seam_set
    user_set = ({int(e) for e in user} & seam_set) - mandatory_set
    auxiliary = seam_set - mandatory_set - user_set

    diagonal = bbox_diagonal(mesh)
    auxiliary_length = seam_length(mesh, auxiliary)
    return {
        "mandatory": seam_length(mesh, mandatory_set),
        "user": seam_length(mesh, user_set),
        "auxiliary": auxiliary_length,
        "total": seam_length(mesh, seam_set),
        "bbox_diagonal": diagonal,
        "auxiliary_normalized": (auxiliary_length / diagonal) if diagonal > 0.0 else 0.0,
        "mandatory_edge_count": len(mandatory_set),
        "user_edge_count": len(user_set),
        "auxiliary_edge_count": len(auxiliary),
    }


__all__ = [
    "CORRECTNESS_METRIC",
    "METRIC_PRIORITY",
    "UvSnapshot",
    "evaluate_candidate",
    "measure_layout",
    "resolve_budget",
    "restore_snapshot",
    "run_refinement",
    "seam_length_report",
    "select_target",
    "take_snapshot",
    "unwrap_and_measure",
]
