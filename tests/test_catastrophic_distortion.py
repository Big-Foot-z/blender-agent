"""Gates CG2 / CG3 / CG11 / CG14 — catastrophic UV distortion detection.

Each case is a hand-built fixture with a hand-computed expectation, so a regression in
:mod:`uv_agent.geometry.catastrophic_distortion` shows up as a number rather than as a
vibe. The fixture helpers are re-implemented here on purpose: the v2 test module owns
its own helpers, and importing them would couple two independent gate suites.
No Blender.
"""

from __future__ import annotations

import json
import math

import pytest

from uv_agent.geometry.catastrophic_distortion import (
    CATASTROPHIC_METRIC_VERSION,
    CatastrophicThresholds,
    catastrophic_counters,
    compact_catastrophic,
    evaluate_catastrophic,
    thresholds_from_profile,
)
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap
from uv_agent.io.fixtures import build_grid_plane


# --- local fixtures ----------------------------------------------------------------


def identity_uv(mesh: MeshGraph) -> UVMap:
    """UV = XY of the (planar) mesh: conformal and equiareal by construction."""
    uvmap = UVMap.for_mesh(mesh)
    for loop in mesh.loops:
        x, y, _ = mesh.vertices[loop.vertex_id].co
        uvmap.set(loop.index, x, y)
    return uvmap


def affine_uv(mesh: MeshGraph, a: float, b: float, c: float, d: float) -> UVMap:
    """UV = [[a, b], [c, d]] @ XY."""
    uvmap = UVMap.for_mesh(mesh)
    for loop in mesh.loops:
        x, y, _ = mesh.vertices[loop.vertex_id].co
        uvmap.set(loop.index, a * x + b * y, c * x + d * y)
    return uvmap


def lone_triangle_loop(mesh: MeshGraph, face_id: int) -> tuple[int, tuple[int, int, int]]:
    """A loop of ``face_id`` that belongs to exactly ONE of the face's triangles.

    Editing that loop's UV therefore touches exactly one triangle of exactly one face —
    which is what "a single bad triangle in an otherwise clean plane" needs."""
    tris = mesh.face_triangles(face_id)
    counts: dict[int, int] = {}
    for tri in tris:
        for li in tri:
            counts[li] = counts.get(li, 0) + 1
    for tri in tris:
        for li in tri:
            if counts[li] == 1:
                return li, tri
    raise AssertionError("no loop unique to a single triangle")


# --- (a) clean plane ---------------------------------------------------------------


def test_conformal_plane_passes():
    mesh = build_grid_plane(4, 4)
    report = evaluate_catastrophic(mesh, identity_uv(mesh))

    assert report["metric_version"] == CATASTROPHIC_METRIC_VERSION == 1
    assert report["valid"] is True
    assert report["hard_failed"] is False
    assert report["region_failed"] is False
    assert report["passed"] is True
    assert report["bad_triangle_count"] == 0
    assert report["bad_region_count"] == 0
    assert report["bad_area_fraction"] == 0.0
    assert report["regions"] == []
    assert report["bad_face_ids"] == []
    assert report["max_anisotropy"] == pytest.approx(1.0, abs=1e-9)
    assert report["invalid_count"] == 0
    assert report["input_defect_count"] == 0
    assert len(report["per_face_score"]) == mesh.face_count
    assert all(s == pytest.approx(1.0, abs=1e-9) for s in report["per_face_score"])
    assert report["per_face_hard_fail"] == [False] * mesh.face_count


# --- (b) global anisotropy -----------------------------------------------------------


def anisotropic_case() -> tuple[MeshGraph, UVMap]:
    """4x wide / 0.25x tall -> anisotropy exactly 16 on every triangle."""
    mesh = build_grid_plane(3, 3)
    return mesh, affine_uv(mesh, 4.0, 0.0, 0.0, 0.25)


def test_global_anisotropy_hard_fails_everywhere():
    mesh, uvmap = anisotropic_case()
    report = evaluate_catastrophic(mesh, uvmap)

    tri_total = report["triangle_count"]
    assert tri_total == 2 * mesh.face_count
    assert report["bad_triangle_count"] == tri_total
    assert report["anisotropy_hard_count"] == tri_total
    assert report["max_anisotropy"] == pytest.approx(16.0, abs=1e-9)
    assert report["hard_failed"] is True
    assert report["passed"] is False
    assert report["bad_area_fraction"] == pytest.approx(1.0, abs=1e-12)
    assert len(report["regions"]) == 1
    region = report["regions"][0]
    assert region["face_ids"] == list(range(mesh.face_count))
    assert "anisotropy_hard" in region["reasons"]
    assert report["bad_face_ids"] == list(range(mesh.face_count))
    assert all(v is True for v in report["per_face_hard_fail"])


# --- (c) one needle triangle ---------------------------------------------------------


def needle_case() -> tuple[MeshGraph, UVMap, int]:
    mesh = build_grid_plane(4, 4)
    uvmap = identity_uv(mesh)
    face_id = 0
    li, tri = lone_triangle_loop(mesh, face_id)
    others = [x for x in tri if x != li]
    a = uvmap.get(others[0])
    c = uvmap.get(others[1])
    dx, dy = c[0] - a[0], c[1] - a[1]
    length = math.hypot(dx, dy)
    nx, ny = -dy / length, dx / length
    # 6x out along the opposite edge, 0.05 off it: a long thin sliver, but with a UV
    # area still ~0.2x of the 3D area so it is a NEEDLE, not an area collapse.
    uvmap.set(li, a[0] + 6.0 * dx + 0.05 * nx, a[1] + 6.0 * dy + 0.05 * ny)
    return mesh, uvmap, face_id


def test_single_needle_triangle_is_isolated():
    mesh, uvmap, face_id = needle_case()
    report = evaluate_catastrophic(mesh, uvmap)

    assert report["bad_triangle_count"] == 1
    assert report["needle_count"] == 1
    assert report["near_collapse_count"] == 0
    assert report["invalid_count"] == 0
    assert report["hard_failed"] is True
    assert report["bad_face_ids"] == [face_id]
    assert len(report["regions"]) == 1
    region = report["regions"][0]
    assert region["face_ids"] == [face_id]
    assert "needle" in region["reasons"]
    assert region["max_uv_aspect_ratio"] > 40.0

    worst = report["worst_triangles"]
    assert len(worst) == 1
    assert worst[0]["face_id"] == face_id
    assert worst[0]["uv_aspect_ratio"] > 40.0

    score = report["per_face_score"]
    assert score[face_id] > 8.0
    for fid in range(mesh.face_count):
        if fid != face_id:
            assert score[fid] == pytest.approx(1.0, abs=1e-9)
    assert report["per_face_hard_fail"][face_id] is True
    assert sum(1 for v in report["per_face_hard_fail"] if v) == 1


def test_conformally_mapped_3d_sliver_is_not_a_needle():
    """CG2: the needle reason must describe UV damage, not an already-thin 3D triangle.

    A decimated sliver whose UV map is the identity keeps its shape exactly; its uv
    aspect ratio is huge (> 40) purely because the 3D triangle is huge-aspect too, and
    flagging it sends the repair loop after damage that does not exist."""
    mesh = MeshGraph.from_faces(
        "sliver",
        [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.5, 0.002, 0.0)],
        [(0, 1, 2)],
    )
    uvmap = identity_uv(mesh)

    report = evaluate_catastrophic(mesh, uvmap)

    # The uv aspect really is past the absolute cap - and the 3D aspect matches it.
    assert report["max_uv_triangle_aspect"] > 40.0
    worst_aspect = report["max_uv_triangle_aspect"]
    assert report["needle_count"] == 0
    assert report["bad_triangle_count"] == 0
    assert report["regions"] == []
    assert report["passed"] is True

    # ... and dropping the 3D comparison factor to 0 brings the needle straight back,
    # so the fixture is genuinely exercising the new term.
    lenient = evaluate_catastrophic(
        mesh, uvmap, thresholds=CatastrophicThresholds(needle_3d_aspect_factor=0.0)
    )
    assert lenient["needle_count"] == 1
    assert lenient["worst_triangles"][0]["aspect_3d"] == pytest.approx(
        worst_aspect, rel=1e-9
    )


# --- (d) UV-collapsed triangle -------------------------------------------------------


def test_uv_collapsed_triangle_is_near_collapse():
    mesh = build_grid_plane(4, 4)
    uvmap = identity_uv(mesh)
    li, tri = lone_triangle_loop(mesh, 0)
    other = next(x for x in tri if x != li)
    u, v = uvmap.get(other)
    uvmap.set(li, u, v)  # two loops of the triangle share one UV -> collapsed

    report = evaluate_catastrophic(mesh, uvmap)
    assert report["near_collapse_count"] == 1
    assert report["bad_triangle_count"] == 1
    assert report["hard_failed"] is True
    assert report["passed"] is False
    assert "near_collapse" in report["regions"][0]["reasons"]
    # A collapsed UV triangle is unmeasurable, not "anisotropy 0".
    assert report["per_face_score"][0] is None


# --- (e) area explosion --------------------------------------------------------------


def test_area_explosion_triangle():
    # A dense grid so that inflating ONE triangle barely moves the global normalisation
    # (which is what makes the local ratio, not the global scale, the thing under test).
    mesh = build_grid_plane(8, 8)
    uvmap = identity_uv(mesh)
    li, tri = lone_triangle_loop(mesh, 0)
    anchor = next(x for x in tri if x != li)
    ax, ay = uvmap.get(anchor)
    ux, uy = uvmap.get(li)
    uvmap.set(li, ax + 400.0 * (ux - ax), ay + 400.0 * (uy - ay))

    report = evaluate_catastrophic(mesh, uvmap)
    assert report["area_explosion_count"] == 1
    assert report["bad_triangle_count"] == 1
    assert "area_explosion" in report["regions"][0]["reasons"]
    assert report["worst_triangles"][0]["normalized_area_ratio"] > 25.0


# --- (f) CG11: rotation + uniform scale invariance ------------------------------------


def test_rotation_and_uniform_scale_invariance():
    mesh, uvmap = anisotropic_case()
    base = evaluate_catastrophic(mesh, uvmap)

    theta = math.radians(37.0)
    s = 0.37
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    rotated = UVMap.for_mesh(mesh)
    for loop in mesh.loops:
        u, v = uvmap.get(loop.index)
        rotated.set(
            loop.index, s * (cos_t * u - sin_t * v), s * (sin_t * u + cos_t * v)
        )
    moved = evaluate_catastrophic(mesh, rotated)

    assert moved["bad_triangle_count"] == base["bad_triangle_count"]
    assert moved["bad_area_fraction"] == pytest.approx(
        base["bad_area_fraction"], abs=1e-9
    )
    assert moved["max_anisotropy"] == pytest.approx(base["max_anisotropy"], abs=1e-9)
    assert moved["max_uv_triangle_aspect"] == pytest.approx(
        base["max_uv_triangle_aspect"], abs=1e-9
    )


# --- (g) NaN UV ----------------------------------------------------------------------


def test_nan_uv_is_invalid_never_zero():
    mesh = build_grid_plane(4, 4)
    uvmap = identity_uv(mesh)
    li, _tri = lone_triangle_loop(mesh, 0)
    uvmap.set(li, float("nan"), 0.0)

    report = evaluate_catastrophic(mesh, uvmap)
    assert report["invalid_count"] == 1
    assert report["valid"] is False
    assert report["hard_failed"] is True
    assert report["passed"] is False
    assert report["per_face_score"][0] is None
    assert report["bad_face_ids"] == [0]
    assert "invalid" in report["regions"][0]["reasons"]


# --- (h) overlap / flip flags ---------------------------------------------------------


def test_overlap_and_flip_face_ids_set_region_flags():
    mesh, uvmap = anisotropic_case()
    report = evaluate_catastrophic(
        mesh, uvmap, overlap_face_ids=[0], flip_face_ids=[1]
    )
    region = report["regions"][0]
    assert region["self_overlap"] is True
    assert region["local_flip"] is True
    assert report["self_overlap_region_count"] == 1
    assert report["flip_region_count"] == 1
    assert report["region_failed"] is True

    clean = evaluate_catastrophic(mesh, uvmap)
    assert clean["regions"][0]["self_overlap"] is False
    assert clean["regions"][0]["local_flip"] is False
    assert clean["self_overlap_region_count"] == 0
    assert clean["flip_region_count"] == 0


# --- (i) below the minimum cluster size ------------------------------------------------


def test_region_below_min_cluster_is_listed_but_not_counted():
    mesh, uvmap, face_id = needle_case()
    # The needle region covers 1/16 of the mesh; a 0.5 floor puts it below the minimum.
    thresholds = CatastrophicThresholds(min_cluster_area_fraction=0.5)
    report = evaluate_catastrophic(mesh, uvmap, thresholds=thresholds)

    assert report["bad_triangle_count"] == 1
    assert report["hard_failed"] is True
    assert len(report["regions"]) == 1
    region = report["regions"][0]
    assert region["face_ids"] == [face_id]
    assert region["below_cluster_min"] is True
    assert region["area_fraction"] < 0.5
    assert report["bad_region_count"] == 0
    assert report["boundary_spike_region_count"] == 0


# --- (j) CG14: determinism --------------------------------------------------------------


def test_two_evaluations_are_byte_identical():
    mesh, uvmap, _face_id = needle_case()
    a = evaluate_catastrophic(mesh, uvmap)
    b = evaluate_catastrophic(mesh, uvmap)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)

    mesh2, uv2 = anisotropic_case()
    assert json.dumps(
        evaluate_catastrophic(mesh2, uv2), sort_keys=True
    ) == json.dumps(evaluate_catastrophic(mesh2, uv2), sort_keys=True)


# --- (k) compact view / profile thresholds ------------------------------------------------


def test_compact_and_thresholds_from_profile():
    mesh, uvmap = anisotropic_case()
    report = evaluate_catastrophic(mesh, uvmap)
    compact = compact_catastrophic(report)

    for dropped in ("regions", "worst_triangles", "per_face_score", "per_face_hard_fail"):
        assert dropped not in compact
    assert compact["bad_triangle_count"] == report["bad_triangle_count"]
    assert compact["max_anisotropy"] == report["max_anisotropy"]
    assert compact["region_face_ids"] == [list(range(mesh.face_count))]
    json.dumps(compact)  # JSON-safe

    counters = catastrophic_counters(report)
    assert counters == (
        report["bad_triangle_count"],
        report["near_collapse_count"],
        report["invalid_count"],
        report["bad_region_count"],
        report["bad_area_fraction"],
        report["max_anisotropy"],
    )

    defaults = CatastrophicThresholds()
    assert thresholds_from_profile({}) == defaults
    from_dict = thresholds_from_profile(
        {"anisotropy_hard_max": 6.0, "bad_area_fraction_cap": 0.01}
    )
    assert from_dict.anisotropy_hard_max == 6.0
    assert from_dict.bad_area_fraction_cap == 0.01
    assert from_dict.max_uv_triangle_aspect == defaults.max_uv_triangle_aspect

    class _Profile:
        anisotropy_hard_max = 12.0
        local_area_ratio_max = 9.0

    from_obj = thresholds_from_profile(_Profile())
    assert from_obj.anisotropy_hard_max == 12.0
    assert from_obj.local_area_ratio_max == 9.0
    assert from_obj.near_collapse_ratio == defaults.near_collapse_ratio
