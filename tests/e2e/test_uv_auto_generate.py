"""Blender-gated e2e evidence for the ``auto_generate`` UV worker path.

Gates: G2 (mode branching — an automatic run needs no seam source, a
``preserve_existing`` run with nothing to preserve still ends ``needs_input``, a
contradictory flag combination is rejected BEFORE the engine runs), G0 (mesh identity
+ run manifest), G1 (mandatory-90 audit on the re-read artifact), G6 (status /
approval-file protection), G7 (seam overlay reasons + reviewer feedback reapplied only
on a matching mesh fingerprint).

Without a Blender executable every test here skips (``requires_blender``). The baseline
fixtures are rebuilt ONCE per module into ``tmp_path`` via
``tests/e2e/fixtures/build_fixture_models.py`` — nothing is written into the repository.

Every assertion reads the JSON artifacts, never stdout (plan §4.1).
"""

from __future__ import annotations

import json
import os

import pytest

from tests.e2e.blender_env import requires_blender, run_blender_python

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_BUILD_SCRIPT = os.path.join(_HERE, "fixtures", "build_fixture_models.py")
_WORKER = os.path.join(_ROOT, "worker", "generate_uv_from_seams.py")
_OBJECT = "Fixture"

# A cube packed at the engine's fixed 0.005 unwrap margin leaves ~2px of island gap at
# 1024px, so the run is measured against a 1px margin: the point of these tests is the
# mode/identity/audit machinery, not a calibrated packing budget (G8 owns that).
_OPTIONS = {"render_size_px": 300, "texture_size_px": 1024, "margin_px": 1}


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory) -> dict:
    """Build the baseline fixture set once for this module (G0)."""
    out_dir = str(tmp_path_factory.mktemp("uv_auto_fixtures"))
    proc = run_blender_python(_BUILD_SCRIPT, ["--out", out_dir])
    assert proc.returncode == 0, (proc.stdout or "")[-2000:] + "\n" + (proc.stderr or "")[-4000:]
    manifest_path = os.path.join(out_dir, "fixture_manifest.json")
    assert os.path.exists(manifest_path), "fixture_manifest.json was written"
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    return {"dir": out_dir, "manifest": manifest,
            "by_name": {f["name"]: f for f in manifest["fixtures"]}}


def _model(fixtures: dict, name: str) -> str:
    return os.path.join(fixtures["dir"], fixtures["by_name"][name]["blend"])


def _job(model: str, out_dir: str, project: str, mode: str, **extra) -> dict:
    job = {
        "command": "generate_uv_from_seams",
        "project_id": "e2e_auto",
        "run_id": "uv_auto_e2e",
        "model": model,
        "model_rel": os.path.basename(model),
        "object_name": _OBJECT,
        "seam_spec": None,
        "mode": mode,
        "out_dir": out_dir,
        "selected_blend_out": os.path.join(project, "work", "uv", "selected_uv.blend"),
        "selected_blend_out_rel": os.path.join("work", "uv", "selected_uv.blend"),
        "selected_summary_out": os.path.join(project, "work", "uv", "selected_uv_summary.json"),
        "options": dict(_OPTIONS),
    }
    job.update(extra)
    return job


def _run(job: dict, tmp_path, tag: str = "job"):
    job_path = str(tmp_path / f"{tag}.json")
    with open(job_path, "w", encoding="utf-8") as fh:
        json.dump(job, fh)
    return run_blender_python(_WORKER, ["--job", job_path], timeout=900)


def _read(out_dir: str, name: str) -> dict:
    with open(os.path.join(out_dir, name), encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# a. auto_generate with no spec at all (G2/G0/G1/G6/G7)
# ---------------------------------------------------------------------------
@requires_blender
def test_auto_generate_cube_without_spec(fixtures, tmp_path):
    project = str(tmp_path)
    out_dir = str(tmp_path / "run")
    os.makedirs(out_dir, exist_ok=True)
    proc = _run(_job(_model(fixtures, "cube"), out_dir, project, "auto_generate"), tmp_path)
    assert proc.returncode == 0, (proc.stdout or "")[-2000:] + "\n" + (proc.stderr or "")[-4000:]

    status = _read(out_dir, "status.json")
    assert status["status"] in ("accepted", "needs_user_review"), status
    assert status["input"]["mode"] == "auto_generate"

    summary = _read(out_dir, "uv_generate_summary.json")
    assert summary["mode"] == "auto_generate"
    assert summary["auto_gate"] is not None

    # G3: the v2 metric block is present and versioned.
    assert summary["distortion_v2"]["metric_version"] == 2

    # G1: the mandatory-90 rule holds on the seam set AND in the real UV.
    assert summary["mandatory_audit"]["mandatory_90_missing"] == 0
    assert summary["mandatory_audit"]["mandatory_90_uv_unsplit"] == 0
    assert summary["correctness"]["passed"] is True
    assert summary["final_reread_audit"]["passed"] is True

    # G0: the approved low-poly was not modified.
    assert summary["mesh_identity"]["unchanged"] is True

    # Evidence artifacts exist and are registered.
    for key in ("distortion_v2", "correctness", "final_reread_audit", "run_manifest",
                "candidate_history", "mesh_identity", "quality_profile", "seam_overlay",
                "selected_heatmap_anisotropy"):
        assert key in summary["artifacts"], key
        assert os.path.exists(os.path.join(out_dir, summary["artifacts"][key])), key

    manifest = _read(out_dir, "run_manifest.json")
    assert manifest["blender_version"]
    assert manifest["code_sha"]
    assert manifest["model_sha256"]

    # G6: only an accepted run ships to work/uv.
    selected = os.path.join(project, "work", "uv", "selected_uv.blend")
    if status["status"] == "accepted":
        assert os.path.exists(selected)
    else:
        assert not os.path.exists(selected)


# ---------------------------------------------------------------------------
# b. preserve_existing with nothing to preserve (G2 compatibility)
# ---------------------------------------------------------------------------
@requires_blender
def test_preserve_existing_without_spec_or_uv_is_needs_input(fixtures, tmp_path):
    project = str(tmp_path)
    out_dir = str(tmp_path / "run")
    os.makedirs(out_dir, exist_ok=True)
    proc = _run(_job(_model(fixtures, "cube"), out_dir, project, "preserve_existing"), tmp_path)
    assert proc.returncode == 0, (proc.stdout or "")[-2000:] + "\n" + (proc.stderr or "")[-4000:]

    status = _read(out_dir, "status.json")
    assert status["status"] == "needs_input", status
    assert status["error"]["code"] == "missing_seam_source"
    assert status["input"]["mode"] == "preserve_existing"
    assert not os.path.exists(os.path.join(project, "work", "uv", "selected_uv.blend"))


# ---------------------------------------------------------------------------
# c. contradictory flags are rejected BEFORE the engine runs (G2)
# ---------------------------------------------------------------------------
@requires_blender
def test_auto_generate_rejects_contradictory_flags(fixtures, tmp_path):
    project = str(tmp_path)
    out_dir = str(tmp_path / "run")
    os.makedirs(out_dir, exist_ok=True)
    job = _job(_model(fixtures, "cube"), out_dir, project, "auto_generate")
    job["options"] = {"enforce_user_mandatory": False}
    proc = _run(job, tmp_path)
    assert proc.returncode == 2, (proc.stdout or "")[-2000:] + "\n" + (proc.stderr or "")[-4000:]

    status = _read(out_dir, "status.json")
    assert status["status"] == "failed", status
    assert status["error"]["code"] == "contradictory_flags"
    # Nothing ran, so nothing was produced.
    assert not os.path.exists(os.path.join(out_dir, "selected_uv.blend"))
    assert not os.path.exists(os.path.join(project, "work", "uv", "selected_uv.blend"))


# ---------------------------------------------------------------------------
# d. 89.9 / 90.0 / 90.1 boundary classification in the seam overlay (G1/G7)
# ---------------------------------------------------------------------------
@requires_blender
def test_angle_boundary_overlay_marks_only_the_90_degree_folds(fixtures, tmp_path):
    project = str(tmp_path)
    out_dir = str(tmp_path / "run")
    os.makedirs(out_dir, exist_ok=True)
    proc = _run(_job(_model(fixtures, "angle_boundary"), out_dir, project, "auto_generate"),
                tmp_path)
    assert proc.returncode == 0, (proc.stdout or "")[-2000:] + "\n" + (proc.stderr or "")[-4000:]

    overlay = _read(out_dir, "seam_overlay.json")

    def _key(a, b) -> tuple:
        pa = tuple(round(float(c), 6) for c in a)
        pb = tuple(round(float(c), 6) for c in b)
        return tuple(sorted((pa, pb)))

    types_by_coord = {_key(e["a"], e["b"]): e["type"] for e in overlay["edges"]}

    folds = {f["angle_deg"]: f["fold_edge_verts"]
             for f in fixtures["by_name"]["angle_boundary"]["notes"]["folds"]}
    for deg in (90.0, 90.1):
        key = _key(*folds[deg])
        assert key in types_by_coord, f"{deg}deg fold edge is a shipped seam"
        assert types_by_coord[key] == "mandatory_90", deg
    assert types_by_coord.get(_key(*folds[89.9])) != "mandatory_90", "89.9deg is not mandatory"


# ---------------------------------------------------------------------------
# e. reviewer feedback is reapplied only on a matching fingerprint (G7)
# ---------------------------------------------------------------------------
@requires_blender
def test_reviewer_feedback_applies_only_on_matching_fingerprint(fixtures, tmp_path):
    project = str(tmp_path)
    model = _model(fixtures, "cube")

    # 1. A first automatic run establishes the mesh fingerprint + the seam overlay.
    first_dir = str(tmp_path / "run1")
    os.makedirs(first_dir, exist_ok=True)
    proc = _run(_job(model, first_dir, project, "auto_generate"), tmp_path, tag="job1")
    assert proc.returncode == 0, (proc.stdout or "")[-2000:] + "\n" + (proc.stderr or "")[-4000:]
    fingerprint = _read(first_dir, "mesh_identity.json")["before"]["fingerprint"]
    assert fingerprint

    overlay = _read(first_dir, "seam_overlay.json")
    non_mandatory = [e["edge_id"] for e in overlay["edges"] if e["type"] != "mandatory_90"]
    locked_edge = non_mandatory[0] if non_mandatory else 0

    # 2. Matching fingerprint -> the saved lock is applied and ships.
    feedback_path = str(tmp_path / "uv_feedback.json")
    with open(feedback_path, "w", encoding="utf-8") as fh:
        json.dump({"locked_seam_edges": [locked_edge], "protected_edges": [],
                   "preferred_edges": [], "mesh_fingerprint": fingerprint}, fh)

    match_dir = str(tmp_path / "run2")
    os.makedirs(match_dir, exist_ok=True)
    proc = _run(_job(model, match_dir, project, "auto_generate",
                     feedback=feedback_path, feedback_rel="work/uv/uv_feedback.json"),
                tmp_path, tag="job2")
    assert proc.returncode == 0, (proc.stdout or "")[-2000:] + "\n" + (proc.stderr or "")[-4000:]
    summary = _read(match_dir, "uv_generate_summary.json")
    assert summary["feedback_applied"]["applied"] is True
    assert summary["feedback_applied"]["reason"] == "fingerprint_match"
    assert summary["feedback_applied"]["locked_seam_count"] == 1
    assert locked_edge in _read(match_dir, "p5_gate.json")["seams"]

    # 3. A stale fingerprint must NEVER silently reuse the saved constraints.
    with open(feedback_path, "w", encoding="utf-8") as fh:
        json.dump({"locked_seam_edges": [locked_edge], "protected_edges": [],
                   "preferred_edges": [], "mesh_fingerprint": "deadbeef"}, fh)
    stale_dir = str(tmp_path / "run3")
    os.makedirs(stale_dir, exist_ok=True)
    proc = _run(_job(model, stale_dir, project, "auto_generate",
                     feedback=feedback_path, feedback_rel="work/uv/uv_feedback.json"),
                tmp_path, tag="job3")
    assert proc.returncode == 0, (proc.stdout or "")[-2000:] + "\n" + (proc.stderr or "")[-4000:]
    stale = _read(stale_dir, "uv_generate_summary.json")
    assert stale["feedback_applied"]["applied"] is False
    assert stale["feedback_applied"]["reason"] == "fingerprint_mismatch"
    assert stale["feedback_applied"]["locked_seam_count"] == 0
