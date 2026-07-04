/**
 * Three.js edge-selection viewport (plan §8 "Initial implementation choice").
 *
 * Renders the worker-exported `edge_geometry.json` as colored line segments and
 * selects edges by **screen-space ray-to-segment distance** (plan §8/§14): every
 * edge's endpoints are projected with the camera and the cursor's pixel distance
 * to each segment is measured, which is far more reliable on thin/complex meshes
 * than a world-space line raycast. The renderer uses ONLY `edges[].id` from the
 * geometry as the selectable id — it never re-derives ids from mesh ordering
 * (plan §5, §14).
 *
 * Self-contained orbit/zoom/pan camera (no OrbitControls dependency) so the
 * renderer build has no extra moving parts. Colors update in place (no geometry
 * rebuild) when seam/protect/select state changes.
 */

import React, { useCallback, useEffect, useRef } from 'react';
import * as THREE from 'three';
import type { EdgeGeometry } from '@shared/contracts';
import {
  applyOrbitToCamera,
  makeOrbitState,
  orbitDrag,
  panDrag,
  resetOrbitState,
  zoomWheel,
} from '../viewport/orbitCamera';
import { useT } from '../i18n';

export interface OverlayToggles {
  showSeams: boolean;
  showProtected: boolean;
  showWire: boolean;
  showDraft: boolean;
}

export interface SeamViewportProps {
  geometry: EdgeGeometry | null;
  seams: Set<number>;
  protectedEdges: Set<number>;
  selected: Set<number>;
  invalid: Set<number>;
  conflict: Set<number>;
  draft: Set<number>;
  overlay: OverlayToggles;
  tolerancePx: number;
  onHover: (id: number | null) => void;
  onPick: (id: number | null, additive: boolean) => void;
}

// Edge colors per display state (plan §8: seam/protect/selected/invalid all distinct).
const COL = {
  normal: new THREE.Color(0x565b68),
  hidden: new THREE.Color(0x1e1f24), // == --bg, blends away when wire is off
  seam: new THREE.Color(0xff5a4d),
  protectedEdge: new THREE.Color(0x5b8cff),
  selected: new THREE.Color(0xffe14d),
  hovered: new THREE.Color(0xffffff),
  invalid: new THREE.Color(0xff3df0),
  conflict: new THREE.Color(0xff9d3d),
  draft: new THREE.Color(0x4fd17a),
};

interface Baked {
  /** Flat xyz for both endpoints of every edge, normalized to a unit sphere. */
  positions: Float32Array;
  /** Per-edge endpoint world vectors (reused each projection to avoid GC). */
  a: THREE.Vector3[];
  b: THREE.Vector3[];
  edgeIds: number[];
}

export function SeamViewport(props: SeamViewportProps): JSX.Element {
  const t = useT();
  const mountRef = useRef<HTMLDivElement | null>(null);
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const sceneRef = useRef<THREE.Scene | null>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const linesRef = useRef<THREE.LineSegments | null>(null);
  const bakedRef = useRef<Baked | null>(null);
  const hoveredRef = useRef<number | null>(null);

  // Orbit state (spherical around target) — shared module (viewer plan §2.2).
  const orbit = useRef(makeOrbitState());
  const drag = useRef<{ x: number; y: number; button: number; moved: boolean } | null>(null);

  const renderFrame = useCallback(() => {
    const r = rendererRef.current,
      s = sceneRef.current,
      c = cameraRef.current;
    if (r && s && c) r.render(s, c);
  }, []);

  const applyCamera = useCallback(() => {
    const c = cameraRef.current;
    if (!c) return;
    applyOrbitToCamera(c, orbit.current);
  }, []);

  // --- recolor every edge in place (no geometry rebuild) -----------------
  const recolor = useCallback(() => {
    const lines = linesRef.current;
    const baked = bakedRef.current;
    if (!lines || !baked) return;
    const colorAttr = lines.geometry.getAttribute('color') as THREE.BufferAttribute;
    const { seams, protectedEdges, selected, invalid, conflict, draft, overlay } = props;
    const hovered = hoveredRef.current;
    const tmp = new THREE.Color();
    for (let i = 0; i < baked.edgeIds.length; i++) {
      const id = baked.edgeIds[i];
      let col: THREE.Color;
      if (hovered === id) col = COL.hovered;
      else if (selected.has(id)) col = COL.selected;
      else if (conflict.has(id)) col = COL.conflict;
      else if (invalid.has(id)) col = COL.invalid;
      else if (overlay.showSeams && seams.has(id)) col = COL.seam;
      else if (overlay.showProtected && protectedEdges.has(id)) col = COL.protectedEdge;
      else if (overlay.showDraft && draft.has(id)) col = COL.draft;
      else col = overlay.showWire ? COL.normal : COL.hidden;
      tmp.copy(col);
      colorAttr.setXYZ(i * 2, tmp.r, tmp.g, tmp.b);
      colorAttr.setXYZ(i * 2 + 1, tmp.r, tmp.g, tmp.b);
    }
    colorAttr.needsUpdate = true;
    renderFrame();
  }, [props, renderFrame]);

  // --- one-time renderer/scene/camera setup ------------------------------
  useEffect(() => {
    const mount = mountRef.current;
    if (!mount) return;
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(mount.clientWidth || 600, mount.clientHeight || 400);
    mount.appendChild(renderer.domElement);
    renderer.domElement.style.display = 'block';
    renderer.domElement.style.width = '100%';
    renderer.domElement.style.height = '100%';

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(
      50,
      (mount.clientWidth || 600) / (mount.clientHeight || 400),
      0.01,
      100,
    );

    rendererRef.current = renderer;
    sceneRef.current = scene;
    cameraRef.current = camera;
    applyCamera();

    const ro = new ResizeObserver(() => {
      const w = mount.clientWidth || 600;
      const h = mount.clientHeight || 400;
      renderer.setSize(w, h);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      renderFrame();
    });
    ro.observe(mount);

    return () => {
      ro.disconnect();
      renderer.dispose();
      if (renderer.domElement.parentElement === mount) mount.removeChild(renderer.domElement);
      rendererRef.current = null;
      sceneRef.current = null;
      cameraRef.current = null;
    };
  }, [applyCamera, renderFrame]);

  // --- (re)build line geometry when the edge geometry changes ------------
  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;
    if (linesRef.current) {
      scene.remove(linesRef.current);
      linesRef.current.geometry.dispose();
      (linesRef.current.material as THREE.Material).dispose();
      linesRef.current = null;
      bakedRef.current = null;
    }
    const geo = props.geometry;
    if (!geo || geo.edges.length === 0) {
      renderFrame();
      return;
    }

    // Normalize the model to a unit sphere at the origin so the camera framing
    // and orbit are scale-independent; bake the transform into the positions so
    // world space == baked space (the projection hit-test needs no model matrix).
    const center = new THREE.Vector3();
    for (const v of geo.vertices) center.add(new THREE.Vector3(v.co[0], v.co[1], v.co[2]));
    if (geo.vertices.length) center.multiplyScalar(1 / geo.vertices.length);
    let radius = 1e-6;
    for (const v of geo.vertices) {
      const d = Math.hypot(v.co[0] - center.x, v.co[1] - center.y, v.co[2] - center.z);
      if (d > radius) radius = d;
    }
    const scale = 1 / radius;
    const baked = (id: [number, number, number]): [number, number, number] => [
      (id[0] - center.x) * scale,
      (id[1] - center.y) * scale,
      (id[2] - center.z) * scale,
    ];

    const n = geo.edges.length;
    const positions = new Float32Array(n * 2 * 3);
    const colors = new Float32Array(n * 2 * 3).fill(0.4);
    const av: THREE.Vector3[] = [];
    const bv: THREE.Vector3[] = [];
    const edgeIds: number[] = [];
    for (let i = 0; i < n; i++) {
      const e = geo.edges[i];
      const v0 = geo.vertices[e.vertex_ids[0]];
      const v1 = geo.vertices[e.vertex_ids[1]];
      const p0 = baked(v0.co);
      const p1 = baked(v1.co);
      positions.set(p0, i * 6);
      positions.set(p1, i * 6 + 3);
      av.push(new THREE.Vector3(p0[0], p0[1], p0[2]));
      bv.push(new THREE.Vector3(p1[0], p1[1], p1[2]));
      edgeIds.push(e.id);
    }

    const bufferGeo = new THREE.BufferGeometry();
    bufferGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    bufferGeo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
    const material = new THREE.LineBasicMaterial({ vertexColors: true, depthTest: false });
    const lines = new THREE.LineSegments(bufferGeo, material);
    lines.renderOrder = 1;
    scene.add(lines);

    linesRef.current = lines;
    bakedRef.current = { positions, a: av, b: bv, edgeIds };

    // Frame the (now unit) model.
    orbit.current.radius = 3;
    orbit.current.target.set(0, 0, 0);
    applyCamera();
    recolor();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.geometry]);

  // Recolor whenever any state set / overlay toggle changes.
  useEffect(() => {
    recolor();
  }, [recolor]);

  // --- screen-space ray-to-segment hit testing ---------------------------
  const pickEdgeAt = useCallback((px: number, py: number): number | null => {
    const baked = bakedRef.current;
    const camera = cameraRef.current;
    const renderer = rendererRef.current;
    if (!baked || !camera || !renderer) return null;
    const size = new THREE.Vector2();
    renderer.getSize(size);
    const w = size.x,
      h = size.y;
    const pa = new THREE.Vector3();
    const pb = new THREE.Vector3();
    let best = props.tolerancePx;
    let bestId: number | null = null;
    for (let i = 0; i < baked.edgeIds.length; i++) {
      pa.copy(baked.a[i]).project(camera);
      pb.copy(baked.b[i]).project(camera);
      if (pa.z > 1 || pb.z > 1) continue; // behind camera / clipped
      const ax = (pa.x * 0.5 + 0.5) * w;
      const ay = (1 - (pa.y * 0.5 + 0.5)) * h;
      const bx = (pb.x * 0.5 + 0.5) * w;
      const by = (1 - (pb.y * 0.5 + 0.5)) * h;
      const d = distToSegment(px, py, ax, ay, bx, by);
      if (d < best) {
        best = d;
        bestId = baked.edgeIds[i];
      }
    }
    return bestId;
  }, [props.tolerancePx]);

  const setHovered = useCallback(
    (id: number | null) => {
      if (hoveredRef.current === id) return;
      hoveredRef.current = id;
      props.onHover(id);
      recolor();
    },
    [props, recolor],
  );

  // --- pointer interaction ------------------------------------------------
  const onPointerDown = (e: React.PointerEvent) => {
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    drag.current = { x: e.clientX, y: e.clientY, button: e.button, moved: false };
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const mount = mountRef.current;
    if (!mount) return;
    const rect = mount.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;

    if (drag.current) {
      const dx = e.clientX - drag.current.x;
      const dy = e.clientY - drag.current.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.current.moved = true;
      drag.current.x = e.clientX;
      drag.current.y = e.clientY;
      const pan = drag.current.button === 2 || e.shiftKey;
      if (pan) {
        const camera = cameraRef.current;
        if (camera) panDrag(orbit.current, camera, dx, dy);
      } else {
        orbitDrag(orbit.current, dx, dy);
      }
      applyCamera();
      renderFrame();
      return;
    }
    // Hover only when not dragging.
    setHovered(pickEdgeAt(px, py));
  };

  const onPointerUp = (e: React.PointerEvent) => {
    const d = drag.current;
    drag.current = null;
    if (!d) return;
    if (!d.moved && d.button === 0) {
      // A click (no orbit) selects the hovered edge.
      const mount = mountRef.current;
      if (!mount) return;
      const rect = mount.getBoundingClientRect();
      const id = pickEdgeAt(e.clientX - rect.left, e.clientY - rect.top);
      props.onPick(id, e.shiftKey || e.ctrlKey || e.metaKey);
    }
  };

  const onWheel = (e: React.WheelEvent) => {
    zoomWheel(orbit.current, e.deltaY);
    applyCamera();
    renderFrame();
  };

  const resetView = () => {
    resetOrbitState(orbit.current);
    applyCamera();
    renderFrame();
  };

  return (
    <div className="seam-viewport">
      <div
        ref={mountRef}
        className="seam-viewport-canvas"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={() => setHovered(null)}
        onWheel={onWheel}
        onContextMenu={(e) => e.preventDefault()}
      />
      {!props.geometry && (
        <div className="seam-viewport-empty">{t('seam.viewportEmpty')}</div>
      )}
      <div className="seam-viewport-ctl">
        <button onClick={resetView} title={t('seam.resetCamera')}>{t('seam.resetView')}</button>
        <span className="muted small">{t('seam.viewportHelp')}</span>
      </div>
    </div>
  );
}

// --- geometry helpers ------------------------------------------------------
/** Pixel distance from point (px,py) to segment (ax,ay)-(bx,by). */
function distToSegment(px: number, py: number, ax: number, ay: number, bx: number, by: number): number {
  const vx = bx - ax;
  const vy = by - ay;
  const wx = px - ax;
  const wy = py - ay;
  const c1 = vx * wx + vy * wy;
  if (c1 <= 0) return Math.hypot(px - ax, py - ay);
  const c2 = vx * vx + vy * vy;
  if (c2 <= c1) return Math.hypot(px - bx, py - by);
  const t = c1 / c2;
  return Math.hypot(px - (ax + t * vx), py - (ay + t * vy));
}
