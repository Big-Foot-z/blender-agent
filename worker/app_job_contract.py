"""Shared worker <-> Electron JSON contract for the MVP 0 review app.

This module is the single source of truth for the JSON shapes exchanged between
the Electron app and the Python/Blender workers
(``docs/ELECTRON_UV_REVIEW_APP_MVP0_PRODUCTION_PLAN.ko.md`` §4, §5).

It is intentionally **pure Python** (no ``bpy``, no NumPy) so it can be imported
and unit-tested outside Blender, and reused by both ``worker/inspect_model.py``
and ``worker/run_app_retopo_job.py``.

Design rules (plan §10 "contract drift" mitigation):

- ``SCHEMA_VERSION`` is pinned; new fields are introduced as optional only.
- The app-facing command names are stable; the underlying worker CLI options may
  change behind the wrappers.
- Failures are always representable as JSON (never only stdout/stderr).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1

# --- App-facing command names (stable contract, plan §5) -------------------
CMD_INSPECT_MODEL = "inspect_model"
CMD_GENERATE_LOWPOLY = "generate_lowpoly"
CMD_APPROVE_LOWPOLY = "approve_lowpoly"
COMMANDS = (CMD_INSPECT_MODEL, CMD_GENERATE_LOWPOLY, CMD_APPROVE_LOWPOLY)

# --- Run status lifecycle (plan §4 status.json) ----------------------------
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUSES = (
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_ACCEPTED,
    STATUS_REJECTED,
    STATUS_FAILED,
    STATUS_CANCELLED,
)

# --- Supported source formats (plan §5.1 / §10 import branching) -----------
SUPPORTED_IMPORT_EXTS = (".fbx", ".obj", ".glb", ".gltf")

# --- Role heuristic thresholds (plan §5.1) ---------------------------------
# faces <= LOWPOLY_FACE_THRESHOLD          -> "lowpoly"
# faces >= HIGHPOLY_FACE_THRESHOLD         -> "highpoly"
# in between                               -> "unknown"
LOWPOLY_FACE_THRESHOLD = 20_000
HIGHPOLY_FACE_THRESHOLD = 60_000

ROLE_LOWPOLY = "lowpoly"
ROLE_HIGHPOLY = "highpoly"
ROLE_UNKNOWN = "unknown"

# --- Recommended next steps (plan §5.1 output) -----------------------------
NEXT_APPROVE_EXISTING = "approve_existing_lowpoly"
NEXT_GENERATE = "generate_lowpoly"
NEXT_INSPECT_MANUALLY = "inspect_manually"


# ---------------------------------------------------------------------------
# Time / IO helpers
# ---------------------------------------------------------------------------
def utc_now_iso() -> str:
    """ISO-8601 UTC timestamp with millisecond precision (matches project.json)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def read_json_optional(path: str) -> Any | None:
    """Read JSON if the file exists and parses; otherwise return ``None``."""
    try:
        return read_json(path)
    except (OSError, ValueError):
        return None


def write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


# ---------------------------------------------------------------------------
# Role heuristic
# ---------------------------------------------------------------------------
def role_hint(
    faces: int,
    *,
    low_threshold: int = LOWPOLY_FACE_THRESHOLD,
    high_threshold: int = HIGHPOLY_FACE_THRESHOLD,
) -> str:
    """Heuristic mesh-role classification by face count (plan §5.1).

    The classification is intentionally conservative: anything between the two
    thresholds is ``unknown`` so the user is asked to confirm rather than the app
    guessing wrong.
    """
    if faces <= 0:
        return ROLE_UNKNOWN
    if faces <= low_threshold:
        return ROLE_LOWPOLY
    if faces >= high_threshold:
        return ROLE_HIGHPOLY
    return ROLE_UNKNOWN


def recommended_next_step(objects: list[dict]) -> str:
    """Recommend the next workflow step from inspected objects (plan §5.1).

    - any object looks high-poly         -> generate_lowpoly
    - all objects look low-poly          -> approve_existing_lowpoly
    - otherwise (unknown / empty)        -> inspect_manually
    """
    if not objects:
        return NEXT_INSPECT_MANUALLY
    roles = [o.get("mesh_role_hint", ROLE_UNKNOWN) for o in objects]
    if any(r == ROLE_HIGHPOLY for r in roles):
        return NEXT_GENERATE
    if roles and all(r == ROLE_LOWPOLY for r in roles):
        return NEXT_APPROVE_EXISTING
    return NEXT_INSPECT_MANUALLY


# ---------------------------------------------------------------------------
# Error / status envelopes
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
    run_id: str,
    command: str = CMD_GENERATE_LOWPOLY,
    status: str = STATUS_QUEUED,
    input: dict | None = None,
) -> dict:
    """Build an initial ``status.json`` document (plan §4)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
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
# Summary normalization (plan §5.2 implementation note)
# ---------------------------------------------------------------------------
# The known run artifacts produced by worker/run_retopo_job.py. ``required`` ones
# missing become warnings; missing optional ones are silently skipped.
_ARTIFACT_FILES = {
    "retopo_plan": ("retopo_plan.json", False),
    "feature_report": ("feature_report.json", False),
    "generation_report": ("generation_report.json", True),
    "quadflow_report": ("quadflow_report.json", False),
    "validation_report": ("validation_report.json", True),
    "shape_report": ("shape_report.json", True),
    "lowpoly_blend": ("lowpoly.blend", True),
    "lowpoly_fbx": ("lowpoly.fbx", False),
    "lowpoly_glb": ("lowpoly.glb", False),
    "preview": ("preview.png", False),
}


def collect_artifacts(run_dir: str) -> tuple[dict, list[str]]:
    """Return ``(artifacts, warnings)`` for the artifacts present in ``run_dir``.

    ``artifacts`` maps a stable key -> filename (relative to the run dir) for each
    file that exists. Missing *required* artifacts are reported as warnings so the
    app can surface partial results instead of treating them as a hard failure
    (plan §3, §5.2 "best-effort artifact 누락을 warnings로").
    """
    artifacts: dict[str, str] = {}
    warnings: list[str] = []
    for key, (filename, required) in _ARTIFACT_FILES.items():
        if os.path.exists(os.path.join(run_dir, filename)):
            artifacts[key] = filename
        elif required:
            warnings.append(f"missing artifact: {filename}")
    return artifacts, warnings


def normalize_summary(
    run_dir: str,
    *,
    run_id: str,
    object_name: str | None = None,
    target_faces: int | None = None,
) -> dict:
    """Normalize the per-phase worker reports into a single ``summary.json``.

    Reads ``generation_report.json``, ``validation_report.json`` and
    ``shape_report.json`` from ``run_dir`` (any of which may be absent) and folds
    them into the app contract shape (plan §5.2 output). ``target`` and ``actual``
    face counts are always present (plan Session B acceptance), falling back to the
    explicit ``target_faces`` input when the generation report is missing.
    """
    gen = read_json_optional(os.path.join(run_dir, "generation_report.json")) or {}
    val = read_json_optional(os.path.join(run_dir, "validation_report.json")) or {}
    shape = read_json_optional(os.path.join(run_dir, "shape_report.json")) or {}

    artifacts, warnings = collect_artifacts(run_dir)

    source_faces = gen.get("source_face_count")
    actual_faces = gen.get("actual_face_count")
    resolved_target = gen.get("target_face_count", target_faces)

    metrics = {
        "source_faces": source_faces,
        "target_faces": resolved_target,
        "actual_faces": actual_faces,
        "target_error_ratio": gen.get("target_error_ratio"),
        "non_manifold_edges": val.get("non_manifold_edge_count"),
        "quad_ratio": val.get("quad_ratio"),
        "triangle_ratio": val.get("triangle_ratio"),
        "ngon_count": val.get("ngon_count"),
        "surface_distance_mean_ratio": shape.get("surface_distance_mean_ratio"),
        "surface_distance_max_ratio": shape.get("surface_distance_max_ratio"),
        "normal_deviation_mean_deg": shape.get("normal_deviation_mean_deg"),
        "volume_error_ratio": shape.get("volume_error_ratio"),
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "command": CMD_GENERATE_LOWPOLY,
        "object_name": object_name or gen.get("object_name"),
        "result_object_name": gen.get("result_object_name"),
        "method": gen.get("method"),
        "metrics": metrics,
        "reports": {
            "generation": _report_status(gen),
            "validation": val.get("status"),
            "shape": shape.get("status"),
        },
        "artifacts": artifacts,
        "warnings": warnings,
    }


def _report_status(gen: dict) -> str | None:
    """Generation has no explicit status field; the band IS its coarse status.

    ``band`` comes from ``retopo_agent.geometry.target_search.quality_band`` and is
    one of ``accepted`` | ``retry`` | ``failed``.
    """
    return gen.get("band")
