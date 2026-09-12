"""Texel-density uniformity audit (Gate G8).

Gate G8 asks a single question about a finished UV layout: *does every island get
the same number of texels per unit of surface*? A checker pattern baked at
``texture_size_px`` must read at the same scale on every part of the model,
unless the asset author deliberately asked for a denser island (a face, a decal
strip) - which is metadata the caller supplies, never something this engine
invents.

Definitions (per island, ``island_id`` is the index into ``islands``):

* ``area_3d``  = sum of :attr:`~uv_agent.geometry.mesh_graph.Face.area_3d`
* ``uv_area``  = sum of ``|signed UV triangle area|`` over
  :meth:`~uv_agent.geometry.mesh_graph.MeshGraph.face_triangles` (the real
  triangulation, so concave n-gons do not gain phantom area)
* ``density``  = ``sqrt(uv_area / area_3d) * texture_size_px``
  -- texels per world unit, i.e. a *length* ratio, which is why the areas go
  under a square root.

Globally:

* ``density_mean`` = 3D-area-weighted mean of the island densities
* ``density_cv``   = 3D-area-weighted standard deviation / ``density_mean``
  (0.0 with fewer than two measurable islands)
* ``relative_density`` = ``density / density_mean``
* an island is an outlier when ``|relative_density - expected| >
  outlier_tolerance``, where ``expected`` is 1.0 unless
  ``intentional_weighting`` supplies a different expectation.

**Invariance (the property Gate G8 relies on):** multiplying *all* UVs by a
uniform scale ``s`` multiplies ``uv_area`` by ``s**2`` and therefore every
``density`` by ``s``; ``density_mean`` scales by ``s`` as well, so
``density_cv``, every ``relative_density`` and the outlier set are unchanged.
The audit measures *uniformity*, not the absolute UV scale.

Degenerate islands (no 3D area, or no UV area) cannot have a density at all;
they are reported with ``density: None``, counted in
``unmeasurable_island_count`` and excluded from the statistics, so a single
zero-area face never poisons the mean.

Pure numpy - no ``bpy`` - so the audit runs from tests and from the worker.
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np

from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap

__all__ = [
    "evaluate_texel_density",
    "compact_texel_density",
    "AREA_3D_EPS",
    "UV_AREA_EPS",
]

#: An island with less 3D area than this has no meaningful density.
AREA_3D_EPS = 1e-12
#: ... and likewise for a collapsed UV footprint.
UV_AREA_EPS = 1e-18


def _tri_area_uv(a, b, c) -> float:
    """Absolute area of a UV triangle (orientation is Gate G1's business)."""
    return abs(0.5 * float((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])))


def _weighted_mean(values: np.ndarray, weights: np.ndarray) -> float:
    w = float(np.sum(weights))
    if w <= AREA_3D_EPS:
        return float(np.mean(values))
    return float(np.dot(values, weights) / w)


def _weighted_std(values: np.ndarray, weights: np.ndarray, mean: float) -> float:
    w = float(np.sum(weights))
    if w <= AREA_3D_EPS:
        return float(np.std(values))
    var = float(np.dot((values - mean) ** 2, weights) / w)
    return math.sqrt(var) if var > 0.0 or math.isnan(var) else 0.0


def evaluate_texel_density(
    mesh: MeshGraph,
    uvmap: UVMap,
    islands: Sequence[Sequence[int]],
    *,
    texture_size_px: int,
    cv_max: float,
    outlier_tolerance: float,
    outlier_count_max: int,
    intentional_weighting: dict | None = None,
) -> dict:
    """Gate G8 texel-density audit over ``islands`` (each a list of face ids).

    ``intentional_weighting`` maps ``island_id -> expected relative density``
    (e.g. ``{3: 2.0}`` for "island 3 is meant to be twice as dense"). The
    expectations are normalised by their own 3D-area-weighted mean before the
    comparison, because ``relative_density`` is itself normalised by the
    area-weighted mean density - so a single ``2.0`` entry means "twice the rest",
    not "twice the average". Only caller-supplied metadata ever lands here.
    """
    size_px = float(texture_size_px)
    valid = True

    rows: list[dict] = []
    for island_id, face_ids in enumerate(islands):
        fids = [int(f) for f in face_ids]
        area_3d = 0.0
        uv_area = 0.0
        for fid in fids:
            face = mesh.faces[fid]
            area_3d += float(face.area_3d)
            loop_uvs = uvmap.uv[face.loop_indices]
            if not bool(np.all(np.isfinite(loop_uvs))):
                valid = False
            for l0, l1, l2 in mesh.face_triangles(fid):
                uv_area += _tri_area_uv(uvmap.uv[l0], uvmap.uv[l1], uvmap.uv[l2])

        unmeasurable = area_3d <= AREA_3D_EPS or (
            math.isfinite(uv_area) and uv_area <= UV_AREA_EPS
        )
        density = None if unmeasurable else math.sqrt(uv_area / area_3d) * size_px
        rows.append(
            {
                "island_id": island_id,
                "face_count": len(fids),
                "area_3d": area_3d,
                "uv_area": uv_area,
                "density": density,
                "relative_density": None,
                "outlier": False,
            }
        )

    measured = [r for r in rows if r["density"] is not None]
    unmeasurable_island_count = len(rows) - len(measured)

    density_mean: float | None = None
    density_cv = 0.0
    if measured:
        values = np.array([r["density"] for r in measured], dtype=float)
        weights = np.array([r["area_3d"] for r in measured], dtype=float)
        density_mean = _weighted_mean(values, weights)
        if len(measured) >= 2:
            std = _weighted_std(values, weights, density_mean)
            if math.isfinite(density_mean) and abs(density_mean) > AREA_3D_EPS:
                density_cv = float(std / density_mean)
            else:
                density_cv = float("nan") if not math.isfinite(density_mean) else 0.0

    # Expected relative density per island (1.0 unless the caller says otherwise),
    # normalised so that the expectations average to 1.0 like relative_density does.
    weighting = dict(intentional_weighting or {})
    expected: dict[int, float] = {}
    if measured:
        raw = np.array(
            [float(weighting.get(r["island_id"], 1.0)) for r in measured], dtype=float
        )
        weights = np.array([r["area_3d"] for r in measured], dtype=float)
        raw_mean = _weighted_mean(raw, weights) if weighting else 1.0
        if not math.isfinite(raw_mean) or abs(raw_mean) <= AREA_3D_EPS:
            raw_mean = 1.0
        for r, e in zip(measured, raw / raw_mean):
            expected[int(r["island_id"])] = float(e)

    outlier_island_ids: list[int] = []
    for r in measured:
        if density_mean is None or not math.isfinite(density_mean) or abs(density_mean) <= AREA_3D_EPS:
            continue
        rel = float(r["density"]) / density_mean
        r["relative_density"] = rel
        exp = expected.get(int(r["island_id"]), 1.0)
        if abs(rel - exp) > float(outlier_tolerance):
            r["outlier"] = True
            outlier_island_ids.append(int(r["island_id"]))

    all_finite = valid and all(math.isfinite(float(r["density"])) for r in measured)
    outlier_count = len(outlier_island_ids)

    checks = [
        {"name": "texel_density_finite", "passed": bool(all_finite)},
        {
            "name": "texel_density_cv",
            "value": float(density_cv),
            "limit": float(cv_max),
            "passed": bool(density_cv <= float(cv_max)),
        },
        {
            "name": "texel_density_outliers",
            "value": int(outlier_count),
            "limit": int(outlier_count_max),
            "passed": bool(outlier_count <= int(outlier_count_max)),
        },
    ]
    failures = [c["name"] for c in checks if not c["passed"]]

    return {
        "valid": bool(valid),
        "passed": bool(valid and not failures),
        "failures": failures,
        "checks": checks,
        "texture_size_px": int(texture_size_px),
        "density_mean": density_mean,
        "density_cv": float(density_cv),
        "outlier_island_ids": outlier_island_ids,
        "outlier_count": int(outlier_count),
        "outlier_tolerance": float(outlier_tolerance),
        "unmeasurable_island_count": int(unmeasurable_island_count),
        "intentional_weighting_applied": bool(weighting),
        "intentional_weighting": weighting,
        "intentional_weighting_normalized": expected if weighting else {},
        "islands": rows,
    }


def compact_texel_density(report: dict) -> dict:
    """Small summary of :func:`evaluate_texel_density` for job reports / JSON."""
    return {
        "passed": bool(report.get("passed", False)),
        "valid": bool(report.get("valid", False)),
        "failures": list(report.get("failures", [])),
        "density_mean": report.get("density_mean"),
        "density_cv": float(report.get("density_cv", 0.0)),
        "outlier_count": int(report.get("outlier_count", 0)),
        "outlier_island_ids": list(report.get("outlier_island_ids", [])),
        "unmeasurable_island_count": int(report.get("unmeasurable_island_count", 0)),
        "intentional_weighting_applied": bool(
            report.get("intentional_weighting_applied", False)
        ),
    }
