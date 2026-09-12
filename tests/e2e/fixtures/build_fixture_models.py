"""Build the deterministic baseline fixture models + manifest (G0, work plan §7).

Run inside Blender:

    blender --background --python tests/e2e/fixtures/build_fixture_models.py -- --out <dir>

Every fixture named by the work plan §7 "필수 fixture" list is rebuilt from
scratch in an empty scene, normalized (object ``Fixture`` / mesh ``FixtureMesh``,
transforms applied) and written twice: ``<name>.blend`` and ``<name>.obj``. The
run then emits ``<out>/fixture_manifest.json`` carrying, per fixture, the file
SHA-256s, the vertex/edge/face/loop counts and a topology+material
``mesh_fingerprint`` — the reproducible-baseline evidence G0 asks for.

The fingerprint algorithm lives in ``fingerprint_from_arrays()``, which has NO
``bpy`` dependency, so a worker can recompute the exact same digest from its own
mesh arrays. ``mesh_fingerprint_from_mesh()`` is the ``bpy`` adapter.

Any failure prints a traceback to stderr and exits 1.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import traceback

# ---------------------------------------------------------------------------
# Fingerprint (bpy-free core so other modules can reuse the exact algorithm)
# ---------------------------------------------------------------------------
_COORD_NDIGITS = 6
_COORD_EPS = 0.5 * 10 ** (-_COORD_NDIGITS)


def _fmt_coord(value: float) -> str:
    v = round(float(value), _COORD_NDIGITS)
    if abs(v) < _COORD_EPS:  # collapse -0.0 / +0.0 to one spelling
        v = 0.0
    return f"{v:.{_COORD_NDIGITS}f}"


def _canonical_face(face) -> tuple:
    """Rotate a face loop to start at its lowest vertex index, keeping winding.

    Blender does not guarantee a stable *storage* order for polygons (or for the
    starting corner of a loop) across runs, but the geometry is the same. Rotating
    each loop and sorting the face list makes the digest depend on topology only.
    """
    idx = [int(i) for i in face]
    if not idx:
        return ()
    start = idx.index(min(idx))
    return tuple(idx[start:] + idx[:start])


def fingerprint_from_arrays(coords, faces, mats) -> str:
    """SHA-256 over a deterministic serialization of geometry + material ids.

    ``coords``: iterable of ``(x, y, z)``; ``faces``: iterable of vertex-index
    lists; ``mats``: per-face material index (same order/length as ``faces``).
    Coordinates are rounded to 6 decimals so float noise below that does not
    change the digest; faces are canonicalized (loop rotated to its lowest vertex
    index, then the face list sorted) so a different polygon storage order does
    not change it either.
    """
    coords = list(coords)
    faces = [list(f) for f in faces]
    mats = list(mats)
    if len(mats) != len(faces):
        raise ValueError(f"mats/faces length mismatch: {len(mats)} vs {len(faces)}")
    parts = [f"nv {len(coords)}", f"nf {len(faces)}"]
    for c in coords:
        parts.append("v " + " ".join(_fmt_coord(x) for x in c))
    rows = sorted((_canonical_face(f), int(m)) for f, m in zip(faces, mats))
    for face, mat in rows:
        parts.append("f " + " ".join(str(i) for i in face) + " m" + str(mat))
    blob = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def mesh_fingerprint_from_mesh(mesh_data) -> str:
    """``bpy`` adapter for :func:`fingerprint_from_arrays`."""
    coords = [tuple(v.co) for v in mesh_data.vertices]
    faces = [list(p.vertices) for p in mesh_data.polygons]
    mats = [int(p.material_index) for p in mesh_data.polygons]
    return fingerprint_from_arrays(coords, faces, mats)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Scene helpers (bpy)
# ---------------------------------------------------------------------------
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


def _reset_scene(bpy) -> None:
    bpy.ops.wm.read_homefile(use_empty=True)


def _active_mesh_object(bpy):
    obj = bpy.context.view_layer.objects.active
    if obj is None or obj.type != "MESH":
        obj = next((o for o in bpy.data.objects if o.type == "MESH"), None)
    return obj


def _select_only(bpy, obj) -> None:
    for o in bpy.data.objects:
        o.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def _normalize(bpy, obj):
    """Rename to the shared fixture identity and bake rotation/scale in."""
    obj.name = "Fixture"
    obj.data.name = "FixtureMesh"
    _select_only(bpy, obj)
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    return obj


def _new_mesh_object(bpy, bm):
    """Create object ``Fixture`` from a finished bmesh in the current scene."""
    import bmesh  # noqa: F401  (imported by callers; kept for symmetry)

    mesh = bpy.data.meshes.new("FixtureMesh")
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    obj = bpy.data.objects.new("Fixture", mesh)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    return obj


# ---------------------------------------------------------------------------
# Fixture builders — each returns (object, notes)
# ---------------------------------------------------------------------------
def build_plane_grid(bpy):
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=8, y_subdivisions=8, size=2)
    return _active_mesh_object(bpy), {"kind": "flat_plane", "x_subdivisions": 8,
                                      "y_subdivisions": 8, "size": 2}


def build_cube(bpy):
    bpy.ops.mesh.primitive_cube_add(size=2)
    return _active_mesh_object(bpy), {"kind": "hard_surface", "size": 2}


def build_bevel_cube(bpy):
    bpy.ops.mesh.primitive_cube_add(size=2)
    obj = _active_mesh_object(bpy)
    _select_only(bpy, obj)
    mod = obj.modifiers.new(name="Bevel", type="BEVEL")
    mod.width = 0.1
    mod.segments = 2
    bpy.ops.object.modifier_apply(modifier=mod.name)
    return obj, {"kind": "bevel_hard_surface", "bevel_width": 0.1, "bevel_segments": 2}


def build_cylinder_capped(bpy):
    bpy.ops.mesh.primitive_cylinder_add(vertices=16, radius=1, depth=2, end_fill_type="NGON")
    return _active_mesh_object(bpy), {"kind": "capped_cylinder", "vertices": 16,
                                      "end_fill_type": "NGON"}


def build_uv_sphere(bpy):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8)
    return _active_mesh_object(bpy), {"kind": "sphere", "segments": 16, "ring_count": 8}


def build_torus(bpy):
    bpy.ops.mesh.primitive_torus_add(major_segments=24, minor_segments=12)
    return _active_mesh_object(bpy), {"kind": "torus", "major_segments": 24,
                                      "minor_segments": 12}


def build_suzanne(bpy):
    bpy.ops.mesh.primitive_monkey_add()
    return _active_mesh_object(bpy), {"kind": "smooth_organic", "source": "primitive_monkey_add"}


def build_concave_plate(bpy):
    """An L-shaped concave 8-gon extruded 0.2 thick (>= 2 concave n-gon faces)."""
    import bmesh
    from mathutils import Vector

    profile = [
        (0.0, 0.0), (2.0, 0.0), (2.0, 1.0), (1.5, 1.0),
        (1.5, 1.5), (1.0, 1.5), (1.0, 2.0), (0.0, 2.0),
    ]
    bm = bmesh.new()
    verts = [bm.verts.new((x, y, 0.0)) for x, y in profile]
    bm.verts.ensure_lookup_table()
    bottom = bm.faces.new(verts)
    ret = bmesh.ops.extrude_face_region(bm, geom=[bottom])
    new_verts = [g for g in ret["geom"] if isinstance(g, bmesh.types.BMVert)]
    bmesh.ops.translate(bm, vec=Vector((0.0, 0.0, 0.2)), verts=new_verts)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.normal_update()
    obj = _new_mesh_object(bpy, bm)
    return obj, {"kind": "concave_ngon_plate", "profile_xy": profile, "thickness": 0.2,
                 "concave_ngon_faces": 2}


def build_angle_boundary(bpy):
    """Three detached 2-quad pieces folded at 89.9 / 90.0 / 90.1 degrees (G1 boundary)."""
    import bmesh

    bm = bmesh.new()
    folds = []
    for idx, deg in enumerate((89.9, 90.0, 90.1)):
        ox = idx * 4.0
        rad = math.radians(deg)
        dx, dz = math.cos(rad), math.sin(rad)
        p0 = bm.verts.new((ox + 0.0, 0.0, 0.0))
        p1 = bm.verts.new((ox + 1.0, 0.0, 0.0))
        p2 = bm.verts.new((ox + 1.0, 1.0, 0.0))
        p3 = bm.verts.new((ox + 0.0, 1.0, 0.0))
        p4 = bm.verts.new((ox + 1.0 + dx, 0.0, dz))
        p5 = bm.verts.new((ox + 1.0 + dx, 1.0, dz))
        bm.faces.new((p0, p1, p2, p3))
        bm.faces.new((p1, p4, p5, p2))
        folds.append({
            "angle_deg": deg,
            "fold_edge_verts": [
                [round(c, 6) for c in tuple(p1.co)],
                [round(c, 6) for c in tuple(p2.co)],
            ],
        })
    bm.verts.ensure_lookup_table()
    bm.normal_update()
    obj = _new_mesh_object(bpy, bm)
    return obj, {"kind": "angle_boundary", "piece_offset_x": 4.0, "folds": folds}


def build_degenerate_input(bpy):
    """Cube + a zero-area collinear triangle + a 3-face non-manifold edge."""
    import bmesh

    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=2.0)

    # Zero-area triangle: three collinear vertices, its own component.
    d0 = bm.verts.new((5.0, 0.0, 0.0))
    d1 = bm.verts.new((6.0, 0.0, 0.0))
    d2 = bm.verts.new((7.0, 0.0, 0.0))
    bm.faces.new((d0, d1, d2))

    # Non-manifold edge: one shared edge carried by three quads.
    a = bm.verts.new((10.0, 0.0, 0.0))
    b = bm.verts.new((10.0, 1.0, 0.0))
    wings = [(1.0, 0.0), (0.0, 1.0), (-1.0, 0.0)]
    for wx, wz in wings:
        w0 = bm.verts.new((10.0 + wx, 0.0, wz))
        w1 = bm.verts.new((10.0 + wx, 1.0, wz))
        bm.faces.new((a, w0, w1, b))
    bm.verts.ensure_lookup_table()
    bm.normal_update()
    obj = _new_mesh_object(bpy, bm)
    return obj, {"kind": "degenerate_and_nonmanifold",
                 "zero_area_triangle_verts": [[5.0, 0.0, 0.0], [6.0, 0.0, 0.0], [7.0, 0.0, 0.0]],
                 "non_manifold_edge_verts": [[10.0, 0.0, 0.0], [10.0, 1.0, 0.0]],
                 "non_manifold_edge_face_count": 3}


def build_protected_path(bpy):
    """A gently curved 12x12 grid — the smooth surface a protected edge run crosses."""
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=12, y_subdivisions=12, size=2)
    obj = _active_mesh_object(bpy)
    for v in obj.data.vertices:
        x, y = v.co.x, v.co.y
        v.co.z = 0.3 * math.sin(x * 1.5) * math.cos(y * 1.5)
    obj.data.update()
    return obj, {"kind": "protected_path_surface", "x_subdivisions": 12, "y_subdivisions": 12,
                 "displacement": "z = 0.3*sin(x*1.5)*cos(y*1.5)",
                 "protected_edges": "chosen by the test via edge id"}


FIXTURE_BUILDERS = [
    ("plane_grid", build_plane_grid),
    ("cube", build_cube),
    ("bevel_cube", build_bevel_cube),
    ("cylinder_capped", build_cylinder_capped),
    ("uv_sphere", build_uv_sphere),
    ("torus", build_torus),
    ("suzanne", build_suzanne),
    ("concave_plate", build_concave_plate),
    ("angle_boundary", build_angle_boundary),
    ("degenerate_input", build_degenerate_input),
    ("protected_path", build_protected_path),
]


# ---------------------------------------------------------------------------
# Export + manifest
# ---------------------------------------------------------------------------
def _export_obj(bpy, obj, path: str) -> None:
    _select_only(bpy, obj)
    bpy.ops.wm.obj_export(
        filepath=path,
        export_selected_objects=True,
        export_uv=False,
        export_materials=False,
        export_triangulated_mesh=False,
        apply_modifiers=False,
    )


def _build_one(bpy, name, builder, out_dir: str) -> dict:
    _reset_scene(bpy)
    obj, notes = builder(bpy)
    if obj is None:
        raise RuntimeError(f"fixture {name!r} produced no mesh object")
    obj = _normalize(bpy, obj)
    mesh = obj.data

    blend_name = f"{name}.blend"
    obj_name = f"{name}.obj"
    blend_path = os.path.join(out_dir, blend_name)
    obj_path = os.path.join(out_dir, obj_name)

    bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(blend_path), copy=True)
    _export_obj(bpy, obj, os.path.abspath(obj_path))

    return {
        "name": name,
        "blend": blend_name,
        "obj": obj_name,
        "blend_sha256": sha256_file(blend_path),
        "obj_sha256": sha256_file(obj_path),
        "vertex_count": len(mesh.vertices),
        "edge_count": len(mesh.edges),
        "face_count": len(mesh.polygons),
        "loop_count": len(mesh.loops),
        "mesh_fingerprint": mesh_fingerprint_from_mesh(mesh),
        "notes": notes,
    }


def _build_hash(bpy) -> str:
    raw = getattr(bpy.app, "build_hash", "")
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def main() -> int:
    import bpy  # only available inside Blender

    opts = _parse_args(sys.argv)
    out_dir = opts.get("out")
    if not out_dir:
        print("build_fixture_models requires --out <dir>", file=sys.stderr)
        return 1
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    fixtures = []
    for name, builder in FIXTURE_BUILDERS:
        entry = _build_one(bpy, name, builder, out_dir)
        fixtures.append(entry)
        print(f"build_fixture_models: {name} v={entry['vertex_count']} "
              f"f={entry['face_count']} fp={entry['mesh_fingerprint'][:12]}", flush=True)

    manifest = {
        "generated_by": "tests/e2e/fixtures/build_fixture_models.py",
        "blender_version": bpy.app.version_string,
        "blender_build_hash": _build_hash(bpy),
        "platform": sys.platform,
        "fixtures": fixtures,
    }
    manifest_path = os.path.join(out_dir, "fixture_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=False)
        fh.write("\n")
    print(f"build_fixture_models: wrote {manifest_path} ({len(fixtures)} fixtures)", flush=True)
    return 0


if __name__ == "__main__":
    try:
        _rc = main()
    except Exception:  # noqa: BLE001 - any failure is a hard build failure
        traceback.print_exc(file=sys.stderr)
        _rc = 1
    sys.exit(_rc)
