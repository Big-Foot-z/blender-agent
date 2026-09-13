"""Tests for the MVP 5 export contract (plan §4, §5, §6, §7, §8, Session A).

Pure-Python: these load ``worker/app_export_contract.py`` stand-alone (no Blender)
and exercise readiness derivation, the export-status policy, manifest/source
builders, validation report classification, and the status + history helpers.
"""

import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def _load_contract():
    path = os.path.join(_ROOT, "worker", "app_export_contract.py")
    spec = importlib.util.spec_from_file_location("app_export_contract", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


contract = _load_contract()


def _accepted_summary():
    return {
        "schema_version": 1,
        "status": "accepted",
        "selected_candidate_id": "slim_concave_m002",
        "selected_uv_model": "work/uv/selected_uv.blend",
        "metrics": {
            "stretch_score": 0.0686612,
            "worst_island_distortion": 0.2029991,
            "raster_overlap_ratio": 0.0,
            "overlap_ratio": 0.0,
            "texel_density_variance": 0.0000021,
            "packing_efficiency": 0.5912781,
            "island_count": 52,
            "uv_bounds_ok": True,
        },
        "seam_integrity": {"valid": True, "user_seam_count": 724, "final_seam_count": 724,
                           "auto_added_seams": 0},
    }


# --- options (plan §5.1) ---------------------------------------------------
def test_default_options():
    o = contract.default_options()
    assert o["apply_scale"] is True
    assert o["include_materials"] is True
    assert o["include_normals"] is True
    assert o["copy_textures"] is False
    assert o["triangulate"] is False
    assert o["selected_uv_layer"] is None


def test_default_options_is_a_copy():
    a = contract.default_options()
    a["apply_scale"] = False
    assert contract.default_options()["apply_scale"] is True


def test_merge_options_overlays():
    m = contract.merge_options({"triangulate": True, "export_name": "Pot_low"})
    assert m["triangulate"] is True
    assert m["export_name"] == "Pot_low"
    assert m["include_normals"] is True  # untouched default


def test_manifest_options_is_the_persisted_subset():
    m = contract.manifest_options(contract.merge_options({"axis_forward": "Y"}))
    assert set(m.keys()) == set(contract.MANIFEST_OPTION_KEYS)
    assert "axis_forward" not in m  # axis is worker-internal, not persisted


# --- format normalization + filenames (plan §5.1) --------------------------
def test_normalize_formats_dedups_filters_keeps_order():
    assert contract.normalize_formats(["FBX", ".obj", "glb", "obj", "stl"]) == ["fbx", "obj", "glb"]
    assert contract.normalize_formats(["glb", "gltf"]) == ["glb", "gltf"]  # both survive
    assert contract.normalize_formats(None) == []


def test_export_filename_uses_export_name_then_object_fallback():
    assert contract.export_filename("SM_Pot_low_uv", "SM_Pot", "fbx") == "SM_Pot_low_uv.fbx"
    assert contract.export_filename(None, "SM_Pot", "obj") == "SM_Pot_low_uv.obj"
    assert contract.export_filename("", None, "glb") == "model_low_uv.glb"


# --- readiness (plan §4) ---------------------------------------------------
def test_readiness_accepted_when_all_checks_pass():
    checks = contract.readiness_checks_from_summary(
        _accepted_summary(), model_exists=True, summary_exists=True)
    out = contract.build_readiness(checks, selected_uv_model="work/uv/selected_uv.blend",
                                   source_uv_run_id="uv_run_x")
    assert out["status"] == contract.READY_ACCEPTED
    assert out["ready"] is True
    assert out["blocking_issues"] == []
    assert out["checks"]["uv_run_accepted"] is True
    assert out["checks"]["ai_review_skipped"] is True
    # AI review skip is a warning, never a blocker (plan §0)
    assert any("AI Review" in w for w in out["warnings"])


def test_readiness_missing_model_is_needs_input():
    checks = contract.readiness_checks_from_summary(None, model_exists=False, summary_exists=False)
    out = contract.build_readiness(checks)
    assert out["status"] == contract.READY_NEEDS_INPUT
    assert out["ready"] is False
    codes = {i["code"] for i in out["blocking_issues"]}
    assert "missing_selected_uv_model" in codes
    assert "missing_selected_uv_summary" in codes


def test_readiness_raster_overlap_blocks():
    summary = _accepted_summary()
    summary["metrics"]["raster_overlap_ratio"] = 0.02
    checks = contract.readiness_checks_from_summary(summary, model_exists=True, summary_exists=True)
    out = contract.build_readiness(checks)
    assert out["ready"] is False
    assert any(i["code"] == "raster_overlap" for i in out["blocking_issues"])


def test_readiness_needs_review_run_blocks():
    summary = _accepted_summary()
    summary["status"] = "needs_user_review"
    summary["seam_integrity"]["valid"] = False
    checks = contract.readiness_checks_from_summary(summary, model_exists=True, summary_exists=True)
    out = contract.build_readiness(checks)
    assert out["ready"] is False
    codes = {i["code"] for i in out["blocking_issues"]}
    assert "uv_run_not_accepted" in codes
    assert "seam_integrity_failed" in codes


def test_readiness_out_of_bounds_blocks():
    summary = _accepted_summary()
    summary["metrics"]["uv_bounds_ok"] = False
    checks = contract.readiness_checks_from_summary(summary, model_exists=True, summary_exists=True)
    out = contract.build_readiness(checks)
    assert any(i["code"] == "uv_out_of_bounds" for i in out["blocking_issues"])


# --- export status policy (plan §5) ----------------------------------------
def test_classify_export_status():
    assert contract.classify_export_status(["fbx", "obj", "glb"], ["fbx", "obj", "glb"]) == "accepted"
    assert contract.classify_export_status(["fbx", "obj", "glb"], ["fbx", "obj"]) == "partial"
    assert contract.classify_export_status(["fbx", "obj", "glb"], []) == "failed"
    # a stray succeeded format not in the request is ignored
    assert contract.classify_export_status(["fbx"], ["fbx", "glb"]) == "accepted"


# --- validation report (plan §7) -------------------------------------------
def test_format_validation_ok_requires_reopen_and_uv():
    assert contract.format_validation_ok({"reopen_ok": True, "has_uv": True}) is True
    assert contract.format_validation_ok({"reopen_ok": True, "has_uv": False}) is False  # missing UV hard-fails
    assert contract.format_validation_ok({"reopen_ok": False, "has_uv": True}) is False
    # normals/materials missing does NOT fail (plan §7 tolerance)
    assert contract.format_validation_ok(
        {"reopen_ok": True, "has_uv": True, "has_normals": False}) is True


def test_build_validation_report_status_derivation():
    good = {"reopen_ok": True, "has_uv": True, "uv_layers": ["AI_UV"]}
    bad = {"reopen_ok": True, "has_uv": False, "uv_layers": []}
    assert contract.build_validation_report({"fbx": good, "obj": good})["status"] == "accepted"
    assert contract.build_validation_report({"fbx": good, "glb": bad})["status"] == "partial"
    assert contract.build_validation_report({"glb": bad})["status"] == "failed"
    assert contract.build_validation_report({})["status"] == "failed"


# --- manifest + source builders (plan §5.1, §6) ----------------------------
def test_build_manifest_shape_and_metrics():
    src = contract.build_manifest_source(
        selected_uv_model="work/uv/selected_uv.blend",
        selected_uv_summary="work/uv/selected_uv_summary.json",
        uv_generate_run_id="uv_run_x",
        active_user_seam_spec="work/seams/user_seam_spec.json",
        candidate_summary="runs/uv_run_x/candidate_summary.json",
        p5_gate="runs/uv_run_x/p5_gate.json",
        seam_report="runs/uv_run_x/seam_report.json")
    manifest = contract.build_export_manifest(
        export_id="export_x", created_at="2026-06-20T00:00:00.000Z", status="accepted",
        formats=["fbx", "obj", "glb"], options=contract.merge_options({"triangulate": True}),
        source=src, metrics=_accepted_summary()["metrics"],
        files={"fbx": "SM_Pot_low_uv.fbx", "obj": "SM_Pot_low_uv.obj", "glb": "SM_Pot_low_uv.glb"})
    assert manifest["schema_version"] == 1
    assert manifest["export_id"] == "export_x"
    assert manifest["status"] == "accepted"
    assert manifest["source"]["ai_review_skipped"] is True
    assert manifest["source"]["ai_review_run_id"] is None
    assert manifest["source"]["candidate_summary"] == "runs/uv_run_x/candidate_summary.json"
    # metrics flattened to the headline subset; island_count is NOT in it
    assert set(manifest["metrics"].keys()) <= set(contract.EXPORT_METRIC_KEYS)
    assert "island_count" not in manifest["metrics"]
    assert manifest["metrics"]["packing_efficiency"] == round(0.5912781, 6)
    assert manifest["options"]["triangulate"] is True
    assert manifest["validation"] == contract.VALIDATION_REPORT_FILE


def test_build_export_result_partial_carries_failed_formats():
    src = contract.build_result_source(
        selected_uv_model="work/uv/selected_uv.blend",
        selected_uv_summary="work/uv/selected_uv_summary.json",
        uv_generate_run_id="uv_run_x", seam_spec="work/seams/user_seam_spec.json",
        selected_candidate_id="slim_concave_m002")
    vr = contract.build_validation_report({
        "fbx": {"reopen_ok": True, "has_uv": True},
        "obj": {"reopen_ok": True, "has_uv": True},
    })
    result = contract.build_export_result(
        export_id="export_x", status="partial", source=src,
        exports={"fbx": "exports/export_x/model.fbx", "obj": "exports/export_x/model.obj"},
        validation=vr, artifacts={"manifest": "export_manifest.json"},
        failed_formats=[{"format": "glb", "code": "export_failed", "message": "Blender GLB export failed."}],
        warnings=["GLB export failed; FBX and OBJ were validated."])
    assert result["status"] == "partial"
    assert result["failed_formats"][0]["format"] == "glb"
    assert "glb" not in result["exports"]
    assert result["source"]["ai_review_skipped"] is True


# --- status lifecycle (plan §5, §6) ----------------------------------------
def test_status_lifecycle():
    st = contract.new_status(export_id="export_x", input={"formats": ["fbx"]})
    assert st["status"] == contract.STATUS_QUEUED
    assert st["finished_at"] is None
    contract.finalize_status(st, status=contract.STATUS_PARTIAL,
                             artifacts={"manifest": "export_manifest.json"})
    assert st["status"] == "partial"
    assert st["finished_at"] is not None
    assert contract.STATUS_PARTIAL in contract.TERMINAL_STATUSES
    assert contract.STATUS_PARTIAL in contract.SHIPPED_STATUSES
    assert contract.STATUS_FAILED not in contract.SHIPPED_STATUSES


def test_error_envelope_is_json_safe():
    env = contract.error_envelope(contract.CMD_EXPORT_PRODUCTION_ASSET, "boom",
                                  code="reopen_failed", failed_formats=[{"format": "glb"}])
    assert env["status"] == contract.STATUS_FAILED
    assert env["error"]["code"] == "reopen_failed"
    assert env["failed_formats"] == [{"format": "glb"}]


# --- artifact collection (plan §5.1, §7) -----------------------------------
def test_collect_export_artifacts_present_and_missing(tmp_path):
    d = str(tmp_path)
    for name in ("export_manifest.json", "validation_report.json", "uv_layout.png",
                 "checker_front.png"):
        with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
            fh.write("x")
    artifacts, warnings = contract.collect_export_artifacts(d)
    assert artifacts["manifest"] == contract.MANIFEST_FILE
    assert artifacts["validation_report"] == contract.VALIDATION_REPORT_FILE
    assert artifacts["uv_layout"] == "uv_layout.png"
    assert artifacts["checker_front"] == "checker_front.png"
    assert "checker_side" not in artifacts  # missing preview is silent (best-effort)
    assert warnings == []


# --- history events (plan §8) ----------------------------------------------
def test_make_export_event():
    ev = contract.make_export_event(
        event_id="event_x", created_at="2026-06-20T00:00:00.000Z", export_id="export_x",
        uv_generate_run_id="uv_run_x", selected_candidate_id="slim_concave_m002",
        seam_spec="work/seams/user_seam_spec.json", manifest="exports/export_x/export_manifest.json",
        summary={"formats": ["fbx", "obj", "glb"], "status": "accepted",
                 "raster_overlap_ratio": 0.0, "packing_efficiency": 0.591278})
    assert ev["type"] == contract.EVENT_EXPORT_CREATED
    assert ev["export_id"] == "export_x"
    assert ev["summary"]["status"] == "accepted"


def test_make_export_event_failed_type():
    ev = contract.make_export_event(
        event_id="e", created_at="t", export_id="x", uv_generate_run_id=None,
        selected_candidate_id=None, seam_spec=None, manifest="m", summary={}, failed=True)
    assert ev["type"] == contract.EVENT_EXPORT_FAILED


def test_make_rollback_event():
    ev = contract.make_rollback_event(
        event_id="event_y", created_at="t", target_type=contract.TARGET_UV_RUN,
        target_id="uv_run_x", selected_uv_model="work/uv/selected_uv.blend")
    assert ev["type"] == contract.EVENT_ROLLBACK_PERFORMED
    assert ev["target_type"] == "uv_run"
    assert ev["target_id"] == "uv_run_x"


def test_empty_history():
    h = contract.empty_history()
    assert h["schema_version"] == 1
    assert h["events"] == []


# --- Gate G13 re-read audit gating (export_reread_report.json) -------------
def test_format_validation_ok_reread_none_pass_fail():
    v = {"reopen_ok": True, "has_uv": True}
    # Not audited -> the plan §7 verdict stands.
    assert contract.format_validation_ok(v) is True
    assert contract.format_validation_ok(v, reread=None) is True
    # Audited and passing -> still ok; audited and failing -> not shippable.
    assert contract.format_validation_ok(v, reread={"passed": True}) is True
    assert contract.format_validation_ok(
        v, reread={"passed": False, "failures": ["uv_fingerprint_mismatch"]}) is False
    # A failed re-open is never rescued by a passing audit.
    assert contract.format_validation_ok({"reopen_ok": False, "has_uv": True},
                                         reread={"passed": True}) is False


def test_build_reread_report_all_pass():
    report = contract.build_reread_report(
        {"fbx": {"passed": True}, "glb": {"passed": True}}, profile_id="engineering_v0")
    assert report["schema_version"] == 1
    assert report["profile_id"] == "engineering_v0"
    assert report["passed"] is True
    assert report["failed_formats"] == []
    assert set(report["formats"]) == {"fbx", "glb"}


def test_build_reread_report_names_failed_formats():
    report = contract.build_reread_report(
        {"fbx": {"passed": True},
         "obj": {"passed": False, "failures": ["mandatory_90_uv_unsplit"]}},
        profile_id=None)
    assert report["passed"] is False
    assert report["failed_formats"] == ["obj"]
    assert report["profile_id"] is None


def test_build_reread_report_empty_is_vacuous_pass():
    report = contract.build_reread_report({}, profile_id="engineering_v0")
    assert report["passed"] is True and report["formats"] == {}


def test_artifact_registry_contains_reread_report(tmp_path):
    assert contract.EXPORT_REREAD_REPORT_FILE == "export_reread_report.json"
    artifacts, _warnings = contract.collect_export_artifacts(str(tmp_path))
    assert artifacts["reread_report"] == contract.EXPORT_REREAD_REPORT_FILE


def test_build_export_result_carries_reread():
    reread = contract.build_reread_report({"fbx": {"passed": True}},
                                          profile_id="engineering_v0")
    result = contract.build_export_result(
        export_id="export_x", status=contract.STATUS_ACCEPTED, source={}, exports={},
        validation={}, artifacts={}, reread=reread)
    assert result["reread"] is reread
    # Omitted when no audit ran, so older readers never see an empty block.
    assert "reread" not in contract.build_export_result(
        export_id="export_x", status=contract.STATUS_ACCEPTED, source={}, exports={},
        validation={}, artifacts={})
