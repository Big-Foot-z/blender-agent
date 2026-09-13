"""App-facing seam-editor worker (Electron MVP 2 plan §5, §6; Sessions A/B/C).

Run inside Blender:

    blender --background --python worker/seam_editor_worker.py -- --job /abs/job.json

``job.json`` carries a ``command`` field selecting the operation (plan §5/§6):

- ``export_edge_geometry``       — open/import the model, extract the selected
  object's edge table with its Blender edge ids, and write ``edge_geometry.json``
  (the only selectable-id source for the renderer, plan §5).
- ``extract_uv_boundary_as_seams`` — read the chosen UV layer and convert its
  island boundaries into ``user_seam_edges`` (plan §6.4). No UV layer ->
  ``status: no_uv`` (not a failure).
- ``validate_user_seam_spec``    — validate a spec against the current mesh
  (invalid edges, seam/protect conflicts, object mismatch) (plan §6.3).
- ``load_user_seam_spec``        — read a spec file + validate it (plan §6.1).
- ``save_user_seam_spec``        — normalize (seam-wins, drop invalid) and write
  the canonical ``user_seam_spec.json`` (plan §6.2).

MVP 2 is **non-generative**: this worker never unwraps, packs, marks seams on the
mesh, edits UVs, or saves the model, and it never auto-adds the mandatory-90 fold
(plan §1, §13). Every exit leaves a structured JSON result so the app never parses
stdout (plan §13). Edge ids come from
:func:`uv_agent.blender.extract.extract_mesh_graph` so they match what
``UserSeamSpec`` is validated against (plan §5, §14).
"""

from __future__ import annotations

import os
import sys
import traceback


# ---------------------------------------------------------------------------
# Arg / path / Blender-IO helpers (siblings of worker/review_existing_uv.py)
# ---------------------------------------------------------------------------
def _parse_args(argv: list[str]) -> dict:
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = []
    opts: dict[str, str] = {}
    i = 0
    while i < len(argv):
        if argv[i].startswith("--"):
            key = argv[i][2:].replace("-", "_")
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                opts[key] = argv[i + 1]
                i += 2
            else:
                opts[key] = "true"
                i += 1
        else:
            i += 1
    return opts


def _ensure_importable() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    for p in (root, here):
        if p not in sys.path:
            sys.path.insert(0, p)


def _open_model(bpy, path: str) -> dict:
    """Open a ``.blend`` or import a model into a fresh scene (plan §5; G1/G14).

    Non-``.blend`` input goes through the ONE shared importer
    (:func:`uv_agent.blender.topology_normalize.import_model`) so the glTF
    ``merge_vertices=True`` policy is identical on every entry point. Returns the
    importer's ``merge_info``; callers that ignore it keep working.
    """
    _ensure_importable()
    from uv_agent.blender.topology_normalize import import_model

    ext = os.path.splitext(path)[1].lower()
    if ext == ".blend":
        bpy.ops.wm.open_mainfile(filepath=path)
        return {"format": "blend", "merge_vertices_requested": False,
                "merge_vertices_supported": None, "merge_vertices_enabled": False}
    try:
        bpy.ops.wm.read_homefile(use_empty=True)
    except Exception:  # noqa: BLE001 - best-effort; default scene is acceptable
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o, do_unlink=True)
    return import_model(bpy, path, merge_vertices=True)


def _mesh_objects(bpy) -> list:
    return [o for o in bpy.data.objects if o.type == "MESH"]


def _resolve_object(bpy, object_name):
    obj = bpy.data.objects.get(object_name) if object_name else None
    if obj is None or obj.type != "MESH":
        obj = next((o for o in _mesh_objects(bpy)), None)
    return obj


#: Input-topology evidence file, same name as the generate worker writes (G6/G14).
IMPORT_TOPOLOGY_FILE = "import_topology.json"


def _normalize_opened_model(bpy, model_path: str, merge_info: dict | None,
                            object_name=None) -> dict:
    """Normalize the just-opened model ONCE, BEFORE any mesh extraction (G6/G14).

    Edge ids handed to the seam editor must come from the SAME normalized topology
    every other UV-path worker measures. Never raises: an unevaluatable
    normalization is recorded as ``{"error": ...}`` and the worker keeps going.
    """
    _ensure_importable()
    try:
        from uv_agent.blender.topology_normalize import (
            format_from_path, normalize_topology,
        )

        obj = _resolve_object(bpy, object_name)
        if obj is None:
            return {"error": "no mesh object to normalize"}
        return normalize_topology(bpy, obj, fmt=format_from_path(model_path),
                                  merge_info=merge_info)
    except Exception as exc:  # noqa: BLE001 - normalization evidence, never fatal
        print(f"seam_editor_worker: topology normalization failed: {exc}", file=sys.stderr)
        return {"error": str(exc)}


def _compact_import_topology(report: dict | None) -> dict:
    """Summary-sized normalization block (G14: the ONE compaction, shared)."""
    rep = dict(report or {})
    if "error" in rep and "format" not in rep:
        return {"error": rep["error"]}
    try:
        from app_uv_generate_contract import compact_import_topology_block  # type: ignore

        return compact_import_topology_block(rep)
    except Exception:  # noqa: BLE001 - fall back to the raw report
        return rep


def _write_import_topology(contract, out_dir: str | None, report: dict | None) -> None:
    """Write ``import_topology.json`` next to the worker's other output."""
    if not out_dir or report is None:
        return
    try:
        contract.write_json(os.path.join(out_dir, IMPORT_TOPOLOGY_FILE), report)
    except Exception as exc:  # noqa: BLE001 - evidence write is best-effort
        print(f"seam_editor_worker: import_topology write failed: {exc}", file=sys.stderr)


def _model_label(job: dict) -> str | None:
    rel = job.get("model_rel")
    if rel:
        return rel
    model = job.get("model")
    return os.path.basename(model) if model else None


def _status_input(job: dict) -> dict:
    return {
        "model": _model_label(job),
        "object_name": job.get("object_name"),
        "uv_layer": job.get("uv_layer"),
    }


# ---------------------------------------------------------------------------
# export_edge_geometry (plan §5.1)
# ---------------------------------------------------------------------------
def _run_export_edge_geometry(bpy, contract, job, out_dir, status_path, status,
                              import_topology=None) -> int:
    from uv_agent.blender.extract import extract_mesh_graph
    from uv_agent.geometry.edge_geometry import (
        build_edge_geometry, edge_geometry_size_warnings, mesh_signature,
    )

    obj = _resolve_object(bpy, job.get("object_name"))
    if obj is None:
        err = {"code": "object_not_found",
               "message": f"no mesh object to export (requested {job.get('object_name')!r})"}
        contract.finalize_status(status, status=contract.STATUS_FAILED, error=err)
        contract.write_json(status_path, status)
        print(f"export_edge_geometry: {err['message']}", file=sys.stderr)
        return 2

    mesh = extract_mesh_graph(obj)
    geometry = build_edge_geometry(mesh)
    contract.write_json(os.path.join(out_dir, "edge_geometry.json"), geometry)

    signature = mesh_signature(mesh)
    warnings = edge_geometry_size_warnings(mesh)
    artifacts = {"edge_geometry": "edge_geometry.json"}
    result = {
        "schema_version": contract.SCHEMA_VERSION,
        "status": contract.STATUS_ACCEPTED,
        "command": contract.CMD_EXPORT_EDGE_GEOMETRY,
        "object_name": obj.name,
        "mesh_signature": signature,
        "import_topology": import_topology,
        "artifacts": artifacts,
        "warnings": warnings,
    }
    contract.write_json(os.path.join(out_dir, "export_result.json"), result)
    contract.finalize_status(status, status=contract.STATUS_ACCEPTED, artifacts=artifacts)
    contract.write_json(status_path, status)
    print(f"export_edge_geometry: accepted object={obj.name!r} edges={signature['edges']}")
    return 0


# ---------------------------------------------------------------------------
# extract_uv_boundary_as_seams (plan §6.4)
# ---------------------------------------------------------------------------
def _run_extract_uv_boundary(bpy, contract, job, out_dir, status_path, status,
                             import_topology=None) -> int:
    from uv_agent.blender.uv_extract import extract_mesh_graph_with_uv
    from uv_agent.geometry.uv_boundary import extract_uv_boundary_seams

    obj = _resolve_object(bpy, job.get("object_name"))
    if obj is None:
        err = {"code": "object_not_found",
               "message": f"no mesh object (requested {job.get('object_name')!r})"}
        contract.finalize_status(status, status=contract.STATUS_FAILED, error=err)
        contract.write_json(status_path, status)
        print(f"extract_uv_boundary_as_seams: {err['message']}", file=sys.stderr)
        return 2

    requested_layer = job.get("uv_layer")
    mesh, uvmap, resolved_layer = extract_mesh_graph_with_uv(obj, requested_layer)

    # No UV layer -> first-class no_uv outcome (plan §6.4), not a failure.
    if uvmap is None or resolved_layer is None:
        result = {
            "schema_version": contract.SCHEMA_VERSION,
            "status": contract.STATUS_NO_UV,
            "command": contract.CMD_EXTRACT_UV_BOUNDARY,
            "path": None,
            "object_name": obj.name,
            "uv_layer": requested_layer,
            "import_topology": import_topology,
            "warnings": ["UV layer not found or empty."],
        }
        contract.write_json(os.path.join(out_dir, "boundary_extract_report.json"), result)
        contract.finalize_status(status, status=contract.STATUS_NO_UV, artifacts={})
        contract.write_json(status_path, status)
        print(f"extract_uv_boundary_as_seams: object {obj.name!r} has no UV -> no_uv")
        return 0

    boundary = extract_uv_boundary_seams(mesh, uvmap)
    spec = contract.make_seam_spec(
        object_name=obj.name,
        user_seam_edges=boundary.seam_edges,
        notes=f"Extracted from UV island boundaries: {resolved_layer}",
    )

    out_path = job.get("out_path")
    path_rel = None
    artifacts = {"boundary_report": "boundary_extract_report.json"}
    if out_path:
        contract.write_json(out_path, spec)
        path_rel = job.get("out_path_rel") or os.path.basename(out_path)
        artifacts["boundary_spec"] = path_rel

    result = {
        "schema_version": contract.SCHEMA_VERSION,
        "status": contract.STATUS_ACCEPTED,
        "command": contract.CMD_EXTRACT_UV_BOUNDARY,
        "path": path_rel,
        "object_name": obj.name,
        "uv_layer": resolved_layer,
        "user_seam_count": len(boundary.seam_edges),
        "user_protected_count": 0,
        "spec": spec,
        "import_topology": import_topology,
        "report": boundary.report(),
    }
    contract.write_json(os.path.join(out_dir, "boundary_extract_report.json"), result)
    contract.finalize_status(status, status=contract.STATUS_ACCEPTED, artifacts=artifacts)
    contract.write_json(status_path, status)
    print(
        f"extract_uv_boundary_as_seams: accepted object={obj.name!r} layer={resolved_layer!r} "
        f"boundary_edges={len(boundary.seam_edges)}"
    )
    return 0


# ---------------------------------------------------------------------------
# validate_user_seam_spec (plan §6.3)
# ---------------------------------------------------------------------------
def _edge_count_for_object(bpy, object_name) -> tuple[int | None, str | None]:
    """Return ``(edge_count, resolved_object_name)`` for the selected object."""
    from uv_agent.blender.extract import extract_mesh_graph

    obj = _resolve_object(bpy, object_name)
    if obj is None:
        return None, None
    mesh = extract_mesh_graph(obj)
    return mesh.edge_count, obj.name


def _run_validate_spec(bpy, contract, job, out_dir, status_path, status,
                       import_topology=None) -> int:
    spec = job.get("spec") or {}
    edge_count, resolved_name = _edge_count_for_object(bpy, job.get("object_name"))
    validation = contract.normalize_and_validate_spec(
        spec, edge_count=edge_count, object_name=resolved_name or job.get("object_name"))
    result = {
        "schema_version": contract.SCHEMA_VERSION,
        "status": contract.STATUS_ACCEPTED,
        "command": contract.CMD_VALIDATE_USER_SEAM_SPEC,
        "object_name": resolved_name,
        "import_topology": import_topology,
        "validation": validation,
    }
    contract.write_json(os.path.join(out_dir, "seam_spec_validation.json"), result)
    contract.finalize_status(status, status=contract.STATUS_ACCEPTED,
                             artifacts={"validation": "seam_spec_validation.json"})
    contract.write_json(status_path, status)
    print(f"validate_user_seam_spec: valid={validation['valid']} "
          f"invalid={validation['invalid_edges']} conflicts={len(validation['conflicts'])}")
    return 0


# ---------------------------------------------------------------------------
# load / save (plan §6.1, §6.2) — single-result-file commands (no run folder)
# ---------------------------------------------------------------------------
def _run_load_spec(bpy, contract, job, import_topology=None) -> int:
    out = job.get("out")
    path = job.get("path")
    if not path or not os.path.exists(path):
        if out:
            contract.write_json(out, contract.error_envelope(
                contract.CMD_LOAD_USER_SEAM_SPEC, f"spec not found: {path}", code="spec_missing"))
        print(f"load_user_seam_spec: spec not found: {path}", file=sys.stderr)
        return 2
    spec = contract.read_json(path)
    edge_count, resolved_name = _edge_count_for_object(bpy, job.get("object_name"))
    validation = contract.normalize_and_validate_spec(
        spec, edge_count=edge_count, object_name=resolved_name or job.get("object_name"))
    result = {
        "schema_version": contract.SCHEMA_VERSION,
        "status": contract.STATUS_ACCEPTED,
        "command": contract.CMD_LOAD_USER_SEAM_SPEC,
        "spec": spec,
        "validation": {
            "valid": validation["valid"],
            "invalid_edges": validation["invalid_edges"],
            "conflicts": validation["conflicts"],
            "object_mismatch": validation["object_mismatch"],
        },
        "import_topology": import_topology,
    }
    if out:
        contract.write_json(out, result)
    print(f"load_user_seam_spec: valid={validation['valid']} from {path}")
    return 0


def _run_save_spec(bpy, contract, job, import_topology=None) -> int:
    out = job.get("out")
    out_path = job.get("out_path")
    spec = job.get("spec") or {}
    if not out_path:
        if out:
            contract.write_json(out, contract.error_envelope(
                contract.CMD_SAVE_USER_SEAM_SPEC, "save requires out_path", code="bad_request"))
        print("save_user_seam_spec: requires out_path", file=sys.stderr)
        return 2
    edge_count, resolved_name = _edge_count_for_object(bpy, job.get("object_name"))
    validation = contract.normalize_and_validate_spec(
        spec, edge_count=edge_count, object_name=resolved_name or job.get("object_name"))
    normalized = validation["normalized_spec"]
    contract.write_json(out_path, normalized)
    result = {
        "schema_version": contract.SCHEMA_VERSION,
        "status": contract.STATUS_ACCEPTED,
        "command": contract.CMD_SAVE_USER_SEAM_SPEC,
        "path": job.get("out_path_rel") or os.path.basename(out_path),
        "validation": {
            "valid": validation["valid"],
            "user_seam_count": validation["user_seam_count"],
            "user_protected_count": validation["user_protected_count"],
            "invalid_edges": validation["invalid_edges"],
            "conflicts": validation["conflicts"],
        },
        "import_topology": import_topology,
    }
    if out:
        contract.write_json(out, result)
    print(f"save_user_seam_spec: wrote {out_path} "
          f"seams={validation['user_seam_count']} protected={validation['user_protected_count']}")
    return 0


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
_RUN_FOLDER_COMMANDS = None  # set after contract import


def main() -> int:
    _ensure_importable()
    import app_seam_spec_contract as contract  # type: ignore

    run_folder_commands = {
        contract.CMD_EXPORT_EDGE_GEOMETRY,
        contract.CMD_EXTRACT_UV_BOUNDARY,
        contract.CMD_VALIDATE_USER_SEAM_SPEC,
    }

    opts = _parse_args(sys.argv)
    if "job" not in opts:
        print("seam_editor_worker requires --job /abs/job.json", file=sys.stderr)
        return 2
    job = contract.read_json(opts["job"])
    command = job.get("command")
    if command not in contract.COMMANDS:
        print(f"seam_editor_worker: unknown command {command!r}", file=sys.stderr)
        if job.get("out"):
            contract.write_json(job["out"], contract.error_envelope(
                command or "unknown", f"unknown command {command!r}", code="bad_command"))
        return 2

    model = job.get("model")
    if not model or not os.path.exists(model):
        msg = f"model not found: {model}"
        _emit_pre_open_failure(contract, job, command, run_folder_commands, msg, "model_missing")
        print(f"seam_editor_worker: {msg}", file=sys.stderr)
        return 2

    ext = os.path.splitext(model)[1].lower()
    if ext not in contract.SUPPORTED_MODEL_EXTS:
        msg = f"unsupported format {ext!r}; supported: {', '.join(contract.SUPPORTED_MODEL_EXTS)}"
        _emit_pre_open_failure(contract, job, command, run_folder_commands, msg, "unsupported_format")
        print(f"seam_editor_worker: {msg}", file=sys.stderr)
        return 2

    import bpy  # only available inside Blender

    # --- single-result-file commands (load/save): no run folder -----------
    if command == contract.CMD_LOAD_USER_SEAM_SPEC:
        return _dispatch_simple(bpy, contract, job, command, _run_load_spec)
    if command == contract.CMD_SAVE_USER_SEAM_SPEC:
        return _dispatch_simple(bpy, contract, job, command, _run_save_spec)

    # --- run-folder commands (export/extract/validate) --------------------
    out_dir = job.get("out_dir") or os.path.join("out", job.get("run_id", "seam_run"))
    os.makedirs(out_dir, exist_ok=True)
    status_path = os.path.join(out_dir, "status.json")
    status = contract.new_status(
        run_id=job.get("run_id", "seam_run"), command=command,
        status=contract.STATUS_RUNNING, input=_status_input(job))
    contract.write_json(status_path, status)

    try:
        merge_info = _open_model(bpy, model)
    except Exception as exc:  # noqa: BLE001 - structured import failure
        err = {"code": "import_failed", "message": f"open/import failed: {exc}"}
        contract.finalize_status(status, status=contract.STATUS_FAILED, error=err)
        contract.write_json(status_path, status)
        print(f"seam_editor_worker: open/import failed: {exc}", file=sys.stderr)
        return 3

    # G6/G14: normalize ONCE, before any extract_mesh_graph* reads edge ids.
    import_topology = _normalize_opened_model(
        bpy, model, merge_info, job.get("object_name"))
    _write_import_topology(contract, out_dir, import_topology)
    topology_block = _compact_import_topology(import_topology)

    try:
        if command == contract.CMD_EXPORT_EDGE_GEOMETRY:
            return _run_export_edge_geometry(bpy, contract, job, out_dir, status_path, status,
                                             topology_block)
        if command == contract.CMD_EXTRACT_UV_BOUNDARY:
            return _run_extract_uv_boundary(bpy, contract, job, out_dir, status_path, status,
                                            topology_block)
        if command == contract.CMD_VALIDATE_USER_SEAM_SPEC:
            return _run_validate_spec(bpy, contract, job, out_dir, status_path, status,
                                      topology_block)
        return 2
    except Exception as exc:  # noqa: BLE001 - any failure becomes a structured status
        tb = traceback.format_exc()
        err = {"code": "exception", "message": str(exc), "traceback": tb}
        contract.finalize_status(status, status=contract.STATUS_FAILED, error=err)
        contract.write_json(status_path, status)
        print(f"seam_editor_worker: failed: {exc}\n{tb}", file=sys.stderr)
        return 1


def _dispatch_simple(bpy, contract, job, command, fn) -> int:
    """Open the model and run a single-result-file command (load/save)."""
    try:
        merge_info = _open_model(bpy, job["model"])
    except Exception as exc:  # noqa: BLE001
        if job.get("out"):
            contract.write_json(job["out"], contract.error_envelope(
                command, f"open/import failed: {exc}", code="import_failed"))
        print(f"seam_editor_worker: open/import failed: {exc}", file=sys.stderr)
        return 3

    # G6/G14: normalize ONCE, before the spec is validated against mesh edge ids.
    import_topology = _normalize_opened_model(
        bpy, job["model"], merge_info, job.get("object_name"))
    out = job.get("out")
    _write_import_topology(contract, os.path.dirname(os.path.abspath(out)) if out else None,
                           import_topology)
    try:
        return fn(bpy, contract, job, _compact_import_topology(import_topology))
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        if job.get("out"):
            contract.write_json(job["out"], contract.error_envelope(
                command, str(exc), code="exception", traceback=tb))
        print(f"seam_editor_worker: failed: {exc}\n{tb}", file=sys.stderr)
        return 1


def _emit_pre_open_failure(contract, job, command, run_folder_commands, msg, code) -> None:
    """Write a structured failure for an error detected before opening Blender."""
    if command in run_folder_commands and job.get("out_dir"):
        out_dir = job["out_dir"]
        os.makedirs(out_dir, exist_ok=True)
        status = contract.new_status(
            run_id=job.get("run_id", "seam_run"), command=command,
            status=contract.STATUS_FAILED, input=_status_input(job))
        contract.finalize_status(status, status=contract.STATUS_FAILED,
                                 error={"code": code, "message": msg})
        contract.write_json(os.path.join(out_dir, "status.json"), status)
    elif job.get("out"):
        contract.write_json(job["out"], contract.error_envelope(command, msg, code=code))


if __name__ == "__main__":
    raise SystemExit(main())
