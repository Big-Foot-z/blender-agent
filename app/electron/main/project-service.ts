/**
 * Project folder lifecycle (plan §4 Project Folder Contract).
 *
 * Pure Node (fs/path/crypto) — no `electron` import — so the main-process logic
 * is unit-testable without a renderer or a running app (Session C acceptance).
 */

import { randomUUID } from 'crypto';
import { existsSync, mkdirSync, copyFileSync, readFileSync, writeFileSync, readdirSync } from 'fs';
import { dirname, extname, isAbsolute, join } from 'path';
import {
  SCHEMA_VERSION,
  type MeshRole,
  type Project,
  type RunStatusDoc,
  type Summary,
  type RunView,
  type UvReviewRunView,
  type UvReviewStatusDoc,
  type UvReviewSummary,
  type EdgeGeometry,
  type ExportEdgeGeometryResult,
  type ExtractUvBoundaryResult,
  type SeamEditorRunView,
  type SeamEditorStatusDoc,
  type CandidateSummary,
  type UvGenerateRunView,
  type UvGenerateStatusDoc,
  type UvGenerateSummary,
  type ExportStatusDoc,
  type ExportManifest,
  type ExportMetrics,
  type ExportResult,
  type ValidationReport,
  type ExportRunView,
  type HistoryEvent,
  type ProjectHistory,
  type RollbackTarget,
  type RollbackTargetType,
  type ListRollbackTargetsResult,
  type RollbackResult,
  SUPPORTED_EXPORT_FORMATS,
  resolveGenerateMode,
  UV_FEEDBACK_REL,
  type ArtistApproval,
  type UvFeedback,
  type UvGenerateMode,
} from '@shared/contracts';

function nowIso(): string {
  return new Date().toISOString();
}

export function projectJsonPath(projectDir: string): string {
  return join(projectDir, 'project.json');
}

export function readProject(projectDir: string): Project {
  const raw = JSON.parse(readFileSync(projectJsonPath(projectDir), 'utf-8')) as Project;
  raw.dir = projectDir;
  return raw;
}

export function writeProject(projectDir: string, project: Project): Project {
  const { dir, ...body } = project;
  void dir; // `dir` is runtime-only, never persisted into the file body.
  body.updated_at = nowIso();
  writeFileSync(projectJsonPath(projectDir), JSON.stringify(body, null, 2));
  return { ...body, dir: projectDir };
}

/** Create `<root>/<name>/` with the project folder skeleton and copy the source. */
export function createProject(opts: {
  root: string;
  name: string;
  sourcePath: string;
  role?: MeshRole;
}): Project {
  const { root, name, sourcePath, role } = opts;
  if (!existsSync(sourcePath)) {
    throw new Error(`source model not found: ${sourcePath}`);
  }
  const safeName = name.trim().replace(/[^\w.-]+/g, '_') || 'project';
  let projectDir = join(root, safeName);
  let suffix = 1;
  while (existsSync(projectDir)) {
    projectDir = join(root, `${safeName}_${suffix++}`);
  }
  for (const sub of ['source', 'work', 'runs', 'previews', 'reports']) {
    mkdirSync(join(projectDir, sub), { recursive: true });
  }

  const ext = extname(sourcePath).toLowerCase();
  const sourceRel = join('source', `original${ext}`);
  copyFileSync(sourcePath, join(projectDir, sourceRel));

  const project: Project = {
    schema_version: SCHEMA_VERSION,
    id: `project_${randomUUID()}`,
    name,
    created_at: nowIso(),
    updated_at: nowIso(),
    source_model: sourceRel,
    source_model_role: role ?? null,
    selected_object: null,
    working_model: null,
    working_model_fbx: null,
    approved_lowpoly_run_id: null,
    runs: [],
  };
  return writeProject(projectDir, project);
}

export function openProject(projectDir: string): Project {
  if (!existsSync(projectJsonPath(projectDir))) {
    throw new Error(`not a project folder (no project.json): ${projectDir}`);
  }
  return readProject(projectDir);
}

export function absSourcePath(project: Project): string {
  if (!project.dir || !project.source_model) {
    throw new Error('project has no source model');
  }
  return join(project.dir, project.source_model);
}

export function newRunId(): string {
  return `run_${randomUUID()}`;
}

export function runDir(projectDir: string, runId: string): string {
  return join(projectDir, 'runs', runId);
}

export function ensureRunDir(projectDir: string, runId: string): string {
  const dir = runDir(projectDir, runId);
  mkdirSync(dir, { recursive: true });
  return dir;
}

export function registerRun(projectDir: string, runId: string): Project {
  const project = readProject(projectDir);
  if (!project.runs.includes(runId)) {
    project.runs.push(runId);
  }
  return writeProject(projectDir, project);
}

function readJsonIfExists<T>(path: string): T | null {
  try {
    return JSON.parse(readFileSync(path, 'utf-8')) as T;
  } catch {
    return null;
  }
}

function readTextIfExists(path: string): string {
  try {
    return readFileSync(path, 'utf-8');
  } catch {
    return '';
  }
}

/** Assemble the combined run view the renderer polls (plan §3). */
export function getRunView(projectDir: string, runId: string): RunView {
  const dir = runDir(projectDir, runId);
  const reportNames = [
    'generation_report',
    'validation_report',
    'shape_report',
    'quadflow_report',
    'feature_report',
    'retopo_plan',
  ];
  const reports: Record<string, unknown> = {};
  for (const n of reportNames) {
    const r = readJsonIfExists<unknown>(join(dir, `${n}.json`));
    if (r !== null) reports[n] = r;
  }
  const previewAbs = join(dir, 'preview.png');
  return {
    run_id: runId,
    dir,
    status: readJsonIfExists<RunStatusDoc>(join(dir, 'status.json')),
    summary: readJsonIfExists<Summary>(join(dir, 'summary.json')),
    reports,
    stdout: readTextIfExists(join(dir, 'stdout.log')),
    stderr: readTextIfExists(join(dir, 'stderr.log')),
    preview_path: existsSync(previewAbs) ? previewAbs : null,
  };
}

export function listRuns(projectDir: string): string[] {
  const runsRoot = join(projectDir, 'runs');
  if (!existsSync(runsRoot)) return [];
  return readdirSync(runsRoot, { withFileTypes: true })
    .filter((d) => d.isDirectory())
    .map((d) => d.name);
}

/** Approve a run: copy its low-poly into `work/` and update the manifest (plan §5.3). */
export function approveLowpoly(projectDir: string, runId: string): {
  status: string;
  working_model: string;
  working_model_fbx: string | null;
  approved_lowpoly_run_id: string;
} {
  const dir = runDir(projectDir, runId);
  const srcBlend = join(dir, 'lowpoly.blend');
  if (!existsSync(srcBlend)) {
    throw new Error(`run ${runId} has no lowpoly.blend to approve`);
  }
  const workBlendRel = join('work', 'working_lowpoly.blend');
  copyFileSync(srcBlend, join(projectDir, workBlendRel));

  let workFbxRel: string | null = null;
  const srcFbx = join(dir, 'lowpoly.fbx');
  if (existsSync(srcFbx)) {
    workFbxRel = join('work', 'working_lowpoly.fbx');
    copyFileSync(srcFbx, join(projectDir, workFbxRel));
  }

  const project = readProject(projectDir);
  project.working_model = workBlendRel;
  project.working_model_fbx = workFbxRel;
  project.approved_lowpoly_run_id = runId;
  writeProject(projectDir, project);

  return {
    status: 'accepted',
    working_model: workBlendRel,
    working_model_fbx: workFbxRel,
    approved_lowpoly_run_id: runId,
  };
}

// ===========================================================================
// MVP 1 — UV review (plan §2, §9)
// ===========================================================================

export function newReviewRunId(): string {
  return `review_run_${randomUUID()}`;
}

/**
 * The model the UV review reads, with both absolute and project-relative forms
 * (plan §2 input resolution). Preference order: approved working model (.blend)
 * -> working FBX -> raw source. The relative form is recorded in the summary so
 * the renderer never sees absolute paths (plan §4 normalized JSON).
 */
export function resolveWorkingModel(project: Project): { abs: string; rel: string } {
  if (!project.dir) throw new Error('project has no directory');
  const candidates = [project.working_model, project.working_model_fbx, project.source_model];
  for (const rel of candidates) {
    if (rel && existsSync(join(project.dir, rel))) {
      return { abs: join(project.dir, rel), rel };
    }
  }
  throw new Error('project has no readable working model, working FBX, or source model');
}

/** Append a review run to the manifest and point `latest_uv_review_run_id` at it. */
export function registerReviewRun(projectDir: string, runId: string): Project {
  const project = readProject(projectDir);
  const runs = project.uv_review_runs ?? [];
  if (!runs.includes(runId)) runs.push(runId);
  project.uv_review_runs = runs;
  project.latest_uv_review_run_id = runId;
  return writeProject(projectDir, project);
}

/**
 * Persist the user's UV layer choice on the manifest (plan §5.3). Read-only with
 * respect to the model: only `project.json` changes, never the mesh/UVs.
 */
export function setSelectedUvLayer(
  projectDir: string,
  objectName: string,
  uvLayer: string,
): { status: string; selected_uv_layer: string } {
  const project = readProject(projectDir);
  project.selected_object = objectName;
  project.selected_uv_layer = uvLayer;
  writeProject(projectDir, project);
  return { status: 'accepted', selected_uv_layer: uvLayer };
}

/** Assemble the combined review-run view the renderer polls (plan §9). */
export function getUvReviewRunView(projectDir: string, runId: string): UvReviewRunView {
  const dir = runDir(projectDir, runId);
  const status = readJsonIfExists<UvReviewStatusDoc>(join(dir, 'status.json'));
  const summary = readJsonIfExists<UvReviewSummary>(join(dir, 'uv_review_summary.json'));

  // Turn run-relative artifact filenames into absolute paths for `uvpreview://`.
  const artifact_paths: Record<string, string> = {};
  const artifacts = summary?.artifacts ?? (status?.artifacts as Record<string, string> | undefined);
  if (artifacts) {
    for (const [key, filename] of Object.entries(artifacts)) {
      if (typeof filename === 'string') {
        const abs = join(dir, filename);
        if (existsSync(abs)) artifact_paths[key] = abs;
      }
    }
  }

  return {
    run_id: runId,
    dir,
    status,
    summary,
    stdout: readTextIfExists(join(dir, 'stdout.log')),
    stderr: readTextIfExists(join(dir, 'stderr.log')),
    artifact_paths,
  };
}

// ===========================================================================
// MVP 2 — seam editor (plan §2, §9)
// ===========================================================================

export function newSeamRunId(): string {
  return `seam_run_${randomUUID()}`;
}

/** `<dir>/work/seams`, created on demand — where canonical specs live (plan §2). */
export function seamsDir(projectDir: string): string {
  const dir = join(projectDir, 'work', 'seams');
  mkdirSync(dir, { recursive: true });
  return dir;
}

/** Append a seam-editor run and point `latest_seam_editor_run_id` at it (plan §9). */
export function registerSeamEditorRun(projectDir: string, runId: string): Project {
  const project = readProject(projectDir);
  const runs = project.seam_editor_runs ?? [];
  if (!runs.includes(runId)) runs.push(runId);
  project.seam_editor_runs = runs;
  project.latest_seam_editor_run_id = runId;
  return writeProject(projectDir, project);
}

/**
 * Record the active user seam spec for the MVP 3 handoff (plan §9, §10). The
 * spec path is stored project-relative so the manifest never leaks an absolute
 * path. Persists the selected object too so MVP 3 has model + object + spec.
 */
export function setActiveUserSeamSpec(
  projectDir: string,
  specRel: string,
  objectName?: string,
): Project {
  const project = readProject(projectDir);
  project.active_user_seam_spec = specRel;
  if (objectName) project.selected_object = objectName;
  return writeProject(projectDir, project);
}

/** Assemble the combined seam-editor-run view the renderer polls (plan §9). */
export function getSeamEditorRunView(projectDir: string, runId: string): SeamEditorRunView {
  const dir = runDir(projectDir, runId);
  return {
    run_id: runId,
    dir,
    status: readJsonIfExists<SeamEditorStatusDoc>(join(dir, 'status.json')),
    edge_geometry: readJsonIfExists<EdgeGeometry>(join(dir, 'edge_geometry.json')),
    export_result: readJsonIfExists<ExportEdgeGeometryResult>(join(dir, 'export_result.json')),
    boundary: readJsonIfExists<ExtractUvBoundaryResult>(join(dir, 'boundary_extract_report.json')),
    stdout: readTextIfExists(join(dir, 'stdout.log')),
    stderr: readTextIfExists(join(dir, 'stderr.log')),
  };
}

// ===========================================================================
// MVP 3 — generate + optimize (plan §2, §9)
// ===========================================================================

/** Project-relative handoff paths for the accepted selected UV (plan §2, §9). */
export const SELECTED_UV_BLEND_REL = join('work', 'uv', 'selected_uv.blend');
export const SELECTED_UV_SUMMARY_REL = join('work', 'uv', 'selected_uv_summary.json');
/** Where the worker writes a UV-boundary-derived seam spec (revision plan §3.1). */
export const DERIVED_SEAM_SPEC_REL = join('work', 'seams', 'derived_from_uv_boundary.json');

export function newUvGenerateRunId(): string {
  return `uv_run_${randomUUID()}`;
}

/** `<dir>/work/uv`, created on demand — where the selected UV blend lives (plan §2). */
export function uvWorkDir(projectDir: string): string {
  const dir = join(projectDir, 'work', 'uv');
  mkdirSync(dir, { recursive: true });
  return dir;
}

/**
 * The project's execution mode (work plan §3; gate G2). A legacy project with no
 * `uv_generate_mode` resolves to `preserve_existing`, so opening an old project
 * never switches it to automatic cutting.
 */
export function resolveProjectMode(project: Project): UvGenerateMode {
  return resolveGenerateMode(project.uv_generate_mode ?? null);
}

/** Persist the user's explicit execution-mode choice (work plan §3; gate G2). */
export function setUvGenerateMode(projectDir: string, mode: UvGenerateMode): Project {
  const project = readProject(projectDir);
  project.uv_generate_mode = resolveGenerateMode(mode);
  return writeProject(projectDir, project);
}

/**
 * Record the ARTIST verdict (gate G6/G7).
 *
 * Deliberately writes only `project.uv_artist_approval` — the run's `status.json`
 * and `uv_generate_summary.json` keep the solver verdict untouched, so
 * `solver_accepted` and artist approval stay two separate facts. A rejection must
 * carry a reason, and it is pinned to the run it was made against.
 */
export function setArtistApproval(projectDir: string, approval: ArtistApproval): Project {
  const project = readProject(projectDir);
  const reason = (approval.reason ?? '').trim();
  if (!approval.approved && reason === '') {
    throw new Error('artist_rejection_requires_reason');
  }
  project.uv_artist_approval = {
    approved: approval.approved,
    run_id: approval.run_id ?? project.latest_uv_generate_run_id ?? null,
    reviewer: approval.reviewer ?? null,
    reason: approval.reason ?? null,
    approved_at: nowIso(),
  };
  return writeProject(projectDir, project);
}

/** Absolute path of the saved reviewer feedback file (gate G7). */
export function uvFeedbackPath(projectDir: string): string {
  return join(projectDir, ...UV_FEEDBACK_REL.split('/'));
}

/** Normalize an edge-id list: integers only, de-duplicated, ascending. */
function normalizeEdgeIds(value: unknown): number[] {
  if (!Array.isArray(value)) return [];
  const out = new Set<number>();
  for (const raw of value) {
    const n = Number(raw);
    if (Number.isInteger(n)) out.add(n);
  }
  return [...out].sort((a, b) => a - b);
}

/** Read the saved reviewer feedback, or null when nothing is saved (gate G7). */
export function readUvFeedback(projectDir: string): UvFeedback | null {
  const path = uvFeedbackPath(projectDir);
  if (!existsSync(path)) return null;
  try {
    return JSON.parse(readFileSync(path, 'utf-8')) as UvFeedback;
  } catch {
    return null;
  }
}

/**
 * Merge a feedback patch into the saved file (gate G7).
 *
 * `mesh_fingerprint` is only overwritten when the patch supplies one — it is the
 * key the worker compares before re-applying constraints, so a partial save must
 * never blank it out.
 */
export function saveUvFeedback(projectDir: string, patch: Partial<UvFeedback>): UvFeedback {
  const existing = readUvFeedback(projectDir);
  const next: UvFeedback = {
    schema_version: 1,
    mesh_fingerprint:
      patch.mesh_fingerprint !== undefined
        ? patch.mesh_fingerprint
        : existing?.mesh_fingerprint ?? null,
    object_name: patch.object_name !== undefined ? patch.object_name : existing?.object_name ?? null,
    locked_seam_edges: normalizeEdgeIds(
      patch.locked_seam_edges !== undefined ? patch.locked_seam_edges : existing?.locked_seam_edges,
    ),
    protected_edges: normalizeEdgeIds(
      patch.protected_edges !== undefined ? patch.protected_edges : existing?.protected_edges,
    ),
    preferred_edges: normalizeEdgeIds(
      patch.preferred_edges !== undefined ? patch.preferred_edges : existing?.preferred_edges,
    ),
    front_axis: patch.front_axis !== undefined ? patch.front_axis : existing?.front_axis ?? '',
    notes: patch.notes !== undefined ? patch.notes : existing?.notes ?? '',
    updated_at: nowIso(),
    source_run_id:
      patch.source_run_id !== undefined ? patch.source_run_id : existing?.source_run_id ?? null,
  };
  const path = uvFeedbackPath(projectDir);
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(next, null, 2));
  return next;
}

/** Append a generate run and point `latest_uv_generate_run_id` at it (plan §9). */
export function registerUvGenerateRun(projectDir: string, runId: string): Project {
  const project = readProject(projectDir);
  const runs = project.uv_generate_runs ?? [];
  if (!runs.includes(runId)) runs.push(runId);
  project.uv_generate_runs = runs;
  project.latest_uv_generate_run_id = runId;
  return writeProject(projectDir, project);
}

/**
 * Record the selected UV model + summary for the MVP 4/5 handoff (plan §9). Only
 * called for an ACCEPTED run — a `needs_user_review`/`failed` run never updates
 * these pointers, so the last good selected UV is preserved (plan §6).
 */
export function setSelectedUvModel(
  projectDir: string,
  input: { selectedUvModel?: string | null; selectedUvSummary?: string | null } = {},
): Project {
  const project = readProject(projectDir);
  project.selected_uv_model = input.selectedUvModel ?? SELECTED_UV_BLEND_REL;
  project.selected_uv_summary = input.selectedUvSummary ?? SELECTED_UV_SUMMARY_REL;
  return writeProject(projectDir, project);
}

/**
 * Record the pointer to a UV-boundary-derived seam spec (revision plan §3.1,
 * §4.4). This is informational only — it NEVER overwrites `active_user_seam_spec`
 * (the MVP 2 editor output stays the explicit source of truth, revision plan §2.3).
 */
export function setLatestDerivedSeamSpec(projectDir: string, specRel: string): Project {
  const project = readProject(projectDir);
  project.latest_derived_seam_spec = specRel;
  return writeProject(projectDir, project);
}

/**
 * Read the accepted run's status and, if it shipped, record the selected UV
 * pointers on the manifest (plan §9). Returns the terminal status string (or
 * null if no status yet) so the runner can decide whether to broadcast a change.
 */
export function recordUvGenerateOutcome(projectDir: string, runId: string): string | null {
  const dir = runDir(projectDir, runId);
  const status = readJsonIfExists<UvGenerateStatusDoc>(join(dir, 'status.json'));
  if (!status) return null;
  // Gate G6: ONLY an accepted run may move the approved pointers. A
  // `needs_user_review` / `failed` / `cancelled` / `needs_input` run touches
  // nothing, so the previously approved selected UV survives.
  if (status.status === 'accepted') {
    const summary = readJsonIfExists<UvGenerateSummary>(join(dir, 'uv_generate_summary.json'));
    // Gate G6: the pointer is only swapped once the file it names actually
    // exists on disk — a failed/partial save keeps the prior approved file.
    if (summary?.selected_uv_model) {
      const selectedAbs = isAbsolute(summary.selected_uv_model)
        ? summary.selected_uv_model
        : join(projectDir, summary.selected_uv_model);
      if (existsSync(selectedAbs)) {
        setSelectedUvModel(projectDir, {
          selectedUvModel: summary.selected_uv_model,
          selectedUvSummary: SELECTED_UV_SUMMARY_REL,
        });
      }
    }
    // A derived run records its derived-spec pointer, but never replaces the
    // explicit `active_user_seam_spec` (revision plan §4.4).
    if (summary?.seam_source?.derived && summary.seam_source.path) {
      setLatestDerivedSeamSpec(projectDir, summary.seam_source.path);
    }
  }
  return status.status;
}

/** Assemble the combined generate-run view the renderer polls (plan §9, §4.1). */
export function getUvGenerateRunView(projectDir: string, runId: string): UvGenerateRunView {
  const dir = runDir(projectDir, runId);
  const status = readJsonIfExists<UvGenerateStatusDoc>(join(dir, 'status.json'));
  const summary = readJsonIfExists<UvGenerateSummary>(join(dir, 'uv_generate_summary.json'));

  // Turn run-relative artifact filenames into absolute paths for `uvpreview://`.
  const artifact_paths: Record<string, string> = {};
  const artifacts = summary?.artifacts ?? (status?.artifacts as Record<string, string> | undefined);
  if (artifacts) {
    for (const [key, filename] of Object.entries(artifacts)) {
      if (typeof filename === 'string') {
        const abs = join(dir, filename);
        if (existsSync(abs)) artifact_paths[key] = abs;
      }
    }
  }

  return {
    run_id: runId,
    dir,
    status,
    summary,
    candidate_summary: readJsonIfExists<CandidateSummary>(join(dir, 'candidate_summary.json')),
    p5_gate: readJsonIfExists<Record<string, unknown>>(join(dir, 'p5_gate.json')),
    seam_report: readJsonIfExists<Record<string, unknown>>(join(dir, 'seam_report.json')),
    stdout: readTextIfExists(join(dir, 'stdout.log')),
    stderr: readTextIfExists(join(dir, 'stderr.log')),
    artifact_paths,
  };
}

/** The candidate table source (plan §5) — `candidate_summary.json` or null. */
export function getCandidateSummary(projectDir: string, runId: string): CandidateSummary | null {
  return readJsonIfExists<CandidateSummary>(join(runDir(projectDir, runId), 'candidate_summary.json'));
}

// ===========================================================================
// MVP 5 — production export, history, rollback (plan §2, §6, §8, §9)
// ===========================================================================

/** Project-relative history file (plan §2, §8). */
export const HISTORY_REL = join('history', 'project_history.json');

export function newExportId(): string {
  return `export_${randomUUID()}`;
}

export function newHistoryEventId(): string {
  return `event_${randomUUID()}`;
}

/** `<dir>/exports`, where each export gets an immutable `<export_id>/` folder (plan §2). */
export function exportsRoot(projectDir: string): string {
  return join(projectDir, 'exports');
}

export function exportDir(projectDir: string, exportId: string): string {
  return join(exportsRoot(projectDir), exportId);
}

export function ensureExportDir(projectDir: string, exportId: string): string {
  const dir = exportDir(projectDir, exportId);
  mkdirSync(dir, { recursive: true });
  return dir;
}

export function historyPath(projectDir: string): string {
  return join(projectDir, HISTORY_REL);
}

/** Read `project_history.json`, or an empty-but-valid history when absent (plan §8). */
export function readHistory(projectDir: string): ProjectHistory {
  const h = readJsonIfExists<ProjectHistory>(historyPath(projectDir));
  if (h && Array.isArray(h.events)) return h;
  return { schema_version: SCHEMA_VERSION, events: [] };
}

/**
 * Append an event to the project history (plan §8 — append-only). Never rewrites
 * or deletes prior events; points `project.history` at the file the first time.
 */
export function appendHistoryEvent(projectDir: string, event: HistoryEvent): ProjectHistory {
  mkdirSync(join(projectDir, 'history'), { recursive: true });
  const history = readHistory(projectDir);
  history.events.push(event);
  writeFileSync(historyPath(projectDir), JSON.stringify(history, null, 2));
  const project = readProject(projectDir);
  if (project.history !== HISTORY_REL) {
    project.history = HISTORY_REL;
    writeProject(projectDir, project);
  }
  return history;
}

/** Append an export to the manifest's `exports` list (plan §2). Records the MVP 4 skip. */
export function registerExport(projectDir: string, exportId: string): Project {
  const project = readProject(projectDir);
  const exports = project.exports ?? [];
  if (!exports.includes(exportId)) exports.push(exportId);
  project.exports = exports;
  project.ai_review_skipped = true; // MVP 4 AI Review is skipped (plan §0)
  return writeProject(projectDir, project);
}

function pickExportMetrics(metrics: Record<string, unknown> | undefined | null): ExportMetrics {
  const out: ExportMetrics = {};
  if (!metrics) return out;
  for (const k of [
    'stretch_score',
    'worst_island_distortion',
    'raster_overlap_ratio',
    'texel_density_variance',
    'packing_efficiency',
  ] as const) {
    const v = metrics[k];
    if (typeof v === 'number') out[k] = v;
  }
  return out;
}

/**
 * Read an export's status + manifest and, when it shipped (accepted/partial),
 * pin `latest_export_id` and append an `export_created` history event; a failed
 * export appends `export_failed` (plan §6, §8). Returns the terminal status (or
 * null if no status yet). Never deletes prior exports (plan §9, §15).
 */
export function recordExportOutcome(projectDir: string, exportId: string): string | null {
  const dir = exportDir(projectDir, exportId);
  const status = readJsonIfExists<ExportStatusDoc>(join(dir, 'status.json'));
  if (!status) return null;
  const manifest = readJsonIfExists<ExportManifest>(join(dir, 'export_manifest.json'));
  const result = readJsonIfExists<ExportResult>(join(dir, 'export_result.json'));
  const shipped = status.status === 'accepted' || status.status === 'partial';

  if (shipped && manifest) {
    const project = readProject(projectDir);
    project.latest_export_id = exportId;
    project.ai_review_skipped = true;
    writeProject(projectDir, project);
    appendHistoryEvent(projectDir, {
      id: newHistoryEventId(),
      type: 'export_created',
      created_at: nowIso(),
      export_id: exportId,
      uv_generate_run_id: manifest.source?.uv_generate_run_id ?? null,
      selected_candidate_id: result?.source?.selected_candidate_id ?? null,
      seam_spec: manifest.source?.active_user_seam_spec ?? null,
      manifest: join('exports', exportId, 'export_manifest.json'),
      summary: {
        formats: manifest.formats ?? [],
        status: manifest.status,
        raster_overlap_ratio: manifest.metrics?.raster_overlap_ratio,
        packing_efficiency: manifest.metrics?.packing_efficiency,
      },
    });
  } else if (status.status === 'failed') {
    appendHistoryEvent(projectDir, {
      id: newHistoryEventId(),
      type: 'export_failed',
      created_at: nowIso(),
      export_id: exportId,
      uv_generate_run_id: result?.source?.uv_generate_run_id ?? null,
      selected_candidate_id: result?.source?.selected_candidate_id ?? null,
      manifest: '',
      summary: {
        formats: result?.exports ? Object.keys(result.exports) : [],
        status: 'failed',
      },
    });
  }
  return status.status;
}

/** Assemble the combined export-run view the renderer polls (plan §10). */
export function getExportRunView(projectDir: string, exportId: string): ExportRunView {
  const dir = exportDir(projectDir, exportId);
  const status = readJsonIfExists<ExportStatusDoc>(join(dir, 'status.json'));
  const manifest = readJsonIfExists<ExportManifest>(join(dir, 'export_manifest.json'));
  const validation = readJsonIfExists<ValidationReport>(join(dir, 'validation_report.json'));
  const result = readJsonIfExists<ExportResult>(join(dir, 'export_result.json'));

  // Split the manifest `files` map into renderable previews vs exported models.
  const artifact_paths: Record<string, string> = {};
  const file_paths: Record<string, string> = {};
  const formats = SUPPORTED_EXPORT_FORMATS as readonly string[];
  for (const [key, filename] of Object.entries(manifest?.files ?? {})) {
    if (typeof filename !== 'string') continue;
    const abs = join(dir, filename);
    if (!existsSync(abs)) continue;
    if (formats.includes(key)) file_paths[key] = abs;
    else artifact_paths[key] = abs; // uv_layout / checker_front / checker_side
  }
  for (const f of ['export_manifest.json', 'validation_report.json']) {
    const abs = join(dir, f);
    if (existsSync(abs)) artifact_paths[f.replace('.json', '')] = abs;
  }

  return {
    export_id: exportId,
    dir,
    status,
    manifest,
    validation,
    result,
    stdout: readTextIfExists(join(dir, 'stdout.log')),
    stderr: readTextIfExists(join(dir, 'stderr.log')),
    artifact_paths,
    file_paths,
  };
}

export function listExports(projectDir: string): string[] {
  const root = exportsRoot(projectDir);
  if (!existsSync(root)) return [];
  return readdirSync(root, { withFileTypes: true })
    .filter((d) => d.isDirectory())
    .map((d) => d.name);
}

/** List rollback targets: accepted UV runs + prior exports, newest first (plan §9.1). */
export function listRollbackTargets(projectDir: string): ListRollbackTargetsResult {
  const project = readProject(projectDir);
  const targets: RollbackTarget[] = [];

  for (const runId of (project.uv_generate_runs ?? []).slice().reverse()) {
    const rdir = runDir(projectDir, runId);
    const summary = readJsonIfExists<UvGenerateSummary>(join(rdir, 'uv_generate_summary.json'));
    if (summary?.status !== 'accepted') continue; // only accepted UV runs can be restored
    const status = readJsonIfExists<UvGenerateStatusDoc>(join(rdir, 'status.json'));
    const runBlendRel = join('runs', runId, 'selected_uv.blend');
    targets.push({
      id: runId,
      type: 'uv_run',
      created_at: status?.finished_at ?? status?.started_at ?? null,
      selected_uv_model: existsSync(join(projectDir, runBlendRel))
        ? runBlendRel
        : summary?.selected_uv_model ?? null,
      selected_candidate_id: summary?.selected_candidate_id ?? null,
      metrics: pickExportMetrics(summary?.metrics as Record<string, unknown> | undefined),
    });
  }

  for (const exportId of (project.exports ?? []).slice().reverse()) {
    const manifest = readJsonIfExists<ExportManifest>(
      join(exportDir(projectDir, exportId), 'export_manifest.json'),
    );
    if (!manifest) continue;
    targets.push({
      id: exportId,
      type: 'export',
      created_at: manifest.created_at ?? null,
      manifest: join('exports', exportId, 'export_manifest.json'),
      formats: manifest.formats ?? [],
      status: manifest.status,
    });
  }

  return { schema_version: SCHEMA_VERSION, status: 'accepted', targets };
}

/**
 * Roll the project state back to a previous UV run or export (plan §9.2).
 *
 * Rollback updates project pointers only; it copies a UV run's immutable
 * `selected_uv.blend`/summary back into `work/uv/` (does not mutate the run), and
 * for an export just re-pins `latest_export_id`. Newer exports/runs are never
 * deleted, and a `rollback_performed` event is appended (plan §9 rules, §15).
 */
export function rollbackProjectState(
  projectDir: string,
  input: { targetType: RollbackTargetType; targetId: string },
): RollbackResult {
  const { targetType, targetId } = input;
  const eventId = newHistoryEventId();

  if (targetType === 'uv_run') {
    const rdir = runDir(projectDir, targetId);
    const srcBlend = join(rdir, 'selected_uv.blend');
    if (!existsSync(srcBlend)) {
      throw new Error(`uv run ${targetId} has no selected_uv.blend to roll back to`);
    }
    uvWorkDir(projectDir);
    copyFileSync(srcBlend, join(projectDir, SELECTED_UV_BLEND_REL));
    const srcSummary = join(rdir, 'uv_generate_summary.json');
    if (existsSync(srcSummary)) {
      const summary = JSON.parse(readFileSync(srcSummary, 'utf-8'));
      writeFileSync(
        join(projectDir, SELECTED_UV_SUMMARY_REL),
        JSON.stringify({ ...summary, source_run_id: targetId }, null, 2),
      );
    }
    const project = readProject(projectDir);
    project.selected_uv_model = SELECTED_UV_BLEND_REL;
    project.selected_uv_summary = SELECTED_UV_SUMMARY_REL;
    project.latest_uv_generate_run_id = targetId;
    writeProject(projectDir, project);
    appendHistoryEvent(projectDir, {
      id: eventId,
      type: 'rollback_performed',
      created_at: nowIso(),
      target_type: 'uv_run',
      target_id: targetId,
      selected_uv_model: SELECTED_UV_BLEND_REL,
      selected_uv_summary: SELECTED_UV_SUMMARY_REL,
    });
    return {
      schema_version: SCHEMA_VERSION,
      status: 'accepted',
      rolled_back_to: {
        type: 'uv_run',
        id: targetId,
        selected_uv_model: SELECTED_UV_BLEND_REL,
        selected_uv_summary: SELECTED_UV_SUMMARY_REL,
      },
      history_event: eventId,
    };
  }

  if (targetType === 'export') {
    const edir = exportDir(projectDir, targetId);
    if (!existsSync(join(edir, 'export_manifest.json'))) {
      throw new Error(`export ${targetId} has no manifest to roll back to`);
    }
    const project = readProject(projectDir);
    project.latest_export_id = targetId; // re-pin; newer exports are NOT deleted (plan §9)
    writeProject(projectDir, project);
    appendHistoryEvent(projectDir, {
      id: eventId,
      type: 'rollback_performed',
      created_at: nowIso(),
      target_type: 'export',
      target_id: targetId,
    });
    return {
      schema_version: SCHEMA_VERSION,
      status: 'accepted',
      rolled_back_to: { type: 'export', id: targetId, latest_export_id: targetId },
      history_event: eventId,
    };
  }

  throw new Error(`unknown rollback target type: ${targetType}`);
}
