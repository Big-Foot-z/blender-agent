"""Gate G15 — ``quality_report.json``: one self-contained, JSON-safe verdict document.

Blender-free: the measurement under test comes from the real
:func:`chart_uv_agent.refinement_loop.unwrap_and_measure` driven by
:class:`tests.helpers.fake_blender_uv.FakeUnwrapBackend`, so the report is asserted
against a genuine measurement rather than a hand-written stub.
"""

from __future__ import annotations

import json

from chart_uv_agent import refinement_loop as R
from chart_uv_agent import segmentation
from chart_uv_agent.fixtures import build_displaced_sphere
from chart_uv_agent.quality_profile import ENGINEERING_V0
from chart_uv_agent.quality_report import SCHEMA_VERSION, build_quality_report
from tests.helpers.fake_blender_uv import FakeUnwrapBackend

PROFILE = ENGINEERING_V0

SECTION_KEYS = {
    "distortion", "correctness", "mandatory", "fragmentation", "texel_density",
    "packing", "island_connectivity", "border_inset", "merge_back", "shading",
}


def _measurement(monkeypatch) -> dict:
    mesh = build_displaced_sphere(segments=12, rings=8)
    backend = FakeUnwrapBackend(mesh)
    obj = backend.install(monkeypatch)
    seams = set(segmentation.segment(mesh).seams)
    return R.unwrap_and_measure(obj, mesh, seams, profile=PROFILE, margin=0.005,
                                stage="final")


def test_quality_report_schema_and_json_round_trip(monkeypatch):
    measurement = _measurement(monkeypatch)
    report = build_quality_report(measurement, PROFILE)

    assert set(report) == {
        "schema_version", "profile_id", "metric_version", "calibrated", "passed",
        "hard_failures", "quality_failures", "distortion_summary", "sections",
    }
    assert report["schema_version"] == SCHEMA_VERSION
    assert report["profile_id"] == PROFILE.profile_id
    assert report["metric_version"] == PROFILE.metric_version
    assert report["calibrated"] == PROFILE.calibrated
    assert set(report["sections"]) == SECTION_KEYS

    # No numpy scalars, no NaN/Infinity tokens — plain json.dumps must accept it.
    text = json.dumps(report)
    assert "NaN" not in text and "Infinity" not in text


def test_quality_report_passed_mirrors_the_measurement(monkeypatch):
    measurement = _measurement(monkeypatch)
    report = build_quality_report(measurement, PROFILE)

    assert report["passed"] == bool(measurement["passed"])
    assert report["hard_failures"] == list(measurement["hard_failures"])
    assert report["quality_failures"] == list(measurement["quality_failures"])
    assert report["sections"]["merge_back"] is None
    assert report["sections"]["shading"] is None

    connectivity = report["sections"]["island_connectivity"]
    assert connectivity["seam_islands"] == measurement["island_count"]
    assert connectivity["uv_islands"] == measurement["uv_island_count"]
    assert connectivity["passed"] is (not measurement["islands_disagree"])


def test_incomplete_merge_back_fails_the_report(monkeypatch):
    measurement = _measurement(monkeypatch)
    report = build_quality_report(measurement, PROFILE,
                                  merge_back={"complete": False, "trials": 3})

    assert "merge_back_incomplete" in report["hard_failures"]
    assert report["passed"] is False
    assert report["sections"]["merge_back"] == {"complete": False, "trials": 3}


def test_failing_shading_policy_fails_the_report(monkeypatch):
    measurement = _measurement(monkeypatch)
    report = build_quality_report(measurement, PROFILE,
                                  shading={"passed": False, "policy": "preserve"})

    assert "shading_policy_failed" in report["hard_failures"]
    assert report["passed"] is False
    assert report["sections"]["shading"]["policy"] == "preserve"


def test_complete_blocks_do_not_add_failures(monkeypatch):
    measurement = _measurement(monkeypatch)
    report = build_quality_report(measurement, PROFILE,
                                  merge_back={"complete": True},
                                  shading={"passed": True})

    assert report["hard_failures"] == list(measurement["hard_failures"])
    assert report["passed"] == bool(measurement["passed"])
