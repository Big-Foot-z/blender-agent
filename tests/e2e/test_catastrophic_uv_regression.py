"""Named-regression evidence for the catastrophic-distortion path on a REAL model (TC7).

The baseline is one concrete failure that this milestone exists to fix: the decimated
statue ``tests/e2e/fixtures/real/statue_lowpoly.fbx`` (5,996 verts / 11,776 faces), whose
automatic UV produced needle-shaped catastrophic distortion (anisotropy max ~187, 80
islands at the island cap). The fixture is untracked, so every test in this module skips
with ``real-model fixture not present`` when it is missing.

Gate IDs:

- **CG0 (reproducible failure baseline)** — the fixture is pinned by SHA-256 and the
  run manifest must report the same digest, so the recorded numbers name their input.
- **CG14 (determinism)** — three identical runs must agree on the seam set, the sorted
  bad face ids, the bad REGION face sets, the island count, the repair row sequence and
  the float metrics (1e-6).
- **CG15 (named regression fixture)** — one preview run copies the before/after evidence
  (heat map, checkers, UV layout, catastrophic report, repair history, quality report)
  into ``$UV_GATE_EVIDENCE_DIR/statue_regression/`` together with the repair's own
  before/after measurement. No acceptance is asserted: the milestone verdict is read off
  this evidence by hand.
- **CG12 (export round trip)** — the same real model through
  ``worker/export_production_asset.py`` as FBX + GLB; an accepted UV run must export
  cleanly, a ``needs_user_review`` one must be refused with ``reread_audit_failed``.
- **CG13 (status)** — every run's status/gate/catastrophic verdict is recorded.

Evidence: when ``UV_GATE_EVIDENCE_DIR`` is set, each test writes
``uv_catastrophic_regression_<test>.json`` there (written in a ``finally`` so a failing
gate still leaves the table). Assertions read JSON artifacts, never stdout.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time

import pytest

from tests.e2e.blender_env import (
    BLENDER,
    blender_version_info,
    requires_blender,
    run_blender_python,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_UV_WORKER = os.path.join(_ROOT, "worker", "generate_uv_from_seams.py")
_EXPORT_WORKER = os.path.join(_ROOT, "worker", "export_production_asset.py")

#: The named regression fixture (untracked; present only on a machine that has it).
FIXTURE = os.path.join(_HERE, "fixtures", "real", "statue_lowpoly.fbx")
FIXTURE_SHA256 = "e221cc2c1d51f92f3c99b852bab8b8fd484aadcac5b0560bce2c47a53635bf3f"

#: The statue ships as ``original_DECIMATED_12000``; ``None`` lets the worker pick the
#: first mesh, which is what the app itself does for a single-object import.
_OBJECT_NAME = None

_EVIDENCE_PREFIX = "uv_catastrophic_regression_"
_EVIDENCE_SUBDIR = "statue_regression"

#: Artifacts copied out for the CG15 before/after review packet.
_EVIDENCE_ARTIFACTS = (
    "selected_heatmap_anisotropy.png",
    "selected_checker_front.png",
    "selected_checker_side.png",
    "selected_uv_layout.png",
    "heatmap_meta.json",
    "uv_catastrophic.json",
    "uv_repair_history.json",
    "quality_report.json",
    "uv_generate_summary.json",
)

requires_fixture = pytest.mark.skipif(
    not os.path.exists(FIXTURE), reason="real-model fixture not present"
)

#: Cache for the preview run so the export round trip (CG12) can reuse it instead of
#: paying for a second 1500 s Blender run.
_PREVIEW_RUN: dict = {}


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


def _evidence_dir() -> str | None:
    return os.environ.get("UV_GATE_EVIDENCE_DIR") or None


def _write_evidence(name: str, payload: dict) -> None:
    """Write ``uv_catastrophic_regression_<name>.json`` into ``$UV_GATE_EVIDENCE_DIR``."""
    out_dir = _evidence_dir()
    if not out_dir:
        return
    try:
        os.makedirs(out_dir, exist_ok=True)
        doc = {
            "test": name,
            "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "blender": blender_version_info() if BLENDER else None,
            "git_head": _git_head(),
            "fixture": FIXTURE,
            "fixture_sha256_expected": FIXTURE_SHA256,
            **payload,
        }
        path = os.path.join(out_dir, f"{_EVIDENCE_PREFIX}{name}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_json_safe(doc), fh, indent=2, ensure_ascii=False, sort_keys=False)
            fh.write("\n")
    except Exception as exc:  # noqa: BLE001 - evidence is never allowed to fail a gate
        print(f"evidence write failed for {name}: {exc}")


# ---------------------------------------------------------------------------
# worker plumbing (local copies of the _job/_run pattern used by the other modules)
# ---------------------------------------------------------------------------
def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _job(model: str, out_dir: str, project: str, run_id: str, *,
         render_previews: bool = False) -> dict:
    return {
        "command": "generate_uv_from_seams",
        "project_id": "e2e_catastrophic_regression",
        "run_id": run_id,
        "model": model,
        "model_rel": os.path.basename(model),
        "object_name": _OBJECT_NAME,
        "seam_spec": None,
        "mode": "auto_generate",
        "out_dir": out_dir,
        "selected_blend_out": os.path.join(project, "work", "uv", "selected_uv.blend"),
        "selected_blend_out_rel": os.path.join("work", "uv", "selected_uv.blend"),
        "selected_summary_out": os.path.join(project, "work", "uv", "selected_uv_summary.json"),
        "options": {"render_previews": bool(render_previews)},
    }


def _run(job: dict, job_dir, tag: str, timeout: int) -> tuple[subprocess.CompletedProcess, float]:
    job_path = os.path.join(str(job_dir), f"{tag}.json")
    os.makedirs(os.path.dirname(job_path), exist_ok=True)
    with open(job_path, "w", encoding="utf-8") as fh:
        json.dump(job, fh)
    started = time.perf_counter()
    proc = run_blender_python(_UV_WORKER, ["--job", job_path], timeout=timeout)
    return proc, time.perf_counter() - started


def _export_job(model: str, summary: str, out_dir: str, formats: list[str],
                object_name) -> dict:
    return {
        "command": "export_production_asset",
        "project_id": "e2e_catastrophic_regression",
        "export_id": "export_cg12",
        "selected_uv_model": model,
        "selected_uv_model_rel": os.path.join("work", "uv", "selected_uv.blend"),
        "selected_uv_summary": summary,
        "selected_uv_summary_rel": os.path.join("work", "uv", "selected_uv_summary.json"),
        "object_name": object_name,
        "formats": list(formats),
        "out_dir": out_dir,
        "out_dir_rel": os.path.join("exports", "export_cg12"),
        "uv_generate_run_id": "uv_cat_preview",
        "options": {"render_previews": False},
        "out": os.path.join(out_dir, "export_result.json"),
    }


def _run_export(job: dict, job_dir, tag: str, timeout: int = 1800):
    job_path = os.path.join(str(job_dir), f"{tag}.json")
    os.makedirs(os.path.dirname(job_path), exist_ok=True)
    with open(job_path, "w", encoding="utf-8") as fh:
        json.dump(job, fh)
    return run_blender_python(_EXPORT_WORKER, ["--job", job_path], timeout=timeout)


def _read(out_dir: str, name: str) -> dict | None:
    path = os.path.join(out_dir, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _tail(proc) -> str:
    return (proc.stdout or "")[-2000:] + "\n" + (proc.stderr or "")[-4000:]


def _pick(block, *keys):
    if not isinstance(block, dict):
        return None
    return {k: block.get(k) for k in keys}


def _sorted_ints(values) -> list[int]:
    return sorted(int(v) for v in (values or ()))


def _region_face_sets(catastrophic) -> list[list[int]]:
    """The bad REGIONS as sorted face-id lists, themselves sorted (a set comparison)."""
    regions = (catastrophic or {}).get("regions") or []
    return sorted(_sorted_ints(r.get("face_ids")) for r in regions)


def _catastrophic_compact(catastrophic) -> dict:
    return _pick(catastrophic or {}, "bad_triangle_count", "bad_region_count",
                 "bad_area_fraction", "max_anisotropy", "near_collapse_count", "passed")


def _repair_rows(repair) -> list[dict]:
    """The accepted/kind/reason sequence of the repair rounds (order matters)."""
    rows = (repair or {}).get("rows") or []
    return [{"round": r.get("round"), "action": r.get("action"), "reason": r.get("reason"),
             "candidate_kind": r.get("candidate_kind")} for r in rows]


def _floats_close(a, b, tol: float = 1e-6) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= tol
    return a == b


def _collect_run(out_dir: str, project: str, proc, wall_s: float, run_id: str) -> dict:
    """Everything TC7 records about one statue run (CG13/CG14 evidence)."""
    summary = _read(out_dir, "uv_generate_summary.json") or {}
    status = _read(out_dir, "status.json") or {}
    catastrophic = _read(out_dir, "uv_catastrophic.json")
    repair = _read(out_dir, "uv_repair_history.json")
    merge_back = _read(out_dir, "merge_back_history.json")
    p5 = _read(out_dir, "p5_gate.json") or {}
    manifest = _read(out_dir, "run_manifest.json") or {}

    gate = summary.get("auto_gate") or {}
    distortion = summary.get("distortion_v2") or {}
    return {
        "run_id": run_id,
        "out_dir": out_dir,
        "project": project,
        "exit_code": proc.returncode,
        "wall_s": round(wall_s, 3),
        "stderr_tail": (proc.stderr or "")[-1500:],
        "status": status.get("status"),
        "status_error": status.get("error"),
        "auto_gate": {"valid": gate.get("valid"), "passed": gate.get("passed"),
                      "failures": gate.get("failures"),
                      "invalid_reasons": gate.get("invalid_reasons")},
        "acceptance_reason": summary.get("acceptance_reason"),
        "catastrophic_compact": _catastrophic_compact(catastrophic
                                                      or summary.get("catastrophic")),
        "bad_face_ids": _sorted_ints((catastrophic or {}).get("bad_face_ids")),
        "region_face_sets": _region_face_sets(catastrophic),
        "repair": repair,
        "repair_rows": _repair_rows(repair),
        "merge_back_history": merge_back,
        "island_count": p5.get("final_island_count"),
        "seams": _sorted_ints(p5.get("seams")),
        "termination": _pick(summary.get("termination"), "reason", "iterations",
                             "candidates_evaluated", "elapsed_s"),
        "uv_hash": summary.get("uv_hash"),
        "heatmap_identity": summary.get("heatmap_identity"),
        "performance": summary.get("performance"),
        "distortion_summary": _pick(distortion.get("summary"), "global_anisotropy_p95",
                                    "global_area_stretch_mean"),
        "mesh_identity": summary.get("mesh_identity"),
        "mesh_identity_unchanged": (summary.get("mesh_identity") or {}).get("unchanged"),
        "mesh_fingerprint": (manifest.get("mesh_identity") or {}).get("fingerprint"),
        "model_sha256": manifest.get("model_sha256"),
        "object_name": summary.get("object_name"),
    }


# ---------------------------------------------------------------------------
# 1. CG0 — the failure baseline is pinned by content, not by file name
# ---------------------------------------------------------------------------
@requires_fixture
def test_fixture_identity():
    evidence: dict = {}
    try:
        evidence["path"] = FIXTURE
        evidence["exists"] = os.path.exists(FIXTURE)
        evidence["size_bytes"] = os.path.getsize(FIXTURE)
        evidence["sha256"] = _sha256_file(FIXTURE)
    finally:
        _write_evidence("fixture_identity", evidence)

    assert evidence["sha256"] == FIXTURE_SHA256, evidence


# ---------------------------------------------------------------------------
# 2. CG14/CG0/CG15 — three identical runs on the statue must agree exactly
# ---------------------------------------------------------------------------
@requires_blender
@requires_fixture
def test_statue_three_runs_are_deterministic(tmp_path):
    rows: list[dict] = []
    comparison: dict = {}
    evidence: dict = {"runs": rows, "comparison": comparison}
    try:
        for i in range(3):
            run_id = f"uv_cat_{i}"
            project = str(tmp_path / run_id)
            out_dir = os.path.join(project, "run")
            os.makedirs(out_dir, exist_ok=True)
            job = _job(FIXTURE, out_dir, project, run_id, render_previews=False)
            proc, wall_s = _run(job, tmp_path, f"job_{run_id}", timeout=1200)
            rows.append(_collect_run(out_dir, project, proc, wall_s, run_id))

        base = rows[0]
        for other in rows[1:]:
            comparison[f"{base['run_id']}_vs_{other['run_id']}"] = {
                "seams_equal": other["seams"] == base["seams"],
                "bad_face_ids_equal": other["bad_face_ids"] == base["bad_face_ids"],
                "region_face_sets_equal":
                    other["region_face_sets"] == base["region_face_sets"],
                "island_count_equal": other["island_count"] == base["island_count"],
                "repair_rows_equal": other["repair_rows"] == base["repair_rows"],
                "status_equal": other["status"] == base["status"],
                "uv_hash_equal": other["uv_hash"] == base["uv_hash"],
            }
    finally:
        _write_evidence("three_runs_deterministic", evidence)

    for row in rows:
        assert row["exit_code"] == 0, _tail_row(row)
        assert row["status"] in ("accepted", "needs_user_review"), row
        assert row["model_sha256"] == FIXTURE_SHA256, row
        assert row["mesh_identity_unchanged"] is True, row
        assert (row["heatmap_identity"] or {}).get("passed") is True, row
        if row["status"] == "accepted":
            assert (row["catastrophic_compact"] or {}).get("passed") is True, row
            assert (row["auto_gate"] or {}).get("passed") is True, row
        else:
            assert (row["auto_gate"] or {}).get("failures"), row

    base = rows[0]
    for other in rows[1:]:
        assert other["seams"] == base["seams"], evidence
        assert other["bad_face_ids"] == base["bad_face_ids"], evidence
        assert other["region_face_sets"] == base["region_face_sets"], evidence
        assert other["island_count"] == base["island_count"], evidence
        assert other["repair_rows"] == base["repair_rows"], evidence
        for key in ("bad_area_fraction", "max_anisotropy"):
            va = (base["catastrophic_compact"] or {}).get(key)
            vb = (other["catastrophic_compact"] or {}).get(key)
            assert _floats_close(va, vb), (key, va, vb)
        for key in ("global_anisotropy_p95", "global_area_stretch_mean"):
            va = (base["distortion_summary"] or {}).get(key)
            vb = (other["distortion_summary"] or {}).get(key)
            assert _floats_close(va, vb), (key, va, vb)


def _tail_row(row: dict) -> str:
    return json.dumps({"run_id": row.get("run_id"), "exit_code": row.get("exit_code"),
                       "status": row.get("status"), "status_error": row.get("status_error"),
                       "stderr_tail": row.get("stderr_tail")}, indent=2, default=str)


# ---------------------------------------------------------------------------
# 3. CG15 — the before/after review packet for the named regression
# ---------------------------------------------------------------------------
def _preview_run(tmp_path_factory) -> dict:
    """Run the statue ONCE with previews on; reused by the export round trip (CG12)."""
    if _PREVIEW_RUN:
        return _PREVIEW_RUN
    base = str(tmp_path_factory.mktemp("statue_preview"))
    run_id = "uv_cat_preview"
    project = os.path.join(base, "project")
    out_dir = os.path.join(project, "run")
    os.makedirs(out_dir, exist_ok=True)
    job = _job(FIXTURE, out_dir, project, run_id, render_previews=True)
    proc, wall_s = _run(job, base, "job_preview", timeout=1500)
    row = _collect_run(out_dir, project, proc, wall_s, run_id)
    row["job_dir"] = base
    _PREVIEW_RUN.update(row)
    return _PREVIEW_RUN


@requires_blender
@requires_fixture
def test_statue_before_after_evidence(tmp_path_factory):
    evidence: dict = {}
    try:
        run = _preview_run(tmp_path_factory)
        out_dir = run["out_dir"]
        evidence["run"] = run

        repair = run.get("repair") or {}
        evidence["repair_before_after"] = {
            "bad_triangles_before": repair.get("bad_triangles_before"),
            "bad_triangles_after": repair.get("bad_triangles_after"),
            "bad_area_before": repair.get("bad_area_before"),
            "bad_area_after": repair.get("bad_area_after"),
            "island_count_before": repair.get("island_count_before"),
            "island_count_after": repair.get("island_count_after"),
            "rounds": repair.get("rounds"),
            "reunwrap_accepted": repair.get("reunwrap_accepted"),
            "relief_accepted": repair.get("relief_accepted"),
            "rejected": repair.get("rejected"),
            "reason": repair.get("reason"),
            "rows": repair.get("rows"),
        }

        heatmap_meta = _read(out_dir, "heatmap_meta.json") or {}
        summary = _read(out_dir, "uv_generate_summary.json") or {}
        evidence["heatmap_meta"] = heatmap_meta
        evidence["summary_uv_hash"] = summary.get("uv_hash")

        present = {name: os.path.exists(os.path.join(out_dir, name))
                   for name in _EVIDENCE_ARTIFACTS}
        evidence["artifacts_present"] = present

        copied: dict[str, str] = {}
        ev_dir = _evidence_dir()
        if ev_dir:
            packet = os.path.join(ev_dir, _EVIDENCE_SUBDIR)
            os.makedirs(packet, exist_ok=True)
            for name in _EVIDENCE_ARTIFACTS:
                src = os.path.join(out_dir, name)
                if not os.path.exists(src):
                    continue
                dst = os.path.join(packet, name)
                try:
                    shutil.copyfile(src, dst)
                    copied[name] = dst
                except Exception as exc:  # noqa: BLE001 - a copy must not fail the gate
                    copied[name] = f"copy failed: {exc}"
        evidence["copied"] = copied
    finally:
        _write_evidence("before_after_evidence", evidence)

    assert evidence["run"]["exit_code"] == 0, _tail_row(evidence["run"])
    missing = [name for name, ok in evidence["artifacts_present"].items() if not ok]
    assert not missing, json.dumps({"missing": missing, "out_dir": evidence["run"]["out_dir"]},
                                   indent=2)
    # CG4: the picture and the reported layout are the same UV.
    assert evidence["heatmap_meta"].get("uv_hash") == evidence["summary_uv_hash"], evidence


# ---------------------------------------------------------------------------
# 4. CG12 — export round trip on the real model
# ---------------------------------------------------------------------------
@requires_blender
@requires_fixture
def test_statue_export_round_trip(tmp_path_factory):
    formats = ["fbx", "glb"]
    evidence: dict = {"formats": formats}
    try:
        run = _preview_run(tmp_path_factory)
        evidence["uv_run"] = {k: run.get(k) for k in
                              ("run_id", "exit_code", "status", "auto_gate",
                               "catastrophic_compact", "uv_hash", "model_sha256")}
        assert run["exit_code"] == 0, _tail_row(run)
        uv_status = run["status"]

        shipped = os.path.join(run["project"], "work", "uv", "selected_uv.blend")
        shipped_summary = os.path.join(run["project"], "work", "uv",
                                       "selected_uv_summary.json")
        if os.path.exists(shipped):
            export_model, export_summary = shipped, shipped_summary
            evidence["export_input"] = "work/uv/selected_uv.blend"
        else:
            export_model = os.path.join(run["out_dir"], "selected_uv.blend")
            export_summary = os.path.join(run["out_dir"], "uv_generate_summary.json")
            evidence["export_input"] = "run/selected_uv.blend"
        evidence["export_model_exists"] = os.path.exists(export_model)

        export_dir = os.path.join(run["project"], "exports", "export_cg12")
        os.makedirs(export_dir, exist_ok=True)
        eproc = _run_export(_export_job(export_model, export_summary, export_dir, formats,
                                        run.get("object_name")),
                            run["job_dir"], "job_export")
        evidence["export_exit_code"] = eproc.returncode
        evidence["export_stderr_tail"] = (eproc.stderr or "")[-1500:]

        estatus = _read(export_dir, "status.json") or {}
        result = _read(export_dir, "export_result.json") or {}
        reread = _read(export_dir, "export_reread_report.json") or {}
        manifest = _read(export_dir, "export_manifest.json") or {}
        evidence["export_status"] = estatus.get("status")
        evidence["export_failed_formats"] = result.get("failed_formats") or []
        evidence["export_manifest_files"] = manifest.get("files")
        evidence["export_reread_report"] = reread
        evidence["per_format"] = {
            fmt: {
                "passed": ((reread.get("formats") or {}).get(fmt) or {}).get("passed"),
                "failures": ((reread.get("formats") or {}).get(fmt) or {}).get("failures"),
                "catastrophic": ((reread.get("formats") or {}).get(fmt)
                                 or {}).get("catastrophic"),
                "fragmentation": ((reread.get("formats") or {}).get(fmt)
                                  or {}).get("fragmentation"),
            }
            for fmt in formats
        }
    finally:
        _write_evidence("export", evidence)

    assert evidence["export_exit_code"] == 0, evidence.get("export_stderr_tail")
    if uv_status == "accepted":
        assert evidence["export_status"] == "accepted", evidence
        for fmt in formats:
            assert evidence["per_format"][fmt]["passed"] is True, evidence["per_format"]
            assert not evidence["per_format"][fmt]["failures"], evidence["per_format"]
    else:
        assert evidence["export_status"] == "failed", evidence
        failed = evidence["export_failed_formats"]
        assert failed, evidence
        codes = sorted({ff.get("code") for ff in failed})
        assert codes == ["reread_audit_failed"], json.dumps(failed, indent=2, default=str)
        for fmt in formats:
            assert evidence["per_format"][fmt]["passed"] is not True, evidence["per_format"]
