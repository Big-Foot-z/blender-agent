"""Re-open validation for exported production assets (MVP 5 plan §7, Session C).

Each exported FBX / OBJ / GLB / GLTF is re-imported into a fresh scene and probed:
mesh present, UV layer present, face/vertex snapshot, normals/material warnings
(:func:`reopen_and_validate`). A missing UV layer is ALWAYS a hard failure; object
/ material naming and vertex-count splits are format differences reported as
warnings, not failures (plan §7 tolerance policy).

On top of that presence check, Gate G13 re-AUDITS the shipped file against the
SOURCE mesh (:func:`reread_audit` / :func:`build_reread_audit`): correctness,
texel density, shading policy, the mandatory-90 rule restricted to real source
edges, and UV/vertex fingerprints that survive triangulation and face reordering.

``bpy`` is imported lazily, so the pure helpers (:func:`uv_layer_warnings`,
:func:`count_warnings`, :func:`normals_warning`, :func:`uv_corner_fingerprint`,
:func:`source_matched_fold_audit`, :func:`build_reread_audit`) unit-test without
Blender.
"""

from __future__ import annotations

import os

# Vertex count may legitimately differ after a format round-trip (UV/normal
# splits). Only warn when the re-opened vertex count drifts beyond this ratio of
# the source; never hard-fail on it (plan §7 "wildly different" tolerance).
VERTEX_DRIFT_WARN_RATIO = 0.5
# Face count should match unless triangulation was requested (plan §7).
FACE_DRIFT_WARN_RATIO = 0.02


# ---------------------------------------------------------------------------
# Pure helpers (no Blender) — the warning policy (plan §7 tolerance)
# ---------------------------------------------------------------------------
def uv_layer_warnings(uv_layers: list[str], expected_uv_layer: str | None, *, fmt: str) -> list[str]:
    """Warn (never fail) when the expected UV layer name is absent after export.

    Formats like OBJ/GLB do not preserve UV-layer *names*, so a renamed-but-present
    layer is a warning, not a failure (plan §7). Missing UV entirely is handled by
    the caller as a hard failure.
    """
    warnings: list[str] = []
    if expected_uv_layer and uv_layers and expected_uv_layer not in uv_layers:
        warnings.append(
            f"{fmt}: exported UV layer named {uv_layers[0]!r}, expected {expected_uv_layer!r} "
            f"({fmt.upper()} may not preserve UV layer names)")
    return warnings


def count_warnings(
    *,
    fmt: str,
    faces: int,
    vertices: int,
    source_faces: int | None,
    source_vertices: int | None,
    triangulated: bool,
) -> list[str]:
    """Warn on face/vertex drift vs the source snapshot (plan §7 tolerance).

    Face count is expected to change only when ``triangulated``; vertex count may
    drift due to format-specific splits. Both are warnings, never failures.
    """
    warnings: list[str] = []
    if source_faces and not triangulated:
        drift = abs(faces - source_faces) / max(source_faces, 1)
        if drift > FACE_DRIFT_WARN_RATIO:
            warnings.append(f"{fmt}: face count {faces} differs from source {source_faces}")
    if source_vertices:
        drift = abs(vertices - source_vertices) / max(source_vertices, 1)
        if drift > VERTEX_DRIFT_WARN_RATIO:
            warnings.append(f"{fmt}: vertex count {vertices} differs from source "
                            f"{source_vertices} (format-specific splits)")
    return warnings


def normals_warning(has_normals: bool, include_normals: bool, *, fmt: str) -> list[str]:
    """Warn when normals were requested but absent after re-open (plan §7)."""
    if include_normals and not has_normals:
        return [f"{fmt}: include_normals was requested but no normals found after re-open"]
    return []


# ---------------------------------------------------------------------------
# Re-open + import (plan §7 step 1)
# ---------------------------------------------------------------------------
def _reset_scene(bpy) -> None:
    """Wipe to an empty scene so the re-opened file is measured in isolation."""
    try:
        bpy.ops.wm.read_homefile(use_empty=True)
    except Exception:  # noqa: BLE001 - best-effort; remove objects manually
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o, do_unlink=True)


def _import(bpy, path: str, fmt: str) -> None:
    if fmt == "fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif fmt == "obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=path)
        else:  # pragma: no cover - legacy Blender
            bpy.ops.import_scene.obj(filepath=path)
    elif fmt in ("glb", "gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    else:
        raise ValueError(f"unsupported format for re-open: {fmt!r}")


def _mesh_has_normals(mesh) -> bool:
    """Best-effort: does the re-opened mesh carry usable normals?"""
    if getattr(mesh, "has_custom_normals", False):
        return True
    # Every Blender mesh with polygons has face normals; treat that as "normals
    # present". (Missing normals only realistically happens on a point cloud.)
    return len(mesh.polygons) > 0 and len(mesh.loops) > 0


def reopen_and_validate(
    bpy,
    path: str,
    fmt: str,
    *,
    expected_uv_layer: str | None = None,
    include_normals: bool = True,
    source_faces: int | None = None,
    source_vertices: int | None = None,
    triangulated: bool = False,
) -> dict:
    """Re-import ``path`` into a fresh scene and validate it (plan §7).

    Returns the per-format validation block::

        {"reopen_ok", "mesh_count", "faces", "vertices", "uv_layers",
         "has_uv", "has_normals", "warnings"}

    ``has_uv`` False is a hard failure for the caller (plan §7); everything else is
    advisory. A re-open exception yields ``reopen_ok=False`` with the error text in
    ``warnings`` so the worker never crashes on a bad file.
    """
    _reset_scene(bpy)
    try:
        _import(bpy, os.path.abspath(path), fmt)
    except Exception as exc:  # noqa: BLE001 - structured re-open failure
        return {
            "reopen_ok": False, "mesh_count": 0, "faces": 0, "vertices": 0,
            "uv_layers": [], "has_uv": False, "has_normals": False,
            "warnings": [f"{fmt}: re-open failed: {exc}"],
        }

    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    faces = sum(len(o.data.polygons) for o in meshes)
    vertices = sum(len(o.data.vertices) for o in meshes)
    uv_names: list[str] = []
    has_normals = False
    for o in meshes:
        for uv in o.data.uv_layers:
            if uv.name not in uv_names:
                uv_names.append(uv.name)
        if _mesh_has_normals(o.data):
            has_normals = True
    has_uv = len(uv_names) > 0

    warnings: list[str] = []
    if not meshes:
        warnings.append(f"{fmt}: no mesh object after re-open")
    if not has_uv:
        warnings.append(f"{fmt}: NO UV layer after re-open (hard failure)")
    warnings += uv_layer_warnings(uv_names, expected_uv_layer, fmt=fmt)
    warnings += normals_warning(has_normals, include_normals, fmt=fmt)
    warnings += count_warnings(fmt=fmt, faces=faces, vertices=vertices,
                               source_faces=source_faces, source_vertices=source_vertices,
                               triangulated=triangulated)

    return {
        "reopen_ok": len(meshes) > 0,
        "mesh_count": len(meshes),
        "faces": faces,
        "vertices": vertices,
        "uv_layers": uv_names,
        "has_uv": has_uv,
        "has_normals": has_normals,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Gate G13 / G15 — source-matched RE-READ audit of the shipped file
# ---------------------------------------------------------------------------
# ``reopen_and_validate`` only proves "a UV layer exists". G13 asks the harder
# question: is the UV the pipeline SHIPPED still the UV it approved? The audit
# below re-measures the exported file with the same engines the solver is gated
# on (correctness / texel density / shading policy / mandatory-90) and compares
# it to the SOURCE mesh through fingerprints that survive a format round trip.
#
# Every helper in this section is pure (MeshGraph + UVMap in, dict out) so the
# whole policy unit-tests without Blender; only :func:`reread_audit` touches bpy.

#: Failure codes :func:`build_reread_audit` can emit.
REREAD_FAILURE_CODES = (
    "uv_missing",
    "uv_not_finite",
    "uv_out_of_bounds",
    "correctness_failed",
    "mandatory_90_uv_unsplit",
    "texel_density_failed",
    "shading_policy_failed",
    "uv_fingerprint_mismatch",
    "vertex_fingerprint_mismatch",
    "topology_mismatch",
)

#: ``edge_geometry_key`` rounding used to match re-read edges back to the source.
EDGE_KEY_NDIGITS = 6
#: UV equality tolerance for "are these two loops welded across an edge?".
UV_WELD_TOL = 1e-5

# A shading snapshot the ``preserve`` policy reads as "unchanged". On a re-read
# there is no before/after pair to compare (the file is the only state we have),
# so the snapshot half of the policy is neutralised deliberately and only the
# sharp-edge UV audit + tangent basis are allowed to fail (G10 on re-read).
_NEUTRAL_SHADING_SNAPSHOT = {
    "sharp_edge_keys": [],
    "smooth_hash": "",
    "face_count": 0,
    "edge_count": 0,
    "smooth_face_count": 0,
}


def _round_value(value: float, ndigits: int) -> float:
    """Round and collapse -0.0 onto +0.0 so the digest is sign-stable."""
    v = round(float(value), ndigits)
    if v == 0.0:
        v = 0.0
    return v


def _sha256_rows(rows) -> str:
    import hashlib

    blob = "\n".join(rows).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def uv_corner_fingerprint(mesh, uvmap, *, ndigits: int = 5) -> dict:
    """SHA-256 over the sorted UNIQUE ``(x, y, z, u, v)`` corner set of a layout.

    One row per *distinct* (vertex position, UV) pair, so the digest is invariant
    under triangulation (a quad's 4 corners stay 4 rows when it becomes 2
    triangles) and under face / loop reordering — exactly the two things a format
    round trip is allowed to change. Moving a single UV changes it.
    """
    rows: set[tuple] = set()
    for loop in mesh.loops:
        x, y, z = mesh.vertices[loop.vertex_id].co
        u, v = uvmap.get(loop.index)
        rows.add((
            _round_value(x, ndigits), _round_value(y, ndigits), _round_value(z, ndigits),
            _round_value(u, ndigits), _round_value(v, ndigits),
        ))
    text = [" ".join(format(c, "." + str(ndigits) + "f") for c in row) for row in sorted(rows)]
    return {"sha256": _sha256_rows(text), "corner_count": len(rows)}


def vertex_position_fingerprint(mesh, *, ndigits: int = 4) -> str:
    """SHA-256 over the sorted unique rounded vertex positions of ``mesh``.

    Coarser than :func:`uv_corner_fingerprint` on purpose: importers split
    vertices for UV/normal seams, so only the position *set* survives a round
    trip — and that set is what must not move (G0 "UV work never touched the
    approved low-poly")."""
    rows = {tuple(_round_value(c, ndigits) for c in v.co) for v in mesh.vertices}
    text = [" ".join(format(c, "." + str(ndigits) + "f") for c in row) for row in sorted(rows)]
    return _sha256_rows(text)


def _loop_uv_by_face_vertex(mesh, uvmap) -> dict:
    return {(loop.face_id, loop.vertex_id): uvmap.get(loop.index) for loop in mesh.loops}


def _welded(fv_uv: dict, fa: int, fb: int, vertex_ids, *, tol: float = UV_WELD_TOL) -> bool:
    """Do faces ``fa`` / ``fb`` carry the SAME UV at every shared vertex?"""
    for vid in vertex_ids:
        ua = fv_uv.get((fa, vid))
        ub = fv_uv.get((fb, vid))
        if ua is None or ub is None:
            return False
        if abs(ua[0] - ub[0]) > tol or abs(ua[1] - ub[1]) > tol:
            return False
    return True


def uv_split_edge_ids(mesh, uvmap, *, tol: float = UV_WELD_TOL) -> list[int]:
    """2-face edges whose two faces carry DIFFERENT UVs — the seam set of a layout.

    A re-read file has no seam flags (no export format stores Blender's
    ``use_seam``), so the seam set the shading policy needs is recovered from the
    UVs themselves."""
    fv_uv = _loop_uv_by_face_vertex(mesh, uvmap)
    out: list[int] = []
    for e in mesh.edges:
        if len(e.face_ids) != 2:
            continue
        fa, fb = e.face_ids
        if not _welded(fv_uv, fa, fb, e.vertex_ids, tol=tol):
            out.append(int(e.id))
    return out


def source_matched_fold_audit(source_mesh, reread_mesh, reread_uvmap, *,
                              fold_angle: float = 90.0) -> dict:
    """Mandatory-90 audit on a re-read mesh, restricted to SOURCE edges (G1/G13).

    Edge IDs are renumbered by every importer and triangulation invents diagonals
    that never existed in the approved low-poly; such an edge is not subject to
    the 90-degree rule. Every fold edge of ``reread_mesh`` is therefore matched to
    ``source_mesh`` by :func:`~uv_agent.geometry.mesh_identity.edge_geometry_key`
    (geometry, not id) and only matched edges are checked.

    Returns ``fold_edges_checked`` (matched 2-face folds), ``mandatory_90_uv_unsplit``
    (matched folds welded in UV — a hard failure) and ``unmatched_fold_edges``
    (folds excluded because they are not source edges)."""
    from uv_agent.geometry.mesh_identity import edge_geometry_key

    source_keys = {edge_geometry_key(source_mesh, int(e.id), EDGE_KEY_NDIGITS)
                   for e in source_mesh.edges}
    fv_uv = _loop_uv_by_face_vertex(reread_mesh, reread_uvmap)

    checked = 0
    unmatched = 0
    unsplit: list[int] = []
    for e in reread_mesh.edges:
        if len(e.face_ids) != 2 or e.dihedral_angle < fold_angle:
            continue
        if edge_geometry_key(reread_mesh, int(e.id), EDGE_KEY_NDIGITS) not in source_keys:
            unmatched += 1
            continue
        checked += 1
        fa, fb = e.face_ids
        if _welded(fv_uv, fa, fb, e.vertex_ids):
            unsplit.append(int(e.id))
    return {
        "fold_angle": float(fold_angle),
        "fold_edges_checked": int(checked),
        "mandatory_90_uv_unsplit": len(unsplit),
        "uv_unsplit_edge_ids": unsplit,
        "unmatched_fold_edges": int(unmatched),
    }


def _triangle_count(mesh) -> int:
    return int(sum(max(len(f.vertex_ids) - 2, 0) for f in mesh.faces))


def _profile_value(profile: dict, key: str, default):
    value = (profile or {}).get(key, default)
    return default if value is None else value


def build_reread_audit(
    *,
    fmt: str,
    source_mesh,
    source_uvmap,
    reread_mesh,
    reread_uvmap,
    uv_layers: list[str],
    active_uv_layer,
    profile: dict,
    tangent_ok: bool | None,
    triangulated: bool,
    expected_uv_layer,
) -> dict:
    """Full re-read audit of one exported format (G13, ``export_reread_report.json``).

    Pure: ``profile`` is a plain dict (``texture_size_px``, ``margin_px``,
    ``border_margin_px``, ``texel_density_cv_max``,
    ``texel_density_outlier_tolerance``, ``texel_density_outlier_count_max``,
    ``shading_uv_policy``), so the whole policy is unit-tested without Blender.

    ``passed`` requires ALL of: a UV layer present, finite UVs, in-bounds UVs, the
    G1/G9 correctness audits, zero source-matched mandatory-90 welds, the G8 texel
    density audit, the G10 shading policy, a matching UV corner fingerprint, a
    matching vertex-position fingerprint and matching topology."""
    import numpy as np

    from uv_agent.geometry.evaluation import uv_bounds_ok, uv_islands_from_uvmap
    from uv_agent.geometry.shading_policy import evaluate_shading_policy
    from uv_agent.geometry.texel_density import compact_texel_density, evaluate_texel_density
    from uv_agent.geometry.uv_correctness import compact_correctness, evaluate_correctness

    texture_size_px = int(_profile_value(profile, "texture_size_px", 1024))
    margin_px = float(_profile_value(profile, "margin_px", 4.0))
    border_margin_px = float(_profile_value(profile, "border_margin_px", margin_px))
    policy = str(_profile_value(profile, "shading_uv_policy", "preserve"))

    uv_present = bool(uv_layers) and active_uv_layer is not None and len(reread_uvmap) > 0
    uv_finite = bool(np.all(np.isfinite(np.asarray(reread_uvmap.uv, dtype=float))))
    bounds = bool(uv_finite and uv_bounds_ok(reread_uvmap))

    islands = uv_islands_from_uvmap(reread_mesh, reread_uvmap)
    correctness = evaluate_correctness(
        reread_mesh, reread_uvmap, islands,
        texture_size_px=texture_size_px, margin_px=margin_px,
        border_margin_px=border_margin_px)
    mandatory = source_matched_fold_audit(source_mesh, reread_mesh, reread_uvmap)
    texel = compact_texel_density(evaluate_texel_density(
        reread_mesh, reread_uvmap, islands,
        texture_size_px=texture_size_px,
        cv_max=float(_profile_value(profile, "texel_density_cv_max", 0.15)),
        outlier_tolerance=float(_profile_value(profile, "texel_density_outlier_tolerance", 0.30)),
        outlier_count_max=int(_profile_value(profile, "texel_density_outlier_count_max", 0))))
    seams = uv_split_edge_ids(reread_mesh, reread_uvmap)
    shading_kwargs: dict = {}
    if policy == "preserve":
        shading_kwargs = {"before_snapshot": dict(_NEUTRAL_SHADING_SNAPSHOT),
                          "after_snapshot": dict(_NEUTRAL_SHADING_SNAPSHOT)}
    shading = evaluate_shading_policy(
        policy, mesh=reread_mesh, uvmap=reread_uvmap, seams=seams,
        tangent_ok=tangent_ok, **shading_kwargs)

    src_fp = uv_corner_fingerprint(source_mesh, source_uvmap)
    re_fp = uv_corner_fingerprint(reread_mesh, reread_uvmap)
    uv_fingerprint = {"source": src_fp, "reread": re_fp,
                      "match": bool(src_fp["sha256"] == re_fp["sha256"])}
    src_vfp = vertex_position_fingerprint(source_mesh)
    re_vfp = vertex_position_fingerprint(reread_mesh)
    vertex_fingerprint = {"source": src_vfp, "reread": re_vfp,
                          "match": bool(src_vfp == re_vfp)}

    source_faces = len(source_mesh.faces)
    reread_faces = len(reread_mesh.faces)
    source_triangles = _triangle_count(source_mesh)
    reread_triangles = _triangle_count(reread_mesh)
    # GLB/GLTF always triangulate, and so does ``triangulate=True``: then the
    # invariant is the TRIANGLE count, not the face count.
    tri_expected = bool(triangulated) or fmt in ("glb", "gltf")
    topology_match = bool(source_faces == reread_faces
                          or (tri_expected and source_triangles == reread_triangles))
    topology = {
        "source_faces": int(source_faces),
        "reread_faces": int(reread_faces),
        "source_triangles": int(source_triangles),
        "reread_triangles": int(reread_triangles),
        "triangulation_expected": tri_expected,
        "match": topology_match,
    }

    checks = [
        {"name": "uv_present", "passed": bool(uv_present)},
        {"name": "uv_finite", "passed": bool(uv_finite)},
        {"name": "uv_bounds", "passed": bool(bounds)},
        {"name": "correctness", "passed": bool(correctness["passed"])},
        {"name": "mandatory_90", "passed": bool(mandatory["mandatory_90_uv_unsplit"] == 0),
         "value": int(mandatory["mandatory_90_uv_unsplit"]), "limit": 0},
        {"name": "texel_density", "passed": bool(texel["passed"])},
        {"name": "shading_policy", "passed": bool(shading["passed"])},
        {"name": "uv_fingerprint", "passed": bool(uv_fingerprint["match"])},
        {"name": "vertex_fingerprint", "passed": bool(vertex_fingerprint["match"])},
        {"name": "topology", "passed": bool(topology_match)},
    ]
    codes = {
        "uv_present": "uv_missing",
        "uv_finite": "uv_not_finite",
        "uv_bounds": "uv_out_of_bounds",
        "correctness": "correctness_failed",
        "mandatory_90": "mandatory_90_uv_unsplit",
        "texel_density": "texel_density_failed",
        "shading_policy": "shading_policy_failed",
        "uv_fingerprint": "uv_fingerprint_mismatch",
        "vertex_fingerprint": "vertex_fingerprint_mismatch",
        "topology": "topology_mismatch",
    }
    failures = [codes[c["name"]] for c in checks if not c["passed"]]

    return {
        "format": fmt,
        "uv_layers": list(uv_layers or []),
        "active_uv_layer": active_uv_layer,
        "expected_uv_layer": expected_uv_layer,
        "uv_layer_warnings": uv_layer_warnings(list(uv_layers or []), expected_uv_layer, fmt=fmt),
        "uv_present": bool(uv_present),
        "uv_finite": bool(uv_finite),
        "bounds": bool(bounds),
        "island_count": len(islands),
        "correctness": compact_correctness(correctness),
        "mandatory": mandatory,
        "texel_density": texel,
        "shading": shading,
        "uv_fingerprint": uv_fingerprint,
        "vertex_fingerprint": vertex_fingerprint,
        "topology": topology,
        "profile": {
            "texture_size_px": texture_size_px,
            "margin_px": margin_px,
            "border_margin_px": border_margin_px,
            "shading_uv_policy": policy,
        },
        "checks": checks,
        "passed": not failures,
        "failures": failures,
    }


def reread_audit(
    bpy,
    path: str,
    fmt: str,
    *,
    source_mesh,
    source_uvmap,
    profile: dict,
    triangulated: bool = False,
    expected_uv_layer: str | None = None,
) -> dict:
    """Re-import ``path`` into an EMPTY scene and run :func:`build_reread_audit` (G13).

    The exported file is measured on its own terms: the mesh graph is rebuilt with
    the same ``extract_mesh_graph`` the solver uses, the ACTIVE UV layer is read
    back with ``read_uvmap``, and ``calc_tangents`` is attempted so a broken
    tangent basis is real evidence rather than an untested assumption.

    Never raises: any exception becomes ``{"passed": False, "error": ...}`` so the
    export worker always ships a structured report."""
    from uv_agent.blender.extract import extract_mesh_graph
    from uv_agent.blender.organic_unwrap import read_uvmap

    try:
        _reset_scene(bpy)
        _import(bpy, os.path.abspath(path), fmt)

        meshes = [o for o in bpy.data.objects if o.type == "MESH" and o.data is not None]
        if not meshes:
            return {"format": fmt, "passed": False, "failures": ["no_mesh"],
                    "error": f"{fmt}: no mesh object after re-import"}
        obj = max(meshes, key=lambda o: len(o.data.polygons))

        uv_layers = [layer.name for layer in obj.data.uv_layers]
        active = obj.data.uv_layers.active
        active_name = active.name if active is not None else None
        if active_name is None:
            return {"format": fmt, "passed": False, "failures": ["uv_missing"],
                    "uv_layers": uv_layers, "active_uv_layer": None,
                    "error": f"{fmt}: re-read file has no UV layer"}

        mesh = extract_mesh_graph(obj)
        uvmap = read_uvmap(obj, mesh, layer_name=active_name)

        tangent_ok: bool | None
        tangent_error = None
        try:
            obj.data.calc_tangents(uvmap=active_name)
            tangent_ok = True
        except Exception as exc:  # noqa: BLE001 - a failed tangent basis is evidence
            tangent_ok = False
            tangent_error = str(exc)

        audit = build_reread_audit(
            fmt=fmt, source_mesh=source_mesh, source_uvmap=source_uvmap,
            reread_mesh=mesh, reread_uvmap=uvmap, uv_layers=uv_layers,
            active_uv_layer=active_name, profile=profile, tangent_ok=tangent_ok,
            triangulated=triangulated, expected_uv_layer=expected_uv_layer)
        audit["object_name"] = obj.name
        audit["vertex_count"] = len(obj.data.vertices)
        if tangent_error:
            audit["tangent_error"] = tangent_error
        return audit
    except Exception as exc:  # noqa: BLE001 - structured re-read failure
        return {"format": fmt, "passed": False, "failures": ["reread_error"],
                "error": f"{fmt}: re-read audit failed: {exc}"}
