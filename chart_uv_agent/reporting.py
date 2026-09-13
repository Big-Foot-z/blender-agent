"""Review artifacts for the app: anisotropy heatmap, seam overlay, run manifest (G7 / G0).

G7 asks the reviewer UI for three things this module produces as *pure data*:

* an anisotropy heat map rendered from the **same final UV** the UV/3D checker shows —
  so the v2 distortion numbers and the picture cannot disagree (the PNG is written
  exactly the way :mod:`uv_agent.geometry.uv_review` writes ``uv_layout.png``: NumPy
  rasterisation + the stdlib-only encoder in :mod:`uv_agent.io.png`, no Blender, no GPU,
  no Pillow);
* a seam overlay that tells mandatory / user / locked / topology / overlap / distortion
  seams apart and, for every added seam, carries the target island, the improvement ratio
  and the reason the engine cut it ("click a seam, see why");
* a G0 run manifest: commit, OS, Blender / Python / app version, model SHA-256, mesh
  fingerprint, mode, seed and the full config.

Pure module: ``numpy`` allowed, ``bpy`` forbidden, no subprocess (the commit SHA is read
straight out of ``.git``), so the whole thing is unit-testable offline.
"""

from __future__ import annotations

import math
import os

import numpy as np

from uv_agent.geometry.evaluation import _point_in_triangle
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.mesh_identity import uv_hash
from uv_agent.geometry.solution import UVMap

__all__ = [
    "write_anisotropy_heatmap_png",
    "write_heatmap_meta_json",
    "heatmap_identity_check",
    "build_seam_overlay",
    "write_seam_overlay_png",
    "build_run_manifest",
    "git_head_sha",
    "json_safe",
    "REASON_CODES",
    "REASON_CODE_COLORS",
]

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# 1. anisotropy heat map
# ---------------------------------------------------------------------------

#: Linear ramp stops (blue -> green -> yellow -> red), evenly spaced over [vmin, vmax].
_RAMP = (
    (0, 0, 255),
    (0, 255, 0),
    (255, 255, 0),
    (255, 0, 0),
)


def _ramp_color(t: float) -> tuple[int, int, int]:
    """Colour at ``t`` in [0, 1] on the blue->green->yellow->red linear ramp."""
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else float(t))
    span = len(_RAMP) - 1
    pos = t * span
    i = int(math.floor(pos))
    if i >= span:
        return _RAMP[span]
    frac = pos - i
    c0 = _RAMP[i]
    c1 = _RAMP[i + 1]
    return tuple(int(round(c0[k] + (c1[k] - c0[k]) * frac)) for k in range(3))


def write_anisotropy_heatmap_png(
    mesh: MeshGraph,
    uvmap: UVMap,
    face_values,
    path: str,
    *,
    size: int = 1024,
    vmin: float = 1.0,
    vmax: float = 3.0,
    nan_color: tuple[int, int, int] = (255, 0, 255),
    hard_fail_faces=(),
    hard_fail_color: tuple[int, int, int] = (0, 0, 0),
    meta: dict | None = None,
) -> dict:
    """Rasterise every face into UV space, coloured by ``face_values[face]`` (G7).

    ``face_values`` is the per-face anisotropy array
    (:func:`uv_agent.geometry.distortion_v2.per_face_anisotropy`): a face with no usable
    measurement is ``NaN`` and is painted ``nan_color`` rather than being silently
    dropped or clamped to "good". Values are normalised ``(v - vmin) / (vmax - vmin)``
    and clamped, then coloured on the blue->green->yellow->red ramp.

    The real triangulation (``MeshGraph.face_triangles``) is used, so a concave n-gon
    never paints outside itself, and the fill test is the same vectorised pixel-centre
    point-in-triangle predicate the overlap raster uses — the heat map and the overlap
    diagnosis therefore see identical geometry.

    ``+/-inf`` is treated like ``NaN`` — painted ``nan_color``, never clamped onto the
    ramp — and counted in ``nonfinite_faces`` (``nan_faces`` stays the NaN-only count).
    Faces listed in ``hard_fail_faces`` are painted ``hard_fail_color`` (black) whatever
    their value is, so a triangle that failed a hard gate can never look "good" (CG4).

    Background is white and no island outline is drawn: the overlay layer above it owns
    the seams. Returns ``{"path", "size", "vmin", "vmax", "nan_faces",
    "nonfinite_faces", "hard_fail_faces", "max_value", "pixel_probe", "uv_hash",
    "meta"}``; ``pixel_probe`` is the canvas colour at each face's UV centroid, in face
    order, so a test can verify the ramp without decoding the PNG, and ``meta`` is the
    caller's ``meta`` dict merged with the render identity fields so the picture and the
    numbers can be proved to come from the same UV (CG0 / CG4).
    """
    from uv_agent.io.png import write_png

    S = int(size)
    vmin = float(vmin)
    vmax = float(vmax)
    denom = vmax - vmin
    if not math.isfinite(denom) or denom <= 0.0:
        denom = 1.0

    values = np.asarray(face_values, dtype=float).reshape(-1)
    canvas = np.empty((S, S, 4), dtype=np.uint8)
    canvas[:] = (255, 255, 255, 255)

    def to_px(uv) -> tuple[float, float]:
        return uv[0] * S, (1.0 - uv[1]) * S

    hard_fail = {int(i) for i in (hard_fail_faces or ())}
    hard_fail_rgb = np.array(tuple(int(c) for c in hard_fail_color), dtype=np.uint8)

    nan_faces = 0
    nonfinite_faces = 0
    hard_fail_painted = 0
    max_value = None
    for f in mesh.faces:
        v = float(values[f.id]) if f.id < values.size else float("nan")
        if math.isfinite(v):
            max_value = v if max_value is None else max(max_value, v)
            color = np.array(_ramp_color((v - vmin) / denom), dtype=np.uint8)
        else:
            nonfinite_faces += 1
            if math.isnan(v):
                nan_faces += 1
            color = np.array(tuple(int(c) for c in nan_color), dtype=np.uint8)
        if f.id in hard_fail:
            hard_fail_painted += 1
            color = hard_fail_rgb
        for l0, l1, l2 in mesh.face_triangles(f.id):
            tri = [to_px(uvmap.get(l0)), to_px(uvmap.get(l1)), to_px(uvmap.get(l2))]
            xs = [p[0] for p in tri]
            ys = [p[1] for p in tri]
            minx = max(0, int(np.floor(min(xs))))
            maxx = min(S - 1, int(np.ceil(max(xs))))
            miny = max(0, int(np.floor(min(ys))))
            maxy = min(S - 1, int(np.ceil(max(ys))))
            if maxx < minx or maxy < miny:
                continue
            xr = np.arange(minx, maxx + 1) + 0.5
            yr = np.arange(miny, maxy + 1) + 0.5
            pgx, pgy = np.meshgrid(xr, yr)
            mask = _point_in_triangle(tri, pgx, pgy)
            if not mask.any():
                continue
            region = canvas[miny:maxy + 1, minx:maxx + 1, :3]
            region[mask] = color
            canvas[miny:maxy + 1, minx:maxx + 1, :3] = region

    probe: list[list[int]] = []
    for f in mesh.faces:
        uvs = [uvmap.get(li) for li in f.loop_indices]
        if not uvs:
            probe.append([255, 255, 255])
            continue
        cu = sum(p[0] for p in uvs) / len(uvs)
        cv = sum(p[1] for p in uvs) / len(uvs)
        px, py = to_px((cu, cv))
        ix = min(S - 1, max(0, int(px)))
        iy = min(S - 1, max(0, int(py)))
        probe.append([int(c) for c in canvas[iy, ix, :3]])

    write_png(path, canvas)
    digest = uv_hash(uvmap)
    out_meta = dict(meta or {})
    out_meta.update({
        "uv_hash": digest,
        "size": S,
        "vmin": vmin,
        "vmax": vmax,
        "nan_color": [int(c) for c in nan_color],
        "hard_fail_color": [int(c) for c in hard_fail_color],
    })
    return {
        "path": path,
        "size": S,
        "vmin": vmin,
        "vmax": vmax,
        "nan_faces": int(nan_faces),
        "nonfinite_faces": int(nonfinite_faces),
        "hard_fail_faces": int(hard_fail_painted),
        "max_value": max_value,
        "pixel_probe": probe,
        "uv_hash": digest,
        "meta": out_meta,
    }


def write_heatmap_meta_json(path: str, meta: dict) -> None:
    """Dump the heat-map identity ``meta`` block next to the PNG (JSON-safe)."""
    import json

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(json_safe(meta), fh, indent=2, sort_keys=True)
        fh.write("\n")


#: Fields a heat map must carry to prove it was rendered from the reported run/UV.
_HEATMAP_IDENTITY_FIELDS = ("uv_hash", "mesh_fingerprint", "metric_version", "run_id")


def heatmap_identity_check(
    heatmap_meta: dict,
    *,
    uv_hash: str,
    mesh_fingerprint: str,
    metric_version,
    run_id,
) -> dict:
    """Gate data identity (CG4): does the picture come from the reported run/UV?

    Compares ``uv_hash`` / ``mesh_fingerprint`` / ``metric_version`` / ``run_id`` in
    ``heatmap_meta`` against the expected values; a field missing from the meta block is
    a mismatch, not a pass. Returns ``{"passed", "mismatches"}``.
    """
    expected = {
        "uv_hash": uv_hash,
        "mesh_fingerprint": mesh_fingerprint,
        "metric_version": metric_version,
        "run_id": run_id,
    }
    meta = heatmap_meta or {}
    mismatches = [
        field for field in _HEATMAP_IDENTITY_FIELDS
        if field not in meta or meta.get(field) != expected[field]
    ]
    return {"passed": not mismatches, "mismatches": mismatches}


# ---------------------------------------------------------------------------
# 2. seam overlay
# ---------------------------------------------------------------------------

#: Default human reason per seam type, used when no history record claims the edge.
_TYPE_REASON = {
    "mandatory_90": "dihedral >= 90 deg",
    "segmentation": "initial segmentation",
    "welded_fold_auxiliary": "fold boundary cut",
    "mandatory_fold_auxiliary": "fold boundary cut",
    "overlap_repair": "overlap repair",
    "distortion_split": "distortion refinement",
    "locked": "user locked seam",
    "user_seam": "user seam",
    "correctness_repair": "correctness repair",
}


#: G15 seam-overlay reason codes, in precedence order (first match wins).
REASON_CODES = (
    "mandatory_90",
    "boundary_topology",
    "user",
    "shading",
    "material",
    "distortion_added",
    "rejected_candidate",
)

#: Seam types that mean "the topology / correctness layer forced this cut".
_TOPOLOGY_TYPES = frozenset({
    "segmentation",
    "welded_fold_auxiliary",
    "mandatory_fold_auxiliary",
    "overlap_repair",
    "correctness_repair",
})

#: Overlay line colour per reason code (RGB).
REASON_CODE_COLORS = {
    "mandatory_90": (220, 40, 40),
    "boundary_topology": (120, 120, 120),
    "user": (40, 80, 220),
    "shading": (160, 40, 200),
    "material": (240, 140, 20),
    "distortion_added": (30, 170, 60),
    "rejected_candidate": (255, 0, 255),
}


def _round3(co) -> list[float]:
    return [round(float(c), 6) for c in co]


def _edge_endpoints(mesh: MeshGraph, edge_id: int) -> tuple[list[float], list[float]]:
    a, b = mesh.edges[edge_id].vertex_ids
    return _round3(mesh.vertex_co(int(a))), _round3(mesh.vertex_co(int(b)))


def _cost_total(cost):
    """The scalar cost of a candidate: ``cost["total"]`` for a breakdown dict, the number
    itself when the record already stored a scalar, ``None`` when nothing usable is there."""
    if cost is None:
        return None
    if isinstance(cost, dict):
        value = cost.get("total")
    else:
        value = cost
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _provenance(history, candidate_history) -> dict[int, dict]:
    """Map ``edge_id -> {reason, stage, round, target_island, improvement_ratio}``.

    The pipeline records a split round as a ``history`` entry carrying ``added_edges``;
    the chart refinement loop additionally records every *candidate* it tried in
    ``candidate_history``, and only the accepted ones actually shipped edges. Later
    records win, so the record that finally cut the edge is the one reported.
    """
    out: dict[int, dict] = {}

    def _claim(edges, *, reason, stage, rnd, island, improvement,
               cut_reason=None, cost_total=None):
        for eid in edges or ():
            out[int(eid)] = {
                "reason": str(reason) if reason else None,
                "stage": str(stage) if stage else None,
                "round": None if rnd is None else int(rnd),
                "target_island": None if island is None else int(island),
                "improvement_ratio": (None if improvement is None
                                      else float(improvement)),
                "cut_reason": str(cut_reason) if cut_reason else None,
                "cost_total": cost_total,
            }

    for rec in candidate_history or ():
        if not isinstance(rec, dict) or not rec.get("accepted"):
            continue
        cand = rec.get("candidate") or {}
        island = rec.get("target_island", cand.get("target_island"))
        _claim(
            cand.get("added_edges"),
            reason=rec.get("reason") or cand.get("reason") or rec.get("target_metric"),
            stage=rec.get("stage") or "candidate",
            rnd=rec.get("round"),
            island=island,
            improvement=rec.get("improvement_ratio"),
            cut_reason=cand.get("cut_reason"),
            cost_total=_cost_total(cand.get("cost")),
        )

    for rec in history or ():
        if not isinstance(rec, dict):
            continue
        if rec.get("reverted") or rec.get("unresolved"):
            continue
        edges = rec.get("added_edges") or rec.get("added")
        if not edges:
            continue
        island = rec.get("target_island", rec.get("split_island"))
        _claim(
            edges,
            reason=rec.get("reason"),
            stage=rec.get("stage") or rec.get("action"),
            rnd=rec.get("round"),
            island=island,
            improvement=rec.get("improvement_ratio"),
        )
    return out


def build_seam_overlay(
    mesh: MeshGraph,
    seams,
    seam_types: dict,
    *,
    history: list,
    candidate_history: list,
    conflicts: list,
    object_name: str | None,
    locked=(),
    user=(),
    required=(),
    material_edges=None,
) -> dict:
    """3D seam overlay payload for the reviewer UI (G7).

    Every shipped seam becomes one entry with its 3D endpoints and a ``type`` that keeps
    mandatory / user / locked / topology (``segmentation``) / overlap / distortion apart.
    Precedence: a ``mandatory_90`` classification always wins (the engine may not drop a
    ≥90° fold, so it must never be displayed as a discretionary cut); then a user *locked*
    edge, then a user-authored edge, then whatever the pipeline classified.

    ``reason`` / ``round`` / ``target_island`` / ``improvement_ratio`` come from the split
    record (``history``) or accepted candidate (``candidate_history``) that added the edge,
    which is what makes "click an added seam -> target, improvement, why" answerable. An
    edge no record claims (initial segmentation, a mandatory fold) gets the per-type
    default reason and ``None`` for the rest — never a fabricated number.

    On top of ``type`` every entry also carries a ``reason_code`` out of the fixed G15
    vocabulary (:data:`REASON_CODES`), so the overlay legend is a closed set the UI can
    colour without knowing the engine's internal type names. Precedence is
    ``mandatory_90`` (never displayable as discretionary) -> ``boundary_topology``
    (open / non-manifold edge, or a cut the segmentation / correctness layer forced) ->
    ``user`` -> ``shading`` (an edge in ``required``) -> ``material`` (a material boundary;
    computed with :func:`artist_uv_agent.seam_policy.material_boundary_edges` when
    ``material_edges`` is None) -> ``distortion_added``. Anything with no signal at all
    falls back to ``boundary_topology`` rather than inventing a motive.

    ``rejected_candidates`` is the G2 history of cuts the engine *tried and dropped*: every
    non-accepted candidate edge that is not in the final seam set, with the reject reason,
    the round, the target island and the candidate cost — the "what did it consider?"
    half of the seam story, kept separate from the shipped ``edges`` list.
    """
    locked_set = {int(e) for e in (locked or ())}
    user_set = {int(e) for e in (user or ())}
    required_set = {int(e) for e in (required or ())}
    if material_edges is None:
        from artist_uv_agent.seam_policy import material_boundary_edges
        material_set = {int(e) for e in material_boundary_edges(mesh)}
    else:
        material_set = {int(e) for e in material_edges}
    prov = _provenance(history, candidate_history)
    seam_set = {int(e) for e in (seams or ())}

    edges: list[dict] = []
    type_counts: dict[str, int] = {}
    reason_code_counts: dict[str, int] = {}
    for eid in sorted(int(e) for e in (seams or ())):
        base = seam_types.get(eid) if seam_types else None
        base = str(base) if base else "unknown"
        if base == "mandatory_90":
            etype = "mandatory_90"
        elif eid in locked_set:
            etype = "locked"
        elif eid in user_set:
            etype = "user_seam"
        else:
            etype = base
        info = prov.get(eid) or {}
        reason = info.get("reason") or _TYPE_REASON.get(etype)
        cut_reason = info.get("cut_reason")
        code = _reason_code(mesh, eid, etype, cut_reason,
                            user_set=user_set | locked_set,
                            required=required_set, material=material_set)
        a, b = _edge_endpoints(mesh, eid)
        edges.append({
            "edge_id": eid,
            "type": etype,
            "reason_code": code,
            "reason": reason,
            "cut_reason": cut_reason,
            "cost_total": info.get("cost_total"),
            "stage": info.get("stage"),
            "round": info.get("round"),
            "target_island": info.get("target_island"),
            "improvement_ratio": info.get("improvement_ratio"),
            "a": a,
            "b": b,
        })
        type_counts[etype] = type_counts.get(etype, 0) + 1
        reason_code_counts[code] = reason_code_counts.get(code, 0) + 1

    rejected = _rejected_candidates(mesh, candidate_history, seam_set)
    if rejected:
        reason_code_counts["rejected_candidate"] = len(rejected)

    return {
        "schema_version": SCHEMA_VERSION,
        "object_name": object_name,
        "edges": edges,
        "rejected_candidates": rejected,
        "conflicts": json_safe(list(conflicts or ())),
        "type_counts": type_counts,
        "reason_code_counts": reason_code_counts,
    }


def _reason_code(mesh: MeshGraph, eid: int, etype: str, cut_reason, *,
                 user_set: set[int], required: set[int], material: set[int]) -> str:
    """The G15 ``reason_code`` for one shipped seam (see :func:`build_seam_overlay`)."""
    if etype == "mandatory_90":
        return "mandatory_90"
    edge = mesh.edges[eid] if 0 <= eid < mesh.edge_count else None
    if edge is not None and (edge.is_boundary or edge.is_non_manifold):
        return "boundary_topology"
    if etype in _TOPOLOGY_TYPES:
        return "boundary_topology"
    if etype in ("locked", "user_seam") or eid in user_set:
        return "user"
    if eid in required:
        return "shading"
    if eid in material:
        return "material"
    if etype == "distortion_split" or cut_reason == "distortion_repair":
        return "distortion_added"
    return "boundary_topology"


def _rejected_candidates(mesh: MeshGraph, candidate_history, seam_set: set[int]) -> list[dict]:
    """G2: the edges a *rejected* candidate would have cut and that never shipped.

    Later records win (an edge proposed twice is reported with the reason it was dropped
    the last time), and an edge that ended up in the final seam set is not listed at all —
    something else cut it, so calling it "rejected" would be a lie.
    """
    claimed: dict[int, dict] = {}
    for rec in candidate_history or ():
        if not isinstance(rec, dict) or rec.get("accepted"):
            continue
        cand = rec.get("candidate") or {}
        added = cand.get("added_edges") or ()
        if not added:
            continue
        island = rec.get("target_island", cand.get("target_island"))
        improvement = rec.get("improvement_ratio")
        for eid in added:
            eid = int(eid)
            if eid in seam_set:
                continue
            if not (0 <= eid < mesh.edge_count):
                continue
            a, b = _edge_endpoints(mesh, eid)
            reject_reason = rec.get("reason")
            claimed[eid] = {
                "edge_id": eid,
                "reason_code": "rejected_candidate",
                "reject_reason": str(reject_reason) if reject_reason else None,
                "round": None if rec.get("round") is None else int(rec["round"]),
                "target_island": None if island is None else int(island),
                "kind": cand.get("kind"),
                "improvement_ratio": (None if improvement is None
                                      else float(improvement)),
                "cost_total": _cost_total(cand.get("cost")),
                "a": a,
                "b": b,
            }
    return [claimed[k] for k in sorted(claimed)]


# ---------------------------------------------------------------------------
# 2b. seam overlay raster (seam_overlay.png)
# ---------------------------------------------------------------------------

#: UV-space fill behind the overlay lines (the islands, so a seam reads as a border).
_OVERLAY_FILL = (235, 235, 235)

#: Dash period in pixels for ``rejected_candidate`` lines (draw 4, skip 4).
_DASH = 4


def _edge_uv_segments(mesh: MeshGraph, uvmap: UVMap, eid: int) -> list[tuple[tuple, tuple]]:
    """The UV segment of ``eid`` inside *each* incident face.

    A seam is exactly the case where the two faces disagree about the UV of the shared
    vertices, so one 3D edge has up to two distinct UV segments — drawing both is what
    makes the overlay line up with the island borders on either side of the cut.
    """
    if not (0 <= eid < mesh.edge_count):
        return []
    va, vb = (int(v) for v in mesh.edges[eid].vertex_ids)
    out: list[tuple[tuple, tuple]] = []
    for fid in mesh.edges[eid].face_ids:
        face = mesh.faces[int(fid)]
        la = lb = None
        for li in face.loop_indices:
            vid = mesh.loops[li].vertex_id
            if vid == va and la is None:
                la = li
            elif vid == vb and lb is None:
                lb = li
        if la is None or lb is None:
            continue
        out.append((tuple(uvmap.get(la)), tuple(uvmap.get(lb))))
    return out


def _draw_line(canvas, p0, p1, color, width: int, *, dashed: bool = False) -> None:
    """Integer DDA line of ``width`` pixels, clipped to the canvas, optionally dashed."""
    S = canvas.shape[0]
    x0, y0 = int(round(p0[0])), int(round(p0[1]))
    x1, y1 = int(round(p1[0])), int(round(p1[1]))
    steps = max(abs(x1 - x0), abs(y1 - y0))
    col = np.array(color, dtype=np.uint8)
    half = max(0, int(width) // 2)
    if steps == 0:
        pts = [(x0, y0)]
    else:
        t = np.arange(steps + 1, dtype=float) / float(steps)
        xs = np.rint(x0 + (x1 - x0) * t).astype(int)
        ys = np.rint(y0 + (y1 - y0) * t).astype(int)
        if dashed:
            # Every other 4px run, with the dash phase anchored so the run straddles the
            # segment midpoint: that is where ``pixel_probe`` samples, and a probe that
            # landed in a gap would report "no seam drawn" for a line that is drawn.
            phase = np.arange(steps + 1) - (steps // 2 - _DASH // 2)
            keep = ((phase // _DASH) % 2) == 0
            xs, ys = xs[keep], ys[keep]
        pts = list(zip(xs.tolist(), ys.tolist()))
    for px, py in pts:
        xa = max(0, px - half)
        xb = min(S - 1, px + half + (1 if int(width) % 2 == 0 else 0))
        ya = max(0, py - half)
        yb = min(S - 1, py + half + (1 if int(width) % 2 == 0 else 0))
        if xb < xa or yb < ya:
            continue
        canvas[ya:yb + 1, xa:xb + 1, :3] = col


def write_seam_overlay_png(
    mesh: MeshGraph,
    uvmap: UVMap,
    overlay: dict,
    path: str,
    *,
    size: int = 1024,
    line_width: int = 2,
) -> dict:
    """Rasterise a :func:`build_seam_overlay` payload into ``seam_overlay.png`` (G15).

    Every overlay edge is drawn in UV space, once per incident face, coloured by its
    ``reason_code`` (:data:`REASON_CODE_COLORS`); rejected candidates are drawn last-but-
    under, dashed magenta, so "the cut it did not make" is visibly different from a real
    seam instead of being invisible. The island fill underneath is flat light gray — this
    layer answers *where and why the seams are*, the anisotropy heat map owns colour.

    Encoded with the same stdlib-only writer as every other review PNG (no Blender, no
    GPU, no Pillow). Returns ``{"path", "size", "edge_count", "rejected_count",
    "reason_code_counts", "legend", "pixel_probe"}``; ``pixel_probe`` is
    ``[[edge_id, [r, g, b]], ...]`` sampled at each drawn edge's UV midpoint in its first
    incident face, so a test can verify the colouring without decoding the PNG.
    """
    from uv_agent.io.png import write_png

    S = int(size)
    canvas = np.empty((S, S, 4), dtype=np.uint8)
    canvas[:] = (255, 255, 255, 255)

    def to_px(uv) -> tuple[float, float]:
        return uv[0] * S, (1.0 - uv[1]) * S

    fill = np.array(_OVERLAY_FILL, dtype=np.uint8)
    for f in mesh.faces:
        for l0, l1, l2 in mesh.face_triangles(f.id):
            tri = [to_px(uvmap.get(l0)), to_px(uvmap.get(l1)), to_px(uvmap.get(l2))]
            xs = [p[0] for p in tri]
            ys = [p[1] for p in tri]
            minx = max(0, int(np.floor(min(xs))))
            maxx = min(S - 1, int(np.ceil(max(xs))))
            miny = max(0, int(np.floor(min(ys))))
            maxy = min(S - 1, int(np.ceil(max(ys))))
            if maxx < minx or maxy < miny:
                continue
            xr = np.arange(minx, maxx + 1) + 0.5
            yr = np.arange(miny, maxy + 1) + 0.5
            pgx, pgy = np.meshgrid(xr, yr)
            mask = _point_in_triangle(tri, pgx, pgy)
            if not mask.any():
                continue
            region = canvas[miny:maxy + 1, minx:maxx + 1, :3]
            region[mask] = fill
            canvas[miny:maxy + 1, minx:maxx + 1, :3] = region

    rows = list((overlay or {}).get("edges") or ())
    rejected = list((overlay or {}).get("rejected_candidates") or ())

    counts: dict[str, int] = {}
    drawn: list[tuple[int, str]] = []
    # Rejected first (drawn under), shipped seams on top.
    for row, dashed in [(r, True) for r in rejected] + [(r, False) for r in rows]:
        eid = int(row.get("edge_id"))
        code = str(row.get("reason_code") or "boundary_topology")
        color = REASON_CODE_COLORS.get(code, (0, 0, 0))
        segments = _edge_uv_segments(mesh, uvmap, eid)
        if not segments:
            continue
        for uv0, uv1 in segments:
            _draw_line(canvas, to_px(uv0), to_px(uv1), color, int(line_width),
                       dashed=dashed)
        counts[code] = counts.get(code, 0) + 1
        drawn.append((eid, code))

    probe: list[list] = []
    for eid, _code in drawn:
        segments = _edge_uv_segments(mesh, uvmap, eid)
        if not segments:
            continue
        uv0, uv1 = segments[0]
        mu = (uv0[0] + uv1[0]) / 2.0
        mv = (uv0[1] + uv1[1]) / 2.0
        px, py = to_px((mu, mv))
        # Same rounding the DDA used, so the probe lands on the pixel the line wrote.
        ix = min(S - 1, max(0, int(round(px))))
        iy = min(S - 1, max(0, int(round(py))))
        probe.append([eid, [int(c) for c in canvas[iy, ix, :3]]])

    write_png(path, canvas)
    return {
        "path": path,
        "size": S,
        "edge_count": len(rows),
        "rejected_count": len(rejected),
        "reason_code_counts": counts,
        "legend": {code: list(REASON_CODE_COLORS[code]) for code in REASON_CODES},
        "pixel_probe": probe,
    }


# ---------------------------------------------------------------------------
# 3. run manifest
# ---------------------------------------------------------------------------


def build_run_manifest(
    *,
    run_id,
    mode,
    seed,
    options: dict,
    quality_profile: dict,
    model_path: str | None,
    model_rel: str | None,
    mesh_identity: dict,
    blender_version: str | None,
    blender_build_hash: str | None,
    python_version: str,
    app_version: str | None,
    code_sha: str | None,
    platform: str,
    started_at: str,
    extra: dict | None = None,
) -> dict:
    """The G0 reproducibility record for one run.

    Everything needed to re-run the exact same computation and to prove the approved
    low-poly was not modified: the commit (``code_sha``), the OS (``platform``), the
    Blender / Python / app versions, the model SHA-256 (lifted out of ``mesh_identity``
    so a reader does not have to know that block's shape), the mesh fingerprint, the mode,
    the seed and the full option + quality-profile config.
    """
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "mode": mode,
        "seed": seed,
        "options": dict(options or {}),
        "quality_profile": dict(quality_profile or {}),
        "model_path": model_path,
        "model_rel": model_rel,
        "mesh_identity": dict(mesh_identity or {}),
        "model_sha256": (mesh_identity or {}).get("model_sha256"),
        "blender_version": blender_version,
        "blender_build_hash": blender_build_hash,
        "python_version": python_version,
        "app_version": app_version,
        "code_sha": code_sha,
        "platform": platform,
        "started_at": started_at,
    }
    if extra:
        manifest["extra"] = dict(extra)
    return json_safe(manifest)


def git_head_sha(repo_root: str) -> str | None:
    """The commit SHA at ``repo_root``'s HEAD, read from ``.git`` without subprocess.

    A worker may run where ``git`` is not on PATH (packaged app, sandbox), and shelling
    out from inside a pure module is not acceptable — so ``HEAD`` is parsed directly:
    a detached SHA, a ``ref:`` into ``refs/``, or the ``packed-refs`` fallback. Returns
    ``None`` on anything unexpected rather than raising: a missing commit id degrades the
    manifest, it must not kill the run.
    """
    try:
        git_dir = os.path.join(str(repo_root), ".git")
        if os.path.isfile(git_dir):                     # worktree / submodule pointer
            with open(git_dir, "r", encoding="utf-8") as fh:
                line = fh.read().strip()
            if not line.startswith("gitdir:"):
                return None
            git_dir = line.split(":", 1)[1].strip()
            if not os.path.isabs(git_dir):
                git_dir = os.path.join(str(repo_root), git_dir)
        head_path = os.path.join(git_dir, "HEAD")
        with open(head_path, "r", encoding="utf-8") as fh:
            head = fh.read().strip()
        if not head.startswith("ref:"):
            return head if _is_sha(head) else None
        ref = head.split(":", 1)[1].strip()
        ref_path = os.path.join(git_dir, *ref.split("/"))
        if os.path.isfile(ref_path):
            with open(ref_path, "r", encoding="utf-8") as fh:
                sha = fh.read().strip()
            return sha if _is_sha(sha) else None
        packed = os.path.join(git_dir, "packed-refs")
        if os.path.isfile(packed):
            with open(packed, "r", encoding="utf-8") as fh:
                for row in fh:
                    row = row.strip()
                    if not row or row.startswith(("#", "^")):
                        continue
                    parts = row.split(None, 1)
                    if len(parts) == 2 and parts[1].strip() == ref and _is_sha(parts[0]):
                        return parts[0]
        return None
    except OSError:
        return None


def _is_sha(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdefABCDEF" for c in value)


# ---------------------------------------------------------------------------
# 4. JSON safety
# ---------------------------------------------------------------------------


def json_safe(value):
    """Recursively convert ``value`` into something :func:`json.dumps` accepts.

    NumPy scalars and arrays become Python scalars / lists, sets become sorted lists
    (deterministic output — two runs of the same input must produce byte-identical JSON
    for G0), tuples become lists, non-finite floats become ``None`` (JSON has no NaN, and
    a silently emitted ``NaN`` token breaks every strict reader), and integer dict keys
    become strings so ``{edge_id: ...}`` maps survive a round trip.
    """
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, np.ndarray):
        return [json_safe(v) for v in value.tolist()]
    if isinstance(value, np.generic):
        return json_safe(value.item())
    if isinstance(value, dict):
        return {_json_key(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        try:
            items = sorted(value)
        except TypeError:
            items = sorted(value, key=repr)
        return [json_safe(v) for v in items]
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


def _json_key(key) -> str:
    if isinstance(key, str):
        return key
    if isinstance(key, (int, np.integer, float, np.floating, bool, np.bool_)):
        return str(json_safe(key))
    return str(key)
