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
from uv_agent.geometry.catastrophic_distortion import (
    catastrophic_counters,
    evaluate_catastrophic,
    thresholds_from_profile,
)
from uv_agent.geometry.distortion_v2 import evaluate_distortion_v2, per_face_anisotropy
from uv_agent.geometry.evaluation import mandatory_seam_uv_audit, uv_islands_from_uvmap
from uv_agent.geometry.fragmentation import evaluate_fragmentation
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.mesh_identity import uv_hash
from uv_agent.geometry.solution import UVMap
from uv_agent.geometry.texel_density import evaluate_texel_density
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

#: The pseudo-metric name a ``catastrophic_repair`` round carries (CG2/CG5): the target is a
#: bad REGION, not a distortion metric, so it names itself rather than borrowing a v2 key.
CATASTROPHIC_METRIC = "catastrophic_score"

#: Which catastrophic reason a repair round goes after first (plan §7 ordering) — a
#: collapsed/invalid triangle is unrecoverable damage, an area explosion is merely ugly.
CATASTROPHIC_REASON_PRIORITY: tuple[tuple[str, ...], ...] = (
    ("near_collapse", "invalid"),
    ("local_flip",),
    ("self_overlap",),
    ("anisotropy_hard",),
    ("needle",),
    ("area_explosion", "area_collapse"),
)


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
    """Everything one candidate trial may mutate (G5 "완전 복원" / CG7 rollback integrity).

    ``uv_hash`` is the EXACT (unrounded) digest of the UVs at snapshot time — it is what
    turns "we called write_uvmap" into "the layout really came back". ``catastrophic_counters``
    and ``island_count`` travel with it so a rollback record can state what was rolled back
    to without re-measuring.
    """

    seams: frozenset[int]
    uvmap: UVMap
    layer_name: str = AI_UV_LAYER
    uv_hash: str = ""
    catastrophic_counters: tuple | None = None
    island_count: int | None = None


def take_snapshot(obj, mesh: MeshGraph, seams, measurement: dict | None = None) -> UvSnapshot:
    """Copy the current seam set + UV coordinates before a candidate is applied.

    ``measurement`` (when given) is the measurement of exactly these UVs; its catastrophic
    counters and island count are carried on the snapshot as the CG7 rollback baseline.
    """
    from chart_uv_agent import unwrap as unwrap_mod

    uvmap = unwrap_mod.read_uvmap(obj, mesh)
    counters = None
    island_count = None
    if measurement is not None:
        report = measurement.get("catastrophic")
        if isinstance(report, dict):
            counters = catastrophic_counters(report)
        raw = measurement.get("island_count")
        if raw is not None:
            island_count = int(raw)
    return UvSnapshot(seams=frozenset(int(e) for e in seams), uvmap=uvmap.copy(),
                      layer_name=AI_UV_LAYER, uv_hash=uv_hash(uvmap),
                      catastrophic_counters=counters, island_count=island_count)


def restore_snapshot(obj, mesh: MeshGraph, snap: UvSnapshot) -> set[int]:
    """Put the UVs, the active UV layer and the marked seams back exactly (G5 / CG7).

    ``mark_seams`` is only meaningful on a real Blender mesh; an off-Blender stand-in has
    no ``data.edges``, so the seam state there lives purely in the returned set.

    The write is VERIFIED: the UVs are read back and hashed, and a digest that does not
    match the snapshot raises ``RuntimeError("snapshot_restore_mismatch")``. A restore that
    does not restore is a bug, and a silent one would let a rejected candidate's UVs ship.
    """
    from chart_uv_agent import unwrap as unwrap_mod

    unwrap_mod.write_uvmap(obj, mesh, snap.uvmap, layer_name=snap.layer_name)
    if hasattr(getattr(obj, "data", None), "edges"):
        mark_seams(obj, set(snap.seams))
    if snap.uv_hash:
        after = uv_hash(unwrap_mod.read_uvmap(obj, mesh))
        if after != snap.uv_hash:
            raise RuntimeError("snapshot_restore_mismatch")
    return set(snap.seams)


# ------------------------------------------------------------- border inset

#: UV-space slack when deciding "already inside the border" — keeps
#: :func:`ensure_border_margin` idempotent against float round-off without eating into
#: the pixel padding (1e-12 UV ≈ 1e-9 px at 1024).
_BORDER_INSET_TOL = 1e-12


def ensure_border_margin(obj, mesh: MeshGraph, *, profile: QualityProfile) -> dict:
    """Move the whole layout inside the tile's ``border_margin_px`` padding (G9/G11).

    A packer that places islands flush against ``u=0``/``u=1``/``v=0``/``v=1`` produces a
    layout that bleeds across the tile seam at the coarser mips. That is a PLACEMENT
    defect, never a reason to cut: this repair is ONE uniform similarity transform of
    every UV — scale ``s = min(1, (1 - 2b) / max(extent_u, extent_v))`` about the UV
    bounding-box centre, then a translation that centres the box in the tile. Uniform
    means every distortion, anisotropy and texel-density RATIO is unchanged; only the
    absolute UV scale (and therefore the texel density mean) moves.

    Idempotent: a layout already inside ``[b, 1-b]`` is left byte-identical and the report
    says ``applied: False``.
    """
    from chart_uv_agent import unwrap as unwrap_mod

    b = float(profile.border_margin_px) / float(max(1, int(profile.texture_size_px)))
    report = {
        "applied": False,
        "scale": 1.0,
        "translation": [0.0, 0.0],
        "bbox_before": None,
        "bbox_after": None,
        "border_margin_px": float(profile.border_margin_px),
        "border_margin_uv": float(b),
    }

    uvmap = unwrap_mod.read_uvmap(obj, mesh)
    uv = np.asarray(uvmap.uv, dtype=float)
    if uv.size == 0:
        return report
    finite = np.all(np.isfinite(uv), axis=1)
    if not bool(finite.any()):
        return report

    pts = uv[finite]
    lo = pts.min(axis=0)
    hi = pts.max(axis=0)
    bbox_before = [float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])]
    report["bbox_before"] = bbox_before
    report["bbox_after"] = list(bbox_before)

    tol = _BORDER_INSET_TOL
    inside = (float(lo[0]) >= b - tol and float(lo[1]) >= b - tol
              and float(hi[0]) <= 1.0 - b + tol and float(hi[1]) <= 1.0 - b + tol)
    if inside:
        return report

    extent = hi - lo
    usable = max(0.0, 1.0 - 2.0 * b)
    widest = float(max(extent[0], extent[1]))
    scale = 1.0 if widest <= 0.0 else float(min(1.0, usable / widest))
    centre = (lo + hi) / 2.0
    translation = [0.5 - float(centre[0]), 0.5 - float(centre[1])]

    moved = uv.copy()
    moved[finite] = (pts - centre) * scale + np.array([0.5, 0.5], dtype=float)
    uvmap.uv[:] = moved
    unwrap_mod.write_uvmap(obj, mesh, uvmap, layer_name=AI_UV_LAYER)

    pts_after = moved[finite]
    lo2 = pts_after.min(axis=0)
    hi2 = pts_after.max(axis=0)
    report.update({
        "applied": True,
        "scale": float(scale),
        "translation": [float(translation[0]), float(translation[1])],
        "bbox_after": [float(lo2[0]), float(lo2[1]), float(hi2[0]), float(hi2[1])],
    })
    return report


# --------------------------------------------------------------- measurement


def _packing_efficiency(mesh: MeshGraph, uvmap: UVMap, *, limit: float) -> dict:
    """Fraction of the layout's own UV bounding box the islands actually cover (G11).

    ADVISORY only: a low number is a packing-quality remark, never a hard failure — a
    layout can be perfectly correct and simply loose."""
    uv = np.asarray(uvmap.uv, dtype=float)
    total = 0.0
    for face in mesh.faces:
        for l0, l1, l2 in mesh.face_triangles(int(face.id)):
            a, b_, c = uv[l0], uv[l1], uv[l2]
            if not (np.all(np.isfinite(a)) and np.all(np.isfinite(b_))
                    and np.all(np.isfinite(c))):
                continue
            total += abs(0.5 * ((b_[0] - a[0]) * (c[1] - a[1])
                                - (c[0] - a[0]) * (b_[1] - a[1])))

    efficiency = 0.0
    if uv.size:
        finite = np.all(np.isfinite(uv), axis=1)
        if bool(finite.any()):
            pts = uv[finite]
            span = pts.max(axis=0) - pts.min(axis=0)
            bbox_area = float(span[0]) * float(span[1])
            if bbox_area > 1e-18:
                efficiency = float(min(1.0, max(0.0, total / bbox_area)))
    return {
        "efficiency": float(efficiency),
        "limit": float(limit),
        "passed": bool(efficiency >= float(limit)),
        "advisory": True,
        "uv_area_total": float(total),
    }


def measure_layout(obj, mesh: MeshGraph, seams, *, profile: QualityProfile,
                   stage: str, regions: dict | None = None) -> dict:
    """Measure the UVs that are ALREADY on ``obj`` — this never unwraps (G3/G5).

    Islands come from the seam flood (``flood_charts``), which is what the loop reasons
    about, but the UV-connectivity island count is measured too and any disagreement is
    surfaced as ``islands_disagree`` rather than being papered over: a seam set that does
    not match the layout actually written is a real defect, not a rounding detail.

    It never unwraps and never touches a seam, but it may apply ONE uniform border-inset
    transform first (:func:`ensure_border_margin`, G9/G11): tile padding is a placement
    problem, so it is repaired in place before the layout is judged, and the repair is
    reported under ``border_inset``.
    """
    from chart_uv_agent import unwrap as unwrap_mod

    seams = {int(e) for e in seams}
    border_inset = ensure_border_margin(obj, mesh, profile=profile)
    uvmap = unwrap_mod.read_uvmap(obj, mesh)
    islands = flood_charts(mesh, seams)
    uv_islands = uv_islands_from_uvmap(mesh, uvmap)

    distortion = evaluate_distortion_v2(
        mesh, uvmap, islands, stage=stage,
        exceed_basis=profile.bad_area_threshold, regions=regions,
    )
    correctness = evaluate_correctness(
        mesh, uvmap, islands,
        texture_size_px=profile.texture_size_px, margin_px=profile.margin_px,
        border_margin_px=profile.border_margin_px,
    )
    quality = evaluate_quality(profile, distortion)

    # CG2/CG3: the HARD catastrophic gate, measured next to (never inside) the quality
    # caps. The overlap / flip face ids come from the correctness report above so the two
    # detectors agree instead of each guessing; they only set region flags.
    overlap_face_ids: list[int] = []
    for sample in (correctness.get("overlap") or {}).get("samples") or ():
        for key in ("face_a", "face_b"):
            value = sample.get(key)
            if isinstance(value, int) and value >= 0:
                overlap_face_ids.append(int(value))
    flip_face_ids = list((correctness.get("orientation") or {})
                         .get("local_flip_face_ids") or ())
    catastrophic = evaluate_catastrophic(
        mesh, uvmap, islands,
        thresholds=thresholds_from_profile(profile),
        overlap_face_ids=sorted(set(overlap_face_ids)),
        flip_face_ids=sorted(set(int(f) for f in flip_face_ids)),
    )

    fragmentation = evaluate_fragmentation(
        mesh, uvmap, islands, seams,
        min_island_uv_area=profile.min_island_uv_area,
        tiny_island_uv_area=profile.tiny_island_uv_area,
        tiny_island_count_max=profile.tiny_island_count_max,
        tiny_island_area_ratio_max=profile.tiny_island_area_ratio_max,
        sliver_aspect_min=profile.sliver_aspect_min,
        sliver_uv_area_max=profile.sliver_uv_area_max,
        sliver_island_count_max=profile.sliver_island_count_max,
        island_aspect_p95_max=profile.island_aspect_p95_max,
        # CG8: the same island hygiene bar expressed in PIXELS at the profile's texture size.
        texture_size_px=profile.texture_size_px,
        min_island_width_px=profile.min_island_width_px,
        min_island_area_px2=profile.min_island_area_px2,
        max_island_bbox_aspect=profile.max_island_bbox_aspect,
        max_island_perimeter_area_ratio=profile.max_island_perimeter_area_ratio,
        max_tiny_island_area_fraction=profile.max_tiny_island_area_fraction,
    )
    texel_density = evaluate_texel_density(
        mesh, uvmap, islands,
        texture_size_px=profile.texture_size_px,
        cv_max=profile.texel_density_cv_max,
        outlier_tolerance=profile.texel_density_outlier_tolerance,
        outlier_count_max=profile.texel_density_outlier_count_max,
    )
    packing = _packing_efficiency(mesh, uvmap, limit=profile.packing_efficiency_min)

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
    # Candidate SEEDING needs a finite array (nan_to_num above); REPORTING must not lie
    # about an unmeasurable face, so the raw per-face score (NaN/None preserved) ships too.
    face_score_raw = list(catastrophic.get("per_face_score") or ())

    islands_disagree = bool(len(islands) != len(uv_islands))

    hard_failures: list[str] = []
    if not (quality.get("passed", False) and quality.get("valid", False)):
        hard_failures.append("quality_profile_failed")
    if not correctness.get("passed", False):
        hard_failures.append("correctness_failed")
    if not (catastrophic.get("passed", False) and catastrophic.get("valid", False)):
        hard_failures.append("catastrophic_failed")
    if mandatory_audit["mandatory_90_missing"] != 0:
        hard_failures.append("mandatory_90_missing")
    if mandatory_audit["mandatory_90_uv_unsplit"] != 0:
        hard_failures.append("mandatory_90_uv_unsplit")
    if not (fragmentation.get("passed", False) and fragmentation.get("valid", False)):
        hard_failures.append("fragmentation_failed")
    if not (texel_density.get("passed", False) and texel_density.get("valid", False)):
        hard_failures.append("texel_density_failed")
    if islands_disagree:
        hard_failures.append("island_connectivity_mismatch")

    quality_failures = list(fragmentation.get("quality_failures") or [])
    if not packing["passed"]:
        quality_failures.append("packing_efficiency")

    passed = not hard_failures

    return {
        "uvmap": uvmap,
        "islands": islands,
        "island_count": len(islands),
        "uv_island_count": len(uv_islands),
        "islands_disagree": islands_disagree,
        "uv_hash": uv_hash(uvmap),
        "distortion_v2": distortion,
        "correctness": correctness,
        "catastrophic": catastrophic,
        "quality": quality,
        "mandatory_audit": mandatory_audit,
        "fragmentation": fragmentation,
        "texel_density": texel_density,
        "packing": packing,
        "border_inset": border_inset,
        "hard_failures": hard_failures,
        "quality_failures": quality_failures,
        "passed": passed,
        "face_anisotropy": face_aniso,
        "face_score_raw": face_score_raw,
    }


# ------------------------------------------------------- layout recipe (CG14/CG0)


def override_from_variant(faces, variant: dict, *, round_index: int,
                          region_id=None) -> dict:
    """One :data:`UnwrapOverride` row: WHICH faces were re-unwrapped and HOW (CG14).

    An accepted R1 repair changes only the UVs, so every later ``unwrap_and_pack`` of the
    same seam set would silently throw it away. The recipe is what makes the repair
    reproducible: replay it after each unwrap and the layout comes back.
    """
    method = str(variant.get("method", "MINIMUM_STRETCH"))
    raw_region = region_id if region_id is not None else variant.get("region_id")
    return {
        "faces": sorted(int(f) for f in faces),
        "variant": {
            "id": str(variant.get("variant_id", variant.get("id", method))),
            "method": method,
            "iterations": variant.get("iterations"),
            "no_flip": bool(variant.get("no_flip", False)),
            "fill_holes": bool(variant.get("fill_holes", False)),
            "minimize_iters": int(variant.get("minimize_iters", 0) or 0),
        },
        "round": int(round_index),
        "region_id": (None if raw_region is None else int(raw_region)),
    }


def apply_unwrap_overrides(obj, mesh: MeshGraph, seams, overrides, *,
                           margin: float) -> dict:
    """Replay the layout recipe onto the UVs that are currently on ``obj`` (CG14/CG0).

    An override is replayed ONLY when its face set is still exactly one flood chart of
    ``seams``: the recipe describes a re-unwrap of a whole island, and if that island has
    since grown (a merge-back) or shrunk (a new cut) the recorded solver settings no longer
    describe the same thing. Such a row is skipped and reported as ``stale`` rather than
    applied to the wrong faces.

    The seam set is never touched — this is exactly the R1 contract.
    """
    from chart_uv_agent import catastrophic_repair as cat_repair

    rows = list(overrides or ())
    report = {"applied": 0, "stale": []}
    if not rows:
        return report

    charts = [frozenset(int(f) for f in chart)
              for chart in flood_charts(mesh, {int(e) for e in seams})]
    chart_set = set(charts)
    applied = 0
    for index, override in enumerate(rows):
        faces = frozenset(int(f) for f in (override.get("faces") or ()))
        if not faces or faces not in chart_set:
            report["stale"].append(int(index))
            continue
        cat_repair.apply_reunwrap_variant(obj, mesh, sorted(faces),
                                          dict(override.get("variant") or {}),
                                          margin=float(margin))
        applied += 1
    report["applied"] = int(applied)
    return report


def unwrap_and_measure(obj, mesh: MeshGraph, seams, *, profile: QualityProfile,
                       margin: float, stage: str, regions: dict | None = None,
                       overrides=None) -> dict:
    """Apply ``seams``, unwrap/pack, replay the layout recipe, then measure the result."""
    from chart_uv_agent import unwrap as unwrap_mod

    seams = {int(e) for e in seams}
    unwrap_mod.unwrap_and_pack(obj, seams, margin=margin)
    override_report = apply_unwrap_overrides(obj, mesh, seams, overrides, margin=margin)
    measurement = measure_layout(obj, mesh, seams, profile=profile, stage=stage,
                                 regions=regions)
    measurement["override_report"] = override_report
    return measurement


# ------------------------------------------------------------- gap re-packing

#: Re-pack margin multipliers tried when the ONLY failing correctness checks are the
#: placement ones (G1 packing 간격 / G5 "packing 단독 문제로 추가 절개 0").
GAP_REPACK_FACTORS: tuple[float, ...] = (1.5, 2.0, 3.0)

#: The correctness checks a pure RE-PACK (never a cut) can repair: island-to-island
#: spacing and tile-border padding are both placement problems (G9/G11).
_REPACK_ONLY_CHECKS = ("island_gap", "border_gap")


def gap_only_failure(correctness: dict) -> bool:
    """True when the failing correctness checks are ONLY the placement ones.

    ``island_gap`` / ``border_gap`` are the two a pure re-pack can repair; if anything
    else (overlap / orientation / degenerate / bounds) fails, re-packing is the wrong
    tool and the caller must not pretend otherwise.
    """
    checks = {str(c.get("name")): bool(c.get("passed"))
              for c in (correctness or {}).get("checks", ())}
    if all(checks.get(name, True) for name in _REPACK_ONLY_CHECKS):
        return False
    return all(v for k, v in checks.items() if k not in _REPACK_ONLY_CHECKS)


def repack_for_gap(obj, mesh: MeshGraph, seams, *, profile: QualityProfile,
                   pack_margin: float, regions: dict | None = None,
                   stage: str = "gap_repack", history: list | None = None) -> dict:
    """Repair a placement-ONLY correctness failure by re-packing wider (G9/G11).

    Measures the layout that is already on ``obj`` (:func:`measure_layout` never unwraps).
    When the measurement does not fail, or fails something a re-pack cannot fix, it is
    returned untouched and nothing is packed. Otherwise the layout is re-packed with a
    progressively larger margin (``GAP_REPACK_FACTORS``, ≤ 3 attempts) until BOTH
    ``island_gap`` and ``border_gap`` pass. The seam set is NEVER touched — a packing gap
    is not a reason to cut.
    """
    from chart_uv_agent import unwrap as unwrap_mod

    def _measure() -> dict:
        return measure_layout(obj, mesh, seams, profile=profile, stage=stage,
                              regions=regions)

    def _check(measurement: dict, name: str) -> dict:
        return (measurement.get("correctness") or {}).get(name) or {}

    measurement = _measure()
    if not gap_only_failure(measurement.get("correctness") or {}):
        return measurement

    attempts, passed = 0, False
    for factor in GAP_REPACK_FACTORS:
        attempts += 1
        unwrap_mod.repack(obj, margin=float(pack_margin) * float(factor))
        measurement = _measure()
        if (bool(_check(measurement, "island_gap").get("passed", False))
                and bool(_check(measurement, "border_gap").get("passed", True))):
            passed = True
            break

    if history is not None:
        history.append({
            "stage": "gap_repack",
            "attempts": int(attempts),
            "passed": bool(passed),
            "min_gap_px": float(_check(measurement, "island_gap")
                                .get("min_gap_px", float("nan"))),
            "min_border_gap_px": float(_check(measurement, "border_gap")
                                       .get("min_gap_px", float("nan"))),
        })
    measurement["gap_repack"] = {"attempts": int(attempts), "passed": bool(passed)}
    return measurement


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
        exceed_basis=profile.bad_area_threshold,
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


def _fragmentation_hard_counts(measurement: dict) -> tuple[int, int, int]:
    """The three NON-EXEMPT G7 hard counts a candidate may not make worse (G6-D)."""
    report = measurement.get("fragmentation") or {}
    metrics = report.get("metrics") or {}
    sliver = 0
    for check in report.get("checks") or ():
        if check.get("name") == "sliver_islands":
            sliver = int(check.get("value", 0) or 0)
            break
    return (
        int(metrics.get("zero_area_island_count", 0) or 0),
        int(metrics.get("below_min_area_island_count", 0) or 0),
        sliver,
    )


def _fragmentation_ok(before: dict, after: dict) -> bool:
    """G6-D candidate fragmentation limit.

    A candidate whose layout passes G7 is fine. A candidate applied to an ALREADY
    fragmented layout may keep it fragmented while a split makes distortion progress —
    but never add a single new non-exempt dust or sliver island.
    """
    after_report = after.get("fragmentation") or {}
    if bool(after_report.get("passed", False)):
        return True
    before_report = before.get("fragmentation") or {}
    if not before_report or bool(before_report.get("passed", True)):
        return False
    b = _fragmentation_hard_counts(before)
    a = _fragmentation_hard_counts(after)
    return all(av <= bv for av, bv in zip(a, b))


def _correctness_counters(measurement: dict) -> tuple[float, int, int]:
    corr = measurement.get("correctness") or {}
    return (
        float((corr.get("overlap") or {}).get("overlap_area_total", 0.0)),
        int((corr.get("orientation") or {}).get("local_flip_count", 0)),
        int((corr.get("degenerate") or {}).get("uv_degenerate_count", 0)),
    )


def _finite_float(value, default: float) -> float:
    """``float(value)`` when it is a real finite number, ``default`` otherwise.

    ``None`` is what :mod:`catastrophic_distortion` reports for "not measurable"; it is
    never silently read as 0.0 by a caller that wanted a magnitude."""
    if value is None or isinstance(value, bool):
        return float(default)
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float(default)
    return out if np.isfinite(out) else float(default)


def _catastrophic_reason_rank(region: dict) -> int:
    """Where ``region`` sits in :data:`CATASTROPHIC_REASON_PRIORITY` (lower = repair first).

    The two boolean flags (``local_flip`` / ``self_overlap``) are folded into the same
    ordering as the triangle reasons, so one comparison decides the whole priority."""
    reasons = set(str(r) for r in (region.get("reasons") or ()))
    if region.get("local_flip"):
        reasons.add("local_flip")
    if region.get("self_overlap"):
        reasons.add("self_overlap")
    for rank, group in enumerate(CATASTROPHIC_REASON_PRIORITY):
        if reasons.intersection(group):
            return rank
    return len(CATASTROPHIC_REASON_PRIORITY)


def _region_catastrophic(measurement: dict, faces) -> dict:
    """The catastrophic sub-report restricted to exactly ``faces`` (a repair's own scope).

    The target of a catastrophic round is a FACE SET; after a re-unwrap or a cut the region
    ids renumber, so "did THIS region get better" can only be answered by re-counting the
    same faces on the new measurement."""
    want = {int(f) for f in faces}
    report = measurement.get("catastrophic") or {}
    bad_faces = {int(f) for f in (report.get("bad_face_ids") or ())}
    bad_triangles = 0
    area_fraction = 0.0
    max_aniso: float | None = None
    max_aspect: float | None = None
    for region in report.get("regions") or ():
        region_faces = {int(f) for f in (region.get("face_ids") or ())}
        if not region_faces & want:
            continue
        bad_triangles += int(region.get("bad_triangle_count", 0) or 0)
        area_fraction += _finite_float(region.get("area_fraction"), 0.0)
        aniso = region.get("max_anisotropy")
        if aniso is not None and np.isfinite(_finite_float(aniso, float("nan"))):
            value = float(aniso)
            max_aniso = value if max_aniso is None else max(max_aniso, value)
        aspect = region.get("max_uv_aspect_ratio")
        if aspect is not None:
            value = float(aspect)
            max_aspect = value if max_aspect is None else max(max_aspect, value)
    score = max_aniso if max_aniso is not None else max_aspect
    return {
        "bad_triangle_count": int(bad_triangles),
        "bad_face_count": len(bad_faces & want),
        "area_fraction": float(area_fraction),
        "max_anisotropy": max_aniso,
        "max_uv_aspect_ratio": max_aspect,
        "score": _finite_float(score, float("nan")),
    }


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

    # (0) CG2/CG5: a CATASTROPHIC region outranks every average-based complaint. A needle
    # or a collapsed face destroys the texture no matter how good the p95 looks, so it is
    # repaired first — and counted regions before below-min ones, because a cluster large
    # enough to be counted is the one the gate is failing on.
    catastrophic = measurement.get("catastrophic") or {}
    regions = list(catastrophic.get("regions") or ())
    if regions:
        ordered = sorted(
            regions,
            key=lambda r: (
                1 if bool(r.get("below_cluster_min")) else 0,
                _catastrophic_reason_rank(r),
                -_finite_float(r.get("area_fraction"), 0.0),
                int(r.get("region_id", 0)),
            ),
        )
        for region in ordered:
            faces = frozenset(int(f) for f in (region.get("face_ids") or ()))
            if not faces or faces in rejected:
                continue
            before = region.get("max_anisotropy")
            if before is None:
                before = region.get("max_uv_aspect_ratio")
            return {
                "kind": "catastrophic_repair",
                "island_id": int(region.get("island_id", -1)),
                "faces": faces,
                "region_id": int(region.get("region_id", 0)),
                "metric": CATASTROPHIC_METRIC,
                "before": _finite_float(before, float("nan")),
                "reasons": list(region.get("reasons") or ()),
            }

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
                       regions: dict | None = None, overrides=None) -> dict:
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
                                   stage="candidate", regions=regions, overrides=overrides)

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

        fragmentation_ok = _fragmentation_ok(before, after)

        verdict = accept_candidate(
            profile,
            target_before=before_value,
            target_after=after_value,
            quality_after_passed=bool(after["passed"]),
            correctness_ok=bool(correctness_ok and mandatory_ok),
            constraints_ok=True,
            regression_ok=bool(regression["ok"]),
            fragmentation_ok=bool(fragmentation_ok),
        )
        record.update(verdict)
        record.update({
            "after_measurement": after,
            "fragmentation_ok": bool(fragmentation_ok),
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


# ----------------------------------------------- catastrophic repair trials (CG5/CG6)


def _catastrophic_verdict(mesh: MeshGraph, target: dict, before: dict, after: dict, *,
                          profile: QualityProfile, constraints_ok: bool,
                          island_cap_ok: bool) -> tuple[dict, dict, dict, dict]:
    """The CG6 verdict for one catastrophic trial, plus the two region sub-reports.

    Returns ``(verdict, region_before, region_after, extras)`` where ``extras`` carries the
    individual gate results the record reports."""
    from chart_uv_agent import catastrophic_repair as cat_repair

    faces = target["faces"]
    region_before = _region_catastrophic(before, faces)
    region_after = _region_catastrophic(after, faces)

    if after["correctness"].get("passed", False):
        correctness_ok = True
    elif before["correctness"].get("passed", False):
        correctness_ok = False
    else:
        b_overlap, b_flip, b_degen = _correctness_counters(before)
        a_overlap, a_flip, a_degen = _correctness_counters(after)
        correctness_ok = (a_overlap <= b_overlap + 1e-12 and a_flip <= b_flip
                          and a_degen <= b_degen)

    mandatory_ok = (
        after["mandatory_audit"]["mandatory_90_missing"] == 0
        and after["mandatory_audit"]["mandatory_90_uv_unsplit"]
        <= before["mandatory_audit"]["mandatory_90_uv_unsplit"]
    )
    fragmentation_ok = _fragmentation_ok(before, after)
    regression = regression_within_budget(
        profile,
        (before.get("distortion_v2") or {}).get("global") or {},
        (after.get("distortion_v2") or {}).get("global") or {},
        target="anisotropy_max",
    )
    # A catastrophic "before" is BROKEN by definition, and a broken layout routinely has a
    # non-finite global metric. There is nothing to regress FROM in that case, so a
    # violation whose ``before`` was never measurable is not held against the repair; a
    # finite before that got worse still is.
    violations = [v for v in regression["violations"]
                  if np.isfinite(_finite_float(v.get("before"), float("nan")))]
    regression_ok = not violations

    verdict = cat_repair.accept_catastrophic_candidate(
        profile,
        before=before, after=after, target_faces=faces, mesh=mesh,
        correctness_ok=bool(correctness_ok), constraints_ok=bool(constraints_ok),
        mandatory_ok=bool(mandatory_ok), fragmentation_ok=bool(fragmentation_ok),
        island_cap_ok=bool(island_cap_ok), regression_ok=bool(regression_ok),
        region_before=region_before, region_after=region_after,
    )
    extras = {
        "correctness_ok": bool(correctness_ok),
        "mandatory_ok": bool(mandatory_ok),
        "fragmentation_ok": bool(fragmentation_ok),
        "regression_ok": bool(regression_ok),
        "regression_violations": list(violations),
    }
    return verdict, region_before, region_after, extras


def _catastrophic_record(target: dict, *, kind: str, candidate: dict,
                         aux_length: float, exposure: float) -> dict:
    return {
        "candidate": candidate,
        "kind": str(kind),
        "cut_reason": "catastrophic_repair",
        "region_id": target.get("region_id"),
        "target_island": int(target.get("island_id", -1)),
        "target_metric": CATASTROPHIC_METRIC,
        "aux_length": float(aux_length),
        "exposure": float(exposure),
    }


def _finish_catastrophic_record(record: dict, *, before: dict, after: dict,
                                verdict: dict, region_before: dict, region_after: dict,
                                extras: dict, started: float) -> dict:
    record.update(verdict)
    record.update(extras)
    record.update({
        "after_measurement": after,
        "island_count_after": int(after["island_count"]),
        "quality_after_passed": bool(after["passed"]),
        "target_before": float(region_before["score"]),
        "target_after": float(region_after["score"]),
        "region_before": dict(region_before),
        "region_after": dict(region_after),
        "catastrophic_before": list(catastrophic_counters(before.get("catastrophic") or {})),
        "catastrophic_after": list(catastrophic_counters(after.get("catastrophic") or {})),
        "elapsed_s": time.monotonic() - started,
    })
    return record


def evaluate_reunwrap_candidate(obj, mesh: MeshGraph, seams, variant: dict, *,
                                target: dict, before: dict, island_faces,
                                profile: QualityProfile, margin: float,
                                island_cap_ok: bool = True,
                                regions: dict | None = None) -> dict:
    """Trial ONE R1 same-seam re-unwrap variant (CG5): apply, measure, judge.

    Like :func:`evaluate_candidate` this never restores — the caller owns the snapshot.
    The seam set is not touched at all, so ``added_edges`` stays empty and a successful R1
    means the model was repaired with ZERO new seams."""
    from chart_uv_agent import catastrophic_repair as cat_repair

    started = time.monotonic()
    record = _catastrophic_record(target, kind="reunwrap", candidate=dict(variant),
                                  aux_length=0.0, exposure=0.0)
    record["variant_id"] = str(variant.get("variant_id", ""))
    try:
        cat_repair.apply_reunwrap_variant(obj, mesh, island_faces, variant, margin=margin)
        after = measure_layout(obj, mesh, seams, profile=profile, stage="candidate",
                               regions=regions)
        verdict, region_before, region_after, extras = _catastrophic_verdict(
            mesh, target, before, after, profile=profile, constraints_ok=True,
            island_cap_ok=bool(island_cap_ok))
        return _finish_catastrophic_record(record, before=before, after=after,
                                           verdict=verdict, region_before=region_before,
                                           region_after=region_after, extras=extras,
                                           started=started)
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


def evaluate_relief_candidate(obj, mesh: MeshGraph, seams, cand, *, target: dict,
                              before: dict, constraints, profile: QualityProfile,
                              margin: float, island_cap_ok: bool = True,
                              regions: dict | None = None, overrides=None) -> dict:
    """Trial ONE R2 relief seam (CG5): apply the cut, unwrap, measure, judge.

    A constraint violation short-circuits before any unwrap, exactly as in
    :func:`evaluate_candidate` — an illegal cut is never measured."""
    started = time.monotonic()
    record = _catastrophic_record(target, kind=str(cand.kind), candidate=cand.to_dict(),
                                  aux_length=seam_length(mesh, cand.added_edges),
                                  exposure=float(cand.exposure_cost))
    record["variant_id"] = None
    record["relief_reason"] = str(cand.reason)

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
                                   stage="candidate", regions=regions, overrides=overrides)
        verdict, region_before, region_after, extras = _catastrophic_verdict(
            mesh, target, before, after, profile=profile, constraints_ok=True,
            island_cap_ok=bool(island_cap_ok))
        return _finish_catastrophic_record(record, before=before, after=after,
                                           verdict=verdict, region_before=region_before,
                                           region_after=region_after, extras=extras,
                                           started=started)
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


def _pick_catastrophic(trials: list[tuple]) -> tuple | None:
    """The winning ``(candidate, record)`` of a catastrophic round, deterministically.

    A candidate whose layout passes the whole bar beats a merely-accepted one; among
    equals the biggest region improvement wins, and the production order (R1 variants
    cheapest-first, relief candidates already ranked) breaks any remaining tie."""
    accepted = [(index, pair) for index, pair in enumerate(trials)
                if pair[1].get("accepted")]
    if not accepted:
        return None
    best = min(accepted, key=lambda item: (
        0 if item[1][1].get("quality_after_passed") else 1,
        -round(float(item[1][1].get("improvement_ratio", 0.0)), 9),
        item[0],
    ))
    return best[1]


def _choice_key(record: dict, cand) -> tuple:
    """Deterministic tie-break for candidates of equal standing (§5 / G4 / G2).

    Islands → auxiliary seam length → exposure → the candidate's own itemised cut cost
    (``cost["total"]``), so two cuts that are otherwise indistinguishable are separated
    by the cost ranking instead of by dict order.
    """
    island_count = record.get("island_count_after")
    if island_count is None:
        island_count = 1 << 30
    total_cost = float((getattr(cand, "cost", None) or {}).get("total", 0.0))
    return rank_key(island_count, record.get("aux_length", 0.0),
                    record.get("exposure", 0.0), total_cost)


# ------------------------------------------------- catastrophic repair round (CG5)


def _catastrophic_history_row(target: dict, *, iterations: int, action: str, reason: str,
                              record: dict | None, added: list[int],
                              candidate_kind, candidates_evaluated: int) -> dict:
    region_before = (record or {}).get("region_before") or {}
    region_after = (record or {}).get("region_after") or region_before
    before_value = float(region_before.get("score", target.get("before", float("nan")))
                         if region_before else target.get("before", float("nan")))
    after_value = float(region_after.get("score", before_value)
                        if region_after else before_value)
    return {
        "round": int(iterations),
        "stage": "refinement",
        "action": str(action),
        "reason": str(reason),
        "cut_reason": "catastrophic_repair",
        "region_id": target.get("region_id"),
        "target_island": int(target.get("island_id", -1)),
        "target_metric": CATASTROPHIC_METRIC,
        "before": before_value,
        "after": after_value,
        "improvement_ratio": float((record or {}).get("improvement_ratio", 0.0)),
        "added_edges": list(added),
        "candidate_kind": candidate_kind,
        "candidates_evaluated": int(candidates_evaluated),
        "bad_triangles_before": int(region_before.get("bad_triangle_count", 0) or 0),
        "bad_triangles_after": int(region_after.get("bad_triangle_count", 0) or 0),
        "bad_area_before": float(region_before.get("area_fraction", 0.0) or 0.0),
        "bad_area_after": float(region_after.get("area_fraction", 0.0) or 0.0),
    }


def _run_catastrophic_round(obj, mesh: MeshGraph, seams: set[int], *, target: dict,
                            measurement: dict, constraints, profile: QualityProfile,
                            budget: dict, margin: float, regions, history: list,
                            candidate_history: list, iterations: int,
                            island_cap_ok: bool, overrides=None) -> dict:
    """ONE catastrophic repair round: R1 same-seam re-unwrap, then (only then) R2 relief.

    Every trial is bracketed by the snapshot pair, so a rejected R1 variant leaves the UVs
    *and* the seam set exactly as they were — verified by the snapshot's ``uv_hash`` (CG7).
    Returns ``measurement`` ``None`` when nothing was accepted; the caller then marks the
    region rejected and moves on.
    """
    from chart_uv_agent import catastrophic_repair as cat_repair

    islands = measurement.get("islands") or []
    island_id = int(target.get("island_id", -1))
    island_faces = (sorted(int(f) for f in islands[island_id])
                    if 0 <= island_id < len(islands) else sorted(target["faces"]))

    snapshot = take_snapshot(obj, mesh, seams, measurement)
    evaluated = 0

    # --- R1: same seams, different solver settings -------------------------------
    r1_trials: list[tuple] = []
    for variant in cat_repair.reunwrap_candidates(target, profile):
        try:
            record = evaluate_reunwrap_candidate(
                obj, mesh, seams, variant, target=target, before=measurement,
                island_faces=island_faces, profile=profile, margin=margin,
                island_cap_ok=True, regions=regions)
        finally:
            restore_snapshot(obj, mesh, snapshot)
        record["round"] = int(iterations)
        evaluated += 1
        candidate_history.append(record)
        r1_trials.append((variant, record))

    chosen = _pick_catastrophic(r1_trials)
    if chosen is not None:
        variant, record = chosen
        cat_repair.apply_reunwrap_variant(obj, mesh, island_faces, variant, margin=margin)
        # CG14/CG0: the repair is UV-only, so it is recorded as a layout-recipe row. Every
        # later unwrap of this seam set replays it instead of silently discarding it.
        if overrides is not None:
            overrides.append(override_from_variant(
                island_faces, variant, round_index=int(iterations),
                region_id=target.get("region_id")))
        after = measure_layout(obj, mesh, seams, profile=profile, stage="refinement",
                               regions=regions)
        history.append(_catastrophic_history_row(
            target, iterations=iterations, action="reunwrap", reason=target["kind"],
            record=record, added=[], candidate_kind="reunwrap",
            candidates_evaluated=len(r1_trials)))
        return {"reason": "r1_accepted", "candidates_evaluated": evaluated,
                "measurement": after, "seams": set(seams), "added_seams": set()}

    # --- R2: a relief seam, and only because every R1 variant failed --------------
    relief = cat_repair.relief_seam_candidates(
        mesh, islands, island_id, seams, constraints, target["faces"],
        max_candidates=int(budget["max_candidates_per_round"]))

    if not island_cap_ok:
        # The cap blocks CUTS. Every relief candidate is recorded as rejected without
        # ever being unwrapped, so the evidence says WHY the repair stopped.
        for cand in relief:
            record = _catastrophic_record(
                target, kind=str(cand.kind), candidate=cand.to_dict(),
                aux_length=seam_length(mesh, cand.added_edges),
                exposure=float(cand.exposure_cost))
            record.update({
                "round": int(iterations),
                "variant_id": None,
                "relief_reason": str(cand.reason),
                "accepted": False,
                "reason": "island_cap_reached",
                "improvement_ratio": 0.0,
                "after_measurement": None,
                "island_count_after": None,
                "elapsed_s": 0.0,
            })
            candidate_history.append(record)
        history.append(_catastrophic_history_row(
            target, iterations=iterations, action="reject_region",
            reason="island_cap_reached", record=None, added=[], candidate_kind=None,
            candidates_evaluated=len(r1_trials)))
        return {"reason": "island_cap_reached", "candidates_evaluated": evaluated,
                "measurement": None, "seams": set(seams), "added_seams": set()}

    r2_trials: list[tuple] = []
    for cand in relief:
        if cand.rejected:
            record = _catastrophic_record(
                target, kind=str(cand.kind), candidate=cand.to_dict(),
                aux_length=seam_length(mesh, cand.added_edges),
                exposure=float(cand.exposure_cost))
            record.update({
                "round": int(iterations),
                "variant_id": None,
                "relief_reason": str(cand.reason),
                "accepted": False,
                "reason": str(cand.rejected),
                "improvement_ratio": 0.0,
                "after_measurement": None,
                "island_count_after": None,
                "elapsed_s": 0.0,
            })
            candidate_history.append(record)
            continue
        try:
            record = evaluate_relief_candidate(
                obj, mesh, seams, cand, target=target, before=measurement,
                constraints=constraints, profile=profile, margin=margin,
                island_cap_ok=True, regions=regions, overrides=overrides)
        finally:
            restore_snapshot(obj, mesh, snapshot)
        record["round"] = int(iterations)
        evaluated += 1
        candidate_history.append(record)
        r2_trials.append((cand, record))

    chosen = _pick_catastrophic(r2_trials)
    if chosen is None:
        history.append(_catastrophic_history_row(
            target, iterations=iterations, action="reject_region", reason=target["kind"],
            record=None, added=[], candidate_kind=None,
            candidates_evaluated=len(r1_trials) + len(r2_trials)))
        return {"reason": "no_improving_candidate", "candidates_evaluated": evaluated,
                "measurement": None, "seams": set(seams), "added_seams": set()}

    cand, record = chosen
    added = sorted(int(e) for e in cand.added_edges)
    new_seams = set(seams) | set(added)
    after = unwrap_and_measure(obj, mesh, new_seams, profile=profile, margin=margin,
                               stage="refinement", regions=regions, overrides=overrides)
    history.append(_catastrophic_history_row(
        target, iterations=iterations, action="split", reason=target["kind"],
        record=record, added=added, candidate_kind=cand.kind,
        candidates_evaluated=len(r1_trials) + len(r2_trials)))
    return {"reason": "r2_accepted", "candidates_evaluated": evaluated,
            "measurement": after, "seams": new_seams, "added_seams": set(added)}


# --------------------------------------------------------------------- loop


def run_refinement(obj, mesh: MeshGraph, seams: set[int], *, constraints,
                   profile: QualityProfile, budget: dict | None = None,
                   margin: float = 0.005, regions: dict | None = None,
                   history: list | None = None, candidate_history: list | None = None,
                   clock=time.monotonic, initial_measurement: dict | None = None,
                   overrides: list | None = None) -> dict:
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
    overrides = overrides if overrides is not None else []
    history = history if history is not None else []
    candidate_history = candidate_history if candidate_history is not None else []
    seams = {int(e) for e in seams}
    distortion_seams: set[int] = set()
    rejected_regions: set[frozenset] = set()

    started = clock()
    iterations = 0
    candidates_evaluated = 0
    catastrophic_rounds = 0
    round_reasons: list[dict] = []

    measurement = initial_measurement
    if measurement is None:
        measurement = unwrap_and_measure(obj, mesh, seams, profile=profile, margin=margin,
                                         stage="refinement", regions=regions,
                                         overrides=overrides)

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

        target = select_target(measurement, rejected_regions, profile)
        if target is None:
            reason = "no_improving_candidate" if rejected_regions else "no_failing_target"
            break

        # CG5 island cap: the cap limits SEAMS, not repairs. A catastrophic target's R1
        # variants add no seam at all, so the cap may not stop them — it only blocks the
        # R2 relief cut (handled inside the catastrophic branch).
        island_cap_ok = bool(measurement["island_count"] < budget["island_cap"])
        is_catastrophic = target["kind"] == "catastrophic_repair"
        if not island_cap_ok and not is_catastrophic:
            reason = "island_cap"
            break

        if is_catastrophic:
            if catastrophic_rounds >= int(profile.catastrophic_repair_max_rounds):
                reason = "catastrophic_budget"
                round_reasons.append({"round": iterations, "kind": target["kind"],
                                      "reason": "catastrophic_budget"})
                break
            catastrophic_rounds += 1
            outcome = _run_catastrophic_round(
                obj, mesh, seams, target=target, measurement=measurement,
                constraints=constraints, profile=profile, budget=budget, margin=margin,
                regions=regions, history=history, candidate_history=candidate_history,
                iterations=iterations, island_cap_ok=island_cap_ok, overrides=overrides)
            candidates_evaluated += int(outcome["candidates_evaluated"])
            round_reasons.append({"round": iterations, "kind": target["kind"],
                                  "reason": outcome["reason"]})
            iterations += 1
            if outcome["measurement"] is not None:
                measurement = outcome["measurement"]
                seams = outcome["seams"]
                distortion_seams |= outcome["added_seams"]
                continue
            rejected_regions.add(target["faces"])
            if outcome["reason"] == "island_cap_reached":
                reason = "island_cap"
                break
            continue

        # G2: WHY this round is cutting at all travels with every candidate and every
        # history row — a correctness repair is not a distortion repair.
        cut_reason = ("correctness_repair" if target["kind"] == "correctness_repair"
                      else "distortion_repair")

        snapshot = take_snapshot(obj, mesh, seams, measurement)
        cands = generate_candidates(
            mesh, measurement["islands"], target["island_id"], seams, constraints,
            measurement["face_anisotropy"],
            max_candidates=budget["max_candidates_per_round"],
            cut_reason=cut_reason,
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
                    "cut_reason": str(getattr(cand, "cut_reason", cut_reason)),
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
                    margin=margin, regions=regions, overrides=overrides,
                )
            finally:
                # G5: restore on EVERY path — accepted, rejected, or raised.
                restore_snapshot(obj, mesh, snapshot)
            record["round"] = iterations
            record["cut_reason"] = str(getattr(cand, "cut_reason", cut_reason))
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
                "cut_reason": cut_reason,
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
                                         stage="refinement", regions=regions,
                                         overrides=overrides)
        history.append({
            "round": iterations,
            "stage": "refinement",
            "action": "split" if added else "unwrap_only",
            "reason": target["kind"],
            "cut_reason": cut_reason,
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
        # CG14/CG0: the layout recipe the caller must replay after every later unwrap.
        "overrides": list(overrides),
        "termination": {
            "reason": reason,
            "iterations": int(iterations),
            "candidates_evaluated": int(candidates_evaluated),
            "elapsed_s": float(clock() - started),
            "budget": dict(budget),
            "catastrophic_rounds": int(catastrophic_rounds),
            "round_reasons": list(round_reasons),
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
    "CATASTROPHIC_METRIC",
    "CATASTROPHIC_REASON_PRIORITY",
    "CORRECTNESS_METRIC",
    "GAP_REPACK_FACTORS",
    "METRIC_PRIORITY",
    "UvSnapshot",
    "apply_unwrap_overrides",
    "ensure_border_margin",
    "evaluate_candidate",
    "evaluate_relief_candidate",
    "evaluate_reunwrap_candidate",
    "gap_only_failure",
    "measure_layout",
    "override_from_variant",
    "repack_for_gap",
    "resolve_budget",
    "restore_snapshot",
    "run_refinement",
    "seam_length_report",
    "select_target",
    "take_snapshot",
    "unwrap_and_measure",
]
