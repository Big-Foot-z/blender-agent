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

#: Why a cut is being proposed at all (W2 §4.3 reason codes).
CUT_REASONS = ("distortion_repair", "correctness_repair")

#: 3D aspect ratio at/above which a predicted sub-chart counts as a sliver.
SLIVER_ASPECT = 8.0

#: Keys of the :func:`candidate_cost_breakdown` dict that are summed as penalties.
COST_PENALTY_KEYS = ("normalized_seam_length", "visible_surface_penalty",
                     "smooth_surface_penalty", "small_island_creation_penalty",
                     "sliver_creation_penalty", "protected_region_penalty")

#: Keys of the :func:`candidate_cost_breakdown` dict that are summed as bonuses.
COST_BONUS_KEYS = ("hidden_back_bonus", "concave_crease_bonus",
                   "material_boundary_bonus", "user_preferred_bonus")


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
    #: WHY the cut is proposed — ``"distortion_repair"`` or ``"correctness_repair"``
    #: (:data:`CUT_REASONS`). Independent of ``reason``, which names the *path* kind.
    cut_reason: str = "distortion_repair"
    #: The :func:`candidate_cost_breakdown` dict for this candidate (out-of-band).
    cost: dict = field(default_factory=dict, compare=False)

    @property
    def rejected(self) -> str | None:
        return self.notes.get("rejected")

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "added_edges": sorted(self.added_edges),
            "target_island": self.target_island,
            "reason": self.reason,
            "cut_reason": self.cut_reason,
            "seam_length": self.seam_length,
            "exposure_cost": self.exposure_cost,
            "cost": dict(self.cost),
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


def rank_key(island_count: int, auxiliary_length: float, exposure: float,
             total_cost: float = 0.0) -> tuple:
    """Tie-break order for candidates of EQUAL quality (§5 / G4, W2 §4.3).

    Lexicographic: **P2** island count → **P3** auxiliary seam length → **P4** visible
    (exposure) cost → the candidate's total cut cost
    (:func:`candidate_cost_breakdown`'s ``total``); anything still tied is left to the
    caller's own stable candidate order. Every float is rounded so float noise never
    decides. ``total_cost`` defaults to ``0.0`` so pre-existing 3-argument callers keep
    their exact previous ordering.
    """
    return (int(island_count), round(float(auxiliary_length), 9),
            round(float(exposure), 9), round(float(total_cost), 9))


# ------------------------------------------------------------- cut cost (W2 §4.3)


def _chart_mean_normal(mesh: MeshGraph, faces) -> np.ndarray:
    """Area-weighted mean normal of ``faces`` (``+Z`` when it degenerates)."""
    acc = np.zeros(3)
    for fid in faces:
        f = mesh.faces[fid]
        acc += np.asarray(f.normal, dtype=float) * float(f.area_3d)
    n = float(np.linalg.norm(acc))
    if n < 1e-12:
        return np.array([0.0, 0.0, 1.0])
    return acc / n


def _plane_axes(normal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """A deterministic orthonormal basis of the plane perpendicular to ``normal``."""
    n = np.asarray(normal, dtype=float)
    ln = float(np.linalg.norm(n))
    n = np.array([0.0, 0.0, 1.0]) if ln < 1e-12 else n / ln
    ref = np.array([1.0, 0.0, 0.0]) if abs(float(n[0])) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = np.cross(n, ref)
    u = u / float(np.linalg.norm(u))
    v = np.cross(n, u)
    return u, v


def _sub_chart_aspect(mesh: MeshGraph, faces, u: np.ndarray, v: np.ndarray) -> float:
    """3D "aspect" of a predicted sub-chart: the ratio of the LONGEST PCA extent to the
    shortest, measured on the chart's mean-normal plane (``inf`` when it collapses to a
    line — the degenerate sliver)."""
    vids = sorted({int(vid) for fid in faces for vid in mesh.faces[fid].vertex_ids})
    if len(vids) < 3:
        return float("inf")
    p = np.array([mesh.vertices[i].co for i in vids], dtype=float)
    q = np.stack([p @ u, p @ v], axis=1)
    q = q - q.mean(axis=0)
    cov = (q.T @ q) / float(len(q))
    _, vecs = np.linalg.eigh(cov)
    ext = np.ptp(q @ vecs, axis=0)
    lo, hi = float(ext.min()), float(ext.max())
    if lo <= 1e-12:
        return float("inf")
    return hi / lo


def _predicted_sub_charts(mesh: MeshGraph, chart_faces: set[int], seams: set[int],
                          added: set[int]) -> list[list[int]]:
    """The sub-charts ``chart_faces`` would break into under ``seams | added``, via
    :func:`flood_charts` restricted to the chart (sorted, deterministic)."""
    if not chart_faces:
        return []
    out: list[list[int]] = []
    for comp in flood_charts(mesh, set(seams) | set(added)):
        inside = sorted(f for f in comp if f in chart_faces)
        if inside:
            out.append(inside)
    return out


def candidate_cost_breakdown(mesh: MeshGraph, chart_faces, seams, added_edges,
                             constraints: SeamConstraints, *, fold_angle: float = 90.0,
                             smooth_angle: float = 45.0,
                             tiny_face_fraction: float = 0.05) -> dict:
    """The itemised cut cost of adding ``added_edges`` to ``chart_faces`` (W2 §4.3).

    Every value is a JSON-safe float; lengths are normalised by the model bounding-box
    diagonal so the numbers are scale-free (``0.0`` when the diagonal is 0). Penalties:

    ``normalized_seam_length``       added 3D seam length / bbox diagonal.
    ``visible_surface_penalty``      ``constraints.exposure_cost`` / diagonal. **0** with
                                     no ``front_axis`` — visibility neutral, the engine
                                     never guesses which way the asset faces.
    ``smooth_surface_penalty``       Σ length × ``max(0, 1 − dihedral/smooth_angle)``: a
                                     FLAT edge costs most, an edge at/above
                                     ``smooth_angle`` costs nothing (cutting across a
                                     smooth surface is what shows).
    ``small_island_creation_penalty``how many predicted sub-charts hold less than
                                     ``tiny_face_fraction`` of the chart's 3D area.
    ``sliver_creation_penalty``      how many predicted sub-charts have ≤ 2 faces or a 3D
                                     aspect ≥ :data:`SLIVER_ASPECT`.
    ``protected_region_penalty``     added edges that are ``constraints.protected``. Only
                                     mandatory-/locked-overridden ones can appear here:
                                     a genuinely forbidden edge is rejected outright.

    Bonuses (subtracted):

    ``hidden_back_bonus``            Σ length × ``max(0, −facing)`` — seams hidden on the
                                     back side are free wins. **0** without ``front_axis``.
    ``concave_crease_bonus``         Σ length × ``dihedral/fold_angle`` over added edges at
                                     or above ``smooth_angle``: creases are good seam
                                     lines. NOTE the *concavity sign* is not available on
                                     :class:`MeshGraph` (``dihedral_angle`` is unsigned),
                                     so this is really a **crease** bonus; the key name is
                                     kept for the W2 §4.3 contract.
    ``material_boundary_bonus``      Σ length of added edges whose two faces differ in
                                     ``material_index``.
    ``user_preferred_bonus``         Σ length of added edges in ``constraints.preferred``.

    ``total`` = Σ penalties − Σ bonuses. Also reports ``predicted_sub_chart_count`` and
    the sorted ``predicted_sub_chart_face_counts``.

    Pure: nothing is applied to the mesh, no seed, no Blender.
    """
    diag = bbox_diagonal(mesh)
    added = {int(e) for e in added_edges if 0 <= int(e) < mesh.edge_count}
    cfaces = {int(f) for f in chart_faces}
    seam_set = {int(e) for e in seams}
    front = constraints.front_vector()
    smooth = max(float(smooth_angle), 1e-9)
    fold = max(float(fold_angle), 1e-9)

    def norm(value: float) -> float:
        return 0.0 if diag <= 0.0 else float(value) / diag

    smooth_raw = crease_raw = material_raw = preferred_raw = hidden_raw = 0.0
    protected_hits = 0
    for eid in sorted(added):
        e = mesh.edges[eid]
        length = edge_length(mesh, eid)
        dihedral = float(e.dihedral_angle)
        smooth_raw += length * max(0.0, 1.0 - dihedral / smooth)
        if dihedral >= smooth:
            crease_raw += length * (dihedral / fold)
        if len(e.face_ids) == 2:
            a, b = e.face_ids
            if mesh.faces[a].material_index != mesh.faces[b].material_index:
                material_raw += length
        if eid in constraints.preferred:
            preferred_raw += length
        if eid in constraints.protected:
            protected_hits += 1
        if front is not None and e.face_ids:
            facing = max(float(np.dot(np.asarray(mesh.faces[f].normal, dtype=float), front))
                         for f in e.face_ids)
            hidden_raw += length * max(0.0, -facing)

    subs = _predicted_sub_charts(mesh, cfaces, seam_set, added)
    chart_area = float(sum(float(mesh.faces[f].area_3d) for f in cfaces))
    mean_n = _chart_mean_normal(mesh, sorted(cfaces))
    u, v = _plane_axes(mean_n)
    small = 0
    slivers = 0
    for sub in subs:
        area = float(sum(float(mesh.faces[f].area_3d) for f in sub))
        if chart_area > 0.0 and (area / chart_area) < float(tiny_face_fraction):
            small += 1
        if len(sub) <= 2 or _sub_chart_aspect(mesh, sub, u, v) >= SLIVER_ASPECT:
            slivers += 1

    out = {
        "normalized_seam_length": norm(seam_length(mesh, added)),
        "visible_surface_penalty": norm(constraints.exposure_cost(mesh, added)),
        "smooth_surface_penalty": norm(smooth_raw),
        "small_island_creation_penalty": float(small),
        "sliver_creation_penalty": float(slivers),
        "protected_region_penalty": float(protected_hits),
        "hidden_back_bonus": norm(hidden_raw),
        "concave_crease_bonus": norm(crease_raw),
        "material_boundary_bonus": norm(material_raw),
        "user_preferred_bonus": norm(preferred_raw),
    }
    out["total"] = float(sum(out[k] for k in COST_PENALTY_KEYS)
                         - sum(out[k] for k in COST_BONUS_KEYS))
    out["predicted_sub_chart_count"] = len(subs)
    out["predicted_sub_chart_face_counts"] = sorted(len(s) for s in subs)
    return out


def _zero_cost_breakdown() -> dict:
    """The cost of adding NOTHING (``unwrap_only``): every term 0, one sub-chart."""
    out = {k: 0.0 for k in COST_PENALTY_KEYS + COST_BONUS_KEYS}
    out["total"] = 0.0
    out["predicted_sub_chart_count"] = 1
    out["predicted_sub_chart_face_counts"] = []
    return out


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


def unwrap_only_candidate(target_island: int, *,
                          cut_reason: str = "distortion_repair") -> SeamCandidate:
    """The no-cut candidate (§5): if re-unwrapping alone meets quality, G4 forbids
    adding any seam at all. Its cost is zero by construction — nothing is cut."""
    return SeamCandidate(kind="unwrap_only", added_edges=frozenset(),
                         target_island=int(target_island), reason="unwrap_only",
                         seam_length=0.0, exposure_cost=0.0,
                         cut_reason=str(cut_reason), cost=_zero_cost_breakdown())


def _make(mesh: MeshGraph, kind: str, added, target_island: int, constraints: SeamConstraints,
          reason: str, notes: dict, *, chart_faces=(), seams=(),
          cut_reason: str = "distortion_repair") -> SeamCandidate:
    added = frozenset(int(e) for e in added)
    check = constraints.check_added(added)
    notes = dict(notes)
    if not check["ok"]:
        notes["rejected"] = check["reason"]
        notes["protected_cut"] = check["protected_cut"]
    return SeamCandidate(kind=kind, added_edges=added, target_island=int(target_island),
                         reason=reason, seam_length=seam_length(mesh, added),
                         exposure_cost=constraints.exposure_cost(mesh, added), notes=notes,
                         cut_reason=str(cut_reason),
                         cost=candidate_cost_breakdown(mesh, chart_faces, seams, added,
                                                       constraints))


def normal_split_candidate(mesh: MeshGraph, chart_faces, seams: set[int],
                           constraints: SeamConstraints, target_island: int, *,
                           cut_reason: str = "distortion_repair") -> SeamCandidate | None:
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
                 "normal_split", {"split_edges": len(added)},
                 chart_faces=faces, seams=seams, cut_reason=cut_reason)


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
                        *, top_fraction: float = 0.2,
                        cut_reason: str = "distortion_repair") -> SeamCandidate | None:
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
                  "start_vertex": int(start)},
                 chart_faces=cfaces, seams=seams, cut_reason=cut_reason)


def preferred_path_candidate(mesh: MeshGraph, chart_faces, seams: set[int],
                             constraints: SeamConstraints, face_stretch, target_island: int,
                             *, top_fraction: float = 0.2,
                             cut_reason: str = "distortion_repair") -> SeamCandidate | None:
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
                  "start_vertex": int(start)},
                 chart_faces=cfaces, seams=seams, cut_reason=cut_reason)


def generate_candidates(mesh: MeshGraph, charts, target_island: int, seams: set[int],
                        constraints: SeamConstraints, face_stretch, *,
                        max_candidates: int,
                        cut_reason: str = "distortion_repair") -> list[SeamCandidate]:
    """All cut candidates for ``target_island``, deterministically ordered (§5 / G4).

    Order is ``unwrap_only → short_cut → preferred_path → normal_split`` so the no-cut
    option is always evaluated first; candidates with identical ``added_edges`` are
    de-duplicated (first kind wins), constraint-violating candidates are pushed to the
    END (kept for the history, never chosen ahead of a valid one), and the list is
    truncated to ``max_candidates``.

    ``cut_reason`` (:data:`CUT_REASONS`) is stamped on every candidate, and every
    candidate carries its :func:`candidate_cost_breakdown` in ``cost``."""
    if max_candidates <= 0:
        return []
    chart_faces = set(charts[target_island])
    seams = set(seams)

    produced: list[SeamCandidate] = [unwrap_only_candidate(target_island,
                                                            cut_reason=cut_reason)]
    for factory in (short_cut_candidate, preferred_path_candidate):
        cand = factory(mesh, chart_faces, seams, constraints, face_stretch, target_island,
                       cut_reason=cut_reason)
        if cand is not None:
            produced.append(cand)
    ns = normal_split_candidate(mesh, chart_faces, seams, constraints, target_island,
                               cut_reason=cut_reason)
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
