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
    "distortion", "catastrophic", "repair", "correctness", "mandatory", "fragmentation",
    "texel_density", "packing", "island_connectivity", "border_inset", "merge_back",
    "shading",
}

#: The five review layers, in order (CG3).
LAYER_NAMES = ["A_correctness", "B_catastrophic", "C_island_quality", "D_seam_economy",
               "E_game_production"]


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
        "hard_failures", "quality_failures", "distortion_summary", "uv_hash",
        "sections", "layers",
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


# ------------------------------------------------- CG2/CG3 catastrophic + repair + layers


def test_catastrophic_section_and_layers(monkeypatch):
    measurement = _measurement(monkeypatch)
    report = build_quality_report(measurement, PROFILE)

    section = report["sections"]["catastrophic"]
    assert section["passed"] == bool(measurement["catastrophic"]["passed"]
                                     and measurement["catastrophic"]["valid"])
    assert [c["name"] for c in section["checks"]] == [
        "catastrophic_hard", "catastrophic_region", "catastrophic_valid"]
    # compact view only: no per-face arrays / regions / worst triangles in the report.
    for dropped in ("regions", "worst_triangles", "per_face_score", "per_face_hard_fail"):
        assert dropped not in section

    assert report["uv_hash"] == measurement["uv_hash"]
    assert [layer["name"] for layer in report["layers"]] == LAYER_NAMES
    assert all(isinstance(layer["passed"], bool) for layer in report["layers"])
    json.dumps(report)


def test_repair_section_is_carried_through(monkeypatch):
    measurement = _measurement(monkeypatch)
    repair = {"rounds": 2, "reunwrap_accepted": 1, "relief_accepted": 1, "rejected": 0,
              "reason": "r2_accepted", "rows": []}

    assert build_quality_report(measurement, PROFILE)["sections"]["repair"] is None
    report = build_quality_report(measurement, PROFILE, repair=repair)
    assert report["sections"]["repair"]["rounds"] == 2
    assert report["sections"]["repair"]["reason"] == "r2_accepted"


def test_failing_catastrophic_section_adds_the_hard_failure(monkeypatch):
    measurement = dict(_measurement(monkeypatch))
    measurement["catastrophic"] = {**measurement["catastrophic"],
                                   "passed": False, "valid": True, "hard_failed": True,
                                   "region_failed": False}
    # A synthetic measurement whose own hard_failures list has not been recomputed: the
    # report must still name the failure it can see in the section.
    measurement["hard_failures"] = [f for f in measurement["hard_failures"]
                                    if f != "catastrophic_failed"]
    report = build_quality_report(measurement, PROFILE)

    assert report["sections"]["catastrophic"]["passed"] is False
    assert "catastrophic_failed" in report["hard_failures"]
    layers = {layer["name"]: layer["passed"] for layer in report["layers"]}
    assert layers["B_catastrophic"] is False
