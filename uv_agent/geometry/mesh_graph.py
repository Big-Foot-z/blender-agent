"""Structured mesh representation shared by the AI agent and the geometry solver.

This mirrors plan §7.1 ``extract_mesh_graph``: vertices, edges, faces and loops
with adjacency, dihedral angles and boundary / non-manifold detection.

The :meth:`MeshGraph.from_faces` builder lets us construct a graph from raw
polygon data (no Blender required), which is what the synthetic fixtures and the
unit tests use. The Blender adapter (:mod:`uv_agent.blender.extract`) produces an
equivalent graph from a ``bmesh``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np

Vec3 = tuple[float, float, float]

#: Numeric tolerance (degrees) for snapping a dihedral angle onto the fold angle.
#: Gate G1: only |angle - 90| <= 1e-5 is treated as exactly 90 degrees.
FOLD_SNAP_EPS_DEG = 1e-5


def snap_fold_angle(angle_deg: float, fold_angle: float = 90.0) -> float:
    """Snap ``angle_deg`` to ``fold_angle`` when within :data:`FOLD_SNAP_EPS_DEG`.

    Floating point normals make an exact 90 degree fold come out as e.g.
    89.99999999999999, which would silently miss the ">= 90 degrees is a
    mandatory seam" rule. Anything further away than the epsilon is returned
    untouched (89.9 stays 89.9, 90.1 stays 90.1)."""
    a = float(angle_deg)
    if abs(a - fold_angle) <= FOLD_SNAP_EPS_DEG:
        return float(fold_angle)
    return a


@dataclass
class Vertex:
    id: int
    co: Vec3  # 3D position


@dataclass
class Loop:
    """A per-face corner. UV coordinates live per-loop in Blender, so this is the
    atomic unit the solver writes to."""

    index: int
    vertex_id: int
    face_id: int


@dataclass
class Face:
    id: int
    vertex_ids: list[int]
    loop_indices: list[int]
    edge_ids: list[int]
    normal: Vec3
    area_3d: float
    material_index: int = 0
    #: Real triangulation of this face as loop-index triples. Empty means "not
    #: computed yet"; use :meth:`MeshGraph.face_triangles` which fills it lazily.
    #: Kept last with a default so existing positional construction still works.
    triangles: list[tuple[int, int, int]] = field(default_factory=list)


@dataclass
class Edge:
    id: int
    vertex_ids: tuple[int, int]
    face_ids: list[int]
    dihedral_angle: float  # degrees, angle between adjacent face normals (0 = flat)
    is_boundary: bool
    is_non_manifold: bool
    is_sharp: bool = False
    is_seam: bool = False


def _newell_normal_and_area(coords: np.ndarray) -> tuple[np.ndarray, float]:
    """Newell's method: robust polygon normal + planar area for any (planar) n-gon."""
    n = np.zeros(3)
    k = len(coords)
    for i in range(k):
        cur = coords[i]
        nxt = coords[(i + 1) % k]
        n[0] += (cur[1] - nxt[1]) * (cur[2] + nxt[2])
        n[1] += (cur[2] - nxt[2]) * (cur[0] + nxt[0])
        n[2] += (cur[0] - nxt[0]) * (cur[1] + nxt[1])
    length = float(np.linalg.norm(n))
    if length < 1e-12:
        return np.array([0.0, 0.0, 1.0]), 0.0
    return n / length, length / 2.0


def _angle_between(n1: np.ndarray, n2: np.ndarray) -> float:
    d = float(np.clip(np.dot(n1, n2), -1.0, 1.0))
    return math.degrees(math.acos(d))


def _fan_triangulate(n: int) -> list[tuple[int, int, int]]:
    return [(0, i, i + 1) for i in range(1, n - 1)]


def _signed_area_2d(points2d: np.ndarray) -> float:
    x = points2d[:, 0]
    y = points2d[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _point_in_triangle(p, a, b, c, eps: float) -> bool:
    """Inclusive point-in-triangle test used to reject non-ears."""
    d1 = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    d2 = (c[0] - b[0]) * (p[1] - b[1]) - (c[1] - b[1]) * (p[0] - b[0])
    d3 = (a[0] - c[0]) * (p[1] - c[1]) - (a[1] - c[1]) * (p[0] - c[0])
    has_neg = (d1 < -eps) or (d2 < -eps) or (d3 < -eps)
    has_pos = (d1 > eps) or (d2 > eps) or (d3 > eps)
    return not (has_neg and has_pos)


def ear_clip_triangulate(points2d: np.ndarray) -> list[tuple[int, int, int]]:
    """Ear-clipping triangulation of a simple 2D polygon.

    Returns ``n - 2`` triangles as triples of *local* vertex indices into
    ``points2d`` (polygon order). Unlike a naive fan this never emits a triangle
    that lies outside a concave polygon (plan §4 / Gate G3). Degenerate
    (zero area) polygons fall back to a fan instead of raising."""
    pts = np.asarray(points2d, dtype=float)
    n = int(pts.shape[0])
    if n < 3:
        return []
    if n == 3:
        return [(0, 1, 2)]

    area = _signed_area_2d(pts)
    scale = float(np.max(np.abs(pts))) if pts.size else 1.0
    scale = scale if scale > 0.0 else 1.0
    if abs(area) <= 1e-14 * scale * scale:
        return _fan_triangulate(n)

    ccw = area > 0.0
    eps = 1e-12 * scale * scale
    remaining = list(range(n))
    tris: list[tuple[int, int, int]] = []

    guard = 0
    max_guard = n * n + 16
    while len(remaining) > 3 and guard < max_guard:
        guard += 1
        m = len(remaining)
        clipped = False
        for i in range(m):
            ia = remaining[(i - 1) % m]
            ib = remaining[i]
            ic = remaining[(i + 1) % m]
            a, b, c = pts[ia], pts[ib], pts[ic]
            cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
            if ccw:
                if cross <= eps:
                    continue
            else:
                if cross >= -eps:
                    continue
            contains = False
            for j in remaining:
                if j in (ia, ib, ic):
                    continue
                if _point_in_triangle(pts[j], a, b, c, eps):
                    contains = True
                    break
            if contains:
                continue
            tris.append((ia, ib, ic))
            remaining.pop(i)
            clipped = True
            break
        if not clipped:
            break

    if len(remaining) == 3:
        tris.append((remaining[0], remaining[1], remaining[2]))
    elif len(remaining) > 3:
        # Could not find an ear (self-intersecting / degenerate input): fan the rest.
        for k in range(1, len(remaining) - 1):
            tris.append((remaining[0], remaining[k], remaining[k + 1]))
    return tris


@dataclass
class MeshGraph:
    object_id: str
    vertices: list[Vertex]
    edges: list[Edge]
    faces: list[Face]
    loops: list[Loop]
    _edge_index: dict[tuple[int, int], int] = field(default_factory=dict, repr=False)
    _face_adjacency_cache: dict[int, list[tuple[int, int]]] | None = field(
        default=None, repr=False, compare=False)

    # -- lookups -----------------------------------------------------------
    @property
    def vertex_count(self) -> int:
        return len(self.vertices)

    @property
    def edge_count(self) -> int:
        return len(self.edges)

    @property
    def face_count(self) -> int:
        return len(self.faces)

    def vertex_co(self, vertex_id: int) -> np.ndarray:
        return np.asarray(self.vertices[vertex_id].co, dtype=float)

    def face(self, face_id: int) -> Face:
        return self.faces[face_id]

    def edge(self, edge_id: int) -> Edge:
        return self.edges[edge_id]

    def loop(self, loop_index: int) -> Loop:
        return self.loops[loop_index]

    def edge_key(self, a: int, b: int) -> int:
        return self._edge_index[(a, b) if a < b else (b, a)]

    def face_adjacency(self) -> dict[int, list[tuple[int, int]]]:
        """face_id -> list of (neighbor_face_id, shared_edge_id).

        Lazily cached: this is rebuilt O(E) on every call, and the segmentation / seam
        passes call it thousands of times (``split_chart``, ``flood_charts``, the diskify
        loop). The graph is used immutably after construction (seam edits live in external
        ``set``s, never on the mesh), and callers only READ the returned dict, so memoising
        and sharing one instance is safe and a large speed-up on real assets."""
        if self._face_adjacency_cache is None:
            adj: dict[int, list[tuple[int, int]]] = {f.id: [] for f in self.faces}
            for e in self.edges:
                if len(e.face_ids) == 2:
                    a, b = e.face_ids
                    adj[a].append((b, e.id))
                    adj[b].append((a, e.id))
            self._face_adjacency_cache = adj
        return self._face_adjacency_cache

    def face_triangles(self, face_id: int) -> list[tuple[int, int, int]]:
        """Real triangulation of a face as loop-index triples (plan §4, Gate G3).

        Triangles come from Blender's ``calc_loop_triangles`` when the graph was
        extracted from Blender; otherwise they are computed lazily here by ear
        clipping the polygon projected onto its Newell normal plane, so concave
        n-gons never produce phantom triangles outside the polygon. The result is
        cached on the ``Face``."""
        f = self.faces[face_id]
        if f.triangles:
            return f.triangles

        loop_indices = f.loop_indices
        n = len(loop_indices)
        if n < 3:
            f.triangles = []
            return f.triangles
        if n == 3:
            f.triangles = [(loop_indices[0], loop_indices[1], loop_indices[2])]
            return f.triangles

        coords = np.asarray(
            [self.vertex_co(self.loops[li].vertex_id) for li in loop_indices], dtype=float
        )
        normal, area = _newell_normal_and_area(coords)
        if area <= 0.0:
            local = _fan_triangulate(n)
        else:
            # Orthonormal basis on the polygon plane.
            ref = np.array([1.0, 0.0, 0.0])
            if abs(float(np.dot(normal, ref))) > 0.9:
                ref = np.array([0.0, 1.0, 0.0])
            u = np.cross(normal, ref)
            u /= float(np.linalg.norm(u))
            v = np.cross(normal, u)
            rel = coords - coords[0]
            points2d = np.column_stack((rel @ u, rel @ v))
            local = ear_clip_triangulate(points2d)

        f.triangles = [
            (loop_indices[a], loop_indices[b], loop_indices[c]) for a, b, c in local
        ]
        return f.triangles

    # -- construction ------------------------------------------------------
    @classmethod
    def from_faces(
        cls,
        object_id: str,
        vertices: Sequence[Vec3],
        faces: Sequence[Sequence[int]],
        *,
        material_indices: Sequence[int] | None = None,
        sharp_edge_keys: Iterable[tuple[int, int]] | None = None,
        seam_edge_keys: Iterable[tuple[int, int]] | None = None,
    ) -> "MeshGraph":
        verts = [Vertex(i, (float(x), float(y), float(z))) for i, (x, y, z) in enumerate(vertices)]
        coords_all = np.asarray(vertices, dtype=float)

        sharp = {_norm_key(*k) for k in (sharp_edge_keys or [])}
        seams = {_norm_key(*k) for k in (seam_edge_keys or [])}

        # First pass: build faces, loops, and discover edges.
        loops: list[Loop] = []
        face_objs: list[Face] = []
        edge_keys: dict[tuple[int, int], int] = {}
        edge_face_ids: list[list[int]] = []
        edge_key_list: list[tuple[int, int]] = []

        def get_edge(a: int, b: int) -> int:
            key = _norm_key(a, b)
            idx = edge_keys.get(key)
            if idx is None:
                idx = len(edge_key_list)
                edge_keys[key] = idx
                edge_key_list.append(key)
                edge_face_ids.append([])
            return idx

        for fid, vids in enumerate(faces):
            vids = list(vids)
            loop_indices: list[int] = []
            for vid in vids:
                loops.append(Loop(index=len(loops), vertex_id=vid, face_id=fid))
                loop_indices.append(len(loops) - 1)
            edge_ids: list[int] = []
            for i in range(len(vids)):
                eidx = get_edge(vids[i], vids[(i + 1) % len(vids)])
                edge_ids.append(eidx)
                edge_face_ids[eidx].append(fid)
            normal, area = _newell_normal_and_area(coords_all[vids])
            mat = int(material_indices[fid]) if material_indices is not None else 0
            face_objs.append(
                Face(
                    id=fid,
                    vertex_ids=vids,
                    loop_indices=loop_indices,
                    edge_ids=edge_ids,
                    normal=(float(normal[0]), float(normal[1]), float(normal[2])),
                    area_3d=float(area),
                    material_index=mat,
                )
            )

        # Second pass: finalize edges with dihedral / boundary / manifold flags.
        edge_objs: list[Edge] = []
        for eidx, key in enumerate(edge_key_list):
            fids = edge_face_ids[eidx]
            is_boundary = len(fids) == 1
            is_non_manifold = len(fids) > 2
            if len(fids) == 2:
                n1 = np.asarray(face_objs[fids[0]].normal)
                n2 = np.asarray(face_objs[fids[1]].normal)
                dihedral = snap_fold_angle(_angle_between(n1, n2))
            else:
                dihedral = 0.0
            edge_objs.append(
                Edge(
                    id=eidx,
                    vertex_ids=key,
                    face_ids=list(fids),
                    dihedral_angle=float(dihedral),
                    is_boundary=is_boundary,
                    is_non_manifold=is_non_manifold,
                    is_sharp=key in sharp,
                    is_seam=key in seams,
                )
            )

        return cls(
            object_id=object_id,
            vertices=verts,
            edges=edge_objs,
            faces=face_objs,
            loops=loops,
            _edge_index=dict(edge_keys),
        )

    # -- serialization (plan §7.1 export format) ---------------------------
    def to_dict(self) -> dict:
        return {
            "object_id": self.object_id,
            "vertex_count": self.vertex_count,
            "edge_count": self.edge_count,
            "face_count": self.face_count,
            "vertices": [{"vertex_id": v.id, "co": list(v.co)} for v in self.vertices],
            "faces": [
                {
                    "face_id": f.id,
                    "vertex_ids": f.vertex_ids,
                    "loop_indices": f.loop_indices,
                    "edge_ids": f.edge_ids,
                    "normal": list(f.normal),
                    "area_3d": f.area_3d,
                    "material_index": f.material_index,
                }
                for f in self.faces
            ],
            "edges": [
                {
                    "edge_id": e.id,
                    "vertex_ids": list(e.vertex_ids),
                    "face_ids": e.face_ids,
                    "dihedral_angle": e.dihedral_angle,
                    "is_boundary": e.is_boundary,
                    "is_non_manifold": e.is_non_manifold,
                    "is_sharp": e.is_sharp,
                    "is_seam": e.is_seam,
                }
                for e in self.edges
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MeshGraph":
        vertices = [tuple(v["co"]) for v in data["vertices"]]
        faces = [f["vertex_ids"] for f in data["faces"]]
        mats = [f.get("material_index", 0) for f in data["faces"]]
        sharp = [tuple(e["vertex_ids"]) for e in data["edges"] if e.get("is_sharp")]
        seams = [tuple(e["vertex_ids"]) for e in data["edges"] if e.get("is_seam")]
        return cls.from_faces(
            data["object_id"],
            vertices,
            faces,
            material_indices=mats,
            sharp_edge_keys=sharp,
            seam_edge_keys=seams,
        )


def _norm_key(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)
