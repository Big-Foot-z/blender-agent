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
  SEAM_REASON_CODES,
  STRICT_FLAGS,
  STRICT_GENERATE_OPTIONS,
  SeamReasonCode,
  UvGenerateMode,
  mergeGenerateOptions,
  resolveGenerateMode,
  validateModeRequest,
} from '../shared/contracts/uvGenerate';
import type {
  RejectedCandidateEntry,
  SeamOverlay,
  SeamOverlayEdge,
  UvGenerateSummary,
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

// --- Game-gate evidence blocks (T14; gate G15) ------------------------------
// The summary is the reviewer's primary input, so the new evidence blocks must
// parse as typed data (not `unknown`) and the reason-code vocabulary must stay
// the fixed seven codes `chart_uv_agent.reporting` emits.
test('summary: the game-gate evidence blocks parse as typed optional blocks', () => {
  const summary: UvGenerateSummary = {
    schema_version: 1,
    run_id: 'gen_1',
    command: 'generate_uv_from_seams',
    status: 'accepted',
    model: 'work/model.blend',
    object_name: 'SM_Test',
    seam_spec: null,
    seam_source: null,
    selected_candidate_id: 'c1',
    selected_uv_model: 'work/uv/selected_uv.blend',
    metrics: {},
    seam_integrity: {
      user_seam_count: 0,
      user_protected_count: 0,
      final_seam_count: 0,
      auto_added_seams: 0,
      mandatory_rule_enabled: false,
      mandatory_gate_enabled: false,
      valid: true,
    },
    layout_optimization: { enabled: false },
    artifacts: {
      quality_report: 'quality_report.json',
      merge_back_history: 'merge_back_history.json',
      seam_overlay_png: 'seam_overlay.png',
      shading_policy: 'shading_policy.json',
    },
    warnings: [],
    correctness: { passed: true, checks: [], min_island_gap_px: 6, min_border_gap_px: 5 },
    fragmentation: {
      passed: true,
      valid: true,
      failures: [],
      quality_failures: [],
      metrics: {
        island_count: 52,
        tiny_island_count: 1,
        tiny_island_area_ratio: 0.0031,
        sliver_island_count: 0,
        one_two_face_island_count: 0,
        island_aspect_p95: 3.2,
        normalized_seam_length: 0.1667,
      },
      exempt_islands: [7],
      tiny_island_ids: [7],
      sliver_island_ids: [],
    },
    texel_density: {
      passed: true,
      valid: true,
      failures: [],
      density_mean: 512.4,
      density_cv: 0.041,
      outlier_count: 1,
      outlier_island_ids: [11],
    },
    packing: { efficiency: 0.59, limit: 0.55, passed: true, advisory: true },
    shading: { policy: 'hard_edges_are_seams', passed: true, valid: true, failures: [], invalid_reasons: [] },
    merge_back: {
      complete: true,
      enabled: true,
      trials: 2,
      accepted: 1,
      island_count_before: 53,
      island_count_after: 52,
      seam_length_before: 1.62,
      seam_length_after: 1.5,
      removable_remaining: 0,
      reason: 'no_removable_seam',
    },
    quality_report_passed: true,
  };

  // 1. the artifact keys are part of the typed artifact map.
  assert.equal(summary.artifacts.merge_back_history, 'merge_back_history.json');
  assert.equal(summary.artifacts.seam_overlay_png, 'seam_overlay.png');

  // 2. the padding-failure location travels on `correctness`.
  assert.equal(summary.correctness!.min_border_gap_px, 5);

  // 3. the reviewer-visible evidence: tiny/sliver ids, outliers, merge-back.
  assert.deepEqual(summary.fragmentation!.tiny_island_ids, [7]);
  assert.equal(summary.fragmentation!.metrics!.island_aspect_p95, 3.2);
  assert.deepEqual(summary.texel_density!.outlier_island_ids, [11]);
  assert.equal(summary.packing!.advisory, true);
  assert.equal(summary.merge_back!.island_count_after, 52);
  assert.equal(summary.quality_report_passed, true);
});

test('seam overlay: the reason-code union covers the seven G15 codes', () => {
  // 1. the exported order IS the vocabulary — no more, no less.
  assert.deepEqual(SEAM_REASON_CODES.slice(), [
    'mandatory_90',
    'boundary_topology',
    'user',
    'shading',
    'material',
    'distortion_added',
    'rejected_candidate',
  ]);

  // 2. an overlay edge carries the code plus the cut reason and its cost.
  const edge: SeamOverlayEdge = {
    edge_id: 12,
    type: 'distortion_split',
    reason_code: SeamReasonCode.DistortionAdded,
    cut_reason: 'distortion_repair',
    cost_total: 0.42,
    target_island: 3,
    improvement_ratio: 0.31,
    a: [0, 0, 0],
    b: [1, 0, 0],
  };
  assert.equal(edge.reason_code, 'distortion_added');

  // 3. rejected candidates are a separate list, tagged with their own code.
  const rejected: RejectedCandidateEntry = {
    edge_id: 44,
    reason_code: SeamReasonCode.RejectedCandidate,
    reject_reason: 'no_improvement',
    round: 2,
    target_island: 5,
    kind: 'distortion_split',
    improvement_ratio: 0.01,
    cost_total: 1.2,
    a: [0, 0, 0],
    b: [0, 1, 0],
  };
  const overlay: SeamOverlay = {
    schema_version: 1,
    object_name: 'SM_Test',
    edges: [edge],
    conflicts: [],
    type_counts: { distortion_split: 1 },
    rejected_candidates: [rejected],
    reason_code_counts: { distortion_added: 1, rejected_candidate: 1 },
  };
  assert.equal(overlay.rejected_candidates!.length, 1);
  assert.equal(overlay.reason_code_counts!.rejected_candidate, 1);
});
