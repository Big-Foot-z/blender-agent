"""Distortion metrics v2 (UV_AUTOMATION_WORK_PLAN §4, Gate G3).

v1 (:mod:`uv_agent.geometry.evaluation`) keeps its meaning untouched: this module is
additive. The differences that matter for G3 are:

* the per-triangle Jacobian ``J`` (3D surface -> UV) and its singular values
  ``s1 >= s2``, giving a DIRECTIONAL distortion number ``anisotropy_ratio = s1 / s2``
  (1 = conformal; 4x wide / 0.25x tall = 16) next to the area number v1 already had;
* real face triangulation (``MeshGraph.face_triangles``) instead of a naive fan, so a
  concave n-gon never contributes phantom triangles outside the polygon;
* degenerate triangles are NEVER silently dropped — they are excluded from the
  statistics but counted and reported with their face ids, split into "input defect"
  (the 3D triangle is degenerate) and "UV degenerate" (a valid 3D triangle collapsed
  in UV, i.e. a correctness failure);
* aggregation is 3D-area weighted (mean, p95 at the 95% point of the area CDF, max,
  and the area fraction above an anisotropy basis) for the whole mesh, for each island
  and for caller-named regions, so a small bad patch cannot hide in the global mean;
* the report records ``metric_version``, the evaluation stage and the scale policy, so
  v1 and v2 numbers are never mixed and a per-candidate island rescale can never be
  mistaken for an area improvement;
* the report carries a flat ``summary`` block (the required global / worst-island metric
  set, ``bad_area_ratio`` with its threshold, and ``summary_valid``) derived from the
  ``global`` / ``islands`` rows, so a gate reader needs one dict and no traversal.

numpy is allowed here; ``bpy`` is not — this must be importable from a plain test
process.
"""

from __future__ import annotations

import math

import numpy as np

from uv_agent.geometry.evaluation import uv_islands_from_uvmap
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap

#: Version stamped on every report produced here (Gate G3 / G8).
METRIC_VERSION = 2

#: Relative area below which a 3D triangle counts as an input defect.
INPUT_DEFECT_AREA_EPS = 1e-14
#: Relative smallest-singular-value below which the UV triangle counts as collapsed.
UV_DEGENERATE_EPS = 1e-12
#: How many face ids are listed per degenerate category.
MAX_REPORTED_FACE_IDS = 50

#: The metric keys every scope (global / island / region) carries.
METRIC_KEYS: tuple[str, ...] = (
    "anisotropy_mean",
    "anisotropy_p95",
    "anisotropy_max",
    "area_stretch_mean",
    "area_stretch_p95",
    "area_stretch_max",
    "exceed_area_fraction",
    "area_3d",
    "area_uv",
    "triangle_count",
)

_OK = "ok"
_INPUT_DEFECT = "input_defect"
_UV_DEGENERATE = "uv_degenerate"
_INVALID = "invalid"


# --- per-triangle core ------------------------------------------------------------


def triangle_singular_values(p0, p1, p2, uv0, uv1, uv2) -> tuple[float, float, str]:
    """Singular values ``(s1, s2, status)`` of the 3D->UV Jacobian of one triangle.

    The 3D triangle is moved into an ORTHONORMAL local 2D basis anchored at ``p0``
    (``e1 = normalize(p1 - p0)``, ``e2 = normalize((p2 - p0) - ((p2 - p0)·e1) e1)``), so
    the metric is invariant to the triangle's placement and orientation in space. With
    ``X`` the 2x2 matrix whose columns are the local coordinates of ``p1 - p0`` and
    ``p2 - p0``, and ``U`` the 2x2 matrix whose columns are ``uv1 - uv0`` and
    ``uv2 - uv0``, the linear map is ``J = U @ inv(X)`` and ``s1 >= s2 >= 0``.

    ``status`` is one of:

    ``"ok"``
        usable for statistics.
    ``"input_defect"``
        the 3D triangle is degenerate (area <= ``1e-14 * scale**2``, or ``X`` singular)
        — a defect of the INPUT mesh, diagnostic rather than a UV failure.
    ``"uv_degenerate"``
        a valid 3D triangle whose UV image collapsed (``s2 <= 1e-12 * max(s1, 1)`` or
        zero UV area) — a UV correctness failure.
    ``"invalid"``
        a NaN / infinity appeared (e.g. a NaN in the UV map).
    """
    p0 = np.asarray(p0, dtype=float)
    p1 = np.asarray(p1, dtype=float)
    p2 = np.asarray(p2, dtype=float)
    uv0 = np.asarray(uv0, dtype=float)
    uv1 = np.asarray(uv1, dtype=float)
    uv2 = np.asarray(uv2, dtype=float)

    if not (
        np.all(np.isfinite(p0)) and np.all(np.isfinite(p1)) and np.all(np.isfinite(p2))
    ):
        return (float("nan"), float("nan"), _INVALID)

    ea = p1 - p0
    eb = p2 - p0
    len_a = float(np.linalg.norm(ea))
    scale = max(len_a, float(np.linalg.norm(eb)), float(np.linalg.norm(p2 - p1)))
    area_3d = 0.5 * float(np.linalg.norm(np.cross(ea, eb)))
    if scale <= 0.0 or len_a <= 0.0 or area_3d <= INPUT_DEFECT_AREA_EPS * scale * scale:
        return (float("nan"), float("nan"), _INPUT_DEFECT)

    e1 = ea / len_a
    w = eb - float(np.dot(eb, e1)) * e1
    len_w = float(np.linalg.norm(w))
    if len_w <= 0.0:
        return (float("nan"), float("nan"), _INPUT_DEFECT)

    # X: columns are the local coordinates of ea and eb. Upper triangular by construction
    # (ea has no e2 component), so inv(X) is written out directly.
    x11 = len_a
    x12 = float(np.dot(eb, e1))
    x22 = len_w
    det_x = x11 * x22
    if det_x <= 0.0 or not math.isfinite(det_x):
        return (float("nan"), float("nan"), _INPUT_DEFECT)

    du1 = uv1 - uv0
    du2 = uv2 - uv0
    if not (np.all(np.isfinite(du1)) and np.all(np.isfinite(du2))):
        return (float("nan"), float("nan"), _INVALID)

    # J = U @ inv(X), with inv(X) = 1/det * [[x22, -x12], [0, x11]].
    a = (du1[0] * x22) / det_x
    b = (du1[0] * -x12 + du2[0] * x11) / det_x
    c = (du1[1] * x22) / det_x
    d = (du1[1] * -x12 + du2[1] * x11) / det_x

    # Numerically stable closed form for the singular values of a 2x2 matrix.
    q = math.hypot(a + d, b - c)
    r = math.hypot(a - d, b + c)
    s1 = 0.5 * (q + r)
    s2 = 0.5 * abs(q - r)
    if not (math.isfinite(s1) and math.isfinite(s2)):
        return (float("nan"), float("nan"), _INVALID)

    det_u = float(du1[0] * du2[1] - du2[0] * du1[1])
    if det_u == 0.0 or s2 <= UV_DEGENERATE_EPS * max(s1, 1.0):
        return (float(s1), float(s2), _UV_DEGENERATE)
    return (float(s1), float(s2), _OK)


class _Records:
    """Flat per-triangle table for the whole mesh (one pass, reused by every scope)."""

    __slots__ = ("face_id", "area_3d", "area_uv", "aniso", "status", "count")

    def __init__(self, face_id, area_3d, area_uv, aniso, status):
        self.face_id = np.asarray(face_id, dtype=np.int64)
        self.area_3d = np.asarray(area_3d, dtype=float)
        self.area_uv = np.asarray(area_uv, dtype=float)
        self.aniso = np.asarray(aniso, dtype=float)
        self.status = list(status)
        self.count = int(self.face_id.size)


def _tri_area_uv(uv0, uv1, uv2) -> float:
    return 0.5 * abs(
        (uv1[0] - uv0[0]) * (uv2[1] - uv0[1]) - (uv2[0] - uv0[0]) * (uv1[1] - uv0[1])
    )


def _collect(mesh: MeshGraph, uvmap: UVMap) -> _Records:
    face_id: list[int] = []
    area_3d: list[float] = []
    area_uv: list[float] = []
    aniso: list[float] = []
    status: list[str] = []
    for f in mesh.faces:
        for l0, l1, l2 in mesh.face_triangles(f.id):
            p0 = mesh.vertex_co(mesh.loops[l0].vertex_id)
            p1 = mesh.vertex_co(mesh.loops[l1].vertex_id)
            p2 = mesh.vertex_co(mesh.loops[l2].vertex_id)
            uv0, uv1, uv2 = uvmap.get(l0), uvmap.get(l1), uvmap.get(l2)
            s1, s2, st = triangle_singular_values(p0, p1, p2, uv0, uv1, uv2)
            a3 = 0.5 * float(np.linalg.norm(np.cross(p1 - p0, p2 - p0)))
            auv = _tri_area_uv(uv0, uv1, uv2)
            ratio = (s1 / s2) if (st == _OK and s2 > 0.0) else float("nan")
            face_id.append(f.id)
            area_3d.append(a3)
            area_uv.append(auv)
            aniso.append(ratio)
            status.append(st)
    return _Records(face_id, area_3d, area_uv, aniso, status)


# --- aggregation ------------------------------------------------------------------


def _weighted_mean(values: np.ndarray, weights: np.ndarray) -> float:
    total = float(weights.sum())
    if total <= 0.0:
        return 0.0
    return float(np.dot(values, weights) / total)


def _weighted_p95(values: np.ndarray, weights: np.ndarray, q: float = 0.95) -> float:
    """Value of the first triangle at which the cumulative 3D area reaches ``q``.

    This is the plan's definition: sort ascending by value, walk the AREA CDF, and take
    the value where it first reaches 95% — not a count-based percentile."""
    total = float(weights.sum())
    if values.size == 0 or total <= 0.0:
        return 0.0
    order = np.argsort(values, kind="stable")
    cum = np.cumsum(weights[order])
    idx = int(np.searchsorted(cum, q * total, side="left"))
    idx = min(idx, values.size - 1)
    return float(values[order][idx])


def _aggregate(
    recs: _Records, mask: np.ndarray, scale_sq: float, exceed_basis: float
) -> dict:
    """Metrics over the usable (``ok``) triangles selected by the boolean ``mask``."""
    a3 = recs.area_3d[mask]
    auv = recs.area_uv[mask]
    ratio = recs.aniso[mask]
    if a3.size == 0:
        out: dict = {k: 0.0 for k in METRIC_KEYS}
        out["triangle_count"] = 0
        return out

    with np.errstate(divide="ignore", invalid="ignore"):
        stretch = np.abs(np.log(np.maximum((auv * scale_sq) / a3, 1e-300)))
    total_3d = float(a3.sum())
    exceed = float(a3[ratio > exceed_basis].sum())
    return {
        "anisotropy_mean": _weighted_mean(ratio, a3),
        "anisotropy_p95": _weighted_p95(ratio, a3),
        "anisotropy_max": float(ratio.max()),
        "area_stretch_mean": _weighted_mean(stretch, a3),
        "area_stretch_p95": _weighted_p95(stretch, a3),
        "area_stretch_max": float(stretch.max()),
        "exceed_area_fraction": (exceed / total_3d) if total_3d > 0.0 else 0.0,
        "area_3d": total_3d,
        "area_uv": float(auv.sum()),
        "triangle_count": int(a3.size),
    }


def _finite_scope(scope: dict) -> bool:
    return all(math.isfinite(float(scope[k])) for k in METRIC_KEYS)


# --- public API -------------------------------------------------------------------


def evaluate_distortion_v2(
    mesh: MeshGraph,
    uvmap: UVMap,
    islands=None,
    *,
    stage: str = "final",
    scale_policy: str = "global_area_normalized",
    exceed_basis: float = 1.6,
    regions: dict[str, list[int]] | None = None,
) -> dict:
    """Full v2 distortion report for ``mesh`` + ``uvmap`` (plan §4, Gate G3).

    ``islands`` is a list of face-id lists. When it is ``None`` the real UV connectivity
    islands are recovered from the UV map itself (``island_source = "uv_connectivity"``)
    rather than trusting a seam flood, which can disagree with the layout actually
    written; a caller-supplied list is reported as ``"caller"``.

    The UV map is normalised ONCE globally (``scale = sqrt(sum area_3d / sum |area_uv|)``
    over the usable triangles) before the area metric, so a later uniform packing scale
    cannot change the numbers and a per-candidate island rescale cannot masquerade as an
    area improvement. Anisotropy is scale-free and untouched by the normalisation.

    Degenerate triangles are excluded from the statistics but reported by count and face
    id in ``degenerate_triangles``. ``valid`` stays True when only UV-degenerate triangles
    are present — that failure belongs to the quality profile
    (``uv_degenerate_triangles`` check), not to the measurement.

    Per-scope ``area_3d`` / ``area_uv`` / ``triangle_count`` cover the triangles that
    entered the statistics, so ``exceed_area_fraction`` and ``area_3d`` always share one
    denominator; ``face_count`` is the full membership of the island / region.
    """
    recs = _collect(mesh, uvmap)
    status = np.asarray(recs.status, dtype=object)
    if recs.count:
        ok = status == _OK
        input_defect = status == _INPUT_DEFECT
        uv_degenerate = status == _UV_DEGENERATE
        invalid = status == _INVALID
    else:
        ok = input_defect = uv_degenerate = invalid = np.zeros(0, dtype=bool)

    if islands is None:
        island_face_ids = uv_islands_from_uvmap(mesh, uvmap)
        island_source = "uv_connectivity"
    else:
        island_face_ids = [list(face_ids) for face_ids in islands]
        island_source = "caller"

    ok_area_3d = float(recs.area_3d[ok].sum()) if recs.count else 0.0
    ok_area_uv = float(recs.area_uv[ok].sum()) if recs.count else 0.0
    scale_sq = (ok_area_3d / ok_area_uv) if ok_area_uv > 1e-300 else 1.0
    if not math.isfinite(scale_sq) or scale_sq <= 0.0:
        scale_sq = 1.0

    global_metrics = _aggregate(recs, ok, scale_sq, exceed_basis)

    def _member(face_ids) -> np.ndarray:
        ids = np.asarray(list(face_ids), dtype=np.int64)
        if recs.count == 0:
            return np.zeros(0, dtype=bool)
        if ids.size == 0:
            return np.zeros(recs.count, dtype=bool)
        return np.isin(recs.face_id, ids)

    island_rows: list[dict] = []
    for island_id, face_ids in enumerate(island_face_ids):
        row = {"island_id": island_id, "face_count": len(face_ids)}
        row.update(_aggregate(recs, ok & _member(face_ids), scale_sq, exceed_basis))
        island_rows.append(row)

    region_rows: dict[str, dict] = {}
    for name, face_ids in (regions or {}).items():
        members = list(face_ids)
        row = {"face_count": len(members)}
        row.update(_aggregate(recs, ok & _member(members), scale_sq, exceed_basis))
        region_rows[str(name)] = row

    def _face_ids_for(mask: np.ndarray) -> list[int]:
        if recs.count == 0 or not bool(mask.any()):
            return []
        out: list[int] = []
        for fid in recs.face_id[mask].tolist():
            fid = int(fid)
            if fid not in out:
                out.append(fid)
            if len(out) >= MAX_REPORTED_FACE_IDS:
                break
        return out

    degenerate = {
        "input_defect_count": int(input_defect.sum()),
        "uv_degenerate_count": int(uv_degenerate.sum()),
        "invalid_count": int(invalid.sum()),
        "input_defect_face_ids": _face_ids_for(input_defect),
        "uv_degenerate_face_ids": _face_ids_for(uv_degenerate),
    }

    worst_island_id = None
    if island_rows:
        worst = max(island_rows, key=lambda r: (r["anisotropy_p95"], -r["island_id"]))
        worst_island_id = worst["island_id"]

    valid = (
        recs.count >= 1
        and degenerate["invalid_count"] == 0
        and _finite_scope(global_metrics)
        and all(_finite_scope(r) for r in island_rows)
        and all(_finite_scope(r) for r in region_rows.values())
    )

    worst_row = island_rows[worst_island_id] if worst_island_id is not None else None
    summary = {
        "metric_version": METRIC_VERSION,
        "global_area_stretch_mean": float(global_metrics["area_stretch_mean"]),
        "global_area_stretch_p95": float(global_metrics["area_stretch_p95"]),
        "global_anisotropy_p95": float(global_metrics["anisotropy_p95"]),
        "global_anisotropy_max": float(global_metrics["anisotropy_max"]),
        "worst_island_id": worst_island_id,
        "worst_island_area_stretch_p95": (
            float(worst_row["area_stretch_p95"]) if worst_row is not None else None
        ),
        "worst_island_anisotropy_p95": (
            float(worst_row["anisotropy_p95"]) if worst_row is not None else None
        ),
        "worst_island_anisotropy_max": (
            float(worst_row["anisotropy_max"]) if worst_row is not None else None
        ),
        # Report alias of global["exceed_area_fraction"]; the threshold travels with it
        # so a reader never has to guess which basis the ratio was measured against.
        "bad_area_ratio": float(global_metrics["exceed_area_fraction"]),
        "bad_area_threshold": float(exceed_basis),
    }
    summary["summary_valid"] = bool(
        valid
        and worst_row is not None
        and all(
            math.isfinite(summary[k])
            for k in (
                "global_area_stretch_mean",
                "global_area_stretch_p95",
                "global_anisotropy_p95",
                "global_anisotropy_max",
                "worst_island_area_stretch_p95",
                "worst_island_anisotropy_p95",
                "worst_island_anisotropy_max",
                "bad_area_ratio",
                "bad_area_threshold",
            )
        )
    )

    return {
        "metric_version": METRIC_VERSION,
        "evaluation_stage": stage,
        "scale_policy": scale_policy,
        "exceed_basis_anisotropy": float(exceed_basis),
        "island_source": island_source,
        "valid": bool(valid),
        "global": global_metrics,
        "islands": island_rows,
        "regions": region_rows,
        "degenerate_triangles": degenerate,
        "worst_island_id": worst_island_id,
        "triangle_count": recs.count,
        "summary": summary,
    }


def compact_distortion_v2(report: dict, *, max_islands: int = 200) -> dict:
    """Summary-sized view of a v2 report: global + the worst islands only.

    Islands are ordered by ``anisotropy_p95`` descending and truncated, because a summary
    JSON must stay bounded on a mesh with thousands of islands while still showing the
    ones a reviewer has to look at."""
    islands = report.get("islands") or []
    ordered = sorted(
        islands,
        key=lambda r: (-float(r.get("anisotropy_p95", 0.0)), r.get("island_id", 0)),
    )
    return {
        "metric_version": report.get("metric_version"),
        "valid": report.get("valid"),
        "global": dict(report.get("global") or {}),
        "islands": [dict(r) for r in ordered[: max(0, int(max_islands))]],
        "island_count": len(islands),
        "degenerate_triangles": dict(report.get("degenerate_triangles") or {}),
        "worst_island_id": report.get("worst_island_id"),
        "summary": report.get("summary"),
    }


def per_face_anisotropy(mesh: MeshGraph, uvmap: UVMap) -> np.ndarray:
    """Per-face 3D-area-weighted mean ``anisotropy_ratio``, as an ``(n_faces,)`` array.

    A face with any UV-degenerate / invalid triangle, or with no usable triangle at all,
    is ``NaN`` — NOT infinity and NOT an arbitrary large number, so a caller picking
    heat-map colours or refinement candidates has to decide explicitly what an
    unmeasurable face means instead of silently ranking it worst."""
    recs = _collect(mesh, uvmap)
    out = np.full(len(mesh.faces), np.nan, dtype=float)
    acc = np.zeros(len(mesh.faces), dtype=float)
    weight = np.zeros(len(mesh.faces), dtype=float)
    blocked = np.zeros(len(mesh.faces), dtype=bool)
    for i in range(recs.count):
        fid = int(recs.face_id[i])
        st = recs.status[i]
        if st in (_UV_DEGENERATE, _INVALID):
            blocked[fid] = True
        elif st == _OK:
            w = float(recs.area_3d[i])
            acc[fid] += float(recs.aniso[i]) * w
            weight[fid] += w
    usable = (~blocked) & (weight > 0.0)
    out[usable] = acc[usable] / weight[usable]
    return out
