/**
 * Shared Electron <-> worker contract for the MVP 0 review app.
 *
 * TypeScript mirror of `worker/app_job_contract.py`. Renderer and main both import
 * from here so a schema change is a single edit (plan §10 "contract drift").
 *
 * Rules: SCHEMA_VERSION is pinned; new fields are optional only.
 */

export const SCHEMA_VERSION = 1;

// MVP 1 UV review contract (commands, review statuses, summary shapes).
export * from './uvReview';
import type {
  InspectUvResult,
  ReviewOptions,
  UvReviewRunView,
} from './uvReview';

// MVP 2 seam editor contract (edge geometry, seam spec, validation, IPC).
export * from './seamEditor';
import type {
  SeamEditorRunView,
  SeamSpec,
  SeamValidation,
} from './seamEditor';

// MVP 3 generate + optimize contract (run, candidate summary, validation, IPC).
export * from './uvGenerate';
import type {
  ArtistApproval,
  CandidateSummary,
  GenerateUvOptions,
  SelectCandidateResult,
  UvGenerateMode,
  UvGenerateRunView,
  ValidateGenerateInput,
} from './uvGenerate';

// MVP 5 production export contract (readiness, export, manifest, history, rollback).
export * from './export';
import type {
  ExportOptions,
  ExportReadiness,
  ExportRunView,
  HistoryEvent,
  ListRollbackTargetsResult,
  RollbackResult,
  RollbackTargetType,
  StartExportResult,
} from './export';

// --- App-facing worker commands (plan §5) ---------------------------------
export const Command = {
  InspectModel: 'inspect_model',
  GenerateLowpoly: 'generate_lowpoly',
  ApproveLowpoly: 'approve_lowpoly',
} as const;
export type Command = (typeof Command)[keyof typeof Command];

// --- Run status lifecycle (plan §4) ---------------------------------------
export const RunStatus = {
  Queued: 'queued',
  Running: 'running',
  Accepted: 'accepted',
  Rejected: 'rejected',
  Failed: 'failed',
  Cancelled: 'cancelled',
} as const;
export type RunStatus = (typeof RunStatus)[keyof typeof RunStatus];

export const SUPPORTED_IMPORT_EXTS = ['.fbx', '.obj', '.glb', '.gltf'] as const;

// --- Mesh role hints (plan §5.1) ------------------------------------------
export type MeshRole = 'lowpoly' | 'highpoly' | 'unknown';
export type NextStep =
  | 'approve_existing_lowpoly'
  | 'generate_lowpoly'
  | 'inspect_manually';

// --- IPC channel names (preload <-> main) ---------------------------------
export const Ipc = {
  ProjectCreate: 'project:create',
  ProjectOpen: 'project:open',
  ProjectGet: 'project:get',
  ModelInspect: 'model:inspect',
  LowpolyGenerate: 'lowpoly:generate',
  LowpolyApprove: 'lowpoly:approve',
  RunGet: 'run:get',
  RunList: 'run:list',
  SettingsGet: 'settings:get',
  SettingsSet: 'settings:set',
  PickFile: 'dialog:pickFile',
  PickProjectDir: 'dialog:pickProjectDir',
  PickBlender: 'dialog:pickBlender',
  // Blender integration (3D viewer + bridge plan §3, §4)
  BlenderOpen: 'blender:open',
  BridgeStatus: 'blender:bridgeStatus',
  BridgeRefresh: 'blender:bridgeRefresh',
  BridgeInstall: 'blender:bridgeInstall',
  // main -> renderer push event
  RunUpdate: 'run:update',
} as const;

// --- Project + run documents (plan §4) ------------------------------------
export interface Project {
  schema_version: number;
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
  dir?: string; // absolute project folder (added by main, not persisted into file body)
  source_model: string | null;
  source_model_role: MeshRole | null;
  selected_object: string | null;
  working_model: string | null;
  working_model_fbx: string | null;
  approved_lowpoly_run_id: string | null;
  runs: string[];
  // --- MVP 1 UV review extension (plan §9) — optional so MVP 0 projects load ---
  selected_uv_layer?: string | null;
  latest_uv_review_run_id?: string | null;
  uv_review_runs?: string[];
  // --- MVP 2 seam editor extension (plan §9) — optional so older projects load ---
  active_user_seam_spec?: string | null;
  latest_seam_editor_run_id?: string | null;
  seam_editor_runs?: string[];
  // --- MVP 3 generate + optimize extension (plan §9) — optional ---
  latest_uv_generate_run_id?: string | null;
  uv_generate_runs?: string[];
  selected_uv_model?: string | null;
  selected_uv_summary?: string | null;
  // UV-boundary fallback (revision plan §3.1): pointer to the most recent
  // accepted derived seam spec. Never overwrites `active_user_seam_spec`.
  latest_derived_seam_spec?: string | null;
  // --- UV automation extension (work plan §3; gates G2/G6) — optional so a
  // project with no mode is read as `preserve_existing`. ---
  uv_generate_mode?: UvGenerateMode | null;
  uv_artist_approval?: ArtistApproval | null;
  // --- MVP 5 production export extension (plan §2) — optional ---
  latest_export_id?: string | null;
  exports?: string[];
  history?: string | null;
  ai_review_skipped?: boolean;
}

export interface MeshObjectSummary {
  name: string;
  vertices: number;
  edges: number;
  faces: number;
  materials: string[];
  uv_layers: string[];
  bounds: { min: [number, number, number]; max: [number, number, number] } | null;
  mesh_role_hint: MeshRole;
}

export interface InspectResult {
  schema_version: number;
  status: 'accepted' | 'failed';
  command: string;
  project_id?: string | null;
  path?: string;
  objects?: MeshObjectSummary[];
  recommended_next_step?: NextStep;
  warnings?: string[];
  error?: WorkerError;
}

export interface GenerateOptions {
  mode?: 'decimation_optimize' | 'quad_retopo';
  preserve_features?: boolean;
  feature_angle?: number;
  apply_shrinkwrap?: boolean;
  retry_ladder?: boolean;
  // Number of retry-ladder escalation rungs to run when the primary collapse
  // misses the target band. 1 = single attempt (fast, default); 0 = disabled.
  retry_ladder_max_attempts?: number;
  render_preview?: boolean;
  // Large-mesh handling (plan §10): voxel-remesh to a proxy before decimation
  // when the source exceeds `proxy_face_threshold` faces.
  voxel_proxy?: boolean;
  proxy_face_threshold?: number;
  proxy_target_faces?: number;
}

export interface GenerateInput {
  command: typeof Command.GenerateLowpoly;
  project_id: string;
  run_id?: string;
  source_model?: string;
  object_name: string;
  target_faces: number;
  options?: GenerateOptions;
  out_dir?: string;
}

export interface WorkerError {
  code: string;
  message: string;
  traceback?: string;
}

export interface RunStatusDoc {
  schema_version: number;
  run_id: string;
  command: string;
  status: RunStatus;
  started_at: string;
  finished_at: string | null;
  input: Record<string, unknown>;
  artifacts: Record<string, string>;
  error: WorkerError | null;
}

export interface SummaryMetrics {
  source_faces: number | null;
  target_faces: number | null;
  actual_faces: number | null;
  target_error_ratio: number | null;
  non_manifold_edges: number | null;
  quad_ratio: number | null;
  triangle_ratio: number | null;
  ngon_count: number | null;
  surface_distance_mean_ratio: number | null;
  surface_distance_max_ratio: number | null;
  normal_deviation_mean_deg: number | null;
  volume_error_ratio: number | null;
}

export interface Summary {
  schema_version: number;
  run_id: string;
  command: string;
  object_name: string | null;
  result_object_name: string | null;
  method: string | null;
  metrics: SummaryMetrics;
  reports: {
    generation: string | null;
    validation: string | null;
    shape: string | null;
  };
  artifacts: Record<string, string>;
  warnings: string[];
}

/** Combined run view the renderer reads via `run:get` (plan §3 polling). */
export interface RunView {
  run_id: string;
  dir: string;
  status: RunStatusDoc | null;
  summary: Summary | null;
  reports: Record<string, unknown>; // raw per-phase reports for the report tabs
  stdout: string;
  stderr: string;
  preview_path: string | null; // absolute path to preview.png if present
}

export interface AppSettings {
  blenderPath: string | null;
  projectsRoot: string | null;
  /** Auto-refresh a connected Blender session after a low-poly approval
   *  (bridge plan §4.3). Optional so pre-bridge stored settings still load. */
  autoRefreshBlender?: boolean;
}

// --- Blender launcher + bridge (3D viewer + bridge plan §3, §4) ------------

export interface BlenderOpenResult {
  status: 'launched';
  /** Absolute path of the model handed to Blender. */
  target: string;
}

/** Result of reading the bridge handshake file + pinging the addon server. */
export interface BridgeStatus {
  connected: boolean;
  /** `bpy.data.filepath` of the connected session (empty if unsaved). */
  file?: string | null;
  /** `bpy.data.is_dirty` of the connected session. */
  dirty?: boolean;
  /** Why not connected: 'no_handshake' | 'unreachable' | error text. */
  reason?: string;
}

/** A single bridge command response (JSON-line protocol, plan §4.1). */
export interface BridgeRefreshResult {
  ok: boolean;
  /** 'revert' | 'reimport' when ok; set on success only. */
  mode?: string;
  /** 'dirty' | 'not_loaded' | 'not_found' | transport error text when !ok. */
  reason?: string;
}

/** Outcome of the auto-refresh attempted right after a lowpoly approval. */
export interface ApproveBlenderRefresh {
  attempted: boolean;
  ok?: boolean;
  mode?: string;
  reason?: string;
}

/** The API surface exposed on `window.api` by the preload bridge. */
export interface RendererApi {
  projectCreate(input: { name: string; sourcePath: string; role?: MeshRole }): Promise<Project>;
  projectOpen(dir: string): Promise<Project>;
  projectGet(projectId: string): Promise<Project>;
  modelInspect(input: { projectId: string; path?: string }): Promise<InspectResult>;
  lowpolyGenerate(input: {
    projectId: string;
    objectName: string;
    targetFaces: number;
    options?: GenerateOptions;
  }): Promise<{ run_id: string }>;
  lowpolyApprove(input: { projectId: string; runId: string }): Promise<{
    status: string;
    working_model: string;
    working_model_fbx: string | null;
    approved_lowpoly_run_id: string;
    /** Auto-refresh outcome when a bridge session was reachable (plan §4.3). */
    blender_refresh?: ApproveBlenderRefresh | null;
  }>;
  runGet(input: { projectId: string; runId: string }): Promise<RunView>;
  runList(projectId: string): Promise<string[]>;
  // --- MVP 1 UV review (plan §5 IPC API) ---
  uvInspectLayers(input: { projectId: string; modelPath?: string }): Promise<InspectUvResult>;
  uvSetActiveLayer(input: {
    projectId: string;
    objectName: string;
    uvLayer: string;
  }): Promise<{ status: string; selected_uv_layer: string }>;
  uvReviewExisting(input: {
    projectId: string;
    objectName: string;
    uvLayer: string;
    options?: ReviewOptions;
  }): Promise<{ run_id: string }>;
  uvGetReviewRun(input: { projectId: string; runId: string }): Promise<UvReviewRunView>;
  // --- MVP 2 seam editor (plan §11 Session D IPC API) ---
  seamExportEdgeGeometry(input: {
    projectId: string;
    objectName: string;
  }): Promise<{ run_id: string }>;
  seamExtractUvBoundary(input: {
    projectId: string;
    objectName: string;
    uvLayer?: string;
  }): Promise<{ run_id: string }>;
  seamGetEditorRun(input: { projectId: string; runId: string }): Promise<SeamEditorRunView>;
  seamLoadSpec(input: {
    projectId: string;
    path?: string;
    objectName: string;
    edgeCount?: number | null;
  }): Promise<{ spec: SeamSpec | null; validation: SeamValidation | null; path: string | null }>;
  seamValidateSpec(input: {
    projectId: string;
    spec: SeamSpec;
    objectName: string;
    edgeCount?: number | null;
  }): Promise<SeamValidation>;
  seamSaveSpec(input: {
    projectId: string;
    spec: SeamSpec;
    objectName: string;
    edgeCount?: number | null;
  }): Promise<{ status: string; path: string; validation: SeamValidation }>;
  // --- MVP 3 generate + optimize (plan §11 Session E IPC API) ---
  uvGenerateValidateInput(input: {
    projectId: string;
    mode?: UvGenerateMode;
  }): Promise<ValidateGenerateInput>;
  /** Persist the project's execution mode (work plan §3; gate G2). */
  uvGenerateSetMode(input: { projectId: string; mode: UvGenerateMode }): Promise<Project>;
  uvGenerateStart(input: {
    projectId: string;
    objectName?: string;
    options?: GenerateUvOptions;
    mode?: UvGenerateMode;
  }): Promise<{ run_id: string; mode: UvGenerateMode }>;
  uvGenerateCancel(input: { projectId: string; runId: string }): Promise<{ status: string }>;
  uvGenerateGetRun(input: { projectId: string; runId: string }): Promise<UvGenerateRunView>;
  uvGenerateGetCandidateSummary(input: {
    projectId: string;
    runId: string;
  }): Promise<CandidateSummary | null>;
  // --- MVP 5 production export (plan §12 Session E IPC API) ---
  exportCheckReadiness(input: { projectId: string }): Promise<ExportReadiness>;
  exportStart(input: {
    projectId: string;
    formats: string[];
    options?: ExportOptions;
  }): Promise<StartExportResult>;
  exportCancel(input: { projectId: string; exportId: string }): Promise<{ status: string }>;
  exportGetRun(input: { projectId: string; exportId: string }): Promise<ExportRunView>;
  exportListHistory(input: { projectId: string }): Promise<HistoryEvent[]>;
  exportListRollbackTargets(input: { projectId: string }): Promise<ListRollbackTargetsResult>;
  exportRollback(input: {
    projectId: string;
    targetType: RollbackTargetType;
    targetId: string;
  }): Promise<RollbackResult>;
  exportRevealFile(input: { projectId: string; exportId: string; key: string }): Promise<boolean>;
  settingsGet(): Promise<AppSettings>;
  settingsSet(patch: Partial<AppSettings>): Promise<AppSettings>;
  pickFile(): Promise<string | null>;
  pickProjectDir(): Promise<string | null>;
  /** Native file dialog to pick the Blender executable (OS-aware; resolves a
   *  macOS Blender.app to its inner binary). Returns null if cancelled. */
  pickBlender(): Promise<string | null>;
  // --- Blender launcher + bridge (3D viewer + bridge plan §3, §4) ---
  /** Open the approved working model (.blend preferred, else .fbx) in Blender. */
  blenderOpen(input: { projectId: string }): Promise<BlenderOpenResult>;
  /** Handshake-file + ping status of a running bridge addon session. */
  bridgeStatus(): Promise<BridgeStatus>;
  /** Ask the connected Blender session to refresh the project's working model. */
  bridgeRefresh(input: { projectId: string }): Promise<BridgeRefreshResult>;
  /** Headless-install the bridge addon into the configured Blender. */
  bridgeInstall(): Promise<{ status: string; log: string }>;
  onRunUpdate(cb: (payload: { projectId: string; runId: string }) => void): () => void;
}
