"""Real-Blender evidence for glTF/FBX import topology normalization (TN4).

A glTF/GLB file cannot carry a shared vertex that has two different loop-domain
attributes: the exporter splits the vertex. Re-importing such a file therefore
yields a mesh that *looks* like a shell of disconnected patches — every split
seam reads as a boundary edge — and the UV engine then treats those fake
boundaries as mandatory seams. FBX does not split the same way, so the same
asset used to unwrap differently depending on the file it arrived in.

The normalization pass (``uv_agent/blender/topology_normalize.py``, wired into
``worker/generate_uv_from_seams.py``) audits the imported mesh, welds by
position when the split is fake, re-audits, and reports both sides in
``import_topology.json`` / ``uv_generate_summary.json['import_topology']``.

Gate IDs measured here:

- **G3/G5/G7** — ``test_glb_split_vertex_recovery``: for five purpose-built
  fixtures, the *post*-normalization boundary / non-manifold / component counts
  must equal the ``reference_topology`` the fixture builder measured on the
  ``.blend`` mesh. Closed fixtures must come back with zero boundary edges, zero
  ``seam_reason_counts.boundary_topology`` and zero
  ``mandatory_audit.mandatory_boundary_edges``; genuine boundary
  (``open_plane``) and genuine non-manifold (``non_manifold_fan``) geometry must
  survive untouched. Face count is never changed (G4).
- **G8/G14** — ``test_fbx_glb_parity``: the same fixture through ``.fbx`` and
  through ``.glb`` must agree on topology, mandatory fold edges, seam reasons,
  island count and ``run_manifest.normalized_mesh_fingerprint`` (G6).
- **G6** — ``test_identity_ordering``: ``source_asset_hash`` is the hash of the
  file on disk, the normalized fingerprint is a *different* digest taken after
  normalization, and ``mesh_identity.json`` records the normalized mesh.
- **G0/G9/G15** — ``test_lowpoly6_real_glb_regression``: the named real-model
  regression (``lowpoly_6``), recorded as a full Before/After table with the
  artifacts copied out. Only the cheap invariants are asserted; the verdict is
  read off the evidence by hand.
- **G13** — ``test_lowpoly6_three_runs_deterministic``: three identical runs on
  the real GLB agree exactly.
- **G14** — ``test_export_reread_parity``: exporting an accepted GLB-sourced run
  back out to FBX + GLB re-reads to the same post-normalization topology.

Every test writes ``$UV_GATE_EVIDENCE_DIR/uv_gltf_topology_<test>.json`` from a
``finally`` block, so a failing gate still leaves its table behind. Assertions
read JSON artifacts, never stdout.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time

import pytest

from tests.e2e.blender_env import (
    BLENDER,
    blender_version_info,
    requires_blender,
    run_blender_python,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_BUILD_SCRIPT = os.path.join(_HERE, "fixtures", "build_fixture_models.py")
_UV_WORKER = os.path.join(_ROOT, "worker", "generate_uv_from_seams.py")
_EXPORT_WORKER = os.path.join(_ROOT, "worker", "export_production_asset.py")

_EVIDENCE_PREFIX = "uv_gltf_topology_"

#: ``None`` lets the worker pick the first/largest mesh — which is what an
#: interchange import gives us (glTF/FBX importers rename objects freely).
_OBJECT_NAME = None

#: Shipped defaults everywhere; previews are review sugar and dominate runtime.
_OPTIONS = {"render_previews": False}

#: Fixtures exercised by the split-vertex recovery gate.
_SPLIT_FIXTURES = (
    "split_normal_glb",
    "split_uv_glb",
    "two_shells",
    "open_plane",
    "non_manifold_fan",
)

#: Fixtures whose reference topology is watertight (boundary 0).
_CLOSED_FIXTURES = ("split_normal_glb", "split_uv_glb", "two_shells")

#: Expected component count for the closed fixtures.
_EXPECTED_COMPONENTS = {"split_normal_glb": 1, "split_uv_glb": 1, "two_shells": 2}

#: Fixtures compared across the two interchange formats.
_PARITY_FIXTURES = ("bevel_cube", "cylinder_capped", "uv_sphere")

# ---------------------------------------------------------------------------
# The named real-model regression (lowpoly_6 / statue_lowpoly.glb).
# ---------------------------------------------------------------------------
REAL_GLB = os.path.join(_HERE, "fixtures", "real", "statue_lowpoly.glb")
REAL_GLB_SHA256 = "6253a4d31568fc546c26c3298a830736ff29813d93c8e884a85db40e6f100b07"
REAL_GLB_SIZE = 287048

#: Baseline run 300c51f3 at code 15bcf6d — the failure this milestone exists to
#: fix, copied into ``docs/evidence/gltf_topology_baseline_300c51f3/``.
LOWPOLY6_BEFORE = {
    "run": "300c51f3",
    "code_sha": "15bcf6d",
    "face_count": 12000,
    "edge_count": 20755,
    "vertex_count": 8920,
    "island_count": 385,
    "mandatory_90_edges": 5510,
    "mandatory_90_fold_edges": 0,
    "seam_count": 6189,
    "seam_reason_counts": {"boundary_topology": 6189},
    "tiny_island_count": 346,
    "sliver_island_count": 59,
    "one_two_face_island_count": 220,
    "raster_overlap_ratio": 0.00241,
    "anisotropy_p95": 1.158,
    "anisotropy_max": 7.79,
}

#: Artifacts copied into ``$UV_GATE_EVIDENCE_DIR/lowpoly6_after/`` (G15).
_LOWPOLY6_ARTIFACTS = (
    "import_topology.json",
    "uv_generate_summary.json",
    "quality_report.json",
    "selected_heatmap_anisotropy.png",
    "selected_uv_layout.png",
)
_LOWPOLY6_SUBDIR = "lowpoly6_after"

requires_real_glb = pytest.mark.skipif(
    not os.path.exists(REAL_GLB), reason="real-model GLB fixture not present"
)


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------
def _json_safe(value):
    try:
        from chart_uv_agent.reporting import json_safe
    except Exception:  # noqa: BLE001 - evidence must never depend on the engine importing
        return value
    return json_safe(value)


def _git_head() -> str | None:
    try:
        from chart_uv_agent.reporting import git_head_sha
    except Exception:  # noqa: BLE001
        return None
    return git_head_sha(_ROOT)


def _evidence_dir() -> str | None:
    return os.environ.get("UV_GATE_EVIDENCE_DIR") or None


def _write_evidence(name: str, payload: dict) -> None:
    """Write ``uv_gltf_topology_<name>.json`` into ``$UV_GATE_EVIDENCE_DIR``."""
    out_dir = _evidence_dir()
    if not out_dir:
        return
    try:
        os.makedirs(out_dir, exist_ok=True)
        doc = {
            "test": name,
            "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "blender": blender_version_info() if BLENDER else None,
            "git_head": _git_head(),
            **payload,
        }
        path = os.path.join(out_dir, f"{_EVIDENCE_PREFIX}{name}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_json_safe(doc), fh, indent=2, ensure_ascii=False, sort_keys=False)
            fh.write("\n")
    except Exception as exc:  # noqa: BLE001 - evidence is never allowed to fail a gate
        print(f"evidence write failed for {name}: {exc}")


def _copy_artifacts(out_dir: str, names, subdir: str) -> dict:
    """Copy ``names`` from ``out_dir`` into ``$UV_GATE_EVIDENCE_DIR/<subdir>/``."""
    copied: dict[str, str] = {}
    ev_dir = _evidence_dir()
    if not ev_dir:
        return copied
    packet = os.path.join(ev_dir, subdir)
    try:
        os.makedirs(packet, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        return {"__error__": str(exc)}
    for name in names:
        src = os.path.join(out_dir, name)
        if not os.path.exists(src):
            copied[name] = "missing"
            continue
        try:
            shutil.copyfile(src, os.path.join(packet, name))
            copied[name] = os.path.join(packet, name)
        except Exception as exc:  # noqa: BLE001 - a copy must not fail the gate
            copied[name] = f"copy failed: {exc}"
    return copied


# ---------------------------------------------------------------------------
# fixtures + worker plumbing
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def fixtures(tmp_path_factory) -> dict:
    """Build the baseline fixture set ONCE for this module (G0)."""
    out_dir = str(tmp_path_factory.mktemp("gltf_topo_fixtures"))
    proc = run_blender_python(_BUILD_SCRIPT, ["--out", out_dir])
    assert proc.returncode == 0, (proc.stdout or "")[-2000:] + "\n" + (proc.stderr or "")[-4000:]
    manifest_path = os.path.join(out_dir, "fixture_manifest.json")
    assert os.path.exists(manifest_path), "fixture_manifest.json was written"
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    return {"dir": out_dir, "manifest": manifest,
            "by_name": {f["name"]: f for f in manifest["fixtures"]}}


def _asset(fixtures: dict, name: str, fmt: str) -> str:
    """Absolute path of the ``fmt`` (``glb``/``fbx``/``obj``/``blend``) export."""
    return os.path.join(fixtures["dir"], fixtures["by_name"][name][fmt])


def _reference(fixtures: dict, name: str) -> dict:
    return fixtures["by_name"][name]["reference_topology"]


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _job(model: str, out_dir: str, project: str, run_id: str, *,
         render_previews: bool = False) -> dict:
    return {
        "command": "generate_uv_from_seams",
        "project_id": "e2e_gltf_topology",
        "run_id": run_id,
        "model": model,
        "model_rel": os.path.basename(model),
        "object_name": _OBJECT_NAME,
        "seam_spec": None,
        "mode": "auto_generate",
        "out_dir": out_dir,
        "selected_blend_out": os.path.join(project, "work", "uv", "selected_uv.blend"),
        "selected_blend_out_rel": os.path.join("work", "uv", "selected_uv.blend"),
        "selected_summary_out": os.path.join(project, "work", "uv", "selected_uv_summary.json"),
        "options": {**_OPTIONS, "render_previews": bool(render_previews)},
    }


def _run(job: dict, job_dir, tag: str, timeout: int = 900):
    job_path = os.path.join(str(job_dir), f"{tag}.json")
    os.makedirs(os.path.dirname(job_path) or ".", exist_ok=True)
    with open(job_path, "w", encoding="utf-8") as fh:
        json.dump(job, fh)
    started = time.perf_counter()
    proc = run_blender_python(_UV_WORKER, ["--job", job_path], timeout=timeout)
    return proc, time.perf_counter() - started


def _export_job(model: str, summary: str, out_dir: str, formats: list[str],
                object_name, export_id: str) -> dict:
    return {
        "command": "export_production_asset",
        "project_id": "e2e_gltf_topology",
        "export_id": export_id,
        "selected_uv_model": model,
        "selected_uv_model_rel": os.path.join("work", "uv", "selected_uv.blend"),
        "selected_uv_summary": summary,
        "selected_uv_summary_rel": os.path.join("work", "uv", "selected_uv_summary.json"),
        "object_name": object_name,
        "formats": list(formats),
        "out_dir": out_dir,
        "out_dir_rel": os.path.join("exports", export_id),
        "uv_generate_run_id": "uv_gltf_export",
        "options": {"render_previews": False},
        "out": os.path.join(out_dir, "export_result.json"),
    }


def _run_export(job: dict, job_dir, tag: str, timeout: int = 1800):
    job_path = os.path.join(str(job_dir), f"{tag}.json")
    os.makedirs(os.path.dirname(job_path) or ".", exist_ok=True)
    with open(job_path, "w", encoding="utf-8") as fh:
        json.dump(job, fh)
    return run_blender_python(_EXPORT_WORKER, ["--job", job_path], timeout=timeout)


def _read(out_dir: str, name: str):
    path = os.path.join(out_dir, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _tail(proc) -> str:
    return (proc.stdout or "")[-2000:] + "\n" + (proc.stderr or "")[-4000:]


def _pick(block, *keys):
    if not isinstance(block, dict):
        return None
    return {k: block.get(k) for k in keys}


_TOPO_KEYS = ("vertex_count", "edge_count", "face_count", "connected_component_count",
              "boundary_edge_count", "non_manifold_edge_count",
              "duplicate_position_vertex_count")

_SEAM_REASON_KEYS = ("boundary_topology", "mandatory_fold", "segmentation", "distortion")

_MANDATORY_KEYS = ("mandatory_fold_edges", "mandatory_boundary_edges",
                   "mandatory_non_manifold_edges", "mandatory_90_edges",
                   "mandatory_90_missing", "mandatory_90_uv_unsplit")


def _topology_side(import_topology, side: str) -> dict:
    return _pick((import_topology or {}).get(side), *_TOPO_KEYS) or {}


def _seam_reasons(summary) -> dict:
    """The seam-reason histogram, recorded in full and normalized for comparison."""
    raw = (summary or {}).get("seam_reason_counts") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): raw.get(k) for k in raw}


def _collect_run(out_dir: str, project: str, proc, wall_s: float, run_id: str,
                 *, fixture: str | None = None, fmt: str | None = None) -> dict:
    """Everything TN4 records about one worker run."""
    summary = _read(out_dir, "uv_generate_summary.json") or {}
    status = _read(out_dir, "status.json") or {}
    manifest = _read(out_dir, "run_manifest.json") or {}
    mesh_identity = _read(out_dir, "mesh_identity.json") or {}
    import_topology_file = _read(out_dir, "import_topology.json")
    import_topology = import_topology_file
    if import_topology is None:
        import_topology = summary.get("import_topology")
    p5 = _read(out_dir, "p5_gate.json") or {}

    gate = summary.get("auto_gate") or {}
    frag_metrics = ((summary.get("fragmentation") or {}).get("metrics")) or {}
    dist_global = ((summary.get("distortion_v2") or {}).get("global")) or {}
    return {
        "run_id": run_id,
        "fixture": fixture,
        "format": fmt,
        "out_dir": out_dir,
        "project": project,
        "exit_code": proc.returncode,
        "wall_s": round(wall_s, 3),
        "stderr_tail": (proc.stderr or "")[-1500:],
        "status": status.get("status"),
        "status_error": status.get("error"),
        "import_topology_file_present": import_topology_file is not None,
        "import_topology_summary_present": summary.get("import_topology") is not None,
        "import_topology": import_topology,
        "import_topology_header": _pick(import_topology, "format",
                                        "merge_vertices_enabled",
                                        "position_weld_applied", "weld_tolerance",
                                        "welded_vertex_count"),
        "pre_normalization": _topology_side(import_topology, "pre_normalization"),
        "post_normalization": _topology_side(import_topology, "post_normalization"),
        "delta": (import_topology or {}).get("delta"),
        "seam_reason_counts": _seam_reasons(summary),
        "mandatory_audit": _pick(summary.get("mandatory_audit"), *_MANDATORY_KEYS),
        "auto_gate": {"valid": gate.get("valid"), "passed": gate.get("passed"),
                      "failures": gate.get("failures"),
                      "invalid_reasons": gate.get("invalid_reasons")},
        "acceptance_reason": summary.get("acceptance_reason"),
        "metrics": summary.get("metrics"),
        "island_count": (summary.get("metrics") or {}).get("island_count"),
        "p5_island_count": p5.get("final_island_count"),
        "seams": sorted(int(s) for s in (p5.get("seams") or ())),
        "seam_count": len(p5.get("seams") or ()),
        "fragmentation_metrics": _pick(frag_metrics, "island_count", "tiny_island_count",
                                       "sliver_island_count", "one_two_face_island_count",
                                       "island_aspect_p95", "normalized_seam_length"),
        "distortion_global": _pick(dist_global, "anisotropy_p95", "anisotropy_max",
                                   "area_stretch_mean", "exceed_area_fraction"),
        "raster_overlap_ratio": (summary.get("metrics") or {}).get("raster_overlap_ratio"),
        "overlap_ratio": (summary.get("metrics") or {}).get("overlap_ratio"),
        "quality_report_passed": summary.get("quality_report_passed"),
        "source_asset_hash": manifest.get("source_asset_hash"),
        "normalized_mesh_fingerprint": manifest.get("normalized_mesh_fingerprint"),
        "model_sha256": manifest.get("model_sha256"),
        "mesh_identity_before_fingerprint": (mesh_identity.get("before") or {}).get("fingerprint"),
        "mesh_identity_after_fingerprint": (mesh_identity.get("after") or {}).get("fingerprint"),
        "mesh_identity_unchanged": (summary.get("mesh_identity") or {}).get("unchanged"),
        "uv_hash": summary.get("uv_hash"),
        "object_name": summary.get("object_name"),
        "warnings": summary.get("warnings"),
    }


def _run_fixture(fixtures: dict, tmp_path, name: str, fmt: str, *,
                 run_id: str | None = None, timeout: int = 900,
                 render_previews: bool = False) -> dict:
    run_id = run_id or f"uv_topo_{name}_{fmt}"
    project = str(tmp_path / run_id)
    out_dir = os.path.join(project, "run")
    os.makedirs(out_dir, exist_ok=True)
    model = _asset(fixtures, name, fmt)
    job = _job(model, out_dir, project, run_id, render_previews=render_previews)
    try:
        proc, wall_s = _run(job, tmp_path, f"job_{run_id}", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"run_id": run_id, "fixture": name, "format": fmt, "timeout": True,
                "exit_code": None, "out_dir": out_dir, "project": project,
                "model": model}
    row = _collect_run(out_dir, project, proc, wall_s, run_id, fixture=name, fmt=fmt)
    row["model"] = model
    row["model_sha256_on_disk"] = _sha256_file(model)
    row["reference_topology"] = _reference(fixtures, name)
    return row


# ---------------------------------------------------------------------------
# 1. G3/G5/G7 — a split-vertex GLB import normalizes back to the source topology
# ---------------------------------------------------------------------------
@requires_blender
def test_glb_split_vertex_recovery(fixtures, tmp_path):
    rows: list[dict] = []
    evidence: dict = {"gates": ["G3", "G5", "G7", "G4"], "rows": rows,
                      "fixtures": list(_SPLIT_FIXTURES)}
    try:
        for name in _SPLIT_FIXTURES:
            rows.append(_run_fixture(fixtures, tmp_path, name, "glb", timeout=900))
    finally:
        _write_evidence("glb_split_vertex_recovery", evidence)

    by_name = {r["fixture"]: r for r in rows}
    assert sorted(by_name) == sorted(_SPLIT_FIXTURES), sorted(by_name)

    for name in _SPLIT_FIXTURES:
        row = by_name[name]
        assert row.get("timeout") is not True, f"{name}: worker timed out"
        # The non-manifold fixture is allowed to fail its gate; it must still run.
        if name == "non_manifold_fan":
            assert row["exit_code"] in (0, 2), row
        else:
            assert row["exit_code"] == 0, row

        assert row["import_topology"], f"{name}: import_topology.json was written"
        pre = row["pre_normalization"]
        post = row["post_normalization"]
        ref = row["reference_topology"]
        assert pre and post, f"{name}: pre/post normalization blocks present"

        # G4 — normalization welds vertices, it never changes the face set.
        assert post["face_count"] == pre["face_count"], f"{name}: face_count unchanged {row}"

        # G3/G5/G7 — the recovered topology is the source topology.
        assert post["boundary_edge_count"] == ref["boundary_edge_count"], \
            f"{name}: boundary {post} vs reference {ref}"
        assert post["non_manifold_edge_count"] == ref["non_manifold_edge_count"], \
            f"{name}: non-manifold {post} vs reference {ref}"
        assert post["connected_component_count"] == ref["component_count"], \
            f"{name}: components {post} vs reference {ref}"

    # Closed fixtures: no fake boundary survives, and nothing downstream saw one.
    for name in _CLOSED_FIXTURES:
        row = by_name[name]
        post = row["post_normalization"]
        assert post["boundary_edge_count"] == 0, f"{name}: {post}"
        assert post["connected_component_count"] == _EXPECTED_COMPONENTS[name], \
            f"{name}: {post}"
        reasons = row["seam_reason_counts"]
        assert reasons.get("boundary_topology") == 0, f"{name}: seam reasons {reasons}"
        mand = row["mandatory_audit"] or {}
        assert mand.get("mandatory_boundary_edges") == 0, f"{name}: {mand}"
        assert mand.get("mandatory_90_missing") == 0, f"{name}: {mand}"

    # Genuine boundary is preserved, genuine non-manifold geometry is reported.
    open_plane = by_name["open_plane"]
    assert open_plane["post_normalization"]["boundary_edge_count"] > 0, open_plane

    fan = by_name["non_manifold_fan"]
    assert fan["post_normalization"]["non_manifold_edge_count"] >= 1, fan
    assert fan["status"] != "accepted", fan


# ---------------------------------------------------------------------------
# 2. G8/G14/G6 — the same asset through FBX and through GLB agrees
# ---------------------------------------------------------------------------
@requires_blender
def test_fbx_glb_parity(fixtures, tmp_path):
    rows: list[dict] = []
    comparison: dict = {}
    evidence: dict = {"gates": ["G8", "G14", "G6"], "rows": rows,
                      "comparison": comparison, "fixtures": list(_PARITY_FIXTURES)}
    try:
        for name in _PARITY_FIXTURES:
            per_format = {}
            for fmt in ("fbx", "glb"):
                row = _run_fixture(fixtures, tmp_path, name, fmt, timeout=900)
                rows.append(row)
                per_format[fmt] = row
            fbx, glb = per_format["fbx"], per_format["glb"]
            comparison[name] = {
                "post_normalization_fbx": fbx.get("post_normalization"),
                "post_normalization_glb": glb.get("post_normalization"),
                "mandatory_audit_fbx": fbx.get("mandatory_audit"),
                "mandatory_audit_glb": glb.get("mandatory_audit"),
                "seam_reason_counts_fbx": fbx.get("seam_reason_counts"),
                "seam_reason_counts_glb": glb.get("seam_reason_counts"),
                "island_count_fbx": fbx.get("island_count"),
                "island_count_glb": glb.get("island_count"),
                "normalized_mesh_fingerprint_fbx": fbx.get("normalized_mesh_fingerprint"),
                "normalized_mesh_fingerprint_glb": glb.get("normalized_mesh_fingerprint"),
                "source_asset_hash_fbx": fbx.get("source_asset_hash"),
                "source_asset_hash_glb": glb.get("source_asset_hash"),
            }
    finally:
        _write_evidence("fbx_glb_parity", evidence)

    by_key = {(r["fixture"], r["format"]): r for r in rows}
    for name in _PARITY_FIXTURES:
        fbx = by_key[(name, "fbx")]
        glb = by_key[(name, "glb")]
        for row, fmt in ((fbx, "fbx"), (glb, "glb")):
            assert row.get("timeout") is not True, f"{name}/{fmt}: worker timed out"
            assert row["exit_code"] == 0, row
            assert row["import_topology"], f"{name}/{fmt}: import_topology present"

        cmp_row = comparison[name]
        for key in ("connected_component_count", "boundary_edge_count",
                    "non_manifold_edge_count"):
            assert fbx["post_normalization"][key] == glb["post_normalization"][key], \
                f"{name}: post_normalization.{key} differs: {cmp_row}"

        # Tolerance 0: the mandatory fold set is a property of the surface, not the file.
        assert (fbx["mandatory_audit"] or {}).get("mandatory_fold_edges") == \
            (glb["mandatory_audit"] or {}).get("mandatory_fold_edges"), cmp_row

        for key in ("boundary_topology", "mandatory_fold"):
            assert fbx["seam_reason_counts"].get(key) == glb["seam_reason_counts"].get(key), \
                f"{name}: seam_reason_counts.{key} differs: {cmp_row}"

        assert fbx["island_count"] == glb["island_count"], cmp_row

        # G6 — the normalized mesh is the same mesh, whatever file it arrived in.
        assert fbx["normalized_mesh_fingerprint"], cmp_row
        assert fbx["normalized_mesh_fingerprint"] == glb["normalized_mesh_fingerprint"], cmp_row


# ---------------------------------------------------------------------------
# 3. G6 — source hash vs normalized fingerprint: two digests, taken in order
# ---------------------------------------------------------------------------
@requires_blender
def test_identity_ordering(fixtures, tmp_path):
    name = "split_normal_glb"
    evidence: dict = {"gates": ["G6"], "fixture": name}
    try:
        row = _run_fixture(fixtures, tmp_path, name, "glb", run_id="uv_topo_identity",
                           timeout=900)
        evidence["row"] = row
        evidence["glb_sha256_on_disk"] = row.get("model_sha256_on_disk")
        evidence["manifest_glb_sha256"] = fixtures["by_name"][name]["glb_sha256"]
    finally:
        _write_evidence("identity_ordering", evidence)

    assert row.get("timeout") is not True, row
    assert row["exit_code"] == 0, row

    # source_asset_hash names the FILE that was read.
    assert row["source_asset_hash"] == row["model_sha256_on_disk"], row
    assert row["source_asset_hash"] == evidence["manifest_glb_sha256"], row

    # The normalized fingerprint is a different digest, taken AFTER normalization.
    assert row["normalized_mesh_fingerprint"], row
    assert row["normalized_mesh_fingerprint"] != row["source_asset_hash"], row

    # mesh_identity's "before" is the normalized mesh: the engine's before/after
    # comparison starts from the normalized mesh, not the raw import.
    assert row["mesh_identity_before_fingerprint"] == row["normalized_mesh_fingerprint"], row


# ---------------------------------------------------------------------------
# 4. G0/G9/G15 — the named real-model regression (lowpoly_6)
# ---------------------------------------------------------------------------
@requires_blender
@requires_real_glb
def test_lowpoly6_real_glb_regression(tmp_path):
    evidence: dict = {"gates": ["G0", "G9", "G15"], "fixture": REAL_GLB,
                      "fixture_sha256_expected": REAL_GLB_SHA256,
                      "fixture_size_bytes_expected": REAL_GLB_SIZE,
                      "before": LOWPOLY6_BEFORE}
    row: dict = {}
    try:
        evidence["fixture_sha256_actual"] = _sha256_file(REAL_GLB)
        evidence["fixture_size_bytes_actual"] = os.path.getsize(REAL_GLB)

        run_id = "uv_topo_lowpoly6"
        project = str(tmp_path / run_id)
        out_dir = os.path.join(project, "run")
        os.makedirs(out_dir, exist_ok=True)
        job = _job(REAL_GLB, out_dir, project, run_id, render_previews=True)
        proc, wall_s = _run(job, tmp_path, f"job_{run_id}", timeout=3000)
        row = _collect_run(out_dir, project, proc, wall_s, run_id,
                           fixture="lowpoly_6", fmt="glb")
        evidence["run"] = row

        summary = _read(out_dir, "uv_generate_summary.json") or {}
        frag = (summary.get("fragmentation") or {})
        gate = summary.get("auto_gate") or {}
        evidence["after"] = {
            "import_topology_pre": row["pre_normalization"],
            "import_topology_post": row["post_normalization"],
            "import_topology_delta": row["delta"],
            "import_topology_header": row["import_topology_header"],
            "mandatory_audit": row["mandatory_audit"],
            "seam_reason_counts": row["seam_reason_counts"],
            "seam_count": row["seam_count"],
            "island_count": row["island_count"],
            "tiny_island_count": (row["fragmentation_metrics"] or {}).get("tiny_island_count"),
            "sliver_island_count": (row["fragmentation_metrics"]
                                    or {}).get("sliver_island_count"),
            "one_two_face_island_count": (row["fragmentation_metrics"]
                                          or {}).get("one_two_face_island_count"),
            "raster_overlap_ratio": row["raster_overlap_ratio"],
            "overlap_ratio": row["overlap_ratio"],
            "anisotropy_p95": (row["distortion_global"] or {}).get("anisotropy_p95"),
            "anisotropy_max": (row["distortion_global"] or {}).get("anisotropy_max"),
            "status": row["status"],
            "gate_passed": gate.get("passed"),
            "gate_failures": gate.get("failures"),
            "fragmentation_failures": frag.get("failures"),
            "fragmentation_quality_failures": frag.get("quality_failures"),
            "quality_report_passed": row["quality_report_passed"],
            "wall_s": row["wall_s"],
        }
        evidence["before_after_table"] = [
            {"metric": "face_count",
             "before": LOWPOLY6_BEFORE["face_count"],
             "after": (row["post_normalization"] or {}).get("face_count")},
            {"metric": "vertex_count",
             "before": LOWPOLY6_BEFORE["vertex_count"],
             "after": (row["post_normalization"] or {}).get("vertex_count")},
            {"metric": "edge_count",
             "before": LOWPOLY6_BEFORE["edge_count"],
             "after": (row["post_normalization"] or {}).get("edge_count")},
            {"metric": "boundary_edge_count",
             "before": LOWPOLY6_BEFORE["mandatory_90_edges"],
             "after": (row["post_normalization"] or {}).get("boundary_edge_count"),
             "note": "before: every mandatory-90 edge was a fake glTF split boundary"},
            {"metric": "seam_reason_counts.boundary_topology",
             "before": LOWPOLY6_BEFORE["seam_reason_counts"]["boundary_topology"],
             "after": row["seam_reason_counts"].get("boundary_topology")},
            {"metric": "seam_count", "before": LOWPOLY6_BEFORE["seam_count"],
             "after": row["seam_count"]},
            {"metric": "island_count", "before": LOWPOLY6_BEFORE["island_count"],
             "after": row["island_count"]},
            {"metric": "tiny_island_count", "before": LOWPOLY6_BEFORE["tiny_island_count"],
             "after": (row["fragmentation_metrics"] or {}).get("tiny_island_count")},
            {"metric": "sliver_island_count", "before": LOWPOLY6_BEFORE["sliver_island_count"],
             "after": (row["fragmentation_metrics"] or {}).get("sliver_island_count")},
            {"metric": "one_two_face_island_count",
             "before": LOWPOLY6_BEFORE["one_two_face_island_count"],
             "after": (row["fragmentation_metrics"] or {}).get("one_two_face_island_count")},
            {"metric": "raster_overlap_ratio",
             "before": LOWPOLY6_BEFORE["raster_overlap_ratio"],
             "after": row["raster_overlap_ratio"]},
            {"metric": "anisotropy_p95", "before": LOWPOLY6_BEFORE["anisotropy_p95"],
             "after": (row["distortion_global"] or {}).get("anisotropy_p95")},
            {"metric": "anisotropy_max", "before": LOWPOLY6_BEFORE["anisotropy_max"],
             "after": (row["distortion_global"] or {}).get("anisotropy_max")},
        ]
        evidence["copied"] = _copy_artifacts(out_dir, _LOWPOLY6_ARTIFACTS, _LOWPOLY6_SUBDIR)
    finally:
        _write_evidence("lowpoly6_real_glb_regression", evidence)

    assert evidence["fixture_sha256_actual"] == REAL_GLB_SHA256, evidence

    # Only the cheap invariants are gated here; the verdict is read off the table.
    assert row.get("timeout") is not True, "worker timed out"
    assert row["exit_code"] == 0, row.get("stderr_tail")
    post = row["post_normalization"]
    assert post, "import_topology.post_normalization present"
    assert post["face_count"] == LOWPOLY6_BEFORE["face_count"], row
    assert post["boundary_edge_count"] < LOWPOLY6_BEFORE["mandatory_90_edges"], row
    assert row["seam_reason_counts"].get("boundary_topology") is not None, row
    assert row["seam_reason_counts"]["boundary_topology"] < \
        LOWPOLY6_BEFORE["seam_reason_counts"]["boundary_topology"], row


# ---------------------------------------------------------------------------
# 5. G13 — three identical runs on the real GLB agree exactly
# ---------------------------------------------------------------------------
@requires_blender
@requires_real_glb
def test_lowpoly6_three_runs_deterministic(tmp_path):
    rows: list[dict] = []
    comparison: dict = {}
    evidence: dict = {"gates": ["G13"], "fixture": REAL_GLB, "runs": rows,
                      "comparison": comparison}
    try:
        for i in range(3):
            run_id = f"uv_topo_lowpoly6_det_{i}"
            project = str(tmp_path / run_id)
            out_dir = os.path.join(project, "run")
            os.makedirs(out_dir, exist_ok=True)
            job = _job(REAL_GLB, out_dir, project, run_id, render_previews=False)
            proc, wall_s = _run(job, tmp_path, f"job_{run_id}", timeout=3000)
            rows.append(_collect_run(out_dir, project, proc, wall_s, run_id,
                                     fixture="lowpoly_6", fmt="glb"))

        base = rows[0]
        for other in rows[1:]:
            comparison[f"{base['run_id']}_vs_{other['run_id']}"] = {
                "pre_equal": other["pre_normalization"] == base["pre_normalization"],
                "post_equal": other["post_normalization"] == base["post_normalization"],
                "fingerprint_equal": (other["normalized_mesh_fingerprint"]
                                      == base["normalized_mesh_fingerprint"]),
                "seams_equal": other["seams"] == base["seams"],
                "island_count_equal": other["island_count"] == base["island_count"],
                "seam_reason_counts_equal": (other["seam_reason_counts"]
                                             == base["seam_reason_counts"]),
            }
    finally:
        _write_evidence("lowpoly6_three_runs_deterministic", evidence)

    assert len(rows) == 3, rows
    for row in rows:
        assert row["exit_code"] == 0, row.get("stderr_tail")
        assert row["post_normalization"], row

    base = rows[0]
    for other in rows[1:]:
        key = f"{base['run_id']}_vs_{other['run_id']}"
        cmp_row = comparison[key]
        assert cmp_row["pre_equal"], cmp_row
        assert cmp_row["post_equal"], cmp_row
        assert cmp_row["fingerprint_equal"], cmp_row
        assert cmp_row["seams_equal"], key
        assert cmp_row["island_count_equal"], cmp_row
        assert cmp_row["seam_reason_counts_equal"], cmp_row
    assert base["normalized_mesh_fingerprint"], base


# ---------------------------------------------------------------------------
# 6. G14 — export the GLB-sourced run back out and re-read the same topology
# ---------------------------------------------------------------------------
@requires_blender
def test_export_reread_parity(fixtures, tmp_path):
    name = "bevel_cube"
    formats = ["fbx", "glb"]
    evidence: dict = {"gates": ["G14"], "fixture": name, "formats": formats}
    row: dict = {}
    try:
        row = _run_fixture(fixtures, tmp_path, name, "glb",
                           run_id="uv_topo_export_source", timeout=900)
        evidence["uv_run"] = row
        assert row.get("timeout") is not True, row
        assert row["exit_code"] == 0, row.get("stderr_tail")

        shipped = os.path.join(row["project"], "work", "uv", "selected_uv.blend")
        shipped_summary = os.path.join(row["project"], "work", "uv",
                                       "selected_uv_summary.json")
        if os.path.exists(shipped):
            export_model, export_summary = shipped, shipped_summary
            evidence["export_input"] = "work/uv/selected_uv.blend"
        else:
            export_model = os.path.join(row["out_dir"], "selected_uv.blend")
            export_summary = os.path.join(row["out_dir"], "uv_generate_summary.json")
            evidence["export_input"] = "run/selected_uv.blend"
        evidence["export_model_exists"] = os.path.exists(export_model)

        export_dir = os.path.join(row["project"], "exports", "export_tn4")
        os.makedirs(export_dir, exist_ok=True)
        eproc = _run_export(_export_job(export_model, export_summary, export_dir, formats,
                                        row.get("object_name"), "export_tn4"),
                            tmp_path, "job_export_tn4")
        evidence["export_exit_code"] = eproc.returncode
        evidence["export_stderr_tail"] = (eproc.stderr or "")[-1500:]

        estatus = _read(export_dir, "status.json") or {}
        result = _read(export_dir, "export_result.json") or {}
        reread = _read(export_dir, "export_reread_report.json") or {}
        evidence["export_status"] = estatus.get("status")
        evidence["export_failed_formats"] = result.get("failed_formats") or []
        evidence["export_reread_report"] = reread

        per_format = {}
        for fmt in formats:
            block = ((reread.get("formats") or {}).get(fmt)) or {}
            block_topology = block.get("import_topology") or {}
            per_format[fmt] = {
                "passed": block.get("passed"),
                "failures": block.get("failures"),
                "post_normalization": _topology_side(block_topology, "post_normalization"),
                "pre_normalization": _topology_side(block_topology, "pre_normalization"),
                "mandatory_90_uv_unsplit": (block.get("mandatory")
                                            or {}).get("mandatory_90_uv_unsplit"),
                "vertex_weld": block.get("vertex_weld"),
            }
        evidence["per_format"] = per_format
        evidence["uv_post_normalization"] = row["post_normalization"]
    finally:
        _write_evidence("export_reread_parity", evidence)

    assert evidence["export_exit_code"] == 0, evidence.get("export_stderr_tail")
    assert evidence["export_status"] == "accepted", evidence

    uv_post = evidence["uv_post_normalization"]
    for fmt in formats:
        block = evidence["per_format"][fmt]
        assert block["passed"] is True, block
        assert not block["failures"], block
        post = block["post_normalization"]
        assert post, f"{fmt}: reread import_topology.post_normalization present"
        for key in ("connected_component_count", "boundary_edge_count"):
            assert post[key] == uv_post[key], \
                f"{fmt}: post_normalization.{key} {post[key]} != UV run {uv_post[key]}"
        assert block["mandatory_90_uv_unsplit"] == 0, block
