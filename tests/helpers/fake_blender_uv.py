"""Deterministic, Blender-free stand-in for the chart-UV unwrap backend (G4/G5 test infra).

The chart-UV pipeline (:mod:`chart_uv_agent.pipeline`) is pure except for a handful of
Blender calls it pulls in with function-local imports from :mod:`chart_uv_agent.unwrap`
(``unwrap_and_pack`` / ``repack`` / ``read_uvmap`` / ``reunwrap_faces`` / ``pack_subset``)
and :mod:`chart_uv_agent.island_layout` (``repack_uv_islands_custom``). Because those are
resolved as *module attributes* at call time, replacing the attributes is enough to run the
whole pipeline without ``bpy``.

This module is TEST-ONLY (never imported by product code) and must never import ``bpy``.
Everything here is deterministic: the same mesh + seam set always yields the same UVs
(no randomness at all — ``seed`` exists only for interface compatibility).
"""

from __future__ import annotations

import numpy as np

from uv_agent.blender.organic_unwrap import AI_UV_LAYER, island_plan_from_seams
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.packing import pack_islands
from uv_agent.geometry.projection import project_island_planar
from uv_agent.geometry.solution import UVMap


class _FakeUvLayers:
    """Minimal ``mesh.uv_layers`` stand-in: name membership + an ``active`` name."""

    def __init__(self) -> None:
        self._names: list[str] = []
        self.active: str | None = None

    def __contains__(self, name) -> bool:
        return name in self._names

    def __iter__(self):
        return iter(self._names)

    def __len__(self) -> int:
        return len(self._names)

    def __getitem__(self, name):
        return name

    def get(self, name, default=None):
        return name if name in self._names else default

    def new(self, name: str = AI_UV_LAYER):
        if name not in self._names:
            self._names.append(name)
        return name


class _FakeMeshData:
    """Minimal ``obj.data`` stand-in (uv layer bookkeeping only).

    ``edges`` / ``polygons`` are present but EMPTY: that is enough for
    :func:`uv_agent.geometry.shading_policy.shading_snapshot` to produce a real (empty)
    snapshot, and it keeps ``mark_seams`` / ``apply_smoothing_split_by_edges`` harmless
    no-ops off-Blender — they only ever iterate these lists.
    """

    def __init__(self) -> None:
        self.uv_layers = _FakeUvLayers()
        self.edges: list = []
        self.polygons: list = []

    def update(self) -> None:
        return None


class FakeUvObject:
    """Stand-in for the Blender object the pipeline mutates.

    ``uv`` holds the current :class:`UVMap` (``None`` before the first unwrap);
    ``marked_seams`` mirrors what ``mark_seams`` would have set on the real mesh.
    """

    def __init__(self, name: str = "fake_uv_object") -> None:
        self.name = name
        self.uv: UVMap | None = None
        self.data = _FakeMeshData()
        self.marked_seams: set[int] = set()


class FakeUnwrapBackend:
    """Deterministic pure-Python replacement for the Blender unwrap/pack calls.

    Each island is flattened with :func:`project_island_planar` (area-weighted normal
    plane), optionally density-normalised (``average_scale``), then laid out in [0,1]^2 by
    :func:`pack_islands`. No randomness is used anywhere.
    """

    def __init__(self, mesh: MeshGraph, *, seed: int = 0) -> None:
        self.mesh = mesh
        self.seed = seed              # interface only — nothing here is random
        self.calls: list[tuple] = []
        self.last_seams: set[int] = set()

    # ---------------------------------------------------------------- internals
    def _project_all(self, seams):
        seams = {int(e) for e in seams}
        plan = island_plan_from_seams(self.mesh, seams)
        uvmap = UVMap.for_mesh(self.mesh)
        for isl in plan.islands:
            if isl.face_ids:
                project_island_planar(self.mesh, isl.face_ids, uvmap)
        return plan, uvmap

    def _island_loops(self, face_ids) -> list[int]:
        return [li for fid in face_ids for li in self.mesh.faces[fid].loop_indices]

    def _uv_area(self, uvmap: UVMap, face_ids) -> float:
        total = 0.0
        for fid in face_ids:
            li = self.mesh.faces[fid].loop_indices
            for i in range(1, len(li) - 1):
                a = uvmap.uv[li[0]]
                b = uvmap.uv[li[i]]
                c = uvmap.uv[li[i + 1]]
                total += abs(0.5 * ((b[0] - a[0]) * (c[1] - a[1])
                                    - (c[0] - a[0]) * (b[1] - a[1])))
        return float(total)

    def _average_islands_scale(self, plan, uvmap: UVMap) -> None:
        """Equalise every island's UV-area / 3D-area ratio (Blender's
        ``average_islands_scale`` equivalent). Deterministic: the target ratio is the
        3D-area-weighted mean over all islands."""
        ratios: list[tuple[list[int], float, float]] = []
        for isl in plan.islands:
            if not isl.face_ids:
                continue
            a3 = float(sum(self.mesh.faces[f].area_3d for f in isl.face_ids))
            auv = self._uv_area(uvmap, isl.face_ids)
            if a3 > 1e-12 and auv > 1e-16:
                ratios.append((isl.face_ids, auv / a3, a3))
        if not ratios:
            return
        wsum = sum(a3 for _f, _r, a3 in ratios)
        target = (sum(r * a3 for _f, r, a3 in ratios) / wsum) if wsum > 0 else ratios[0][1]
        for face_ids, ratio, _a3 in ratios:
            s = float(np.sqrt(target / ratio))
            loops = self._island_loops(face_ids)
            pts = uvmap.uv[loops]
            centroid = pts.mean(axis=0)
            uvmap.uv[loops] = centroid + (pts - centroid) * s

    def _pack(self, plan, uvmap: UVMap, margin: float, rotate: bool) -> None:
        pack_islands(self.mesh, plan, uvmap, padding=float(max(0.0, min(0.1, margin))),
                     allow_rotate=bool(rotate), strategy="auto")

    # ------------------------------------------------------------ backend API
    def unwrap_and_pack(self, obj, seams, *, margin: float = 0.02,
                        method: str = "MINIMUM_STRETCH", minimize_iters: int = 0,
                        pack_shape: str = "CONCAVE", rotate: bool = True,
                        average_scale: bool = True, layer_name: str = AI_UV_LAYER,
                        iterations: int | None = None, no_flip: bool = False,
                        fill_holes: bool = False) -> int:
        """``iterations`` / ``no_flip`` / ``fill_holes`` are accepted for interface parity
        with the real backend and IGNORED by the planar projection (they are solver knobs);
        they are recorded in ``self.calls`` so tests can assert what was requested."""
        seams = {int(e) for e in seams}
        plan, uvmap = self._project_all(seams)
        if average_scale:
            self._average_islands_scale(plan, uvmap)
        self._pack(plan, uvmap, margin, rotate)
        obj.data.uv_layers.new(layer_name)
        obj.data.uv_layers.active = layer_name
        obj.uv = uvmap
        obj.marked_seams = set(seams)
        self.last_seams = set(seams)
        self.calls.append(("unwrap", sorted(seams), margin, method,
                           {"iterations": iterations, "no_flip": bool(no_flip),
                            "fill_holes": bool(fill_holes)}))
        return len(seams)

    def repack(self, obj, *, margin: float = 0.02, pack_shape: str = "CONCAVE",
               rotate: bool = True, layer_name: str = AI_UV_LAYER) -> None:
        seams = set(obj.marked_seams or self.last_seams)
        plan = island_plan_from_seams(self.mesh, seams)
        uvmap = obj.uv.copy() if obj.uv is not None else UVMap.for_mesh(self.mesh)
        self._pack(plan, uvmap, margin, rotate)
        obj.uv = uvmap
        self.calls.append(("repack", sorted(seams), margin, pack_shape))

    def read_uvmap(self, obj, mesh: MeshGraph, *, layer_name: str = AI_UV_LAYER) -> UVMap:
        return obj.uv.copy() if obj.uv is not None else UVMap.for_mesh(mesh)

    def write_uvmap(self, obj, mesh: MeshGraph, uvmap: UVMap, *,
                    layer_name: str = AI_UV_LAYER) -> None:
        """Write half of the snapshot pair — restore ``uvmap`` onto ``obj`` (G5)."""
        obj.data.uv_layers.new(layer_name)
        obj.data.uv_layers.active = layer_name
        obj.uv = uvmap.copy()
        self.calls.append(("write_uvmap", layer_name, len(uvmap.uv)))

    def reunwrap_faces(self, obj, face_ids, *, method: str = "MINIMUM_STRETCH",
                       minimize_iters: int = 0, margin: float = 0.001,
                       layer_name: str = AI_UV_LAYER, iterations: int | None = None,
                       no_flip: bool = False, fill_holes: bool = False) -> int:
        """Re-project ONLY the islands containing ``face_ids`` in place (no re-pack — the
        caller re-packs). Islands are recovered from the last marked seam set; each
        re-projected island is translated back to its previous centroid so it stays where
        the packer put it.

        ``iterations`` / ``no_flip`` / ``fill_holes`` are accepted for interface parity and
        IGNORED by the projection; they are recorded in ``self.calls``."""
        face_ids = {int(f) for f in face_ids}
        seams = set(obj.marked_seams or self.last_seams)
        plan = island_plan_from_seams(self.mesh, seams)
        uvmap = obj.uv.copy() if obj.uv is not None else UVMap.for_mesh(self.mesh)
        touched = 0
        for isl in plan.islands:
            if isl.face_ids and face_ids.intersection(isl.face_ids):
                loops = self._island_loops(isl.face_ids)
                before = uvmap.uv[loops].copy()
                project_island_planar(self.mesh, isl.face_ids, uvmap)
                pts = uvmap.uv[loops]
                uvmap.uv[loops] = pts - pts.mean(axis=0) + before.mean(axis=0)
                touched += len(isl.face_ids)
        obj.uv = uvmap
        self.calls.append(("reunwrap_faces", sorted(face_ids), margin, method,
                           {"iterations": iterations, "no_flip": bool(no_flip),
                            "fill_holes": bool(fill_holes)}))
        return touched

    def pack_subset(self, obj, face_ids, *, margin: float = 0.01,
                    pack_shape: str = "AABB", layer_name: str = AI_UV_LAYER) -> int:
        """NO-OP placement-wise: this fake backend only packs globally. The call is
        recorded and the face count returned; existing UVs are left untouched."""
        face_ids = [int(f) for f in face_ids]
        self.calls.append(("pack_subset", sorted(face_ids), margin, pack_shape))
        return len(face_ids)

    def repack_uv_islands_custom(self, obj, mesh: MeshGraph, *, seams, padding: float,
                                 algorithm: str = "maxrects", allow_rotate: bool = True,
                                 density_normalize: bool = True,
                                 orient_long_islands: bool = False,
                                 layer_name: str | None = None) -> dict:
        """Custom-packer stand-in — ``algorithm`` is ignored; :func:`pack_islands` is used."""
        seams = {int(e) for e in seams}
        plan = island_plan_from_seams(mesh, seams)
        uvmap = obj.uv.copy() if obj.uv is not None else UVMap.for_mesh(mesh)
        if density_normalize:
            self._average_islands_scale(plan, uvmap)
        self._pack(plan, uvmap, padding, allow_rotate)
        obj.uv = uvmap
        islands = [isl.face_ids for isl in plan.islands if isl.face_ids]
        self.calls.append(("repack_custom", sorted(seams), padding, algorithm))
        return {"algorithm": algorithm, "islands": len(islands),
                "normalized": len(islands) if density_normalize else 0,
                "oriented": len(islands) if orient_long_islands else 0,
                "density_normalize": bool(density_normalize),
                "orient_long_islands": bool(orient_long_islands)}

    # ------------------------------------------------------------------ install
    def install(self, monkeypatch, *, name: str = "fake_uv_object") -> FakeUvObject:
        """Swap the Blender-backed module attributes for this backend's bound methods and
        return a fresh :class:`FakeUvObject` to run the pipeline against.

        ``chart_uv_agent.unwrap.island_plan_from_seams`` / ``flipped_faces`` are already
        pure, so they are left alone."""
        import chart_uv_agent.island_layout as island_layout
        import chart_uv_agent.unwrap as unwrap_mod

        monkeypatch.setattr(unwrap_mod, "unwrap_and_pack", self.unwrap_and_pack)
        monkeypatch.setattr(unwrap_mod, "repack", self.repack)
        monkeypatch.setattr(unwrap_mod, "read_uvmap", self.read_uvmap)
        monkeypatch.setattr(unwrap_mod, "write_uvmap", self.write_uvmap)
        monkeypatch.setattr(unwrap_mod, "reunwrap_faces", self.reunwrap_faces)
        monkeypatch.setattr(unwrap_mod, "pack_subset", self.pack_subset)
        monkeypatch.setattr(island_layout, "repack_uv_islands_custom",
                            self.repack_uv_islands_custom)
        return FakeUvObject(name)


__all__ = ["FakeUvObject", "FakeUnwrapBackend"]
