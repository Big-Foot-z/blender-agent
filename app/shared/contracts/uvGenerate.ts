/**
 * Shared Electron <-> worker contract for the MVP 3 Generate + Optimize app.
 *
 * TypeScript mirror of `worker/app_uv_generate_contract.py` (plan §4, §5, §9).
 * Renderer and main both import from here so a schema change is a single edit
 * (plan §14 "shared contract 변경은 먼저 문서를 갱신").
 *
 * MVP 3 runs the chart engine in STRICT user/reference mode: the MVP 2
 * `active_user_seam_spec` is the source of truth and the seam set is never
 * auto-changed. Seam integrity (`auto_added_seams == 0`,
 * `final_seam_count == user_seam_count`) is a hard acceptance; a run that breaks
 * it is `needs_user_review` and must not ship (plan §1, §6).
 *
 * Rules: UV_GENERATE_SCHEMA_VERSION is pinned; new fields are optional only.
 */

export const UV_GENERATE_SCHEMA_VERSION = 1;

// --- App-facing commands (plan §4) ----------------------------------------
export const UvGenerateCommand = {
  GenerateUvFromSeams: 'generate_uv_from_seams',
  SelectUvCandidate: 'select_uv_candidate',
} as const;
export type UvGenerateCommand = (typeof UvGenerateCommand)[keyof typeof UvGenerateCommand];

// --- Run lifecycle (plan §9) — note the `needs_user_review` outcome --------
// `needs_input` is the UV-boundary-fallback revision terminal status: no active
// seam spec AND no usable UV layer, so nothing could be unwrapped (revision plan
// §1 case 3, §4.2). It is NOT a failure — the UI asks the user to pick a UV layer
// / import a UV'd model / open the Seam Editor.
export const UvGenerateRunStatus = {
  Queued: 'queued',
  Running: 'running',
  Accepted: 'accepted',
  NeedsUserReview: 'needs_user_review',
  NeedsInput: 'needs_input',
  Failed: 'failed',
  Cancelled: 'cancelled',
} as const;
export type UvGenerateRunStatus = (typeof UvGenerateRunStatus)[keyof typeof UvGenerateRunStatus];

export const UV_GENERATE_TERMINAL_STATUSES: ReadonlySet<string> = new Set([
  UvGenerateRunStatus.Accepted,
  UvGenerateRunStatus.NeedsUserReview,
  UvGenerateRunStatus.NeedsInput,
  UvGenerateRunStatus.Failed,
  UvGenerateRunStatus.Cancelled,
]);

// --- Seam source resolution (UV-boundary-fallback revision plan §2, §4) ------
// A Generate run resolves its seam source by precedence: an explicit
// `active_user_seam_spec` wins; otherwise the selected UV layer's island
// boundaries are derived into a seam spec; otherwise the run is `needs_input`.
export const SeamSourceType = {
  UserSeamSpec: 'user_seam_spec',
  UvBoundaryDerived: 'uv_boundary_derived',
} as const;
export type SeamSourceType = (typeof SeamSourceType)[keyof typeof SeamSourceType];

/** The pre-flight seam-source kind `validateInput` reports (revision plan §4.4). */
export const SeamSourceKind = {
  Explicit: 'explicit',
  Derived: 'derived',
  Missing: 'missing',
} as const;
export type SeamSourceKind = (typeof SeamSourceKind)[keyof typeof SeamSourceKind];

export const DEFAULT_SEAM_SOURCE_POLICY = 'prefer_spec_then_uv_boundary';
export const MISSING_SEAM_SOURCE_CODE = 'missing_seam_source';
export const MISSING_SEAM_SOURCE_MESSAGE =
  'No user seam spec or usable UV layer was found. Select a UV layer or create seams.';

/** The summary's `seam_source` block (revision plan §2.3, §4). */
export interface SeamSource {
  type: SeamSourceType;
  path: string | null;
  uv_layer: string | null;
  user_confirmed: boolean;
  derived: boolean;
}

export const SUPPORTED_GENERATE_EXTS = ['.blend', '.fbx', '.obj', '.glb', '.gltf'] as const;

// --- Execution modes (work plan §3; gate G2) -------------------------------
// `preserve_existing` is the default so a legacy project with no `mode` keeps its
// MVP 3 strict behaviour; `auto_generate` is only ever entered explicitly.
export const UvGenerateMode = {
  AutoGenerate: 'auto_generate',
  PreserveExisting: 'preserve_existing',
} as const;
export type UvGenerateMode = (typeof UvGenerateMode)[keyof typeof UvGenerateMode];

export const DEFAULT_UV_GENERATE_MODE: UvGenerateMode = UvGenerateMode.PreserveExisting;

/** The four strict flags that, if flipped on, would let the seam set change. */
export const STRICT_FLAGS = [
  'auto_refine_user_seams',
  'repair_user_seams',
  'enforce_user_mandatory',
  'gate_user_mandatory',
] as const;
export type StrictFlag = (typeof STRICT_FLAGS)[number];

// --- Strict user/reference defaults (plan §1) -----------------------------
export const DEFAULT_LAYOUT_OPT_PRESET = 'user_reference';
export const DEFAULT_LAYOUT_OPT_MAX_CANDIDATES = 24;

/** The Generate options block. All strict flags default false (plan §1). */
export interface GenerateUvOptions {
  uv_engine?: string;
  auto_refine_user_seams?: boolean;
  repair_user_seams?: boolean;
  enforce_user_mandatory?: boolean;
  gate_user_mandatory?: boolean;
  optimize_layout?: boolean;
  layout_opt_preset?: string;
  layout_opt_max_candidates?: number;
  render_previews?: boolean;
  save_selected_blend?: boolean;
  // Render tuning (optional; worker defaults apply when omitted).
  texture_size_px?: number;
  checker_scale?: number;
  render_size_px?: number;
  // Execution mode + common automation options (work plan §3, §5; gates G2/G5).
  // `null` means "use the quality profile's value" — never silently 0 (gate G6).
  mode?: UvGenerateMode;
  quality_profile?: string;
  seed?: number;
  margin_px?: number;
  max_iterations?: number | null;
  max_candidates_per_round?: number | null;
  time_budget_s?: number | null;
  island_cap?: number | null;
}

export const DEFAULT_QUALITY_PROFILE = 'engineering_v0';
export const DEFAULT_SEED = 0;
export const DEFAULT_MARGIN_PX = 4;

/** The fully resolved option set a mode's defaults provide (work plan §3). */
export type ResolvedGenerateOptions = Required<
  Pick<
    GenerateUvOptions,
    | 'uv_engine'
    | 'auto_refine_user_seams'
    | 'repair_user_seams'
    | 'enforce_user_mandatory'
    | 'gate_user_mandatory'
    | 'optimize_layout'
    | 'layout_opt_preset'
    | 'layout_opt_max_candidates'
    | 'render_previews'
    | 'save_selected_blend'
    | 'quality_profile'
    | 'seed'
    | 'margin_px'
    | 'max_iterations'
    | 'max_candidates_per_round'
    | 'time_budget_s'
    | 'island_cap'
  >
>;

export const STRICT_GENERATE_OPTIONS: ResolvedGenerateOptions = {
  uv_engine: 'chart',
  auto_refine_user_seams: false,
  repair_user_seams: false,
  enforce_user_mandatory: false,
  gate_user_mandatory: false,
  optimize_layout: true,
  layout_opt_preset: DEFAULT_LAYOUT_OPT_PRESET,
  layout_opt_max_candidates: DEFAULT_LAYOUT_OPT_MAX_CANDIDATES,
  render_previews: true,
  save_selected_blend: true,
  // Common automation options — shared by BOTH modes (work plan §3, §5).
  quality_profile: DEFAULT_QUALITY_PROFILE,
  seed: DEFAULT_SEED,
  margin_px: DEFAULT_MARGIN_PX,
  max_iterations: null,
  max_candidates_per_round: null,
  time_budget_s: null,
  island_cap: null,
};

/**
 * `auto_generate` defaults: the same surface as the strict/preserve defaults with
 * the four seam-changing flags ON, because automatic cutting is the point of the
 * mode (work plan §3). Seam locks/protections are enforced by the constraint
 * evaluation, not by holding these flags false.
 */
export const AUTO_GENERATE_OPTIONS: ResolvedGenerateOptions = {
  ...STRICT_GENERATE_OPTIONS,
  auto_refine_user_seams: true,
  repair_user_seams: true,
  enforce_user_mandatory: true,
  gate_user_mandatory: true,
};

/** Per-mode default option sets (work plan §3 "UI/TS/Python의 기본값을 맞춘다"). */
export const MODE_GENERATE_OPTIONS: Record<UvGenerateMode, ResolvedGenerateOptions> = {
  [UvGenerateMode.PreserveExisting]: STRICT_GENERATE_OPTIONS,
  [UvGenerateMode.AutoGenerate]: AUTO_GENERATE_OPTIONS,
};

/** Normalize a raw mode value; empty/absent falls back to the default (gate G2). */
export function resolveGenerateMode(value: unknown): UvGenerateMode {
  if (value === null || value === undefined || value === '') return DEFAULT_UV_GENERATE_MODE;
  if (value === UvGenerateMode.AutoGenerate || value === UvGenerateMode.PreserveExisting) {
    return value;
  }
  throw new Error(`unknown_mode: ${String(value)}`);
}

export interface ModeRequestError {
  code: string;
  message: string;
  flag?: string;
}

export interface ModeRequestValidation {
  ok: boolean;
  mode: UvGenerateMode | null;
  errors: ModeRequestError[];
}

/**
 * Reject a mode + raw-options combination that contradicts itself BEFORE the run
 * starts (work plan §3; gate G2).
 */
export function validateModeRequest(
  mode: unknown,
  rawOptions?: GenerateUvOptions | null,
): ModeRequestValidation {
  const errors: ModeRequestError[] = [];
  let resolved: UvGenerateMode;
  try {
    resolved = resolveGenerateMode(mode);
  } catch {
    errors.push({ code: 'unknown_mode', message: `Unknown generate mode: ${String(mode)}` });
    return { ok: false, mode: null, errors };
  }

  const raw = (rawOptions ?? {}) as Record<string, unknown>;
  for (const flag of STRICT_FLAGS) {
    const value = raw[flag];
    if (value === undefined) continue;
    if (resolved === UvGenerateMode.PreserveExisting && value === true) {
      errors.push({
        code: 'strict_flag_contradicts_preserve',
        message: `${flag}=true contradicts mode ${resolved}`,
        flag,
      });
    } else if (resolved === UvGenerateMode.AutoGenerate && value === false) {
      errors.push({
        code: 'strict_flag_contradicts_auto',
        message: `${flag}=false contradicts mode ${resolved}`,
        flag,
      });
    }
  }

  const optionMode = raw.mode;
  if (
    optionMode !== undefined &&
    optionMode !== null &&
    optionMode !== '' &&
    optionMode !== resolved
  ) {
    errors.push({
      code: 'mode_mismatch',
      message: `options.mode ${String(optionMode)} does not match requested mode ${resolved}`,
    });
  }

  return { ok: errors.length === 0, mode: resolved, errors };
}

/**
 * Overlay caller options on the mode's defaults (mirror `merge_options`).
 *
 * The mode is decided by precedence: explicit `mode` argument > `user.mode` >
 * `DEFAULT_UV_GENERATE_MODE`, so an existing `mergeGenerateOptions(x)` call keeps
 * its strict/preserve behaviour. The resolved mode is recorded in the result.
 */
export function mergeGenerateOptions(
  user?: GenerateUvOptions | null,
  mode?: UvGenerateMode,
): GenerateUvOptions {
  const resolved = resolveGenerateMode(mode ?? user?.mode ?? null);
  return { ...MODE_GENERATE_OPTIONS[resolved], ...(user ?? {}), mode: resolved };
}

// --- IPC channels (preload <-> main, plan §11 Session E) ------------------
export const UvGenerateIpc = {
  ValidateInput: 'uvGenerate:validateInput',
  Start: 'uvGenerate:start',
  Cancel: 'uvGenerate:cancel',
  GetRun: 'uvGenerate:getRun',
  GetCandidateSummary: 'uvGenerate:getCandidateSummary',
} as const;

// --- Summary shapes (plan §4.1) -------------------------------------------
export interface GenerateMetrics {
  stretch_score?: number;
  worst_island_distortion?: number;
  raster_overlap_ratio?: number;
  overlap_ratio?: number;
  texel_density_variance?: number;
  packing_efficiency?: number;
  island_count?: number;
  uv_bounds_ok?: boolean;
}

export interface SeamIntegrity {
  user_seam_count: number;
  user_protected_count: number;
  final_seam_count: number;
  auto_added_seams: number;
  mandatory_rule_enabled: boolean;
  mandatory_gate_enabled: boolean;
  valid: boolean;
}

/** The honest optimization verdict (plan §2 Goal D status 문구). */
export const OptimizationVerdict = {
  Meaningful: 'meaningful',
  MinorPackingOnly: 'minor_packing_only',
  BaselineRetained: 'baseline_retained',
  NeedsBetterPacking: 'needs_better_packing',
  ConsiderSeamEdits: 'consider_seam_edits',
} as const;
export type OptimizationVerdict = (typeof OptimizationVerdict)[keyof typeof OptimizationVerdict];

/** Packing/stretch/texel deltas + meaningful flag (plan §2 Goal C). */
export interface OptimizationImprovement {
  meaningful: boolean;
  packing_delta: number;
  stretch_delta: number;
  texel_density_delta: number;
  score_ratio: number;
  packing_meaningful: boolean;
  texel_meaningful: boolean;
  score_meaningful: boolean;
}

export interface LayoutOptimizationBlock {
  enabled: boolean;
  selected_candidate_id?: string | null;
  kept_baseline?: boolean;
  candidate_count?: number;
  score_before?: number | null;
  score_after?: number | null;
  packing_efficiency_before?: number | null;
  packing_efficiency_after?: number | null;
  stretch_before?: number | null;
  stretch_after?: number | null;
  // MVP3 §2 Goal C/D: honest improvement + verdict the UI shows verbatim.
  texel_density_before?: number | null;
  texel_density_after?: number | null;
  improvement?: OptimizationImprovement;
  verdict?: OptimizationVerdict | string;
}

// --- Automation report blocks (work plan §3-§5; gates G1/G3/G5/G6) ---------
// All optional on the summary so a pre-automation run still parses.

/** The frozen quality profile a run was scored against (gate G8). */
export interface QualityProfileBlock {
  profile_id: string;
  metric_version: number;
  calibrated: boolean;
  [k: string]: unknown;
}

/** Automatic-mode gate verdict (gate G2/G6). `valid` false means not evaluable. */
export interface AutoGateBlock {
  valid: boolean;
  passed: boolean;
  failures: string[];
  invalid_reasons: string[];
}

/** Locked/protected seam constraints carried into the automatic solver (gate G4). */
export interface AutoConstraintsBlock {
  locked_seam_count?: number;
  locked_missing_count?: number;
  protected_count?: number;
  protected_cut_count?: number;
  conflict_count?: number;
  conflicts_unresolved?: boolean;
  valid: boolean;
}

/** Distortion v2 metric set — global, per-island or per-region (work plan §4). */
export interface DistortionMetricSet {
  anisotropy_mean?: number;
  anisotropy_p95?: number;
  anisotropy_max?: number;
  area_stretch_mean?: number;
  area_stretch_p95?: number;
  area_stretch_max?: number;
  exceed_area_fraction?: number;
}

export interface DistortionIslandRow extends DistortionMetricSet {
  island_id: number;
  face_count: number;
  area_3d?: number;
  area_uv?: number;
}

export interface DistortionV2Block {
  metric_version: number;
  evaluation_stage?: string;
  scale_policy?: string;
  valid: boolean;
  global: DistortionMetricSet;
  islands: DistortionIslandRow[];
  degenerate_triangles?: {
    input_defect_count: number;
    uv_degenerate_count: number;
  };
  regions?: Record<string, DistortionMetricSet>;
}

/** Hard UV correctness checks (gate G1). */
export interface CorrectnessCheck {
  name: string;
  passed: boolean;
  value?: number | null;
  limit?: number | null;
  detail?: string;
}

export interface CorrectnessBlock {
  passed: boolean;
  checks: CorrectnessCheck[];
  overlap_area_total?: number;
  local_flip_count?: number;
  mirrored_island_count?: number;
  uv_degenerate_count?: number;
  min_island_gap_px?: number | null;
  bounds_ok?: boolean;
}

/** Re-read of the saved file, re-measured from disk (gate G6). */
export interface RereadAuditBlock {
  passed: boolean;
  source?: string;
  mandatory_90_uv_unsplit?: number;
  correctness?: CorrectnessBlock | null;
  edge_id_remap?: boolean;
  [k: string]: unknown;
}

/** Before/after mesh hash so UV work never silently changed the mesh (gate G1). */
export interface MeshIdentityBlock {
  before_sha256?: string;
  after_sha256?: string;
  unchanged: boolean;
  vertex_count?: number;
  face_count?: number;
  loop_count?: number;
}

/** Why the candidate search stopped and what it spent (gate G5). */
export interface TerminationBlock {
  reason: string;
  iterations: number;
  candidates_evaluated: number;
  elapsed_s: number;
  budget?: Record<string, number | null>;
}

/** Seam length accounting by origin (work plan §5). */
export interface SeamLengthBlock {
  mandatory: number;
  user: number;
  auxiliary: number;
  total: number;
  bbox_diagonal: number;
  auxiliary_normalized: number;
  mandatory_edge_count?: number;
  user_edge_count?: number;
  auxiliary_edge_count?: number;
}

/** 90-degree mandatory seam audit; `reported_only` in preserve mode (gate G2). */
export interface MandatoryAuditBlock {
  mandatory_90_edges: number;
  mandatory_90_missing: number;
  mandatory_90_uv_unsplit: number;
  reported_only?: boolean;
}

/** Artist sign-off — tracked separately from the solver verdict (gate G6/G7). */
export interface ArtistApproval {
  approved: boolean;
  run_id?: string | null;
  reviewer?: string | null;
  reason?: string | null;
  approved_at?: string | null;
}

/** Stable artifact keys -> run-relative filenames (plan §4.1). */
export interface GenerateArtifacts {
  summary?: string;
  p5_gate?: string;
  seam_report?: string;
  candidate_summary?: string;
  // UV-boundary fallback artifacts (revision plan §3.1) — present on a derived run.
  seam_source_resolution?: string;
  derived_seam_spec?: string;
  baseline_uv_layout?: string;
  baseline_checker_front?: string;
  baseline_checker_side?: string;
  selected_uv_layout?: string;
  selected_checker_front?: string;
  selected_checker_side?: string;
  selected_blend?: string;
  // --- Automation artifacts (work plan §4, §7; gates G3/G5/G6) — optional ---
  distortion_v2?: string;
  correctness?: string;
  final_reread_audit?: string;
  run_manifest?: string;
  candidate_history?: string;
  mesh_identity?: string;
  quality_profile?: string;
  seam_overlay?: string;
  selected_heatmap_anisotropy?: string;
}

export interface UvGenerateWorkerError {
  code: string;
  message: string;
  traceback?: string;
  details?: Record<string, unknown>;
}

/** `uv_generate_summary.json` — the renderer's primary input (plan §3, §4.1). */
export interface UvGenerateSummary {
  schema_version: number;
  run_id: string;
  command: string;
  status: UvGenerateRunStatus;
  model: string | null;
  object_name: string | null;
  seam_spec: string | null;
  /** Where the seam set came from — explicit spec or derived UV boundary
   * (revision plan §2.3, §4). `null` only on legacy/pre-revision summaries. */
  seam_source: SeamSource | null;
  selected_candidate_id: string | null;
  selected_uv_model: string | null;
  metrics: GenerateMetrics;
  seam_integrity: SeamIntegrity;
  layout_optimization: LayoutOptimizationBlock;
  artifacts: GenerateArtifacts;
  warnings: string[];
  // --- Automation extension (work plan §3-§5; gates G2/G3/G5/G6) — optional
  // so a pre-automation summary still parses. ---
  mode?: UvGenerateMode;
  solver_accepted?: boolean;
  artist_approved?: boolean;
  artist_approval?: ArtistApproval | null;
  acceptance_reason?: string | null;
  quality_profile?: QualityProfileBlock | null;
  auto_gate?: AutoGateBlock | null;
  auto_constraints?: AutoConstraintsBlock | null;
  distortion_v2?: DistortionV2Block | null;
  correctness?: CorrectnessBlock | null;
  final_reread_audit?: RereadAuditBlock | null;
  mesh_identity?: MeshIdentityBlock | null;
  termination?: TerminationBlock | null;
  seam_length?: SeamLengthBlock | null;
  mandatory_audit?: MandatoryAuditBlock | null;
}

// --- Candidate summary (plan §5) ------------------------------------------
export interface CandidateRow {
  id: string | null;
  unwrap_method: string | null;
  minimize_iters: number;
  margin: number | null;
  pack_shape: string | null;
  rotate: boolean;
  average_scale: boolean;
  // MVP3 §2 Goal B: island-level custom packing backend + pre-pass flags.
  pack_backend?: string;
  orient_long_islands?: boolean;
  density_normalize?: boolean;
  accepted: boolean;
  reason: string;
  score: number | null;
  metrics: GenerateMetrics;
}

export interface RejectedCandidate {
  id: string | null;
  reason: string;
}

export interface CandidateSummary {
  schema_version: number;
  baseline_candidate_id: string | null;
  selected_candidate_id: string | null;
  kept_baseline: boolean;
  score_weights: Record<string, number>;
  candidates: CandidateRow[];
  rejected: RejectedCandidate[];
}

export interface UvGenerateStatusDoc {
  schema_version: number;
  run_id: string;
  command: string;
  status: UvGenerateRunStatus;
  started_at: string;
  finished_at: string | null;
  input: Record<string, unknown>;
  artifacts: Record<string, string>;
  error: UvGenerateWorkerError | null;
}

/** Combined generate-run view the renderer reads via `uvGenerate:getRun`. */
export interface UvGenerateRunView {
  run_id: string;
  dir: string;
  status: UvGenerateStatusDoc | null;
  summary: UvGenerateSummary | null;
  candidate_summary: CandidateSummary | null;
  /** Parsed `p5_gate.json` for the debug tab (plan §5 "Raw p5_gate.json은 debug tab"). */
  p5_gate: Record<string, unknown> | null;
  /** Parsed `seam_report.json` for the raw-report tab. */
  seam_report: Record<string, unknown> | null;
  stdout: string;
  stderr: string;
  /** Stable artifact key -> absolute path on disk, for `uvpreview://` rendering. */
  artifact_paths: Record<string, string>;
}

// --- validateInput (plan §8 "Validate Seam Spec" pre-flight) --------------
export interface ValidateGenerateIssue {
  code: string;
  message: string;
}

/** Pre-flight readiness for a Generate run (cheap, pure-Node checks). */
export interface ValidateGenerateInput {
  ready: boolean;
  model: string | null;
  object_name: string | null;
  seam_spec: string | null;
  spec_object: string | null;
  user_seam_count: number | null;
  user_protected_count: number | null;
  object_mismatch: boolean;
  /** Which seam source the run will use (revision plan §4.4, §4.5). `derived`
   * means no usable spec but a selected UV layer exists; `missing` blocks Generate. */
  seam_source: SeamSourceKind;
  /** The selected/active UV layer used for the derived fallback (revision plan §4.4). */
  selected_uv_layer: string | null;
  issues: ValidateGenerateIssue[];
}

export interface StartGenerateResult {
  run_id: string;
}

/** select_uv_candidate placeholder result (plan §4.2 — read-only in MVP 3.0). */
export interface SelectCandidateResult {
  status: string;
  selected_candidate_id: string;
  selected_uv_model: string | null;
}
