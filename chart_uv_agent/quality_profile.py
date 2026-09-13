"""Versioned quality profile for the automatic UV path (UV_AUTOMATION_WORK_PLAN §3–§5).

Gates: G8 (frozen profile with the mandated required keys), G5 (candidate acceptance,
regression budget, explicit iteration/candidate/time/island budgets), G3 (metric_version
so v1 and v2 distortion reports are never mixed).

Style follows ``chart_uv_agent.gate.ChartGateConfig``: ONE frozen dataclass holds every
threshold and budget, and the report it produces is self-contained (a reviewer can read
it without knowing the calibration story).

Pure Python on purpose — no numpy, no bpy. This module is the shared decision core for
the no-spec path and the user-assisted automatic path, so it must be importable from a
plain test process as well as from inside Blender.

The numeric defaults are the ENGINEERING starting point (``calibrated = False``), NOT a
product quality bar. G8 requires calibration against reviewer-approved real models plus a
holdout set before the automatic quality path ships; the area-stretch 0.50/0.60 values
carried over from the chart gate are explicitly NOT copied onto the anisotropy caps.

The G8 "profile 필수 키" set is every field of :class:`QualityProfile`, named after the
acceptance document: ``anisotropy_global_p95_max`` / ``anisotropy_island_p95_max`` /
``anisotropy_max_max``, ``bad_area_threshold`` / ``bad_area_ratio_max`` /
``bad_area_ratio_island_max``, ``area_stretch_global_mean_max`` /
``area_stretch_island_mean_max`` / ``area_stretch_global_p95_max`` /
``area_stretch_island_p95_max``, the packing keys (``border_margin_px``,
``packing_efficiency_min``), the island-hygiene keys (``min_island_uv_area``,
``tiny_island_uv_area``, ``tiny_island_count_max``, ``tiny_island_area_ratio_max``,
``sliver_aspect_min``, ``sliver_uv_area_max``, ``sliver_island_count_max``,
``island_aspect_p95_max``), the texel-density keys (``texel_density_cv_max``,
``texel_density_outlier_tolerance``, ``texel_density_outlier_count_max``),
``shading_uv_policy``, and the merge-back keys (``merge_back_enabled``,
``merge_back_max_trials``).

CG16 / CG8 (game-UV gates) add the following REQUIRED keys — engineering defaults, not a
calibrated product bar:

CG16 (catastrophic distortion, a HARD gate that is separate from the quality caps above —
the quality gate keeps ``anisotropy_max_max`` 3.0 and CG16 adds its own 8.0 hard ceiling,
so nothing is loosened): ``catastrophic_metric_version``, ``anisotropy_hard_max``,
``near_collapse_ratio``, ``max_uv_triangle_aspect``, ``local_area_ratio_min``,
``local_area_ratio_max``, ``bad_area_fraction_cap``, ``catastrophic_repair_max_rounds``,
``catastrophic_reunwrap_variants``.

CG8 (tiny / sliver island gate, expressed in PIXELS at the profile's ``texture_size_px``):
``min_island_width_px`` (the stored value 10.0 is ``max(2 * margin_px + 2, 6)`` evaluated at
``margin_px`` 4 — the number is frozen in the profile, not recomputed),
``min_island_area_px2``, ``max_island_bbox_aspect``, ``max_island_perimeter_area_ratio``
(the ratio is ``perimeter_px / sqrt(area_px2)``, so it is scale free — a square is 4.0),
``max_tiny_island_area_fraction``.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field


def _default_regression_budget() -> dict:
    """Relative regression allowed on the NON-target distortion metrics (G5).

    A metric missing from this mapping has a budget of 0 — see ``regression_budget_for``.
    """
    return {
        "anisotropy_p95": 0.05,
        "anisotropy_max": 0.05,
        "area_stretch_mean": 0.05,
        "exceed_area_fraction": 0.02,
    }


@dataclass(frozen=True)
class QualityProfile:
    """Frozen, versioned quality profile (G8 required keys)."""

    # --- identity / versioning (G3, G8) ---
    profile_id: str = "engineering_v0"
    metric_version: int = 2
    calibrated: bool = False

    # --- anisotropy caps (G8; deliberately NOT the area-stretch numbers) ---
    anisotropy_global_p95_max: float = 1.6
    anisotropy_island_p95_max: float = 1.8
    anisotropy_max_max: float = 3.0

    # --- exceed-area basis and caps (G8) ---
    bad_area_threshold: float = 1.6
    bad_area_ratio_max: float = 0.05
    bad_area_ratio_island_max: float = 0.10

    # --- area stretch caps (existing chart-gate meaning kept) ---
    area_stretch_global_mean_max: float = 0.50
    area_stretch_island_mean_max: float = 0.60
    area_stretch_global_p95_max: float = 0.9
    area_stretch_island_p95_max: float = 1.1

    # --- texture context (margin/texel decisions must be part of the frozen profile) ---
    texture_size_px: int = 1024
    margin_px: int = 4

    # --- packing (G8) ---
    border_margin_px: int = 4
    packing_efficiency_min: float = 0.42

    # --- island hygiene: tiny islands and slivers (G8) ---
    min_island_uv_area: float = 1e-4
    tiny_island_uv_area: float = 0.002
    tiny_island_count_max: int = 8
    tiny_island_area_ratio_max: float = 0.05
    sliver_aspect_min: float = 8.0
    sliver_uv_area_max: float = 0.01
    sliver_island_count_max: int = 0
    island_aspect_p95_max: float = 6.0

    # --- catastrophic distortion hard gate (CG16) ---
    catastrophic_metric_version: int = 1
    anisotropy_hard_max: float = 8.0
    near_collapse_ratio: float = 1e-4
    max_uv_triangle_aspect: float = 40.0
    local_area_ratio_min: float = 0.04
    local_area_ratio_max: float = 25.0
    bad_area_fraction_cap: float = 0.005
    catastrophic_repair_max_rounds: int = 8
    catastrophic_reunwrap_variants: int = 3

    # --- tiny / sliver island gate in pixels (CG8) ---
    min_island_width_px: float = 10.0
    min_island_area_px2: float = 100.0
    max_island_bbox_aspect: float = 8.0
    max_island_perimeter_area_ratio: float = 12.0
    max_tiny_island_area_fraction: float = 0.02

    # --- texel density uniformity (G8) ---
    texel_density_cv_max: float = 0.15
    texel_density_outlier_tolerance: float = 0.30
    texel_density_outlier_count_max: int = 0

    # --- shading / seam policy (G8) ---
    shading_uv_policy: str = "preserve"

    # --- merge-back budget (G8) ---
    merge_back_enabled: bool = True
    merge_back_max_trials: int = 64

    # --- regression budget (G5): relative allowance on non-target metrics ---
    regression_budget: dict = field(default_factory=_default_regression_budget)

    # --- explicit search budgets (G5) ---
    max_iterations: int = 24
    max_candidates_per_round: int = 4
    time_budget_s: float = 600.0
    island_cap: int = 80
    min_improvement_ratio: float = 0.15
    seed: int = 0

    def regression_budget_for(self, name: str) -> float:
        """Relative regression budget for ``name``; missing metric ⇒ 0 (G5)."""
        value = self.regression_budget.get(name, 0.0)
        return float(value)

    def to_dict(self) -> dict:
        """Every profile key, so a report is self-contained. Round-trips through
        ``load_quality_profile``."""
        data = asdict(self)
        data["regression_budget"] = dict(self.regression_budget)
        return data


#: Every field name of :class:`QualityProfile` — the G8 "profile 필수 키" set.
REQUIRED_PROFILE_KEYS: tuple[str, ...] = (
    "profile_id",
    "metric_version",
    "calibrated",
    "anisotropy_global_p95_max",
    "anisotropy_island_p95_max",
    "anisotropy_max_max",
    "bad_area_threshold",
    "bad_area_ratio_max",
    "bad_area_ratio_island_max",
    "area_stretch_global_mean_max",
    "area_stretch_island_mean_max",
    "area_stretch_global_p95_max",
    "area_stretch_island_p95_max",
    "texture_size_px",
    "margin_px",
    "border_margin_px",
    "packing_efficiency_min",
    "min_island_uv_area",
    "tiny_island_uv_area",
    "tiny_island_count_max",
    "tiny_island_area_ratio_max",
    "sliver_aspect_min",
    "sliver_uv_area_max",
    "sliver_island_count_max",
    "island_aspect_p95_max",
    "catastrophic_metric_version",
    "anisotropy_hard_max",
    "near_collapse_ratio",
    "max_uv_triangle_aspect",
    "local_area_ratio_min",
    "local_area_ratio_max",
    "bad_area_fraction_cap",
    "catastrophic_repair_max_rounds",
    "catastrophic_reunwrap_variants",
    "min_island_width_px",
    "min_island_area_px2",
    "max_island_bbox_aspect",
    "max_island_perimeter_area_ratio",
    "max_tiny_island_area_fraction",
    "texel_density_cv_max",
    "texel_density_outlier_tolerance",
    "texel_density_outlier_count_max",
    "shading_uv_policy",
    "merge_back_enabled",
    "merge_back_max_trials",
    "regression_budget",
    "max_iterations",
    "max_candidates_per_round",
    "time_budget_s",
    "island_cap",
    "min_improvement_ratio",
    "seed",
)

#: The only shading/seam policies a frozen profile may declare (G8).
SHADING_UV_POLICIES: tuple[str, ...] = (
    "preserve",
    "split_normals_on_uv_seams",
    "require_uv_seam_on_sharp_edges",
)

ENGINEERING_V0 = QualityProfile()

PROFILES: dict = {"engineering_v0": ENGINEERING_V0}


def load_quality_profile(value=None) -> QualityProfile:
    """Resolve ``None`` / a profile id / a full profile dict to a :class:`QualityProfile`.

    A dict must carry EXACTLY the required keys: a missing key raises ``ValueError``
    (G8 — a profile is only frozen if it is complete) and an unknown key raises
    ``ValueError`` rather than being silently dropped. ``shading_uv_policy`` must be one
    of :data:`SHADING_UV_POLICIES`; anything else raises ``ValueError``.
    """
    if value is None:
        return ENGINEERING_V0
    if isinstance(value, QualityProfile):
        return value
    if isinstance(value, str):
        if value not in PROFILES:
            raise KeyError(
                f"unknown quality profile {value!r}; known: {sorted(PROFILES)}"
            )
        return PROFILES[value]
    if isinstance(value, dict):
        keys = set(value)
        missing = [k for k in REQUIRED_PROFILE_KEYS if k not in keys]
        if missing:
            raise ValueError(f"quality profile missing required keys: {missing}")
        extra = sorted(keys - set(REQUIRED_PROFILE_KEYS))
        if extra:
            raise ValueError(f"quality profile has unknown keys: {extra}")
        data = dict(value)
        data["regression_budget"] = dict(data["regression_budget"])
        policy = data["shading_uv_policy"]
        if policy not in SHADING_UV_POLICIES:
            raise ValueError(
                f"unknown shading_uv_policy {policy!r}; "
                f"allowed: {list(SHADING_UV_POLICIES)}"
            )
        return QualityProfile(**data)
    raise TypeError(f"cannot load quality profile from {type(value).__name__}")


# --- evaluation -------------------------------------------------------------------

#: Global distortion metrics compared across candidates (G5 regression budget).
_REGRESSION_METRICS: tuple[str, ...] = (
    "anisotropy_p95",
    "anisotropy_max",
    "area_stretch_mean",
    "exceed_area_fraction",
)


def _finite(value) -> bool:
    """True only for a real, finite float/int. Bools are NOT numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _check(name: str, scope: str, value: float, limit: float, *, island_id=None) -> dict:
    check = {
        "name": name,
        "scope": scope,
        "value": float(value),
        "limit": float(limit),
        "passed": float(value) <= float(limit),
    }
    if scope == "island":
        check["island_id"] = island_id
    return check


def evaluate_quality(profile: QualityProfile, distortion_v2: dict) -> dict:
    """Judge a v2 distortion report against the frozen profile (G3/G8).

    Missing / non-float / NaN / Infinity values are NEVER substituted with 0: they make
    the report invalid (``valid`` False, ``passed`` False) and are listed in
    ``invalid_reasons``. A metric_version mismatch is likewise invalid — v1 and v2 are
    never mixed.
    """
    report = distortion_v2 if isinstance(distortion_v2, dict) else {}
    invalid_reasons: list[str] = []
    checks: list[dict] = []

    version = report.get("metric_version")
    if version != profile.metric_version:
        invalid_reasons.append("metric_version_mismatch")

    if not bool(report.get("valid", False)):
        invalid_reasons.append("distortion_report_invalid")

    glob = report.get("global")
    if not isinstance(glob, dict):
        invalid_reasons.append("global")
        glob = {}

    def value_of(container: dict, key: str, label: str):
        if key not in container:
            invalid_reasons.append(label)
            return None
        raw = container[key]
        if not _finite(raw):
            invalid_reasons.append(label)
            return None
        return float(raw)

    global_limits = (
        ("anisotropy_p95", profile.anisotropy_global_p95_max),
        ("anisotropy_max", profile.anisotropy_max_max),
        ("area_stretch_mean", profile.area_stretch_global_mean_max),
        ("area_stretch_p95", profile.area_stretch_global_p95_max),
        ("exceed_area_fraction", profile.bad_area_ratio_max),
    )
    for key, limit in global_limits:
        val = value_of(glob, key, f"global.{key}")
        if val is not None:
            checks.append(_check(key, "global", val, limit))

    islands = report.get("islands")
    if islands is None:
        invalid_reasons.append("islands")
        islands = []
    elif not isinstance(islands, list):
        invalid_reasons.append("islands")
        islands = []

    island_limits = (
        ("anisotropy_p95", profile.anisotropy_island_p95_max),
        ("anisotropy_max", profile.anisotropy_max_max),
        ("area_stretch_mean", profile.area_stretch_island_mean_max),
        ("area_stretch_p95", profile.area_stretch_island_p95_max),
        ("exceed_area_fraction", profile.bad_area_ratio_island_max),
    )
    for index, island in enumerate(islands):
        if not isinstance(island, dict):
            invalid_reasons.append(f"islands[{index}]")
            continue
        island_id = island.get("island_id", index)
        for key, limit in island_limits:
            val = value_of(island, key, f"islands[{island_id}].{key}")
            if val is not None:
                checks.append(_check(key, "island", val, limit, island_id=island_id))

    degenerate = report.get("degenerate_triangles")
    if not isinstance(degenerate, dict):
        invalid_reasons.append("degenerate_triangles")
    else:
        uv_degenerate = degenerate.get("uv_degenerate_count")
        if not _finite(uv_degenerate):
            invalid_reasons.append("degenerate_triangles.uv_degenerate_count")
        else:
            checks.append(
                _check("uv_degenerate_triangles", "global", float(uv_degenerate), 0.0)
            )

    valid = not invalid_reasons
    passed = valid and all(c["passed"] for c in checks)
    return {
        "profile_id": profile.profile_id,
        "metric_version": profile.metric_version,
        "calibrated": profile.calibrated,
        "valid": valid,
        "passed": passed,
        "checks": checks,
        "failures": [c["name"] for c in checks if not c["passed"]],
        "invalid_reasons": invalid_reasons,
    }


def regression_within_budget(
    profile: QualityProfile, before: dict, after: dict, *, target: str
) -> dict:
    """Check the NON-target global metrics against the profile's regression budget (G5).

    ``after <= before * (1 + budget)``; when ``before`` is 0 the budget is read as an
    absolute allowance (``after <= budget``). A metric with no budget entry has budget 0.
    A missing / non-finite value is a violation — it is never treated as 0.
    """
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    violations: list[dict] = []
    for metric in _REGRESSION_METRICS:
        if metric == target:
            continue
        budget = profile.regression_budget_for(metric)
        b_raw = before.get(metric)
        a_raw = after.get(metric)
        if not _finite(b_raw) or not _finite(a_raw):
            violations.append(
                {"metric": metric, "before": b_raw, "after": a_raw, "budget": budget}
            )
            continue
        b_val = float(b_raw)
        a_val = float(a_raw)
        limit = budget if b_val == 0.0 else b_val * (1.0 + budget)
        if a_val > limit:
            violations.append(
                {"metric": metric, "before": b_val, "after": a_val, "budget": budget}
            )
    return {"ok": not violations, "violations": violations}


def accept_candidate(
    profile: QualityProfile,
    *,
    target_before: float,
    target_after: float,
    quality_after_passed: bool,
    correctness_ok: bool,
    constraints_ok: bool,
    regression_ok: bool,
    fragmentation_ok: bool = True,
) -> dict:
    """Decide whether a seam/unwrap candidate is kept (G5 ordering).

    Order: correctness → constraints → fragmentation limit (G6) → regression budget →
    full quality pass → relative improvement on the TARGET failing metric
    (``min_improvement_ratio``). Anything else is rejected as insufficient improvement,
    so the loop ends in needs_user_review rather than drifting on noise.

    ``fragmentation_ok`` is the G6 candidate-level fragmentation limit: a cut that creates
    new dust/sliver islands is rejected with ``fragmentation_limit_exceeded`` BEFORE the
    regression budget is consulted, because "the split made the layout unpaintable" is a
    harder objection than "a non-target metric drifted inside its budget".
    """
    measurable = (
        _finite(target_before)
        and _finite(target_after)
        and float(target_before) > 1e-12
    )
    if measurable:
        ratio = (float(target_before) - float(target_after)) / float(target_before)
    else:
        ratio = 0.0

    if not correctness_ok:
        reason = "correctness_regression"
    elif not constraints_ok:
        reason = "constraint_violation"
    elif not fragmentation_ok:
        reason = "fragmentation_limit_exceeded"
    elif not regression_ok:
        reason = "regression_budget_exceeded"
    else:
        reason = None

    if reason is not None:
        return {"accepted": False, "reason": reason, "improvement_ratio": ratio}

    if quality_after_passed:
        return {"accepted": True, "reason": "quality_passed", "improvement_ratio": ratio}

    if measurable and ratio >= profile.min_improvement_ratio:
        return {
            "accepted": True,
            "reason": "improvement_ratio",
            "improvement_ratio": ratio,
        }

    return {
        "accepted": False,
        "reason": "insufficient_improvement",
        "improvement_ratio": ratio,
    }
