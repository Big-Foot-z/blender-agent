/**
 * Blender executable detection + supported-version gate (gate G9).
 *
 * Pure Node (no `electron` import) so it is unit-testable: finds a Blender
 * install (Windows `Blender Foundation\Blender X.Y` directories preferred,
 * newest first), parses `blender --version`, and classifies the result against
 * the pinned supported range. Results are memoised per path so a session does
 * not re-spawn Blender for every start/settings read.
 */

import { execFileSync } from 'child_process';
import { existsSync, readdirSync } from 'fs';
import { join } from 'path';
import type { BlenderVersionCheck } from '@shared/contracts';

/** Oldest Blender the worker scripts are supported on. */
export const BLENDER_MIN_VERSION = '4.2';
/** Version the pipeline is actually tested/frozen against (gate G9). */
export const BLENDER_TESTED_VERSION = '5.1';

const MIN_MAJOR = 4;
const MIN_MINOR = 2;

/** Non-Windows fallbacks, kept in the historical probe order. */
const COMMON_BLENDER_PATHS = [
  '/Applications/Blender.app/Contents/MacOS/Blender',
  '/usr/bin/blender',
  '/usr/local/bin/blender',
  'C:\\Program Files\\Blender Foundation\\Blender\\blender.exe',
];

/** Sort key for a `Blender Foundation` child directory: an unversioned
 *  "Blender" folder sorts lowest so versioned installs win. */
function dirVersionKey(name: string): { major: number; minor: number } {
  const m = /^Blender\s+(\d+)\.(\d+)/.exec(name);
  if (!m) return { major: -1, minor: -1 };
  return { major: Number(m[1]), minor: Number(m[2]) };
}

/**
 * All `<programFiles>\Blender Foundation\Blender*\blender.exe` that exist,
 * newest version first.
 */
export function windowsBlenderCandidates(
  programFiles: string[] = ['C:\\Program Files', 'C:\\Program Files (x86)'],
): string[] {
  const found: { exe: string; major: number; minor: number }[] = [];
  for (const root of programFiles) {
    const foundation = join(root, 'Blender Foundation');
    let entries: string[];
    try {
      entries = readdirSync(foundation);
    } catch {
      continue;
    }
    for (const name of entries) {
      if (!name.startsWith('Blender')) continue;
      const exe = join(foundation, name, 'blender.exe');
      if (!existsSync(exe)) continue;
      const { major, minor } = dirVersionKey(name);
      found.push({ exe, major, minor });
    }
  }
  found.sort((a, b) => (b.major - a.major) || (b.minor - a.minor));
  return found.map((f) => f.exe);
}

/** Best-guess Blender executable for this platform, or null if none found. */
export function detectBlenderPath(platform: string = process.platform): string | null {
  if (platform === 'win32') {
    const candidates = windowsBlenderCandidates();
    return candidates[0] ?? null;
  }
  for (const p of COMMON_BLENDER_PATHS) {
    if (existsSync(p)) return p;
  }
  return null;
}

/** Parse the two lines Blender prints for `--version`. */
export function parseBlenderVersion(
  versionOutput: string,
): { version: string; major: number; minor: number; build_hash: string | null } | null {
  if (!versionOutput) return null;
  const m = /^\s*Blender\s+(\d+)\.(\d+)(?:\.(\d+))?/m.exec(versionOutput);
  if (!m) return null;
  const hash = /build hash:\s*(\S+)/i.exec(versionOutput);
  const version = m[3] !== undefined ? `${m[1]}.${m[2]}.${m[3]}` : `${m[1]}.${m[2]}`;
  return {
    version,
    major: Number(m[1]),
    minor: Number(m[2]),
    build_hash: hash ? hash[1] : null,
  };
}

const cache = new Map<string, BlenderVersionCheck>();

/** Drop the memoised results (called when the configured path changes). */
export function clearBlenderVersionCache(): void {
  cache.clear();
}

function defaultRunner(path: string): string {
  return execFileSync(path, ['--version'], { encoding: 'utf-8', timeout: 20000 });
}

/**
 * Classify the Blender at `blenderPath` against the supported range.
 * `runner` is injectable so tests never spawn a real Blender.
 */
export function checkBlenderVersion(
  blenderPath: string,
  runner: (path: string) => string = defaultRunner,
): BlenderVersionCheck {
  const cached = cache.get(blenderPath);
  if (cached) return cached;

  const base = {
    min_version: BLENDER_MIN_VERSION,
    tested_version: BLENDER_TESTED_VERSION,
  };
  let result: BlenderVersionCheck;

  if (!blenderPath || !existsSync(blenderPath)) {
    result = {
      ...base,
      ok: false,
      version: null,
      build_hash: null,
      code: 'blender_not_found',
      message: `Blender executable not found: ${blenderPath || '(not configured)'}`,
    };
  } else {
    let out: string;
    try {
      out = runner(blenderPath);
    } catch (err) {
      const res: BlenderVersionCheck = {
        ...base,
        ok: false,
        version: null,
        build_hash: null,
        code: 'blender_version_unreadable',
        message: `could not read the Blender version from ${blenderPath}: ${String(err)}`,
      };
      cache.set(blenderPath, res);
      return res;
    }
    const parsed = parseBlenderVersion(out);
    if (!parsed) {
      result = {
        ...base,
        ok: false,
        version: null,
        build_hash: null,
        code: 'blender_version_unreadable',
        message: `could not parse the Blender version output of ${blenderPath}`,
      };
    } else if (parsed.major < MIN_MAJOR || (parsed.major === MIN_MAJOR && parsed.minor < MIN_MINOR)) {
      result = {
        ...base,
        ok: false,
        version: parsed.version,
        build_hash: parsed.build_hash,
        code: 'blender_version_unsupported',
        message:
          `Blender ${parsed.version} is not supported — ` +
          `install Blender ${BLENDER_MIN_VERSION} or newer (tested: ${BLENDER_TESTED_VERSION})`,
      };
    } else {
      result = {
        ...base,
        ok: true,
        version: parsed.version,
        build_hash: parsed.build_hash,
        code: null,
        message: `Blender ${parsed.version} (supported; tested against ${BLENDER_TESTED_VERSION})`,
      };
    }
  }

  cache.set(blenderPath, result);
  return result;
}
