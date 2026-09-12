"""OBJ loader + end-to-end packing on the committed sample model."""

import os

import pytest

from uv_agent.geometry.evaluation import evaluate_uv_solution
from uv_agent.geometry.packing import pack_islands
from uv_agent.geometry.projection import project_island
from uv_agent.geometry.solution import UVMap
from uv_agent.io.obj_loader import load_obj
from uv_agent.planner.island_planner import plan_islands

SAMPLE = os.path.join(os.path.dirname(__file__), "..", "sample", "uv_no.obj")

requires_sample = pytest.mark.skipif(
    not os.path.exists(SAMPLE),
    reason=(
        "sample/uv_no.obj not present (gitignored mesh asset); "
        "affects G9 evidence only, not a UV gate"
    ),
)


@requires_sample
def test_load_sample_obj():
    m = load_obj(SAMPLE)
    assert m.vertex_count == 20
    assert m.face_count == 18
    # closed-ish hard-surface block: every face has a normal + area
    assert all(f.area_3d > 0 for f in m.faces)


@requires_sample
def test_sample_obj_end_to_end_auto_packing():
    m = load_obj(SAMPLE)
    plan = plan_islands(m)
    uvm = UVMap.for_mesh(m)
    for isl in plan.islands:
        project_island(m, isl.face_ids, uvm, isl.projection)
    pack_islands(m, plan, uvm, strategy="auto")
    ev = evaluate_uv_solution(m, plan, uvm)
    assert (uvm.uv >= -1e-6).all() and (uvm.uv <= 1 + 1e-6).all()
    assert ev.overlap_ratio == 0.0
    assert ev.status == "accepted"
