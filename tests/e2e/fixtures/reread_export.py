"""Re-read an EXPORTED production file inside Blender and audit its UV (G9, G1).

Run inside Blender::

    blender --background --python tests/e2e/fixtures/reread_export.py -- \
        --model <exported .fbx/.obj/.glb/.gltf> --out <result.json>
        [--source <source .blend>]

G9 asks for "UV 없는 상태의 자동 생성 → 리뷰 → 최종 export 재읽기 성공": the evidence
is that the file the pipeline actually shipped still carries a usable UV layer, and
that G1's mandatory-90 rule (``mandatory_90_uv_unsplit == 0``) survives the export
round trip. This script therefore re-imports the exported file into an EMPTY scene
(never the source ``.blend``), rebuilds the mesh graph with the same
``extract_mesh_graph`` the solver uses, reads the ACTIVE UV layer with
``read_uvmap`` and writes::

    {"uv_layers": [...], "vertex_count": int, "face_count": int,
     "triangle_count": int, "mandatory_90_uv_unsplit": int,
     "mandatory_audit": {...}, "uv_bounds_ok": bool}

With ``--source`` the ORIGINAL mesh is re-opened afterwards and its
``source_face_count`` / ``source_triangle_count`` are recorded too, so a caller can
tell a lossless round trip from a legitimately triangulated one (GLB always
triangulates: 500 quads -> 968 tris). The source mesh also supplies the edge
correspondence G1 asks for ("artifact 재읽기에서 edge ID가 바뀌면 fingerprint/대응표를
통해 같은 기하 edge를 검사"): triangulation invents diagonals that never existed in the
original mesh, and such an edge is not subject to the 90-degree rule. Every fold edge of
the re-read mesh is therefore matched to the source by ``edge_geometry_key`` (6 digits)
and the source-matched audit is reported as ``mandatory_audit_source_matched`` next to
the unfiltered ``mandatory_audit``.

Any failure prints a traceback to stderr and exits 1 while still writing whatever
it knows into ``--out`` (``error`` key), so the host test reports raw evidence
instead of a bare non-zero exit.
"""

from __future__ import annotations

import json
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


def _ensure_importable() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


def _import_model(bpy, path: str) -> None:
    """Import ``path`` into an empty scene (same dispatch as the export worker)."""
    ext = os.path.splitext(path)[1].lower()
    try:
        bpy.ops.wm.read_homefile(use_empty=True)
    except Exception:  # noqa: BLE001 - best-effort; fall back to manual purge
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o, do_unlink=True)
    if ext == ".blend":
        bpy.ops.wm.open_mainfile(filepath=os.path.abspath(path))
    elif ext == ".fbx":
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


def _mesh_counts(obj) -> dict:
    """``face_count`` + ``triangle_count`` (fan triangulation: ``len(face) - 2``)."""
    polys = obj.data.polygons
    return {
        "face_count": len(polys),
        "triangle_count": int(sum(max(len(p.vertices) - 2, 0) for p in polys)),
    }


def _source_counts(bpy, source: str) -> tuple[dict, object]:
    """Re-open the SOURCE ``.blend`` (last, after the audit) and count its mesh.

    Returns the count block and the source mesh object, so the caller can build the
    geometric edge key set used to filter the re-read audit."""
    _import_model(bpy, source)
    obj = _pick_mesh(bpy)
    if obj is None:
        raise RuntimeError(f"no mesh object in source file: {source}")
    counts = _mesh_counts(obj)
    return ({"source_face_count": counts["face_count"],
             "source_triangle_count": counts["triangle_count"]}, obj)


def _pick_mesh(bpy):
    """The mesh object with the most polygons (importers may add empties/armatures)."""
    meshes = [o for o in bpy.data.objects if o.type == "MESH" and o.data is not None]
    if not meshes:
        return None
    return max(meshes, key=lambda o: len(o.data.polygons))


def main() -> int:
    out_path = None
    result: dict = {}
    try:
        opts = _parse_args(sys.argv)
        model = opts.get("model")
        out_path = opts.get("out")
        if not model or not out_path:
            raise SystemExit("usage: -- --model <path> --out <json> [--source <blend>]")

        _ensure_importable()
        import bpy

        from uv_agent.blender.extract import extract_mesh_graph
        from uv_agent.blender.organic_unwrap import read_uvmap
        from uv_agent.geometry.evaluation import mandatory_seam_uv_audit, uv_bounds_ok
        from uv_agent.geometry.mesh_identity import edge_geometry_key

        _import_model(bpy, model)
        obj = _pick_mesh(bpy)
        if obj is None:
            raise RuntimeError(f"no mesh object in re-read file: {model}")

        uv_layers = [layer.name for layer in obj.data.uv_layers]
        active = obj.data.uv_layers.active
        active_name = active.name if active is not None else None

        result = {
            "model": os.path.abspath(model),
            "format": os.path.splitext(model)[1].lower().lstrip("."),
            "object_name": obj.name,
            "uv_layers": uv_layers,
            "active_uv_layer": active_name,
            "vertex_count": len(obj.data.vertices),
            **_mesh_counts(obj),
        }

        if active_name is None:
            result["error"] = "re-read file has no UV layer"
            result["mandatory_90_uv_unsplit"] = None
            result["uv_bounds_ok"] = False
        else:
            mesh = extract_mesh_graph(obj)
            uvmap = read_uvmap(obj, mesh, layer_name=active_name)
            audit = mandatory_seam_uv_audit(mesh, uvmap)
            result["mandatory_audit"] = {
                "mandatory_90_fold_edges": int(audit["mandatory_90_fold_edges"]),
                "mandatory_90_uv_unsplit": int(audit["mandatory_90_uv_unsplit"]),
                "uv_unsplit_edge_ids": [int(i) for i in audit["uv_unsplit_edge_ids"]],
            }
            result["mandatory_90_uv_unsplit"] = int(audit["mandatory_90_uv_unsplit"])
            result["uv_bounds_ok"] = bool(uv_bounds_ok(uvmap))
            result["loop_count"] = len(mesh.loops)
            # Same fold predicate mandatory_seam_uv_audit uses, kept so the fold count
            # can be recomputed per geometric-key membership below.
            fold_edge_ids = [int(e.id) for e in mesh.edges
                             if len(e.face_ids) == 2 and e.dihedral_angle >= 90.0]
            reread_edge_keys = {eid: edge_geometry_key(mesh, eid, 6)
                                for eid in fold_edge_ids}

        # LAST: re-opening the source wipes the scene, so every read above is done.
        source = opts.get("source")
        if source:
            result["source"] = os.path.abspath(source)
            counts, source_obj = _source_counts(bpy, source)
            result.update(counts)
            if active_name is not None:
                source_mesh = extract_mesh_graph(source_obj)
                source_keys = {edge_geometry_key(source_mesh, int(e.id), 6)
                               for e in source_mesh.edges}
                matched_folds = [eid for eid in fold_edge_ids
                                 if reread_edge_keys[eid] in source_keys]
                matched_unsplit = [int(i) for i in audit["uv_unsplit_edge_ids"]
                                   if reread_edge_keys.get(
                                       int(i), edge_geometry_key(mesh, int(i), 6))
                                   in source_keys]
                result["mandatory_audit_source_matched"] = {
                    "mandatory_90_fold_edges": len(matched_folds),
                    "mandatory_90_uv_unsplit": len(matched_unsplit),
                    "uv_unsplit_edge_ids": matched_unsplit,
                    "excluded_non_source_edges": len(fold_edge_ids) - len(matched_folds),
                }

        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
        print("reread_export: {model} obj={obj!r} uv_layers={uv} faces={f} "
              "unsplit={u} bounds_ok={b}".format(
                  model=os.path.basename(model), obj=result.get("object_name"),
                  uv=uv_layers, f=result.get("face_count"),
                  u=result.get("mandatory_90_uv_unsplit"),
                  b=result.get("uv_bounds_ok")), flush=True)
        return 0
    except Exception:  # noqa: BLE001 - always leave structured evidence
        traceback.print_exc()
        if out_path:
            try:
                result["error"] = traceback.format_exc()
                with open(out_path, "w", encoding="utf-8") as fh:
                    json.dump(result, fh, indent=2)
            except Exception:  # noqa: BLE001
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
