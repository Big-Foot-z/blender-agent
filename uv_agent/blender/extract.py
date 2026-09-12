"""Extract a :class:`MeshGraph` from a Blender object (plan §7.1).

Only runs inside Blender (imports ``bmesh`` lazily). Produces the same graph
structure as the synthetic fixtures so the rest of the engine is identical
whether the mesh came from Blender or a test.
"""

from __future__ import annotations

import math

from uv_agent.geometry.mesh_graph import (
    Edge,
    Face,
    Loop,
    MeshGraph,
    Vertex,
    snap_fold_angle,
)


def extract_mesh_graph(obj, *, apply_modifiers: bool = False) -> MeshGraph:
    """Build a MeshGraph from a Blender mesh object.

    Computes per-edge dihedral angle, sharp/seam flags, boundary/non-manifold
    status, and per-face normal/area/material - matching :meth:`MeshGraph.from_faces`.
    """
    import bmesh  # lazy: only available inside Blender

    bm = bmesh.new()
    try:
        depsgraph = None
        if apply_modifiers:
            import bpy  # noqa: F401

            depsgraph = _get_depsgraph()
        if depsgraph is not None:
            bm.from_object(obj, depsgraph)
        else:
            bm.from_mesh(obj.data)

        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        vertices = [Vertex(i, tuple(v.co)) for i, v in enumerate(bm.verts)]

        loops: list[Loop] = []
        faces: list[Face] = []
        # (face index, vertex index) -> our loop index, so Blender's loop triangles
        # can be re-expressed in our loop indexing (plan §4, Gate G3).
        loop_lookup: dict[tuple[int, int], int] = {}
        for f in bm.faces:
            loop_indices = []
            for loop in f.loops:
                loops.append(Loop(index=len(loops), vertex_id=loop.vert.index, face_id=f.index))
                loop_indices.append(len(loops) - 1)
                loop_lookup[(f.index, loop.vert.index)] = len(loops) - 1
            faces.append(
                Face(
                    id=f.index,
                    vertex_ids=[v.index for v in f.verts],
                    loop_indices=loop_indices,
                    edge_ids=[e.index for e in f.edges],
                    normal=tuple(f.normal),
                    area_3d=float(f.calc_area()),
                    material_index=int(f.material_index),
                )
            )

        _fill_face_triangles(bm, faces, loop_lookup)

        edges: list[Edge] = []
        edge_index: dict[tuple[int, int], int] = {}
        for e in bm.edges:
            a, b = e.verts[0].index, e.verts[1].index
            key = (a, b) if a < b else (b, a)
            face_ids = [f.index for f in e.link_faces]
            is_boundary = len(face_ids) == 1
            is_non_manifold = not e.is_manifold and not is_boundary
            # bmesh edge angle is in radians; 0 means flat (coplanar faces).
            try:
                dihedral = snap_fold_angle(math.degrees(e.calc_face_angle(0.0)))
            except (ValueError, RuntimeError):
                dihedral = 0.0
            edges.append(
                Edge(
                    id=e.index,
                    vertex_ids=key,
                    face_ids=face_ids,
                    dihedral_angle=float(dihedral),
                    is_boundary=is_boundary,
                    is_non_manifold=is_non_manifold,
                    is_sharp=not e.smooth,
                    is_seam=bool(e.seam),
                )
            )
            edge_index[key] = e.index

        return MeshGraph(
            object_id=obj.name,
            vertices=vertices,
            edges=edges,
            faces=faces,
            loops=loops,
            _edge_index=edge_index,
        )
    finally:
        bm.free()


def _fill_face_triangles(bm, faces: list[Face], loop_lookup: dict[tuple[int, int], int]) -> None:
    """Populate ``Face.triangles`` from Blender's own loop triangulation.

    Using ``bm.calc_loop_triangles()`` keeps our evaluation consistent with what
    Blender actually renders/unwraps, and avoids phantom triangles on concave
    n-gons. On failure the faces are left empty so ``MeshGraph.face_triangles``
    falls back to its own ear clipping."""
    try:
        loop_tris = bm.calc_loop_triangles()
    except (AttributeError, ValueError, RuntimeError):
        return

    by_face: dict[int, list[tuple[int, int, int]]] = {}
    for tri in loop_tris:
        try:
            fidx = tri[0].face.index
            triple = tuple(loop_lookup[(fidx, lp.vert.index)] for lp in tri)
        except (KeyError, AttributeError, IndexError):
            continue
        by_face.setdefault(fidx, []).append(triple)

    for f in faces:
        tris = by_face.get(f.id)
        if tris:
            f.triangles = tris


def _get_depsgraph():
    import bpy

    return bpy.context.evaluated_depsgraph_get()
