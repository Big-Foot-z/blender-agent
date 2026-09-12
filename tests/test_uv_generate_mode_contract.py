"""Tests for the run-mode / acceptance contract (work plan §3, gates G2 + G6).

Pure-Python: loads ``worker/app_uv_generate_contract.py`` stand-alone (no
Blender) and exercises mode resolution, contradictory-flag rejection, the
per-mode option defaults, the automatic-mode constraint + gate evaluation, the
v2 status classification, the acceptance finalization, and the new summary /
artifact keys.
"""

import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def _load_contract():
    path = os.path.join(_ROOT, "worker", "app_uv_generate_contract.py")
    spec = importlib.util.spec_from_file_location("app_uv_generate_contract", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


contract = _load_contract()


# --- resolve_mode (work plan §3, gate G2) ----------------------------------
def test_resolve_mode_missing_is_preserve_existing():
    assert contract.DEFAULT_MODE == contract.MODE_PRESERVE_EXISTING
    assert contract.resolve_mode(None) == contract.MODE_PRESERVE_EXISTING
    assert contract.resolve_mode("") == contract.MODE_PRESERVE_EXISTING


def test_resolve_mode_passes_known_modes_through():
    assert contract.resolve_mode("auto_generate") == contract.MODE_AUTO_GENERATE
    assert contract.resolve_mode("preserve_existing") == contract.MODE_PRESERVE_EXISTING
    assert set(contract.MODES) == {"auto_generate", "preserve_existing"}


def test_resolve_mode_rejects_unknown():
    try:
        contract.resolve_mode("magic")
    except ValueError as exc:
        assert "unknown_mode" in str(exc)
    else:
        raise AssertionError("expected ValueError for an unknown mode")


# --- validate_mode_request (work plan §3 "모순 플래그 조합은 실행 전에 오류") ---
def test_validate_mode_request_default_preserve_is_ok():
    r = contract.validate_mode_request(None, None)
    assert r["ok"] is True
    assert r["mode"] == contract.MODE_PRESERVE_EXISTING
    assert r["errors"] == []
    # an omitted strict flag takes its default and is NOT a contradiction
    r2 = contract.validate_mode_request("preserve_existing", {"layout_opt_max_candidates": 8})
    assert r2["ok"] is True


def test_validate_mode_request_preserve_rejects_explicit_true_strict_flag():
    r = contract.validate_mode_request(
        "preserve_existing", {"auto_refine_user_seams": True})
    assert r["ok"] is False
    assert r["mode"] == contract.MODE_PRESERVE_EXISTING
    assert [e["code"] for e in r["errors"]] == ["strict_flag_contradicts_preserve"]
    assert r["errors"][0]["flag"] == "auto_refine_user_seams"


def test_validate_mode_request_auto_rejects_explicit_false_strict_flag():
    r = contract.validate_mode_request(
        "auto_generate", {"enforce_user_mandatory": False})
    assert r["ok"] is False
    assert [e["code"] for e in r["errors"]] == ["strict_flag_contradicts_auto"]
    assert r["errors"][0]["flag"] == "enforce_user_mandatory"


def test_validate_mode_request_unknown_mode():
    r = contract.validate_mode_request("magic", {})
    assert r["ok"] is False
    assert r["mode"] is None
    assert [e["code"] for e in r["errors"]] == ["unknown_mode"]


def test_validate_mode_request_mode_mismatch():
    r = contract.validate_mode_request("auto_generate", {"mode": "preserve_existing"})
    assert r["ok"] is False
    assert "mode_mismatch" in [e["code"] for e in r["errors"]]


# --- per-mode options (work plan §3 defaults) ------------------------------
def test_merge_options_legacy_call_is_preserve_existing():
    merged = contract.merge_options({"layout_opt_max_candidates": 8})
    assert merged["mode"] == contract.MODE_PRESERVE_EXISTING
    assert merged["layout_opt_max_candidates"] == 8
    for flag in contract.STRICT_FLAGS:
        assert merged[flag] is False
    # common automation keys are present with their documented defaults
    assert merged["quality_profile"] == "engineering_v0"
    assert merged["seed"] == 0
    assert merged["texture_size_px"] == 1024
    assert merged["margin_px"] == 4
    assert merged["max_iterations"] is None
    assert merged["max_candidates_per_round"] is None
    assert merged["time_budget_s"] is None
    assert merged["island_cap"] is None


def test_merge_options_auto_mode_turns_the_strict_flags_on():
    merged = contract.merge_options(None, contract.MODE_AUTO_GENERATE)
    assert merged["mode"] == contract.MODE_AUTO_GENERATE
    for flag in contract.STRICT_FLAGS:
        assert merged[flag] is True
    assert merged["texture_size_px"] == 1024
    assert contract.default_options(contract.MODE_AUTO_GENERATE) == merged


def test_merge_options_reads_mode_from_user_options():
    merged = contract.merge_options({"mode": "auto_generate", "seed": 7})
    assert merged["mode"] == contract.MODE_AUTO_GENERATE
    assert merged["seed"] == 7
    assert merged["repair_user_seams"] is True
    # an explicit argument beats the options dict
    forced = contract.merge_options({"mode": "auto_generate"}, "preserve_existing")
    assert forced["mode"] == contract.MODE_PRESERVE_EXISTING
    assert forced["repair_user_seams"] is False


# --- evaluate_auto_constraints (work plan §3, gate G4) ---------------------
def test_evaluate_auto_constraints_clean_run():
    r = contract.evaluate_auto_constraints({
        "locked_seam_edges": [1, 2], "locked_missing": [],
        "protected_edges": [9], "protected_cut": [],
        "conflicts": [], "conflicts_unresolved": False,
    })
    assert r["valid"] is True
    assert r["violations"] == []
    assert r["block"]["locked_seam_count"] == 2
    assert r["block"]["protected_edge_count"] == 1
    assert r["block"]["valid"] is True


def test_evaluate_auto_constraints_locked_seam_removed():
    r = contract.evaluate_auto_constraints({
        "locked_seam_edges": [1, 2], "locked_missing": [2]})
    assert r["valid"] is False
    assert r["violations"] == [{"code": "locked_seam_removed", "edges": [2]}]
    assert r["block"]["locked_missing_count"] == 1


def test_evaluate_auto_constraints_protected_cut_and_pending_conflict():
    r = contract.evaluate_auto_constraints({
        "protected_edges": [4, 5], "protected_cut": [5],
        "conflicts": [{"edge_id": 5}, {"edge_id": 6}], "conflicts_unresolved": True})
    codes = [v["code"] for v in r["violations"]]
    assert codes == ["protected_edge_cut", "pending_conflict"]
    assert r["violations"][0]["edges"] == [5]
    assert r["violations"][1]["count"] == 2
    assert r["valid"] is False


def test_evaluate_auto_constraints_none_is_empty_and_valid():
    r = contract.evaluate_auto_constraints(None)
    assert r["valid"] is True
    assert r["violations"] == []
    assert r["block"]["locked_seam_count"] == 0
    assert r["block"]["conflict_count"] == 0
    assert r["block"]["conflicts_unresolved"] is False


# --- evaluate_auto_gate (work plan §3 상태, gates G1/G3/G6) -----------------
def _ok_gate_inputs(**over):
    base = {
        "mandatory": {"mandatory_90_missing": 0, "mandatory_90_uv_unsplit": 0},
        "quality": {"valid": True, "passed": True},
        "correctness": {"passed": True},
        "constraints": {"valid": True},
    }
    base.update(over)
    return base


def test_evaluate_auto_gate_passes_when_everything_holds():
    g = contract.evaluate_auto_gate(**_ok_gate_inputs())
    assert g["valid"] is True
    assert g["passed"] is True
    assert g["failures"] == []
    assert g["invalid_reasons"] == []


def test_evaluate_auto_gate_fails_on_missing_mandatory_seams():
    g = contract.evaluate_auto_gate(**_ok_gate_inputs(
        mandatory={"mandatory_90_missing": 3, "mandatory_90_uv_unsplit": 1}))
    assert g["valid"] is True
    assert g["passed"] is False
    assert g["failures"] == ["mandatory_90_missing", "mandatory_90_uv_unsplit"]


def test_evaluate_auto_gate_quality_invalid_is_not_a_pass():
    g = contract.evaluate_auto_gate(**_ok_gate_inputs(quality=None))
    assert g["valid"] is False and g["passed"] is False
    assert "quality_metrics_invalid" in g["invalid_reasons"]
    g2 = contract.evaluate_auto_gate(**_ok_gate_inputs(
        quality={"valid": False, "passed": True}))
    assert g2["valid"] is False and g2["passed"] is False
    # an evaluatable-but-failing profile is a FAILURE, not an invalid run
    g3 = contract.evaluate_auto_gate(**_ok_gate_inputs(
        quality={"valid": True, "passed": False}))
    assert g3["valid"] is True and g3["passed"] is False
    assert g3["failures"] == ["quality_profile_failed"]


def test_evaluate_auto_gate_correctness_failure_and_missing():
    g = contract.evaluate_auto_gate(**_ok_gate_inputs(correctness={"passed": False}))
    assert g["failures"] == ["correctness_failed"]
    assert g["valid"] is True and g["passed"] is False
    g2 = contract.evaluate_auto_gate(**_ok_gate_inputs(correctness=None))
    assert g2["valid"] is False
    assert "correctness_missing" in g2["invalid_reasons"]


def test_evaluate_auto_gate_constraints_and_reread_failures():
    g = contract.evaluate_auto_gate(**_ok_gate_inputs(constraints={"valid": False}))
    assert g["failures"] == ["constraints_violated"]
    g2 = contract.evaluate_auto_gate(**_ok_gate_inputs(constraints=None))
    assert g2["valid"] is False
    assert "constraints_missing" in g2["invalid_reasons"]
    g3 = contract.evaluate_auto_gate(
        reread_audit={"passed": False}, **_ok_gate_inputs())
    assert g3["valid"] is True and g3["passed"] is False
    assert g3["failures"] == ["reread_audit_failed"]
    g4 = contract.evaluate_auto_gate(reread_audit={"passed": True}, **_ok_gate_inputs())
    assert g4["passed"] is True


def test_evaluate_auto_gate_nan_mandatory_count_is_invalid():
    g = contract.evaluate_auto_gate(**_ok_gate_inputs(
        mandatory={"mandatory_90_missing": float("nan"), "mandatory_90_uv_unsplit": 0}))
    assert g["valid"] is False and g["passed"] is False
    g2 = contract.evaluate_auto_gate(**_ok_gate_inputs(
        mandatory={"mandatory_90_missing": 0, "mandatory_90_uv_unsplit": float("inf")}))
    assert g2["valid"] is False
    g3 = contract.evaluate_auto_gate(**_ok_gate_inputs(mandatory=None))
    assert g3["valid"] is False
    assert "mandatory_audit_missing" in g3["invalid_reasons"]


# --- classify_generate_status_v2 (work plan §3 상태) ------------------------
def test_classify_v2_preserve_delegates_to_the_legacy_rule():
    integrity = {"valid": True}
    quality = {"ok": True}
    assert contract.classify_generate_status_v2(
        "preserve_existing", integrity=integrity, quality=quality) == contract.STATUS_ACCEPTED
    assert contract.classify_generate_status_v2(
        "preserve_existing", integrity={"valid": False}, quality=quality
    ) == contract.STATUS_NEEDS_USER_REVIEW


def test_classify_v2_preserve_missing_inputs_is_needs_user_review():
    assert contract.classify_generate_status_v2(
        "preserve_existing", integrity=None, quality={"ok": True}
    ) == contract.STATUS_NEEDS_USER_REVIEW
    assert contract.classify_generate_status_v2(
        None, integrity={"valid": True}, quality=None) == contract.STATUS_NEEDS_USER_REVIEW


def test_classify_v2_auto_accepts_only_a_valid_passing_gate():
    assert contract.classify_generate_status_v2(
        "auto_generate", auto_gate={"valid": True, "passed": True}
    ) == contract.STATUS_ACCEPTED
    assert contract.classify_generate_status_v2(
        "auto_generate", auto_gate={"valid": True, "passed": False}
    ) == contract.STATUS_NEEDS_USER_REVIEW
    assert contract.classify_generate_status_v2(
        "auto_generate", auto_gate={"valid": False, "passed": False}
    ) == contract.STATUS_NEEDS_USER_REVIEW


def test_classify_v2_auto_without_a_gate_is_needs_user_review():
    assert contract.classify_generate_status_v2(
        "auto_generate", integrity={"valid": True}, quality={"ok": True}
    ) == contract.STATUS_NEEDS_USER_REVIEW


# --- finalize_acceptance (gate G6 승인 파일) --------------------------------
def test_finalize_acceptance_passes_a_fully_shipped_run():
    assert contract.finalize_acceptance(
        contract.STATUS_ACCEPTED, artifacts_saved=True, handoff_ok=True,
        mesh_identity_ok=True) == (contract.STATUS_ACCEPTED, None)


def test_finalize_acceptance_leaves_non_accepted_alone():
    assert contract.finalize_acceptance(
        contract.STATUS_NEEDS_USER_REVIEW, artifacts_saved=False, handoff_ok=False,
        mesh_identity_ok=False) == (contract.STATUS_NEEDS_USER_REVIEW, None)


def test_finalize_acceptance_downgrades_on_save_or_handoff_failure():
    assert contract.finalize_acceptance(
        contract.STATUS_ACCEPTED, artifacts_saved=False, handoff_ok=True,
        mesh_identity_ok=True) == (contract.STATUS_NEEDS_USER_REVIEW,
                                   "required_artifact_save_failed")
    assert contract.finalize_acceptance(
        contract.STATUS_ACCEPTED, artifacts_saved=True, handoff_ok=False,
        mesh_identity_ok=True) == (contract.STATUS_NEEDS_USER_REVIEW, "handoff_failed")


def test_finalize_acceptance_downgrades_on_mesh_identity_change():
    assert contract.finalize_acceptance(
        contract.STATUS_ACCEPTED, artifacts_saved=True, handoff_ok=True,
        mesh_identity_ok=False) == (contract.STATUS_NEEDS_USER_REVIEW,
                                    "mesh_identity_changed")


# --- summary additions (work plan §3, gates G6/G7) -------------------------
def _integrity():
    return {"user_seam_count": 10, "user_protected_count": 0, "final_seam_count": 10,
            "auto_added_seams": 0, "mandatory_rule_enabled": False,
            "mandatory_gate_enabled": False, "valid": True}


def test_build_generate_summary_defaults_the_new_keys():
    s = contract.build_generate_summary(
        run_id="r", status=contract.STATUS_NEEDS_USER_REVIEW, model="m",
        object_name="Pot", seam_spec=None, metrics=None, seam_integrity=_integrity(),
        layout_optimization={"enabled": False}, artifacts={})
    assert s["mode"] == contract.MODE_PRESERVE_EXISTING
    assert s["solver_accepted"] is False
    assert s["artist_approved"] is False
    assert s["artist_approval"] is None
    assert s["acceptance_reason"] is None
    for key in ("quality_profile", "auto_gate", "auto_constraints", "distortion_v2",
                "correctness", "final_reread_audit", "mesh_identity", "termination",
                "seam_length", "mandatory_audit"):
        assert s[key] is None
    # existing keys are untouched
    assert s["schema_version"] == contract.SCHEMA_VERSION
    assert s["command"] == contract.CMD_GENERATE_UV_FROM_SEAMS
    assert s["seam_source"] is None


def test_build_generate_summary_carries_the_automation_blocks():
    auto_gate = {"valid": True, "passed": True, "failures": [], "invalid_reasons": []}
    s = contract.build_generate_summary(
        run_id="r", status=contract.STATUS_ACCEPTED, model="m", object_name="Pot",
        seam_spec=None, metrics=None, seam_integrity=_integrity(),
        layout_optimization={"enabled": True}, artifacts={},
        mode=contract.MODE_AUTO_GENERATE,
        quality_profile={"name": "engineering_v0", "metric_version": 2},
        auto_gate=auto_gate, auto_constraints={"valid": True},
        distortion_v2={"anisotropy_p95": 1.2}, correctness={"passed": True},
        final_reread_audit={"passed": True}, mesh_identity={"changed": False},
        termination={"reason": "converged"}, seam_length={"total": 3.5},
        mandatory_audit={"mandatory_90_missing": 0},
        artist_approval={"approved": True, "reviewer": "a"},
        acceptance_reason=None)
    assert s["mode"] == "auto_generate"
    assert s["solver_accepted"] is True
    assert s["artist_approved"] is True
    assert s["artist_approval"]["reviewer"] == "a"
    assert s["auto_gate"] == auto_gate
    assert s["quality_profile"]["metric_version"] == 2
    assert s["distortion_v2"]["anisotropy_p95"] == 1.2
    assert s["termination"]["reason"] == "converged"


# --- artifact registry additions (work plan §3/§4) -------------------------
def test_artifact_files_carry_the_new_optional_artifacts():
    expected = {
        "distortion_v2": "distortion_v2.json",
        "correctness": "correctness.json",
        "final_reread_audit": "final_reread_audit.json",
        "run_manifest": "run_manifest.json",
        "candidate_history": "candidate_history.json",
        "mesh_identity": "mesh_identity.json",
        "quality_profile": "quality_profile.json",
        "seam_overlay": "seam_overlay.json",
        "selected_heatmap_anisotropy": "selected_heatmap_anisotropy.png",
    }
    for key, filename in expected.items():
        assert key in contract.ARTIFACT_FILES
        assert contract.ARTIFACT_FILES[key] == (filename, False)


# --- input diagnostics (G1 topology/입력) -----------------------------------
def test_evaluate_auto_gate_flags_input_defects():
    bad = {"non_manifold_edge_count": 2, "zero_area_face_count": 1,
           "input_defect_triangle_count": 1, "isolated_vertex_count": 0, "ok": False}
    g = contract.evaluate_auto_gate(**_ok_gate_inputs(), input_diagnostics=bad)
    assert g["valid"] is True and g["passed"] is False
    assert "input_defects" in g["failures"]


def test_evaluate_auto_gate_clean_input_diagnostics_still_passes():
    ok = {"non_manifold_edge_count": 0, "zero_area_face_count": 0,
          "input_defect_triangle_count": 0, "isolated_vertex_count": 0, "ok": True}
    g = contract.evaluate_auto_gate(**_ok_gate_inputs(), input_diagnostics=ok)
    assert g["passed"] is True
    assert g["failures"] == []


def test_evaluate_auto_gate_without_input_diagnostics_is_unchanged():
    base = contract.evaluate_auto_gate(**_ok_gate_inputs())
    same = contract.evaluate_auto_gate(**_ok_gate_inputs(), input_diagnostics=None)
    assert same == base
    assert same["failures"] == []


def test_build_generate_summary_carries_input_diagnostics():
    diag = {"non_manifold_edge_count": 1, "zero_area_face_count": 0,
            "input_defect_triangle_count": 0, "isolated_vertex_count": 0, "ok": False}
    s = contract.build_generate_summary(
        run_id="r", status=contract.STATUS_NEEDS_USER_REVIEW, model="m.blend",
        object_name="Fixture", seam_spec=None, metrics={}, seam_integrity={},
        layout_optimization={}, artifacts={}, mode=contract.MODE_AUTO_GENERATE,
        input_diagnostics=diag)
    assert s["input_diagnostics"] == diag

    legacy = contract.build_generate_summary(
        run_id="r", status=contract.STATUS_ACCEPTED, model="m.blend",
        object_name="Fixture", seam_spec=None, metrics={}, seam_integrity={},
        layout_optimization={}, artifacts={})
    assert legacy["input_diagnostics"] is None
