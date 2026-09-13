"""Review artifact tests: anisotropy heat map, seam overlay, run manifest (G7 / G0)."""

from __future__ import annotations

import json
import math
import os

import numpy as np

from chart_uv_agent.fixtures import build_folded_planes
from chart_uv_agent.reporting import (
    build_run_manifest,
    build_seam_overlay,
    git_head_sha,
    json_safe,
    write_anisotropy_heatmap_png,
    write_seam_overlay_png,
)
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _flat_quad_grid() -> tuple[MeshGraph, UVMap]:
    """2x2 quads on z=0 over [0,1]^2, with UV == XY (identity layout)."""
    coords = [(i / 2.0, j / 2.0, 0.0) for i in range(3) for j in range(3)]

    def v(i, j):
        return i * 3 + j

    faces = [[v(i, j), v(i + 1, j), v(i + 1, j + 1), v(i, j + 1)]
             for i in range(2) for j in range(2)]
    mesh = MeshGraph.from_faces("flat_quads", coords, faces)
    uvmap = UVMap.for_mesh(mesh)
    for loop in mesh.loops:
        x, y, _z = mesh.vertex_co(loop.vertex_id)
        uvmap.set(loop.index, float(x), float(y))
    return mesh, uvmap


def test_heatmap_png_written_with_nan_and_ramp_order(tmp_path):
    mesh, uvmap = _flat_quad_grid()
    values = [1.0, 2.0, 3.0, float("nan")]
    path = str(tmp_path / "aniso.png")

    out = write_anisotropy_heatmap_png(mesh, uvmap, values, path, size=256,
                                       vmin=1.0, vmax=3.0)

    assert out["path"] == path
    assert out["size"] == 256
    assert (out["vmin"], out["vmax"]) == (1.0, 3.0)
    assert out["nan_faces"] == 1
    assert out["max_value"] == 3.0
    assert os.path.isfile(path)
    with open(path, "rb") as fh:
        assert fh.read(8) == PNG_SIGNATURE

    probe = out["pixel_probe"]
    assert len(probe) == 4
    # blue -> green/yellow -> red: red channel rises, blue channel falls.
    assert probe[0][2] > probe[0][0]                    # face 0 (vmin) is blue
    assert probe[0][0] < probe[1][0] < probe[2][0]      # red channel ramps up
    assert probe[2][0] > probe[2][2]                    # face 2 (vmax) is red
    assert probe[3] == [255, 0, 255]                    # NaN face


def test_seam_overlay_types_reasons_and_provenance():
    mesh = build_folded_planes(n=3)
    fold_edges = sorted(e.id for e in mesh.edges if e.dihedral_angle >= 90.0)
    assert fold_edges
    mandatory = fold_edges[0]
    other = sorted(e.id for e in mesh.edges if e.id not in set(fold_edges))
    seg_edge, dist_edge, locked_edge, user_edge = other[0], other[1], other[2], other[3]

    seams = [mandatory, seg_edge, dist_edge, locked_edge, user_edge]
    seam_types = {
        mandatory: "mandatory_90",
        seg_edge: "segmentation",
        dist_edge: "distortion_split",
        locked_edge: "segmentation",
        user_edge: "segmentation",
    }
    history = [
        {"stage": "refinement", "action": "split", "reason": "worst_island_distortion",
         "round": 2, "target_island": 4, "improvement_ratio": 0.42,
         "added_edges": [dist_edge]},
    ]
    candidate_history = [
        {"round": 2, "accepted": False, "improvement_ratio": 0.01,
         "candidate": {"added_edges": [seg_edge], "target_island": 9}},
    ]

    overlay = build_seam_overlay(
        mesh, seams, seam_types,
        history=history, candidate_history=candidate_history,
        conflicts=[{"edge_id": mandatory, "resolution": "mandatory_wins"}],
        object_name="folded_planes",
        locked=[locked_edge, mandatory],
        user=[user_edge],
    )

    assert overlay["schema_version"] == 1
    assert overlay["object_name"] == "folded_planes"
    by_id = {row["edge_id"]: row for row in overlay["edges"]}
    assert set(by_id) == set(seams)

    # mandatory wins over the lock; defaults fill the reason when no record claims it.
    assert by_id[mandatory]["type"] == "mandatory_90"
    assert by_id[mandatory]["reason"] == "dihedral >= 90 deg"
    assert by_id[mandatory]["improvement_ratio"] is None
    assert by_id[locked_edge]["type"] == "locked"
    assert by_id[locked_edge]["reason"] == "user locked seam"
    assert by_id[user_edge]["type"] == "user_seam"
    assert by_id[seg_edge]["type"] == "segmentation"
    # a REJECTED candidate never lends its numbers to a shipped edge
    assert by_id[seg_edge]["reason"] == "initial segmentation"
    assert by_id[seg_edge]["improvement_ratio"] is None

    split = by_id[dist_edge]
    assert split["type"] == "distortion_split"
    assert split["reason"] == "worst_island_distortion"
    assert split["round"] == 2
    assert split["target_island"] == 4
    assert split["improvement_ratio"] == 0.42
    assert split["stage"] == "refinement"

    assert sum(overlay["type_counts"].values()) == len(seams)
    for row in overlay["edges"]:
        assert len(row["a"]) == 3 and len(row["b"]) == 3
    assert overlay["conflicts"][0]["edge_id"] == mandatory
    json.dumps(overlay)


def test_run_manifest_keys_and_git_head_sha():
    manifest = build_run_manifest(
        run_id="run-0001",
        mode="auto_generate",
        seed=7,
        options={"margin_px": 4, "texture_size": 2048},
        quality_profile={"metric_version": 2, "anisotropy_global_p95_max": 1.6},
        model_path="C:/models/low.fbx",
        model_rel="models/low.fbx",
        mesh_identity={"fingerprint": "abc", "model_sha256": "deadbeef", "face_count": 12},
        blender_version="4.2.1",
        blender_build_hash="0123456789ab",
        python_version="3.11.9",
        app_version="0.4.0",
        code_sha="f" * 40,
        platform="Windows-10-10.0.19045",
        started_at="2026-01-01T00:00:00Z",
        extra={"host": "ci"},
    )
    for key in ("schema_version", "run_id", "mode", "seed", "options", "quality_profile",
                "model_path", "model_rel", "mesh_identity", "model_sha256",
                "blender_version", "blender_build_hash", "python_version", "app_version",
                "code_sha", "platform", "started_at", "extra"):
        assert key in manifest, key
    assert manifest["model_sha256"] == "deadbeef"
    assert manifest["schema_version"] == 1
    json.dumps(manifest)

    sha = git_head_sha(REPO_ROOT)
    assert sha is not None and len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha.lower())


def test_json_safe_converts_numpy_sets_nan_and_int_keys():
    payload = {
        1: np.float32(1.5),
        "i": np.int64(9),
        "arr": np.array([1.0, 2.0]),
        "set": {3, 1, 2},
        "nan": float("nan"),
        "inf": float("-inf"),
        "tuple": (1, np.float64(2.0)),
        "bool": np.bool_(True),
    }
    out = json_safe(payload)

    assert out["1"] == 1.5
    assert out["i"] == 9 and isinstance(out["i"], int)
    assert out["arr"] == [1.0, 2.0]
    assert out["set"] == [1, 2, 3]
    assert out["nan"] is None
    assert out["inf"] is None
    assert out["tuple"] == [1, 2.0]
    assert out["bool"] is True
    assert not any(isinstance(v, float) and math.isnan(v) for v in out.values())
    json.dumps(out)


# ---------------------------------------------------------------------------
# G15 reason codes / G2 rejected seam history
# ---------------------------------------------------------------------------


def _folded_with_material(n: int = 3) -> MeshGraph:
    """``build_folded_planes`` with one quad on a second material, so that quad's two
    interior edges are genuine material boundaries for the ``material`` reason code."""
    mesh = build_folded_planes(n=n)
    mesh.faces[0].material_index = 1
    return mesh


def _folded_uvmap(mesh: MeshGraph) -> UVMap:
    """Two islands: the z=0 grid in v<=0.49, the folded grid in v>=0.51 (so the fold edge
    really has two UV positions, the way a cut seam does)."""
    uvmap = UVMap.for_mesh(mesh)
    for f in mesh.faces:
        cos = [mesh.vertex_co(mesh.loops[li].vertex_id) for li in f.loop_indices]
        flat = all(abs(float(c[2])) < 1e-9 for c in cos)
        for li, co in zip(f.loop_indices, cos):
            x, y, z = (float(co[0]), float(co[1]), float(co[2]))
            if flat:
                uvmap.set(li, x, y * 0.49)
            else:
                uvmap.set(li, x, 0.51 + z * 0.49)
    return uvmap


def test_seam_overlay_reason_code_precedence_and_counts():
    from artist_uv_agent.seam_policy import material_boundary_edges

    mesh = _folded_with_material(n=3)
    mat = material_boundary_edges(mesh)
    assert mat, "fixture must expose a material boundary"
    mat_edge = sorted(mat)[0]

    fold = sorted(e.id for e in mesh.edges if e.dihedral_angle >= 90.0)
    assert fold
    mandatory = fold[0]
    boundary_edge = sorted(e.id for e in mesh.edges if e.is_boundary)[0]
    plain = sorted(
        e.id for e in mesh.edges
        if not e.is_boundary and not e.is_non_manifold
        and e.dihedral_angle < 90.0 and e.id not in mat
    )
    dist_edge, locked_edge, req_edge = plain[0], plain[1], plain[2]
    ghost = plain[3]                                    # never shipped, only proposed

    seams = [mandatory, boundary_edge, dist_edge, locked_edge, req_edge, mat_edge]
    seam_types = {
        mandatory: "mandatory_90",
        boundary_edge: "unknown",
        dist_edge: "unknown",
        locked_edge: "unknown",
        req_edge: "unknown",
        mat_edge: "unknown",
    }
    candidate_history = [
        {"round": 1, "accepted": True, "reason": "worst_island_distortion",
         "improvement_ratio": 0.3,
         "candidate": {"added_edges": [dist_edge], "target_island": 2, "kind": "short_cut",
                       "cut_reason": "distortion_repair", "cost": {"total": 1.25}}},
        {"round": 2, "accepted": False, "reason": "below_min_improvement",
         "improvement_ratio": 0.01,
         "candidate": {"added_edges": [ghost], "target_island": 5, "kind": "normal_split",
                       "cut_reason": "distortion_repair", "cost": {"total": 9.5}}},
        {"round": 3, "accepted": False, "reason": "protected_region",
         "candidate": {"added_edges": [mat_edge], "target_island": 7, "kind": "short_cut",
                       "cost": {"total": 2.0}}},
    ]

    overlay = build_seam_overlay(
        mesh, seams, seam_types,
        history=[], candidate_history=candidate_history,
        conflicts=[], object_name="folded_planes",
        locked=[locked_edge], user=[], required=[req_edge],
    )

    by_id = {row["edge_id"]: row for row in overlay["edges"]}
    assert by_id[mandatory]["reason_code"] == "mandatory_90"
    assert by_id[boundary_edge]["reason_code"] == "boundary_topology"
    assert by_id[locked_edge]["reason_code"] == "user"
    assert by_id[req_edge]["reason_code"] == "shading"
    assert by_id[mat_edge]["reason_code"] == "material"
    assert by_id[dist_edge]["reason_code"] == "distortion_added"
    # the accepted candidate's cut_reason / cost ride along for the "why" panel
    assert by_id[dist_edge]["cut_reason"] == "distortion_repair"
    assert by_id[dist_edge]["cost_total"] == 1.25
    assert by_id[mandatory]["cut_reason"] is None
    assert by_id[mandatory]["cost_total"] is None
    # the existing `type` field is untouched
    assert by_id[mandatory]["type"] == "mandatory_90"
    assert by_id[locked_edge]["type"] == "locked"

    # G2: a rejected candidate edge that never shipped is listed; one that did is not.
    rejected = overlay["rejected_candidates"]
    assert [r["edge_id"] for r in rejected] == [ghost]
    row = rejected[0]
    assert row["reason_code"] == "rejected_candidate"
    assert row["reject_reason"] == "below_min_improvement"
    assert row["round"] == 2
    assert row["target_island"] == 5
    assert row["kind"] == "normal_split"
    assert row["improvement_ratio"] == 0.01
    assert row["cost_total"] == 9.5
    assert len(row["a"]) == 3 and len(row["b"]) == 3
    assert all(r["edge_id"] != mat_edge for r in rejected)

    codes = overlay["reason_code_counts"]
    assert sum(codes.values()) == len(seams) + len(rejected)
    assert codes["rejected_candidate"] == 1
    assert sum(overlay["type_counts"].values()) == len(seams)
    json.dumps(overlay)


def test_seam_overlay_rejected_dedupes_to_last_record():
    mesh = build_folded_planes(n=3)
    edge = sorted(e.id for e in mesh.edges if not e.is_boundary)[0]
    candidate_history = [
        {"round": 1, "accepted": False, "reason": "first_try",
         "candidate": {"added_edges": [edge], "target_island": 1, "kind": "short_cut"}},
        {"round": 4, "accepted": False, "reason": "last_try",
         "candidate": {"added_edges": [edge], "target_island": 3, "kind": "normal_split"}},
    ]
    overlay = build_seam_overlay(
        mesh, [], {}, history=[], candidate_history=candidate_history,
        conflicts=[], object_name="folded_planes",
    )
    assert len(overlay["rejected_candidates"]) == 1
    assert overlay["rejected_candidates"][0]["reject_reason"] == "last_try"
    assert overlay["rejected_candidates"][0]["round"] == 4
    assert overlay["rejected_candidates"][0]["cost_total"] is None


def test_seam_overlay_png_colours_edges_by_reason_code(tmp_path):
    mesh = build_folded_planes(n=3)
    uvmap = _folded_uvmap(mesh)
    fold = sorted(e.id for e in mesh.edges if e.dihedral_angle >= 90.0)
    mandatory = fold[0]
    ghost = sorted(e.id for e in mesh.edges
                   if not e.is_boundary and e.dihedral_angle < 90.0)[0]

    overlay = build_seam_overlay(
        mesh, fold, {e: "mandatory_90" for e in fold},
        history=[], candidate_history=[
            {"round": 1, "accepted": False, "reason": "below_min_improvement",
             "candidate": {"added_edges": [ghost], "target_island": 0,
                           "kind": "short_cut"}},
        ],
        conflicts=[], object_name="folded_planes",
    )
    path = str(tmp_path / "seam_overlay.png")

    out = write_seam_overlay_png(mesh, uvmap, overlay, path, size=256, line_width=2)

    assert out["path"] == path and out["size"] == 256
    assert out["edge_count"] == len(fold)
    assert out["rejected_count"] == 1
    assert out["reason_code_counts"]["mandatory_90"] == len(fold)
    assert out["reason_code_counts"]["rejected_candidate"] == 1
    assert out["legend"]["mandatory_90"] == [220, 40, 40]
    assert out["legend"]["rejected_candidate"] == [255, 0, 255]
    assert set(out["legend"]) == {"mandatory_90", "boundary_topology", "user", "shading",
                                 "material", "distortion_added", "rejected_candidate"}
    assert os.path.isfile(path)
    with open(path, "rb") as fh:
        assert fh.read(8) == PNG_SIGNATURE

    probe = dict((eid, tuple(rgb)) for eid, rgb in out["pixel_probe"])
    assert probe[mandatory] == (220, 40, 40)            # a mandatory seam is red
    assert probe[ghost] == (255, 0, 255)                # rejected candidate is magenta
