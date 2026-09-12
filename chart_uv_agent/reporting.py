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
from uv_agent.geometry.solution import UVMap

__all__ = [
    "write_anisotropy_heatmap_png",
    "build_seam_overlay",
    "build_run_manifest",
    "git_head_sha",
    "json_safe",
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

    Background is white and no island outline is drawn: the overlay layer above it owns
    the seams. Returns ``{"path", "size", "vmin", "vmax", "nan_faces", "max_value",
    "pixel_probe"}``; ``pixel_probe`` is the canvas colour at each face's UV centroid,
    in face order, so a test can verify the ramp without decoding the PNG.
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

    nan_faces = 0
    max_value = None
    for f in mesh.faces:
        v = float(values[f.id]) if f.id < values.size else float("nan")
        if math.isfinite(v):
            max_value = v if max_value is None else max(max_value, v)
            color = np.array(_ramp_color((v - vmin) / denom), dtype=np.uint8)
        else:
            nan_faces += 1
            color = np.array(tuple(int(c) for c in nan_color), dtype=np.uint8)
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
    return {
        "path": path,
        "size": S,
        "vmin": vmin,
        "vmax": vmax,
        "nan_faces": int(nan_faces),
        "max_value": max_value,
        "pixel_probe": probe,
    }


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


def _round3(co) -> list[float]:
    return [round(float(c), 6) for c in co]


def _edge_endpoints(mesh: MeshGraph, edge_id: int) -> tuple[list[float], list[float]]:
    a, b = mesh.edges[edge_id].vertex_ids
    return _round3(mesh.vertex_co(int(a))), _round3(mesh.vertex_co(int(b)))


def _provenance(history, candidate_history) -> dict[int, dict]:
    """Map ``edge_id -> {reason, stage, round, target_island, improvement_ratio}``.

    The pipeline records a split round as a ``history`` entry carrying ``added_edges``;
    the chart refinement loop additionally records every *candidate* it tried in
    ``candidate_history``, and only the accepted ones actually shipped edges. Later
    records win, so the record that finally cut the edge is the one reported.
    """
    out: dict[int, dict] = {}

    def _claim(edges, *, reason, stage, rnd, island, improvement):
        for eid in edges or ():
            out[int(eid)] = {
                "reason": str(reason) if reason else None,
                "stage": str(stage) if stage else None,
                "round": None if rnd is None else int(rnd),
                "target_island": None if island is None else int(island),
                "improvement_ratio": (None if improvement is None
                                      else float(improvement)),
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
    """
    locked_set = {int(e) for e in (locked or ())}
    user_set = {int(e) for e in (user or ())}
    prov = _provenance(history, candidate_history)

    edges: list[dict] = []
    type_counts: dict[str, int] = {}
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
        a, b = _edge_endpoints(mesh, eid)
        edges.append({
            "edge_id": eid,
            "type": etype,
            "reason": reason,
            "stage": info.get("stage"),
            "round": info.get("round"),
            "target_island": info.get("target_island"),
            "improvement_ratio": info.get("improvement_ratio"),
            "a": a,
            "b": b,
        })
        type_counts[etype] = type_counts.get(etype, 0) + 1

    return {
        "schema_version": SCHEMA_VERSION,
        "object_name": object_name,
        "edges": edges,
        "conflicts": json_safe(list(conflicts or ())),
        "type_counts": type_counts,
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
