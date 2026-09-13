"""G0 mesh identity + G1 edge correspondence (uv_agent.geometry.mesh_identity)."""

import importlib.util
import math
from pathlib import Path

from uv_agent.geometry import mesh_identity as mi
from uv_agent.geometry.mesh_graph import MeshGraph

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SCRIPT = REPO_ROOT / "tests" / "e2e" / "fixtures" / "build_fixture_models.py"


def _load_fixture_module():
    """Load the Blender fixture builder as a plain module (its core is bpy-free)."""
    spec = importlib.util.spec_from_file_location("_fixture_builder", FIXTURE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CUBE_VERTS = [
    (-0.5, -0.5, -0.5),
    (0.5, -0.5, -0.5),
    (0.5, 0.5, -0.5),
    (-0.5, 0.5, -0.5),
    (-0.5, -0.5, 0.5),
    (0.5, -0.5, 0.5),
    (0.5, 0.5, 0.5),
    (-0.5, 0.5, 0.5),
]
CUBE_FACES = [
    [0, 3, 2, 1],
    [4, 5, 6, 7],
    [0, 1, 5, 4],
    [2, 3, 7, 6],
    [1, 2, 6, 5],
    [3, 0, 4, 7],
]
CUBE_MATS = [0] * 6


def _rotate(loop, k):
    return list(loop[k:]) + list(loop[:k])


# ---------------------------------------------------------------------------
# (1) byte-identical serialization against the fixture script
# ---------------------------------------------------------------------------
def test_fingerprint_matches_fixture_script_implementation():
    ref = _load_fixture_module()

    shuffled = list(reversed(CUBE_FACES))
    rotated = [_rotate(f, i % len(f)) for i, f in enumerate(CUBE_FACES)]

    cases = [CUBE_FACES, shuffled, rotated]
    digests = []
    for faces in cases:
        mine = mi.fingerprint_from_arrays(CUBE_VERTS, faces, CUBE_MATS)
        theirs = ref.fingerprint_from_arrays(CUBE_VERTS, faces, CUBE_MATS)
        assert mine == theirs
        digests.append(mine)

    # Face order permutation and loop rotation are invariants of the digest.
    assert len(set(digests)) == 1


# ---------------------------------------------------------------------------
# (2) sensitivity: 1e-3 changes the digest, 1e-9 does not; materials matter
# ---------------------------------------------------------------------------
def test_digest_sensitivity_to_coordinate_and_material_changes():
    base = mi.fingerprint_from_arrays(CUBE_VERTS, CUBE_FACES, CUBE_MATS)

    coarse = list(CUBE_VERTS)
    coarse[0] = (CUBE_VERTS[0][0] + 1e-3, CUBE_VERTS[0][1], CUBE_VERTS[0][2])
    assert mi.fingerprint_from_arrays(coarse, CUBE_FACES, CUBE_MATS) != base

    tiny = list(CUBE_VERTS)
    tiny[0] = (CUBE_VERTS[0][0] + 1e-9, CUBE_VERTS[0][1], CUBE_VERTS[0][2])
    assert mi.fingerprint_from_arrays(tiny, CUBE_FACES, CUBE_MATS) == base

    mats = list(CUBE_MATS)
    mats[2] = 1
    assert mi.fingerprint_from_arrays(CUBE_VERTS, CUBE_FACES, mats) != base


# ---------------------------------------------------------------------------
# (3) mesh_identity / compare_identity
# ---------------------------------------------------------------------------
def test_mesh_identity_unchanged_and_changed():
    a = MeshGraph.from_faces("cube", CUBE_VERTS, CUBE_FACES)
    b = MeshGraph.from_faces("cube", CUBE_VERTS, CUBE_FACES)

    id_a = mi.mesh_identity(a)
    id_b = mi.mesh_identity(b)
    assert id_a["fingerprint"] == mi.mesh_fingerprint(a)
    assert (id_a["vertex_count"], id_a["edge_count"], id_a["face_count"]) == (8, 12, 6)
    assert id_a["loop_count"] == 24
    assert id_a["model_sha256"] is None

    same = mi.compare_identity(id_a, id_b)
    assert same["unchanged"] is True
    assert same["differences"] == []
    assert same["before_sha256"] == same["after_sha256"] == id_a["fingerprint"]
    assert (same["vertex_count"], same["face_count"], same["loop_count"]) == (8, 6, 24)

    moved_verts = list(CUBE_VERTS)
    moved_verts[0] = (-0.4, -0.5, -0.5)
    moved = MeshGraph.from_faces("cube", moved_verts, CUBE_FACES)
    diff = mi.compare_identity(id_a, mi.mesh_identity(moved))
    assert diff["unchanged"] is False
    assert "fingerprint" in diff["differences"]


# ---------------------------------------------------------------------------
# (4) edge correspondence survives renumbering
# ---------------------------------------------------------------------------
def test_edge_correspondence_and_remap_across_face_reordering():
    a = MeshGraph.from_faces("cube", CUBE_VERTS, CUBE_FACES)
    b = MeshGraph.from_faces("cube", CUBE_VERTS, list(reversed(CUBE_FACES)))

    assert mi.mesh_fingerprint(a) == mi.mesh_fingerprint(b)

    corr = mi.edge_correspondence(a, b)
    assert corr["unmatched_a"] == [] and corr["unmatched_b"] == []
    assert len(corr["a_to_b"]) == a.edge_count == b.edge_count
    # Reversing the face list renumbers edges, so the IDs are NOT identical.
    assert corr["identical_ids"] is False
    assert any(k != v for k, v in corr["a_to_b"].items())

    mandatory_a = [e.id for e in a.edges if abs(e.dihedral_angle - 90.0) <= 1e-5]
    assert len(mandatory_a) == 12  # every cube edge is a 90 degree fold

    mapped, unmatched = mi.remap_edge_ids(mandatory_a, corr)
    assert unmatched == []
    mandatory_b = {e.id for e in b.edges if abs(e.dihedral_angle - 90.0) <= 1e-5}
    assert set(mapped) == mandatory_b

    # The remapped edges describe the same geometry.
    for src, dst in zip(mandatory_a, mapped):
        assert mi.edge_geometry_key(a, src) == mi.edge_geometry_key(b, dst)
        assert math.isclose(a.edges[src].dihedral_angle, b.edges[dst].dihedral_angle,
                            abs_tol=1e-9)


# ---------------------------------------------------------------------------
# (5) uv_hash — exact UV identity for the rollback baseline (CG7 / CG0)
# ---------------------------------------------------------------------------
def test_uv_hash_stable_perturbation_sensitive_and_nan_aware():
    import numpy as np

    from uv_agent.geometry.solution import UVMap

    a = UVMap(4)
    a.uv[:] = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.25, 0.75]]
    b = UVMap(4)
    b.uv[:] = a.uv.copy()

    # equal arrays -> equal digests, and the UVMap adapter matches the raw array
    assert mi.uv_hash(a) == mi.uv_hash(b)
    assert mi.uv_hash(a) == mi.uv_hash_from_array(a.uv)
    assert mi.uv_hash(a) == mi.uv_hash_from_array(np.asarray(a.uv, dtype="<f8"))

    # a 1e-12 nudge is NOT rounded away
    c = a.copy()
    c.uv[3, 0] += 1e-12
    assert mi.uv_hash(c) != mi.uv_hash(a)

    # NaN-aware: a NaN UV never hashes equal to a finite one, and is reproducible
    n1 = a.copy()
    n1.uv[2, 1] = float("nan")
    n2 = a.copy()
    n2.uv[2, 1] = float("nan")
    assert mi.uv_hash(n1) != mi.uv_hash(a)
    assert mi.uv_hash(n1) == mi.uv_hash(n2)

    # the loop-count prefix keeps different lengths apart
    short = UVMap(3)
    short.uv[:] = a.uv[:3]
    assert mi.uv_hash(short) != mi.uv_hash(a)
    assert len(mi.uv_hash(a)) == 64
