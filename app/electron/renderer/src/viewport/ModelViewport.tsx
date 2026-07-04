/**
 * Shaded 3D model viewport for the Prepare workspace (3D viewer plan §2.2).
 *
 * Loads the run's `lowpoly.glb` over the existing `uvpreview://` protocol
 * (fetch -> arrayBuffer -> GLTFLoader.parse) and renders it with a neutral
 * MeshStandardMaterial, hemisphere + directional lights, grid floor and axes.
 * Camera is the shared Blender-like orbit/pan/zoom from `orbitCamera.ts`
 * (drag = orbit, Shift/right-drag = pan, wheel = zoom).
 *
 * The model is normalized to a unit sphere at the origin (same policy as
 * SeamViewport) so framing is scale-independent. Toggles: wireframe overlay,
 * flat/smooth shading, grid. HUD shows triangle/vertex counts.
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import * as THREE from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import {
  applyOrbitToCamera,
  makeOrbitState,
  orbitDrag,
  panDrag,
  resetOrbitState,
  zoomWheel,
} from './orbitCamera';
import { useT } from '../i18n';
import { previewUrl } from '../previewUrl';

const MODEL_COLOR = 0x9aa2b1;

export function ModelViewport(props: { glbPath: string | null }): JSX.Element {
  const t = useT();
  const mountRef = useRef<HTMLDivElement | null>(null);
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const sceneRef = useRef<THREE.Scene | null>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const modelGroupRef = useRef<THREE.Group | null>(null);
  const wireRef = useRef<THREE.Group | null>(null);
  const gridRef = useRef<THREE.GridHelper | null>(null);
  const axesRef = useRef<THREE.AxesHelper | null>(null);

  const orbit = useRef(makeOrbitState());
  const drag = useRef<{ x: number; y: number; button: number } | null>(null);

  const [showWire, setShowWire] = useState(false);
  const [flatShading, setFlatShading] = useState(false);
  const [showGrid, setShowGrid] = useState(true);
  const [counts, setCounts] = useState<{ tris: number; verts: number } | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

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

  // --- one-time renderer/scene/camera/lights setup ------------------------
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

    scene.add(new THREE.HemisphereLight(0xdfe6f5, 0x2a2c33, 1.0));
    const sun = new THREE.DirectionalLight(0xffffff, 1.6);
    sun.position.set(2.5, 4, 2);
    scene.add(sun);

    const grid = new THREE.GridHelper(4, 16, 0x3a3f4c, 0x2a2d36);
    grid.position.y = -1;
    scene.add(grid);
    const axes = new THREE.AxesHelper(1.4);
    axes.position.y = -1;
    scene.add(axes);
    gridRef.current = grid;
    axesRef.current = axes;

    rendererRef.current = renderer;
    sceneRef.current = scene;
    cameraRef.current = camera;
    applyCamera();
    renderFrame();

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
      disposeModel(modelGroupRef, wireRef, scene);
      grid.geometry.dispose();
      (grid.material as THREE.Material).dispose();
      axes.geometry.dispose();
      (axes.material as THREE.Material).dispose();
      renderer.dispose();
      if (renderer.domElement.parentElement === mount) mount.removeChild(renderer.domElement);
      rendererRef.current = null;
      sceneRef.current = null;
      cameraRef.current = null;
    };
  }, [applyCamera, renderFrame]);

  // --- (re)load the GLB when the path changes -----------------------------
  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;
    disposeModel(modelGroupRef, wireRef, scene);
    setCounts(null);
    setLoadError(null);
    if (!props.glbPath) {
      renderFrame();
      return;
    }
    let cancelled = false;
    setLoading(true);
    fetch(previewUrl(props.glbPath))
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.arrayBuffer();
      })
      .then(
        (buffer) =>
          new Promise<THREE.Group>((resolvePromise, reject) => {
            new GLTFLoader().parse(buffer, '', (gltf) => resolvePromise(gltf.scene), reject);
          }),
      )
      .then((model) => {
        if (cancelled || !sceneRef.current) return;
        const { group, wire, tris, verts } = prepareModel(model, flatShading);
        wire.visible = showWire;
        sceneRef.current.add(group);
        sceneRef.current.add(wire);
        modelGroupRef.current = group;
        wireRef.current = wire;
        setCounts({ tris, verts });
        resetOrbitState(orbit.current);
        applyCamera();
        renderFrame();
      })
      .catch((err) => {
        if (!cancelled) setLoadError(String((err as Error)?.message ?? err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // Shading/wire are applied in-place by the effects below; reload only on path change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.glbPath, renderFrame, applyCamera]);

  // --- toggles applied in place -------------------------------------------
  useEffect(() => {
    modelGroupRef.current?.traverse((o) => {
      if (o instanceof THREE.Mesh) {
        const m = o.material as THREE.MeshStandardMaterial;
        m.flatShading = flatShading;
        m.needsUpdate = true;
      }
    });
    renderFrame();
  }, [flatShading, renderFrame]);

  useEffect(() => {
    if (wireRef.current) wireRef.current.visible = showWire;
    renderFrame();
  }, [showWire, renderFrame]);

  useEffect(() => {
    if (gridRef.current) gridRef.current.visible = showGrid;
    if (axesRef.current) axesRef.current.visible = showGrid;
    renderFrame();
  }, [showGrid, renderFrame]);

  // --- pointer interaction (same scheme as SeamViewport) ------------------
  const onPointerDown = (e: React.PointerEvent) => {
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    drag.current = { x: e.clientX, y: e.clientY, button: e.button };
  };
  const onPointerMove = (e: React.PointerEvent) => {
    if (!drag.current) return;
    const dx = e.clientX - drag.current.x;
    const dy = e.clientY - drag.current.y;
    drag.current.x = e.clientX;
    drag.current.y = e.clientY;
    const camera = cameraRef.current;
    if (drag.current.button === 2 || e.shiftKey) {
      if (camera) panDrag(orbit.current, camera, dx, dy);
    } else {
      orbitDrag(orbit.current, dx, dy);
    }
    applyCamera();
    renderFrame();
  };
  const onPointerUp = () => {
    drag.current = null;
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
    <div className="model-viewport">
      <div
        ref={mountRef}
        className="model-viewport-canvas"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onWheel={onWheel}
        onContextMenu={(e) => e.preventDefault()}
      />
      {!props.glbPath && <div className="model-viewport-empty">{t('viewport.empty')}</div>}
      {loading && <div className="model-viewport-empty">{t('viewport.loading')}</div>}
      {loadError && (
        <div className="model-viewport-empty err">{t('viewport.loadError', { err: loadError })}</div>
      )}
      <div className="model-viewport-ctl">
        <button onClick={resetView} title={t('seam.resetCamera')}>{t('seam.resetView')}</button>
        <label className="check">
          <input type="checkbox" checked={showWire} onChange={(e) => setShowWire(e.target.checked)} />
          {t('viewport.wireframe')}
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={flatShading}
            onChange={(e) => setFlatShading(e.target.checked)}
          />
          {t('viewport.flatShading')}
        </label>
        <label className="check">
          <input type="checkbox" checked={showGrid} onChange={(e) => setShowGrid(e.target.checked)} />
          {t('viewport.grid')}
        </label>
        {counts && (
          <span className="muted small">
            {t('viewport.hudCounts', {
              tris: counts.tris.toLocaleString(),
              verts: counts.verts.toLocaleString(),
            })}
          </span>
        )}
        <span className="muted small">{t('seam.viewportHelp')}</span>
      </div>
    </div>
  );
}

/**
 * Normalize the loaded scene to a unit sphere at the origin, swap every mesh's
 * material for the neutral viewer material, and build the wireframe overlay.
 */
function prepareModel(
  model: THREE.Group,
  flatShading: boolean,
): { group: THREE.Group; wire: THREE.Group; tris: number; verts: number } {
  const group = new THREE.Group();
  group.add(model);

  const box = new THREE.Box3().setFromObject(model);
  const sphere = box.getBoundingSphere(new THREE.Sphere());
  const scale = 1 / Math.max(sphere.radius, 1e-6);
  group.scale.setScalar(scale);
  group.position.copy(sphere.center).multiplyScalar(-scale);

  const wire = new THREE.Group();
  wire.scale.copy(group.scale);
  wire.position.copy(group.position);
  wire.quaternion.copy(group.quaternion);

  let tris = 0;
  let verts = 0;
  model.updateMatrixWorld(true);
  model.traverse((o) => {
    if (!(o instanceof THREE.Mesh)) return;
    const geo = o.geometry as THREE.BufferGeometry;
    verts += geo.getAttribute('position')?.count ?? 0;
    tris += geo.index ? geo.index.count / 3 : (geo.getAttribute('position')?.count ?? 0) / 3;
    (o.material as THREE.Material)?.dispose?.();
    o.material = new THREE.MeshStandardMaterial({
      color: MODEL_COLOR,
      roughness: 0.8,
      metalness: 0.05,
      flatShading,
    });

    const seg = new THREE.LineSegments(
      new THREE.WireframeGeometry(geo),
      new THREE.LineBasicMaterial({ color: 0x1c1e24, transparent: true, opacity: 0.5 }),
    );
    // Bake the mesh's model-local transform into the overlay's placement.
    o.updateWorldMatrix(true, false);
    seg.applyMatrix4(o.matrixWorld);
    wire.add(seg);
  });

  return { group, wire, tris: Math.round(tris), verts };
}

function disposeModel(
  modelGroupRef: React.MutableRefObject<THREE.Group | null>,
  wireRef: React.MutableRefObject<THREE.Group | null>,
  scene: THREE.Scene,
): void {
  for (const ref of [modelGroupRef, wireRef]) {
    const g = ref.current;
    if (!g) continue;
    scene.remove(g);
    g.traverse((o) => {
      if (o instanceof THREE.Mesh || o instanceof THREE.LineSegments) {
        (o.geometry as THREE.BufferGeometry)?.dispose?.();
        const mat = o.material as THREE.Material | THREE.Material[];
        if (Array.isArray(mat)) mat.forEach((m) => m.dispose());
        else mat?.dispose?.();
      }
    });
    ref.current = null;
  }
}
