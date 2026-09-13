/**
 * Blender UV generate + optimize worker orchestration (plan §4, §10, §11 Session E).
 *
 * Spawns headless Blender for `generate_uv_from_seams`, captures stdout/stderr to
 * the run folder, drives the `status.json` lifecycle, and — on an ACCEPTED run —
 * records the selected UV pointers on the manifest (plan §6, §9). A mock runner
 * fabricates a deterministic accepted run (candidate table + before/after
 * previews) so the app + renderer + e2e smoke work without a Blender install
 * (plan §11 "mock worker로 선개발 가능").
 *
 * `validateInput` is a cheap pure-Node pre-flight (does the working model +
 * active seam spec exist, does the spec object match the selected object) — the
 * deep edge-id validation runs inside the worker (plan §6). MVP 3 never
 * overwrites the source working model or the user seam spec (plan §1, §6, §14).
 *
 * No `electron` import — constructed with explicit config so it is unit-testable.
 */

import { ChildProcess, spawn } from 'child_process';
import {
  createWriteStream,
  existsSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
} from 'fs';
import { dirname, isAbsolute, join } from 'path';
import {
  UvGenerateCommand,
  UvGenerateMode,
  UvGenerateRunStatus,
  mergeGenerateOptions,
  validateModeRequest,
  DEFAULT_SEAM_SOURCE_POLICY,
  MISSING_SEAM_SOURCE_CODE,
  MISSING_SEAM_SOURCE_MESSAGE,
  SeamSourceType,
  UV_FEEDBACK_REL,
  type BlenderVersionCheck,
  type GenerateUvOptions,
  type SeamSourceKind,
  type SeamSpec,
  type ValidateGenerateInput,
  type ValidateGenerateIssue,
} from '@shared/contracts';
import {
  ensureRunDir,
  newUvGenerateRunId,
  readProject,
  recordUvGenerateOutcome,
  registerUvGenerateRun,
  resolveProjectMode,
  resolveWorkingModel,
  setUvGenerateMode,
  writeProject,
  uvFeedbackPath,
  uvWorkDir,
  DERIVED_SEAM_SPEC_REL,
  SELECTED_UV_BLEND_REL,
  SELECTED_UV_SUMMARY_REL,
} from './project-service';
import { checkBlenderVersion } from './blenderVersion';

export interface UvGenerateWorkerConfig {
  blenderPath: string | null;
  workerRoot: string; // absolute path to repo `worker/`
  mock?: boolean; // force the mock runner (tests / no Blender)
  onRunUpdate?: (projectId: string, runId: string) => void;
  /** Gate G9: supported-Blender-version gate; injectable for tests. */
  versionChecker?: (path: string) => BlenderVersionCheck;
}

export class UvGenerateRunner {
  private running = new Map<string, ChildProcess>();

  constructor(private cfg: UvGenerateWorkerConfig) {}

  /** Pick up a changed Blender path without dropping the live `running` map. */
  setBlenderPath(path: string | null): void {
    this.cfg.blenderPath = path;
  }

  private useMock(): boolean {
    return this.cfg.mock === true || this.cfg.blenderPath === null;
  }

  private blenderArgs(after: string[]): string[] {
    return ['--background', '--python', join(this.cfg.workerRoot, 'generate_uv_from_seams.py'), '--', ...after];
  }

  // --- validateInput (plan §8 "Validate Seam Spec") ----------------------
  /** Cheap pure-Node readiness check for a Generate run (plan §6 pre-flight). */
  validateInput(projectDir: string, mode?: UvGenerateMode): ValidateGenerateInput {
    const project = readProject(projectDir);
    // Gate G2: readiness is mode-dependent — `auto_generate` needs no seam source.
    const resolvedMode = mode ?? resolveProjectMode(project);
    const issues: ValidateGenerateIssue[] = [];

    let modelRel: string | null = null;
    try {
      modelRel = resolveWorkingModel(project).rel;
    } catch {
      issues.push({ code: 'no_working_model', message: 'Project has no readable working model.' });
    }

    const objectName = project.selected_object ?? null;
    const specRel = project.active_user_seam_spec ?? null;
    const selectedUvLayer = project.selected_uv_layer ?? null;
    let specObject: string | null = null;
    let userSeamCount: number | null = null;
    let userProtectedCount: number | null = null;
    let objectMismatch = false;
    // 'explicit' the moment an active-spec FILE exists (even if it later proves
    // broken/mismatched — an explicit spec still takes precedence, revision plan
    // §7). UV-boundary fallback only applies when no usable spec file is present.
    let seamSource: SeamSourceKind = 'missing';

    if (specRel) {
      const specAbs = isAbsolute(specRel) ? specRel : join(projectDir, specRel);
      if (!existsSync(specAbs)) {
        // Configured spec file is gone — recoverable via UV fallback, non-blocking.
        issues.push({ code: 'seam_spec_missing', message: `Seam spec file not found: ${specRel}` });
      } else {
        seamSource = 'explicit';
        try {
          const spec = JSON.parse(readFileSync(specAbs, 'utf-8')) as SeamSpec;
          specObject = spec.object ?? null;
          userSeamCount = Array.isArray(spec.user_seam_edges) ? spec.user_seam_edges.length : 0;
          userProtectedCount = Array.isArray(spec.user_protected_edges)
            ? spec.user_protected_edges.length
            : 0;
          objectMismatch = !!(objectName && specObject && specObject !== objectName);
          if (objectMismatch) {
            issues.push({
              code: 'object_mismatch',
              message: `Seam spec object "${specObject}" does not match selected object "${objectName}".`,
            });
          }
          if (userSeamCount === 0) {
            issues.push({ code: 'empty_seam_spec', message: 'Seam spec has no user seam edges.' });
          }
        } catch (err) {
          issues.push({ code: 'invalid_seam_spec', message: `Could not parse seam spec: ${String(err)}` });
        }
      }
    }

    // UV-boundary fallback when there is no usable explicit spec file
    // (revision plan §1 case 2/3, §4.4): a selected UV layer is a valid seam
    // source; nothing at all is `needs_input`.
    if (seamSource !== 'explicit') {
      if (selectedUvLayer) {
        seamSource = 'derived';
      } else {
        seamSource = 'missing';
        // Gate G2: in `auto_generate` a missing seam source is NOT an issue — the
        // solver cuts from scratch. The kind is still reported as `missing`.
        if (resolvedMode !== UvGenerateMode.AutoGenerate) {
          issues.push({ code: MISSING_SEAM_SOURCE_CODE, message: MISSING_SEAM_SOURCE_MESSAGE });
        }
      }
    }

    // `empty_seam_spec` / `seam_spec_missing` are non-blocking: an empty explicit
    // spec still runs (matches prior behavior) and a missing-file spec falls back
    // to the UV boundary. Mismatch / invalid / missing-source block (revision §4.5).
    const NON_BLOCKING = new Set(['empty_seam_spec', 'seam_spec_missing']);
    const ready = issues.filter((i) => !NON_BLOCKING.has(i.code)).length === 0;
    return {
      ready,
      model: modelRel,
      object_name: objectName,
      seam_spec: specRel,
      spec_object: specObject,
      user_seam_count: userSeamCount,
      user_protected_count: userProtectedCount,
      object_mismatch: objectMismatch,
      seam_source: seamSource,
      selected_uv_layer: selectedUvLayer,
      mode: resolvedMode,
      issues,
    };
  }

  // --- generate_uv_from_seams (plan §4.1) --------------------------------
  /** Kick off a generate+optimize run asynchronously; returns the run id. */
  start(
    projectId: string,
    projectDir: string,
    input: { objectName?: string; options?: GenerateUvOptions; mode?: UvGenerateMode },
  ): { run_id: string; mode: UvGenerateMode } {
    // Gate G9: an unsupported / unreadable Blender fails BEFORE any run
    // directory exists, so a rejected request leaves no run behind.
    if (!this.useMock() && this.cfg.blenderPath) {
      const check = (this.cfg.versionChecker ?? checkBlenderVersion)(this.cfg.blenderPath);
      if (!check.ok) {
        throw Object.assign(new Error(check.message), { code: check.code, check });
      }
    }
    const project = readProject(projectDir);
    // Gate G2: the mode is decided (explicit argument > options.mode > project)
    // and a self-contradicting flag combination is rejected BEFORE any run
    // directory exists, so a rejected request leaves no run behind.
    const requestedMode = input.mode ?? input.options?.mode ?? resolveProjectMode(project);
    const validation = validateModeRequest(requestedMode, input.options);
    if (!validation.ok) {
      const first = validation.errors[0];
      throw Object.assign(new Error(first.message), {
        code: first.code,
        errors: validation.errors,
      });
    }
    const mode = validation.mode as UvGenerateMode;
    const { abs: modelAbs, rel: modelRel } = resolveWorkingModel(project);
    const objectName = input.objectName ?? project.selected_object ?? '';
    // Gate G2/G9: a project imported straight from a UV-less low-poly has no
    // `selected_object` yet. An explicit object travels with the run, so record
    // it on the project too — the run + manifest must agree.
    if (input.objectName && project.selected_object !== input.objectName) {
      project.selected_object = input.objectName;
      writeProject(projectDir, project);
    }
    // Seam source: an explicit spec FILE wins; else the worker derives one from
    // the selected UV layer boundary (revision plan §1, §4.4). `seam_spec` is null
    // when no usable spec file exists so the worker falls back to `uv_layer`.
    const specRel = project.active_user_seam_spec ?? null;
    const specAbs = specRel ? (isAbsolute(specRel) ? specRel : join(projectDir, specRel)) : null;
    const hasSpec = !!(specAbs && existsSync(specAbs));
    const uvLayer = project.selected_uv_layer ?? null;

    const runId = newUvGenerateRunId();
    const dir = ensureRunDir(projectDir, runId);
    registerUvGenerateRun(projectDir, runId);
    // The chosen mode is persisted on the project so UI -> IPC -> worker ->
    // report all agree on one mode (gate G2).
    setUvGenerateMode(projectDir, mode);
    uvWorkDir(projectDir); // ensure work/uv exists for the handoff copy

    // Gate G7: saved reviewer feedback (locks/protected/preferred) travels with
    // the job. The worker compares `mesh_fingerprint` before applying it — a
    // topology change must never silently re-use stale edge ids.
    const feedbackAbs = uvFeedbackPath(projectDir);
    const hasFeedback = existsSync(feedbackAbs);

    const job = {
      command: UvGenerateCommand.GenerateUvFromSeams,
      project_id: projectId,
      run_id: runId,
      model: modelAbs,
      model_rel: modelRel,
      object_name: objectName,
      seam_spec: hasSpec ? specAbs : null,
      seam_spec_rel: hasSpec ? specRel : null,
      uv_layer: uvLayer,
      selected_uv_layer: uvLayer,
      seam_source_policy: DEFAULT_SEAM_SOURCE_POLICY,
      mode,
      options: mergeGenerateOptions(input.options, mode),
      out_dir: dir,
      selected_blend_out: join(projectDir, SELECTED_UV_BLEND_REL),
      selected_blend_out_rel: SELECTED_UV_BLEND_REL,
      selected_summary_out: join(projectDir, SELECTED_UV_SUMMARY_REL),
      derived_seam_spec_out: join(projectDir, DERIVED_SEAM_SPEC_REL),
      derived_seam_spec_out_rel: DERIVED_SEAM_SPEC_REL,
      feedback: hasFeedback ? feedbackAbs : null,
      feedback_rel: hasFeedback ? UV_FEEDBACK_REL : null,
    };
    writeFileSync(join(dir, 'job.json'), JSON.stringify(job, null, 2));
    writeQueuedStatus(dir, runId, modelRel, objectName, hasSpec ? specRel ?? '' : '', mode);

    const finish = () => {
      this.running.delete(runId);
      try {
        recordUvGenerateOutcome(projectDir, runId);
      } catch {
        /* manifest update is best-effort; the run view still reads status.json */
      }
      this.cfg.onRunUpdate?.(projectId, runId);
    };

    if (this.useMock()) {
      setTimeout(() => {
        try {
          mockGenerate(dir, runId, projectDir, job);
        } catch (err) {
          writeFailedStatus(dir, runId, modelRel, objectName, specRel ?? '', String(err), mode);
        }
        finish();
      }, 10);
      return { run_id: runId, mode };
    }

    const child = this.run(
      this.cfg.blenderPath as string,
      this.blenderArgs(['--job', join(dir, 'job.json')]),
      dir,
      (err) => writeFailedStatus(dir, runId, modelRel, objectName, specRel ?? '', String(err), mode),
      finish,
    );
    if (child) this.running.set(runId, child);
    return { run_id: runId, mode };
  }

  /** Cancel a running generate job (plan §8 "cancel running job"). */
  cancel(projectDir: string, runId: string): { status: string } {
    const child = this.running.get(runId);
    if (child) {
      child.kill();
      this.running.delete(runId);
    }
    const dir = ensureRunDir(projectDir, runId);
    if (existsSync(join(dir, 'status.json'))) {
      try {
        const status = JSON.parse(readFileSync(join(dir, 'status.json'), 'utf-8'));
        // Gate G6: never overwrite a terminal status — a run that already
        // finished (or already ended as needs_input/cancelled) keeps its verdict
        // and the previously approved selected UV stays untouched.
        if (
          !['accepted', 'needs_user_review', 'failed', 'needs_input', 'cancelled'].includes(
            status.status,
          )
        ) {
          status.status = UvGenerateRunStatus.Cancelled;
          status.finished_at = new Date().toISOString();
          writeFileSync(join(dir, 'status.json'), JSON.stringify(status, null, 2));
        }
      } catch {
        /* leave the on-disk status as-is if it cannot be parsed */
      }
    }
    return { status: 'cancelled' };
  }

  /** Spawn a process, tee stdout/stderr to the run folder; returns the child. */
  private run(
    cmd: string,
    args: string[],
    logDir: string,
    onError: (err: unknown) => void,
    onDone: () => void,
  ): ChildProcess | null {
    let child: ChildProcess;
    try {
      child = spawn(cmd, args, { stdio: ['ignore', 'pipe', 'pipe'] });
    } catch (err) {
      onError(err);
      onDone();
      return null;
    }
    const out = createWriteStream(join(logDir, 'stdout.log'));
    const errStream = createWriteStream(join(logDir, 'stderr.log'));
    child.stdout?.on('data', (d) => out.write(d));
    child.stderr?.on('data', (d) => errStream.write(d));
    child.on('error', (err) => {
      out.end();
      errStream.end();
      onError(err);
      onDone();
    });
    child.on('close', () => {
      out.end();
      errStream.end();
      onDone();
    });
    return child;
  }
}

// ---------------------------------------------------------------------------
// status.json helpers (plan §9)
// ---------------------------------------------------------------------------
function statusInput(
  modelRel: string,
  objectName: string,
  specRel: string,
  mode?: UvGenerateMode,
): Record<string, unknown> {
  // Gate G2: the mode travels with the run record, not just the job.
  return { model: modelRel, object_name: objectName, seam_spec: specRel, mode: mode ?? null };
}

function writeQueuedStatus(
  dir: string,
  runId: string,
  modelRel: string,
  objectName: string,
  specRel: string,
  mode?: UvGenerateMode,
): void {
  writeFileSync(
    join(dir, 'status.json'),
    JSON.stringify(
      {
        schema_version: 1,
        run_id: runId,
        command: UvGenerateCommand.GenerateUvFromSeams,
        status: UvGenerateRunStatus.Queued,
        started_at: new Date().toISOString(),
        finished_at: null,
        input: statusInput(modelRel, objectName, specRel, mode),
        artifacts: {},
        error: null,
      },
      null,
      2,
    ),
  );
}

function writeFailedStatus(
  dir: string,
  runId: string,
  modelRel: string,
  objectName: string,
  specRel: string,
  message: string,
  mode?: UvGenerateMode,
): void {
  writeFileSync(
    join(dir, 'status.json'),
    JSON.stringify(
      {
        schema_version: 1,
        run_id: runId,
        command: UvGenerateCommand.GenerateUvFromSeams,
        status: UvGenerateRunStatus.Failed,
        started_at: new Date().toISOString(),
        finished_at: new Date().toISOString(),
        input: statusInput(modelRel, objectName, specRel, mode),
        artifacts: {},
        error: { code: 'spawn_failed', message },
      },
      null,
      2,
    ),
  );
}

// ---------------------------------------------------------------------------
// Mock runner — fabricates a deterministic accepted generate run without Blender.
// Shapes mirror the plan §4.1 / §5 examples so the renderer + e2e smoke work.
// ---------------------------------------------------------------------------
function mockGenerate(dir: string, runId: string, projectDir: string, job: any): void {
  mkdirSync(dir, { recursive: true });
  const objectName = job.object_name ?? 'SM_Test_Pottery_a_02';
  const hasSpec = !!job.seam_spec;
  const uvLayer = job.uv_layer ?? job.selected_uv_layer ?? null;
  const policy = job.seam_source_policy ?? DEFAULT_SEAM_SOURCE_POLICY;
  // Execution mode (work plan §3; gate G2). `auto_generate` cuts from scratch,
  // so it never stops at `needs_input` for a missing seam source.
  const mode: UvGenerateMode =
    job.mode ?? job.options?.mode ?? UvGenerateMode.PreserveExisting;
  const auto = mode === UvGenerateMode.AutoGenerate;
  // Tests only (gate G6): force a non-accepted terminal status.
  const mockStatus: string | null = job.options?.mock_status ?? null;

  // No seam source at all -> needs_input in preserve mode only
  // (revision plan §1 case 3, §4.2; gate G2 auto exemption).
  if (!auto && !hasSpec && !uvLayer) {
    writeFileSync(
      join(dir, 'seam_source_resolution.json'),
      JSON.stringify({ policy, kind: 'needs_input', seam_spec: null, uv_layer: null }, null, 2),
    );
    writeFileSync(
      join(dir, 'status.json'),
      JSON.stringify(
        {
          schema_version: 1,
          run_id: runId,
          command: UvGenerateCommand.GenerateUvFromSeams,
          status: UvGenerateRunStatus.NeedsInput,
          started_at: new Date().toISOString(),
          finished_at: new Date().toISOString(),
          input: statusInput(job.model_rel ?? '', objectName, '', mode),
          artifacts: { seam_source_resolution: 'seam_source_resolution.json' },
          error: { code: MISSING_SEAM_SOURCE_CODE, message: MISSING_SEAM_SOURCE_MESSAGE },
        },
        null,
        2,
      ),
    );
    return;
  }

  // Derived when there is no explicit spec file but a selected UV layer exists.
  // In `auto_generate` with neither, the summary reports a null seam source.
  const derived = !hasSpec && !!uvLayer;
  const noSource = !hasSpec && !uvLayer;
  const seamSource: Record<string, unknown> | null = noSource
    ? null
    : derived
    ? {
        type: SeamSourceType.UvBoundaryDerived,
        path: job.derived_seam_spec_out_rel ?? DERIVED_SEAM_SPEC_REL,
        uv_layer: uvLayer,
        user_confirmed: false,
        derived: true,
      }
    : {
        type: SeamSourceType.UserSeamSpec,
        path: job.seam_spec_rel ?? null,
        uv_layer: null,
        user_confirmed: true,
        derived: false,
      };
  const specSeamCount = readMockSeamCount(job.seam_spec) ?? 0;
  const userSeamCount = (derived ? null : readMockSeamCount(job.seam_spec)) ?? 1230;

  // A derived run writes its spec separately (revision plan §4.1) — canonical
  // work/seams copy + a run-folder copy — and a resolution report.
  if (derived) {
    const derivedSpec = {
      version: 1,
      object: objectName,
      mode: 'user_seams',
      mandatory_fold_angle: 90.0,
      user_seam_edges: Array.from({ length: userSeamCount }, (_, i) => i),
      user_protected_edges: [],
      chapters: [],
      notes: `Derived from UV island boundaries: ${uvLayer}`,
    };
    if (job.derived_seam_spec_out) {
      mkdirSync(dirname(job.derived_seam_spec_out), { recursive: true });
      writeFileSync(job.derived_seam_spec_out, JSON.stringify(derivedSpec, null, 2));
    }
    writeFileSync(join(dir, 'derived_from_uv_boundary.json'), JSON.stringify(derivedSpec, null, 2));
  }
  writeFileSync(
    join(dir, 'seam_source_resolution.json'),
    JSON.stringify(
      {
        policy,
        kind: seamSource ? seamSource.type : 'auto_generate_no_source',
        uv_layer: derived ? uvLayer : null,
        seam_spec: derived || !seamSource ? null : seamSource.path,
      },
      null,
      2,
    ),
  );

  const selectedMetrics = {
    stretch_score: 0.06866,
    worst_island_distortion: 0.202999,
    raster_overlap_ratio: 0.0,
    overlap_ratio: 0.0,
    texel_density_variance: 0.000002,
    packing_efficiency: 0.591278,
    island_count: 52,
    uv_bounds_ok: true,
  };
  const baselineMetrics = { ...selectedMetrics, packing_efficiency: 0.583109 };

  const candidateSummary = {
    schema_version: 1,
    baseline_candidate_id: 'slim_concave_m005',
    selected_candidate_id: 'slim_concave_m002',
    kept_baseline: false,
    score_weights: {
      stretch_score: 4.0,
      worst_island_distortion: 3.0,
      texel_density_variance: 2.0,
      raster_overlap_ratio: 2.0,
      overlap_ratio: 1.0,
      packing_efficiency: -1.5,
      small_island_ratio: 0.2,
    },
    candidates: [
      {
        id: 'slim_concave_m002',
        unwrap_method: 'MINIMUM_STRETCH',
        minimize_iters: 0,
        margin: 0.002,
        pack_shape: 'CONCAVE',
        rotate: true,
        average_scale: true,
        accepted: true,
        reason: 'best_score',
        score: -0.003276,
        metrics: selectedMetrics,
      },
      {
        id: 'slim_concave_m005',
        unwrap_method: 'MINIMUM_STRETCH',
        minimize_iters: 0,
        margin: 0.005,
        pack_shape: 'CONCAVE',
        rotate: true,
        average_scale: true,
        accepted: true,
        reason: '',
        score: -0.0031,
        metrics: baselineMetrics,
      },
      {
        id: 'abf_aabb_m010_min30',
        unwrap_method: 'ANGLE_BASED',
        minimize_iters: 30,
        margin: 0.01,
        pack_shape: 'AABB',
        rotate: true,
        average_scale: true,
        accepted: false,
        reason: 'raster_overlap',
        score: 0.5,
        metrics: { ...selectedMetrics, raster_overlap_ratio: 0.02 },
      },
    ],
    rejected: [{ id: 'abf_aabb_m010_min30', reason: 'raster_overlap' }],
  };
  writeFileSync(join(dir, 'candidate_summary.json'), JSON.stringify(candidateSummary, null, 2));

  writeFileSync(
    join(dir, 'p5_gate.json'),
    JSON.stringify(
      {
        engine: 'chart',
        mode: 'user_seams',
        chart_count: 52,
        metrics: selectedMetrics,
        gate: { verdict: 'pass', failures: [] },
        user_seams: {
          mode: 'user_seams',
          user_seam_count: userSeamCount,
          user_protected_count: 0,
          final_seam_count: userSeamCount,
          auto_added_seams: 0,
        },
        seam_count: userSeamCount,
      },
      null,
      2,
    ),
  );
  writeFileSync(
    join(dir, 'seam_report.json'),
    JSON.stringify({ mode: 'user_seams', note: 'mock seam report' }, null, 2),
  );

  for (const png of [
    'baseline_uv_layout.png',
    'baseline_checker_front.png',
    'baseline_checker_side.png',
    'selected_uv_layout.png',
    'selected_checker_front.png',
    'selected_checker_side.png',
  ]) {
    writeFileSync(join(dir, png), MOCK_PNG);
  }
  writeFileSync(join(dir, 'selected_uv.blend'), 'MOCK-BLEND');

  const artifacts: Record<string, string> = {
    summary: 'uv_generate_summary.json',
    p5_gate: 'p5_gate.json',
    seam_report: 'seam_report.json',
    candidate_summary: 'candidate_summary.json',
    seam_source_resolution: 'seam_source_resolution.json',
    baseline_uv_layout: 'baseline_uv_layout.png',
    baseline_checker_front: 'baseline_checker_front.png',
    baseline_checker_side: 'baseline_checker_side.png',
    selected_uv_layout: 'selected_uv_layout.png',
    selected_checker_front: 'selected_checker_front.png',
    selected_checker_side: 'selected_checker_side.png',
    selected_blend: 'selected_uv.blend',
  };
  if (derived) artifacts.derived_seam_spec = 'derived_from_uv_boundary.json';

  // Gate G15 reviewer evidence: the raw game-gate report, the merge-back trial
  // log, the flat seam-overlay image and the shading policy. Written in
  // `auto_generate` only — a preserve run neither cuts nor merges, so it has no
  // merge-back history and no automatic-cut report to show.
  if (auto) {
    writeFileSync(join(dir, 'quality_report.json'), JSON.stringify(MOCK_QUALITY_REPORT, null, 2));
    writeFileSync(join(dir, 'merge_back_history.json'), JSON.stringify(MOCK_MERGE_BACK_HISTORY, null, 2));
    writeFileSync(join(dir, 'shading_policy.json'), JSON.stringify(MOCK_SHADING_POLICY, null, 2));
    writeFileSync(join(dir, 'seam_overlay.png'), MOCK_PNG);
    artifacts.quality_report = 'quality_report.json';
    artifacts.merge_back_history = 'merge_back_history.json';
    artifacts.shading_policy = 'shading_policy.json';
    artifacts.seam_overlay_png = 'seam_overlay.png';
  }

  // Gate G6: a forced non-accepted mock run produces NO handoff and no pointer.
  const runStatus: string =
    mockStatus === 'needs_user_review'
      ? UvGenerateRunStatus.NeedsUserReview
      : mockStatus === 'failed'
      ? UvGenerateRunStatus.Failed
      : UvGenerateRunStatus.Accepted;
  const shipped = runStatus === UvGenerateRunStatus.Accepted;

  const summary = {
    schema_version: 1,
    run_id: runId,
    command: UvGenerateCommand.GenerateUvFromSeams,
    status: runStatus,
    model: job.model_rel ?? null,
    object_name: objectName,
    seam_spec: derived ? seamSource!.path : (job.seam_spec_rel ?? null),
    seam_source: seamSource,
    selected_candidate_id: 'slim_concave_m002',
    selected_uv_model: shipped ? SELECTED_UV_BLEND_REL : null,
    metrics: selectedMetrics,
    seam_integrity: {
      user_seam_count: userSeamCount,
      user_protected_count: 0,
      final_seam_count: userSeamCount,
      auto_added_seams: 0,
      mandatory_rule_enabled: false,
      mandatory_gate_enabled: false,
      valid: true,
    },
    layout_optimization: {
      enabled: true,
      selected_candidate_id: 'slim_concave_m002',
      kept_baseline: false,
      candidate_count: 3,
      score_before: -0.0031,
      score_after: -0.003276,
      packing_efficiency_before: 0.583109,
      packing_efficiency_after: 0.591278,
      stretch_before: 0.06866,
      stretch_after: 0.06866,
    },
    artifacts,
    warnings: ['mock generate: not a real Blender run'],
    // --- Automation report blocks (work plan §3; gates G2/G6) --------------
    mode,
    solver_accepted: shipped,
    artist_approved: false,
    ...(auto ? autoReportBlocks(specSeamCount) : preserveReportBlocks()),
    ...(auto ? mockFeedbackBlock(job.feedback) : {}),
  };
  writeFileSync(join(dir, 'uv_generate_summary.json'), JSON.stringify(summary, null, 2));

  // Handoff copies (an accepted run ships to work/uv, plan §6, §9). Gate G6: a
  // needs_user_review / failed run copies NOTHING, so the prior approved file
  // stays byte-identical.
  if (shipped) {
    const uvDir = uvWorkDir(projectDir);
    writeFileSync(join(uvDir, 'selected_uv.blend'), 'MOCK-BLEND');
    writeFileSync(
      join(projectDir, SELECTED_UV_SUMMARY_REL),
      JSON.stringify({ ...summary, source_run_id: runId }, null, 2),
    );
  }

  writeFileSync(
    join(dir, 'status.json'),
    JSON.stringify(
      {
        schema_version: 1,
        run_id: runId,
        command: UvGenerateCommand.GenerateUvFromSeams,
        status: runStatus,
        started_at: new Date().toISOString(),
        finished_at: new Date().toISOString(),
        input: statusInput(job.model_rel ?? '', objectName, job.seam_spec_rel ?? '', mode),
        artifacts,
        error: shipped ? null : { code: `mock_${runStatus}`, message: `mock generate: forced ${runStatus}` },
      },
      null,
      2,
    ),
  );
}

/**
 * `preserve_existing` report blocks (work plan §3; gate G2): the 90-degree audit
 * is REPORTED ONLY — this mode never cuts, so a violation is not a failure.
 */
function preserveReportBlocks(): Record<string, unknown> {
  return {
    mandatory_audit: {
      mandatory_90_edges: 0,
      mandatory_90_missing: 0,
      mandatory_90_uv_unsplit: 0,
      reported_only: true,
    },
  };
}

/** `auto_generate` report blocks (work plan §3-§5; gates G1/G3/G5/G6). */
function autoReportBlocks(lockedSeamCount: number): Record<string, unknown> {
  return {
    quality_profile: { profile_id: 'engineering_v0', metric_version: 2, calibrated: false },
    auto_gate: { valid: true, passed: true, failures: [], invalid_reasons: [] },
    auto_constraints: {
      valid: true,
      locked_seam_count: lockedSeamCount,
      protected_cut_count: 0,
      conflict_count: 0,
      conflicts_unresolved: false,
    },
    distortion_v2: {
      metric_version: 2,
      valid: true,
      global: {
        anisotropy_mean: 1.05,
        anisotropy_p95: 1.2,
        anisotropy_max: 1.6,
        area_stretch_mean: 0.05,
        area_stretch_p95: 0.1,
        area_stretch_max: 0.2,
        exceed_area_fraction: 0.0,
      },
      islands: [
        {
          island_id: 0,
          face_count: 100,
          anisotropy_p95: 1.2,
          anisotropy_max: 1.6,
          area_stretch_mean: 0.05,
          exceed_area_fraction: 0.0,
        },
      ],
      degenerate_triangles: { input_defect_count: 0, uv_degenerate_count: 0 },
    },
    correctness: {
      passed: true,
      checks: [],
      overlap_area_total: 0,
      local_flip_count: 0,
      mirrored_island_count: 0,
      uv_degenerate_count: 0,
      min_island_gap_px: 6,
      min_border_gap_px: 5,
      bounds_ok: true,
    },
    // --- Game gates (T14; gate G15) — passing evidence blocks -------------
    fragmentation: MOCK_FRAGMENTATION,
    texel_density: MOCK_TEXEL_DENSITY,
    packing: { efficiency: 0.591278, limit: 0.55, passed: true, advisory: true },
    shading: { policy: MOCK_SHADING_POLICY.policy, passed: true, valid: true, failures: [], invalid_reasons: [] },
    merge_back: MOCK_MERGE_BACK_BLOCK,
    quality_report_passed: true,
    termination: {
      reason: 'quality_passed',
      iterations: 2,
      candidates_evaluated: 3,
      elapsed_s: 1.5,
    },
    seam_length: {
      mandatory: 1,
      user: 0,
      auxiliary: 0.5,
      total: 1.5,
      bbox_diagonal: 3,
      auxiliary_normalized: 0.1667,
    },
    mesh_identity: { unchanged: true },
    mandatory_audit: {
      mandatory_90_edges: 0,
      mandatory_90_missing: 0,
      mandatory_90_uv_unsplit: 0,
      reported_only: false,
    },
  };
}

/**
 * Mock re-application of saved reviewer feedback (gate G7).
 *
 * A feedback file with no `mesh_fingerprint` cannot be matched to this mesh, so
 * it is NOT applied — the honest "no_fingerprint" reason. The mock has no real
 * mesh hash, so a fingerprinted file is reported as a match; the real worker
 * compares hashes and can report `fingerprint_mismatch`.
 */
function mockFeedbackBlock(feedbackAbs?: string | null): Record<string, unknown> {
  if (!feedbackAbs || !existsSync(feedbackAbs)) return {};
  let fb: Record<string, unknown> = {};
  try {
    fb = JSON.parse(readFileSync(feedbackAbs, 'utf-8')) as Record<string, unknown>;
  } catch {
    return {};
  }
  const count = (key: string): number => (Array.isArray(fb[key]) ? (fb[key] as unknown[]).length : 0);
  const hasFingerprint = typeof fb.mesh_fingerprint === 'string' && fb.mesh_fingerprint !== '';
  return {
    feedback_applied: {
      applied: hasFingerprint,
      reason: hasFingerprint ? 'fingerprint_match' : 'no_fingerprint',
      locked_seam_count: count('locked_seam_edges'),
      protected_count: count('protected_edges'),
      preferred_count: count('preferred_edges'),
    },
  };
}

function readMockSeamCount(specAbs?: string): number | null {
  if (!specAbs || !existsSync(specAbs)) return null;
  try {
    const spec = JSON.parse(readFileSync(specAbs, 'utf-8'));
    return Array.isArray(spec.user_seam_edges) ? spec.user_seam_edges.length : null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Mock game-gate evidence (T14; gate G15). Deterministic PASSING values so the
// renderer's "Game gates" panel, merge-back list and reason-code legend have a
// realistic shape to draw without Blender.
// ---------------------------------------------------------------------------
const MOCK_FRAGMENTATION = {
  passed: true,
  valid: true,
  failures: [] as string[],
  quality_failures: [] as string[],
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
  sliver_island_ids: [] as number[],
};

const MOCK_TEXEL_DENSITY = {
  passed: true,
  valid: true,
  failures: [] as string[],
  density_mean: 512.4,
  density_cv: 0.041,
  outlier_count: 1,
  outlier_island_ids: [11],
};

const MOCK_SHADING_POLICY = {
  policy: 'hard_edges_are_seams',
  passed: true,
  valid: true,
  failures: [] as string[],
  invalid_reasons: [] as string[],
};

const MOCK_MERGE_BACK_BLOCK = {
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
};

const MOCK_MERGE_BACK_HISTORY = {
  ...MOCK_MERGE_BACK_BLOCK,
  history: [
    {
      trial: 1,
      island_a: 4,
      island_b: 5,
      edges: [101, 102],
      shared_length: 0.12,
      accepted: true,
      reason: 'accepted',
      island_count_before: 53,
      island_count_after: 52,
      elapsed_s: 0.21,
    },
    {
      trial: 2,
      island_a: 8,
      island_b: 9,
      edges: [204],
      shared_length: 0.04,
      accepted: false,
      reason: 'quality_failed',
      island_count_before: 52,
      island_count_after: 52,
      elapsed_s: 0.18,
    },
  ],
};

const MOCK_QUALITY_REPORT = {
  schema_version: 1,
  passed: true,
  failures: [] as string[],
  fragmentation: MOCK_FRAGMENTATION,
  texel_density: MOCK_TEXEL_DENSITY,
  packing: { efficiency: 0.591278, limit: 0.55, passed: true, advisory: true },
  shading: MOCK_SHADING_POLICY,
  merge_back: MOCK_MERGE_BACK_BLOCK,
};

// 1x1 transparent PNG (base64) used as the mock image placeholder.
const MOCK_PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
  'base64',
);
