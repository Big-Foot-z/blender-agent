"""Blender-gated e2e evidence for G9 (automatic UV -> export -> re-read) and
G8 (performance: 3 runs on fixed hardware, median/max time + peak memory).

- ``test_auto_uv_then_export_reread`` runs the ``auto_generate`` UV worker on the
  ``bevel_cube`` fixture (a hard-surface model with NO UV that the gate ACCEPTS —
  G13's positive path needs an accepted asset), asserts the export worker's own
  ``export_reread_report.json`` passes for every format, ships the selected ``.blend`` through
  ``worker/export_production_asset.py`` as FBX + GLB, then RE-READS each exported
  file inside Blender (``tests/e2e/fixtures/reread_export.py``) and asserts the
  G1 rule still holds on the shipped artifact: exactly ONE UV layer exists (so the
  audit cannot read a leftover original map), ``mandatory_90_uv_unsplit == 0``, UVs
  are in bounds, and the topology matches the source ``.blend`` by face count or —
  for triangulating formats like GLB — by triangle count.
- ``test_export_refuses_a_non_accepted_uv_result`` is the negative half of G13:
  suzanne's automatic layout ends ``needs_user_review``, and exporting it must end
  ``failed`` with ``reread_audit_failed`` on every format (evidence
  ``uv_export_refused.json``).
- ``test_performance_three_runs`` runs the automatic path 3x on suzanne and 3x on
  torus and records ``summary.performance`` (``elapsed_s`` / ``peak_memory_mb``)
  plus the host-measured wall time, with median/max. No time threshold is asserted
  — G8's budget is fixed at calibration time, which has not happened yet; this test
  only proves the runs complete and report the numbers, and writes the table.

Evidence: when ``UV_GATE_EVIDENCE_DIR`` is set, ``uv_export_reread.json`` and
``uv_performance.json`` are written there. Assertions read JSON artifacts, never stdout.
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import subprocess
import sys
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
_BUILD_SCRIPT = os.path.join(_HERE, "fixtures", "build_fixture_models.py")
_REREAD_SCRIPT = os.path.join(_HERE, "fixtures", "reread_export.py")
_UV_WORKER = os.path.join(_ROOT, "worker", "generate_uv_from_seams.py")
_EXPORT_WORKER = os.path.join(_ROOT, "worker", "export_production_asset.py")
_OBJECT = "Fixture"

# Previews are pure reporting overhead here: G8 measures the solver, G9 the artifact.
_UV_OPTIONS = {"render_previews": False}
_EXPORT_OPTIONS = {"render_previews": False}

_EVIDENCE_DIR = os.environ.get("UV_GATE_EVIDENCE_DIR")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def fixtures(tmp_path_factory) -> dict:
    """Build the baseline fixture set ONCE for this module (G0)."""
    out_dir = str(tmp_path_factory.mktemp("uv_export_perf_fixtures"))
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


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _tail(proc) -> str:
    return (proc.stdout or "")[-2000:] + "\n" + (proc.stderr or "")[-4000:]


def _uv_job(model: str, out_dir: str, project: str, run_id: str) -> dict:
    return {
        "command": "generate_uv_from_seams",
        "project_id": "e2e_auto_export",
        "run_id": run_id,
        "model": model,
        "model_rel": os.path.basename(model),
        "object_name": _OBJECT,
        "seam_spec": None,
        "mode": "auto_generate",
        "out_dir": out_dir,
        "selected_blend_out": os.path.join(project, "work", "uv", "selected_uv.blend"),
        "selected_blend_out_rel": os.path.join("work", "uv", "selected_uv.blend"),
        "selected_summary_out": os.path.join(project, "work", "uv", "selected_uv_summary.json"),
        "options": dict(_UV_OPTIONS),
    }


def _run_uv(job: dict, tmp_path, tag: str) -> tuple[subprocess.CompletedProcess, float]:
    """Run the UV worker; return the process and the host-measured wall time."""
    job_path = str(tmp_path / f"{tag}.json")
    with open(job_path, "w", encoding="utf-8") as fh:
        json.dump(job, fh)
    started = time.perf_counter()
    proc = run_blender_python(_UV_WORKER, ["--job", job_path], timeout=1800)
    return proc, time.perf_counter() - started


def _export_job(model: str, summary: str, out_dir: str, formats: list[str]) -> dict:
    return {
        "command": "export_production_asset",
        "project_id": "e2e_auto_export",
        "export_id": "export_g9",
        "selected_uv_model": model,
        "selected_uv_model_rel": os.path.join("work", "uv", "selected_uv.blend"),
        "selected_uv_summary": summary,
        "selected_uv_summary_rel": os.path.join("work", "uv", "selected_uv_summary.json"),
        "object_name": _OBJECT,
        "formats": formats,
        "out_dir": out_dir,
        "out_dir_rel": os.path.join("exports", "export_g9"),
        "uv_generate_run_id": "uv_auto_export_e2e",
        "options": dict(_EXPORT_OPTIONS),
        "out": os.path.join(out_dir, "export_result.json"),
    }


def _run_export(job: dict, tmp_path, tag: str) -> subprocess.CompletedProcess:
    job_path = str(tmp_path / f"{tag}.json")
    with open(job_path, "w", encoding="utf-8") as fh:
        json.dump(job, fh)
    return run_blender_python(_EXPORT_WORKER, ["--job", job_path], timeout=1800)


def _hardware_info() -> dict:
    """Fixed-hardware identification for the G8 performance table."""
    info = {
        "platform": platform.platform(),
        "processor": platform.processor(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "python": sys.version.split()[0],
        "total_memory_mb": None,
    }
    try:  # Windows: GlobalMemoryStatusEx
        if sys.platform.startswith("win"):
            import ctypes
            from ctypes import wintypes

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                info["total_memory_mb"] = round(stat.ullTotalPhys / (1024 * 1024), 1)
    except Exception:  # noqa: BLE001 - a missing number must not fail the gate run
        pass
    return info


def _write_evidence(name: str, payload: dict) -> str | None:
    if not _EVIDENCE_DIR:
        return None
    from chart_uv_agent.reporting import git_head_sha, json_safe

    os.makedirs(_EVIDENCE_DIR, exist_ok=True)
    doc = json_safe({
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "code_sha": git_head_sha(_ROOT),
        "blender": blender_version_info(BLENDER),
        "hardware": _hardware_info(),
        **payload,
    })
    path = os.path.join(_EVIDENCE_DIR, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
    return path


# ---------------------------------------------------------------------------
# G9 — automatic UV generation -> production export -> re-read audit
# ---------------------------------------------------------------------------
@requires_blender
def test_auto_uv_then_export_reread(fixtures, tmp_path):
    project = str(tmp_path)
    run_dir = str(tmp_path / "run")
    os.makedirs(run_dir, exist_ok=True)
    fixture = fixtures["by_name"]["bevel_cube"]
    evidence: dict = {"fixture": "bevel_cube",
                      "fixture_face_count": fixture["face_count"],
                      "fixture_vertex_count": fixture["vertex_count"]}

    # 1. automatic UV on a model with NO UV and NO seam spec.
    proc, wall_s = _run_uv(_uv_job(_model(fixtures, "bevel_cube"), run_dir, project,
                                   "uv_auto_export_e2e"), tmp_path, "uv_job")
    evidence["uv_run"] = {"returncode": proc.returncode, "wall_s": round(wall_s, 3)}
    assert proc.returncode == 0, _tail(proc)

    status = _read(os.path.join(run_dir, "status.json"))
    summary = _read(os.path.join(run_dir, "uv_generate_summary.json"))
    evidence["uv_status"] = status["status"]
    evidence["uv_performance"] = summary.get("performance")
    evidence["uv_mandatory_audit"] = summary.get("mandatory_audit")
    evidence["uv_final_reread_audit"] = summary.get("final_reread_audit")

    # G13's positive path needs an ACCEPTED asset: only an accepted run ships to
    # work/uv (G6), and only an accepted layout can be expected to survive the
    # re-read audit (a needs_user_review layout carries the same defects into the
    # exported file — that is the refusal test below).
    if status["status"] != "accepted":
        _write_evidence("uv_export_reread.json", evidence)
        raise AssertionError(json.dumps(
            {"reason": "bevel_cube auto UV did not reach accepted",
             "uv_status": status["status"], "summary_status": summary.get("status"),
             "gate": summary.get("gate"), "failures": summary.get("failures"),
             "mandatory_audit": summary.get("mandatory_audit")}, indent=2, default=str))

    export_model = os.path.join(project, "work", "uv", "selected_uv.blend")
    export_summary = os.path.join(project, "work", "uv", "selected_uv_summary.json")
    evidence["export_input"] = "work/uv/selected_uv.blend"
    assert os.path.exists(export_model), f"no selected_uv.blend at {export_model}"

    # 2. production export (FBX + GLB). MVP 4 AI review is skipped by design.
    export_dir = str(tmp_path / "exports" / "export_g9")
    os.makedirs(export_dir, exist_ok=True)
    formats = ["fbx", "glb"]
    eproc = _run_export(_export_job(export_model, export_summary, export_dir, formats),
                        tmp_path, "export_job")
    evidence["export_returncode"] = eproc.returncode
    assert eproc.returncode == 0, _tail(eproc)

    estatus = _read(os.path.join(export_dir, "status.json"))
    evidence["export_status"] = estatus["status"]
    assert estatus["status"] == "accepted", estatus

    manifest = _read(os.path.join(export_dir, "export_manifest.json"))
    evidence["export_files"] = manifest["files"]
    assert set(manifest["formats"]) == set(formats), manifest["formats"]
    for fmt in formats:
        assert fmt in manifest["files"], fmt
        assert os.path.exists(os.path.join(export_dir, manifest["files"][fmt])), fmt

    # 2b. the worker's OWN re-read audit must pass for every shipped format (G13).
    reread_report = _read(os.path.join(export_dir, "export_reread_report.json"))
    evidence["export_reread_report"] = reread_report
    report_failures = {
        fmt: list((reread_report.get("formats", {}).get(fmt) or {}).get("failures") or [])
        for fmt in formats}
    evidence["export_reread_failures"] = report_failures
    if any(report_failures.values()) or not reread_report.get("passed"):
        _write_evidence("uv_export_reread.json", evidence)
    assert not any(report_failures.values()) and reread_report.get("passed") is True, \
        json.dumps({"reread_report_passed": reread_report.get("passed"),
                    "failed_formats": reread_report.get("failed_formats"),
                    "per_format_failures": report_failures}, indent=2, default=str)

    # 3. re-read every exported file and audit it (G9 / G1 on the shipped artifact).
    rereads: dict[str, dict] = {}
    for fmt in formats:
        model_path = os.path.join(export_dir, manifest["files"][fmt])
        out_json = str(tmp_path / f"reread_{fmt}.json")
        rproc = run_blender_python(
            _REREAD_SCRIPT,
            ["--model", model_path, "--out", out_json, "--source", export_model],
            timeout=1800)
        assert os.path.exists(out_json), _tail(rproc)
        data = _read(out_json)
        data["returncode"] = rproc.returncode
        rereads[fmt] = data
    evidence["reread"] = rereads
    _write_evidence("uv_export_reread.json", evidence)

    failures: list[str] = []
    for fmt, data in rereads.items():
        if data.get("returncode") != 0:
            failures.append(f"{fmt}: reread script exit {data.get('returncode')} "
                            f"{str(data.get('error'))[-400:]}")
            continue
        # (a) exactly ONE UV layer: importers re-activate the FIRST layer, so a
        # second (original) layer means the audit below measured the wrong map.
        layers = data.get("uv_layers") or []
        if len(layers) != 1:
            failures.append(f"{fmt}: expected exactly 1 UV layer, got {layers}")
        # (b)/(c) G1 still holds on the shipped artifact. The audit is restricted to
        # edges that exist in the SOURCE mesh (matched by edge_geometry_key): GLB
        # triangulates, and a diagonal invented by triangulation is not an edge of the
        # original mesh, so the 90-degree rule does not apply to it.
        matched = data.get("mandatory_audit_source_matched")
        unsplit = (matched.get("mandatory_90_uv_unsplit")
                   if isinstance(matched, dict) else data.get("mandatory_90_uv_unsplit"))
        if unsplit != 0:
            failures.append(f"{fmt}: mandatory_90_uv_unsplit={unsplit}")
        if data.get("uv_bounds_ok") is not True:
            failures.append(f"{fmt}: uv_bounds_ok={data.get('uv_bounds_ok')}")
        # (d) topology preserved: identical faces, OR identical triangles (GLB
        # always triangulates, so quads legitimately become 2 tris each).
        faces_ok = data.get("face_count") == data.get("source_face_count")
        tris_ok = data.get("triangle_count") == data.get("source_triangle_count")
        if not (faces_ok or tris_ok):
            failures.append(
                f"{fmt}: face_count={data.get('face_count')} vs source "
                f"{data.get('source_face_count')} and triangle_count="
                f"{data.get('triangle_count')} vs source "
                f"{data.get('source_triangle_count')}")
    assert not failures, json.dumps({"failures": failures, "reread": rereads}, indent=2)


# ---------------------------------------------------------------------------
# G13 — a NON-ACCEPTED UV result must be refused by the export re-read audit
# ---------------------------------------------------------------------------
@requires_blender
def test_export_refuses_a_non_accepted_uv_result(fixtures, tmp_path):
    """Suzanne's automatic layout ends ``needs_user_review``; exporting it must FAIL.

    The defects that blocked acceptance (overlap / orientation / island gap) travel
    into the exported file, so the export worker's re-read audit has to refuse every
    format with ``reread_audit_failed`` rather than ship an unapproved asset."""
    project = str(tmp_path)
    run_dir = str(tmp_path / "run")
    os.makedirs(run_dir, exist_ok=True)
    fixture = fixtures["by_name"]["suzanne"]
    evidence: dict = {"fixture": "suzanne",
                      "fixture_face_count": fixture["face_count"],
                      "fixture_vertex_count": fixture["vertex_count"]}

    proc, wall_s = _run_uv(_uv_job(_model(fixtures, "suzanne"), run_dir, project,
                                   "uv_refuse_export_e2e"), tmp_path, "uv_refuse_job")
    evidence["uv_run"] = {"returncode": proc.returncode, "wall_s": round(wall_s, 3)}
    assert proc.returncode == 0, _tail(proc)

    status = _read(os.path.join(run_dir, "status.json"))
    summary = _read(os.path.join(run_dir, "uv_generate_summary.json"))
    evidence["uv_status"] = status["status"]
    evidence["uv_mandatory_audit"] = summary.get("mandatory_audit")
    evidence["uv_final_reread_audit"] = summary.get("final_reread_audit")
    assert status["status"] in ("needs_user_review", "accepted"), status
    if status["status"] == "accepted":
        _write_evidence("uv_export_refused.json", evidence)
        pytest.skip("suzanne auto UV was ACCEPTED on this build; there is no "
                    "non-accepted result to refuse in this run")

    # needs_user_review never ships to work/uv (G6): the run directory's own
    # selected_uv.blend is the reviewed candidate, and it is what we try to export.
    export_model = os.path.join(run_dir, "selected_uv.blend")
    export_summary = os.path.join(run_dir, "uv_generate_summary.json")
    evidence["export_input"] = "run/selected_uv.blend"
    assert os.path.exists(export_model), f"no selected_uv.blend at {export_model}"

    export_dir = str(tmp_path / "exports" / "export_refused")
    os.makedirs(export_dir, exist_ok=True)
    formats = ["fbx", "glb"]
    eproc = _run_export(_export_job(export_model, export_summary, export_dir, formats),
                        tmp_path, "export_refuse_job")
    evidence["export_returncode"] = eproc.returncode
    assert eproc.returncode == 0, _tail(eproc)

    estatus = _read(os.path.join(export_dir, "status.json"))
    result = _read(os.path.join(export_dir, "export_result.json"))
    reread_report = _read(os.path.join(export_dir, "export_reread_report.json"))
    failed_formats = result.get("failed_formats") or []
    evidence["export_status"] = estatus["status"]
    evidence["export_failed_formats"] = failed_formats
    evidence["export_reread_report"] = reread_report
    _write_evidence("uv_export_refused.json", evidence)

    assert estatus["status"] == "failed", estatus
    assert failed_formats, result
    codes = sorted({ff.get("code") for ff in failed_formats})
    assert codes == ["reread_audit_failed"], json.dumps(failed_formats, indent=2, default=str)
    assert {ff.get("format") for ff in failed_formats} == set(formats), failed_formats
    assert reread_report.get("passed") is False, reread_report
    assert sorted(reread_report.get("failed_formats") or []) == sorted(formats), reread_report


# ---------------------------------------------------------------------------
# G8 — performance: 3 sequential runs per fixture, median/max time + peak memory
# ---------------------------------------------------------------------------
@requires_blender
def test_performance_three_runs(fixtures, tmp_path):
    runs: dict[str, list[dict]] = {}
    stats: dict[str, dict] = {}

    for name in ("suzanne", "torus"):
        entries: list[dict] = []
        for i in range(3):
            project = str(tmp_path / f"{name}_{i}")
            run_dir = os.path.join(project, "run")
            os.makedirs(run_dir, exist_ok=True)
            job = _uv_job(_model(fixtures, name), run_dir, project, f"perf_{name}_{i}")
            proc, wall_s = _run_uv(job, tmp_path, f"perf_{name}_{i}")
            entry: dict = {"run": i, "returncode": proc.returncode,
                           "wall_s": round(wall_s, 3)}
            summary_path = os.path.join(run_dir, "uv_generate_summary.json")
            if os.path.exists(summary_path):
                summary = _read(summary_path)
                entry["status"] = summary.get("status")
                entry["performance"] = summary.get("performance")
            status_path = os.path.join(run_dir, "status.json")
            if os.path.exists(status_path):
                entry["run_status"] = _read(status_path).get("status")
            if proc.returncode != 0:
                entry["tail"] = _tail(proc)[-2000:]
            entries.append(entry)
        runs[name] = entries

        def _series(key: str) -> list[float]:
            out = []
            for e in entries:
                v = (e.get("performance") or {}).get(key)
                if isinstance(v, (int, float)):
                    out.append(float(v))
            return out

        walls = [float(e["wall_s"]) for e in entries]
        elapsed = _series("elapsed_s")
        peaks = _series("peak_memory_mb")
        stats[name] = {
            "wall_s": {"median": round(statistics.median(walls), 3),
                       "max": round(max(walls), 3)},
            "elapsed_s": ({"median": round(statistics.median(elapsed), 3),
                           "max": round(max(elapsed), 3)} if elapsed else None),
            "peak_memory_mb": ({"median": round(statistics.median(peaks), 3),
                                "max": round(max(peaks), 3)} if peaks else None),
        }

    _write_evidence("uv_performance.json", {
        "note": "No time threshold is asserted: the G8 budget is fixed at "
                "calibration, which has not happened yet. Numbers are raw evidence.",
        "runs": runs, "stats": stats})

    failures: list[str] = []
    for name, entries in runs.items():
        for e in entries:
            if e["returncode"] != 0:
                failures.append(f"{name} run {e['run']}: exit {e['returncode']}")
            perf = e.get("performance")
            if not isinstance(perf, dict) or "elapsed_s" not in perf \
                    or "peak_memory_mb" not in perf:
                failures.append(f"{name} run {e['run']}: performance block missing ({perf})")
    assert not failures, json.dumps({"failures": failures, "runs": runs, "stats": stats},
                                    indent=2, default=str)
