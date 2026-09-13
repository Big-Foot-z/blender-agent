"""App-facing UV review worker (MVP 1 plan §5.1, §5.2, Session B/C).

Run inside Blender:

    blender --background --python worker/review_existing_uv.py -- --job /abs/job.json

``job.json`` carries a ``command`` field selecting the operation:

- ``inspect_uv_layers`` — open/import the model, summarize every mesh object and
  its UV layers, and write the inspect contract JSON to ``job["out"]``.
- ``review_existing_uv`` — read the chosen (or active) UV layer of one object,
  compute the MVP 1 metrics, render the layout + checker artifacts, and write the
  run-folder contract (``status.json`` / ``uv_review_summary.json`` / per-metric
  JSON) into ``job["out_dir"]``.

MVP 1 is read-only: this worker never marks seams, edits UVs, or saves the model
(plan §1, §14). Every exit — accepted, no_uv, or failed — leaves a structured JSON
result so the app never parses stdout (plan §3, §4). An object with no UV layer is
``status: no_uv``, not a failure (plan §4).
"""

from __future__ import annotations

import os
import sys
import traceback


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
    """Open a ``.blend`` or import a model into a fresh scene (plan §5.2; G1/G14).

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
    # Start from an empty scene so only the imported model is summarized/reviewed.
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

    One normalized topology = one fingerprint / edge-id space across every UV-path
    worker, so this runs on the same resolved object the review will measure. Never
    raises: an unevaluatable normalization is recorded as ``{"error": ...}`` and the
    worker keeps going (the contract output still gets written).
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
        print(f"review_existing_uv: topology normalization failed: {exc}", file=sys.stderr)
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
        print(f"review_existing_uv: import_topology write failed: {exc}", file=sys.stderr)


def _model_label(job: dict) -> str | None:
    """Project-relative model path for the summary (plan §5.2 ``model``)."""
    rel = job.get("model_rel")
    if rel:
        return rel
    model = job.get("model")
    return os.path.basename(model) if model else None


# ---------------------------------------------------------------------------
# inspect_uv_layers (plan §5.1)
# ---------------------------------------------------------------------------
def _run_inspect(bpy, contract, job: dict, import_topology: dict | None = None) -> int:
    from uv_agent.blender.uv_extract import object_uv_summary

    out_path = job.get("out")
    if not out_path:
        print("inspect_uv_layers requires --job with an 'out' path", file=sys.stderr)
        return 2

    _write_import_topology(contract, os.path.dirname(os.path.abspath(out_path)),
                           import_topology)

    objects = [object_uv_summary(o) for o in _mesh_objects(bpy)]
    any_uv = any(o["has_uv"] for o in objects)
    result = {
        "schema_version": contract.SCHEMA_VERSION,
        "status": contract.STATUS_ACCEPTED if any_uv else contract.STATUS_NO_UV,
        "command": contract.CMD_INSPECT_UV_LAYERS,
        "project_id": job.get("project_id"),
        "model": _model_label(job),
        "objects": objects,
        "import_topology": _compact_import_topology(import_topology),
        "recommended_next_step": (
            contract.NEXT_REVIEW_EXISTING_UV if any_uv
            else contract.NEXT_OPEN_SEAM_OR_GENERATE
        ),
        "warnings": [] if objects else ["no mesh objects found in model"],
    }
    contract.write_json(out_path, result)
    print(f"inspect_uv_layers: {len(objects)} object(s), any_uv={any_uv} -> {out_path}")
    return 0


# ---------------------------------------------------------------------------
# review_existing_uv (plan §5.2, §6, §7)
# ---------------------------------------------------------------------------
def _run_review(bpy, contract, job: dict, status_path: str, status: dict,
                import_topology: dict | None = None) -> int:
    from uv_agent.blender.review_render import render_checker_views
    from uv_agent.blender.uv_extract import extract_mesh_graph_with_uv, list_uv_layers
    from uv_agent.geometry.uv_review import (
        compute_uv_review, uv_layout_svg, write_uv_layout_png,
    )

    out_dir = job["out_dir"]
    run_id = job.get("run_id", "review_run")
    requested_object = job.get("object_name")
    requested_layer = job.get("uv_layer")
    options = job.get("options") or {}
    model_label = _model_label(job)

    # G6/G14: the normalization evidence lands in the run folder before anything
    # can fail; the mesh below is extracted from the ALREADY normalized object.
    _write_import_topology(contract, out_dir, import_topology)
    topology_block = _compact_import_topology(import_topology)

    obj = _resolve_object(bpy, requested_object)
    if obj is None:
        err = {"code": "object_not_found",
               "message": f"no mesh object to review (requested {requested_object!r})"}
        contract.finalize_status(status, status=contract.STATUS_FAILED, error=err)
        contract.write_json(status_path, status)
        print(f"review_existing_uv: {err['message']}", file=sys.stderr)
        return 2

    # A specifically-requested layer that doesn't exist is a structured error
    # (plan §13 "UV layer name이 잘못되면 structured error"). A missing/empty layer
    # set is the no_uv outcome, not an error.
    layer_names = [lyr.name for lyr in obj.data.uv_layers]
    if requested_layer and layer_names and requested_layer not in layer_names:
        err = {"code": "uv_layer_not_found",
               "message": f"UV layer {requested_layer!r} not found on {obj.name!r}; "
                          f"available: {layer_names}"}
        contract.finalize_status(status, status=contract.STATUS_FAILED, error=err)
        contract.write_json(status_path, status)
        print(f"review_existing_uv: {err['message']}", file=sys.stderr)
        return 2

    mesh, uvmap, resolved_layer = extract_mesh_graph_with_uv(obj, requested_layer)

    # No UV layer -> first-class no_uv outcome (plan §4), not a failure.
    if uvmap is None or resolved_layer is None:
        summary = contract.no_uv_summary(
            run_id=run_id, model=model_label, object_name=obj.name)
        summary["import_topology"] = topology_block
        contract.write_json(os.path.join(out_dir, "uv_review_summary.json"), summary)
        contract.write_json(os.path.join(out_dir, "uv_layers.json"), list_uv_layers(obj))
        contract.finalize_status(status, status=contract.STATUS_NO_UV, artifacts={})
        contract.write_json(status_path, status)
        print(f"review_existing_uv: object {obj.name!r} has no UV layer -> no_uv")
        return 0

    # --- metrics (plan §6) -------------------------------------------------
    review = compute_uv_review(
        mesh, uvmap,
        raster_resolution=int(options.get("raster_overlap_resolution", 1024)),
    )
    metrics, uv_block, islands = review["metrics"], review["uv"], review["islands"]
    review_status, issues = contract.classify_review(metrics, uv_block)
    mesh_block = {
        "vertices": mesh.vertex_count,
        "edges": mesh.edge_count,
        "faces": mesh.face_count,
        "loops": len(mesh.loops),
    }

    # Per-metric detail files (debug tabs / heatmaps).
    contract.write_json(os.path.join(out_dir, "uv_metrics.json"), {
        "schema_version": contract.SCHEMA_VERSION,
        "run_id": run_id,
        "object_name": obj.name,
        "uv_layer": resolved_layer,
        "metrics": metrics,
        "uv": uv_block,
        "review_status": review_status,
        "issues": issues,
        "islands": islands,
    })
    contract.write_json(os.path.join(out_dir, "uv_layers.json"), list_uv_layers(obj))
    contract.write_json(os.path.join(out_dir, "uv_bounds.json"), uv_block["uv_bounds"])

    # --- image artifacts (plan §7, best-effort) ---------------------------
    warnings: list[str] = []

    # uv_layout.png is rasterized headlessly (NumPy), NOT via uv.export_layout,
    # which needs a GPU unavailable under --background (plan §7, §13). The SVG is
    # an additional vector artifact. Both share the loop-UV island recovery, so the
    # layout exactly matches the metrics.
    try:
        write_uv_layout_png(
            mesh, uvmap, os.path.join(out_dir, "uv_layout.png"),
            size=int(options.get("texture_size_px", 1024)),
        )
    except Exception as exc:  # noqa: BLE001 - layout is best-effort (plan §7)
        warnings.append(f"uv_layout.png render failed: {exc}")
    try:
        with open(os.path.join(out_dir, "uv_layout.svg"), "w", encoding="utf-8") as fh:
            fh.write(uv_layout_svg(mesh, uvmap, title=f"{obj.name} · {resolved_layer}"))
    except Exception as exc:  # noqa: BLE001 - svg is a bonus
        warnings.append(f"uv_layout.svg failed: {exc}")

    checker = render_checker_views(
        obj, out_dir,
        scale=float(options.get("checker_scale", 40.0)),
        size=int(options.get("render_size_px", 900)),
        make_3q=bool(options.get("make_3q", False)),
    )
    for required_view in ("front", "side"):
        if required_view not in checker:
            warnings.append(f"checker_{required_view}.png render failed")

    artifacts, artifact_warnings = contract.collect_review_artifacts(out_dir)
    warnings.extend(artifact_warnings)

    summary = contract.build_review_summary(
        run_id=run_id,
        model=model_label,
        object_name=obj.name,
        uv_layer=resolved_layer,
        mesh=mesh_block,
        uv=uv_block,
        metrics=metrics,
        artifacts=artifacts,
        review_status=review_status,
        issues=issues,
        warnings=warnings,
    )
    summary["import_topology"] = topology_block
    contract.write_json(os.path.join(out_dir, "uv_review_summary.json"), summary)

    contract.finalize_status(status, status=contract.STATUS_ACCEPTED, artifacts=artifacts)
    contract.write_json(status_path, status)
    print(
        f"review_existing_uv: accepted run={run_id} object={obj.name!r} "
        f"layer={resolved_layer!r} review_status={review_status} islands={uv_block['island_count']}"
    )
    return 0


def main() -> int:
    _ensure_importable()
    import app_uv_review_contract as contract  # type: ignore

    opts = _parse_args(sys.argv)
    if "job" not in opts:
        print("review_existing_uv requires --job /abs/job.json", file=sys.stderr)
        return 2
    job = contract.read_json(opts["job"])
    command = job.get("command", contract.CMD_REVIEW_EXISTING_UV)

    model = job.get("model")
    if not model or not os.path.exists(model):
        msg = f"model not found: {model}"
        out = job.get("out")
        if command == contract.CMD_REVIEW_EXISTING_UV and job.get("out_dir"):
            out_dir = job["out_dir"]
            os.makedirs(out_dir, exist_ok=True)
            status = contract.new_status(
                run_id=job.get("run_id", "review_run"),
                command=command,
                status=contract.STATUS_FAILED,
                input={"model": _model_label(job), "object_name": job.get("object_name"),
                       "uv_layer": job.get("uv_layer")},
            )
            contract.finalize_status(status, status=contract.STATUS_FAILED,
                                     error={"code": "model_missing", "message": msg})
            contract.write_json(os.path.join(out_dir, "status.json"), status)
        elif out:
            contract.write_json(out, contract.error_envelope(
                command, msg, code="model_missing", project_id=job.get("project_id")))
        print(f"review_existing_uv: {msg}", file=sys.stderr)
        return 2

    ext = os.path.splitext(model)[1].lower()
    if ext not in contract.SUPPORTED_REVIEW_EXTS:
        msg = f"unsupported format {ext!r}; supported: {', '.join(contract.SUPPORTED_REVIEW_EXTS)}"
        if command == contract.CMD_INSPECT_UV_LAYERS and job.get("out"):
            contract.write_json(job["out"], contract.error_envelope(
                command, msg, code="unsupported_format", project_id=job.get("project_id")))
        print(f"review_existing_uv: {msg}", file=sys.stderr)
        return 2

    import bpy  # only available inside Blender

    # --- inspect path: write result to job["out"], no run folder ----------
    if command == contract.CMD_INSPECT_UV_LAYERS:
        try:
            merge_info = _open_model(bpy, model)
        except Exception as exc:  # noqa: BLE001 - structured import failure (plan §10)
            if job.get("out"):
                contract.write_json(job["out"], contract.error_envelope(
                    command, f"open/import failed: {exc}", code="import_failed",
                    project_id=job.get("project_id"), model=_model_label(job)))
            print(f"inspect_uv_layers: open/import failed: {exc}", file=sys.stderr)
            return 3
        import_topology = _normalize_opened_model(
            bpy, model, merge_info, job.get("object_name"))
        return _run_inspect(bpy, contract, job, import_topology)

    # --- review path: full run-folder lifecycle ---------------------------
    out_dir = job.get("out_dir") or os.path.join("out", job.get("run_id", "review_run"))
    os.makedirs(out_dir, exist_ok=True)
    status_path = os.path.join(out_dir, "status.json")
    status = contract.new_status(
        run_id=job.get("run_id", "review_run"),
        command=contract.CMD_REVIEW_EXISTING_UV,
        status=contract.STATUS_RUNNING,
        input={"model": _model_label(job), "object_name": job.get("object_name"),
               "uv_layer": job.get("uv_layer")},
    )
    contract.write_json(status_path, status)

    try:
        merge_info = _open_model(bpy, model)
        import_topology = _normalize_opened_model(
            bpy, model, merge_info, job.get("object_name"))
        return _run_review(bpy, contract, job, status_path, status, import_topology)
    except Exception as exc:  # noqa: BLE001 - any failure becomes a structured status
        tb = traceback.format_exc()
        err = {"code": "exception", "message": str(exc), "traceback": tb}
        contract.finalize_status(status, status=contract.STATUS_FAILED, error=err)
        contract.write_json(status_path, status)
        print(f"review_existing_uv: failed: {exc}\n{tb}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
