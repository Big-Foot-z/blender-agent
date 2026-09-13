"""Pure-helper tests for export validation (MVP 5 plan §7, Session C).

``uv_agent/blender/export_validation.py`` imports ``bpy`` lazily, so its warning
policy helpers import + test without Blender. The actual re-open validation is
exercised by the Blender-gated e2e smoke (``tests/e2e/test_mvp5_export.py``).
"""

from uv_agent.blender import export_validation as ev


# --- UV layer naming tolerance (plan §7) -----------------------------------
def test_uv_layer_warning_when_expected_name_absent():
    w = ev.uv_layer_warnings(["UVMap"], "AI_UV", fmt="obj")
    assert len(w) == 1 and "AI_UV" in w[0]


def test_uv_layer_no_warning_when_present_or_unspecified():
    assert ev.uv_layer_warnings(["AI_UV"], "AI_UV", fmt="fbx") == []
    assert ev.uv_layer_warnings(["UVMap"], None, fmt="obj") == []
    assert ev.uv_layer_warnings([], "AI_UV", fmt="glb") == []  # no UV -> caller hard-fails, not here


# --- face/vertex drift tolerance (plan §7) ---------------------------------
def test_face_drift_warns_only_when_not_triangulated():
    # big face delta, not triangulated -> warn
    w = ev.count_warnings(fmt="glb", faces=20000, vertices=6562,
                          source_faces=12152, source_vertices=6562, triangulated=False)
    assert any("face count" in m for m in w)
    # same delta, triangulated -> no face warning (expected change)
    w2 = ev.count_warnings(fmt="glb", faces=24304, vertices=6562,
                           source_faces=12152, source_vertices=6562, triangulated=True)
    assert not any("face count" in m for m in w2)


def test_vertex_drift_only_warns_when_large():
    # small vertex split (format round-trip) -> no warning
    assert ev.count_warnings(fmt="fbx", faces=12152, vertices=6800,
                             source_faces=12152, source_vertices=6562, triangulated=False) == []
    # huge vertex blow-up -> warn
    w = ev.count_warnings(fmt="fbx", faces=12152, vertices=20000,
                          source_faces=12152, source_vertices=6562, triangulated=False)
    assert any("vertex count" in m for m in w)


def test_count_warnings_tolerates_missing_source():
    assert ev.count_warnings(fmt="obj", faces=10, vertices=10,
                             source_faces=None, source_vertices=None, triangulated=False) == []


# --- normals tolerance (plan §7) -------------------------------------------
def test_normals_warning_only_when_requested_and_absent():
    assert ev.normals_warning(False, True, fmt="obj") != []
    assert ev.normals_warning(True, True, fmt="obj") == []
    assert ev.normals_warning(False, False, fmt="obj") == []


# ---------------------------------------------------------------------------
# Gate G13 — source-matched re-read audit (pure half)
# ---------------------------------------------------------------------------
from chart_uv_agent.fixtures import build_folded_planes  # noqa: E402
from chart_uv_agent.quality_profile import load_quality_profile  # noqa: E402
from uv_agent.geometry.mesh_graph import MeshGraph  # noqa: E402
from uv_agent.geometry.mesh_identity import edge_geometry_key  # noqa: E402
from uv_agent.geometry.solution import UVMap  # noqa: E402
from uv_agent.io.fixtures import build_grid_plane  # noqa: E402


def _triangulated(mesh: MeshGraph) -> MeshGraph:
    """Fan-triangulate every face — what GLB/GLTF (and ``triangulate=True``) ship."""
    coords = [v.co for v in mesh.vertices]
    faces: list[list[int]] = []
    for f in mesh.faces:
        vids = [int(v) for v in f.vertex_ids]
        for i in range(1, len(vids) - 1):
            faces.append([vids[0], vids[i], vids[i + 1]])
    return MeshGraph.from_faces(mesh.object_id + "_tri", coords, faces)


def _grid_uvmap(mesh: MeshGraph, *, nudge_loop: int | None = None) -> UVMap:
    """Planar XY projection of a unit-size grid plane — one UV per vertex position,
    so the layout is identical before and after triangulation."""
    uvmap = UVMap(len(mesh.loops))
    for loop in mesh.loops:
        x, y, _z = mesh.vertices[loop.vertex_id].co
        uvmap.set(loop.index, 0.5 + x, 0.5 + y)
    if nudge_loop is not None:
        u, v = uvmap.get(nudge_loop)
        uvmap.set(nudge_loop, u + 0.01, v)
    return uvmap


def _folded_uvmap(mesh: MeshGraph, *, welded: bool = False,
                  nudge_loop: int | None = None) -> UVMap:
    """UVs for ``build_folded_planes``: grid B is every face whose vertices all sit
    at ``y == 1``. ``welded=False`` packs the two halves as separate islands (the
    fold is UV-split); ``welded=True`` maps them into ONE continuous island, which
    is exactly the mandatory-90 violation the gate must catch."""
    uvmap = UVMap(len(mesh.loops))
    for f in mesh.faces:
        in_b = all(abs(mesh.vertices[v].co[1] - 1.0) < 1e-9 for v in f.vertex_ids)
        for li in f.loop_indices:
            x, y, z = mesh.vertices[mesh.loops[li].vertex_id].co
            if welded:
                u = 0.01 + 0.9 * x
                v = 0.01 + 0.49 * ((1.0 + z) if in_b else y)
            elif in_b:
                u, v = 0.52 + 0.47 * x, 0.01 + 0.47 * z
            else:
                u, v = 0.01 + 0.47 * x, 0.01 + 0.47 * y
            uvmap.set(li, u, v)
    if nudge_loop is not None:
        u, v = uvmap.get(nudge_loop)
        uvmap.set(nudge_loop, u + 0.005, v)
    return uvmap


def _profile() -> dict:
    return dict(load_quality_profile(None).to_dict())


def _audit(source, source_uvmap, reread, reread_uvmap, *, fmt="fbx", triangulated=False):
    return ev.build_reread_audit(
        fmt=fmt, source_mesh=source, source_uvmap=source_uvmap,
        reread_mesh=reread, reread_uvmap=reread_uvmap,
        uv_layers=["AI_UV"], active_uv_layer="AI_UV", profile=_profile(),
        tangent_ok=True, triangulated=triangulated, expected_uv_layer="AI_UV")


# --- fingerprints (invariant under triangulation, sensitive to UVs) --------
def test_uv_corner_fingerprint_invariant_under_triangulation():
    quads = build_grid_plane(4, 4)
    tris = _triangulated(quads)
    assert len(tris.faces) == 2 * len(quads.faces)

    fq = ev.uv_corner_fingerprint(quads, _grid_uvmap(quads))
    ft = ev.uv_corner_fingerprint(tris, _grid_uvmap(tris))
    assert fq["sha256"] == ft["sha256"]
    # One row per distinct (position, UV) pair -> one per vertex here.
    assert fq["corner_count"] == ft["corner_count"] == len(quads.vertices)
    assert ev.vertex_position_fingerprint(quads) == ev.vertex_position_fingerprint(tris)


def _perturbed_uvmap(mesh: MeshGraph, base: UVMap, *, delta: float) -> UVMap:
    """Every UV shifted by ``delta`` — the shape a float32 round trip leaves behind."""
    out = UVMap(len(mesh.loops))
    for loop in mesh.loops:
        u, v = base.get(loop.index)
        out.set(loop.index, u + delta, v + delta)
    return out


def test_uv_corners_match_identical_layout_uses_hash():
    mesh = build_grid_plane(4, 4)
    uvmap = _grid_uvmap(mesh)
    res = ev.uv_corners_match(mesh, uvmap, mesh, uvmap)
    assert res["match"] is True
    assert res["method"] == "hash"
    assert res["max_abs_error"] is None
    assert res["corner_count_source"] == res["corner_count_reread"] == len(mesh.vertices)


def test_uv_corners_match_tolerates_float32_scale_drift():
    mesh = build_grid_plane(4, 4)
    # Sit the layout on a 5-digit rounding boundary so a 1e-6 drift — the scale of
    # float32 UV storage — actually changes the digest and exercises the fall-back.
    base = _perturbed_uvmap(mesh, _grid_uvmap(mesh), delta=4.5e-6)
    res = ev.uv_corners_match(mesh, base, mesh, _perturbed_uvmap(mesh, base, delta=1e-6))
    assert res["match"] is True
    assert res["method"] == "tolerance"
    assert abs(res["max_abs_error"] - 1e-6) < 1e-9


def test_uv_corners_match_rejects_real_uv_movement():
    mesh = build_grid_plane(4, 4)
    base = _grid_uvmap(mesh)
    res = ev.uv_corners_match(mesh, base, mesh, _perturbed_uvmap(mesh, base, delta=1e-3))
    assert res["match"] is False
    assert res["method"] == "tolerance"
    assert res["max_abs_error"] > 5e-4


def test_uv_corners_match_rejects_corner_count_mismatch():
    mesh = build_grid_plane(4, 4)
    smaller = build_grid_plane(3, 3)
    res = ev.uv_corners_match(mesh, _grid_uvmap(mesh), smaller, _grid_uvmap(smaller))
    assert res["match"] is False
    assert res["method"] == "tolerance"
    assert res["max_abs_error"] is None
    assert res["corner_count_source"] != res["corner_count_reread"]


def test_uv_corner_fingerprint_changes_when_one_uv_moves():
    quads = build_grid_plane(4, 4)
    base = ev.uv_corner_fingerprint(quads, _grid_uvmap(quads))
    moved = ev.uv_corner_fingerprint(quads, _grid_uvmap(quads, nudge_loop=0))
    assert base["sha256"] != moved["sha256"]
    # The geometry did not move, so the vertex fingerprint must not react.
    assert ev.vertex_position_fingerprint(quads) == ev.vertex_position_fingerprint(quads)


# --- source-matched mandatory-90 (triangulation diagonals excluded) --------
def test_source_matched_fold_audit_ignores_triangulation_diagonals():
    source = build_folded_planes(n=4)
    reread = _triangulated(source)
    src_keys = {edge_geometry_key(source, int(e.id), 6) for e in source.edges}
    diagonals = [int(e.id) for e in reread.edges
                 if edge_geometry_key(reread, int(e.id), 6) not in src_keys]
    assert diagonals, "fan triangulation must invent edges the source never had"

    split = ev.source_matched_fold_audit(source, reread, _folded_uvmap(reread))
    # The 4 shared-row edges of the 90-degree fold, and nothing the triangulation added.
    assert split["fold_edges_checked"] == 4
    assert split["unmatched_fold_edges"] == 0
    assert split["mandatory_90_uv_unsplit"] == 0
    assert split["uv_unsplit_edge_ids"] == []


def test_source_matched_fold_audit_flags_welded_fold():
    source = build_folded_planes(n=4)
    reread = _triangulated(source)
    welded = ev.source_matched_fold_audit(source, reread,
                                          _folded_uvmap(reread, welded=True))
    assert welded["fold_edges_checked"] == 4
    assert welded["mandatory_90_uv_unsplit"] == welded["fold_edges_checked"]
    assert len(welded["uv_unsplit_edge_ids"]) == 4


# --- build_reread_audit verdicts ------------------------------------------
def test_build_reread_audit_passes_on_clean_split_layout():
    mesh = build_folded_planes(n=4)
    uvmap = _folded_uvmap(mesh)
    audit = _audit(mesh, uvmap, mesh, uvmap)
    assert audit["failures"] == []
    assert audit["passed"] is True
    assert audit["island_count"] == 2
    assert audit["uv_fingerprint"]["match"] is True
    assert audit["vertex_fingerprint"]["match"] is True
    assert audit["topology"]["match"] is True
    assert audit["mandatory"]["mandatory_90_uv_unsplit"] == 0


def test_build_reread_audit_fails_when_fold_is_welded():
    mesh = build_folded_planes(n=4)
    uvmap = _folded_uvmap(mesh, welded=True)
    # Source and re-read carry the SAME (welded) layout, so the fingerprints match
    # and the mandatory-90 rule is the only thing that can fail.
    audit = _audit(mesh, uvmap, mesh, uvmap)
    assert audit["passed"] is False
    assert audit["failures"] == ["mandatory_90_uv_unsplit"]
    assert audit["mandatory"]["mandatory_90_uv_unsplit"] == 4


def test_build_reread_audit_fails_on_uv_fingerprint_mismatch():
    mesh = build_folded_planes(n=4)
    audit = _audit(mesh, _folded_uvmap(mesh), mesh, _folded_uvmap(mesh, nudge_loop=0))
    assert audit["passed"] is False
    assert "uv_fingerprint_mismatch" in audit["failures"]
    assert audit["vertex_fingerprint"]["match"] is True


def test_build_reread_audit_fails_on_topology_mismatch_untriangulated():
    source = build_folded_planes(n=4)
    reread = build_folded_planes(n=3)
    audit = _audit(source, _folded_uvmap(source), reread, _folded_uvmap(reread))
    assert audit["passed"] is False
    assert "topology_mismatch" in audit["failures"]
    assert audit["topology"]["source_faces"] != audit["topology"]["reread_faces"]
    assert audit["topology"]["triangulation_expected"] is False


def test_build_reread_audit_accepts_triangulated_glb_topology():
    source = build_folded_planes(n=4)
    reread = _triangulated(source)
    audit = _audit(source, _folded_uvmap(source), reread, _folded_uvmap(reread),
                   fmt="glb")
    assert audit["topology"]["triangulation_expected"] is True
    assert audit["topology"]["match"] is True
    assert "topology_mismatch" not in audit["failures"]


def test_build_reread_audit_fails_when_uv_layer_missing():
    mesh = build_folded_planes(n=4)
    uvmap = _folded_uvmap(mesh)
    audit = ev.build_reread_audit(
        fmt="obj", source_mesh=mesh, source_uvmap=uvmap, reread_mesh=mesh,
        reread_uvmap=uvmap, uv_layers=[], active_uv_layer=None, profile=_profile(),
        tangent_ok=None, triangulated=False, expected_uv_layer="AI_UV")
    assert audit["passed"] is False
    assert "uv_missing" in audit["failures"]


def test_build_reread_audit_fails_on_broken_tangent_basis():
    mesh = build_folded_planes(n=4)
    uvmap = _folded_uvmap(mesh)
    audit = ev.build_reread_audit(
        fmt="fbx", source_mesh=mesh, source_uvmap=uvmap, reread_mesh=mesh,
        reread_uvmap=uvmap, uv_layers=["AI_UV"], active_uv_layer="AI_UV",
        profile=_profile(), tangent_ok=False, triangulated=False,
        expected_uv_layer="AI_UV")
    assert audit["passed"] is False
    assert "shading_policy_failed" in audit["failures"]
    assert "tangent_basis_failed" in audit["shading"]["failures"]


# --- GLB re-read vertex weld (G13) -----------------------------------------
def test_vertex_weld_helper_exported_and_audit_carries_the_key():
    # ``reread_audit`` welds the glTF re-read by position with this helper.
    assert callable(getattr(ev, "_weld_vertices_by_position"))
    mesh = build_folded_planes(n=4)
    uvmap = _folded_uvmap(mesh)
    audit = _audit(mesh, uvmap, mesh, uvmap, fmt="glb", triangulated=True)
    # The pure builder does not invent the key; ``reread_audit`` adds it.
    assert "vertex_weld" not in audit
    audit["vertex_weld"] = {"applied": True, "vertices_before": 54,
                            "vertices_after": 8, "dist": 1e-6}
    assert audit["passed"] is True
