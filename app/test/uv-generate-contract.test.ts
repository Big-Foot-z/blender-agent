/**
 * UV generate mode contract (work plan §3; gate G2).
 *
 * Pure TS-mirror checks — no Electron, no worker, no Blender. Keeps the
 * UI → IPC → worker → report mode contract aligned with
 * `worker/app_uv_generate_contract.py`.
 *
 * Run: npm run test:integration
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  AUTO_GENERATE_OPTIONS,
  DEFAULT_UV_GENERATE_MODE,
  STRICT_FLAGS,
  STRICT_GENERATE_OPTIONS,
  UvGenerateMode,
  mergeGenerateOptions,
  resolveGenerateMode,
  validateModeRequest,
} from '../shared/contracts/uvGenerate';

test('resolveGenerateMode: empty falls back, known passes through, unknown throws', () => {
  // 1. null / undefined / '' -> the default (legacy project with no mode).
  assert.equal(resolveGenerateMode(null), DEFAULT_UV_GENERATE_MODE);
  assert.equal(resolveGenerateMode(undefined), UvGenerateMode.PreserveExisting);
  assert.equal(resolveGenerateMode(''), UvGenerateMode.PreserveExisting);

  // 2. both known modes resolve to themselves.
  assert.equal(resolveGenerateMode('auto_generate'), UvGenerateMode.AutoGenerate);
  assert.equal(resolveGenerateMode('preserve_existing'), UvGenerateMode.PreserveExisting);

  // 3. anything else is an explicit error, never a silent default.
  assert.throws(() => resolveGenerateMode('turbo'), /unknown_mode: turbo/);
});

test('validateModeRequest: contradictory mode/flag combinations are rejected', () => {
  // 1. unknown mode.
  const unknown = validateModeRequest('turbo');
  assert.equal(unknown.ok, false);
  assert.equal(unknown.mode, null);
  assert.deepEqual(
    unknown.errors.map((e) => e.code),
    ['unknown_mode'],
  );

  // 2. preserve_existing + an explicitly-true strict flag.
  const preserveTrue = validateModeRequest(UvGenerateMode.PreserveExisting, {
    repair_user_seams: true,
  });
  assert.equal(preserveTrue.ok, false);
  assert.equal(preserveTrue.mode, UvGenerateMode.PreserveExisting);
  assert.deepEqual(
    preserveTrue.errors.map((e) => [e.code, e.flag]),
    [['strict_flag_contradicts_preserve', 'repair_user_seams']],
  );

  // 3. auto_generate + an explicitly-false strict flag.
  const autoFalse = validateModeRequest(UvGenerateMode.AutoGenerate, {
    enforce_user_mandatory: false,
  });
  assert.equal(autoFalse.ok, false);
  assert.deepEqual(
    autoFalse.errors.map((e) => [e.code, e.flag]),
    [['strict_flag_contradicts_auto', 'enforce_user_mandatory']],
  );

  // 4. options.mode disagreeing with the requested mode.
  const mismatch = validateModeRequest(UvGenerateMode.AutoGenerate, {
    mode: UvGenerateMode.PreserveExisting,
  });
  assert.equal(mismatch.ok, false);
  assert.deepEqual(
    mismatch.errors.map((e) => e.code),
    ['mode_mismatch'],
  );

  // 5. a consistent request passes — no raw flags, matching mode.
  const ok = validateModeRequest(UvGenerateMode.PreserveExisting, {
    mode: UvGenerateMode.PreserveExisting,
    layout_opt_max_candidates: 4,
  });
  assert.equal(ok.ok, true);
  assert.equal(ok.mode, UvGenerateMode.PreserveExisting);
  assert.deepEqual(ok.errors, []);
});

test('mergeGenerateOptions: mode precedence and per-mode defaults', () => {
  // 1. existing single-argument callers keep strict/preserve behaviour.
  const legacy = mergeGenerateOptions({ layout_opt_max_candidates: 4 });
  assert.equal(legacy.mode, UvGenerateMode.PreserveExisting);
  assert.equal(legacy.layout_opt_max_candidates, 4);
  assert.equal(legacy.uv_engine, STRICT_GENERATE_OPTIONS.uv_engine);
  for (const flag of STRICT_FLAGS) {
    assert.equal(legacy[flag], false, `${flag} must stay false in preserve mode`);
  }
  assert.equal(legacy.quality_profile, 'engineering_v0');
  assert.equal(legacy.seed, 0);
  assert.equal(legacy.margin_px, 4);
  assert.equal(legacy.max_iterations, null);
  assert.equal(legacy.texture_size_px, undefined);

  // 2. the auto mode turns all four strict flags on by default.
  const auto = mergeGenerateOptions(null, UvGenerateMode.AutoGenerate);
  assert.equal(auto.mode, UvGenerateMode.AutoGenerate);
  for (const flag of STRICT_FLAGS) {
    assert.equal(auto[flag], true, `${flag} must default true in auto mode`);
  }
  assert.equal(auto.quality_profile, AUTO_GENERATE_OPTIONS.quality_profile);
  assert.equal(auto.island_cap, null);

  // 3. the explicit mode argument wins over user.mode, and overlays still apply.
  const explicit = mergeGenerateOptions(
    { mode: UvGenerateMode.PreserveExisting, seed: 7 },
    UvGenerateMode.AutoGenerate,
  );
  assert.equal(explicit.mode, UvGenerateMode.AutoGenerate);
  assert.equal(explicit.seed, 7);
  assert.equal(explicit.auto_refine_user_seams, true);

  // ...and user.mode alone selects the mode when no argument is given.
  const fromUser = mergeGenerateOptions({ mode: UvGenerateMode.AutoGenerate });
  assert.equal(fromUser.mode, UvGenerateMode.AutoGenerate);
  assert.equal(fromUser.gate_user_mandatory, true);
});
