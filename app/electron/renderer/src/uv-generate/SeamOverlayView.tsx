/**
 * 3D seam overlay for the Generate review (work plan §7; gate G7).
 *
 * Draws `seam_overlay.json` edges as colored line segments — one color per seam
 * ORIGIN (mandatory_90 / user_seam / locked / welded_fold_auxiliary /
 * overlap_repair / distortion_split / correctness_repair / segmentation) so the
 * reviewer can tell why each cut exists, and picks an edge by screen-space
 * ray-to-segment distance (the same technique `SeamViewport` uses, plan §8/§14).
 *
 * `SeamViewport` itself is not reusable here: it consumes the worker's full
 * `EdgeGeometry` (vertices + vertex_ids), whereas the overlay artifact ships
 * per-edge endpoint coordinates only. So the proven camera math is shared via
 * `viewport/orbitCamera` and only the tiny baking/pick loop is re-stated.
 */

import React, { useCallback, useEffect, useRef } from 'react';
import * as THREE from 'three';
import type { RejectedCandidateEntry, SeamOverlayEdge } from '@shared/contracts';
import { SEAM_REASON_CODES } from '@shared/contracts';
import {
  applyOrbitToCamera,
  makeOrbitState,
  orbitDrag,
  panDrag,
  resetOrbitState,
  zoomWheel,
} from '../viewport/orbitCamera';
import { useT } from '../i18n';

/** Seam-origin palette; `other` catches an unknown/new type without crashing. */
export const SEAM_TYPE_COLORS: Record<string, string> = {
  mandatory_90: '#ff5a4d',
  user_seam: '#5b8cff',
  locked: '#ffe14d',
  welded_fold_auxiliary: '#4fd17a',
  overlap_repair: '#ff9d3d',
  distortion_split: '#c07bff',
  correctness_repair: '#ff3df0',
  segmentation: '#8a92a6',
  other: '#565b68',
};

export const SEAM_TYPE_ORDER = [
  'mandatory_90',
  'user_seam',
  'locked',
  'welded_fold_auxiliary',
  'overlap_repair',
  'distortion_split',
  'correctness_repair',
  'segmentation',
] as const;

export function seamTypeColor(type: string): string {
  return SEAM_TYPE_COLORS[type] ?? SEAM_TYPE_COLORS.other;
}

/**
 * Reason-code palette (gate G15). Seven fixed codes, so the reviewer reads
 * "why was this cut?" straight off the legend. `rejected_candidate` is drawn
 * dashed as well as magenta — it never shipped.
 */
export const REASON_CODE_COLORS: Record<string, string> = {
  mandatory_90: '#ff5a4d',
  boundary_topology: '#8a92a6',
  user: '#5b8cff',
  shading: '#c07bff',
  material: '#ff9d3d',
  distortion_added: '#4fd17a',
  rejected_candidate: '#ff3df0',
  other: '#565b68',
};

export const REASON_CODE_ORDER = SEAM_REASON_CODES;

export function reasonCodeColor(code: string | null | undefined): string {
  return REASON_CODE_COLORS[code ?? ''] ?? REASON_CODE_COLORS.other;
}

/** An overlay edge's color: its reason code when present, else its origin type. */
export function seamEdgeColor(edge: Pick<SeamOverlayEdge, 'type' | 'reason_code'>): string {
  return edge.reason_code ? reasonCodeColor(String(edge.reason_code)) : seamTypeColor(edge.type);
}

const PICK_TOLERANCE_PX = 8;

interface Baked {
  a: THREE.Vector3[];
  b: THREE.Vector3[];
  edgeIds: number[];
}

export function SeamOverlayView(props: {
  edges: SeamOverlayEdge[];
  /** Tried-and-dropped cuts, drawn dashed when `showRejected` (gate G15). */
  rejected?: RejectedCandidateEntry[] | null;
  showRejected?: boolean;
  selectedEdgeId: number | null;
  onPick: (edgeId: number | null) => void;
}): JSX.Element {
  const t = useT();
  const mountRef = useRef<HTMLDivElement | null>(null);
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const sceneRef = useRef<THREE.Scene | null>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const linesRef = useRef<THREE.LineSegments | null>(null);
  const rejectedLinesRef = useRef<THREE.LineSegments | null>(null);
  const bakedRef = useRef<Baked | null>(null);

  const orbit = useRef(makeOrbitState());
  const drag = useRef<{ x: number; y: number; button: number; moved: boolean } | null>(null);

  const renderFrame = useCallback(() => {
    const r = rendererRef.current;
    const s = sceneRef.current;
    const c = cameraRef.current;
    if (r && s && c) r.render(s, c);
  }, []);

  const applyCamera = useCallback(() => {
    const c = cameraRef.current;
    if (c) applyOrbitToCamera(c, orbit.current);
  }, []);

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

  // --- recolor in place (selection highlight) -----------------------------
  const recolor = useCallback(() => {
    const lines = linesRef.current;
    const baked = bakedRef.current;
    if (!lines || !baked) return;
    const colorAttr = lines.geometry.getAttribute('color') as THREE.BufferAttribute;
    const tmp = new THREE.Color();
    for (let i = 0; i < baked.edgeIds.length; i++) {
      const e = props.edges[i];
      const isSel = props.selectedEdgeId === baked.edgeIds[i];
      tmp.set(isSel ? '#ffffff' : e ? seamEdgeColor(e) : SEAM_TYPE_COLORS.other);
      colorAttr.setXYZ(i * 2, tmp.r, tmp.g, tmp.b);
      colorAttr.setXYZ(i * 2 + 1, tmp.r, tmp.g, tmp.b);
    }
    colorAttr.needsUpdate = true;
    renderFrame();
  }, [props.edges, props.selectedEdgeId, renderFrame]);

  // --- (re)build line geometry when the overlay changes -------------------
  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;
    for (const ref of [linesRef, rejectedLinesRef]) {
      if (ref.current) {
        scene.remove(ref.current);
        ref.current.geometry.dispose();
        (ref.current.material as THREE.Material).dispose();
        ref.current = null;
      }
    }
    bakedRef.current = null;
    const edges = props.edges;
    if (!edges.length) {
      renderFrame();
      return;
    }

    // Normalize to a unit sphere at the origin so orbit/zoom are scale-independent.
    const center = new THREE.Vector3();
    let n = 0;
    for (const e of edges) {
      center.x += e.a[0] + e.b[0];
      center.y += e.a[1] + e.b[1];
      center.z += e.a[2] + e.b[2];
      n += 2;
    }
    if (n) center.multiplyScalar(1 / n);
    let radius = 1e-6;
    for (const e of edges) {
      for (const p of [e.a, e.b]) {
        const d = Math.hypot(p[0] - center.x, p[1] - center.y, p[2] - center.z);
        if (d > radius) radius = d;
      }
    }
    const scale = 1 / radius;
    const bake = (p: [number, number, number]): [number, number, number] => [
      (p[0] - center.x) * scale,
      (p[1] - center.y) * scale,
      (p[2] - center.z) * scale,
    ];

    const positions = new Float32Array(edges.length * 6);
    const colors = new Float32Array(edges.length * 6).fill(0.4);
    const av: THREE.Vector3[] = [];
    const bv: THREE.Vector3[] = [];
    const edgeIds: number[] = [];
    for (let i = 0; i < edges.length; i++) {
      const p0 = bake(edges[i].a);
      const p1 = bake(edges[i].b);
      positions.set(p0, i * 6);
      positions.set(p1, i * 6 + 3);
      av.push(new THREE.Vector3(p0[0], p0[1], p0[2]));
      bv.push(new THREE.Vector3(p1[0], p1[1], p1[2]));
      edgeIds.push(edges[i].edge_id);
    }

    const bufferGeo = new THREE.BufferGeometry();
    bufferGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    bufferGeo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
    const material = new THREE.LineBasicMaterial({ vertexColors: true, depthTest: false });
    const lines = new THREE.LineSegments(bufferGeo, material);
    lines.renderOrder = 1;
    scene.add(lines);
    linesRef.current = lines;
    bakedRef.current = { a: av, b: bv, edgeIds };

    // Rejected candidates: same space, dashed, magenta — never shipped (G15).
    const rejected = props.showRejected ? props.rejected ?? [] : [];
    if (rejected.length) {
      const rp = new Float32Array(rejected.length * 6);
      for (let i = 0; i < rejected.length; i++) {
        rp.set(bake(rejected[i].a), i * 6);
        rp.set(bake(rejected[i].b), i * 6 + 3);
      }
      const rgeo = new THREE.BufferGeometry();
      rgeo.setAttribute('position', new THREE.BufferAttribute(rp, 3));
      const rmat = new THREE.LineDashedMaterial({
        color: REASON_CODE_COLORS.rejected_candidate,
        dashSize: 0.03,
        gapSize: 0.03,
        depthTest: false,
      });
      const rlines = new THREE.LineSegments(rgeo, rmat);
      rlines.computeLineDistances();
      rlines.renderOrder = 2;
      scene.add(rlines);
      rejectedLinesRef.current = rlines;
    }

    orbit.current.radius = 3;
    orbit.current.target.set(0, 0, 0);
    applyCamera();
    recolor();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.edges, props.rejected, props.showRejected]);

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
    const pa = new THREE.Vector3();
    const pb = new THREE.Vector3();
    let best = PICK_TOLERANCE_PX;
    let bestId: number | null = null;
    for (let i = 0; i < baked.edgeIds.length; i++) {
      pa.copy(baked.a[i]).project(camera);
      pb.copy(baked.b[i]).project(camera);
      if (pa.z > 1 || pb.z > 1) continue;
      const ax = (pa.x * 0.5 + 0.5) * size.x;
      const ay = (1 - (pa.y * 0.5 + 0.5)) * size.y;
      const bx = (pb.x * 0.5 + 0.5) * size.x;
      const by = (1 - (pb.y * 0.5 + 0.5)) * size.y;
      const d = distToSegment(px, py, ax, ay, bx, by);
      if (d < best) {
        best = d;
        bestId = baked.edgeIds[i];
      }
    }
    return bestId;
  }, []);

  const onPointerDown = (e: React.PointerEvent) => {
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    drag.current = { x: e.clientX, y: e.clientY, button: e.button, moved: false };
  };

  const onPointerMove = (e: React.PointerEvent) => {
    if (!drag.current) return;
    const dx = e.clientX - drag.current.x;
    const dy = e.clientY - drag.current.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.current.moved = true;
    drag.current.x = e.clientX;
    drag.current.y = e.clientY;
    if (drag.current.button === 2 || e.shiftKey) {
      const camera = cameraRef.current;
      if (camera) panDrag(orbit.current, camera, dx, dy);
    } else {
      orbitDrag(orbit.current, dx, dy);
    }
    applyCamera();
    renderFrame();
  };

  const onPointerUp = (e: React.PointerEvent) => {
    const d = drag.current;
    drag.current = null;
    if (!d || d.moved || d.button !== 0) return;
    const mount = mountRef.current;
    if (!mount) return;
    const rect = mount.getBoundingClientRect();
    props.onPick(pickEdgeAt(e.clientX - rect.left, e.clientY - rect.top));
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
    <div className="seam-overlay-view">
      <div
        ref={mountRef}
        className="seam-overlay-canvas"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onWheel={onWheel}
        onContextMenu={(e) => e.preventDefault()}
      />
      {props.edges.length === 0 && (
        <div className="seam-overlay-empty">{t('generate.noSeamOverlay')}</div>
      )}
      <div className="seam-overlay-ctl">
        <button onClick={resetView}>{t('seam.resetView')}</button>
        <span className="muted small">{t('generate.seamOverlayHelp')}</span>
      </div>
    </div>
  );
}

/** Pixel distance from point (px,py) to segment (ax,ay)-(bx,by). */
function distToSegment(
  px: number,
  py: number,
  ax: number,
  ay: number,
  bx: number,
  by: number,
): number {
  const vx = bx - ax;
  const vy = by - ay;
  const c1 = vx * (px - ax) + vy * (py - ay);
  if (c1 <= 0) return Math.hypot(px - ax, py - ay);
  const c2 = vx * vx + vy * vy;
  if (c2 <= c1) return Math.hypot(px - bx, py - by);
  const s = c1 / c2;
  return Math.hypot(px - (ax + s * vx), py - (ay + s * vy));
}
