/**
 * Persistent app settings (plan §5 "Blender executable path setting").
 *
 * Stores the Blender executable path and the projects root. Blender path detection
 * falls back to common install locations so first run can work without setup.
 */

import Store from 'electron-store';
import { homedir } from 'os';
import { join } from 'path';
import type { AppSettings } from '@shared/contracts';
import { detectBlenderPath } from './blenderVersion';

/** Gate G9: detection covers the common install paths plus the Windows
 *  `Blender Foundation\Blender X.Y` version directories (newest first). */
function detectBlender(): string | null {
  return detectBlenderPath();
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
