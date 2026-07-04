/**
 * Blender bridge client unit tests (bridge plan §5).
 *
 * Covers the pure pieces without Blender: handshake parsing, JSON-line framing
 * + token echo against a mock TCP server, status mapping, refresh target
 * resolution, and the launcher's working-model preference order.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer, type Server } from 'node:net';
import { mkdtempSync, writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  parseHandshake,
  readHandshake,
  bridgeRequest,
  bridgeStatus,
  bridgeRefresh,
  refreshTargets,
  type BridgeHandshake,
} from '../electron/main/blenderBridge';
import { resolveBlenderOpenTarget } from '../electron/main/blenderLauncher';
import { createProject, approveLowpoly, ensureRunDir } from '../electron/main/project-service';

// --- helpers ---------------------------------------------------------------

function mockBridge(
  handler: (req: Record<string, unknown>) => Record<string, unknown>,
): Promise<{ server: Server; handshake: BridgeHandshake; close: () => void }> {
  const token = 'test-token';
  return new Promise((resolve) => {
    const server = createServer((conn) => {
      let buf = '';
      conn.on('data', (chunk) => {
        buf += chunk.toString('utf-8');
        const nl = buf.indexOf('\n');
        if (nl < 0) return;
        const req = JSON.parse(buf.slice(0, nl)) as Record<string, unknown>;
        const res = req['token'] === token ? handler(req) : { ok: false, reason: 'bad_token' };
        conn.write(JSON.stringify(res) + '\n');
        conn.end();
      });
    });
    server.listen(0, '127.0.0.1', () => {
      const port = (server.address() as { port: number }).port;
      resolve({
        server,
        handshake: { port, token },
        close: () => server.close(),
      });
    });
  });
}

function makeHandshakeFile(handshake: BridgeHandshake): string {
  const dir = mkdtempSync(join(tmpdir(), 'bridge-'));
  const path = join(dir, 'bridge.json');
  writeFileSync(path, JSON.stringify(handshake));
  return path;
}

function makeApprovedProject(): string {
  const srcDir = mkdtempSync(join(tmpdir(), 'bridgesrc-'));
  const src = join(srcDir, 'model.fbx');
  writeFileSync(src, 'FAKE-FBX');
  const root = mkdtempSync(join(tmpdir(), 'bridgeproj-'));
  const project = createProject({ root, name: 'p', sourcePath: src });
  const dir = project.dir as string;
  const runId = 'run_test';
  const runDir = ensureRunDir(dir, runId);
  writeFileSync(join(runDir, 'lowpoly.blend'), 'FAKE-BLEND');
  writeFileSync(join(runDir, 'lowpoly.fbx'), 'FAKE-FBX-LOW');
  approveLowpoly(dir, runId);
  return dir;
}

// --- handshake parsing -------------------------------------------------------

test('parseHandshake accepts a valid doc and rejects malformed ones', () => {
  assert.deepEqual(parseHandshake('{"port": 43717, "token": "abc"}'), {
    port: 43717,
    token: 'abc',
  });
  assert.equal(parseHandshake('not json'), null);
  assert.equal(parseHandshake('{"port": "43717"}'), null); // wrong type
  assert.equal(parseHandshake('{"token": "abc"}'), null); // missing port
});

test('readHandshake returns null for a missing file', () => {
  assert.equal(readHandshake(join(tmpdir(), 'nope', 'bridge.json')), null);
});

// --- request framing + token -------------------------------------------------

test('bridgeRequest sends one JSON line with the token and parses the reply', async () => {
  const { handshake, close } = await mockBridge((req) => ({
    ok: true,
    echo: req['cmd'],
  }));
  try {
    const res = await bridgeRequest(handshake, { cmd: 'ping' });
    assert.deepEqual(res, { ok: true, echo: 'ping' });
  } finally {
    close();
  }
});

test('bridgeRequest is rejected by a token-checking server', async () => {
  const { handshake, close } = await mockBridge(() => ({ ok: true }));
  try {
    const res = await bridgeRequest({ ...handshake, token: 'wrong' }, { cmd: 'ping' });
    assert.equal(res['ok'], false);
    assert.equal(res['reason'], 'bad_token');
  } finally {
    close();
  }
});

test('bridgeRequest rejects when nothing listens', async () => {
  await assert.rejects(
    bridgeRequest({ port: 1, token: 't' }, { cmd: 'ping' }, 2000),
  );
});

// --- status ------------------------------------------------------------------

test('bridgeStatus: no handshake file -> not connected', async () => {
  const status = await bridgeStatus(join(tmpdir(), 'nope', 'bridge.json'));
  assert.deepEqual(status, { connected: false, reason: 'no_handshake' });
});

test('bridgeStatus: ping ok -> connected with file/dirty', async () => {
  const { handshake, close } = await mockBridge(() => ({
    ok: true,
    file: '/tmp/x.blend',
    dirty: true,
  }));
  try {
    const status = await bridgeStatus(makeHandshakeFile(handshake));
    assert.deepEqual(status, { connected: true, file: '/tmp/x.blend', dirty: true });
  } finally {
    close();
  }
});

test('bridgeStatus: unreachable server -> not connected', async () => {
  const path = makeHandshakeFile({ port: 1, token: 't' });
  const status = await bridgeStatus(path);
  assert.equal(status.connected, false);
  assert.ok(status.reason?.startsWith('unreachable'));
});

// --- refresh target resolution ------------------------------------------------

test('refreshTargets resolves the approved working blend + fbx', () => {
  const dir = makeApprovedProject();
  const targets = refreshTargets(dir);
  assert.equal(targets.blend_path, join(dir, 'work', 'working_lowpoly.blend'));
  assert.equal(targets.fbx_path, join(dir, 'work', 'working_lowpoly.fbx'));
});

test('bridgeRefresh sends absolute working paths and maps dirty holds', async () => {
  const dir = makeApprovedProject();
  let seen: Record<string, unknown> | null = null;
  const { handshake, close } = await mockBridge((req) => {
    seen = req;
    return { ok: false, reason: 'dirty' };
  });
  try {
    const res = await bridgeRefresh(dir, makeHandshakeFile(handshake));
    assert.deepEqual(res, { ok: false, mode: undefined, reason: 'dirty' });
    assert.ok(seen);
    assert.equal(seen!['cmd'], 'refresh');
    assert.equal(seen!['blend_path'], join(dir, 'work', 'working_lowpoly.blend'));
    assert.equal(seen!['fbx_path'], join(dir, 'work', 'working_lowpoly.fbx'));
  } finally {
    close();
  }
});

test('bridgeRefresh without a working model reports no_working_model', async () => {
  const srcDir = mkdtempSync(join(tmpdir(), 'bridgesrc-'));
  const src = join(srcDir, 'model.fbx');
  writeFileSync(src, 'FAKE');
  const root = mkdtempSync(join(tmpdir(), 'bridgeproj-'));
  const project = createProject({ root, name: 'p', sourcePath: src });
  const { handshake, close } = await mockBridge(() => ({ ok: true }));
  try {
    const res = await bridgeRefresh(project.dir as string, makeHandshakeFile(handshake));
    assert.deepEqual(res, { ok: false, reason: 'no_working_model' });
  } finally {
    close();
  }
});

// --- launcher target preference -------------------------------------------------

test('resolveBlenderOpenTarget prefers .blend, falls back to .fbx, errors when none', () => {
  const dir = makeApprovedProject();
  assert.equal(resolveBlenderOpenTarget(dir), join(dir, 'work', 'working_lowpoly.blend'));

  // Unapproved project has no working model.
  const srcDir = mkdtempSync(join(tmpdir(), 'bridgesrc-'));
  const src = join(srcDir, 'model.fbx');
  writeFileSync(src, 'FAKE');
  const root = mkdtempSync(join(tmpdir(), 'bridgeproj-'));
  const project = createProject({ root, name: 'p', sourcePath: src });
  assert.throws(() => resolveBlenderOpenTarget(project.dir as string), /no approved working model/);

  // fbx-only fallback: fake a manifest with only working_model_fbx present.
  const fbxOnly = mkdtempSync(join(tmpdir(), 'bridgefbx-'));
  mkdirSync(join(fbxOnly, 'work'), { recursive: true });
  writeFileSync(join(fbxOnly, 'work', 'working_lowpoly.fbx'), 'FAKE');
  writeFileSync(
    join(fbxOnly, 'project.json'),
    JSON.stringify({
      schema_version: 1,
      id: 'project_x',
      name: 'x',
      created_at: '',
      updated_at: '',
      source_model: null,
      source_model_role: null,
      selected_object: null,
      working_model: join('work', 'missing.blend'),
      working_model_fbx: join('work', 'working_lowpoly.fbx'),
      approved_lowpoly_run_id: 'run_x',
      runs: [],
    }),
  );
  assert.equal(resolveBlenderOpenTarget(fbxOnly), join(fbxOnly, 'work', 'working_lowpoly.fbx'));
});
