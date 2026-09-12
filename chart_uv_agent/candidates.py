"""Seam cut CANDIDATES for one distortion round (UV_AUTOMATION §5 / Gate G4).

G4 requires that the existing normal-based ``split_chart`` is compared against at least
one other cut path, with the reason for the choice recorded, and that an *unwrap-only*
(no extra cut) candidate wins whenever it already meets quality. This module is the
candidate supplier; it proposes, it never applies:

``unwrap_only``    no new seam at all — re-unwrap/relax the island as it is.
``short_cut``      a minimal-cost cut from the island's worst-distortion region out to
                   the chart boundary and back out again (two legs ⇒ the disk splits).
``preferred_path`` the same construction, but the path cost also carries the visibility
                   (exposure) term, so the cut prefers less-visible / preferred edges.
``normal_split``   the legacy VSA 2-way normal split, kept as the baseline.

Every path cost comes from :meth:`SeamConstraints.edge_cost`, so a forbidden (protected,
non-mandatory) edge is ``inf`` and the route goes *around* the protected band — or no
candidate is produced at all when the region is fully enclosed. Nothing is silently
discarded: a candidate that would cut a protected edge is returned carrying
``notes["rejected"]`` so the caller can log it.

Pure Python / numpy on a :class:`MeshGraph` — no Blender. Fully deterministic (no seed).
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

import numpy as np

from chart_uv_agent.constraints import SeamConstraints, edge_exposure, edge_length
from chart_uv_agent.segmentation import (
    _chart_boundary_vertices,
    _vertex_edges,
    flood_charts,
    split_chart,
)
from uv_agent.geometry.mesh_graph import MeshGraph

#: Weight of the exposure term inside ``preferred_path_candidate``'s path cost.
EXPOSURE_COST_WEIGHT = 1.0

#: Candidate kinds, in the order :func:`generate_candidates` emits them.
KIND_ORDER = ("unwrap_only", "short_cut", "preferred_path", "normal_split")


@dataclass(frozen=True)
class SeamCandidate:
    """One proposed cut for ``target_island``.

    ``added_edges`` is the *auxiliary* seam set the candidate would add (empty for
    ``unwrap_only``). ``notes`` is out-of-band bookkeeping (never part of equality) and
    carries ``rejected`` / ``protected_cut`` when the candidate violates a constraint.
    """

    kind: str
    added_edges: frozenset[int]
    target_island: int
    reason: str
    seam_length: float
    exposure_cost: float
    notes: dict = field(default_factory=dict, compare=False)

    @property
    def rejected(self) -> str | None:
        return self.notes.get("rejected")

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "added_edges": sorted(self.added_edges),
            "target_island": self.target_island,
            "reason": self.reason,
            "seam_length": self.seam_length,
            "exposure_cost": self.exposure_cost,
            "notes": dict(self.notes),
        }


# ---------------------------------------------------------------- measures


def seam_length(mesh: MeshGraph, edges) -> float:
    """Total 3D length of ``edges`` — the "보조 seam 길이" G4/G8 report on."""
    return float(sum(edge_length(mesh, int(e)) for e in edges
                     if 0 <= int(e) < mesh.edge_count))


def bbox_diagonal(mesh: MeshGraph) -> float:
    """Model bounding-box diagonal, the normaliser for reported seam lengths (G8)."""
    if mesh.vertex_count == 0:
        return 0.0
    co = np.array([v.co for v in mesh.vertices], dtype=float)
    return float(np.linalg.norm(co.max(axis=0) - co.min(axis=0)))


def edges_cut_protected(mesh: MeshGraph, edges, constraints: SeamConstraints) -> list[int]:
    """The protected (forbidden) edges inside ``edges`` — G4's "protected edge 절개 0"."""
    return constraints.check_added(edges)["protected_cut"]


def rank_key(island_count: int, auxiliary_length: float, exposure: float) -> tuple:
    """Tie-break order for candidates of EQUAL quality (§5 / G4): fewest islands, then
    shortest auxiliary seam, then least visible. Rounded so float noise never decides."""
    return (int(island_count), round(float(auxiliary_length), 9), round(float(exposure), 9))


# ------------------------------------------------------------------ paths


def min_cost_path(mesh: MeshGraph, start: int, targets: set[int], chart_faces: set[int],
                  seams: set[int], cost_fn, blocked_vertices=frozenset()) -> list[int] | None:
    """Minimum-cost path of CHART-INTERIOR edges from ``start`` to any vertex in
    ``targets`` (Dijkstra over ``cost_fn(edge_id)``).

    Generalises ``segmentation._cut_path_to_boundary``: the cost function is injected
    (so exposure/preference can enter it) and ``blocked_vertices`` may be excluded, which
    is what makes the *second* leg of a cut disjoint from the first. Edges already in
    ``seams``, non-2-face edges, edges leaving ``chart_faces`` and ``inf``-cost (forbidden)
    edges are never traversed. Returns the path's edge ids (``[]`` when ``start`` is
    already a target) or ``None`` when no route exists.
    """
    if not targets:
        return None
    if start in targets:
        return []
    inf = float("inf")
    vert_edges = _vertex_edges(mesh)
    dist: dict[int, float] = {start: 0.0}
    prev_edge: dict[int, int] = {}
    pq: list[tuple[float, int]] = [(0.0, start)]
    while pq:
        d, v = heapq.heappop(pq)
        if d > dist.get(v, inf):
            continue
        if v != start and v in targets:
            path: list[int] = []
            cur = v
            while cur in prev_edge:
                eid = prev_edge[cur]
                path.append(eid)
                a, b = mesh.edges[eid].vertex_ids
                cur = a if b == cur else b
            path.reverse()
            return path
        for eid in vert_edges.get(v, ()):
            e = mesh.edges[eid]
            if eid in seams or len(e.face_ids) != 2:
                continue
            if e.face_ids[0] not in chart_faces or e.face_ids[1] not in chart_faces:
                continue
            a, b = e.vertex_ids
            nb = a if b == v else b
            if nb in blocked_vertices:
                continue
            cost = float(cost_fn(eid))
            if cost == inf or cost != cost:      # inf / NaN -> untraversable
                continue
            nd = d + cost
            if nd < dist.get(nb, inf) - 1e-15:
                dist[nb] = nd
                prev_edge[nb] = eid
                heapq.heappush(pq, (nd, nb))
    return None


def _path_vertices(mesh: MeshGraph, start: int, path_edges) -> list[int]:
    """Ordered vertices of a path (walking from ``start`` along ``path_edges``)."""
    verts = [start]
    cur = start
    for eid in path_edges:
        a, b = mesh.edges[eid].vertex_ids
        cur = b if a == cur else a
        verts.append(cur)
    return verts


# -------------------------------------------------------------- candidates


def unwrap_only_candidate(target_island: int) -> SeamCandidate:
    """The no-cut candidate (§5): if re-unwrapping alone meets quality, G4 forbids
    adding any seam at all."""
    return SeamCandidate(kind="unwrap_only", added_edges=frozenset(),
                         target_island=int(target_island), reason="unwrap_only",
                         seam_length=0.0, exposure_cost=0.0)


def _make(mesh: MeshGraph, kind: str, added, target_island: int, constraints: SeamConstraints,
          reason: str, notes: dict) -> SeamCandidate:
    added = frozenset(int(e) for e in added)
    check = constraints.check_added(added)
    notes = dict(notes)
    if not check["ok"]:
        notes["rejected"] = check["reason"]
        notes["protected_cut"] = check["protected_cut"]
    return SeamCandidate(kind=kind, added_edges=added, target_island=int(target_island),
                         reason=reason, seam_length=seam_length(mesh, added),
                         exposure_cost=constraints.exposure_cost(mesh, added), notes=notes)


def normal_split_candidate(mesh: MeshGraph, chart_faces, seams: set[int],
                           constraints: SeamConstraints, target_island: int) -> SeamCandidate | None:
    """The legacy VSA normal split, kept as the G4 baseline to compare against.

    A split that would cut a protected edge is NOT dropped: it comes back with
    ``notes["rejected"] = "protected_edge_cut"`` so the round can record why the
    baseline lost. ``None`` only when the chart cannot be split at all."""
    faces = sorted(set(int(f) for f in chart_faces))
    _, _, new_seams = split_chart(mesh, faces, set(seams))
    added = {int(e) for e in new_seams} - set(seams)
    if not added:
        return None
    return _make(mesh, "normal_split", added, target_island, constraints,
                 "normal_split", {"split_edges": len(added)})


def _stretch_of(face_stretch, fid: int) -> float:
    try:
        return float(face_stretch[fid])
    except (KeyError, IndexError, TypeError):
        return 0.0


def _high_distortion_faces(mesh: MeshGraph, chart_faces: set[int], face_stretch,
                           top_fraction: float) -> list[int]:
    """The worst-distortion faces of the chart, taking faces in descending stretch until
    their 3D area reaches ``top_fraction`` of the chart area (at least one face)."""
    faces = sorted(chart_faces, key=lambda f: (-_stretch_of(face_stretch, f), f))
    total = sum(float(mesh.faces[f].area_3d) for f in faces)
    budget = max(0.0, float(top_fraction)) * total
    out: list[int] = []
    acc = 0.0
    for f in faces:
        out.append(f)
        acc += float(mesh.faces[f].area_3d)
        if acc >= budget:
            break
    return out


def _two_leg_cut(mesh: MeshGraph, chart_faces: set[int], seams: set[int], region_faces,
                 bverts: set[int], cost_fn) -> tuple[set[int], float, int] | None:
    """From a vertex of the high-distortion region, cut out to the chart boundary twice
    (the second leg disjoint from the first) — two legs turn a disk into two disks.

    Returns ``(cut_edges, total_cost, start_vertex)`` for the cheapest start vertex, or
    ``None`` when no interior region vertex can reach the boundary twice (e.g. the region
    is fully ringed by protected edges)."""
    region_verts: set[int] = set()
    for f in region_faces:
        region_verts.update(mesh.faces[f].vertex_ids)
    best: tuple[float, set[int], int] | None = None
    for v in sorted(region_verts - bverts):
        p1 = min_cost_path(mesh, v, set(bverts), chart_faces, seams, cost_fn)
        if not p1:
            continue
        p1_verts = _path_vertices(mesh, v, p1)
        end1 = p1_verts[-1]
        blocked = frozenset(p1_verts) - {v}
        p2 = min_cost_path(mesh, v, set(bverts) - {end1}, chart_faces, seams, cost_fn,
                           blocked_vertices=blocked)
        if not p2:
            continue
        cut = set(p1) | set(p2)
        cost = float(sum(cost_fn(e) for e in cut))
        if best is None or (cost, sorted(cut)) < (best[0], sorted(best[1])):
            best = (cost, cut, v)
    if best is None:
        return None
    return best[1], best[0], best[2]


def _splits_in_two(mesh: MeshGraph, chart_faces: set[int], seams: set[int], cut: set[int]) -> bool:
    """Does ``seams | cut`` break exactly this chart into exactly two charts?"""
    charts = flood_charts(mesh, set(seams) | set(cut))
    ids = {i for i, fs in enumerate(charts) for f in fs if f in chart_faces}
    if len(ids) != 2:
        return False
    covered = set()
    for i in ids:
        covered.update(charts[i])
    return covered == set(chart_faces)


def short_cut_candidate(mesh: MeshGraph, chart_faces, seams: set[int],
                        constraints: SeamConstraints, face_stretch, target_island: int,
                        *, top_fraction: float = 0.2) -> SeamCandidate | None:
    """A minimal-cost cut from the island's worst-distortion region to the boundary (§5).

    Cheaper and far shorter than a whole-chart normal split, and it routes *around* any
    protected band because those edges cost ``inf``. ``None`` when the region cannot be
    reached/split (G4's "경로 없는 보호 영역" case — reported, never silently ignored)."""
    cfaces = set(int(f) for f in chart_faces)
    if len(cfaces) < 2:
        return None
    seams = set(seams)
    bverts = _chart_boundary_vertices(mesh, cfaces, seams)
    if not bverts:
        return None
    region = _high_distortion_faces(mesh, cfaces, face_stretch, top_fraction)

    def cost_fn(eid: int) -> float:
        return constraints.edge_cost(mesh, eid)

    found = _two_leg_cut(mesh, cfaces, seams, region, bverts, cost_fn)
    if found is None:
        return None
    cut, cost, start = found
    if not _splits_in_two(mesh, cfaces, seams, cut):
        return None
    return _make(mesh, "short_cut", cut, target_island, constraints, "short_cut",
                 {"region_faces": len(region), "path_cost": float(cost),
                  "start_vertex": int(start)})


def preferred_path_candidate(mesh: MeshGraph, chart_faces, seams: set[int],
                             constraints: SeamConstraints, face_stretch, target_island: int,
                             *, top_fraction: float = 0.2) -> SeamCandidate | None:
    """Same construction as :func:`short_cut_candidate`, but the path cost also carries
    the exposure term (edge length × visibility), so the route leaves the visible front
    and follows preferred / less-visible edges (§5, G4 "선호 경로 fixture").

    With no preference signal at all — empty ``preferred``, no ``front_axis``, no
    ``region_policy`` — this is by construction identical to ``short_cut``, so it returns
    ``None`` instead of a duplicate candidate."""
    if not constraints.preferred and not constraints.front_axis and constraints.region_policy is None:
        return None
    cfaces = set(int(f) for f in chart_faces)
    if len(cfaces) < 2:
        return None
    seams = set(seams)
    bverts = _chart_boundary_vertices(mesh, cfaces, seams)
    if not bverts:
        return None
    region = _high_distortion_faces(mesh, cfaces, face_stretch, top_fraction)
    front = constraints.front_vector()

    def cost_fn(eid: int) -> float:
        base = constraints.edge_cost(mesh, eid)
        if base == float("inf") or front is None:
            return base
        return base + EXPOSURE_COST_WEIGHT * edge_length(mesh, eid) * edge_exposure(mesh, eid, front)

    found = _two_leg_cut(mesh, cfaces, seams, region, bverts, cost_fn)
    if found is None:
        return None
    cut, cost, start = found
    if not _splits_in_two(mesh, cfaces, seams, cut):
        return None
    return _make(mesh, "preferred_path", cut, target_island, constraints, "preferred_path",
                 {"region_faces": len(region), "path_cost": float(cost),
                  "start_vertex": int(start)})


def generate_candidates(mesh: MeshGraph, charts, target_island: int, seams: set[int],
                        constraints: SeamConstraints, face_stretch, *,
                        max_candidates: int) -> list[SeamCandidate]:
    """All cut candidates for ``target_island``, deterministically ordered (§5 / G4).

    Order is ``unwrap_only → short_cut → preferred_path → normal_split`` so the no-cut
    option is always evaluated first; candidates with identical ``added_edges`` are
    de-duplicated (first kind wins), constraint-violating candidates are pushed to the
    END (kept for the history, never chosen ahead of a valid one), and the list is
    truncated to ``max_candidates``."""
    if max_candidates <= 0:
        return []
    chart_faces = set(charts[target_island])
    seams = set(seams)

    produced: list[SeamCandidate] = [unwrap_only_candidate(target_island)]
    for factory in (short_cut_candidate, preferred_path_candidate):
        cand = factory(mesh, chart_faces, seams, constraints, face_stretch, target_island)
        if cand is not None:
            produced.append(cand)
    ns = normal_split_candidate(mesh, chart_faces, seams, constraints, target_island)
    if ns is not None:
        produced.append(ns)

    seen: set[frozenset[int]] = set()
    unique: list[SeamCandidate] = []
    for c in produced:
        if c.added_edges in seen:
            continue
        seen.add(c.added_edges)
        unique.append(c)

    ordered = [c for c in unique if not c.rejected] + [c for c in unique if c.rejected]
    return ordered[:max_candidates]
