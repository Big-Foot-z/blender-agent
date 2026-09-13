"""Shared worker <-> Electron JSON contract for the MVP 5 Production Export app.

Single source of truth for the JSON shapes the Electron app and the
Python/Blender ``export_production_asset`` worker exchange
(``docs/ELECTRON_UV_REVIEW_APP_MVP5_PRODUCTION_PLAN.ko.md`` §4, §5, §6, §7, §8, §9).

Like ``worker/app_uv_generate_contract.py`` it is intentionally **pure Python**
(no ``bpy``, no NumPy) so it loads stand-alone in unit tests and stays a tiny,
self-contained dependency. ``app/shared/contracts/export.ts`` is its TypeScript
mirror.

MVP 5 product rules encoded here (plan §0, §5, §6, §7, §9):

- Only an ACCEPTED MVP 3 selected UV may export. Readiness derives ONLY from the
  MVP 3 metrics + export validation; MVP 4 AI Review is skipped and is an
  informational warning, never an export blocker (plan §0, §15).
- Export status lifecycle adds ``partial``: at least one requested format
  exported + validated, but not all (plan §5 failure policy). All-fail is
  ``failed``; the UI must never hide a partial failure.
- A missing UV layer in the re-opened file is ALWAYS a hard failure; object /
  material naming and vertex-count splits are format differences reported as
  warnings, not failures (plan §7 tolerance policy).
- The worker NEVER overwrites the source ``working_model``, ``selected_uv_model``
  or ``user_seam_spec``; every exit leaves a structured JSON result (plan §11, §16).
- History is append-only and rollback updates project pointers, never historical
  artifacts (plan §8, §9).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Iterable

SCHEMA_VERSION = 1

# --- App-facing command names (stable contract, plan §4, §5, §9) -----------
CMD_CHECK_EXPORT_READINESS = "check_export_readiness"
CMD_EXPORT_PRODUCTION_ASSET = "export_production_asset"
CMD_LIST_ROLLBACK_TARGETS = "list_rollback_targets"
CMD_ROLLBACK_PROJECT_STATE = "rollback_project_state"
COMMANDS = (
    CMD_CHECK_EXPORT_READINESS,
    CMD_EXPORT_PRODUCTION_ASSET,
    CMD_LIST_ROLLBACK_TARGETS,
    CMD_ROLLBACK_PROJECT_STATE,
)

# --- Export run status lifecycle (plan §5, §6 status.json) -----------------
# ``partial`` is the MVP-5-specific terminal status for a run where at least one
# requested format exported + validated but not all did (plan §5). It still gets
# an ``export_manifest.json`` and still updates ``latest_export_id`` (plan §14).
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_ACCEPTED = "accepted"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUSES = (
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_ACCEPTED,
    STATUS_PARTIAL,
    STATUS_FAILED,
    STATUS_CANCELLED,
)
TERMINAL_STATUSES = (STATUS_ACCEPTED, STATUS_PARTIAL, STATUS_FAILED, STATUS_CANCELLED)
# An export that produced a manifest the app should pin as ``latest_export_id``.
SHIPPED_STATUSES = (STATUS_ACCEPTED, STATUS_PARTIAL)

# --- Readiness status (plan §4.1) ------------------------------------------
READY_ACCEPTED = "accepted"
READY_NEEDS_INPUT = "needs_input"

# --- Supported export formats (plan §5.1) ----------------------------------
FMT_FBX = "fbx"
FMT_OBJ = "obj"
FMT_GLB = "glb"
FMT_GLTF = "gltf"
SUPPORTED_FORMATS = (FMT_FBX, FMT_OBJ, FMT_GLB, FMT_GLTF)
FORMAT_EXT = {FMT_FBX: ".fbx", FMT_OBJ: ".obj", FMT_GLB: ".glb", FMT_GLTF: ".gltf"}

# The MVP 3 handoff input (plan §2). selected_uv_model is the accepted .blend.
SELECTED_UV_BLEND_REL = os.path.join("work", "uv", "selected_uv.blend")
SELECTED_UV_SUMMARY_REL = os.path.join("work", "uv", "selected_uv_summary.json")

# --- History event types (plan §8) -----------------------------------------
EVENT_UV_SELECTED = "uv_selected"
EVENT_EXPORT_CREATED = "export_created"
EVENT_EXPORT_FAILED = "export_failed"
EVENT_ROLLBACK_PERFORMED = "rollback_performed"
EVENT_TYPES = (
    EVENT_UV_SELECTED,
    EVENT_EXPORT_CREATED,
    EVENT_EXPORT_FAILED,
    EVENT_ROLLBACK_PERFORMED,
)

# --- Rollback target types (plan §9) ---------------------------------------
TARGET_UV_RUN = "uv_run"
TARGET_EXPORT = "export"
TARGET_TYPES = (TARGET_UV_RUN, TARGET_EXPORT)

# --- Default export options (plan §5.1 options block) ----------------------
# ``selected_uv_layer = None`` means "keep the object's active UV layer".
# ``export_name = None`` means "derive from object name + _low_uv".
DEFAULT_EXPORT_OPTIONS: dict[str, Any] = {
    "selected_uv_layer": None,
    "apply_scale": True,
    "include_materials": True,
    "include_normals": True,
    "copy_textures": False,
    "triangulate": False,
    "axis_forward": "-Z",
    "axis_up": "Y",
    "export_name": None,
}
# The options block persisted into the manifest (plan §6 ``options``).
MANIFEST_OPTION_KEYS = (
    "selected_uv_layer",
    "apply_scale",
    "include_materials",
    "include_normals",
    "copy_textures",
    "triangulate",
)

# --- Metric subset surfaced on the manifest (plan §6 ``metrics``) ----------
# Pulled from the MVP 3 ``selected_uv_summary.json`` metrics so the export
# manifest carries the headline quality numbers without re-measuring UVs.
EXPORT_METRIC_KEYS = (
    "stretch_score",
    "worst_island_distortion",
    "raster_overlap_ratio",
    "texel_density_variance",
    "packing_efficiency",
)

# --- Artifact registry (plan §2 folder layout / §5.1 artifacts) ------------
MANIFEST_FILE = "export_manifest.json"
VALIDATION_REPORT_FILE = "validation_report.json"
# Gate G13/G15: the source-matched re-read audit of every shipped file. Separate
# from ``validation_report.json`` on purpose — that one answers "does the file
# re-open with a UV layer?", this one answers "is it still the approved UV?".
EXPORT_REREAD_REPORT_FILE = "export_reread_report.json"
STATUS_FILE = "status.json"
# key -> (filename, required). Missing required preview becomes a warning, not a
# hard failure (plan §7 step 7 "best-effort preview").
PREVIEW_ARTIFACTS: dict[str, tuple[str, bool]] = {
    "uv_layout": ("uv_layout.png", False),
    "checker_front": ("checker_front.png", False),
    "checker_side": ("checker_side.png", False),
}

# --- Readiness check -> blocking issue mapping (plan §4) --------------------
# Only these checks block export. ``ai_review_*`` are informational (plan §0).
READINESS_BLOCKERS: dict[str, tuple[str, str]] = {
    "model_exists": ("missing_selected_uv_model",
                     "Run MVP 3 Generate + Optimize before export."),
    "summary_exists": ("missing_selected_uv_summary",
                       "Selected UV summary is missing; re-run MVP 3 Generate + Optimize."),
    "uv_run_accepted": ("uv_run_not_accepted",
                        "Selected UV run is not accepted; resolve seam/overlap review in MVP 3."),
    "raster_overlap_ok": ("raster_overlap",
                          "Selected UV has raster overlap; it cannot be exported."),
    "uv_bounds_ok": ("uv_out_of_bounds",
                     "Selected UV is outside the [0,1] tile; it cannot be exported."),
    "seam_integrity_ok": ("seam_integrity_failed",
                          "Selected UV failed seam integrity; revisit the MVP 2 Seam Editor."),
}
# The order readiness checks appear in the output ``checks`` block (plan §4.1).
READINESS_CHECK_ORDER = (
    "model_exists",
    "summary_exists",
    "uv_run_accepted",
    "raster_overlap_ok",
    "uv_bounds_ok",
    "seam_integrity_ok",
    "ai_review_required",
    "ai_review_skipped",
)


# ---------------------------------------------------------------------------
# Time / IO helpers (kept local so the module loads stand-alone in tests)
# ---------------------------------------------------------------------------
def utc_now_iso() -> str:
    """ISO-8601 UTC timestamp with millisecond precision (matches project.json)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def read_json_optional(path: str | None) -> Any | None:
    """Read JSON if ``path`` is set and the file exists + parses; else ``None``."""
    if not path:
        return None
    try:
        return read_json(path)
    except (OSError, ValueError, TypeError):
        return None


def write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


# ---------------------------------------------------------------------------
# Error / status envelopes (mirror the MVP 1/2/3 contracts)
# ---------------------------------------------------------------------------
def error_envelope(command: str, message: str, *, code: str = "worker_error", **extra: Any) -> dict:
    """A JSON error any worker can emit so the app never has to parse stdout."""
    env = {
        "schema_version": SCHEMA_VERSION,
        "status": STATUS_FAILED,
        "command": command,
        "error": {"code": code, "message": message},
    }
    env.update(extra)
    return env


def new_status(
    *,
    export_id: str,
    command: str = CMD_EXPORT_PRODUCTION_ASSET,
    status: str = STATUS_QUEUED,
    input: dict | None = None,
) -> dict:
    """Build an initial ``status.json`` document (plan §5, §6)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "export_id": export_id,
        "command": command,
        "status": status,
        "started_at": utc_now_iso(),
        "finished_at": None,
        "input": input or {},
        "artifacts": {},
        "error": None,
    }


def finalize_status(
    status_doc: dict,
    *,
    status: str,
    artifacts: dict | None = None,
    error: dict | None = None,
) -> dict:
    """Stamp a terminal status onto an existing status doc."""
    status_doc["status"] = status
    status_doc["finished_at"] = utc_now_iso()
    if artifacts is not None:
        status_doc["artifacts"] = artifacts
    if error is not None:
        status_doc["error"] = error
    return status_doc


# ---------------------------------------------------------------------------
# Export options (plan §5.1 options block)
# ---------------------------------------------------------------------------
def default_options() -> dict:
    """A fresh copy of the default export options (plan §5.1)."""
    return dict(DEFAULT_EXPORT_OPTIONS)


def merge_options(user_options: dict | None) -> dict:
    """Overlay caller-supplied options on the defaults (plan §5.1).

    Unknown keys pass through (e.g. render tuning), so the worker can read its own
    extras, but the manifest only persists ``MANIFEST_OPTION_KEYS``.
    """
    opts = default_options()
    for k, v in (user_options or {}).items():
        opts[k] = v
    return opts


def manifest_options(options: dict) -> dict:
    """The subset of options persisted into the manifest (plan §6)."""
    return {k: options.get(k, DEFAULT_EXPORT_OPTIONS.get(k)) for k in MANIFEST_OPTION_KEYS}


def normalize_formats(formats: Iterable[str] | None) -> list[str]:
    """Lower-case, de-duplicate, and keep only supported formats, in request order.

    ``glb`` and ``gltf`` are distinct outputs (plan §5.1) and both survive.
    """
    out: list[str] = []
    for f in formats or []:
        key = str(f).strip().lower().lstrip(".")
        if key in FORMAT_EXT and key not in out:
            out.append(key)
    return out


def export_filename(export_name: str | None, object_name: str | None, fmt: str) -> str:
    """Resolve ``<name>.<ext>`` for one format (plan §5.1 ``export_name``)."""
    base = (export_name or "").strip()
    if not base:
        obj = (object_name or "model").strip() or "model"
        base = f"{obj}_low_uv"
    return f"{base}{FORMAT_EXT[fmt]}"


# ---------------------------------------------------------------------------
# Metric flattening (plan §6 metrics block)
# ---------------------------------------------------------------------------
def flatten_export_metrics(metrics: dict | None) -> dict:
    """Pick the headline metrics off the MVP 3 summary, rounding floats (plan §6)."""
    out: dict[str, Any] = {}
    for key in EXPORT_METRIC_KEYS:
        if not metrics or key not in metrics:
            continue
        val = metrics[key]
        out[key] = round(float(val), 6) if isinstance(val, (int, float)) else val
    return out


# ---------------------------------------------------------------------------
# Export readiness (plan §4 — accepted MVP 3 selected UV only)
# ---------------------------------------------------------------------------
def readiness_checks_from_summary(
    summary: dict | None,
    *,
    model_exists: bool,
    summary_exists: bool,
    ai_review_skipped: bool = True,
    raster_overlap_max: float = 0.005,
) -> dict:
    """Derive the readiness ``checks`` block from the MVP 3 selected UV summary.

    Pure — the summary is a plain dict, so this is unit-tested without Blender.
    ``model_exists`` / ``summary_exists`` are filesystem facts the caller resolves.
    The quality checks read the accepted MVP 3 summary (plan §0 entry conditions):
    run status ``accepted``, no raster overlap, UVs in [0,1], seam integrity valid.
    """
    s = summary or {}
    metrics = s.get("metrics") or {}
    integrity = s.get("seam_integrity") or {}

    def _num(key: str, default: float = 0.0) -> float:
        v = metrics.get(key, default)
        return float(v) if isinstance(v, (int, float)) else default

    uv_run_accepted = summary_exists and s.get("status") == "accepted"
    raster_overlap_ok = summary_exists and _num("raster_overlap_ratio") <= raster_overlap_max
    # ``uv_bounds_ok`` defaults True when the summary omits it (older runs).
    uv_bounds_ok = summary_exists and bool(metrics.get("uv_bounds_ok", True))
    seam_integrity_ok = summary_exists and bool(integrity.get("valid", False))

    return {
        "model_exists": bool(model_exists),
        "summary_exists": bool(summary_exists),
        "uv_run_accepted": bool(uv_run_accepted),
        "raster_overlap_ok": bool(raster_overlap_ok),
        "uv_bounds_ok": bool(uv_bounds_ok),
        "seam_integrity_ok": bool(seam_integrity_ok),
        # MVP 4 is skipped: never required, always reported as skipped (plan §0).
        "ai_review_required": False,
        "ai_review_skipped": bool(ai_review_skipped),
    }


def readiness_blocking_issues(checks: dict) -> list[dict]:
    """Map false hard checks to blocking issues, in check order (plan §4.1, §4.2)."""
    issues: list[dict] = []
    for key, (code, message) in READINESS_BLOCKERS.items():
        if not checks.get(key, False):
            issues.append({"code": code, "message": message})
    return issues


def build_readiness(
    checks: dict,
    *,
    selected_uv_model: str | None = None,
    source_uv_run_id: str | None = None,
    warnings: list[str] | None = None,
) -> dict:
    """Assemble the ``check_export_readiness`` output (plan §4.1, §4.2).

    ``ready`` and ``status`` derive from the blocking issues, so an accepted
    project is ``status=accepted, ready=true`` and a project missing the selected
    UV is ``status=needs_input, ready=false`` (plan §4.2).
    """
    blocking = readiness_blocking_issues(checks)
    warn = list(warnings or [])
    if checks.get("ai_review_skipped"):
        warn.append("AI Review was skipped.")
    ready = not blocking
    return {
        "schema_version": SCHEMA_VERSION,
        "status": READY_ACCEPTED if ready else READY_NEEDS_INPUT,
        "ready": ready,
        "selected_uv_model": selected_uv_model,
        "source_uv_run_id": source_uv_run_id,
        "checks": {k: checks.get(k) for k in READINESS_CHECK_ORDER if k in checks},
        "blocking_issues": blocking,
        "warnings": warn,
    }


# ---------------------------------------------------------------------------
# Export result status policy (plan §5 failure policy)
# ---------------------------------------------------------------------------
def classify_export_status(requested_formats: Iterable[str], succeeded_formats: Iterable[str]) -> str:
    """The terminal export status from requested vs validated formats (plan §5).

    - all-fail -> ``failed``
    - some succeed, not all -> ``partial``
    - all succeed -> ``accepted``
    """
    requested = list(dict.fromkeys(requested_formats))
    succeeded = [f for f in dict.fromkeys(succeeded_formats) if f in requested]
    if not succeeded:
        return STATUS_FAILED
    if len(succeeded) < len(requested):
        return STATUS_PARTIAL
    return STATUS_ACCEPTED


def format_validation_ok(fmt_validation: dict | None, reread: dict | None = None) -> bool:
    """A format "succeeds" iff it re-opened AND carries a UV layer (plan §7).

    Missing UV is always a hard failure; normals / material / vertex-count
    differences are warnings, never failures (plan §7 tolerance policy).

    When a Gate G13 ``reread`` audit was run for the format it must ALSO pass:
    a file that re-opens with a UV layer but whose UV no longer matches the
    approved layout is not a shippable export. ``reread=None`` means "not
    audited" and leaves the plan §7 verdict untouched.
    """
    v = fmt_validation or {}
    ok = bool(v.get("reopen_ok")) and bool(v.get("has_uv"))
    if reread is not None:
        ok = ok and bool(reread.get("passed"))
    return ok


def classify_validation_status(formats: dict) -> str:
    """Overall validation status from the per-format results (plan §7)."""
    if not formats:
        return STATUS_FAILED
    oks = [format_validation_ok(v) for v in formats.values()]
    if all(oks):
        return STATUS_ACCEPTED
    if any(oks):
        return STATUS_PARTIAL
    return STATUS_FAILED


def build_validation_report(formats: dict, *, status: str | None = None) -> dict:
    """Assemble ``validation_report.json`` (plan §7).

    ``formats`` maps each attempted format -> its reopen result; the overall
    status is derived unless the caller pins one.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status or classify_validation_status(formats),
        "formats": formats,
    }


def build_reread_report(formats: dict, *, profile_id: str | None) -> dict:
    """Assemble ``export_reread_report.json`` (Gate G13 / G15 evidence).

    ``formats`` maps each audited format -> its ``build_reread_audit`` block.
    ``passed`` is the conjunction over every audited format (an empty map is a
    vacuous pass — nothing was shipped to audit), and ``failed_formats`` names
    the ones that failed so the evidence is readable without walking the blocks.
    """
    fmts = dict(formats or {})
    failed = [f for f, block in fmts.items() if not bool((block or {}).get("passed"))]
    return {
        "schema_version": SCHEMA_VERSION,
        "profile_id": profile_id,
        "formats": fmts,
        "passed": not failed,
        "failed_formats": failed,
    }


# ---------------------------------------------------------------------------
# Source + manifest builders (plan §5.1 source, §6 manifest)
# ---------------------------------------------------------------------------
def build_result_source(
    *,
    selected_uv_model: str | None,
    selected_uv_summary: str | None,
    uv_generate_run_id: str | None,
    seam_spec: str | None,
    selected_candidate_id: str | None,
    ai_review_run_id: str | None = None,
    ai_review_skipped: bool = True,
) -> dict:
    """The ``source`` block on the export *result* (plan §5.1 output)."""
    return {
        "selected_uv_model": selected_uv_model,
        "selected_uv_summary": selected_uv_summary,
        "uv_generate_run_id": uv_generate_run_id,
        "seam_spec": seam_spec,
        "selected_candidate_id": selected_candidate_id,
        "ai_review_run_id": ai_review_run_id,
        "ai_review_skipped": bool(ai_review_skipped),
    }


def build_manifest_source(
    *,
    selected_uv_model: str | None,
    selected_uv_summary: str | None,
    uv_generate_run_id: str | None,
    active_user_seam_spec: str | None,
    candidate_summary: str | None = None,
    p5_gate: str | None = None,
    seam_report: str | None = None,
    ai_review_run_id: str | None = None,
    ai_review_skipped: bool = True,
) -> dict:
    """The ``source`` block on the export *manifest* (plan §6).

    Differs from the result source: it links the MVP 3 run artifacts
    (candidate_summary / p5_gate / seam_report) instead of the selected candidate
    id, so the manifest is the self-contained source of truth for history UI.
    """
    return {
        "selected_uv_model": selected_uv_model,
        "selected_uv_summary": selected_uv_summary,
        "uv_generate_run_id": uv_generate_run_id,
        "active_user_seam_spec": active_user_seam_spec,
        "candidate_summary": candidate_summary,
        "p5_gate": p5_gate,
        "seam_report": seam_report,
        "ai_review_run_id": ai_review_run_id,
        "ai_review_skipped": bool(ai_review_skipped),
    }


def build_export_manifest(
    *,
    export_id: str,
    created_at: str,
    status: str,
    formats: Iterable[str],
    options: dict,
    source: dict,
    metrics: dict | None,
    files: dict,
    validation: str = VALIDATION_REPORT_FILE,
) -> dict:
    """Assemble ``export_manifest.json`` — the source of truth for history UI (plan §6)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "export_id": export_id,
        "created_at": created_at,
        "status": status,
        "formats": list(formats),
        "options": manifest_options(options),
        "source": source,
        "metrics": flatten_export_metrics(metrics),
        "files": files,
        "validation": validation,
    }


def build_export_result(
    *,
    export_id: str,
    status: str,
    source: dict,
    exports: dict,
    validation: dict,
    artifacts: dict,
    failed_formats: list[dict] | None = None,
    warnings: list[str] | None = None,
    reread: dict | None = None,
) -> dict:
    """Assemble the ``export_production_asset`` result (plan §5.1).

    ``exports`` maps each *succeeded* format -> its project-relative path;
    ``failed_formats`` carries structured per-format failures so the UI can never
    hide a partial failure (plan §5). ``reread`` is the optional Gate G13 report
    (:func:`build_reread_report`); it is omitted when no re-read audit ran.
    """
    result = {
        "schema_version": SCHEMA_VERSION,
        "export_id": export_id,
        "command": CMD_EXPORT_PRODUCTION_ASSET,
        "status": status,
        "source": source,
        "exports": exports,
        "validation": validation,
        "artifacts": artifacts,
        "warnings": list(warnings or []),
    }
    if reread is not None:
        result["reread"] = reread
    if failed_formats:
        result["failed_formats"] = failed_formats
    return result


def collect_export_files(out_dir: str, export_paths: dict, export_name_files: dict) -> dict:
    """Build the manifest ``files`` block (plan §6 ``files``).

    ``export_name_files`` maps format -> bare filename for each shipped format;
    preview artifacts are added when present on disk.
    """
    files = dict(export_name_files)
    for key, (filename, _required) in PREVIEW_ARTIFACTS.items():
        if os.path.exists(os.path.join(out_dir, filename)):
            files[key] = filename
    return files


def collect_export_artifacts(out_dir: str) -> tuple[dict, list[str]]:
    """Return ``(artifacts, warnings)`` for the export artifacts in ``out_dir`` (plan §5.1).

    ``manifest`` + ``validation_report`` + ``reread_report`` (Gate G13/G15) keys
    always point at their canonical
    filenames; preview keys are added when present. Missing previews are silent
    (they are best-effort, plan §7) — the result still ships.
    """
    artifacts: dict[str, str] = {
        "manifest": MANIFEST_FILE,
        "validation_report": VALIDATION_REPORT_FILE,
        "reread_report": EXPORT_REREAD_REPORT_FILE,
    }
    warnings: list[str] = []
    for key, (filename, required) in PREVIEW_ARTIFACTS.items():
        if os.path.exists(os.path.join(out_dir, filename)):
            artifacts[key] = filename
        elif required:
            warnings.append(f"missing preview artifact: {filename}")
    return artifacts, warnings


# ---------------------------------------------------------------------------
# Project history (plan §8 — append-only)
# ---------------------------------------------------------------------------
def empty_history() -> dict:
    """A fresh, valid ``project_history.json`` (plan §8)."""
    return {"schema_version": SCHEMA_VERSION, "events": []}


def make_export_event(
    *,
    event_id: str,
    created_at: str,
    export_id: str,
    uv_generate_run_id: str | None,
    selected_candidate_id: str | None,
    seam_spec: str | None,
    manifest: str,
    summary: dict,
    failed: bool = False,
) -> dict:
    """An ``export_created`` / ``export_failed`` history event (plan §8)."""
    return {
        "id": event_id,
        "type": EVENT_EXPORT_FAILED if failed else EVENT_EXPORT_CREATED,
        "created_at": created_at,
        "export_id": export_id,
        "uv_generate_run_id": uv_generate_run_id,
        "selected_candidate_id": selected_candidate_id,
        "seam_spec": seam_spec,
        "manifest": manifest,
        "summary": summary,
    }


def make_rollback_event(
    *,
    event_id: str,
    created_at: str,
    target_type: str,
    target_id: str,
    selected_uv_model: str | None = None,
    selected_uv_summary: str | None = None,
) -> dict:
    """A ``rollback_performed`` history event (plan §8, §9)."""
    return {
        "id": event_id,
        "type": EVENT_ROLLBACK_PERFORMED,
        "created_at": created_at,
        "target_type": target_type,
        "target_id": target_id,
        "selected_uv_model": selected_uv_model,
        "selected_uv_summary": selected_uv_summary,
    }
