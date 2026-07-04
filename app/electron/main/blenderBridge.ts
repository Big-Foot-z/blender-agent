/**
 * Client for the `reforge_bridge` Blender addon (3D viewer + bridge plan §4.3).
 *
 * Discovery: the addon writes `~/.reforge/bridge.json` `{port, token, ...}` on
 * register; every request echoes the token (localhost port-hijack protection).
 * Protocol: one JSON object per line over a short-lived TCP connection.
 *
 * Pure Node (net/fs/path) — no `electron` import — so the framing/handshake
 * logic is unit-testable against a mock TCP server (plan §5).
 */

import { readFileSync, existsSync } from 'fs';
import { homedir } from 'os';
import { join } from 'path';
import { connect } from 'net';
import type { BridgeRefreshResult, BridgeStatus } from '@shared/contracts';
import { readProject } from './project-service';

export interface BridgeHandshake {
  port: number;
  token: string;
  blender_version?: string;
  pid?: number;
}

export function handshakePath(): string {
  return join(homedir(), '.reforge', 'bridge.json');
}

/** Parse a handshake document; null when missing/unusable (stale files are
 *  handled later by the connection failing, not here). */
export function parseHandshake(raw: string): BridgeHandshake | null {
  try {
    const doc = JSON.parse(raw) as Partial<BridgeHandshake>;
    if (typeof doc.port !== 'number' || typeof doc.token !== 'string') return null;
    return doc as BridgeHandshake;
  } catch {
    return null;
  }
}

export function readHandshake(path = handshakePath()): BridgeHandshake | null {
  if (!existsSync(path)) return null;
  try {
    return parseHandshake(readFileSync(path, 'utf-8'));
  } catch {
    return null;
  }
}

/** Send one JSON-line request and await the single JSON-line response. */
export function bridgeRequest(
  handshake: BridgeHandshake,
  request: Record<string, unknown>,
  timeoutMs = 15_000,
): Promise<Record<string, unknown>> {
  return new Promise((resolvePromise, reject) => {
    const socket = connect({ host: '127.0.0.1', port: handshake.port });
    let buf = '';
    let settled = false;
    const finish = (fn: () => void) => {
      if (settled) return;
      settled = true;
      socket.destroy();
      fn();
    };
    socket.setTimeout(timeoutMs, () => finish(() => reject(new Error('bridge timeout'))));
    socket.on('error', (err) => finish(() => reject(err)));
    socket.on('connect', () => {
      socket.write(JSON.stringify({ ...request, token: handshake.token }) + '\n');
    });
    socket.on('data', (chunk) => {
      buf += chunk.toString('utf-8');
      const nl = buf.indexOf('\n');
      if (nl < 0) return;
      const line = buf.slice(0, nl);
      try {
        finish(() => resolvePromise(JSON.parse(line) as Record<string, unknown>));
      } catch {
        finish(() => reject(new Error('bridge sent malformed JSON')));
      }
    });
    socket.on('close', () => finish(() => reject(new Error('bridge closed connection'))));
  });
}

/** Handshake file + `ping` -> connection status for the UI indicator. */
export async function bridgeStatus(path = handshakePath()): Promise<BridgeStatus> {
  const handshake = readHandshake(path);
  if (!handshake) return { connected: false, reason: 'no_handshake' };
  try {
    const res = await bridgeRequest(handshake, { cmd: 'ping' }, 3_000);
    if (!res['ok']) return { connected: false, reason: String(res['reason'] ?? 'ping_failed') };
    return {
      connected: true,
      file: (res['file'] as string | undefined) ?? null,
      dirty: !!res['dirty'],
    };
  } catch (err) {
    return { connected: false, reason: `unreachable: ${(err as Error).message}` };
  }
}

/** The absolute working-model paths a `refresh` command targets (plan §4.1). */
export function refreshTargets(projectDir: string): {
  blend_path: string | null;
  fbx_path: string | null;
} {
  const project = readProject(projectDir);
  const abs = (rel: string | null | undefined): string | null =>
    rel && existsSync(join(projectDir, rel)) ? join(projectDir, rel) : null;
  return { blend_path: abs(project.working_model), fbx_path: abs(project.working_model_fbx) };
}

/** Ask the connected session to refresh the project's working model. */
export async function bridgeRefresh(
  projectDir: string,
  path = handshakePath(),
): Promise<BridgeRefreshResult> {
  const handshake = readHandshake(path);
  if (!handshake) return { ok: false, reason: 'no_handshake' };
  const targets = refreshTargets(projectDir);
  if (!targets.blend_path && !targets.fbx_path) {
    return { ok: false, reason: 'no_working_model' };
  }
  try {
    const res = await bridgeRequest(handshake, {
      cmd: 'refresh',
      project_dir: projectDir,
      blend_path: targets.blend_path,
      fbx_path: targets.fbx_path,
    });
    return {
      ok: !!res['ok'],
      mode: res['mode'] as string | undefined,
      reason: res['reason'] as string | undefined,
    };
  } catch (err) {
    return { ok: false, reason: `unreachable: ${(err as Error).message}` };
  }
}
