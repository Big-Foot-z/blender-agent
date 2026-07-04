/**
 * Shared orbit/pan/zoom camera math (3D viewer plan §2.2).
 *
 * Extracted verbatim from `SeamViewport` so the seam editor and the new
 * `ModelViewport` share one proven implementation (behaviour-invariant
 * refactor, plan §6). Pure Three.js math — no React, no DOM — so it is
 * trivially unit-testable.
 *
 * Controls (Blender-like): drag = orbit, Shift+drag / right-drag = pan,
 * wheel = zoom.
 */

import * as THREE from 'three';

export interface OrbitState {
  radius: number;
  theta: number;
  phi: number;
  target: THREE.Vector3;
}

export const ORBIT_DEFAULTS = { radius: 3, theta: 0.9, phi: 1.1 } as const;

export function makeOrbitState(): OrbitState {
  return { ...ORBIT_DEFAULTS, target: new THREE.Vector3() };
}

export function resetOrbitState(state: OrbitState): void {
  state.radius = ORBIT_DEFAULTS.radius;
  state.theta = ORBIT_DEFAULTS.theta;
  state.phi = ORBIT_DEFAULTS.phi;
  state.target.set(0, 0, 0);
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

/** Position + aim the camera from the spherical orbit state. */
export function applyOrbitToCamera(camera: THREE.Camera, state: OrbitState): void {
  const { radius, theta, phi, target } = state;
  const sinPhi = Math.sin(phi);
  camera.position.set(
    target.x + radius * sinPhi * Math.cos(theta),
    target.y + radius * Math.cos(phi),
    target.z + radius * sinPhi * Math.sin(theta),
  );
  camera.lookAt(target);
  camera.updateMatrixWorld();
}

/** Drag = orbit around the target (dx/dy in pixels). */
export function orbitDrag(state: OrbitState, dx: number, dy: number): void {
  state.theta -= dx * 0.01;
  state.phi = clamp(state.phi - dy * 0.01, 0.05, Math.PI - 0.05);
}

/** Shift/right drag = pan the target in the camera plane (dx/dy in pixels). */
export function panDrag(state: OrbitState, camera: THREE.Camera, dx: number, dy: number): void {
  const right = new THREE.Vector3();
  const up = new THREE.Vector3();
  camera.matrixWorld.extractBasis(right, up, new THREE.Vector3());
  const k = state.radius * 0.0015;
  state.target.addScaledVector(right, -dx * k);
  state.target.addScaledVector(up, dy * k);
}

/** Wheel = zoom (multiplicative, clamped). */
export function zoomWheel(state: OrbitState, deltaY: number): void {
  state.radius = clamp(state.radius * (1 + deltaY * 0.001), 0.4, 40);
}
