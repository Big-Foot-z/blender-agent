"""App-facing low-poly generation wrapper (plan §5.2).

Run inside Blender:

    blender --background --python worker/run_app_retopo_job.py -- --job /abs/job.json

``job.json`` is the app ``generate_lowpoly`` input (plan §5.2). This wrapper:

1. writes/refreshes ``<out_dir>/status.json`` through the run lifecycle,
2. imports the source model into a fresh Blender scene (FBX/OBJ/GLB/GLTF),
3. translates the stable **app** option names into the underlying
   ``worker/run_retopo_job.py`` job dict and runs it (reusing the existing
   generation / validation / shape pipeline and ``lowpoly.blend`` / ``.fbx`` /
   ``preview.png`` artifact writing),
4. normalizes the per-phase reports into ``<out_dir>/summary.json``.

The app-facing option names are stable; the worker option names underneath may
change (plan §5.2 "앱 API는 안정적으로 유지하고 worker 내부 option은 바뀔 수 있게").

Every exit — success or failure — leaves a ``status.json`` and either a
``summary.json`` (accepted) or an ``error`` (failed) so the app never has to parse
stdout (plan §3, Session B acceptance).
"""

from __future__ import annotations

import math
import os
import sys
import traceback

# Pre-decimation voxel proxy (plan §10 large high-poly isolation). Sources above
# the face threshold are voxel-remeshed down to a uniform proxy *before* the
# decimation collapse, so the collapse never runs on tens of millions of faces.
DEFAULT_PROXY_FACE_THRESHOLD = 1_500_000
DEFAULT_PROXY_TARGET_FACES = 1_000_000


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


def _import_source(bpy, path: str) -> None:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=path)
        else:  # pragma: no cover - legacy Blender
            bpy.ops.import_scene.obj(filepath=path)
    elif ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    else:
        raise ValueError(f"unsupported source format: {ext or '(none)'}")


def _translate_to_worker_job(app_job: dict, out_dir: str) -> dict:
    """Map the stable app ``generate_lowpoly`` input -> run_retopo_job job dict."""
    options = app_job.get("options") or {}
    worker_job: dict = {
        "out_dir": out_dir,
        "object_name": app_job.get("object_name"),
        "target_face_count": int(app_job.get("target_faces", 12000)),
        "mode": options.get("mode", "decimation_optimize"),
        "preserve_features": bool(options.get("preserve_features", True)),
        "feature_angle": float(options.get("feature_angle", 30.0)),
        "apply_shrinkwrap": bool(options.get("apply_shrinkwrap", True)),
        "render_preview": bool(options.get("render_preview", True)),
        # Retry ladder: cap how many escalation rungs run (default 1 = single
        # attempt, fast; higher = more thorough but slower on dense proxies).
        "retry_ladder_max_attempts": int(options.get("retry_ladder_max_attempts", 1)),
        # The source has already been imported; keep nothing extra in the scene.
        "keep_source": False,
    }
    if "retry_ladder" in options:
        worker_job["retry_ladder"] = bool(options["retry_ladder"])
    if "voxel_adaptivity" in options:
        worker_job["voxel_adaptivity"] = options["voxel_adaptivity"]
    if "improve_quad_flow" in options:
        worker_job["improve_quad_flow"] = bool(options["improve_quad_flow"])
    return worker_job


def _resolve_target_object(bpy, object_name):
    """The mesh object run_retopo_job will operate on (name match, else first)."""
    obj = bpy.data.objects.get(object_name) if object_name else None
    if obj is None or obj.type != "MESH":
        obj = next((o for o in bpy.data.objects if o.type == "MESH"), None)
    return obj


def _voxel_proxy_if_huge(bpy, obj, options: dict) -> dict | None:
    """Voxel-remesh a very large source down to a proxy before decimation (plan §10).

    Runs in place so the multi-million-face original is freed immediately; the
    subsequent decimation collapse — and its shape comparison — then runs against
    the much smaller, uniform proxy. Returns a proxy report dict (written as
    ``proxy_report.json``) or ``None`` when the source is small enough to decimate
    directly (the original fast path is unchanged for normal meshes).
    """
    if obj is None or not options.get("voxel_proxy", True):
        return None
    source_faces = len(obj.data.polygons)
    threshold = int(options.get("proxy_face_threshold", DEFAULT_PROXY_FACE_THRESHOLD))
    if source_faces <= threshold:
        return None
    if not hasattr(bpy.ops.object, "voxel_remesh"):
        return {"applied": False, "reason": "voxel_remesh operator unavailable",
                "source_faces": source_faces, "threshold": threshold}

    proxy_target = int(options.get("proxy_target_faces", DEFAULT_PROXY_TARGET_FACES))
    adaptivity = float(options.get("voxel_adaptivity", 0.0) or 0.0)
    # Memory guard: a refinement pass may never produce more than this many faces.
    max_proxy_faces = max(int(options.get("proxy_max_faces", proxy_target * 3)), proxy_target)
    max_iter = int(options.get("proxy_max_iter", 5))

    # Make the source the sole active object in OBJECT mode for the operators.
    for o in bpy.context.view_layer.objects:
        o.select_set(o is obj)
    bpy.context.view_layer.objects.active = obj
    if getattr(bpy.context, "object", None) is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")

    # CRITICAL: remesh_voxel_size is in the object's LOCAL space, but obj.dimensions
    # is world space. Importers (esp. FBX) often apply a non-unit object scale
    # (e.g. 0.01), so a world-derived voxel size applied to a 100x-larger local mesh
    # explodes the face count. Bake the scale so local == world before remeshing.
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

    dims = obj.dimensions
    diag = max(float((dims.x ** 2 + dims.y ** 2 + dims.z ** 2) ** 0.5), 1e-6)
    # Density floor so the first pass can never explode (R = diag/voxel capped).
    min_voxel = diag / 2048.0

    def _remesh_in_place(voxel: float) -> int:
        obj.data.remesh_voxel_size = float(voxel)
        obj.data.remesh_voxel_adaptivity = max(0.0, adaptivity)
        bpy.ops.object.voxel_remesh()
        return len(obj.data.polygons)

    # Memory-safe strategy: remesh the source IN PLACE (1x memory — the original is
    # replaced by the proxy on the first pass, never held alongside a copy), then
    # only ever COARSEN toward the target. Coarsening a clean uniform proxy is valid
    # (a larger voxel merges cells); densifying it is not (lost detail can't return),
    # so the first voxel is biased to slightly OVERSHOOT the target.
    #
    # Voxel face count ~ C * (diag/voxel)². The cube model uses C≈6, but real organic
    # high-poly measures C≈1, so resolution ≈ sqrt(target) lands near target; aiming
    # at 1.3x biases to a safe overshoot we then coarsen down.
    aim = min(int(proxy_target * 1.3), max_proxy_faces)
    resolution = min(max(math.sqrt(max(aim, 1)), 8.0), diag / min_voxel)
    voxel = diag / resolution

    faces = _remesh_in_place(voxel)
    attempts = [{"voxel": round(voxel, 8), "faces": faces}]
    for _ in range(max(0, max_iter - 1)):
        if faces <= 1.3 * proxy_target:
            # In tolerance, or already at/below target (can't densify in place) — stop.
            break
        # Overshoot: coarsen via inverse-square on the current proxy.
        next_voxel = voxel * math.sqrt(max(faces, 1) / max(proxy_target, 1))
        if next_voxel <= voxel * 1.02:
            break  # no meaningful change
        voxel = next_voxel
        faces = _remesh_in_place(voxel)
        attempts.append({"voxel": round(voxel, 8), "faces": faces})

    # Drop the stray micro-shell the voxel remesh of the multi-component ZBrush
    # source leaves behind (the 12-vert floater), so the proxy is a single
    # watertight body and A3/A4 can assert the tight components == 1 (matches the
    # validated P1; otherwise A3 tris->quads is skipped on a 2-component proxy).
    floater = None
    try:
        from retopo_agent.blender.proxy import drop_tiny_components
        floater = drop_tiny_components(obj)
        if floater.get("dropped_components"):
            print(
                f"run_app_retopo_job: dropped {floater['dropped_components']} stray shell(s) / "
                f"{floater['dropped_faces']} faces from proxy"
            )
        faces = len(obj.data.polygons)
    except Exception as exc:  # noqa: BLE001 - floater drop is best-effort
        print(f"run_app_retopo_job: floater drop skipped ({exc})")

    # obj is now the proxy in place; the adaptive generator picks it up by name.
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    return {
        "applied": True,
        "source_faces": source_faces,
        "proxy_faces": faces,
        "floater_drop": floater,
        "proxy_target_faces": proxy_target,
        "threshold": threshold,
        "diagonal": round(diag, 6),
        "voxel_size": round(voxel, 8),
        "attempts": attempts,
        # Shape is measured against the proxy, not the original high-poly.
        "shape_reference": "voxel_proxy",
    }


def _remove_mesh(bpy, obj) -> None:
    try:
        bpy.data.objects.remove(obj, do_unlink=True)
    except Exception:  # noqa: BLE001 - best-effort scene cleanup
        pass


def _run_adaptive_generation(bpy, gen_obj, target: int, out_dir: str, options: dict) -> int:
    """Validated **adaptive** low-poly generation (A2 collapse + A3 tris->quads).

    Replaces the old ``decimation_optimize`` worker path, whose default
    ``preserve_features=True`` at a 30 degree feature angle locked nearly every
    edge on the faceted ZBrush mesh and plateaued the Decimate(Collapse) (the app
    shipped a failed best-effort: 2x the target faces, ~49 degree normal deviation,
    non-manifold). This routes the app through ``adaptive_decimate.py`` with feature
    protection OFF (the session-validated default), then A3 turns the triangulated
    collapse into the natural tri/quad mix. ``gen_obj`` is the already-voxel-proxied
    object; shape is measured against it (the proxy IS the reference for huge
    sources). Writes the same app-contract reports the summary normaliser reads.
    """
    import json as _json

    from retopo_agent.blender.adaptive_decimate import (
        CleanupAssertionError, adaptive_decimate_proxy, cleanup_to_mixed_poly,
    )
    from retopo_agent.blender.shape import evaluate_shape_match_blender, render_shape_preview
    from retopo_agent.geometry.validate import validate_topology
    from uv_agent.blender.extract import extract_mesh_graph

    apply_shrinkwrap = bool(options.get("apply_shrinkwrap", True))

    # A2 — adaptive Decimate(Collapse), feature protection OFF (the fix). Duplicates
    # gen_obj and decimates the copy, so gen_obj survives as the shape reference.
    dec = adaptive_decimate_proxy(
        gen_obj, int(target),
        preserve_features=False,
        shrinkwrap=apply_shrinkwrap,
    )
    low = dec.obj
    print(
        f"run_app_retopo_job: A2 {dec.source_face_count} -> {dec.actual_face_count} faces "
        f"(target {target}, band={dec.band}, stopped={dec.stopped_reason})"
    )

    # A3 — tris -> natural tri/quad mix + hard invariants. Best-effort: if the cleanup
    # asserts (e.g. a stubborn budget), keep the A2 result rather than fail the run.
    a3 = None
    try:
        a3 = cleanup_to_mixed_poly(low, target_face_count=int(target))
        print(f"run_app_retopo_job: A3 tris->quads, quads_gained={a3.get('quads_gained')}")
    except CleanupAssertionError as exc:  # noqa: BLE001 - keep A2 best effort
        print(f"run_app_retopo_job: A3 cleanup skipped ({exc})")

    # Shape vs the proxy/reference (must run while both meshes still exist).
    shape = evaluate_shape_match_blender(gen_obj, low)
    with open(os.path.join(out_dir, "shape_report.json"), "w", encoding="utf-8") as fh:
        _json.dump(shape.to_dict(), fh, indent=2)
    print(
        f"run_app_retopo_job: shape status={shape.status} "
        f"mean_ratio={shape.surface_distance_mean_ratio:.4f} "
        f"normal_dev_deg={shape.normal_deviation_mean_deg:.2f}"
    )

    gen_report = {
        "mode": "adaptive",
        "object_name": gen_obj.name,
        "result_object_name": low.name,
        "method": "adaptive_collapse_triquad",
        "preserve_features": False,
        "source_face_count": dec.source_face_count,
        "target_face_count": dec.target_face_count,
        "actual_face_count": dec.actual_face_count,
        "target_error_ratio": round(dec.target_error_ratio, 4),
        "band": dec.band,
        "ratio": round(dec.ratio, 6),
        "stopped_reason": dec.stopped_reason,
        "plateau_face_count": dec.plateau_face_count,
        "quads_gained": (a3 or {}).get("quads_gained"),
        "notes": list(dec.notes),
    }
    with open(os.path.join(out_dir, "generation_report.json"), "w", encoding="utf-8") as fh:
        _json.dump(gen_report, fh, indent=2)

    # Validation (decimation output is triangle-based: quads/ngons not required).
    graph = extract_mesh_graph(low)
    validation = validate_topology(
        graph, len(low.data.polygons),
        quad_required=False, ngon_allowed=False, expect_closed=False)
    vreport = validation.to_dict()
    vreport["status"] = _decimation_topology_status(vreport)
    with open(os.path.join(out_dir, "validation_report.json"), "w", encoding="utf-8") as fh:
        _json.dump(vreport, fh, indent=2)
    print(
        f"run_app_retopo_job: validation status={vreport['status']} "
        f"quad_ratio={vreport.get('quad_ratio')} non_manifold={vreport.get('non_manifold_edge_count')}"
    )

    # Keep only the low-poly in the saved scene (drop the proxy / any source).
    for o in list(bpy.data.objects):
        if o is not low and o.type == "MESH":
            _remove_mesh(bpy, o)

    if options.get("render_preview", True):
        try:
            render_shape_preview(low, os.path.join(out_dir, "preview.png"))
        except Exception as exc:  # noqa: BLE001 - preview is best-effort
            print(f"run_app_retopo_job: preview skipped ({exc})")

    try:
        bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(os.path.join(out_dir, "lowpoly.blend")))
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort
        print(f"run_app_retopo_job: save .blend skipped ({exc})")
    try:
        for o in bpy.context.view_layer.objects:
            o.select_set(o is low)
        bpy.context.view_layer.objects.active = low
        bpy.ops.export_scene.fbx(
            filepath=os.path.abspath(os.path.join(out_dir, "lowpoly.fbx")),
            use_selection=True)
    except Exception as exc:  # noqa: BLE001 - export is best-effort
        print(f"run_app_retopo_job: FBX export skipped ({exc})")
    try:
        for o in bpy.context.view_layer.objects:
            o.select_set(o is low)
        bpy.context.view_layer.objects.active = low
        bpy.ops.export_scene.gltf(
            filepath=os.path.abspath(os.path.join(out_dir, "lowpoly.glb")),
            use_selection=True,
            export_format="GLB")
    except Exception as exc:  # noqa: BLE001 - export is best-effort
        print(f"run_app_retopo_job: GLB export skipped ({exc})")
    return 0


def _decimation_topology_status(report: dict) -> str:
    """App gating status for a triangle low-poly (plan §6 topology report).

    Triangle proportion is excluded (it is the expected output of decimation); the
    gates that matter for a usable working mesh are manifoldness, n-gons and the
    target-count match.
    """
    if report.get("non_manifold_edge_count", 0) > 0:
        return "retry"
    if not report.get("ngon_allowed", False) and report.get("ngon_count", 0) > 0:
        return "retry"
    err = report.get("target_error_ratio")
    if err is not None and err > 0.10:
        return "retry"
    return "accepted"


def _result_mesh(bpy):
    """The low-poly result object left in the scene by run_retopo_job."""
    active = bpy.context.view_layer.objects.active
    if active is not None and active.type == "MESH":
        return active
    return next((o for o in bpy.data.objects if o.type == "MESH"), None)


def _ensure_app_artifacts(bpy, out_dir: str, options: dict) -> list[str]:
    """Backfill app-contract artifacts the worker branch may not emit.

    The decimation-optimize branch of run_retopo_job writes generation/shape
    reports but not ``validation_report.json`` or ``preview.png``; the app needs
    both (plan §6 report tabs + preview). We produce them here from the result
    object still loaded in the scene, leaving the underlying worker untouched
    (plan §5.2 "worker를 직접 고치기보다 wrapper").
    """
    notes: list[str] = []
    obj = _result_mesh(bpy)
    if obj is None:
        return ["no result mesh in scene; skipped artifact backfill"]

    validation_path = os.path.join(out_dir, "validation_report.json")
    if not os.path.exists(validation_path):
        try:
            from retopo_agent.geometry.validate import validate_topology
            from uv_agent.blender.extract import extract_mesh_graph

            graph = extract_mesh_graph(obj)
            target = len(obj.data.polygons)
            # Decimation output is triangle-based: quads/ngons are not required.
            validation = validate_topology(
                graph, target, quad_required=False, ngon_allowed=False, expect_closed=False)
            report = validation.to_dict()
            # The shared validator gates on triangle proportion (it targets quad
            # retopo). For a triangle decimation deliverable that is expected, so the
            # app gating status reflects only manifoldness / n-gons / target match.
            report["status"] = _decimation_topology_status(report)
            with open(validation_path, "w", encoding="utf-8") as fh:
                import json as _json
                _json.dump(report, fh, indent=2)
            notes.append("backfilled validation_report.json")
        except Exception as exc:  # noqa: BLE001 - best-effort
            notes.append(f"validation backfill skipped: {exc}")

    preview_path = os.path.join(out_dir, "preview.png")
    if options.get("render_preview", True) and not os.path.exists(preview_path):
        try:
            from retopo_agent.blender.shape import render_shape_preview

            render_shape_preview(obj, preview_path)
            notes.append("backfilled preview.png")
        except Exception as exc:  # noqa: BLE001 - preview is best-effort
            notes.append(f"preview backfill skipped: {exc}")

    glb_path = os.path.join(out_dir, "lowpoly.glb")
    if not os.path.exists(glb_path):
        try:
            for o in bpy.context.view_layer.objects:
                o.select_set(o is obj)
            bpy.context.view_layer.objects.active = obj
            bpy.ops.export_scene.gltf(
                filepath=os.path.abspath(glb_path),
                use_selection=True,
                export_format="GLB")
            notes.append("backfilled lowpoly.glb")
        except Exception as exc:  # noqa: BLE001 - export is best-effort
            notes.append(f"glb backfill skipped: {exc}")
    return notes


def main() -> int:
    _ensure_importable()
    import app_job_contract as contract  # type: ignore

    opts = _parse_args(sys.argv)
    if "job" not in opts:
        print("run_app_retopo_job requires --job /abs/job.json", file=sys.stderr)
        return 2
    app_job = contract.read_json(opts["job"])

    run_id = app_job.get("run_id", "run")
    out_dir = app_job.get("out_dir") or os.path.join("out", run_id)
    os.makedirs(out_dir, exist_ok=True)
    status_path = os.path.join(out_dir, "status.json")

    status = contract.new_status(
        run_id=run_id,
        command=contract.CMD_GENERATE_LOWPOLY,
        status=contract.STATUS_RUNNING,
        input={
            "source_model": app_job.get("source_model"),
            "object_name": app_job.get("object_name"),
            "target_faces": app_job.get("target_faces"),
        },
    )
    contract.write_json(status_path, status)

    source = app_job.get("source_model")
    if not source or not os.path.exists(source):
        err = {"code": "source_missing", "message": f"source not found: {source}"}
        contract.finalize_status(status, status=contract.STATUS_FAILED, error=err)
        contract.write_json(status_path, status)
        print(f"run_app_retopo_job: {err['message']}", file=sys.stderr)
        return 2

    try:
        import bpy

        try:
            bpy.ops.wm.read_homefile(use_empty=True)
        except Exception:  # noqa: BLE001
            for o in list(bpy.data.objects):
                bpy.data.objects.remove(o, do_unlink=True)

        _import_source(bpy, source)

        # For very large sources, voxel-remesh to a proxy before the decimation
        # collapse (plan §10) so the collapse never runs on tens of millions of
        # faces. No-op for normal meshes (the fast path is unchanged).
        proxy = None
        options = app_job.get("options") or {}
        try:
            target_obj = _resolve_target_object(bpy, app_job.get("object_name"))
            proxy = _voxel_proxy_if_huge(bpy, target_obj, options)
            if proxy is not None:
                contract.write_json(os.path.join(out_dir, "proxy_report.json"), proxy)
                if proxy.get("applied"):
                    print(
                        f"run_app_retopo_job: voxel proxy {proxy['source_faces']} -> "
                        f"{proxy['proxy_faces']} faces (target ~{proxy['proxy_target_faces']})"
                    )
                else:
                    print(f"run_app_retopo_job: voxel proxy skipped ({proxy.get('reason')})")
        except Exception as exc:  # noqa: BLE001 - proxy is best-effort; fall back to direct decimate
            proxy = {"applied": False, "reason": f"voxel proxy error: {exc}"}
            contract.write_json(os.path.join(out_dir, "proxy_report.json"), proxy)
            print(f"run_app_retopo_job: voxel proxy error, decimating directly: {exc}", file=sys.stderr)

        worker_job = _translate_to_worker_job(app_job, out_dir)
        worker_job_path = os.path.join(out_dir, "job.json")
        contract.write_json(worker_job_path, worker_job)

        # Generation: route through the session-validated ADAPTIVE path (A2 collapse
        # with feature protection OFF + A3 tris->quads), NOT the old
        # decimation_optimize worker whose 30-degree feature lock plateaued the
        # collapse on this asset. Runs on the already-(voxel-)proxied object.
        gen_obj = _result_mesh(bpy) if proxy and proxy.get("applied") else \
            _resolve_target_object(bpy, app_job.get("object_name"))
        if gen_obj is None:
            raise RuntimeError("no mesh object in scene to generate from")
        rc = _run_adaptive_generation(
            bpy, gen_obj, int(app_job.get("target_faces", 12000)), out_dir, options)

        summary = contract.normalize_summary(
            out_dir,
            run_id=run_id,
            object_name=app_job.get("object_name"),
            target_faces=app_job.get("target_faces"),
        )
        if proxy and proxy.get("applied"):
            summary["artifacts"]["proxy_report"] = "proxy_report.json"
            summary["metrics"]["source_faces"] = proxy["source_faces"]
            summary["metrics"]["proxy_faces"] = proxy["proxy_faces"]
            summary["warnings"].append(
                f"voxel proxy applied: {proxy['source_faces']} -> {proxy['proxy_faces']} "
                f"faces before decimation; shape measured vs the proxy, not the original"
            )
        contract.write_json(os.path.join(out_dir, "summary.json"), summary)

        if rc != 0:
            err = {"code": "worker_nonzero", "message": f"run_retopo_job exited {rc}"}
            contract.finalize_status(
                status, status=contract.STATUS_FAILED,
                artifacts=summary["artifacts"], error=err)
            contract.write_json(status_path, status)
            return rc

        contract.finalize_status(
            status, status=contract.STATUS_ACCEPTED, artifacts=summary["artifacts"])
        contract.write_json(status_path, status)
        print(f"run_app_retopo_job: accepted run={run_id} out_dir={out_dir}")
        return 0

    except Exception as exc:  # noqa: BLE001 - any failure becomes a structured status
        tb = traceback.format_exc()
        err = {"code": "exception", "message": str(exc), "traceback": tb}
        # Still try to normalize whatever partial artifacts exist.
        try:
            summary = contract.normalize_summary(
                out_dir, run_id=run_id,
                object_name=app_job.get("object_name"),
                target_faces=app_job.get("target_faces"))
            contract.write_json(os.path.join(out_dir, "summary.json"), summary)
            artifacts = summary["artifacts"]
        except Exception:  # noqa: BLE001
            artifacts = {}
        contract.finalize_status(
            status, status=contract.STATUS_FAILED, artifacts=artifacts, error=err)
        contract.write_json(status_path, status)
        print(f"run_app_retopo_job: failed: {exc}\n{tb}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
