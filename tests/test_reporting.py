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
        quality_profile={"metric_version": 2, "anisotropy_p95_cap": 1.6},
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
