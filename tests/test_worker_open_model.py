"""TN5 / G6 / G14: the UV-path workers normalize input topology once per open.

``worker/generate_uv_from_seams.py`` already normalizes before it measures
anything. These tests pin the same behaviour on the two remaining UV-path
workers — ``review_existing_uv`` and ``seam_editor_worker`` — with a fake ``bpy``:
one opened model produces exactly ONE
:func:`uv_agent.blender.topology_normalize.normalize_topology` call, on the
resolved mesh object, with the format taken from the model path.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
_WORKER_DIR = os.path.join(_ROOT, "worker")
if _WORKER_DIR not in sys.path:
    sys.path.insert(0, _WORKER_DIR)


# --- minimal fake bpy ------------------------------------------------------
class _FakeOps:
    def __init__(self, calls):
        self._calls = calls

    class _Group:
        def __init__(self, calls, prefix):
            self._calls = calls
            self._prefix = prefix

        def __getattr__(self, name):
            def _call(**kwargs):
                self._calls.append((f"{self._prefix}.{name}", dict(kwargs)))
            return _call

    def __getattr__(self, name):
        return _FakeOps._Group(self._calls, name)


class _FakeMeshData:
    def __init__(self):
        self.vertices = []
        self.polygons = []
        self.uv_layers = []


class _FakeObject:
    def __init__(self, name="Cube"):
        self.name = name
        self.type = "MESH"
        self.data = _FakeMeshData()


class _FakeObjects(list):
    def get(self, name, default=None):
        for o in self:
            if o.name == name:
                return o
        return default

    def remove(self, obj, do_unlink=False):  # pragma: no cover - fallback path
        if obj in self:
            super().remove(obj)


class _FakeBpy:
    def __init__(self, objects=None):
        self.calls = []
        self.ops = _FakeOps(self.calls)

        class _Data:
            pass

        self.data = _Data()
        self.data.objects = _FakeObjects(objects if objects is not None else [_FakeObject()])


def _load_worker(name):
    path = os.path.join(_WORKER_DIR, name)
    spec = importlib.util.spec_from_file_location(name[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_UV_PATH_WORKERS = ("review_existing_uv.py", "seam_editor_worker.py")


@pytest.fixture
def recorder(monkeypatch):
    """Replace the real ``normalize_topology`` with a call recorder."""
    import uv_agent.blender.topology_normalize as tn

    calls = []

    def _fake(bpy, obj, *, fmt, merge_info=None, **kwargs):
        calls.append({"bpy": bpy, "obj": obj, "fmt": fmt, "merge_info": merge_info})
        return {"format": fmt, "merge_vertices_enabled": bool(
            (merge_info or {}).get("merge_vertices_enabled")),
            "position_weld_applied": False, "weld_tolerance": 0.0,
            "welded_vertex_count": 0,
            "pre_normalization": {}, "post_normalization": {}, "delta": {}}

    monkeypatch.setattr(tn, "normalize_topology", _fake)
    return calls


@pytest.mark.parametrize("worker", _UV_PATH_WORKERS)
def test_uv_path_worker_normalizes_topology_once_per_open(worker, recorder):
    mod = _load_worker(worker)
    bpy = _FakeBpy()
    model = os.path.join(_ROOT, "model.glb")

    merge_info = mod._open_model(bpy, model)
    report = mod._normalize_opened_model(bpy, model, merge_info, None)

    assert len(recorder) == 1, recorder
    call = recorder[0]
    assert call["fmt"] == "glb"
    assert call["obj"] is bpy.data.objects[0]
    assert call["merge_info"]["merge_vertices_enabled"] is True
    assert report["format"] == "glb"
    assert "error" not in report


@pytest.mark.parametrize("worker", _UV_PATH_WORKERS)
def test_uv_path_worker_normalization_failure_is_recorded_not_raised(worker, monkeypatch):
    import uv_agent.blender.topology_normalize as tn

    def _boom(*_a, **_kw):
        raise RuntimeError("bmesh unavailable")

    monkeypatch.setattr(tn, "normalize_topology", _boom)

    mod = _load_worker(worker)
    bpy = _FakeBpy()
    model = os.path.join(_ROOT, "model.glb")
    report = mod._normalize_opened_model(bpy, model, mod._open_model(bpy, model), None)
    assert report == {"error": "bmesh unavailable"}


@pytest.mark.parametrize("worker", _UV_PATH_WORKERS)
def test_no_mesh_object_is_an_error_record(worker, recorder):
    mod = _load_worker(worker)
    bpy = _FakeBpy(objects=[])
    model = os.path.join(_ROOT, "model.glb")
    report = mod._normalize_opened_model(bpy, model, mod._open_model(bpy, model), None)
    assert report == {"error": "no mesh object to normalize"}
    assert recorder == []


@pytest.mark.parametrize("worker", _UV_PATH_WORKERS)
def test_compact_block_keeps_the_summary_fields(worker):
    mod = _load_worker(worker)
    block = mod._compact_import_topology({
        "format": "glb", "merge_vertices_enabled": True, "position_weld_applied": True,
        "weld_tolerance": 1.7e-7, "welded_vertex_count": 16,
        "pre_normalization": {"boundary_edge_count": 24},
        "post_normalization": {"boundary_edge_count": 0},
        "delta": {"boundary_edge_delta": -24},
        "guards": {"face_count_unchanged": True},
        "policy": {"merge_vertices": True},
    })
    for key in ("format", "merge_vertices_enabled", "position_weld_applied",
                "weld_tolerance", "welded_vertex_count", "pre_normalization",
                "post_normalization", "delta"):
        assert key in block, (worker, key)
    assert "guards" not in block and "policy" not in block
    assert mod._compact_import_topology({"error": "boom"}) == {"error": "boom"}
    assert mod.IMPORT_TOPOLOGY_FILE == "import_topology.json"


def test_review_worker_run_functions_accept_the_report():
    mod = _load_worker("review_existing_uv.py")
    assert "import_topology" in inspect.signature(mod._run_inspect).parameters
    assert "import_topology" in inspect.signature(mod._run_review).parameters


def test_seam_worker_run_functions_accept_the_report():
    mod = _load_worker("seam_editor_worker.py")
    for fn in (mod._run_export_edge_geometry, mod._run_extract_uv_boundary,
               mod._run_validate_spec, mod._run_load_spec, mod._run_save_spec):
        assert "import_topology" in inspect.signature(fn).parameters, fn.__name__
