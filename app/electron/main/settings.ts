/**
 * Persistent app settings (plan §5 "Blender executable path setting").
 *
 * Stores the Blender executable path and the projects root. Blender path detection
 * falls back to common install locations so first run can work without setup.
 */

import Store from 'electron-store';
import { existsSync } from 'fs';
import { homedir } from 'os';
import { join } from 'path';
import type { AppSettings } from '@shared/contracts';

const COMMON_BLENDER_PATHS = [
  '/Applications/Blender.app/Contents/MacOS/Blender',
  '/usr/bin/blender',
  '/usr/local/bin/blender',
  'C:\\Program Files\\Blender Foundation\\Blender\\blender.exe',
];

function detectBlender(): string | null {
  for (const p of COMMON_BLENDER_PATHS) {
    if (existsSync(p)) return p;
  }
  return null;
}

const store = new Store<{ settings: AppSettings }>({
  defaults: {
    settings: {
      blenderPath: detectBlender(),
      projectsRoot: join(homedir(), 'UVReviewProjects'),
      autoRefreshBlender: true,
    },
  },
});

export function getSettings(): AppSettings {
  const s = store.get('settings');
  // Re-detect Blender each launch if it was never set.
  if (!s.blenderPath) {
    s.blenderPath = detectBlender();
  }
  // Pre-bridge stored settings lack the flag; default it on (bridge plan §4.3).
  if (s.autoRefreshBlender === undefined) {
    s.autoRefreshBlender = true;
  }
  return s;
}

export function setSettings(patch: Partial<AppSettings>): AppSettings {
  const next = { ...getSettings(), ...patch };
  store.set('settings', next);
  return next;
}
