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
  /** Tests only: force the mock runner into a terminal status (gate G6). */
  mock_status?: string;
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
  /** Persist the project's execution mode (work plan §3; gate G2). */
  SetMode: 'uvGenerate:setMode',
  /** Record the artist verdict — separate from `solver_accepted` (gate G6/G7). */
  SetArtistApproval: 'uvGenerate:setArtistApproval',
  /** Read the saved reviewer feedback (locks/protected/preferred) (gate G7). */
  GetFeedback: 'uvGenerate:getFeedback',
  /** Persist reviewer feedback so the next run can re-apply it (gate G7). */
  SaveFeedback: 'uvGenerate:saveFeedback',
} as const;

// --- Reviewer feedback (gate G7) -------------------------------------------
/** Project-relative path of the saved reviewer feedback (posix; joined in main). */
export const UV_FEEDBACK_REL = 'work/uv/uv_feedback.json';

/**
 * Saved reviewer constraints, re-applied on a later run of the SAME mesh.
 *
 * `mesh_fingerprint` is what makes re-use safe: when the model's topology
 * changes the edge ids no longer correspond, so the worker must refuse to reuse
 * the constraints silently (gate G7 "topology 변경으로 edge 대응이 무효해지면
 * 제약을 조용히 재사용하지 않음").
 */
export interface UvFeedback {
  schema_version: 1;
  mesh_fingerprint: string | null;
  object_name: string | null;
  locked_seam_edges: number[];
  protected_edges: number[];
  preferred_edges: number[];
  front_axis: string;
  notes: string;
  updated_at: string;
  source_run_id: string | null;
}

/** Whether a run actually applied the saved feedback, and why (gate G7). */
export interface FeedbackApplied {
  applied: boolean;
  reason: 'fingerprint_match' | 'fingerprint_mismatch' | 'no_fingerprint';
  locked_seam_count: number;
  protected_count: number;
  preferred_count: number;
}

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
  /** Smallest island-to-tile-border gap in px — WHERE padding failed (gate G15). */
  min_border_gap_px?: number | null;
  bounds_ok?: boolean;
}

// --- Game-gate evidence blocks (T14; gate G15) -----------------------------
// Mirrors of `compact_fragmentation_block` / `compact_texel_density_block` /
// `compact_merge_back_block` plus the `packing` / `shading` / `quality_report_passed`
// keys `build_generate_summary` now writes. All optional on the summary so a
// pre-T14 run still parses.

/** Island fragmentation metrics — tiny/sliver/aspect evidence (gate G15). */
export interface FragmentationMetrics {
  island_count?: number | null;
  tiny_island_count?: number | null;
  tiny_island_area_ratio?: number | null;
  sliver_island_count?: number | null;
  one_two_face_island_count?: number | null;
  island_aspect_p95?: number | null;
  normalized_seam_length?: number | null;
  [k: string]: unknown;
}

export interface FragmentationBlock {
  passed?: boolean | null;
  valid?: boolean | null;
  failures?: string[] | null;
  quality_failures?: string[] | null;
  metrics?: FragmentationMetrics | null;
  /** Islands exempted from the tiny/sliver rules (small hard-surface details). */
  exempt_islands?: number[] | null;
  tiny_island_ids?: number[] | null;
  sliver_island_ids?: number[] | null;
}

/** Texel-density uniformity + the islands that break it (gate G15). */
export interface TexelDensityBlock {
  passed?: boolean | null;
  valid?: boolean | null;
  failures?: string[] | null;
  density_mean?: number | null;
  density_cv?: number | null;
  outlier_count?: number | null;
  outlier_island_ids?: number[] | null;
}

/** Packing efficiency vs its floor. `advisory` means it never fails a run. */
export interface PackingBlock {
  efficiency?: number | null;
  limit?: number | null;
  passed?: boolean | null;
  advisory?: boolean | null;
}

/** Shading/normal-split policy verdict (gate G15). */
export interface ShadingBlock {
  policy?: string | null;
  passed?: boolean | null;
  valid?: boolean | null;
  failures?: string[] | null;
  invalid_reasons?: string[] | null;
}

/** Merge-back accounting: how many seams were dissolved, and to what end. */
export interface MergeBackBlock {
  complete?: boolean | null;
  enabled?: boolean | null;
  trials?: number | null;
  accepted?: number | null;
  island_count_before?: number | null;
  island_count_after?: number | null;
  seam_length_before?: number | null;
  seam_length_after?: number | null;
  removable_remaining?: number | null;
  reason?: string | null;
}

/** One `merge_back_history.json` trial row (gate G15 "merge-back history"). */
export interface MergeBackHistoryEntry {
  trial?: number | null;
  island_a?: number | null;
  island_b?: number | null;
  edges?: number[] | null;
  shared_length?: number | null;
  accepted?: boolean | null;
  reason?: string | null;
  island_count_before?: number | null;
  island_count_after?: number | null;
  elapsed_s?: number | null;
  [k: string]: unknown;
}

/** `merge_back_history.json` — the merge-back block with its full trial log. */
export interface MergeBackHistory extends MergeBackBlock {
  history?: MergeBackHistoryEntry[] | null;
  [k: string]: unknown;
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
  // --- Reviewer evidence artifacts (T14; gate G15) — optional --------------
  quality_report?: string;
  merge_back_history?: string;
  seam_overlay_png?: string;
  shading_policy?: string;
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
  /** Saved reviewer feedback re-application verdict (gate G7). */
  feedback_applied?: FeedbackApplied | null;
  // --- Game gates (T14; gate G15) — reviewer-visible evidence blocks -------
  fragmentation?: FragmentationBlock | null;
  texel_density?: TexelDensityBlock | null;
  packing?: PackingBlock | null;
  shading?: ShadingBlock | null;
  merge_back?: MergeBackBlock | null;
  /** The combined game-quality verdict; null when the report was not produced. */
  quality_report_passed?: boolean | null;
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

// --- Review artifacts the renderer draws (gate G7) -------------------------

/**
 * One seam edge as exported for the 3D overlay, with the world-space endpoints
 * so the renderer never needs the full mesh. `type` distinguishes the origin
 * (mandatory_90 / user_seam / locked / welded_fold_auxiliary / overlap_repair /
 * distortion_split / correctness_repair / segmentation) so the reviewer can tell
 * WHY an edge was cut (gate G7 "mandatory/user/topology/overlap/distortion 이유 구분").
 */
/**
 * The fixed reviewer-facing reason vocabulary (gate G15): WHY a cut exists, in
 * seven codes the legend can enumerate. `rejected_candidate` is not a shipped
 * seam — it is a cut the engine tried and dropped (drawn dashed).
 */
export const SeamReasonCode = {
  Mandatory90: 'mandatory_90',
  BoundaryTopology: 'boundary_topology',
  User: 'user',
  Shading: 'shading',
  Material: 'material',
  DistortionAdded: 'distortion_added',
  RejectedCandidate: 'rejected_candidate',
} as const;
export type SeamReasonCode = (typeof SeamReasonCode)[keyof typeof SeamReasonCode];

export const SEAM_REASON_CODES: readonly SeamReasonCode[] = [
  SeamReasonCode.Mandatory90,
  SeamReasonCode.BoundaryTopology,
  SeamReasonCode.User,
  SeamReasonCode.Shading,
  SeamReasonCode.Material,
  SeamReasonCode.DistortionAdded,
  SeamReasonCode.RejectedCandidate,
] as const;

export interface SeamOverlayEdge {
  edge_id: number;
  type: string;
  /** The G15 reason code; absent on a pre-T14 overlay. */
  reason_code?: SeamReasonCode | string | null;
  /** The engine's own cut reason string behind the code (e.g. `distortion_repair`). */
  cut_reason?: string | null;
  /** Scalar candidate cost that justified the cut, when one was recorded. */
  cost_total?: number | null;
  reason?: string | null;
  stage?: string | null;
  round?: number | null;
  target_island?: number | null;
  improvement_ratio?: number | null;
  a: [number, number, number];
  b: [number, number, number];
}

/**
 * A cut a candidate proposed that was REJECTED and never shipped (gate G15).
 * Drawn dashed in the overlay so "what the engine decided not to do" is visible.
 */
export interface RejectedCandidateEntry {
  edge_id: number;
  reason_code?: SeamReasonCode | string | null;
  reject_reason?: string | null;
  round?: number | null;
  target_island?: number | null;
  kind?: string | null;
  improvement_ratio?: number | null;
  cost_total?: number | null;
  a: [number, number, number];
  b: [number, number, number];
}

/** `seam_overlay.json` — the 3D seam overlay source (gate G7/G15). */
export interface SeamOverlay {
  schema_version: number;
  object_name: string | null;
  edges: SeamOverlayEdge[];
  conflicts: { edge_id: number; user_rule?: string; engine_rule?: string; resolution?: string }[];
  type_counts: Record<string, number>;
  /** Tried-and-dropped cuts (gate G15); absent on a pre-T14 overlay. */
  rejected_candidates?: RejectedCandidateEntry[] | null;
  reason_code_counts?: Record<string, number> | null;
}

/**
 * One `candidate_history.json` row: what a refinement round changed, what it
 * measured before/after, and whether it was kept (work plan §7, gate G5/G7).
 */
export interface CandidateHistoryEntry {
  round: number;
  kind: string;
  target_island?: number | null;
  target_metric?: string | null;
  added_edges?: number[];
  before?: number | null;
  after?: number | null;
  accepted: boolean;
  reason: string;
  improvement_ratio?: number | null;
  island_count_after?: number | null;
  elapsed_s?: number | null;
  [k: string]: unknown;
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
  /** Parsed `seam_overlay.json` for the 3D seam overlay tab (gate G7). */
  seam_overlay: SeamOverlay | null;
  /** Parsed `candidate_history.json` for the candidates tab (gate G5/G7). */
  candidate_history: CandidateHistoryEntry[] | null;
  /** Parsed `merge_back_history.json` — the merge-back trial log (gate G15). */
  merge_back_history: MergeBackHistory | null;
  /** Parsed `quality_report.json` — the raw game-gate report (gate G15). */
  quality_report: Record<string, unknown> | null;
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
  /** The mode the readiness was evaluated for (work plan §3; gate G2). */
  mode?: UvGenerateMode;
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
