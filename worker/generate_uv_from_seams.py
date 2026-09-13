"""App-facing UV generate + optimize worker (Electron MVP 3 plan §4, §10; Sessions A–D).

Run inside Blender:

    blender --background --python worker/generate_uv_from_seams.py -- --job /abs/job.json

``job.json`` carries ``command: "generate_uv_from_seams"`` (plan §4.1) and an explicit
run ``mode`` (UV_AUTOMATION_WORK_PLAN §3, gate G2):

``preserve_existing`` (the default, and how a mode-less legacy project is read):

1. open/import the working model + resolve the selected object,
2. load the MVP 2 ``user_seam_spec.json`` (or derive one from an existing UV island
   boundary) and VALIDATE its edge ids against the current mesh,
3. run ``chart_uv_agent.pipeline.run_chart_uv`` in STRICT user/reference mode — the
   user's seam set is the source of truth and is never changed (plan §1, §6),
4. evaluate layout-optimization candidates over that FIXED seam set (plan §5).

``auto_generate`` (explicit automatic cutting from an approved low-poly, work plan §3):

1. same object resolution + mesh identity capture,
2. an OPTIONAL seam spec only supplies locked / protected edges — its absence is NOT
   ``needs_input`` (gate G2); the no-spec automatic chart core runs,
3. ``run_chart_uv`` runs the constrained refinement loop under the frozen quality
   profile and the explicit budgets; layout optimization does NOT run (the loop's own
   final measure owns packing).

Both modes then share the acceptance machinery (gates G0/G1/G6/G7):

- the low-poly's mesh identity is captured before and after the engine and diffed,
- reviewer feedback (locks/protected/preferred) is applied ONLY when its
  ``mesh_fingerprint`` matches this mesh (gate G7),
- ``selected_uv.blend`` is written into ``<run>/.staging/`` first, RE-READ from disk and
  audited (gate G1), then atomically ``os.replace``-d into the run directory,
- an accepted run copies the blend + summary to ``work/uv/`` through ``.tmp`` +
  sha256 compare + ``os.replace`` so a failed handoff never destroys the previously
  approved files (gate G6),
- the run emits the G0/G1/G3/G5/G7 evidence artifacts (run manifest, distortion v2,
  correctness, re-read audit, candidate history, mesh identity, quality profile, seam
  overlay, anisotropy heatmap).

Hard rules (plan §1, §6, §14): the worker NEVER overwrites the source working model and
NEVER overwrites the user seam spec. In ``preserve_existing`` it never changes the seam
set; a run that breaks seam integrity ends ``needs_user_review`` and does NOT replace
``work/uv/selected_uv.blend``. Every exit leaves a structured JSON result so the app
never parses stdout (plan §4.1).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import time
import traceback

#: Where the selected blend is written BEFORE it is audited and atomically promoted
#: into the run directory (gate G6 "임시 경로에 저장·검증 후 원자적 교체").
STAGING_DIR = ".staging"


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


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _open_model(bpy, path: str) -> None:
    """Open a ``.blend`` or import a model into a fresh scene (plan §4.1 import set)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".blend":
        bpy.ops.wm.open_mainfile(filepath=path)
        return
    try:
        bpy.ops.wm.read_homefile(use_empty=True)
    except Exception:  # noqa: BLE001 - best-effort; default scene is acceptable
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o, do_unlink=True)
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
        raise ValueError(f"unsupported model format: {ext or '(none)'}")


def _mesh_objects(bpy) -> list:
    return [o for o in bpy.data.objects if o.type == "MESH"]


def _resolve_object(bpy, object_name):
    obj = bpy.data.objects.get(object_name) if object_name else None
    if obj is None or obj.type != "MESH":
        obj = next((o for o in _mesh_objects(bpy)), None)
    return obj


def _model_label(job: dict) -> str | None:
    rel = job.get("model_rel")
    if rel:
        return rel
    model = job.get("model")
    return os.path.basename(model) if model else None


def _seam_spec_label(job: dict) -> str | None:
    rel = job.get("seam_spec_rel")
    if rel:
        return rel
    sp = job.get("seam_spec")
    return os.path.basename(sp) if sp else None


def _status_input(job: dict, mode: str | None = None) -> dict:
    """The ``status.json`` input block. ``mode`` travels with the run (gate G2)."""
    return {
        "model": _model_label(job),
        "object_name": job.get("object_name"),
        "seam_spec": _seam_spec_label(job),
        "mode": mode,
    }


# ---------------------------------------------------------------------------
# Small local utilities (no contract / engine changes — worker-local, plan §14)
# ---------------------------------------------------------------------------
def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _peak_memory_mb():
    """Peak resident memory of THIS process in MiB, or ``None`` when unavailable.

    Windows has no ``resource`` module, so the psapi ``PeakWorkingSetSize`` is read
    through ``ctypes``; everywhere else ``resource.getrusage`` is used. Any failure
    degrades to ``None`` — a missing performance number must never kill a run.
    """
    try:
        if sys.platform.startswith("win"):
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _PMC()
            counters.cb = ctypes.sizeof(_PMC)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            try:
                psapi = ctypes.WinDLL("psapi", use_last_error=True)
                get_info = psapi.GetProcessMemoryInfo
            except OSError:
                get_info = kernel32.K32GetProcessMemoryInfo
            get_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PMC), wintypes.DWORD]
            get_info.restype = wintypes.BOOL
            handle = kernel32.GetCurrentProcess()
            if not get_info(handle, ctypes.byref(counters), counters.cb):
                return None
            return round(float(counters.PeakWorkingSetSize) / (1024.0 * 1024.0), 3)
        import resource  # noqa: PLC0415 - POSIX only

        peak = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        # macOS reports bytes; Linux reports kilobytes.
        divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
        return round(peak / divisor, 3)
    except Exception:  # noqa: BLE001 - best-effort diagnostics only
        return None


def _blender_build_hash(bpy) -> str | None:
    raw = getattr(bpy.app, "build_hash", None)
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def _profile_summary(profile_dict: dict | None) -> dict | None:
    """The headline quality-profile keys for the summary (the full profile is an artifact)."""
    if not profile_dict:
        return None
    keys = ("profile_id", "metric_version", "calibrated", "anisotropy_global_p95_max",
            "anisotropy_island_p95_max", "anisotropy_max_max",
            "bad_area_ratio_max", "area_stretch_global_mean_max",
            "area_stretch_global_p95_max",
            "anisotropy_hard_max", "bad_area_fraction_cap", "min_island_width_px",
            "texture_size_px", "margin_px", "border_margin_px",
            "shading_uv_policy", "merge_back_enabled", "seed", "max_iterations",
            "max_candidates_per_round", "time_budget_s", "island_cap",
            "min_improvement_ratio")
    return {k: profile_dict.get(k) for k in keys if k in profile_dict}


# ---------------------------------------------------------------------------
# Spec validation (plan §6 — pre-run edge-id / object checks)
# ---------------------------------------------------------------------------
def _validate_spec_against_mesh(spec, mesh, object_name: str | None) -> dict:
    """Edge-id range + object-match validation (plan §4.1, §6).

    Returns ``{"invalid_edges": [...], "object_mismatch": bool}``. Invalid edge
    ids or an object mismatch make the run a structured ``failed`` BEFORE the
    engine runs, so nothing ships (plan §4.1, §6).
    """
    n = mesh.edge_count
    edges = set(spec.effective_seam_edges()) | set(spec.effective_protected_edges())
    invalid = sorted(e for e in edges if not (0 <= e < n))
    mismatch = bool(spec.object and object_name and spec.object != object_name)
    return {"invalid_edges": invalid, "object_mismatch": mismatch}


# ---------------------------------------------------------------------------
# Seam source resolution (UV-boundary-fallback revision plan §1, §4.1)
# ---------------------------------------------------------------------------
def _resolve_seam_source(contract, job: dict, obj) -> dict:
    """Resolve the seam source for a ``preserve_existing`` run (revision plan §1, §4.1).

    Precedence (``decide_seam_source``): an existing ``seam_spec`` file is the
    source of truth; else the selected/active ``uv_layer`` is read and its UV
    island boundary becomes a *derived* ``UserSeamSpec``; else ``needs_input``.
    The derived path NEVER overwrites the MVP 2 ``user_seam_spec.json`` and NEVER
    adds a seam beyond the UV boundary (revision plan §4.1 "Do not").

    Returns a dict:

    - ``{"status": "ok", "spec": UserSeamSpec, "seam_source": <block>,
        "label": <str>, "derived_spec": <dict|None>, "resolution": <report>}``
    - ``{"status": "needs_input", "error": {...}, "resolution": <report>}``
    - ``{"status": "failed", "error": {...}, "resolution": <report>}`` for a
      malformed spec / unreadable UV (a structured pre-run failure, plan §4.1).
    """
    from artist_uv_agent.user_seams import UserSeamSpec, load_user_seam_spec
    from uv_agent.blender.uv_extract import extract_uv_boundary_edges

    seam_spec_path = job.get("seam_spec")
    seam_spec_exists = bool(seam_spec_path and os.path.exists(seam_spec_path))
    uv_layer = job.get("uv_layer") or job.get("selected_uv_layer")
    policy = job.get("seam_source_policy", contract.DEFAULT_SEAM_SOURCE_POLICY)
    decision = contract.decide_seam_source(
        seam_spec_path=seam_spec_path, seam_spec_exists=seam_spec_exists,
        uv_layer=uv_layer, policy=policy)

    # 1) Explicit MVP 2 spec wins (revision plan §1 case 1, §7).
    if decision["kind"] == contract.SEAM_SOURCE_USER_SPEC:
        try:
            spec = load_user_seam_spec(seam_spec_path)
        except Exception as exc:  # noqa: BLE001 - malformed spec is a setup error
            return {"status": "failed", "resolution": {"policy": policy, "kind": "failed"},
                    "error": {"code": "invalid_seam_spec", "message": f"could not load seam spec: {exc}"}}
        path = job.get("seam_spec_rel") or os.path.basename(seam_spec_path)
        seam_source = contract.build_seam_source(
            source_type=contract.SEAM_SOURCE_USER_SPEC, path=path, uv_layer=None,
            user_confirmed=True, derived=False)
        return {"status": "ok", "spec": spec, "seam_source": seam_source, "label": path,
                "derived_spec": None,
                "resolution": {"policy": policy, "kind": decision["kind"], "seam_spec": path}}

    # 2) Derive a spec from the existing UV island boundary (revision plan §1 case 2).
    if decision["kind"] == contract.SEAM_SOURCE_UV_BOUNDARY:
        try:
            edge_ids, report = extract_uv_boundary_edges(obj, uv_layer)
        except Exception as exc:  # noqa: BLE001 - unreadable UV is a structured failure
            return {"status": "failed",
                    "resolution": {"policy": policy, "kind": "failed", "uv_layer": uv_layer},
                    "error": {"code": "uv_boundary_extract_failed",
                              "message": f"UV boundary extraction failed: {exc}"}}
        # Requested fallback layer is missing/empty -> needs_input (revision plan §1 case 3).
        if report.get("uv_layer_missing"):
            return {"status": contract.STATUS_NEEDS_INPUT,
                    "resolution": {"policy": policy, "kind": contract.STATUS_NEEDS_INPUT,
                                   "requested_uv_layer": uv_layer, "uv_layer_missing": True},
                    "error": {"code": contract.MISSING_SEAM_SOURCE_CODE,
                              "message": contract.MISSING_SEAM_SOURCE_MESSAGE}}
        resolved_layer = report.get("uv_layer") or uv_layer
        derived_spec = contract.make_derived_seam_spec(
            object_name=obj.name, user_seam_edges=edge_ids, uv_layer=resolved_layer)
        spec = UserSeamSpec.from_dict(derived_spec)
        path = job.get("derived_seam_spec_out_rel") or contract.DERIVED_SEAM_SPEC_REL
        seam_source = contract.build_seam_source(
            source_type=contract.SEAM_SOURCE_UV_BOUNDARY, path=path, uv_layer=resolved_layer,
            user_confirmed=False, derived=True)
        # Flatten the boundary report's headline fields onto the resolution block so
        # ``seam_source_resolution.json`` is self-explanatory (MVP3 §2 Goal A completion
        # criterion / §3 Step 2): island_count, boundary_edge_count, the extraction method,
        # and any dropped/ambiguous edges that explain a low boundary count.
        return {"status": "ok", "spec": spec, "seam_source": seam_source, "label": path,
                "derived_spec": derived_spec,
                "resolution": {"policy": policy, "kind": decision["kind"],
                               "uv_layer": resolved_layer, "derived_seam_spec": path,
                               "object_name": obj.name,
                               "island_count": report.get("island_count"),
                               "uv_layer_loop_count": report.get("uv_layer_loop_count"),
                               "boundary_edge_count": report.get("boundary_edge_count"),
                               "boundary_extraction_method": report.get("method"),
                               "mesh_boundary_edge_count": report.get("mesh_boundary_edge_count"),
                               "ambiguous_boundary_count": report.get("ambiguous_boundary_count"),
                               "dropped_or_ambiguous_edges": report.get("dropped_or_ambiguous_edges", []),
                               "boundary_report": report}}

    # 3) Nothing to unwrap from (revision plan §1 case 3, §4.2).
    return {"status": contract.STATUS_NEEDS_INPUT, "error": decision["error"],
            "resolution": {"policy": policy, "kind": contract.STATUS_NEEDS_INPUT,
                           "seam_spec": None, "uv_layer": None}}


# ---------------------------------------------------------------------------
# Reviewer feedback (gate G7 — reapplied ONLY on the same mesh fingerprint)
# ---------------------------------------------------------------------------
def _load_feedback(contract, job: dict, identity_before: dict) -> tuple[dict, dict, list[str]]:
    """Read ``job["feedback"]`` and decide whether it may be reused (gate G7).

    Returns ``(feedback_applied_block, inputs, warnings)``. ``inputs`` carries the
    locked / protected / preferred edge sets and ``front_axis`` and is EMPTY unless
    the saved ``mesh_fingerprint`` matches this mesh's fingerprint: a topology change
    must never silently re-apply stale edge-id constraints (gate G7).
    """
    empty_counts = {"locked_seam_count": 0, "protected_count": 0, "preferred_count": 0}
    inputs = {"locked": set(), "protected": set(), "preferred": set(), "front_axis": ""}
    warnings: list[str] = []

    path = job.get("feedback")
    if not path or not os.path.exists(path):
        return ({"applied": False, "reason": "no_feedback", "path": job.get("feedback_rel"),
                 **empty_counts}, inputs, warnings)
    try:
        data = contract.read_json(path)
    except Exception as exc:  # noqa: BLE001 - unreadable feedback is a warning, not a failure
        warnings.append(f"reviewer feedback unreadable: {exc}")
        return ({"applied": False, "reason": "unreadable", "path": job.get("feedback_rel"),
                 **empty_counts}, inputs, warnings)
    if not isinstance(data, dict):
        warnings.append("reviewer feedback is not a JSON object")
        return ({"applied": False, "reason": "unreadable", "path": job.get("feedback_rel"),
                 **empty_counts}, inputs, warnings)

    fp = data.get("mesh_fingerprint")
    rel = job.get("feedback_rel") or os.path.basename(path)
    if not fp:
        warnings.append("reviewer feedback has no mesh_fingerprint; constraints not reused")
        return ({"applied": False, "reason": "no_fingerprint", "path": rel,
                 **empty_counts}, inputs, warnings)
    if str(fp) != str(identity_before.get("fingerprint")):
        warnings.append("reviewer feedback mesh_fingerprint does not match this mesh; "
                        "saved constraints were NOT reused")
        return ({"applied": False, "reason": "fingerprint_mismatch", "path": rel,
                 "feedback_fingerprint": str(fp),
                 "mesh_fingerprint": identity_before.get("fingerprint"),
                 **empty_counts}, inputs, warnings)

    def _ids(key: str) -> set[int]:
        raw = data.get(key)
        if not isinstance(raw, (list, tuple)):
            return set()
        out: set[int] = set()
        for e in raw:
            try:
                out.add(int(e))
            except (TypeError, ValueError):
                continue
        return out

    inputs["locked"] = _ids("locked_seam_edges")
    inputs["protected"] = _ids("protected_edges")
    inputs["preferred"] = _ids("preferred_edges")
    axis = data.get("front_axis")
    inputs["front_axis"] = str(axis) if isinstance(axis, str) else ""
    block = {
        "applied": True,
        "reason": "fingerprint_match",
        "path": rel,
        "mesh_fingerprint": identity_before.get("fingerprint"),
        "locked_seam_count": len(inputs["locked"]),
        "protected_count": len(inputs["protected"]),
        "preferred_count": len(inputs["preferred"]),
        "front_axis": inputs["front_axis"],
    }
    return block, inputs, warnings


# ---------------------------------------------------------------------------
# Preview rendering (plan §7 — baseline vs selected, stable framing)
# ---------------------------------------------------------------------------
def _render_previews(obj, mesh, final_seams, out_dir: str, *, render_size: int,
                     texture_size: int, checker_scale: float) -> list[str]:
    """Render the SELECTED then the BASELINE UV-layout + checker previews (plan §7).

    The object enters holding the SELECTED layout (``run_chart_uv`` left it there).
    Selected previews are rendered first; then the baseline strict-seam unwrap is
    re-applied to the SAME fixed seam set and the baseline previews are rendered.
    Camera framing is on the (UV-independent) mesh bounds, so it is identical
    between baseline and selected (plan §7). Best-effort: a failing render becomes
    a warning, not a failure (plan §13). ``selected_uv.blend`` must already be
    saved before this runs (the checker material must not persist, plan §7)."""
    from chart_uv_agent.layout_optimization import BASELINE_SPEC
    from chart_uv_agent.unwrap import read_uvmap, unwrap_and_pack
    from uv_agent.blender.review_render import render_checker_views
    from uv_agent.geometry.uv_review import write_uv_layout_png

    warnings: list[str] = []
    seams = set(int(e) for e in final_seams)

    def _layout(tag: str) -> None:
        try:
            uvmap = read_uvmap(obj, mesh)
            write_uv_layout_png(mesh, uvmap, os.path.join(out_dir, f"{tag}_uv_layout.png"),
                                size=texture_size)
        except Exception as exc:  # noqa: BLE001 - layout is best-effort (plan §7)
            warnings.append(f"{tag}_uv_layout.png render failed: {exc}")

    def _checker(tag: str) -> None:
        checker = render_checker_views(
            obj, out_dir, scale=checker_scale, size=render_size,
            filenames={"front": f"{tag}_checker_front.png", "side": f"{tag}_checker_side.png"})
        for view in ("front", "side"):
            if view not in checker:
                warnings.append(f"{tag}_checker_{view}.png render failed")

    # 1) Selected layout (object already holds it).
    _layout("selected")
    _checker("selected")

    # 2) Baseline = the first strict user-seam unwrap, re-applied on the SAME seams
    #    (plan §7 "Baseline means first strict user seam unwrap before layout
    #    optimization replacement"). Never adds/removes a seam.
    try:
        unwrap_and_pack(obj, seams, margin=BASELINE_SPEC["margin"],
                        method=BASELINE_SPEC["unwrap_method"],
                        minimize_iters=BASELINE_SPEC["minimize_iters"],
                        pack_shape=BASELINE_SPEC["pack_shape"], rotate=BASELINE_SPEC["rotate"],
                        average_scale=BASELINE_SPEC["average_scale"])
    except Exception as exc:  # noqa: BLE001 - baseline preview is best-effort
        warnings.append(f"baseline unwrap failed: {exc}")
        return warnings
    _layout("baseline")
    _checker("baseline")
    return warnings


# ---------------------------------------------------------------------------
# Final artifact re-read audit (gate G1 "저장·export 재읽기 후에도 0")
# ---------------------------------------------------------------------------
def _final_reread_audit(bpy, staging_blend: str, out_dir: str, mesh, final_seams,
                        *, object_data_name: str, texture_size_px: int, margin_px: float,
                        reported_only: bool, shading_policy: str = "preserve") -> dict:
    """Re-open the SAVED blend from disk and re-audit the UV it actually carries.

    Edge ids may be renumbered by the save/load round trip, so the original seam set is
    translated through :func:`edge_correspondence` (geometry, not ids) before the
    mandatory-90 seam audit runs on the re-read mesh (gate G1).

    In ``preserve_existing`` the mandatory numbers are REPORTED ONLY — that mode never
    cuts, so an original 90° violation is not this run's failure (gate G1/G2).
    """
    from chart_uv_agent.segmentation import mandatory_seam_audit
    from uv_agent.blender.extract import extract_mesh_graph
    from uv_agent.blender.organic_unwrap import AI_UV_LAYER, read_uvmap
    from uv_agent.geometry.distortion_v2 import evaluate_distortion_v2
    from uv_agent.geometry.evaluation import mandatory_seam_uv_audit
    from uv_agent.geometry.mesh_identity import edge_correspondence, remap_edge_ids
    from uv_agent.geometry.shading_policy import sharp_edge_uv_audit
    from uv_agent.geometry.uv_correctness import compact_correctness, evaluate_correctness

    source_rel = os.path.relpath(staging_blend, out_dir).replace(os.sep, "/")
    tmp_obj = None
    mesh_db = None
    try:
        with bpy.data.libraries.load(staging_blend, link=False) as (data_from, data_to):
            names = list(data_from.meshes)
            if not names:
                raise RuntimeError("saved blend carries no mesh datablock")
            name = object_data_name if object_data_name in names else names[0]
            data_to.meshes = [name]
        loaded = [m for m in data_to.meshes if m is not None]
        if not loaded:
            raise RuntimeError("mesh datablock could not be re-read")
        mesh_db = loaded[0]
        tmp_obj = bpy.data.objects.new("_uv_reread_audit_tmp", mesh_db)

        mesh_r = extract_mesh_graph(tmp_obj)
        uvmap_r = read_uvmap(tmp_obj, mesh_r, layer_name=AI_UV_LAYER)

        uv_audit = mandatory_seam_uv_audit(mesh_r, uvmap_r)
        corr = compact_correctness(evaluate_correctness(
            mesh_r, uvmap_r, texture_size_px=int(texture_size_px),
            margin_px=float(margin_px)))
        correspondence = edge_correspondence(mesh, mesh_r)
        remapped, unmatched = remap_edge_ids(sorted(int(e) for e in final_seams),
                                             correspondence)
        seam_audit = mandatory_seam_audit(mesh_r, set(remapped), fold_angle=90.0)
        distortion_global = (evaluate_distortion_v2(mesh_r, uvmap_r) or {}).get("global")

        # G10: does the SAVED file still split the UVs on every sharp edge?
        sharp_audit = sharp_edge_uv_audit(mesh_r, uvmap_r)

        unsplit = int(uv_audit["mandatory_90_uv_unsplit"])
        missing = int(seam_audit["mandatory_90_missing"])
        sharp_unsplit = int(sharp_audit["sharp_edge_uv_unsplit"])
        audit = {
            "source": source_rel,
            "mandatory_90_uv_unsplit": unsplit,
            "mandatory_90_missing_remapped": missing,
            "edge_id_remap": not bool(correspondence.get("identical_ids")),
            "unmatched_edges": len(unmatched),
            "correctness": corr,
            "distortion_global": distortion_global,
            "sharp_edge_uv_audit": sharp_audit,
            "shading_policy": str(shading_policy),
        }
        if reported_only:
            audit["reported_only"] = True
            audit["passed"] = bool(corr.get("passed")) and not unmatched
        else:
            sharp_ok = (sharp_unsplit == 0
                        if str(shading_policy) == "require_uv_seam_on_sharp_edges" else True)
            audit["passed"] = (unsplit == 0 and missing == 0 and sharp_ok
                               and bool(corr.get("passed")) and not unmatched)
        return audit
    except Exception as exc:  # noqa: BLE001 - an unevaluatable audit is never a pass
        return {"passed": False, "source": source_rel, "error": str(exc)}
    finally:
        try:
            if tmp_obj is not None:
                bpy.data.objects.remove(tmp_obj, do_unlink=True)
            if mesh_db is not None:
                bpy.data.meshes.remove(mesh_db, do_unlink=True)
        except Exception:  # noqa: BLE001 - cleanup is best effort
            pass


# ---------------------------------------------------------------------------
# generate_uv_from_seams (plan §4.1, §10; work plan §3)
# ---------------------------------------------------------------------------
def _run_generate(bpy, contract, job: dict, out_dir: str, status_path: str, status: dict,
                  *, mode: str, options: dict) -> int:
    from uv_agent.blender.extract import extract_mesh_graph
    from uv_agent.geometry.mesh_identity import mesh_identity
    from uv_agent.geometry.shading_policy import shading_snapshot

    started = time.monotonic()

    def _fail(code: str, message: str, **details) -> int:
        err = {"code": code, "message": message}
        if details:
            err["details"] = details
        contract.finalize_status(status, status=contract.STATUS_FAILED, error=err)
        contract.write_json(status_path, status)
        print(f"generate_uv_from_seams: {message}", file=sys.stderr)
        return 2

    def _needs_input(code: str, message: str) -> int:
        contract.finalize_status(status, status=contract.STATUS_NEEDS_INPUT,
                                 error={"code": code, "message": message})
        contract.write_json(status_path, status)
        print(f"generate_uv_from_seams: needs_input: {message}", file=sys.stderr)
        # needs_input is a product outcome (no usable seam source), not a process
        # error — the verdict lives in status.json, so exit 0 (plan §4.1, revision §4.2).
        return 0

    # --- object -----------------------------------------------------------
    obj = _resolve_object(bpy, job.get("object_name"))
    if obj is None:
        return _fail("object_not_found",
                     f"no mesh object to generate (requested {job.get('object_name')!r})")

    # --- G0: the approved low-poly's identity BEFORE anything touches it ---
    mesh = extract_mesh_graph(obj)
    identity_before = mesh_identity(mesh, model_path=job.get("model"))

    # --- G10: the shading state BEFORE the engine (sharp edges / smooth faces) ---
    try:
        shading_before = shading_snapshot(obj.data)
    except Exception as exc:  # noqa: BLE001 - an unreadable snapshot must not kill the run
        shading_before = None
        print(f"generate_uv_from_seams: shading snapshot failed: {exc}", file=sys.stderr)

    # --- G7: reviewer feedback, only on a matching fingerprint ------------
    feedback_applied, feedback_inputs, warnings = _load_feedback(contract, job, identity_before)

    ctx = {
        "started": started,
        "obj": obj,
        "mesh": mesh,
        "identity_before": identity_before,
        "shading_before": shading_before,
        "feedback_applied": feedback_applied,
        "feedback_inputs": feedback_inputs,
        "warnings": warnings,
        "mode": mode,
        "options": options,
        "fail": _fail,
        "needs_input": _needs_input,
    }
    if mode == contract.MODE_AUTO_GENERATE:
        return _run_auto(bpy, contract, job, out_dir, status_path, status, ctx)
    return _run_preserve(bpy, contract, job, out_dir, status_path, status, ctx)


def _run_preserve(bpy, contract, job: dict, out_dir: str, status_path: str, status: dict,
                  ctx: dict) -> int:
    """``preserve_existing``: the user's seam set is FIXED; only layout is optimized."""
    from chart_uv_agent.layout_optimization import BASELINE_SPEC, make_config, spec_id
    from chart_uv_agent.pipeline import run_chart_uv

    obj, mesh, options = ctx["obj"], ctx["mesh"], ctx["options"]
    warnings = ctx["warnings"]

    def _write_resolution(resolution: dict | None) -> None:
        if resolution is not None:
            contract.write_json(os.path.join(out_dir, contract.SEAM_SOURCE_RESOLUTION_FILE),
                                resolution)

    # --- seam source: explicit spec | derived UV boundary | needs_input ---
    resolved = _resolve_seam_source(contract, job, obj)
    _write_resolution(resolved.get("resolution"))
    if resolved["status"] == contract.STATUS_NEEDS_INPUT:
        return ctx["needs_input"](resolved["error"]["code"], resolved["error"]["message"])
    if resolved["status"] == "failed":
        return ctx["fail"](resolved["error"]["code"], resolved["error"]["message"])

    spec = resolved["spec"]
    seam_source = resolved["seam_source"]
    seam_spec_label = resolved["label"]
    derived_spec = resolved["derived_spec"]

    # Persist a derived spec SEPARATELY (canonical work/seams + run-folder copy);
    # never overwrite the user's MVP 2 spec (revision plan §4.1 "Do not").
    if derived_spec is not None:
        if job.get("derived_seam_spec_out"):
            try:
                contract.write_json(job["derived_seam_spec_out"], derived_spec)
            except Exception as exc:  # noqa: BLE001 - canonical copy is best-effort
                print(f"generate_uv_from_seams: derived spec write failed: {exc}", file=sys.stderr)
        contract.write_json(os.path.join(out_dir, contract.DERIVED_SEAM_SPEC_FILE), derived_spec)

    validation = _validate_spec_against_mesh(spec, mesh, obj.name)
    if validation["invalid_edges"] or validation["object_mismatch"]:
        msg = ("Seam spec contains edge ids that do not exist on the selected mesh."
               if validation["invalid_edges"]
               else f"Seam spec object {spec.object!r} does not match selected object {obj.name!r}.")
        return ctx["fail"]("invalid_seam_spec", msg,
                           invalid_edges=validation["invalid_edges"],
                           object_mismatch=validation["object_mismatch"])

    # --- strict user/reference run (plan §1, §6) --------------------------
    optimize_layout = bool(options.get("optimize_layout", True))
    lo_cfg = None
    if optimize_layout:
        lo_cfg = make_config(options.get("layout_opt_preset", contract.DEFAULT_LAYOUT_OPT_PRESET),
                             max_candidates=int(options.get("layout_opt_max_candidates",
                                                            contract.DEFAULT_LAYOUT_OPT_MAX_CANDIDATES)),
                             enabled=True)
    print(f"generate_uv_from_seams: mode=preserve_existing object={obj.name!r} "
          f"user_seams={len(spec.user_seam_edges)} protected={len(spec.user_protected_edges)} "
          f"optimize_layout={optimize_layout} "
          f"max_candidates={getattr(lo_cfg, 'max_candidates', 0)} "
          f"flags(auto_refine={options['auto_refine_user_seams']},repair={options['repair_user_seams']},"
          f"enforce={options['enforce_user_mandatory']},gate={options['gate_user_mandatory']})", flush=True)

    res = run_chart_uv(
        obj, mesh, user_seam_spec=spec,
        auto_refine_user_seams=bool(options["auto_refine_user_seams"]),
        repair_user_seams=bool(options["repair_user_seams"]),
        enforce_user_mandatory=bool(options["enforce_user_mandatory"]),
        gate_user_mandatory=bool(options["gate_user_mandatory"]),
        optimize_layout=optimize_layout,
        layout_optimization_config=lo_cfg,
        quality_profile=options["quality_profile"],
        texture_size_px=options["texture_size_px"],
        margin_px=options["margin_px"],
        seed=options["seed"])

    final_seams = res.get("seams", [])
    metrics = res.get("metrics", {})
    user_block = res.get("user_seams", {})
    layout_report = res.get("layout_optimization")

    candidate_summary = contract.normalize_candidate_summary(
        layout_report, baseline_candidate_id=spec_id(BASELINE_SPEC),
        score_weights=getattr(lo_cfg, "score_weights", None),
        max_candidates=int(options.get("layout_opt_max_candidates",
                                       contract.DEFAULT_LAYOUT_OPT_MAX_CANDIDATES)),
        average_scale=bool(getattr(lo_cfg, "average_scale", True)))
    contract.write_json(os.path.join(out_dir, contract.CANDIDATE_SUMMARY_FILE), candidate_summary)

    # --- seam integrity + layout quality (plan §6, §13) -------------------
    integrity = contract.evaluate_seam_integrity(
        user_block, options, final_seams=final_seams,
        invalid_edges=user_block.get("invalid_edges"), object_mismatch=False)
    quality_v1 = contract.evaluate_layout_quality(metrics)
    for v in integrity["violations"]:
        warnings.append(f"seam integrity: {v.get('code')}")
    for issue in quality_v1["issues"]:
        warnings.append(f"layout quality: {issue.get('code')}")

    return _finish_run(
        bpy, contract, job, out_dir, status_path, status, ctx,
        res=res, final_seams=final_seams, metrics=metrics,
        seam_source=seam_source, seam_spec_label=seam_spec_label,
        seam_integrity_block=integrity["block"],
        layout_optimization=contract.build_layout_optimization_block(layout_report),
        selected_candidate_id=candidate_summary.get("selected_candidate_id"),
        integrity=integrity, quality_v1=quality_v1,
        auto_constraints_eval=None, locked=set(), user_seam_ids=set(
            int(e) for e in spec.effective_seam_edges()),
        p5_extra={"user_seams": user_block, "layout_optimization": layout_report})


def _run_auto(bpy, contract, job: dict, out_dir: str, status_path: str, status: dict,
              ctx: dict) -> int:
    """``auto_generate``: the no-spec automatic core under locks/protections (work plan §3).

    A seam spec is OPTIONAL here and only supplies locked / protected edges; its absence
    is never ``needs_input`` (gate G2). Layout optimization does NOT run — the refinement
    loop's own final measure owns the shipped packing (work plan §5).
    """
    from artist_uv_agent.user_seams import load_user_seam_spec
    from chart_uv_agent.pipeline import run_chart_uv
    from chart_uv_agent.quality_profile import load_quality_profile
    from uv_agent.geometry.uv_correctness import compact_correctness

    obj, mesh, options = ctx["obj"], ctx["mesh"], ctx["options"]
    warnings = ctx["warnings"]
    fb = ctx["feedback_inputs"]

    profile = load_quality_profile(options["quality_profile"])

    locked: set[int] = set()
    protected: set[int] = set()
    seam_spec_label = None
    seam_spec_path = job.get("seam_spec")
    if seam_spec_path and os.path.exists(seam_spec_path):
        try:
            spec = load_user_seam_spec(seam_spec_path)
        except Exception as exc:  # noqa: BLE001 - malformed spec is a setup error
            return ctx["fail"]("invalid_seam_spec", f"could not load seam spec: {exc}")
        validation = _validate_spec_against_mesh(spec, mesh, obj.name)
        if validation["invalid_edges"] or validation["object_mismatch"]:
            msg = ("Seam spec contains edge ids that do not exist on the selected mesh."
                   if validation["invalid_edges"]
                   else f"Seam spec object {spec.object!r} does not match selected object {obj.name!r}.")
            return ctx["fail"]("invalid_seam_spec", msg,
                               invalid_edges=validation["invalid_edges"],
                               object_mismatch=validation["object_mismatch"])
        locked = set(int(e) for e in spec.effective_seam_edges())
        protected = set(int(e) for e in spec.effective_protected_edges())
        seam_spec_label = job.get("seam_spec_rel") or os.path.basename(seam_spec_path)

    user_seam_ids = set(locked)
    # G7: matching reviewer feedback unions into the spec's constraints.
    edge_count = mesh.edge_count
    def _in_range(ids):
        return {e for e in ids if 0 <= e < edge_count}

    locked |= _in_range(fb["locked"])
    protected |= _in_range(fb["protected"])
    preferred = _in_range(fb["preferred"])
    front_axis = fb["front_axis"] or str(job.get("front_axis") or "")

    # The automatic path reads no UV layer: the seam source IS the solver (gate G2).
    contract.write_json(os.path.join(out_dir, contract.SEAM_SOURCE_RESOLUTION_FILE),
                        {"policy": "auto_generate", "kind": "auto_solver",
                         "seam_spec": seam_spec_label, "uv_layer": None,
                         "locked_seam_count": len(locked),
                         "protected_count": len(protected),
                         "preferred_count": len(preferred)})

    budget = {k: options[k] for k in ("max_iterations", "max_candidates_per_round",
                                      "time_budget_s", "island_cap")}
    max_rounds = int(options["max_iterations"] or profile.max_iterations)
    print(f"generate_uv_from_seams: mode=auto_generate object={obj.name!r} "
          f"locked={len(locked)} protected={len(protected)} preferred={len(preferred)} "
          f"profile={profile.profile_id} seed={options['seed']} max_rounds={max_rounds}",
          flush=True)

    res = run_chart_uv(
        obj, mesh,
        forbidden_edges=protected,
        locked_seam_edges=locked,
        preferred_edges=preferred,
        front_axis=front_axis,
        quality_profile=options["quality_profile"],
        budget=budget,
        texture_size_px=options["texture_size_px"],
        margin_px=options["margin_px"],
        seed=options["seed"],
        max_rounds=max_rounds,
        margin=0.005,
        shading_snapshot_before=ctx.get("shading_before"))

    final_seams = res.get("seams", [])
    metrics = res.get("metrics", {})
    auto_constraints_eval = contract.evaluate_auto_constraints(res.get("auto_constraints"))
    for v in auto_constraints_eval["violations"]:
        warnings.append(f"auto constraints: {v.get('code')}")
    correctness_compact = compact_correctness(res.get("correctness") or {})

    seam_integrity_block = {
        "user_seam_count": len(user_seam_ids),
        "user_protected_count": len(protected),
        "final_seam_count": len(final_seams),
        "auto_added_seams": len(set(int(e) for e in final_seams) - locked),
        "mandatory_rule_enabled": bool(options.get("enforce_user_mandatory")),
        "mandatory_gate_enabled": bool(options.get("gate_user_mandatory")),
        "locked_missing_count": auto_constraints_eval["block"]["locked_missing_count"],
        "protected_cut_count": auto_constraints_eval["block"]["protected_cut_count"],
        "valid": bool(auto_constraints_eval["valid"]),
        "mode": contract.MODE_AUTO_GENERATE,
    }

    return _finish_run(
        bpy, contract, job, out_dir, status_path, status, ctx,
        res=res, final_seams=final_seams, metrics=metrics,
        seam_source=None, seam_spec_label=seam_spec_label,
        seam_integrity_block=seam_integrity_block,
        layout_optimization=contract.build_layout_optimization_block(None),
        selected_candidate_id=None,
        integrity=None, quality_v1=None,
        auto_constraints_eval=auto_constraints_eval,
        locked=locked, user_seam_ids=user_seam_ids,
        correctness_compact=correctness_compact,
        input_diagnostics=res.get("input_diagnostics"),
        p5_extra={})


def _finish_run(bpy, contract, job: dict, out_dir: str, status_path: str, status: dict,
                ctx: dict, *, res: dict, final_seams, metrics: dict,
                seam_source, seam_spec_label, seam_integrity_block: dict,
                layout_optimization: dict, selected_candidate_id,
                integrity, quality_v1, auto_constraints_eval,
                locked, user_seam_ids, p5_extra: dict,
                correctness_compact: dict | None = None,
                input_diagnostics: dict | None = None) -> int:
    """The shared post-engine path: identity, staging save, re-read audit, atomic
    promotion, artifacts, status classification, handoff and summary (G0/G1/G6/G7)."""
    from chart_uv_agent.reporting import (
        build_run_manifest, build_seam_overlay, git_head_sha, json_safe,
        write_anisotropy_heatmap_png, write_seam_overlay_png,
    )
    from uv_agent.blender.extract import extract_mesh_graph
    from uv_agent.blender.organic_unwrap import AI_UV_LAYER, read_uvmap
    from uv_agent.geometry.distortion_v2 import compact_distortion_v2, per_face_anisotropy
    from uv_agent.geometry.mesh_identity import compare_identity, mesh_identity
    from uv_agent.geometry.uv_correctness import compact_correctness

    mode = ctx["mode"]
    options = ctx["options"]
    obj, mesh = ctx["obj"], ctx["mesh"]
    warnings = ctx["warnings"]
    auto = mode == contract.MODE_AUTO_GENERATE
    run_id = job.get("run_id", "uv_run")
    model_label = _model_label(job)
    texture_size_px = int(options.get("texture_size_px") or 1024)
    margin_px = float(options.get("margin_px") or 0)

    artifacts_ok = True

    def _write(name: str, data) -> None:
        nonlocal artifacts_ok
        try:
            contract.write_json(os.path.join(out_dir, name), json_safe(data))
        except Exception as exc:  # noqa: BLE001 - a failed artifact blocks acceptance (G6)
            artifacts_ok = False
            warnings.append(f"{name} write failed: {exc}")

    # --- G0: mesh identity AFTER the engine -------------------------------
    identity_before = ctx["identity_before"]
    try:
        mesh_after = extract_mesh_graph(obj)
        identity_after = mesh_identity(mesh_after, model_path=job.get("model"))
    except Exception as exc:  # noqa: BLE001 - an unevaluatable identity is never "unchanged"
        warnings.append(f"post-run mesh identity failed: {exc}")
        identity_after = {}
    mesh_identity_block = compare_identity(identity_before, identity_after)
    if not mesh_identity_block["unchanged"]:
        warnings.append("mesh identity changed during the UV run: "
                        + ",".join(mesh_identity_block.get("differences") or []))
    _write(contract.MESH_IDENTITY_FILE, {"before": identity_before, "after": identity_after,
                                         "diff": mesh_identity_block})

    if correctness_compact is None:
        correctness_compact = compact_correctness(res.get("correctness") or {})
    distortion_v2 = res.get("distortion_v2") or {}
    mandatory = dict(res.get("mandatory_audit") or {})
    quality = res.get("quality")
    # G1 (topology/입력): the engine's input-defect diagnosis. It GATES an automatic run and
    # is report-only in preserve_existing (recorded in the summary, never a status change).
    if input_diagnostics is None:
        input_diagnostics = res.get("input_diagnostics")
    if input_diagnostics is not None and not input_diagnostics.get("ok"):
        warnings.append("input defects: " + ",".join(
            f"{k}={input_diagnostics.get(k)}" for k in
            ("non_manifold_edge_count", "zero_area_face_count",
             "input_defect_triangle_count", "isolated_vertex_count")
            if input_diagnostics.get(k)))

    # --- (a) staging save (gate G6) ---------------------------------------
    staging_dir = os.path.join(out_dir, STAGING_DIR)
    os.makedirs(staging_dir, exist_ok=True)
    staging_blend = os.path.join(staging_dir, contract.SELECTED_BLEND_FILE)
    run_blend = os.path.join(out_dir, contract.SELECTED_BLEND_FILE)
    blend_saved = False
    if bool(options.get("save_selected_blend", True)):
        try:
            bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(staging_blend), copy=True)
            blend_saved = os.path.exists(staging_blend)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"selected_uv.blend save failed: {exc}")
    if not blend_saved:
        artifacts_ok = False

    # --- (b) final re-read audit of the SAVED file (gate G1) --------------
    if blend_saved:
        audit = _final_reread_audit(
            bpy, staging_blend, out_dir, mesh, final_seams,
            object_data_name=obj.data.name, texture_size_px=texture_size_px,
            margin_px=margin_px, reported_only=not auto,
            shading_policy=str((res.get("quality_profile") or {}).get(
                "shading_uv_policy", "preserve")))
    else:
        audit = {"passed": False, "error": "selected_uv.blend was not saved"}
        if not auto:
            audit["reported_only"] = True
    if not audit.get("passed"):
        warnings.append("final re-read audit failed: "
                        + str(audit.get("error") or "see final_reread_audit"))

    # --- (c) atomic promotion into the run directory ----------------------
    if blend_saved:
        try:
            os.replace(staging_blend, run_blend)
        except Exception as exc:  # noqa: BLE001
            blend_saved = False
            artifacts_ok = False
            warnings.append(f"selected_uv.blend promotion failed: {exc}")
    try:
        if os.path.isdir(staging_dir) and not os.listdir(staging_dir):
            os.rmdir(staging_dir)
    except OSError:
        pass

    # --- evidence artifacts (G0/G1/G3/G5/G7) ------------------------------
    _write(contract.DISTORTION_V2_FILE, distortion_v2)
    _write(contract.CORRECTNESS_FILE, res.get("correctness") or {})
    _write(contract.QUALITY_PROFILE_FILE, res.get("quality_profile") or {})
    manifest = build_run_manifest(
        run_id=run_id, mode=mode, seed=options.get("seed"), options=options,
        quality_profile=res.get("quality_profile") or {},
        model_path=job.get("model"), model_rel=job.get("model_rel"),
        mesh_identity=identity_before,
        blender_version=getattr(bpy.app, "version_string", None),
        blender_build_hash=_blender_build_hash(bpy),
        python_version=sys.version, app_version=job.get("app_version"),
        code_sha=git_head_sha(_repo_root()), platform=sys.platform,
        started_at=status.get("started_at"),
        extra={"elapsed_s": round(time.monotonic() - ctx["started"], 3),
               "peak_memory_mb": _peak_memory_mb()})
    _write(contract.RUN_MANIFEST_FILE, manifest)

    if auto:
        _write(contract.FINAL_REREAD_AUDIT_FILE, audit)
        _write(contract.CANDIDATE_HISTORY_FILE, res.get("candidate_history") or [])
        # T7b evidence artifacts (G15): only written when the engine produced them,
        # so an older engine result keeps the previous artifact set.
        if res.get("quality_report") is not None:
            _write(contract.QUALITY_REPORT_FILE, res.get("quality_report"))
        if res.get("merge_back") is not None:
            _write(contract.MERGE_BACK_HISTORY_FILE, res.get("merge_back"))
        if res.get("shading") is not None:
            _write(contract.SHADING_POLICY_FILE, res.get("shading"))
        # G15: the overlay carries the shading-policy required edges, so the reviewer
        # legend can separate a `shading` cut from a discretionary one.
        overlay = build_seam_overlay(
            mesh, final_seams, res.get("seam_types") or {},
            history=res.get("history") or [],
            candidate_history=res.get("candidate_history") or [],
            conflicts=(res.get("constraints") or {}).get("conflicts") or [],
            object_name=obj.name, locked=locked, user=user_seam_ids,
            required=set(int(e) for e in
                         ((res.get("auto_constraints") or {}).get("required_seam_edges") or [])))
        _write(contract.SEAM_OVERLAY_FILE, overlay)
        try:
            write_seam_overlay_png(
                mesh, read_uvmap(obj, mesh, layer_name=AI_UV_LAYER), overlay,
                os.path.join(out_dir, contract.SEAM_OVERLAY_PNG_FILE),
                size=texture_size_px)
        except Exception as exc:  # noqa: BLE001 - an image artifact failure is a warning (plan §13)
            warnings.append(f"seam_overlay.png render failed: {exc}")
        try:
            heat_uvmap = read_uvmap(obj, mesh, layer_name=AI_UV_LAYER)
            write_anisotropy_heatmap_png(
                mesh, heat_uvmap, per_face_anisotropy(mesh, heat_uvmap),
                os.path.join(out_dir, contract.SELECTED_HEATMAP_ANISOTROPY_FILE),
                size=texture_size_px,
                vmax=float((res.get("quality_profile") or {}).get("anisotropy_max_max", 3.0)))
        except Exception as exc:  # noqa: BLE001 - an image artifact failure is a warning (plan §13)
            warnings.append(f"selected_heatmap_anisotropy.png render failed: {exc}")

    # --- normalized run reports (plan §3, §4.1; work plan §3) -------------
    gate = res.get("gate")
    p5 = {
        "engine": "chart", "mode": mode, "engine_mode": res.get("mode"),
        "chart_count": res.get("chart_count"),
        "metrics": metrics, "gate": gate.to_dict() if gate is not None else None,
        "gate_config": res.get("gate_config"),
        "distortion": res.get("distortion"), "conclusion": res.get("conclusion"),
        "mandatory_90_edges": res.get("mandatory_90_edges"),
        "mandatory_90_missing": res.get("mandatory_90_missing"),
        "mandatory_90_fold_edges": res.get("mandatory_90_fold_edges"),
        "mandatory_90_uv_unsplit": res.get("mandatory_90_uv_unsplit"),
        "initial_island_count": res.get("initial_island_count"),
        "final_island_count": res.get("final_island_count"),
        "seam_type_counts": res.get("seam_type_counts"),
        "history": res.get("history"),
        "termination": res.get("termination"), "quality": quality,
        "seam_count": len(final_seams), "seams": list(final_seams),
        "input_diagnostics": input_diagnostics,
    }
    p5.update(p5_extra)
    p5["auto_gate"] = None  # filled in below once the gate is evaluated

    # --- previews (plan §7) — AFTER the blend + heatmap, they mutate the UV
    if bool(options.get("render_previews", True)):
        warnings += _render_previews(
            obj, mesh, final_seams, out_dir,
            render_size=int(options.get("render_size_px", 900)),
            texture_size=int(options.get("texture_size_px", 1024)),
            checker_scale=float(options.get("checker_scale", 40.0)))

    # --- (d) status ------------------------------------------------------
    auto_gate = None
    if auto:
        auto_gate = contract.evaluate_auto_gate(
            mandatory=mandatory, quality=quality, correctness=correctness_compact,
            constraints=auto_constraints_eval, reread_audit=audit,
            input_diagnostics=input_diagnostics,
            fragmentation=res.get("fragmentation"),
            texel_density=res.get("texel_density"),
            islands_disagree=res.get("islands_disagree"),
            shading=res.get("shading"),
            merge_back=res.get("merge_back"))
        for code in auto_gate["failures"]:
            warnings.append(f"auto gate: {code}")
        for reason in auto_gate["invalid_reasons"]:
            warnings.append(f"auto gate invalid: {reason}")
        run_status = contract.classify_generate_status_v2(mode, auto_gate=auto_gate)
    else:
        run_status = contract.classify_generate_status_v2(
            mode, integrity=integrity, quality=quality_v1)
    p5["auto_gate"] = auto_gate
    _write(contract.P5_GATE_FILE, p5)
    seam_report = res.get("seam_report")
    if seam_report is not None:
        _write(contract.SEAM_REPORT_FILE, seam_report)

    # --- (e)/(f) acceptance, then handoff, then the final verdict (G6) ----
    identity_ok = bool(mesh_identity_block["unchanged"])
    provisional, reason = contract.finalize_acceptance(
        run_status, artifacts_saved=artifacts_ok, handoff_ok=True,
        mesh_identity_ok=identity_ok)

    handoff_ok = True
    selected_uv_model = None
    blend_out = job.get("selected_blend_out")
    if provisional == contract.STATUS_ACCEPTED and blend_out:
        handoff_ok = _handoff_blend(run_blend, blend_out, warnings)
        if handoff_ok:
            selected_uv_model = job.get("selected_blend_out_rel") or contract.SELECTED_UV_BLEND_REL

    final_status, reason = contract.finalize_acceptance(
        run_status, artifacts_saved=artifacts_ok,
        handoff_ok=(handoff_ok if blend_out else True), mesh_identity_ok=identity_ok)
    if final_status != contract.STATUS_ACCEPTED:
        selected_uv_model = None
    if reason:
        warnings.append(f"acceptance downgraded: {reason}")

    artifacts, art_warnings = contract.collect_generate_artifacts(out_dir)
    warnings = art_warnings + warnings

    summary = contract.build_generate_summary(
        run_id=run_id, status=final_status, model=model_label, object_name=obj.name,
        seam_spec=seam_spec_label, seam_source=seam_source, metrics=metrics,
        seam_integrity=seam_integrity_block,
        layout_optimization=layout_optimization,
        artifacts=artifacts, selected_candidate_id=selected_candidate_id,
        selected_uv_model=selected_uv_model, warnings=warnings,
        mode=mode,
        quality_profile=_profile_summary(res.get("quality_profile")),
        auto_gate=auto_gate,
        auto_constraints=(auto_constraints_eval["block"] if auto_constraints_eval else None),
        distortion_v2=compact_distortion_v2(distortion_v2) if distortion_v2 else None,
        correctness=correctness_compact,
        final_reread_audit=audit,
        mesh_identity=mesh_identity_block,
        termination=res.get("termination"),
        seam_length=res.get("seam_length"),
        mandatory_audit={**mandatory, "reported_only": not auto},
        input_diagnostics=(None if input_diagnostics is None
                           else {**input_diagnostics, "reported_only": not auto}),
        acceptance_reason=reason,
        fragmentation=(contract.compact_fragmentation_block(res.get("fragmentation"))
                       if res.get("fragmentation") is not None else None),
        texel_density=(contract.compact_texel_density_block(res.get("texel_density"))
                       if res.get("texel_density") is not None else None),
        packing=res.get("packing"),
        shading=res.get("shading"),
        merge_back=(contract.compact_merge_back_block(res.get("merge_back"))
                    if res.get("merge_back") is not None else None),
        quality_report_passed=(res.get("quality_report") or {}).get("passed"))
    summary["feedback_applied"] = ctx["feedback_applied"]
    summary["performance"] = {
        "elapsed_s": round(time.monotonic() - ctx["started"], 3),
        "peak_memory_mb": _peak_memory_mb(),
    }
    summary = json_safe(summary)
    contract.write_json(os.path.join(out_dir, contract.SUMMARY_FILE), summary)

    # Stable handoff copy for MVP 4/5 (one file to read, plan §9) — same atomic
    # .tmp + replace discipline as the blend (gate G6).
    if selected_uv_model is not None and job.get("selected_summary_out"):
        try:
            dest = job["selected_summary_out"]
            os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
            tmp = dest + ".tmp"
            contract.write_json(tmp, {**summary, "source_run_id": run_id})
            os.replace(tmp, dest)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"selected_uv_summary.json copy failed: {exc}")

    contract.finalize_status(status, status=final_status, artifacts=artifacts)
    contract.write_json(status_path, status)
    print(f"generate_uv_from_seams: {final_status} mode={mode} run={run_id} "
          f"object={obj.name!r} final_seams={len(final_seams)} "
          f"reread_audit_passed={audit.get('passed')} "
          f"mesh_identity_unchanged={identity_ok} reason={reason}", flush=True)
    # needs_user_review is a product outcome, not a process error — the real
    # verdict lives in status.json, so the process still exits 0 (plan §4.1, §6).
    return 0


def _handoff_blend(run_blend: str, dest: str, warnings: list[str]) -> bool:
    """Copy ``run_blend`` to ``dest`` through ``.tmp`` + sha256 compare + ``os.replace``.

    A failed copy/verify leaves the PREVIOUSLY approved file byte-identical (gate G6).
    """
    tmp = dest + ".tmp"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
        shutil.copyfile(run_blend, tmp)
        if _sha256_file(tmp) != _sha256_file(run_blend):
            warnings.append("selected_uv.blend handoff sha256 mismatch; previous file kept")
            os.remove(tmp)
            return False
        os.replace(tmp, dest)
        return True
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"selected_uv.blend handoff failed: {exc}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def main() -> int:
    _ensure_importable()
    import app_uv_generate_contract as contract  # type: ignore

    opts = _parse_args(sys.argv)
    if "job" not in opts:
        print("generate_uv_from_seams requires --job /abs/job.json", file=sys.stderr)
        return 2
    job = contract.read_json(opts["job"])
    command = job.get("command", contract.CMD_GENERATE_UV_FROM_SEAMS)
    if command != contract.CMD_GENERATE_UV_FROM_SEAMS:
        print(f"generate_uv_from_seams: unsupported command {command!r}", file=sys.stderr)
        if job.get("out"):
            contract.write_json(job["out"], contract.error_envelope(
                command or "unknown", f"unsupported command {command!r}", code="bad_command"))
        return 2

    out_dir = job.get("out_dir") or os.path.join("out", job.get("run_id", "uv_run"))
    os.makedirs(out_dir, exist_ok=True)
    status_path = os.path.join(out_dir, "status.json")

    # --- run mode (work plan §3, gate G2) — decided BEFORE anything runs ---
    raw_mode = job.get("mode") or (job.get("options") or {}).get("mode")
    mode_error = None
    try:
        mode = contract.resolve_mode(raw_mode)
    except ValueError as exc:
        mode = None
        mode_error = {"code": "unknown_mode", "message": str(exc)}

    status = contract.new_status(
        run_id=job.get("run_id", "uv_run"), command=command,
        status=contract.STATUS_RUNNING, input=_status_input(job, mode))
    contract.write_json(status_path, status)

    def _early_fail(error: dict, rc: int = 2) -> int:
        contract.finalize_status(status, status=contract.STATUS_FAILED, error=error)
        contract.write_json(status_path, status)
        print(f"generate_uv_from_seams: {error['message']}", file=sys.stderr)
        return rc

    if mode_error is not None:
        return _early_fail(mode_error)

    validation = contract.validate_mode_request(mode, job.get("options"))
    if not validation["ok"]:
        return _early_fail({
            "code": "contradictory_flags",
            "message": "; ".join(e.get("message", e.get("code", "")) for e in validation["errors"]),
            "details": {"errors": validation["errors"], "mode": mode},
        })
    options = contract.merge_options(job.get("options"), mode)

    model = job.get("model")
    if not model or not os.path.exists(model):
        return _early_fail({"code": "model_missing", "message": f"model not found: {model}"})

    ext = os.path.splitext(model)[1].lower()
    if ext not in contract.SUPPORTED_MODEL_EXTS:
        return _early_fail({
            "code": "unsupported_format",
            "message": f"unsupported format {ext!r}; supported: {', '.join(contract.SUPPORTED_MODEL_EXTS)}"})

    import bpy  # only available inside Blender

    try:
        _open_model(bpy, model)
    except Exception as exc:  # noqa: BLE001 - structured import failure
        return _early_fail({"code": "import_failed", "message": f"open/import failed: {exc}"}, rc=3)

    try:
        return _run_generate(bpy, contract, job, out_dir, status_path, status,
                             mode=mode, options=options)
    except Exception as exc:  # noqa: BLE001 - any failure becomes a structured status
        tb = traceback.format_exc()
        contract.finalize_status(status, status=contract.STATUS_FAILED,
                                 error={"code": "exception", "message": str(exc), "traceback": tb})
        contract.write_json(status_path, status)
        print(f"generate_uv_from_seams: failed: {exc}\n{tb}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
