"""Catastrophic UV distortion detection (GAME_UV catastrophic recovery plan §5.1, §14).

:mod:`uv_agent.geometry.distortion_v2` answers "how distorted is this layout on average",
which is the right question for ranking two decent candidates and the WRONG question for
"is this layout shippable at all": a single needle triangle, a UV-collapsed face or a
20x area explosion is invisible in an area-weighted mean and yet destroys the texture.

This module is the hard gate next to that metric. It walks the SAME per-triangle table
(``distortion_v2.collect_triangle_records`` — one Jacobian implementation, never two),
classifies every triangle against explicit thresholds, groups the bad FACES into
connected regions inside their island, and reports counts, regions and per-face scores.

Design rules that the gate depends on:

* nothing is ever silently substituted — a non-finite value is ``invalid`` and fails,
  it is never coerced to ``0.0``;
* input-defect triangles (the 3D triangle itself is degenerate) are NOT UV failures:
  they are skipped and counted separately in ``input_defect_count``;
* the area normalisation is byte-identical to distortion_v2's
  (``scale_sq = sum area_3d(ok) / sum |area_uv|(ok)``), so a uniform packing rescale
  cannot change any number here (CG11);
* every iteration is over sorted ids, there is no randomness and no set iteration in an
  output path, so two runs on the same input produce the same dict (CG14).

numpy is allowed; ``bpy`` is not.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from uv_agent.geometry.distortion_v2 import collect_triangle_records
from uv_agent.geometry.evaluation import uv_islands_from_uvmap
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap

#: Version stamped on every report produced here (CG2 / CG3).
CATASTROPHIC_METRIC_VERSION = 1

#: How many bad triangles are listed in ``worst_triangles``.
MAX_WORST_TRIANGLES = 50
#: How many regions contribute their face ids to :func:`compact_catastrophic`.
MAX_COMPACT_REGIONS = 20

_OK = "ok"
_INPUT_DEFECT = "input_defect"
_UV_DEGENERATE = "uv_degenerate"
_INVALID = "invalid"

REASON_ANISOTROPY_HARD = "anisotropy_hard"
REASON_NEAR_COLLAPSE = "near_collapse"
REASON_NEEDLE = "needle"
REASON_AREA_EXPLOSION = "area_explosion"
REASON_AREA_COLLAPSE = "area_collapse"
REASON_INVALID = "invalid"
#: Reported, never fatal.
REASON_ANISOTROPY_WARN = "anisotropy_warn"


@dataclass(frozen=True)
class CatastrophicThresholds:
    """The explicit line between "distorted" and "broken"."""

    anisotropy_hard_max: float = 8.0
    anisotropy_warn: float = 4.0
    near_collapse_ratio: float = 1e-4  # s2 / max(s1, eps) below this = near collapse
    max_uv_triangle_aspect: float = 40.0
    #: A needle is a UV artefact, not a thin 3D triangle: a decimated sliver whose UV map
    #: is nearly conformal has a huge uv aspect and is NOT broken (CG2). The uv aspect must
    #: also exceed this factor times the triangle's own 3D aspect before it counts.
    needle_3d_aspect_factor: float = 2.0
    local_area_ratio_min: float = 1.0 / 25.0
    local_area_ratio_max: float = 25.0
    bad_area_fraction_cap: float = 0.005
    min_cluster_area_fraction: float = 1e-5

    def to_dict(self) -> dict:
        return {k: float(v) for k, v in asdict(self).items()}


@dataclass(frozen=True)
class BadTriangle:
    """One triangle that failed at least one catastrophic check."""

    face_id: int
    tri_index: int
    island_id: int
    status: str
    anisotropy: float
    s1: float
    s2: float
    area_3d: float
    area_uv: float
    normalized_area_ratio: float
    uv_aspect_ratio: float
    aspect_3d: float = float("nan")
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "face_id": int(self.face_id),
            "tri_index": int(self.tri_index),
            "island_id": int(self.island_id),
            "status": str(self.status),
            "anisotropy": _num(self.anisotropy),
            "s1": _num(self.s1),
            "s2": _num(self.s2),
            "area_3d": _num(self.area_3d),
            "area_uv": _num(self.area_uv),
            "normalized_area_ratio": _num(self.normalized_area_ratio),
            "uv_aspect_ratio": _num(self.uv_aspect_ratio),
            "aspect_3d": _num(self.aspect_3d),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class BadRegion:
    """A connected cluster of bad faces inside one island."""

    region_id: int
    face_ids: tuple[int, ...]
    island_id: int
    area_fraction: float
    max_anisotropy: float
    max_uv_aspect_ratio: float
    reasons: tuple[str, ...] = ()
    touches_island_boundary: bool = False
    boundary_spike: bool = False
    self_overlap: bool = False
    local_flip: bool = False
    below_cluster_min: bool = False
    triangle_count: int = 0
    bad_triangle_count: int = 0

    def to_dict(self) -> dict:
        return {
            "region_id": int(self.region_id),
            "face_ids": [int(f) for f in self.face_ids],
            "island_id": int(self.island_id),
            "area_fraction": _num(self.area_fraction),
            "max_anisotropy": _num(self.max_anisotropy),
            "max_uv_aspect_ratio": _num(self.max_uv_aspect_ratio),
            "reasons": list(self.reasons),
            "touches_island_boundary": bool(self.touches_island_boundary),
            "boundary_spike": bool(self.boundary_spike),
            "self_overlap": bool(self.self_overlap),
            "local_flip": bool(self.local_flip),
            "below_cluster_min": bool(self.below_cluster_min),
            "triangle_count": int(self.triangle_count),
            "bad_triangle_count": int(self.bad_triangle_count),
        }


def _num(x) -> float | None:
    """``float`` for a real number, ``None`` for NaN — never a substituted ``0.0``.

    ``+/-inf`` survives as a float: "this UV triangle has zero area, its aspect ratio is
    literally unbounded" is information a reviewer needs, and it round-trips through
    ``json.dumps``. NaN means "not measurable", and the only honest JSON value for that
    is ``null``."""
    v = float(x)
    if math.isnan(v):
        return None
    return v


def _max_finite(values) -> float | None:
    best: float | None = None
    for v in values:
        f = float(v)
        if math.isfinite(f) and (best is None or f > best):
            best = f
    return best


def _uv_aspect_ratio(longest_edge: float, area_uv: float) -> float:
    """``longest_uv_edge**2 / (2 * uv_area)`` — 1.155 for an equilateral triangle.

    A zero-area UV triangle gives ``+inf`` rather than an exception or a sentinel, which
    is exactly what the needle test wants to see."""
    le = float(longest_edge)
    a = float(area_uv)
    if math.isnan(le) or math.isnan(a):
        return float("nan")
    if a <= 0.0:
        return float("inf") if le > 0.0 else float("nan")
    return (le * le) / (2.0 * a)


def _aspect_3d(longest_edge: float, area_3d: float) -> float:
    """The SAME shape measure as :func:`_uv_aspect_ratio`, applied to the 3D triangle.

    A thin 3D triangle scores high here, which is exactly how the needle test tells a
    UV-made needle apart from a mesh that was already a sliver before unwrapping."""
    return _uv_aspect_ratio(longest_edge, area_3d)


def _island_of_face(mesh: MeshGraph, island_face_ids) -> dict[int, int]:
    out: dict[int, int] = {}
    for island_id, face_ids in enumerate(island_face_ids):
        for fid in face_ids:
            out[int(fid)] = int(island_id)
    return out


def evaluate_catastrophic(
    mesh: MeshGraph,
    uvmap: UVMap,
    islands=None,
    *,
    thresholds: CatastrophicThresholds = CatastrophicThresholds(),
    overlap_face_ids=(),
    flip_face_ids=(),
) -> dict:
    """Catastrophic-distortion report for ``mesh`` + ``uvmap`` (plan §5.1).

    ``islands`` is a list of face-id lists; ``None`` recovers the real UV connectivity
    islands from the UV map itself (:func:`uv_islands_from_uvmap`).

    ``overlap_face_ids`` / ``flip_face_ids`` are the faces an overlap / flip detector
    already flagged; they only set region FLAGS here, they never create a bad triangle,
    so this gate stays a pure function of the geometry it measured itself.
    """
    recs = collect_triangle_records(mesh, uvmap)
    face_count = len(mesh.faces)

    if islands is None:
        island_face_ids = uv_islands_from_uvmap(mesh, uvmap)
    else:
        island_face_ids = [list(face_ids) for face_ids in islands]
    island_of_face = _island_of_face(mesh, island_face_ids)

    # --- global area normalisation, identical to distortion_v2 --------------------
    ok_area_3d = 0.0
    ok_area_uv = 0.0
    for i in range(recs.count):
        if recs.status[i] == _OK:
            ok_area_3d += float(recs.area_3d[i])
            ok_area_uv += abs(float(recs.area_uv[i]))
    scale_sq = (ok_area_3d / ok_area_uv) if ok_area_uv > 1e-300 else 1.0
    if not math.isfinite(scale_sq) or scale_sq <= 0.0:
        scale_sq = 1.0

    overlap_set = {int(f) for f in overlap_face_ids}
    flip_set = {int(f) for f in flip_face_ids}

    # --- per-triangle classification ---------------------------------------------
    bad_triangles: list[BadTriangle] = []
    per_face_tri_index: dict[int, int] = {}
    per_face_max_aniso: list[float] = [float("nan")] * face_count
    per_face_blocked: list[bool] = [False] * face_count
    per_face_hard_fail: list[bool] = [False] * face_count
    face_bad_triangles: dict[int, int] = {}
    face_all_triangles: dict[int, int] = {}

    input_defect_count = 0
    invalid_count = 0
    near_collapse_count = 0
    needle_count = 0
    anisotropy_hard_count = 0
    area_explosion_count = 0
    area_collapse_count = 0
    warn_triangle_count = 0
    bad_area_3d = 0.0
    considered_aniso: list[float] = []
    considered_aspect: list[float] = []
    island_bad_counts: dict[int, int] = {}

    for i in range(recs.count):
        fid = int(recs.face_id[i])
        tri_index = per_face_tri_index.get(fid, 0)
        per_face_tri_index[fid] = tri_index + 1
        face_all_triangles[fid] = face_all_triangles.get(fid, 0) + 1

        status = recs.status[i]
        if status == _INPUT_DEFECT:
            input_defect_count += 1
            continue

        a3 = float(recs.area_3d[i])
        auv = float(recs.area_uv[i])
        s1 = float(recs.s1[i])
        s2 = float(recs.s2[i])
        aniso = float(recs.aniso[i])
        aspect = _uv_aspect_ratio(float(recs.uv_longest_edge[i]), auv)
        aspect3 = _aspect_3d(float(recs.longest_3d_edge[i]), a3)
        ratio = (auv * scale_sq / a3) if a3 > 0.0 else float("nan")

        if math.isfinite(aniso):
            considered_aniso.append(aniso)
            if 0 <= fid < face_count:
                cur = per_face_max_aniso[fid]
                per_face_max_aniso[fid] = (
                    aniso if math.isnan(cur) else max(cur, aniso)
                )
        if math.isfinite(aspect):
            considered_aspect.append(aspect)
        if status in (_UV_DEGENERATE, _INVALID) and 0 <= fid < face_count:
            per_face_blocked[fid] = True

        reasons: list[str] = []
        if status == _INVALID:
            reasons.append(REASON_INVALID)
        else:
            # A value that should be a number but is not is a measurement failure, and
            # a measurement failure is a gate failure — never a zero.
            if not (math.isfinite(a3) and math.isfinite(auv)):
                reasons.append(REASON_INVALID)
            elif status == _OK and not math.isfinite(aniso):
                reasons.append(REASON_INVALID)

        collapse_ratio = s2 / max(s1, 1e-300) if math.isfinite(s2) and math.isfinite(s1) else float("nan")
        if status == _UV_DEGENERATE or (
            math.isfinite(collapse_ratio)
            and collapse_ratio < thresholds.near_collapse_ratio
        ):
            reasons.append(REASON_NEAR_COLLAPSE)
        if math.isfinite(aniso) and aniso > thresholds.anisotropy_hard_max:
            reasons.append(REASON_ANISOTROPY_HARD)
        # CG2: a needle is UV damage. The uv aspect must clear the absolute cap AND be
        # meaningfully worse than the 3D triangle's own aspect — a conformally mapped 3D
        # sliver keeps its shape and is not a UV failure. An unmeasurable 3D aspect (NaN)
        # cannot exonerate anything, so the absolute cap alone decides there.
        if aspect > thresholds.max_uv_triangle_aspect and not math.isnan(aspect):
            if math.isnan(aspect3) or aspect > thresholds.needle_3d_aspect_factor * aspect3:
                reasons.append(REASON_NEEDLE)
        if math.isfinite(ratio):
            if ratio > thresholds.local_area_ratio_max:
                reasons.append(REASON_AREA_EXPLOSION)
            elif ratio < thresholds.local_area_ratio_min:
                reasons.append(REASON_AREA_COLLAPSE)

        if (
            math.isfinite(aniso)
            and aniso > thresholds.anisotropy_warn
            and aniso <= thresholds.anisotropy_hard_max
        ):
            warn_triangle_count += 1

        if not reasons:
            continue

        reasons = sorted(set(reasons))
        if REASON_INVALID in reasons:
            invalid_count += 1
        if REASON_NEAR_COLLAPSE in reasons:
            near_collapse_count += 1
        if REASON_NEEDLE in reasons:
            needle_count += 1
        if REASON_ANISOTROPY_HARD in reasons:
            anisotropy_hard_count += 1
        if REASON_AREA_EXPLOSION in reasons:
            area_explosion_count += 1
        if REASON_AREA_COLLAPSE in reasons:
            area_collapse_count += 1

        if math.isfinite(a3):
            bad_area_3d += a3
        face_bad_triangles[fid] = face_bad_triangles.get(fid, 0) + 1
        if 0 <= fid < face_count:
            per_face_hard_fail[fid] = True
        island_id = island_of_face.get(fid, -1)
        island_bad_counts[island_id] = island_bad_counts.get(island_id, 0) + 1

        bad_triangles.append(
            BadTriangle(
                face_id=fid,
                tri_index=tri_index,
                island_id=island_id,
                status=status,
                anisotropy=aniso,
                s1=s1,
                s2=s2,
                area_3d=a3,
                area_uv=auv,
                normalized_area_ratio=ratio,
                uv_aspect_ratio=aspect,
                aspect_3d=aspect3,
                reasons=tuple(reasons),
            )
        )

    per_face_score: list[float | None] = []
    for fid in range(face_count):
        if per_face_blocked[fid]:
            per_face_score.append(None)
        else:
            per_face_score.append(_num(per_face_max_aniso[fid]))

    bad_face_ids = sorted(face_bad_triangles)
    bad_triangle_count = len(bad_triangles)
    bad_area_fraction = (bad_area_3d / ok_area_3d) if ok_area_3d > 0.0 else 0.0

    # --- regions: connected components of bad faces inside one island -------------
    regions = _build_regions(
        mesh,
        bad_face_ids,
        island_face_ids,
        island_of_face,
        bad_triangles,
        recs,
        ok_area_3d,
        thresholds,
        overlap_set,
        flip_set,
        face_all_triangles,
        face_bad_triangles,
    )

    counted = [r for r in regions if not r.below_cluster_min]
    bad_region_count = len(counted)
    boundary_spike_region_count = sum(1 for r in counted if r.boundary_spike)
    self_overlap_region_count = sum(1 for r in counted if r.self_overlap)
    flip_region_count = sum(1 for r in counted if r.local_flip)

    hard_failed = bad_triangle_count > 0 or invalid_count > 0
    region_failed = (
        bad_region_count > 0
        or bad_area_fraction > thresholds.bad_area_fraction_cap
        or boundary_spike_region_count > 0
        or self_overlap_region_count > 0
    )

    ordered_worst = sorted(
        bad_triangles,
        key=lambda t: (
            -(t.anisotropy if math.isfinite(t.anisotropy) else float("-inf")),
            t.face_id,
            t.tri_index,
        ),
    )

    return {
        "metric_version": CATASTROPHIC_METRIC_VERSION,
        "thresholds": thresholds.to_dict(),
        "valid": bool(invalid_count == 0),
        "hard_failed": bool(hard_failed),
        "region_failed": bool(region_failed),
        "passed": bool(not hard_failed and not region_failed),
        "bad_triangle_count": int(bad_triangle_count),
        "bad_region_count": int(bad_region_count),
        "bad_area_fraction": float(bad_area_fraction),
        "max_anisotropy": _max_finite(considered_aniso),
        "max_uv_triangle_aspect": _max_finite(considered_aspect),
        "near_collapse_count": int(near_collapse_count),
        "needle_count": int(needle_count),
        "anisotropy_hard_count": int(anisotropy_hard_count),
        "area_explosion_count": int(area_explosion_count),
        "area_collapse_count": int(area_collapse_count),
        "invalid_count": int(invalid_count),
        "input_defect_count": int(input_defect_count),
        "warn_triangle_count": int(warn_triangle_count),
        "boundary_spike_region_count": int(boundary_spike_region_count),
        "self_overlap_region_count": int(self_overlap_region_count),
        "flip_region_count": int(flip_region_count),
        "regions": [r.to_dict() for r in regions],
        "worst_triangles": [t.to_dict() for t in ordered_worst[:MAX_WORST_TRIANGLES]],
        "bad_face_ids": [int(f) for f in bad_face_ids],
        "per_face_score": per_face_score,
        "per_face_hard_fail": [bool(v) for v in per_face_hard_fail],
        "island_bad_counts": {
            int(k): int(island_bad_counts[k]) for k in sorted(island_bad_counts)
        },
        "triangle_count": int(recs.count),
    }


def _build_regions(
    mesh: MeshGraph,
    bad_face_ids,
    island_face_ids,
    island_of_face,
    bad_triangles,
    recs,
    ok_area_3d: float,
    thresholds: CatastrophicThresholds,
    overlap_set: set[int],
    flip_set: set[int],
    face_all_triangles: dict[int, int],
    face_bad_triangles: dict[int, int],
) -> list[BadRegion]:
    bad_set = set(bad_face_ids)
    if not bad_set:
        return []

    adjacency = mesh.face_adjacency()
    island_members = [set(int(f) for f in ids) for ids in island_face_ids]

    per_face_bad: dict[int, list[BadTriangle]] = {}
    for t in bad_triangles:
        per_face_bad.setdefault(t.face_id, []).append(t)

    # 3D area of the ok/measured triangles per face (denominator shared with the report).
    face_area_3d: dict[int, float] = {}
    for i in range(recs.count):
        if recs.status[i] == _OK:
            fid = int(recs.face_id[i])
            face_area_3d[fid] = face_area_3d.get(fid, 0.0) + float(recs.area_3d[i])

    seen: set[int] = set()
    components: list[tuple[int, list[int]]] = []
    for fid in sorted(bad_set):
        if fid in seen:
            continue
        island_id = island_of_face.get(fid, -1)
        members = island_members[island_id] if 0 <= island_id < len(island_members) else None
        comp: list[int] = []
        stack = [fid]
        seen.add(fid)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nb, _edge_id in sorted(adjacency.get(cur, [])):
                if nb in seen or nb not in bad_set:
                    continue
                if members is not None and nb not in members:
                    continue
                if members is None and island_of_face.get(nb, -1) != island_id:
                    continue
                seen.add(nb)
                stack.append(nb)
        components.append((island_id, sorted(comp)))

    components.sort(key=lambda c: (c[0], c[1][0]))

    regions: list[BadRegion] = []
    for region_id, (island_id, comp) in enumerate(components):
        members = island_members[island_id] if 0 <= island_id < len(island_members) else set(comp)
        reasons: set[str] = set()
        aniso_values: list[float] = []
        aspect_values: list[float] = []
        area = 0.0
        tri_total = 0
        bad_total = 0
        for fid in comp:
            area += face_area_3d.get(fid, 0.0)
            tri_total += int(face_all_triangles.get(fid, 0))
            bad_total += int(face_bad_triangles.get(fid, 0))
            for t in per_face_bad.get(fid, ()):  # already deterministic order
                reasons.update(t.reasons)
                aniso_values.append(t.anisotropy)
                aspect_values.append(t.uv_aspect_ratio)

        touches = _touches_island_boundary(mesh, comp, members)
        max_aniso = _max_finite(aniso_values)
        # inf is a real answer for a zero-area UV triangle; keep it instead of hiding it.
        max_aspect = max(
            (a for a in aspect_values if not math.isnan(a)), default=float("nan")
        )
        sorted_reasons = tuple(sorted(reasons))
        area_fraction = (area / ok_area_3d) if ok_area_3d > 0.0 else 0.0
        regions.append(
            BadRegion(
                region_id=region_id,
                face_ids=tuple(comp),
                island_id=int(island_id),
                area_fraction=float(area_fraction),
                max_anisotropy=(max_aniso if max_aniso is not None else float("nan")),
                max_uv_aspect_ratio=max_aspect,
                reasons=sorted_reasons,
                touches_island_boundary=touches,
                boundary_spike=bool(
                    touches
                    and (
                        REASON_NEEDLE in sorted_reasons
                        or REASON_ANISOTROPY_HARD in sorted_reasons
                    )
                ),
                self_overlap=any(fid in overlap_set for fid in comp),
                local_flip=any(fid in flip_set for fid in comp),
                below_cluster_min=bool(
                    area_fraction < thresholds.min_cluster_area_fraction
                ),
                triangle_count=tri_total,
                bad_triangle_count=bad_total,
            )
        )
    return regions


def _touches_island_boundary(mesh: MeshGraph, comp, island_members) -> bool:
    """True when any face of ``comp`` owns an edge on the island's UV border.

    An edge counts when it is a mesh boundary / non-manifold edge (not exactly two
    faces) or when its other face lives outside the island — both are places where the
    UV chart ends, which is where a needle does its visible damage."""
    for fid in comp:
        for eid in mesh.faces[fid].edge_ids:
            edge = mesh.edges[eid]
            if len(edge.face_ids) != 2:
                return True
            other = edge.face_ids[0] if edge.face_ids[1] == fid else edge.face_ids[1]
            if int(other) not in island_members:
                return True
    return False


def catastrophic_counters(report: dict) -> tuple:
    """The comparable tuple for ranking two candidates (lower is better, left to right).

    Deliberately small and ordered: a candidate that removes a broken triangle always
    beats one that only shaved the average, no matter what the averages say."""
    return (
        int(report.get("bad_triangle_count", 0)),
        int(report.get("near_collapse_count", 0)),
        int(report.get("invalid_count", 0)),
        int(report.get("bad_region_count", 0)),
        float(report.get("bad_area_fraction", 0.0)),
        _finite_or(report.get("max_anisotropy"), 0.0),
    )


def _finite_or(value, default: float) -> float:
    if value is None:
        return float(default)
    v = float(value)
    return v if math.isfinite(v) else float(default)


def compact_catastrophic(report: dict) -> dict:
    """Summary-sized view: every scalar, no regions / worst triangles / per-face arrays.

    ``region_face_ids`` keeps the first :data:`MAX_COMPACT_REGIONS` regions' face ids so a
    reviewer can still see WHERE the damage is without carrying the full report."""
    skip = {"regions", "worst_triangles", "per_face_score", "per_face_hard_fail"}
    out = {k: report[k] for k in report if k not in skip}
    out["region_face_ids"] = [
        [int(f) for f in (r.get("face_ids") or [])]
        for r in (report.get("regions") or [])[:MAX_COMPACT_REGIONS]
    ]
    return out


_PROFILE_KEYS = (
    "anisotropy_hard_max",
    "near_collapse_ratio",
    "max_uv_triangle_aspect",
    "needle_3d_aspect_factor",
    "local_area_ratio_min",
    "local_area_ratio_max",
    "bad_area_fraction_cap",
)


def thresholds_from_profile(profile_like) -> CatastrophicThresholds:
    """Build thresholds from a dict or an object, defaulting every missing key.

    A profile is allowed to override only what it cares about; a missing key means "the
    default", never ``0.0``."""
    defaults = CatastrophicThresholds()
    values: dict[str, float] = {}
    for key in _PROFILE_KEYS:
        if isinstance(profile_like, dict):
            raw = profile_like.get(key, None)
        else:
            raw = getattr(profile_like, key, None)
        if raw is None:
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isnan(v):
            continue
        values[key] = v
    return CatastrophicThresholds(
        anisotropy_hard_max=values.get(
            "anisotropy_hard_max", defaults.anisotropy_hard_max
        ),
        anisotropy_warn=defaults.anisotropy_warn,
        near_collapse_ratio=values.get(
            "near_collapse_ratio", defaults.near_collapse_ratio
        ),
        max_uv_triangle_aspect=values.get(
            "max_uv_triangle_aspect", defaults.max_uv_triangle_aspect
        ),
        needle_3d_aspect_factor=values.get(
            "needle_3d_aspect_factor", defaults.needle_3d_aspect_factor
        ),
        local_area_ratio_min=values.get(
            "local_area_ratio_min", defaults.local_area_ratio_min
        ),
        local_area_ratio_max=values.get(
            "local_area_ratio_max", defaults.local_area_ratio_max
        ),
        bad_area_fraction_cap=values.get(
            "bad_area_fraction_cap", defaults.bad_area_fraction_cap
        ),
        min_cluster_area_fraction=defaults.min_cluster_area_fraction,
    )
