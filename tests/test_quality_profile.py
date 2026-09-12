"""Unit tests for the frozen quality profile (G8), the regression budget and candidate
acceptance rules (G5), and metric_version handling (G3)."""

from __future__ import annotations

import math

import pytest

from chart_uv_agent.quality_profile import (
    ENGINEERING_V0,
    PROFILES,
    REQUIRED_PROFILE_KEYS,
    QualityProfile,
    accept_candidate,
    evaluate_quality,
    load_quality_profile,
    regression_within_budget,
)


def _report(**overrides) -> dict:
    report = {
        "metric_version": 2,
        "valid": True,
        "global": {
            "anisotropy_mean": 1.1,
            "anisotropy_p95": 1.3,
            "anisotropy_max": 2.0,
            "area_stretch_mean": 0.20,
            "area_stretch_p95": 0.35,
            "area_stretch_max": 0.40,
            "exceed_area_fraction": 0.01,
        },
        "islands": [
            {
                "island_id": 0,
                "face_count": 120,
                "area_3d": 4.0,
                "anisotropy_p95": 1.4,
                "anisotropy_max": 2.2,
                "area_stretch_mean": 0.25,
                "exceed_area_fraction": 0.03,
            }
        ],
        "degenerate_triangles": {"input_defect_count": 0, "uv_degenerate_count": 0},
    }
    report.update(overrides)
    return report


# --- profile loading (G8) -----------------------------------------------------------


def test_required_keys_cover_every_field():
    assert set(REQUIRED_PROFILE_KEYS) == set(ENGINEERING_V0.to_dict())


def test_load_none_returns_engineering_default():
    assert load_quality_profile(None) is ENGINEERING_V0
    assert ENGINEERING_V0.calibrated is False
    assert PROFILES["engineering_v0"] is ENGINEERING_V0


def test_load_by_string_id():
    assert load_quality_profile("engineering_v0") == ENGINEERING_V0
    with pytest.raises(KeyError):
        load_quality_profile("no_such_profile")


def test_missing_required_key_raises_value_error():
    data = ENGINEERING_V0.to_dict()
    del data["anisotropy_p95_cap_island"]
    del data["regression_budget"]
    with pytest.raises(ValueError) as exc:
        load_quality_profile(data)
    message = str(exc.value)
    assert "anisotropy_p95_cap_island" in message
    assert "regression_budget" in message


def test_extra_key_raises_value_error():
    data = ENGINEERING_V0.to_dict()
    data["anisotropy_p99_cap"] = 2.0
    with pytest.raises(ValueError) as exc:
        load_quality_profile(data)
    assert "anisotropy_p99_cap" in str(exc.value)


def test_to_dict_round_trip():
    assert load_quality_profile(ENGINEERING_V0.to_dict()) == ENGINEERING_V0


def test_regression_budget_for_missing_metric_is_zero():
    assert ENGINEERING_V0.regression_budget_for("anisotropy_p95") == 0.05
    assert ENGINEERING_V0.regression_budget_for("angle_distortion_mean") == 0.0


# --- evaluate_quality (G3 / G8) -----------------------------------------------------


def test_evaluate_quality_passing_report():
    result = evaluate_quality(ENGINEERING_V0, _report())
    assert result["valid"] is True
    assert result["passed"] is True
    assert result["failures"] == []
    assert result["invalid_reasons"] == []
    assert result["profile_id"] == "engineering_v0"
    assert result["metric_version"] == 2
    assert result["calibrated"] is False
    assert any(c["scope"] == "island" and c["island_id"] == 0 for c in result["checks"])


def test_evaluate_quality_nan_is_invalid_not_zero():
    report = _report()
    report["global"]["anisotropy_p95"] = float("nan")
    result = evaluate_quality(ENGINEERING_V0, report)
    assert result["valid"] is False
    assert result["passed"] is False
    assert "global.anisotropy_p95" in result["invalid_reasons"]
    assert not any(
        c["name"] == "anisotropy_p95" and c["scope"] == "global"
        for c in result["checks"]
    )


def test_evaluate_quality_missing_value_is_invalid():
    report = _report()
    del report["global"]["exceed_area_fraction"]
    result = evaluate_quality(ENGINEERING_V0, report)
    assert result["valid"] is False
    assert result["passed"] is False
    assert "global.exceed_area_fraction" in result["invalid_reasons"]


def test_evaluate_quality_metric_version_mismatch():
    result = evaluate_quality(ENGINEERING_V0, _report(metric_version=1))
    assert result["valid"] is False
    assert result["passed"] is False
    assert "metric_version_mismatch" in result["invalid_reasons"]


def test_evaluate_quality_island_failure():
    report = _report()
    report["islands"][0]["anisotropy_p95"] = 2.5
    result = evaluate_quality(ENGINEERING_V0, report)
    assert result["valid"] is True
    assert result["passed"] is False
    assert "anisotropy_p95" in result["failures"]
    failing = [
        c
        for c in result["checks"]
        if c["scope"] == "island" and c["name"] == "anisotropy_p95"
    ]
    assert failing[0]["passed"] is False
    assert failing[0]["limit"] == ENGINEERING_V0.anisotropy_p95_cap_island


def test_evaluate_quality_uv_degenerate_failure():
    report = _report()
    report["degenerate_triangles"]["uv_degenerate_count"] = 3
    result = evaluate_quality(ENGINEERING_V0, report)
    assert result["valid"] is True
    assert result["passed"] is False
    assert "uv_degenerate_triangles" in result["failures"]


def test_evaluate_quality_invalid_flag():
    result = evaluate_quality(ENGINEERING_V0, _report(valid=False))
    assert result["valid"] is False
    assert result["passed"] is False
    assert "distortion_report_invalid" in result["invalid_reasons"]


# --- regression_within_budget (G5) --------------------------------------------------


def test_regression_within_budget_ok():
    before = {
        "anisotropy_p95": 1.0,
        "anisotropy_max": 2.0,
        "area_stretch_mean": 0.20,
        "exceed_area_fraction": 0.02,
    }
    after = dict(before)
    after["anisotropy_p95"] = 0.5  # target metric improved
    after["area_stretch_mean"] = 0.21  # +5% == budget
    result = regression_within_budget(
        ENGINEERING_V0, before, after, target="anisotropy_p95"
    )
    assert result["ok"] is True
    assert result["violations"] == []


def test_regression_over_budget():
    before = {
        "anisotropy_p95": 1.0,
        "anisotropy_max": 2.0,
        "area_stretch_mean": 0.20,
        "exceed_area_fraction": 0.02,
    }
    after = dict(before)
    after["area_stretch_mean"] = 0.30
    result = regression_within_budget(
        ENGINEERING_V0, before, after, target="anisotropy_p95"
    )
    assert result["ok"] is False
    assert [v["metric"] for v in result["violations"]] == ["area_stretch_mean"]
    assert result["violations"][0]["budget"] == 0.05


def test_regression_budget_missing_metric_is_zero():
    profile = QualityProfile(regression_budget={"anisotropy_p95": 0.5})
    before = {
        "anisotropy_p95": 1.0,
        "anisotropy_max": 2.0,
        "area_stretch_mean": 0.20,
        "exceed_area_fraction": 0.02,
    }
    after = dict(before)
    after["anisotropy_max"] = 2.0000001
    result = regression_within_budget(profile, before, after, target="area_stretch_mean")
    assert result["ok"] is False
    assert [v["metric"] for v in result["violations"]] == ["anisotropy_max"]
    assert result["violations"][0]["budget"] == 0.0


def test_regression_zero_before_uses_absolute_budget():
    before = {
        "anisotropy_p95": 1.0,
        "anisotropy_max": 2.0,
        "area_stretch_mean": 0.20,
        "exceed_area_fraction": 0.0,
    }
    ok_after = dict(before, exceed_area_fraction=0.02)
    bad_after = dict(before, exceed_area_fraction=0.03)
    target = "anisotropy_p95"
    assert regression_within_budget(ENGINEERING_V0, before, ok_after, target=target)["ok"]
    assert not regression_within_budget(
        ENGINEERING_V0, before, bad_after, target=target
    )["ok"]


def test_regression_nan_is_not_ok():
    before = {
        "anisotropy_p95": 1.0,
        "anisotropy_max": 2.0,
        "area_stretch_mean": 0.20,
        "exceed_area_fraction": 0.02,
    }
    after = dict(before, anisotropy_max=float("nan"))
    result = regression_within_budget(
        ENGINEERING_V0, before, after, target="anisotropy_p95"
    )
    assert result["ok"] is False
    assert result["violations"][0]["metric"] == "anisotropy_max"
    assert math.isnan(result["violations"][0]["after"])


# --- accept_candidate (G5) ----------------------------------------------------------


def _accept(**overrides) -> dict:
    kwargs = {
        "target_before": 2.0,
        "target_after": 1.0,
        "quality_after_passed": False,
        "correctness_ok": True,
        "constraints_ok": True,
        "regression_ok": True,
    }
    kwargs.update(overrides)
    return accept_candidate(ENGINEERING_V0, **kwargs)


def test_accept_candidate_correctness_regression():
    result = _accept(correctness_ok=False, constraints_ok=False, regression_ok=False)
    assert result == {
        "accepted": False,
        "reason": "correctness_regression",
        "improvement_ratio": pytest.approx(0.5),
    }


def test_accept_candidate_constraint_violation():
    result = _accept(constraints_ok=False, regression_ok=False)
    assert result["accepted"] is False
    assert result["reason"] == "constraint_violation"


def test_accept_candidate_regression_budget_exceeded():
    result = _accept(regression_ok=False)
    assert result["accepted"] is False
    assert result["reason"] == "regression_budget_exceeded"


def test_accept_candidate_quality_passed():
    result = _accept(quality_after_passed=True, target_before=1.0, target_after=1.0)
    assert result["accepted"] is True
    assert result["reason"] == "quality_passed"


def test_accept_candidate_improvement_ratio():
    result = _accept(target_before=1.0, target_after=0.80)
    assert result["accepted"] is True
    assert result["reason"] == "improvement_ratio"
    assert result["improvement_ratio"] == pytest.approx(0.20)


def test_accept_candidate_insufficient_improvement():
    result = _accept(target_before=1.0, target_after=0.95)
    assert result["accepted"] is False
    assert result["reason"] == "insufficient_improvement"
    assert result["improvement_ratio"] == pytest.approx(0.05)


def test_accept_candidate_zero_target_before():
    result = _accept(target_before=0.0, target_after=0.0)
    assert result["accepted"] is False
    assert result["reason"] == "insufficient_improvement"
    assert result["improvement_ratio"] == 0.0
