"""Shared seam constraints for every cut path (UV_AUTOMATION §5 / Gate G4).

One immutable object that every cut-producing stage (initial segmentation, distortion
split, overlap repair, welded-fold repair, merge/prune) consults BEFORE it cuts, instead
of each stage re-deriving "what may I cut?" from a loose ``forbidden`` set:

    mandatory (≥ fold / boundary / non-manifold)  >  user locked seam  >  protected  >  preferred

- ``mandatory`` is never removed and never blocked — a protected edge that is also a
  mandatory fold ships as a seam and the clash is recorded as a *conflict*
  (``resolution="mandatory_wins"``), so the run can be held for ``needs_user_review``.
- ``locked`` (an explicit user seam) also wins over ``protected`` (``"locked_wins"``).
- ``forbidden`` = protected − mandatory − locked: the edges a cut must route *around*.
- ``preferred`` is a SOFT cost (× 0.25), never a hard rule.
- ``front_axis`` drives the visibility/exposure cost. Empty ⇒ **visibility neutral**: the
  engine never guesses which way an asset faces (G4).

Pure Python / numpy on a :class:`MeshGraph` — no Blender.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from artist_uv_agent.seam_policy import _axis_vector
from chart_uv_agent import segmentation
from chart_uv_agent.segmentation import FOLD_ANGLE
from uv_agent.geometry.mesh_graph import MeshGraph

#: Soft multiplier applied to a *preferred* sub-fold edge's cut cost.
PREFERRED_COST_FACTOR = 0.25


@dataclass(frozen=True)
class SeamConstraints:
    """The resolved, mesh-validated cut rules for one run (G4).

    ``conflicts`` entries are ``{"edge_id", "user_rule", "engine_rule", "resolution"}``
    dicts; ``invalid_edges`` are spec edge ids outside ``[0, edge_count)`` that were
    dropped rather than silently honoured.
    """

    mandatory: frozenset[int]
    locked: frozenset[int]
    protected: frozenset[int]
    preferred: frozenset[int]
    conflicts: tuple[dict, ...] = ()
    region_policy: object | None = field(default=None, compare=False)
    front_axis: str = ""
    invalid_edges: tuple[int, ...] = ()
    fold_angle: float = FOLD_ANGLE

    # -- construction -----------------------------------------------------
    @classmethod
    def build(cls, mesh: MeshGraph, *, fold_angle: float = FOLD_ANGLE,
              locked=(), protected=(), preferred=(), region_policy=None,
              front_axis: str = "") -> "SeamConstraints":
        """Resolve the precedence against ``mesh``.

        Out-of-range edge ids are dropped into ``invalid_edges`` (an edge cannot be a
        constraint if it does not exist). ``protected ∩ mandatory`` and
        ``protected ∩ locked`` become recorded conflicts — mandatory / locked win.
        """
        n = mesh.edge_count

        def split_valid(ids) -> tuple[set[int], set[int]]:
            valid: set[int] = set()
            bad: set[int] = set()
            for raw in ids:
                eid = int(raw)
                (valid if 0 <= eid < n else bad).add(eid)
            return valid, bad

        locked_v, bad_l = split_valid(locked)
        protected_v, bad_p = split_valid(protected)
        preferred_v, bad_f = split_valid(preferred)
        invalid = tuple(sorted(bad_l | bad_p | bad_f))

        mandatory = frozenset(segmentation.mandatory_seam_edges(mesh, fold_angle=fold_angle))

        conflicts: list[dict] = [
            {"edge_id": e, "user_rule": "protected", "engine_rule": "mandatory_90",
             "resolution": "mandatory_wins"}
            for e in sorted(protected_v & mandatory)
        ]
        conflicts += [
            {"edge_id": e, "user_rule": "protected", "engine_rule": "locked_seam",
             "resolution": "locked_wins"}
            for e in sorted((protected_v & locked_v) - mandatory)
        ]

        return cls(
            mandatory=mandatory,
            locked=frozenset(locked_v),
            protected=frozenset(protected_v),
            preferred=frozenset(preferred_v),
            conflicts=tuple(conflicts),
            region_policy=region_policy,
            front_axis=str(front_axis or ""),
            invalid_edges=invalid,
            fold_angle=float(fold_angle),
        )

    # -- hard rules -------------------------------------------------------
    @property
    def forbidden(self) -> frozenset[int]:
        """Protected edges that are neither mandatory nor an explicit user seam — the
        only edges a cut is actually forbidden to traverse."""
        return frozenset(self.protected - self.mandatory - self.locked)

    def check_added(self, edges) -> dict:
        """Would adding ``edges`` cut a protected edge? (G4: "mandatory와 충돌하지 않는
        protected edge 절개 0")."""
        cut = sorted(set(int(e) for e in edges) & self.forbidden)
        return {"ok": not cut, "protected_cut": cut,
                "reason": None if not cut else "protected_edge_cut"}

    def check_removed(self, edges) -> dict:
        """Would removing ``edges`` drop a mandatory fold or a user-locked seam?
        (G4: "mandatory seam 유지, 사용자 seam lock 제거 0")."""
        ids = set(int(e) for e in edges)
        mand = sorted(ids & self.mandatory)
        lock = sorted(ids & self.locked)
        reason = None
        if mand:
            reason = "mandatory_seam_removed"
        elif lock:
            reason = "locked_seam_removed"
        return {"ok": not mand and not lock, "mandatory_removed": mand,
                "locked_removed": lock, "reason": reason}

    def filter_added(self, edges) -> tuple[set[int], set[int]]:
        """``(allowed, rejected)`` — the rejected edges are RETURNED, never silently
        dropped, so the caller can record why a candidate shrank."""
        ids = set(int(e) for e in edges)
        forbidden = self.forbidden
        rejected = ids & forbidden
        return ids - rejected, rejected

    # -- soft costs -------------------------------------------------------
    def edge_cost(self, mesh: MeshGraph, edge_id: int) -> float:
        """Cost of routing a cut through ``edge_id`` under these constraints.

        ``segmentation.edge_cut_cost`` supplies the crease/region base cost (``inf`` on a
        forbidden edge); a *locked* seam is free (0.0 — it is already a seam) and a
        *preferred* sub-fold edge is discounted by :data:`PREFERRED_COST_FACTOR`. A
        mandatory (≥ fold) edge is never discounted: preference can never outrank the
        engine's own rule.
        """
        eid = int(edge_id)
        if eid in self.locked:
            return 0.0
        cost = segmentation.edge_cut_cost(
            mesh, eid, forbidden=self.forbidden, fold_angle=self.fold_angle,
            region_policy=self.region_policy,
        )
        if eid in self.preferred and eid not in self.mandatory:
            cost *= PREFERRED_COST_FACTOR
        return float(cost)

    def front_vector(self):
        """Unit front vector, or ``None`` when no ``front_axis`` was supplied."""
        return _axis_vector(self.front_axis)

    def exposure_cost(self, mesh: MeshGraph, edges) -> float:
        """Visibility cost of a seam set: Σ edge length × how much its faces face the
        camera. With no ``front_axis`` this is **0.0** for every seam — visibility
        neutral, the engine does not guess a facing direction (G4)."""
        front = self.front_vector()
        if front is None:
            return 0.0
        total = 0.0
        for raw in edges:
            eid = int(raw)
            if not (0 <= eid < mesh.edge_count):
                continue
            total += edge_length(mesh, eid) * edge_exposure(mesh, eid, front)
        return float(total)

    # -- report -----------------------------------------------------------
    def to_report(self) -> dict:
        """The ``seam_report.json`` constraint block (G4 evidence)."""
        return {
            "mandatory_count": len(self.mandatory),
            "locked_count": len(self.locked),
            "protected_count": len(self.protected),
            "preferred_count": len(self.preferred),
            "forbidden_count": len(self.forbidden),
            "conflict_count": len(self.conflicts),
            "conflicts": [dict(c) for c in self.conflicts],
            "invalid_edges": list(self.invalid_edges),
            "front_axis": self.front_axis,
        }


def edge_length(mesh: MeshGraph, edge_id: int) -> float:
    """3D length of ``edge_id``."""
    a, b = mesh.edges[edge_id].vertex_ids
    pa = np.asarray(mesh.vertices[a].co, dtype=float)
    pb = np.asarray(mesh.vertices[b].co, dtype=float)
    return float(np.linalg.norm(pb - pa))


def edge_exposure(mesh: MeshGraph, edge_id: int, front) -> float:
    """How visible ``edge_id`` is from ``front``: the max of its adjacent face normals
    projected on the front vector, clamped at 0 (a back-facing edge costs nothing)."""
    if front is None:
        return 0.0
    f = np.asarray(front, dtype=float)
    best = 0.0
    for fid in mesh.edges[edge_id].face_ids:
        n = np.asarray(mesh.faces[fid].normal, dtype=float)
        best = max(best, float(np.dot(n, f)))
    return max(0.0, best)
