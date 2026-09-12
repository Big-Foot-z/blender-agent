/**
 * Blender detection + supported-version gate tests (gate G9).
 *
 * Covers the pure pieces without a real Blender: `--version` parsing, the
 * classification codes with an injected runner, the Windows candidate ordering
 * against a fake `Blender Foundation` tree, and the generate runner refusing to
 * create a run directory for an unsupported Blender.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  BLENDER_MIN_VERSION,
  BLENDER_TESTED_VERSION,
  checkBlenderVersion,
  clearBlenderVersionCache,
  parseBlenderVersion,
  windowsBlenderCandidates,
} from '../electron/main/blenderVersion';
import { createProject } from '../electron/main/project-service';
import { UvGenerateRunner } from '../electron/main/uvGenerate';

function workerRoot(): string {
  return join(__dirname, '..', '..', 'worker');
}

// --- parseBlenderVersion ---------------------------------------------------

test('parseBlenderVersion: version + build hash, bare version, garbage', () => {
  const full = parseBlenderVersion('Blender 5.1.2\n\tbuild hash: abc');
  assert.ok(full);
  assert.equal(full!.version, '5.1.2');
  assert.equal(full!.major, 5);
  assert.equal(full!.minor, 1);
  assert.equal(full!.build_hash, 'abc');

  const bare = parseBlenderVersion('Blender 3.6.0');
  assert.ok(bare);
  assert.equal(bare!.version, '3.6.0');
  assert.equal(bare!.major, 3);
  assert.equal(bare!.minor, 6);
  assert.equal(bare!.build_hash, null);

  assert.equal(parseBlenderVersion('not a blender at all'), null);
});

// --- checkBlenderVersion ---------------------------------------------------

function fakeExe(name: string): string {
  const dir = mkdtempSync(join(tmpdir(), 'blvers-'));
  const exe = join(dir, name);
  writeFileSync(exe, 'FAKE');
  return exe;
}

test('checkBlenderVersion: ok / unsupported / unreadable / not_found', () => {
  clearBlenderVersionCache();

  const okExe = fakeExe('blender-ok.exe');
  const ok = checkBlenderVersion(okExe, () => 'Blender 5.1.2\n\tbuild hash: deadbeef');
  assert.equal(ok.ok, true);
  assert.equal(ok.code, null);
  assert.equal(ok.version, '5.1.2');
  assert.equal(ok.build_hash, 'deadbeef');
  assert.equal(ok.min_version, BLENDER_MIN_VERSION);
  assert.equal(ok.tested_version, BLENDER_TESTED_VERSION);

  const oldExe = fakeExe('blender-old.exe');
  const old = checkBlenderVersion(oldExe, () => 'Blender 3.6.0');
  assert.equal(old.ok, false);
  assert.equal(old.code, 'blender_version_unsupported');
  assert.equal(old.version, '3.6.0');

  const brokenExe = fakeExe('blender-broken.exe');
  const broken = checkBlenderVersion(brokenExe, () => {
    throw new Error('spawn failed');
  });
  assert.equal(broken.ok, false);
  assert.equal(broken.code, 'blender_version_unreadable');
  assert.equal(broken.version, null);

  const missing = checkBlenderVersion(join(tmpdir(), 'no-such-blender-xyz.exe'), () => 'Blender 5.1.2');
  assert.equal(missing.ok, false);
  assert.equal(missing.code, 'blender_not_found');

  clearBlenderVersionCache();
});

// --- windowsBlenderCandidates ---------------------------------------------

test('windowsBlenderCandidates: versioned installs sort newest first', () => {
  const root = mkdtempSync(join(tmpdir(), 'blroot-'));
  for (const name of ['Blender 4.2', 'Blender 5.1', 'Blender']) {
    const dir = join(root, 'Blender Foundation', name);
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, 'blender.exe'), 'FAKE');
  }
  const candidates = windowsBlenderCandidates([root]);
  assert.equal(candidates.length, 3);
  assert.equal(candidates[0], join(root, 'Blender Foundation', 'Blender 5.1', 'blender.exe'));
  assert.equal(candidates[1], join(root, 'Blender Foundation', 'Blender 4.2', 'blender.exe'));
  assert.equal(candidates[2], join(root, 'Blender Foundation', 'Blender', 'blender.exe'));

  // A missing programFiles root is skipped, not fatal.
  assert.deepEqual(windowsBlenderCandidates([join(root, 'nope')]), []);
});

// --- generate start() refuses an unsupported Blender -----------------------

test('uv generate start: unsupported Blender throws and leaves no run dir', () => {
  const root = mkdtempSync(join(tmpdir(), 'uvproj-g9-'));
  const srcDir = mkdtempSync(join(tmpdir(), 'uvsrc-g9-'));
  const sourcePath = join(srcDir, 'SM_Test_Pottery_a_02.fbx');
  writeFileSync(sourcePath, 'FAKE-FBX-CONTENT');
  const project = createProject({ root, name: 'g9_version_gate', sourcePath, role: 'lowpoly' });

  const runner = new UvGenerateRunner({
    blenderPath: join(tmpdir(), 'pretend-blender.exe'),
    workerRoot: workerRoot(),
    mock: false,
    versionChecker: () => ({
      ok: false,
      version: '3.6.0',
      build_hash: null,
      min_version: BLENDER_MIN_VERSION,
      tested_version: BLENDER_TESTED_VERSION,
      code: 'blender_version_unsupported',
      message: 'Blender 3.6.0 is not supported',
    }),
  });

  assert.throws(
    () => runner.start(project.id, project.dir!, {}),
    (err: NodeJS.ErrnoException) => err.code === 'blender_version_unsupported',
  );

  const runsRoot = join(project.dir!, 'runs');
  assert.ok(existsSync(runsRoot));
  assert.deepEqual(readdirSync(runsRoot), [], 'no run directory was created');
});
