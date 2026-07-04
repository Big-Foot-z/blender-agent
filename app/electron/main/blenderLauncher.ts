/**
 * One-click "Open in Blender" (3D viewer + bridge plan §3).
 *
 * Resolves the approved working model — `.blend` preferred (opened directly as
 * Blender's file argument), else `.fbx` via the bundled `open_in_blender.py`
 * script (a script file, not `--python-expr`, so Windows quoting is a non-issue).
 * Pure Node (child_process/fs/path) so it is unit-testable without Electron.
 */

import { spawn } from 'child_process';
import { existsSync } from 'fs';
import { join } from 'path';
import type { BlenderOpenResult } from '@shared/contracts';
import { readProject } from './project-service';

/** Pick the model to open: working .blend first, then working .fbx (plan §3.1). */
export function resolveBlenderOpenTarget(projectDir: string): string {
  const project = readProject(projectDir);
  for (const rel of [project.working_model, project.working_model_fbx]) {
    if (rel && existsSync(join(projectDir, rel))) return join(projectDir, rel);
  }
  throw new Error('no approved working model (.blend/.fbx) to open — approve a low-poly first');
}

export function blenderOpen(opts: {
  projectDir: string;
  blenderPath: string | null;
  workerRoot: string;
}): BlenderOpenResult {
  const { projectDir, blenderPath, workerRoot } = opts;
  if (!blenderPath) {
    throw new Error('Blender path is not configured — set it in Settings first');
  }
  const target = resolveBlenderOpenTarget(projectDir);

  const args = target.toLowerCase().endsWith('.blend')
    ? [target]
    : ['--python', join(workerRoot, 'open_in_blender.py'), '--', target];

  const child = spawn(blenderPath, args, { detached: true, stdio: 'ignore' });
  child.unref();
  return { status: 'launched', target };
}

/**
 * Headless-install the bridge addon into the user's Blender prefs (plan §4.2).
 * `addon_install` + `addon_enable` + `save_userprefs` via the bundled script —
 * no per-version addons-directory guessing. Resolves with the combined log.
 */
export function installBridge(opts: {
  blenderPath: string | null;
  workerRoot: string;
}): Promise<{ status: string; log: string }> {
  const { blenderPath, workerRoot } = opts;
  if (!blenderPath) {
    return Promise.reject(new Error('Blender path is not configured — set it in Settings first'));
  }
  const script = join(workerRoot, 'install_bridge.py');
  const addon = join(workerRoot, 'addon', 'reforge_bridge.py');
  if (!existsSync(addon)) {
    return Promise.reject(new Error(`bridge addon not found: ${addon}`));
  }
  return new Promise((resolvePromise, reject) => {
    const child = spawn(blenderPath, ['--background', '--python', script, '--', addon]);
    let log = '';
    child.stdout.on('data', (d) => (log += d.toString()));
    child.stderr.on('data', (d) => (log += d.toString()));
    child.on('error', reject);
    child.on('close', (code) => {
      if (code === 0 && log.includes('install_bridge: installed')) {
        resolvePromise({ status: 'installed', log });
      } else {
        reject(new Error(`bridge install failed (exit ${code}):\n${log.slice(-2000)}`));
      }
    });
  });
}
