"""App-facing production export worker (Electron MVP 5 plan §5, §7, §11; Sessions B–C).

Run inside Blender:

    blender --background --python worker/export_production_asset.py -- --job /abs/job.json

``job.json`` carries ``command: "export_production_asset"`` (plan §5.1):

1. open the MVP 3 accepted ``selected_uv_model`` (``work/uv/selected_uv.blend``),
2. resolve the object + activate the selected UV layer (plan §5.1 options),
3. apply export options on a DUPLICATE object so the source blend is never
   mutated (plan §11, §15), and export each requested FBX/OBJ/GLB/GLTF,
4. re-open every exported file in a fresh scene and validate UV presence +
   face/vertex/normal snapshot (plan §7); a missing UV layer is a hard failure,
   then re-AUDIT the same file against the source mesh/UV snapshot (Gate G13:
   correctness, texel density, shading policy, source-matched mandatory-90 and
   UV/vertex fingerprints) into ``export_reread_report.json`` — a format that
   fails the audit does not count as shipped,
5. render best-effort UV-layout + checker previews of the exported result (plan §7),
6. write ``export_manifest.json`` (accepted/partial), ``validation_report.json``
   and the ``status.json`` lifecycle (plan §6, §7).

Hard rules (plan §11, §15, §16): NEVER save back into ``selected_uv_model``,
NEVER mutate ``working_model`` / ``user_seam_spec``, NEVER delete prior exports.
At least one format that exports AND validates -> ``accepted`` (all) / ``partial``
(some); none -> ``failed``. Every exit leaves a structured JSON status so the app
never parses stdout (plan §5).
"""

from __future__ import annotations

import os
import sys
import traceback


def _parse_args(argv: list[str]) -> dict:
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
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
    """Open the selected UV ``.blend`` (or import a model) into a fresh scene (G1/G14).

    Non-``.blend`` input goes through the ONE shared importer
    (:func:`uv_agent.blender.topology_normalize.import_model`) so the glTF
    ``merge_vertices=True`` policy is identical on every entry point. Returns the
    importer's ``merge_info``; callers that ignore it keep working.
    """
    _ensure_importable()
    from uv_agent.blender.topology_normalize import import_model

    ext = os.path.splitext(path)[1].lower()
    if ext == ".blend":
        bpy.ops.wm.open_mainfile(filepath=os.path.abspath(path))
        return {"format": "blend", "merge_vertices_requested": False,
                "merge_vertices_supported": None, "merge_vertices_enabled": False}
    try:
        bpy.ops.wm.read_homefile(use_empty=True)
    except Exception:  # noqa: BLE001 - best-effort; default scene is acceptable
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o, do_unlink=True)
    return import_model(bpy, path, merge_vertices=True)


def _status_input(job: dict) -> dict:
    return {
        "selected_uv_model": job.get("selected_uv_model_rel") or job.get("selected_uv_model"),
        "object_name": job.get("object_name"),
        "formats": job.get("formats", []),
    }


def _export_rel(job: dict, filename: str) -> str:
    """Project-relative path of an exported file (plan §5.1 ``exports`` block)."""
    base = job.get("out_dir_rel") or os.path.join("exports", job.get("export_id", "export"))
    return os.path.join(base, filename)


# ---------------------------------------------------------------------------
# Previews (plan §7 step 7 — best-effort UV layout + checker of the export)
# ---------------------------------------------------------------------------
def _render_previews(obj, out_dir: str, *, render_size: int, texture_size: int,
                     checker_scale: float) -> list[str]:
    """Render UV-layout + checker previews of the exported object (plan §7).

    Best-effort: a failing render becomes a warning, not a failure. Runs on the
    in-memory source object (already holding the selected UVs) BEFORE validation
    re-opens reset the scene. The checker material is applied in this ephemeral
    process only and is never saved (plan §15)."""
    from chart_uv_agent.unwrap import read_uvmap
    from uv_agent.blender.extract import extract_mesh_graph
    from uv_agent.blender.review_render import render_checker_views
    from uv_agent.geometry.uv_review import write_uv_layout_png

    warnings: list[str] = []
    try:
        mesh = extract_mesh_graph(obj)
        uvmap = read_uvmap(obj, mesh)
        write_uv_layout_png(mesh, uvmap, os.path.join(out_dir, "uv_layout.png"), size=texture_size)
    except Exception as exc:  # noqa: BLE001 - layout is best-effort (plan §7)
        warnings.append(f"uv_layout.png render failed: {exc}")

    try:
        checker = render_checker_views(
            obj, out_dir, scale=checker_scale, size=render_size,
            filenames={"front": "checker_front.png", "side": "checker_side.png"})
        for view in ("front", "side"):
            if view not in checker:
                warnings.append(f"checker_{view}.png render failed")
    except Exception as exc:  # noqa: BLE001 - checker is best-effort (plan §7)
        warnings.append(f"checker render failed: {exc}")
    return warnings


# ---------------------------------------------------------------------------
# check_export_readiness (plan §4 — Blender-backed readiness)
# ---------------------------------------------------------------------------
def _run_readiness(bpy, contract, job: dict, out_dir: str) -> int:
    """Build the readiness result from the selected UV summary + a reopen probe.

    The Electron app computes readiness in pure Node for speed, but the worker
    command verifies the same checks AND that the ``selected_uv_model`` actually
    re-opens in Blender (a plan §0 entry condition)."""
    model = job.get("selected_uv_model")
    summary_path = job.get("selected_uv_summary")
    summary = contract.read_json_optional(summary_path) if summary_path else None
    model_exists = bool(model and os.path.exists(model))
    summary_exists = summary is not None

    warnings: list[str] = []
    if model_exists:
        try:
            _open_model(bpy, model)
        except Exception as exc:  # noqa: BLE001 - reopen failure is a blocking fact
            model_exists = False
            warnings.append(f"selected UV model failed to re-open: {exc}")

    checks = contract.readiness_checks_from_summary(
        summary, model_exists=model_exists, summary_exists=summary_exists)
    out = contract.build_readiness(
        checks,
        selected_uv_model=job.get("selected_uv_model_rel") or model,
        source_uv_run_id=job.get("uv_generate_run_id"),
        warnings=warnings)
    contract.write_json(os.path.join(out_dir, "readiness.json"), out)
    if job.get("out"):
        contract.write_json(job["out"], out)
    print(f"check_export_readiness: status={out['status']} ready={out['ready']} "
          f"blocking={[i['code'] for i in out['blocking_issues']]}", flush=True)
    return 0


# ---------------------------------------------------------------------------
# export_production_asset (plan §5.1, §7, §11)
# ---------------------------------------------------------------------------
def _run_export(bpy, contract, job: dict, out_dir: str, status_path: str, status: dict) -> int:
    from uv_agent.blender import export as exporter
    from uv_agent.blender import export_validation as validation

    export_id = job.get("export_id", "export")
    options = contract.merge_options(job.get("options"))
    formats = contract.normalize_formats(job.get("formats"))

    def _fail(code: str, message: str, **details) -> int:
        err = {"code": code, "message": message}
        if details:
            err["details"] = details
        contract.finalize_status(status, status=contract.STATUS_FAILED, error=err)
        contract.write_json(status_path, status)
        print(f"export_production_asset: {message}", file=sys.stderr)
        return 2

    if not formats:
        return _fail("no_formats", "no supported export formats requested "
                     f"(got {job.get('formats')!r}; supported {contract.SUPPORTED_FORMATS})")

    # --- selected UV summary (metrics + selected candidate, plan §6) ------
    summary = contract.read_json_optional(job.get("selected_uv_summary"))
    metrics = (summary or {}).get("metrics") or {}
    selected_candidate_id = job.get("selected_candidate_id") or (summary or {}).get("selected_candidate_id")

    # --- resolve object + activate the selected UV layer ------------------
    obj = exporter.resolve_export_object(bpy, job.get("object_name"))
    if obj is None:
        return _fail("object_not_found",
                     f"no mesh object to export (requested {job.get('object_name')!r})")
    # Capture the name now — re-open validation (read_homefile) later wipes the
    # scene, leaving ``obj`` a dangling StructRNA we must not touch afterwards.
    object_name = obj.name
    active_uv, uv_warnings = exporter.set_active_uv_layer(obj, options.get("selected_uv_layer"))
    if active_uv is None:
        return _fail("no_uv_layer",
                     "selected UV model has no UV layer to export (re-run MVP 3)")
    # Ship exactly ONE UV layer: FBX/glTF importers keep every layer and re-activate
    # the FIRST one, so a leftover original ``UVMap`` next to ``AI_UV`` makes the
    # re-read (and any DCC consumer) read the WRONG map (G9 re-read audit). This
    # mutates the in-memory mesh only — this worker NEVER saves back into
    # ``selected_uv_model`` (plan §11, §15), so the accepted asset is untouched.
    removed_uv_layers = exporter.make_uv_layer_exclusive(obj, active_uv)
    source_faces = len(obj.data.polygons)
    source_vertices = len(obj.data.vertices)
    triangulated = bool(options.get("triangulate", False))
    warnings: list[str] = list(uv_warnings)
    if removed_uv_layers:
        warnings.append(
            "removed non-exported UV layers so the export ships only "
            f"{active_uv!r}: {removed_uv_layers}")

    print(f"export_production_asset: removed_uv_layers={removed_uv_layers}", flush=True)
    print(f"export_production_asset: object={object_name!r} uv_layer={active_uv!r} "
          f"formats={formats} faces={source_faces} verts={source_vertices} "
          f"apply_scale={options.get('apply_scale')} triangulate={triangulated} "
          f"materials={options.get('include_materials')} normals={options.get('include_normals')}",
          flush=True)

    # --- SOURCE snapshot for the G13 re-read audit (before the duplicate) -
    # The re-read audit compares the shipped file against the mesh + UV that was
    # actually exported, so it must be captured here: the export duplicate is
    # modified (scale/triangulate) and every re-open below wipes the scene.
    source_mesh = source_uvmap = None
    reread_profile: dict = {}
    profile_id = None
    try:
        from chart_uv_agent.quality_profile import load_quality_profile
        from uv_agent.blender.extract import extract_mesh_graph
        from uv_agent.blender.organic_unwrap import read_uvmap

        source_mesh = extract_mesh_graph(obj)
        source_uvmap = read_uvmap(obj, source_mesh, layer_name=active_uv)
        # Frozen profile defaults, overridden by whatever the accepted MVP 3 run
        # recorded — the audit must judge the export by the profile it was made
        # under, not by today's defaults.
        reread_profile = dict(load_quality_profile(None).to_dict())
        reread_profile.update({k: v for k, v in ((summary or {}).get("quality_profile") or {}).items()
                               if v is not None})
        profile_id = reread_profile.get("profile_id")
    except Exception as exc:  # noqa: BLE001 - the audit is evidence, never a crash
        warnings.append(f"re-read audit source snapshot failed: {exc}")

    # --- export each format from a DUPLICATE (source never mutated) -------
    dup = exporter.build_export_object(bpy, obj, options)
    export_results: dict[str, dict] = {}
    name_files: dict[str, str] = {}
    try:
        for fmt in formats:
            filename = contract.export_filename(options.get("export_name"), object_name, fmt)
            name_files[fmt] = filename
            res = exporter.export_one(bpy, fmt, os.path.join(out_dir, filename), options)
            export_results[fmt] = res
            print(f"export_production_asset: {fmt} -> {'ok' if res['ok'] else res['error']['message']}",
                  flush=True)
    finally:
        exporter.cleanup_export_object(bpy, dup)

    # --- previews of the exported result (best-effort, BEFORE validation) -
    if bool(options.get("render_previews", True)):
        warnings += _render_previews(
            obj, out_dir,
            render_size=int(options.get("render_size_px", 900)),
            texture_size=int(options.get("texture_size_px", 1024)),
            checker_scale=float(options.get("checker_scale", 40.0)))

    # --- re-open validation (resets the scene per file, plan §7) ----------
    validation_formats: dict[str, dict] = {}
    reread_formats: dict[str, dict] = {}
    for fmt, res in export_results.items():
        if not res["ok"]:
            continue
        validation_formats[fmt] = validation.reopen_and_validate(
            bpy, res["path"], fmt, expected_uv_layer=active_uv,
            include_normals=bool(options.get("include_normals", True)),
            source_faces=source_faces, source_vertices=source_vertices,
            triangulated=triangulated)
        # G13: same place, same scene-reset cost — re-read the shipped file and
        # audit it against the source snapshot (never raises).
        if source_mesh is not None and source_uvmap is not None:
            reread_formats[fmt] = validation.reread_audit(
                bpy, res["path"], fmt, source_mesh=source_mesh,
                source_uvmap=source_uvmap, profile=reread_profile,
                triangulated=triangulated, expected_uv_layer=active_uv)
            block = reread_formats[fmt]
            print(f"export_production_asset: reread {fmt} passed={block.get('passed')} "
                  f"failures={block.get('failures')}", flush=True)

    # --- re-read report (Gate G13 / G15 evidence; always written) ---------
    reread_report = contract.build_reread_report(reread_formats, profile_id=profile_id)
    contract.write_json(os.path.join(out_dir, contract.EXPORT_REREAD_REPORT_FILE), reread_report)

    # --- status: a format must export AND validate to count (plan §5, §7) -
    succeeded = [f for f in formats
                 if export_results[f]["ok"]
                 and contract.format_validation_ok(validation_formats.get(f),
                                                   reread=reread_formats.get(f))]
    failed_formats: list[dict] = []
    for fmt in formats:
        res = export_results[fmt]
        if not res["ok"]:
            failed_formats.append({"format": fmt, **res["error"]})
        elif not contract.format_validation_ok(validation_formats.get(fmt)):
            failed_formats.append({"format": fmt, "code": "validation_failed",
                                   "message": f"{fmt.upper()} re-opened without a UV layer"})
        elif not contract.format_validation_ok(validation_formats.get(fmt),
                                               reread=reread_formats.get(fmt)):
            block = reread_formats.get(fmt) or {}
            failed_formats.append({"format": fmt, "code": "reread_audit_failed",
                                   "message": f"{fmt.upper()} re-read audit failed: "
                                              f"{block.get('failures') or block.get('error')}",
                                   "failures": list(block.get("failures") or [])})
    for fmt, v in validation_formats.items():
        warnings += [w for w in v.get("warnings", []) if "hard failure" not in w]

    run_status = contract.classify_export_status(formats, succeeded)
    for ff in failed_formats:
        warnings.append(f"{ff['format']} export failed: {ff.get('message', ff.get('code'))}")

    # --- validation report (always; plan §7) ------------------------------
    validation_report = contract.build_validation_report(validation_formats)
    contract.write_json(os.path.join(out_dir, contract.VALIDATION_REPORT_FILE), validation_report)

    exports = {f: _export_rel(job, name_files[f]) for f in succeeded}

    # --- manifest (accepted / partial only, plan §6, §14) -----------------
    artifacts, art_warnings = contract.collect_export_artifacts(out_dir)
    warnings = art_warnings + warnings
    manifest_written = None
    if run_status in contract.SHIPPED_STATUSES:
        manifest_source = contract.build_manifest_source(
            selected_uv_model=job.get("selected_uv_model_rel"),
            selected_uv_summary=job.get("selected_uv_summary_rel"),
            uv_generate_run_id=job.get("uv_generate_run_id"),
            active_user_seam_spec=job.get("seam_spec_rel"),
            candidate_summary=job.get("candidate_summary_rel"),
            p5_gate=job.get("p5_gate_rel"), seam_report=job.get("seam_report_rel"))
        files = contract.collect_export_files(out_dir, exports, {f: name_files[f] for f in succeeded})
        manifest = contract.build_export_manifest(
            export_id=export_id, created_at=contract.utc_now_iso(), status=run_status,
            formats=succeeded, options=options, source=manifest_source,
            metrics=metrics, files=files)
        contract.write_json(os.path.join(out_dir, contract.MANIFEST_FILE), manifest)
        manifest_written = contract.MANIFEST_FILE
    else:
        # No manifest for an all-fail export; keep the artifact key out of the way.
        artifacts.pop("manifest", None)

    # --- structured result + status (plan §5.1) ---------------------------
    result_source = contract.build_result_source(
        selected_uv_model=job.get("selected_uv_model_rel"),
        selected_uv_summary=job.get("selected_uv_summary_rel"),
        uv_generate_run_id=job.get("uv_generate_run_id"), seam_spec=job.get("seam_spec_rel"),
        selected_candidate_id=selected_candidate_id)
    result = contract.build_export_result(
        export_id=export_id, status=run_status, source=result_source, exports=exports,
        validation=validation_report, artifacts=artifacts,
        failed_formats=failed_formats or None, warnings=warnings,
        reread=reread_report)
    if job.get("out"):
        contract.write_json(job["out"], result)

    error = None
    if run_status == contract.STATUS_FAILED:
        error = {"code": "all_formats_failed",
                 "message": "every requested format failed to export or validate",
                 "failed_formats": failed_formats}
    contract.finalize_status(status, status=run_status, artifacts=artifacts, error=error)
    contract.write_json(status_path, status)
    print(f"export_production_asset: {run_status} export={export_id} object={object_name!r} "
          f"succeeded={succeeded} failed={[f['format'] for f in failed_formats]} "
          f"manifest={manifest_written} validation={validation_report['status']} "
          f"reread_passed={reread_report['passed']} "
          f"reread_failed={reread_report['failed_formats']}", flush=True)
    # partial/failed are product outcomes — the verdict lives in status.json, so a
    # completed export attempt still exits 0 (plan §5).
    return 0


def main() -> int:
    _ensure_importable()
    import app_export_contract as contract  # type: ignore

    opts = _parse_args(sys.argv)
    if "job" not in opts:
        print("export_production_asset requires --job /abs/job.json", file=sys.stderr)
        return 2
    job = contract.read_json(opts["job"])
    command = job.get("command", contract.CMD_EXPORT_PRODUCTION_ASSET)

    out_dir = job.get("out_dir") or os.path.join("out", job.get("export_id", "export"))
    os.makedirs(out_dir, exist_ok=True)

    import bpy  # only available inside Blender

    if command == contract.CMD_CHECK_EXPORT_READINESS:
        try:
            return _run_readiness(bpy, contract, job, out_dir)
        except Exception as exc:  # noqa: BLE001 - readiness must not crash
            tb = traceback.format_exc()
            if job.get("out"):
                contract.write_json(job["out"], contract.error_envelope(
                    command, str(exc), code="exception", traceback=tb))
            print(f"check_export_readiness: failed: {exc}\n{tb}", file=sys.stderr)
            return 1

    if command != contract.CMD_EXPORT_PRODUCTION_ASSET:
        print(f"export_production_asset: unsupported command {command!r}", file=sys.stderr)
        if job.get("out"):
            contract.write_json(job["out"], contract.error_envelope(
                command or "unknown", f"unsupported command {command!r}", code="bad_command"))
        return 2

    status_path = os.path.join(out_dir, contract.STATUS_FILE)
    status = contract.new_status(
        export_id=job.get("export_id", "export"), command=command,
        status=contract.STATUS_RUNNING, input=_status_input(job))
    contract.write_json(status_path, status)

    model = job.get("selected_uv_model")
    if not model or not os.path.exists(model):
        contract.finalize_status(status, status=contract.STATUS_FAILED, error={
            "code": "missing_selected_uv_model",
            "message": f"selected UV model not found: {model}"})
        contract.write_json(status_path, status)
        print(f"export_production_asset: selected UV model not found: {model}", file=sys.stderr)
        return 2

    try:
        _open_model(bpy, model)
    except Exception as exc:  # noqa: BLE001 - structured open failure
        contract.finalize_status(status, status=contract.STATUS_FAILED, error={
            "code": "open_failed", "message": f"selected UV model failed to open: {exc}"})
        contract.write_json(status_path, status)
        print(f"export_production_asset: open failed: {exc}", file=sys.stderr)
        return 3

    try:
        return _run_export(bpy, contract, job, out_dir, status_path, status)
    except Exception as exc:  # noqa: BLE001 - any failure becomes a structured status
        tb = traceback.format_exc()
        contract.finalize_status(status, status=contract.STATUS_FAILED,
                                 error={"code": "exception", "message": str(exc), "traceback": tb})
        contract.write_json(status_path, status)
        print(f"export_production_asset: failed: {exc}\n{tb}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
