"""Real-Blender acceptance-gate evidence for the automatic UV path (G1–G6, G9).

Unlike ``test_uv_auto_generate.py`` (which proves the mode/identity/audit machinery on a
cube with a relaxed packing margin), this module runs the **default** options over the
whole baseline fixture set and records what actually happened, fixture by fixture:

- **G1** — mandatory-90 seam set + real UV split, correctness, the 89.9/90.0/90.1
  boundary classification read out of ``seam_overlay.json``.
- **G2** — an automatic run needs no spec; a ``preserve_existing`` round trip keeps the
  seam set byte-for-byte; a contradictory flag combination is rejected before the engine.
- **G3** — the v2 distortion block of every run is recorded (global anisotropy/stretch).
- **G4/G5** — termination reason/budget, candidate counts and auxiliary seam length are
  recorded; three identical runs must produce an identical seam set and identical floats.
- **G6** — status classification: only a passing gate may be ``accepted``; a non-accepted
  run must carry a recorded reason.
- **G9** — the same automatic run through a path containing Hangul and a space.

Every test writes its rows to ``$UV_GATE_EVIDENCE_DIR/uv_auto_gates_<test>.json`` when
that variable is set (the table is written whether or not the assertions hold, so a
failing gate still leaves evidence). Assertions read JSON artifacts, never stdout.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from tests.e2e.blender_env import BLENDER, blender_version_info, requires_blender, run_blender_python

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_BUILD_SCRIPT = os.path.join(_HERE, "fixtures", "build_fixture_models.py")
_WORKER = os.path.join(_ROOT, "worker", "generate_uv_from_seams.py")
_OBJECT = "Fixture"

# Default options everywhere (this module measures the SHIPPED profile). The only
# override is the preview render, which is pure review sugar and dominates the runtime.
_OPTIONS = {"render_previews": False}

_FIXTURE_NAMES = (
    "plane_grid", "cube", "bevel_cube", "cylinder_capped", "uv_sphere", "torus",
    "suzanne", "concave_plate", "angle_boundary", "degenerate_input", "protected_path",
)
_DEGENERATE = "degenerate_input"


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------
def _json_safe(value):
    try:
        from chart_uv_agent.reporting import json_safe
    except Exception:  # noqa: BLE001 - evidence must never depend on the engine importing
        return value
    return json_safe(value)


def _git_head() -> str | None:
    try:
        from chart_uv_agent.reporting import git_head_sha
    except Exception:  # noqa: BLE001
        return None
    return git_head_sha(_ROOT)


def _write_evidence(name: str, payload: dict) -> None:
    """Write ``uv_auto_gates_<name>.json`` into ``$UV_GATE_EVIDENCE_DIR`` when set."""
    out_dir = os.environ.get("UV_GATE_EVIDENCE_DIR")
    if not out_dir:
        return
    try:
        os.makedirs(out_dir, exist_ok=True)
        doc = {
            "test": name,
            "blender": blender_version_info() if BLENDER else None,
            "git_head": _git_head(),
            **payload,
        }
        path = os.path.join(out_dir, f"uv_auto_gates_{name}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_json_safe(doc), fh, indent=2, ensure_ascii=False, sort_keys=False)
            fh.write("\n")
    except Exception as exc:  # noqa: BLE001 - evidence is never allowed to fail a gate
        print(f"evidence write failed for {name}: {exc}")


# ---------------------------------------------------------------------------
# fixtures + worker plumbing (same pattern as test_uv_auto_generate.py)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def fixtures(tmp_path_factory) -> dict:
    """Build the baseline fixture set ONCE for this module (G0)."""
    out_dir = str(tmp_path_factory.mktemp("uv_gate_fixtures"))
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


def _job(model: str, out_dir: str, project: str, mode: str, *, run_id: str = "uv_gate",
         **extra) -> dict:
    job = {
        "command": "generate_uv_from_seams",
        "project_id": "e2e_gates",
        "run_id": run_id,
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


def _run(job: dict, tmp_path, tag: str = "job", timeout: int = 600):
    job_path = str(tmp_path / f"{tag}.json")
    with open(job_path, "w", encoding="utf-8") as fh:
        json.dump(job, fh)
    return run_blender_python(_WORKER, ["--job", job_path], timeout=timeout)


def _read(out_dir: str, name: str) -> dict:
    with open(os.path.join(out_dir, name), encoding="utf-8") as fh:
        return json.load(fh)


def _read_optional(out_dir: str, name: str):
    path = os.path.join(out_dir, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _pick(block, *keys):
    if not isinstance(block, dict):
        return None
    return {k: block.get(k) for k in keys}


# ---------------------------------------------------------------------------
# 1. every fixture through auto_generate, with no spec at all (G1/G2/G3/G5/G6)
# ---------------------------------------------------------------------------
@requires_blender
def test_auto_generate_all_fixtures(fixtures, tmp_path):
    rows: list[dict] = []
    try:
        for name in _FIXTURE_NAMES:
            project = str(tmp_path / name)
            out_dir = os.path.join(project, "run")
            os.makedirs(out_dir, exist_ok=True)
            job = _job(_model(fixtures, name), out_dir, project, "auto_generate",
                       run_id=f"uv_gate_{name}")
            row: dict = {"fixture": name}
            try:
                proc = _run(job, tmp_path, tag=f"job_{name}", timeout=300)
                row["exit_code"] = proc.returncode
                row["stderr_tail"] = (proc.stderr or "")[-1500:]
            except subprocess.TimeoutExpired:
                row["exit_code"] = None
                row["timeout"] = True
                rows.append(row)
                continue

            status = _read_optional(out_dir, "status.json") or {}
            summary = _read_optional(out_dir, "uv_generate_summary.json") or {}
            p5 = _read_optional(out_dir, "p5_gate.json") or {}

            gate = summary.get("auto_gate") or {}
            mand = summary.get("mandatory_audit") or {}
            corr = summary.get("correctness") or {}
            audit = summary.get("final_reread_audit")
            row.update({
                "status": status.get("status"),
                "status_error": status.get("error"),
                "warnings": summary.get("warnings"),
                "auto_gate": {
                    "valid": gate.get("valid"), "passed": gate.get("passed"),
                    "failures": gate.get("failures"),
                    "invalid_reasons": gate.get("invalid_reasons"),
                },
                "acceptance_reason": summary.get("acceptance_reason"),
                "mandatory_90_missing": mand.get("mandatory_90_missing"),
                "mandatory_90_uv_unsplit": mand.get("mandatory_90_uv_unsplit"),
                "correctness": {
                    "passed": corr.get("passed"),
                    "failed_checks": [c.get("name") for c in (corr.get("checks") or [])
                                      if not c.get("passed")],
                },
                "final_reread_audit": {
                    "present": audit is not None,
                    "passed": (audit or {}).get("passed"),
                    "error": (audit or {}).get("error"),
                },
                "mesh_identity_unchanged": (summary.get("mesh_identity") or {}).get("unchanged"),
                "distortion_v2_global": _pick(
                    (summary.get("distortion_v2") or {}).get("global"),
                    "anisotropy_p95", "anisotropy_max", "area_stretch_mean",
                    "exceed_area_fraction"),
                "island_count": p5.get("final_island_count"),
                "seam_length": _pick(summary.get("seam_length"),
                                     "total", "auxiliary", "auxiliary_normalized"),
                "termination": _pick(summary.get("termination"), "reason", "iterations",
                                     "candidates_evaluated", "elapsed_s"),
                "performance": summary.get("performance"),
            })
            rows.append(row)
    finally:
        _write_evidence("test_auto_generate_all_fixtures", {"rows": rows})

    by_name = {r["fixture"]: r for r in rows}
    assert set(by_name) == set(_FIXTURE_NAMES), sorted(by_name)

    for name, row in by_name.items():
        assert row.get("timeout") is not True, f"{name}: worker timed out"
        if name == _DEGENERATE:
            assert row["exit_code"] in (0, 2), row
        else:
            assert row["exit_code"] == 0, row
        assert row["status"] in ("accepted", "needs_user_review", "failed"), row

    # The degenerate/non-manifold input must be diagnosed, never quietly accepted.
    deg = by_name[_DEGENERATE]
    assert deg["status"] != "accepted", deg
    diagnosed = bool(deg.get("status_error")) or bool(deg.get("warnings")) or bool(
        (deg.get("auto_gate") or {}).get("invalid_reasons"))
    assert diagnosed, deg

    for name in _FIXTURE_NAMES:
        if name == _DEGENERATE:
            continue
        row = by_name[name]
        assert row["mandatory_90_missing"] == 0, row
        assert row["mandatory_90_uv_unsplit"] == 0, row
        assert row["mesh_identity_unchanged"] is True, row
        assert row["final_reread_audit"]["present"] is True, row
        if row["status"] == "accepted":
            assert row["correctness"]["passed"] is True, row
            assert row["final_reread_audit"]["passed"] is True, row
        else:
            gate = row["auto_gate"] or {}
            assert gate.get("failures") or gate.get("invalid_reasons"), row


# ---------------------------------------------------------------------------
# 2. 89.9 / 90.0 / 90.1 boundary (G1)
# ---------------------------------------------------------------------------
@requires_blender
def test_angle_boundary_rule(fixtures, tmp_path):
    project = str(tmp_path)
    out_dir = str(tmp_path / "run")
    os.makedirs(out_dir, exist_ok=True)
    evidence: dict = {}
    try:
        proc = _run(_job(_model(fixtures, "angle_boundary"), out_dir, project,
                         "auto_generate", run_id="uv_gate_angle"), tmp_path, tag="job_angle")
        evidence["exit_code"] = proc.returncode
        overlay = _read_optional(out_dir, "seam_overlay.json") or {}

        def _key(a, b) -> tuple:
            pa = tuple(round(float(c), 6) for c in a)
            pb = tuple(round(float(c), 6) for c in b)
            return tuple(sorted((pa, pb)))

        types_by_coord = {_key(e["a"], e["b"]): e.get("type") for e in overlay.get("edges", [])}
        folds = {f["angle_deg"]: f["fold_edge_verts"]
                 for f in fixtures["by_name"]["angle_boundary"]["notes"]["folds"]}
        evidence["folds"] = {
            str(deg): {"verts": verts,
                       "in_seam_set": _key(*verts) in types_by_coord,
                       "type": types_by_coord.get(_key(*verts))}
            for deg, verts in folds.items()
        }
        evidence["seam_type_counts"] = (_read_optional(out_dir, "p5_gate.json")
                                        or {}).get("seam_type_counts")
    finally:
        _write_evidence("test_angle_boundary_rule", evidence)

    assert evidence["exit_code"] == 0, evidence
    for deg in ("90.0", "90.1"):
        info = evidence["folds"][deg]
        assert info["in_seam_set"], f"{deg}deg fold edge must ship as a seam: {info}"
        assert info["type"] == "mandatory_90", info
    soft = evidence["folds"]["89.9"]
    assert not soft["in_seam_set"] or soft["type"] != "mandatory_90", soft


# ---------------------------------------------------------------------------
# 3. determinism over three identical runs (G5)
# ---------------------------------------------------------------------------
@requires_blender
@pytest.mark.parametrize("fixture_name", ["suzanne", "bevel_cube"])
def test_determinism_three_runs(fixtures, tmp_path, fixture_name):
    project = str(tmp_path / fixture_name)
    runs: list[dict] = []
    evidence: dict = {"fixture": fixture_name, "runs": runs}
    try:
        for i in range(3):
            out_dir = os.path.join(project, f"run{i}")
            os.makedirs(out_dir, exist_ok=True)
            job = _job(_model(fixtures, fixture_name), out_dir, project, "auto_generate",
                       run_id=f"uv_gate_det_{fixture_name}_{i}")
            proc = _run(job, tmp_path, tag=f"job_det_{fixture_name}_{i}", timeout=600)
            p5 = _read_optional(out_dir, "p5_gate.json") or {}
            summary = _read_optional(out_dir, "uv_generate_summary.json") or {}
            runs.append({
                "run_id": job["run_id"],
                "exit_code": proc.returncode,
                "status": (_read_optional(out_dir, "status.json") or {}).get("status"),
                "seams": p5.get("seams"),
                "final_island_count": p5.get("final_island_count"),
                "distortion_global": (summary.get("distortion_v2") or {}).get("global"),
            })
    finally:
        _write_evidence(f"test_determinism_three_runs_{fixture_name}", evidence)

    base = runs[0]
    for other in runs[1:]:
        assert other["exit_code"] == base["exit_code"] == 0, evidence
        assert other["seams"] == base["seams"], evidence
        assert other["final_island_count"] == base["final_island_count"], evidence
        a, b = base["distortion_global"] or {}, other["distortion_global"] or {}
        assert set(a) == set(b), evidence
        for key, va in a.items():
            vb = b.get(key)
            if isinstance(va, (int, float)) and not isinstance(va, bool):
                assert isinstance(vb, (int, float)), (key, va, vb)
                assert abs(float(va) - float(vb)) <= 1e-6, (key, va, vb)
            else:
                assert va == vb, (key, va, vb)


# ---------------------------------------------------------------------------
# 4. auto seam set -> user spec -> preserve_existing round trip (G2)
# ---------------------------------------------------------------------------
@requires_blender
@pytest.mark.parametrize("fixture_name", ["cube", "suzanne"])
def test_preserve_round_trip_keeps_seam_set(fixtures, tmp_path, fixture_name):
    project = str(tmp_path / fixture_name)
    auto_dir = os.path.join(project, "auto")
    keep_dir = os.path.join(project, "preserve")
    os.makedirs(auto_dir, exist_ok=True)
    os.makedirs(keep_dir, exist_ok=True)
    model = _model(fixtures, fixture_name)
    evidence: dict = {"fixture": fixture_name}
    try:
        proc = _run(_job(model, auto_dir, project, "auto_generate",
                         run_id=f"uv_gate_rt_auto_{fixture_name}"),
                    tmp_path, tag=f"job_rt_auto_{fixture_name}", timeout=600)
        evidence["auto_exit_code"] = proc.returncode
        auto_seams = sorted(int(e) for e in
                            ((_read_optional(auto_dir, "p5_gate.json") or {}).get("seams") or []))
        evidence["auto_seam_count"] = len(auto_seams)

        spec_path = str(tmp_path / f"user_seam_spec_{fixture_name}.json")
        with open(spec_path, "w", encoding="utf-8") as fh:
            json.dump({
                "version": 1,
                "object": _OBJECT,
                "mode": "user_seams",
                "mandatory_fold_angle": 90,
                "user_seam_edges": auto_seams,
                "user_protected_edges": [],
                "chapters": [],
            }, fh)

        proc2 = _run(_job(model, keep_dir, project, "preserve_existing",
                          run_id=f"uv_gate_rt_keep_{fixture_name}", seam_spec=spec_path),
                     tmp_path, tag=f"job_rt_keep_{fixture_name}", timeout=600)
        evidence["preserve_exit_code"] = proc2.returncode
        summary = _read_optional(keep_dir, "uv_generate_summary.json") or {}
        p5 = _read_optional(keep_dir, "p5_gate.json") or {}
        kept = sorted(int(e) for e in (p5.get("seams") or []))
        evidence.update({
            "preserve_status": (_read_optional(keep_dir, "status.json") or {}).get("status"),
            "mode": summary.get("mode"),
            "seam_integrity": summary.get("seam_integrity"),
            "mandatory_audit": summary.get("mandatory_audit"),
            "preserve_seam_count": len(kept),
            "symmetric_difference": sorted(set(auto_seams) ^ set(kept)),
            "warnings": summary.get("warnings"),
        })
    finally:
        _write_evidence(f"test_preserve_round_trip_keeps_seam_set_{fixture_name}", evidence)

    assert evidence["auto_exit_code"] == 0, evidence
    assert evidence["preserve_exit_code"] == 0, evidence
    assert evidence["mode"] == "preserve_existing", evidence
    assert (evidence["seam_integrity"] or {}).get("auto_added_seams") == 0, evidence
    assert evidence["symmetric_difference"] == [], evidence
    assert (evidence["mandatory_audit"] or {}).get("reported_only") is True, evidence
    assert evidence["preserve_status"] in ("accepted", "needs_user_review"), evidence


# ---------------------------------------------------------------------------
# 5. Hangul + space in the whole path (G9)
# ---------------------------------------------------------------------------
@requires_blender
def test_korean_space_path(fixtures, tmp_path):
    project = os.path.join(str(tmp_path), "한글 경로 테스트", "with space")
    os.makedirs(project, exist_ok=True)
    model = os.path.join(project, "cube.blend")
    shutil.copyfile(_model(fixtures, "cube"), model)
    out_dir = os.path.join(project, "run")
    os.makedirs(out_dir, exist_ok=True)

    evidence: dict = {"project": project, "model": model, "out_dir": out_dir}
    try:
        proc = _run(_job(model, out_dir, project, "auto_generate", run_id="uv_gate_korean"),
                    tmp_path, tag="job_korean", timeout=600)
        evidence["exit_code"] = proc.returncode
        evidence["stderr_tail"] = (proc.stderr or "")[-1500:]
        status = _read_optional(out_dir, "status.json") or {}
        summary = _read_optional(out_dir, "uv_generate_summary.json") or {}
        evidence.update({
            "status": status.get("status"),
            "status_error": status.get("error"),
            "selected_uv_blend": os.path.join(out_dir, "selected_uv.blend"),
            "selected_uv_blend_exists": os.path.exists(
                os.path.join(out_dir, "selected_uv.blend")),
            "auto_gate": summary.get("auto_gate"),
            "warnings": summary.get("warnings"),
        })
    finally:
        _write_evidence("test_korean_space_path", evidence)

    assert evidence["exit_code"] == 0, evidence
    assert evidence["status"] in ("accepted", "needs_user_review"), evidence
    assert evidence["selected_uv_blend_exists"] is True, evidence


# ---------------------------------------------------------------------------
# 6. contradictory flags are refused before the engine runs (G2)
# ---------------------------------------------------------------------------
@requires_blender
def test_contradictory_flags_rejected(fixtures, tmp_path):
    project = str(tmp_path)
    out_dir = str(tmp_path / "run")
    os.makedirs(out_dir, exist_ok=True)
    job = _job(_model(fixtures, "cube"), out_dir, project, "preserve_existing",
               run_id="uv_gate_flags")
    job["options"] = {"auto_refine_user_seams": True}

    evidence: dict = {}
    try:
        proc = _run(job, tmp_path, tag="job_flags", timeout=300)
        evidence["exit_code"] = proc.returncode
        status = _read_optional(out_dir, "status.json") or {}
        evidence["status"] = status.get("status")
        evidence["error"] = status.get("error")
    finally:
        _write_evidence("test_contradictory_flags_rejected", evidence)

    assert evidence["status"] == "failed", evidence
    assert (evidence["error"] or {}).get("code") == "contradictory_flags", evidence
