"""G0 evidence: the baseline fixture set rebuilds reproducibly (work plan §7).

Blender-gated (G9): without a Blender executable every test here skips. With one,
the fixture builder runs into ``tmp_path`` — nothing is ever written into the
repository — and the emitted ``fixture_manifest.json`` is checked against the
files on disk:

- all 11 required fixtures exist as ``.blend`` + ``.obj`` and their SHA-256s match
  what the manifest recorded,
- the angle-boundary fixture records which fold edge carries which angle,
- the degenerate/non-manifold fixture really carries extra geometry beyond a cube,
- a second build reproduces every ``mesh_fingerprint`` exactly (the .blend byte
  hash is NOT compared: Blender stamps a timestamp into the file).
"""

from __future__ import annotations

import json
import os

from tests.e2e.blender_env import BLENDER, blender_version_info, requires_blender, run_blender_python

_HERE = os.path.dirname(os.path.abspath(__file__))
_BUILD_SCRIPT = os.path.join(_HERE, "fixtures", "build_fixture_models.py")

EXPECTED_FIXTURES = [
    "plane_grid",
    "cube",
    "bevel_cube",
    "cylinder_capped",
    "uv_sphere",
    "torus",
    "suzanne",
    "concave_plate",
    "angle_boundary",
    "degenerate_input",
    "protected_path",
]


def _sha256(path: str) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _build(out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    proc = run_blender_python(_BUILD_SCRIPT, ["--out", out_dir])
    assert proc.returncode == 0, (proc.stdout or "")[-2000:] + "\n" + (proc.stderr or "")[-4000:]
    manifest_path = os.path.join(out_dir, "fixture_manifest.json")
    assert os.path.exists(manifest_path), "fixture_manifest.json was written"
    with open(manifest_path, encoding="utf-8") as fh:
        return json.load(fh)


@requires_blender
def test_fixture_manifest_matches_files_on_disk(tmp_path):
    out_dir = str(tmp_path / "fixtures")
    manifest = _build(out_dir)

    # Environment provenance (G0: 실행 환경 기록).
    assert manifest["generated_by"] == "tests/e2e/fixtures/build_fixture_models.py"
    assert manifest["blender_version"], "blender_version recorded"
    assert isinstance(manifest["blender_build_hash"], str)
    assert manifest["platform"]

    by_name = {f["name"]: f for f in manifest["fixtures"]}
    assert sorted(by_name) == sorted(EXPECTED_FIXTURES), sorted(by_name)

    for name in EXPECTED_FIXTURES:
        entry = by_name[name]
        blend_path = os.path.join(out_dir, entry["blend"])
        obj_path = os.path.join(out_dir, entry["obj"])
        assert os.path.exists(blend_path), f"{name}: .blend exists"
        assert os.path.exists(obj_path), f"{name}: .obj exists"
        assert _sha256(blend_path) == entry["blend_sha256"], f"{name}: blend sha256"
        assert _sha256(obj_path) == entry["obj_sha256"], f"{name}: obj sha256"
        for key in ("vertex_count", "edge_count", "face_count", "loop_count"):
            assert entry[key] > 0, f"{name}: {key}"
        assert len(entry["mesh_fingerprint"]) == 64, f"{name}: fingerprint is a sha256"

    # angle_boundary records which fold edge carries which angle (G1 boundary evidence).
    folds = by_name["angle_boundary"]["notes"]["folds"]
    assert [f["angle_deg"] for f in folds] == [89.9, 90.0, 90.1]
    for fold in folds:
        verts = fold["fold_edge_verts"]
        assert len(verts) == 2 and all(len(v) == 3 for v in verts)

    # The degenerate fixture carries the cube plus the extra bad geometry.
    assert by_name["degenerate_input"]["face_count"] > by_name["cube"]["face_count"]


@requires_blender
def test_fixture_build_is_deterministic(tmp_path):
    first = _build(str(tmp_path / "build_a"))
    second = _build(str(tmp_path / "build_b"))

    fp_a = {f["name"]: f["mesh_fingerprint"] for f in first["fixtures"]}
    fp_b = {f["name"]: f["mesh_fingerprint"] for f in second["fixtures"]}
    assert fp_a == fp_b, "mesh fingerprints are reproducible across builds"

    counts_a = {f["name"]: (f["vertex_count"], f["edge_count"], f["face_count"], f["loop_count"])
                for f in first["fixtures"]}
    counts_b = {f["name"]: (f["vertex_count"], f["edge_count"], f["face_count"], f["loop_count"])
                for f in second["fixtures"]}
    assert counts_a == counts_b


@requires_blender
def test_blender_version_info_is_recorded():
    info = blender_version_info(BLENDER)
    assert info["executable"] == BLENDER
    assert info["version"], info["raw"][:500]
