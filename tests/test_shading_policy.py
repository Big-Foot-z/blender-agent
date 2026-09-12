"""Tests for uv_agent.geometry.shading_policy (Gate G10, game shading / tangents).

Everything here is Blender-free: the mesh-data snapshots use the same fake mesh
pattern as tests/test_smoothing_split.py (plus a ``vertices`` attribute on the
fake edge), and the UV side uses a MeshGraph + UVMap.
"""

import pytest

from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.shading_policy import (
    SHADING_POLICIES,
    compare_shading_snapshots,
    evaluate_shading_policy,
    required_seam_edges_for_policy,
    shading_snapshot,
    sharp_edge_uv_audit,
)
from uv_agent.geometry.solution import UVMap
from uv_agent.io.fixtures import build_grid_plane


# -- fake Blender mesh data --------------------------------------------------
class _FakeEdge:
    def __init__(self, index, sharp=False, vertices=None):
        self.index = index
        self.use_edge_sharp = sharp
        if vertices is not None:
            self.vertices = list(vertices)


class _FakePoly:
    def __init__(self, smooth=False):
        self.use_smooth = smooth


class _FakeMesh:
    def __init__(self, n_edges, n_polys, presharp=(), smooth=True, edge_verts=None):
        sharp = set(presharp)
        self.edges = [
            _FakeEdge(i, i in sharp, None if edge_verts is None else edge_verts[i])
            for i in range(n_edges)
        ]
        self.polygons = [_FakePoly(smooth) for _ in range(n_polys)]


# -- fixtures ----------------------------------------------------------------
NX, NY = 4, 2
#: middle column of vertical edges of the 4x2 grid plane (vertex ids j*(NX+1)+i, i=2)
SHARP_KEYS = [(2, 7), (7, 12)]


def _sharp_grid() -> MeshGraph:
    base = build_grid_plane(nx=NX, ny=NY)
    return MeshGraph.from_faces(
        "sharp_plane",
        [v.co for v in base.vertices],
        [list(f.vertex_ids) for f in base.faces],
        sharp_edge_keys=SHARP_KEYS,
    )


def _identity_uv(mesh: MeshGraph, *, split_right: bool = False) -> UVMap:
    uvmap = UVMap.for_mesh(mesh)
    for loop in mesh.loops:
        x, y, _ = mesh.vertices[loop.vertex_id].co
        du = 0.0
        if split_right and (loop.face_id % NX) >= 2:  # right half of the grid
            du = 2.0
        uvmap.set(loop.index, x + du, y)
    return uvmap


def _fake_from_graph(mesh: MeshGraph, sharp_edge_ids, *, smooth=True) -> _FakeMesh:
    return _FakeMesh(
        mesh.edge_count,
        mesh.face_count,
        presharp=sharp_edge_ids,
        smooth=smooth,
        edge_verts=[list(e.vertex_ids) for e in mesh.edges],
    )


# -- (a) snapshot / compare --------------------------------------------------
def test_snapshot_shape_and_counts():
    mesh = _FakeMesh(n_edges=12, n_polys=6, presharp=[1, 3], edge_verts=[[i, i + 1] for i in range(12)])
    snap = shading_snapshot(mesh)
    assert snap["edge_count"] == 12
    assert snap["face_count"] == 6
    assert snap["sharp_edge_count"] == 2
    assert snap["smooth_face_count"] == 6
    assert snap["sharp_edge_keys"] == [(1, 2), (3, 4)]
    assert len(snap["sharp_hash"]) == 64
    assert len(snap["smooth_hash"]) == 64


def test_snapshot_without_vertices_uses_edge_index():
    snap = shading_snapshot(_FakeMesh(n_edges=6, n_polys=2, presharp=[4, 0], smooth=False))
    assert snap["sharp_edge_keys"] == [0, 4]
    assert snap["smooth_face_count"] == 0


def test_compare_unchanged():
    a = shading_snapshot(_FakeMesh(12, 6, presharp=[1]))
    b = shading_snapshot(_FakeMesh(12, 6, presharp=[1]))
    diff = compare_shading_snapshots(a, b)
    assert diff == {
        "unchanged": True,
        "sharp_added": [],
        "sharp_removed": [],
        "smooth_face_delta": 0,
    }


def test_compare_detects_added_and_removed_sharp():
    before = shading_snapshot(_FakeMesh(12, 6, presharp=[1]))
    after = shading_snapshot(_FakeMesh(12, 6, presharp=[5]))
    diff = compare_shading_snapshots(before, after)
    assert diff["unchanged"] is False
    assert diff["sharp_added"] == [5]
    assert diff["sharp_removed"] == [1]


def test_compare_detects_smooth_change():
    before = shading_snapshot(_FakeMesh(12, 6, smooth=True))
    after = shading_snapshot(_FakeMesh(12, 6, smooth=False))
    diff = compare_shading_snapshots(before, after)
    assert diff["unchanged"] is False
    assert diff["smooth_face_delta"] == -6


# -- (b) preserve ------------------------------------------------------------
def test_preserve_passes_when_unchanged():
    mesh = _sharp_grid()
    uvmap = _identity_uv(mesh)
    snap = shading_snapshot(_fake_from_graph(mesh, []))
    res = evaluate_shading_policy(
        "preserve", mesh=mesh, uvmap=uvmap, seams=set(),
        before_snapshot=snap, after_snapshot=snap,
    )
    assert res["passed"] is True
    assert res["valid"] is True
    assert res["failures"] == []
    assert res["snapshot_diff"]["unchanged"] is True
    assert res["tangent_checked"] is False


def test_preserve_fails_when_an_edge_became_sharp():
    mesh = _sharp_grid()
    uvmap = _identity_uv(mesh)
    before = shading_snapshot(_fake_from_graph(mesh, []))
    after = shading_snapshot(_fake_from_graph(mesh, [3]))
    res = evaluate_shading_policy(
        "preserve", mesh=mesh, uvmap=uvmap, seams=set(),
        before_snapshot=before, after_snapshot=after,
    )
    assert res["passed"] is False
    assert res["failures"] == ["shading_state_changed"]
    assert res["snapshot_diff"]["sharp_added"]


def test_preserve_without_snapshots_is_invalid():
    mesh = _sharp_grid()
    res = evaluate_shading_policy(
        "preserve", mesh=mesh, uvmap=_identity_uv(mesh), seams=set(),
    )
    assert res["valid"] is False
    assert res["passed"] is False
    assert res["invalid_reasons"] == ["shading_snapshot_missing"]
    assert "snapshot_diff" not in res


# -- (c) require_uv_seam_on_sharp_edges --------------------------------------
def test_sharp_edge_uv_audit_flags_welded_sharp_edges():
    mesh = _sharp_grid()
    audit = sharp_edge_uv_audit(mesh, _identity_uv(mesh))
    assert audit["sharp_edge_count"] == 2
    assert audit["sharp_edge_uv_unsplit"] == 2
    assert len(audit["unsplit_edge_ids"]) == 2


def test_require_policy_fails_on_welded_identity_uv():
    mesh = _sharp_grid()
    res = evaluate_shading_policy(
        "require_uv_seam_on_sharp_edges",
        mesh=mesh, uvmap=_identity_uv(mesh), seams=set(),
    )
    assert res["passed"] is False
    assert res["failures"] == ["sharp_edge_not_uv_split", "sharp_edge_not_seam"]
    assert res["sharp_edge_uv_audit"]["sharp_edge_uv_unsplit"] == 2


def test_require_policy_passes_when_uv_split_and_seamed():
    mesh = _sharp_grid()
    uvmap = _identity_uv(mesh, split_right=True)
    sharp_ids = {mesh.edge_key(*k) for k in SHARP_KEYS}
    res = evaluate_shading_policy(
        "require_uv_seam_on_sharp_edges", mesh=mesh, uvmap=uvmap, seams=sharp_ids,
    )
    assert res["sharp_edge_uv_audit"]["sharp_edge_uv_unsplit"] == 0
    assert res["failures"] == []
    assert res["passed"] is True


def test_require_policy_fails_when_seam_set_missing_sharp_edge():
    mesh = _sharp_grid()
    uvmap = _identity_uv(mesh, split_right=True)
    sharp_ids = [mesh.edge_key(*k) for k in SHARP_KEYS]
    res = evaluate_shading_policy(
        "require_uv_seam_on_sharp_edges", mesh=mesh, uvmap=uvmap, seams={sharp_ids[0]},
    )
    assert res["failures"] == ["sharp_edge_not_seam"]
    assert res["passed"] is False


# -- (d) split_normals_on_uv_seams -------------------------------------------
def _split_case(after_sharp_ids, seam_ids, before_sharp_ids=SHARP_KEYS):
    mesh = _sharp_grid()
    before_ids = [mesh.edge_key(*k) for k in before_sharp_ids]
    before = shading_snapshot(_fake_from_graph(mesh, before_ids))
    after = shading_snapshot(_fake_from_graph(mesh, after_sharp_ids))
    return evaluate_shading_policy(
        "split_normals_on_uv_seams",
        mesh=mesh, uvmap=_identity_uv(mesh, split_right=True), seams=seam_ids,
        before_snapshot=before, after_snapshot=after,
    )


def test_split_policy_passes_when_after_covers_before_and_seams():
    mesh = _sharp_grid()
    before_ids = [mesh.edge_key(*k) for k in SHARP_KEYS]
    seam_ids = {0, 1}
    res = _split_case(sorted(set(before_ids) | seam_ids), seam_ids)
    assert res["failures"] == []
    assert res["passed"] is True
    assert res["snapshot_diff"]["sharp_removed"] == []


def test_split_policy_fails_when_a_seam_edge_is_not_sharp():
    mesh = _sharp_grid()
    before_ids = [mesh.edge_key(*k) for k in SHARP_KEYS]
    seam_ids = {0, 1}
    res = _split_case(sorted(set(before_ids) | {0}), seam_ids)  # edge 1 missing
    assert res["failures"] == ["seam_not_sharp"]
    assert res["passed"] is False


def test_split_policy_fails_when_original_sharp_removed():
    mesh = _sharp_grid()
    before_ids = [mesh.edge_key(*k) for k in SHARP_KEYS]
    seam_ids = {0, 1}
    after_ids = sorted((set(before_ids) | seam_ids) - {before_ids[0]})
    res = _split_case(after_ids, seam_ids)
    assert "sharp_edge_removed" in res["failures"]
    assert res["passed"] is False


# -- (e) tangent flag --------------------------------------------------------
def test_tangent_ok_false_is_a_failure_for_any_policy():
    mesh = _sharp_grid()
    uvmap = _identity_uv(mesh, split_right=True)
    sharp_ids = {mesh.edge_key(*k) for k in SHARP_KEYS}
    res = evaluate_shading_policy(
        "require_uv_seam_on_sharp_edges",
        mesh=mesh, uvmap=uvmap, seams=sharp_ids, tangent_ok=False,
    )
    assert res["failures"] == ["tangent_basis_failed"]
    assert res["passed"] is False
    assert res["tangent_checked"] is True
    assert res["tangent_ok"] is False


def test_tangent_ok_true_is_recorded_as_checked():
    mesh = _sharp_grid()
    uvmap = _identity_uv(mesh, split_right=True)
    sharp_ids = {mesh.edge_key(*k) for k in SHARP_KEYS}
    res = evaluate_shading_policy(
        "require_uv_seam_on_sharp_edges",
        mesh=mesh, uvmap=uvmap, seams=sharp_ids, tangent_ok=True,
    )
    assert res["tangent_checked"] is True
    assert res["passed"] is True


# -- (f) unknown policy ------------------------------------------------------
def test_unknown_policy_raises():
    mesh = _sharp_grid()
    with pytest.raises(ValueError):
        evaluate_shading_policy(
            "smooth_everything", mesh=mesh, uvmap=_identity_uv(mesh), seams=set(),
        )
    with pytest.raises(ValueError):
        required_seam_edges_for_policy("smooth_everything", mesh)


# -- (g) required_seam_edges_for_policy --------------------------------------
def test_required_seam_edges_only_for_require_policy():
    mesh = _sharp_grid()
    expected = {mesh.edge_key(*k) for k in SHARP_KEYS}
    assert required_seam_edges_for_policy("require_uv_seam_on_sharp_edges", mesh) == expected
    assert required_seam_edges_for_policy("preserve", mesh) == set()
    assert required_seam_edges_for_policy("split_normals_on_uv_seams", mesh) == set()
    assert set(SHADING_POLICIES) == {
        "preserve", "split_normals_on_uv_seams", "require_uv_seam_on_sharp_edges"
    }
