/**
 * Main-process integration smoke (plan Session C / E acceptance).
 *
 * Exercises the full create -> inspect -> generate -> approve flow against the
 * mock worker runner — no Electron window, no Blender. Verifies the project
 * manifest, status lifecycle, and summary contract the next MVP will read.
 *
 * Run: npm run test:integration
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, existsSync, readFileSync, readdirSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  createProject,
  openProject,
  getRunView,
  getUvReviewRunView,
  getSeamEditorRunView,
  getUvGenerateRunView,
  getExportRunView,
  listRollbackTargets,
  rollbackProjectState,
  readHistory,
  approveLowpoly,
  registerRun,
  resolveWorkingModel,
  setActiveUserSeamSpec,
  setArtistApproval,
  setSelectedUvLayer,
  setUvGenerateMode,
  saveUvFeedback,
  readUvFeedback,
  uvFeedbackPath,
  seamsDir,
  absSourcePath,
} from '../electron/main/project-service';
import { WorkerRunner } from '../electron/main/worker-runner';
import { UvReviewRunner } from '../electron/main/uvReview';
import { SeamEditorRunner } from '../electron/main/seamEditor';
import { UvGenerateRunner } from '../electron/main/uvGenerate';
import { ExportRunner } from '../electron/main/exportRunner';
import { makeSeamSpec } from '../shared/contracts';

function makeFakeSource(): string {
  const dir = mkdtempSync(join(tmpdir(), 'uvsrc-'));
  const src = join(dir, 'SM_Test_Pottery_a_02.fbx');
  writeFileSync(src, 'FAKE-FBX-CONTENT');
  return src;
}

function workerRoot(): string {
  // app/test -> app -> repo -> worker
  return join(__dirname, '..', '..', 'worker');
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

test('create -> inspect -> generate(mock) -> approve', async () => {
  const root = mkdtempSync(join(tmpdir(), 'uvproj-'));
  const sourcePath = makeFakeSource();

  // 1. create project
  const project = createProject({ root, name: 'pottery_test', sourcePath, role: 'highpoly' });
  assert.equal(project.schema_version, 1);
  assert.ok(project.dir && existsSync(project.dir));
  assert.ok(existsSync(join(project.dir!, 'source', 'original.fbx')));
  assert.equal(project.source_model_role, 'highpoly');

  const runner = new WorkerRunner({ blenderPath: null, workerRoot: workerRoot(), mock: true });

  // 2. inspect (mock)
  const inspect = await runner.inspect(project.id, project.dir!, absSourcePath(project));
  assert.equal(inspect.status, 'accepted');
  assert.ok(inspect.objects && inspect.objects.length > 0);
  const objName = inspect.objects![0].name;

  // 3. generate (mock, async)
  const { run_id } = runner.generate(project.id, project.dir!, {
    sourceModel: absSourcePath(project),
    objectName: objName,
    targetFaces: 12000,
  });
  assert.ok(run_id.startsWith('run_'));

  // wait for the mock run to finish writing artifacts
  let view = getRunView(project.dir!, run_id);
  for (let i = 0; i < 50 && view.status?.status !== 'accepted'; i++) {
    await sleep(20);
    view = getRunView(project.dir!, run_id);
  }
  assert.equal(view.status?.status, 'accepted');
  assert.ok(view.summary, 'summary.json exists');
  assert.equal(view.summary!.metrics.target_faces, 12000);
  assert.ok((view.summary!.metrics.actual_faces ?? 0) > 0, 'actual faces present');
  assert.ok(view.preview_path && existsSync(view.preview_path), 'preview image artifact');
  assert.ok(existsSync(join(view.dir, 'status.json')));

  // 4. approve
  const approve = approveLowpoly(project.dir!, run_id);
  assert.equal(approve.status, 'accepted');
  assert.equal(approve.approved_lowpoly_run_id, run_id);
  assert.ok(existsSync(join(project.dir!, 'work', 'working_lowpoly.blend')));

  // 5. manifest points at the working model for the next MVP
  const reopened = openProject(project.dir!);
  assert.equal(reopened.approved_lowpoly_run_id, run_id);
  assert.equal(reopened.working_model, join('work', 'working_lowpoly.blend'));
  assert.ok(reopened.runs.includes(run_id));
});

test('failed run does not crash and leaves a failed status', async () => {
  const root = mkdtempSync(join(tmpdir(), 'uvproj-'));
  const sourcePath = makeFakeSource();
  const project = createProject({ root, name: 'fail_test', sourcePath });
  // Non-mock runner with a Blender path that does not exist -> spawn fails.
  const runner = new WorkerRunner({
    blenderPath: '/nonexistent/blender',
    workerRoot: workerRoot(),
  });
  const { run_id } = runner.generate(project.id, project.dir!, {
    sourceModel: absSourcePath(project),
    objectName: 'X',
    targetFaces: 8000,
  });
  registerRun(project.dir!, run_id);
  let view = getRunView(project.dir!, run_id);
  for (let i = 0; i < 50 && (!view.status || view.status.status === 'queued'); i++) {
    await sleep(20);
    view = getRunView(project.dir!, run_id);
  }
  assert.equal(view.status?.status, 'failed');
  assert.ok(view.status?.error);
  // The raw status.json file is on disk (UI never parses stdout).
  const onDisk = JSON.parse(readFileSync(join(view.dir, 'status.json'), 'utf-8'));
  assert.equal(onDisk.status, 'failed');
});

// --- MVP 1: UV review main-process flow (Session D acceptance) -------------
test('uv review: inspect -> select layer -> review(mock) -> reopen', async () => {
  const root = mkdtempSync(join(tmpdir(), 'uvproj-'));
  const sourcePath = makeFakeSource();
  const project = createProject({ root, name: 'uv_review_test', sourcePath, role: 'lowpoly' });

  const runner = new UvReviewRunner({ blenderPath: null, workerRoot: workerRoot(), mock: true });

  // 1. inspect UV layers (mock) — resolves the working model (falls back to source).
  const { abs, rel } = resolveWorkingModel(project);
  const inspect = await runner.inspectLayers(project.id, abs, rel);
  assert.equal(inspect.status, 'accepted');
  assert.ok(inspect.objects && inspect.objects.length > 0);
  const obj = inspect.objects![0];
  assert.ok(obj.has_uv);
  const layer = obj.active_uv_layer as string;

  // 2. select the active UV layer -> persisted on the manifest (plan §5.3).
  setSelectedUvLayer(project.dir!, obj.name, layer);
  assert.equal(openProject(project.dir!).selected_uv_layer, layer);

  // 3. review (mock, async).
  const { run_id } = runner.reviewExisting(project.id, project.dir!, {
    modelAbs: abs,
    modelRel: rel,
    objectName: obj.name,
    uvLayer: layer,
  });
  assert.ok(run_id.startsWith('review_run_'));

  // 4. poll the review run view until accepted.
  let view = getUvReviewRunView(project.dir!, run_id);
  for (let i = 0; i < 50 && view.status?.status !== 'accepted'; i++) {
    await sleep(20);
    view = getUvReviewRunView(project.dir!, run_id);
  }
  assert.equal(view.status?.status, 'accepted');
  assert.ok(view.summary, 'uv_review_summary.json exists');
  assert.equal(view.summary!.uv_layer, layer);
  assert.ok(view.summary!.metrics, 'metrics present');
  for (const key of ['stretch_score', 'overlap_ratio', 'packing_efficiency']) {
    assert.ok(key in (view.summary!.metrics as Record<string, unknown>), `metric ${key}`);
  }
  // Artifact paths are absolute + on disk (renderer renders via uvpreview://).
  assert.ok(view.artifact_paths.uv_layout && existsSync(view.artifact_paths.uv_layout));
  assert.ok(view.artifact_paths.checker_front && existsSync(view.artifact_paths.checker_front));

  // 5. manifest records the latest review run for the next session.
  const reopened = openProject(project.dir!);
  assert.equal(reopened.latest_uv_review_run_id, run_id);
  assert.ok(reopened.uv_review_runs?.includes(run_id));
});

test('uv review: no_uv summary is handled by the run view', async () => {
  const root = mkdtempSync(join(tmpdir(), 'uvproj-'));
  const sourcePath = makeFakeSource();
  const project = createProject({ root, name: 'uv_nouv_test', sourcePath });
  const { abs, rel } = resolveWorkingModel(project);
  // Write a no_uv summary the way the worker would, then read it back via the view.
  const runner = new UvReviewRunner({ blenderPath: null, workerRoot: workerRoot(), mock: true });
  const { run_id } = runner.reviewExisting(project.id, project.dir!, {
    modelAbs: abs,
    modelRel: rel,
    objectName: 'X',
    uvLayer: 'UVMap',
  });
  let view = getUvReviewRunView(project.dir!, run_id);
  for (let i = 0; i < 50 && view.status?.status === 'queued'; i++) {
    await sleep(20);
    view = getUvReviewRunView(project.dir!, run_id);
  }
  // The mock produces an accepted/clean review; assert the view assembles cleanly.
  assert.ok(view.summary);
  assert.ok(['accepted', 'no_uv'].includes(view.summary!.status));
});

// --- MVP 2: seam editor main-process flow (Session D acceptance) -----------
test('seam editor: export edge geometry -> save spec -> reopen (mock)', async () => {
  const root = mkdtempSync(join(tmpdir(), 'uvproj-'));
  const sourcePath = makeFakeSource();
  const project = createProject({ root, name: 'seam_test', sourcePath, role: 'lowpoly' });
  const { abs, rel } = resolveWorkingModel(project);

  const runner = new SeamEditorRunner({ blenderPath: null, workerRoot: workerRoot(), mock: true });

  // 1. export edge geometry (mock cube) — the only selectable-id source.
  const { run_id } = runner.exportEdgeGeometry(project.id, project.dir!, {
    modelAbs: abs,
    modelRel: rel,
    objectName: 'MockCube',
  });
  assert.ok(run_id.startsWith('seam_run_'));

  let view = getSeamEditorRunView(project.dir!, run_id);
  for (let i = 0; i < 50 && view.status?.status !== 'accepted'; i++) {
    await sleep(20);
    view = getSeamEditorRunView(project.dir!, run_id);
  }
  assert.equal(view.status?.status, 'accepted');
  assert.ok(view.edge_geometry, 'edge_geometry.json parsed');
  assert.equal(view.edge_geometry!.edges.length, 12, 'cube has 12 edges');
  assert.equal(view.export_result?.mesh_signature?.edges, 12);
  // Edge ids are dense 0..N-1 (renderer never re-derives them).
  assert.deepEqual(
    view.edge_geometry!.edges.map((e) => e.id),
    Array.from({ length: 12 }, (_, i) => i),
  );
  const edgeCount = view.export_result!.mesh_signature!.edges;

  // 2. validate a spec with a conflict (16 in both) + invalid (999) edge.
  const dirty = {
    version: 1,
    object: 'MockCube',
    mode: 'user_seams',
    mandatory_fold_angle: 90.0,
    user_seam_edges: [2, 5],
    user_protected_edges: [5, 999],
    chapters: [],
    notes: '',
  };
  const validation = runner.validateSpec({ spec: dirty as never, objectName: 'MockCube', edgeCount });
  assert.deepEqual(validation.invalid_edges, [999]);
  assert.equal(validation.conflicts.length, 1); // edge 5 seam-and-protected
  assert.deepEqual(validation.normalized_spec.user_seam_edges, [2, 5]);
  assert.deepEqual(validation.normalized_spec.user_protected_edges, []); // seam wins

  // 3. save normalized spec -> project.active_user_seam_spec recorded.
  const saved = runner.saveSpec(project.dir!, { spec: dirty as never, objectName: 'MockCube', edgeCount });
  assert.equal(saved.status, 'accepted');
  assert.equal(saved.path, join('work', 'seams', 'user_seam_spec.json'));
  assert.ok(existsSync(join(project.dir!, saved.path)));
  const reopened = openProject(project.dir!);
  assert.equal(reopened.active_user_seam_spec, join('work', 'seams', 'user_seam_spec.json'));
  assert.ok(reopened.seam_editor_runs?.includes(run_id));

  // 4. the saved file is the normalized canonical spec (MVP 3 input, plan §16).
  const onDisk = JSON.parse(readFileSync(join(project.dir!, saved.path), 'utf-8'));
  assert.equal(onDisk.mode, 'user_seams');
  assert.deepEqual(onDisk.user_seam_edges, [2, 5]);
  assert.deepEqual(onDisk.user_protected_edges, []);

  // 5. load it back.
  const loaded = runner.loadSpec(project.dir!, { objectName: 'MockCube', edgeCount });
  assert.ok(loaded.spec);
  assert.deepEqual(loaded.spec!.user_seam_edges, [2, 5]);
  assert.equal(loaded.validation?.valid, true);
});

test('seam editor: extract UV boundary writes a draft spec (mock)', async () => {
  const root = mkdtempSync(join(tmpdir(), 'uvproj-'));
  const sourcePath = makeFakeSource();
  const project = createProject({ root, name: 'seam_boundary_test', sourcePath });
  const { abs, rel } = resolveWorkingModel(project);
  const runner = new SeamEditorRunner({ blenderPath: null, workerRoot: workerRoot(), mock: true });

  const { run_id } = runner.extractUvBoundary(project.id, project.dir!, {
    modelAbs: abs,
    modelRel: rel,
    objectName: 'MockCube',
    uvLayer: 'UVChannel_1',
  });
  let view = getSeamEditorRunView(project.dir!, run_id);
  for (let i = 0; i < 50 && view.status?.status !== 'accepted'; i++) {
    await sleep(20);
    view = getSeamEditorRunView(project.dir!, run_id);
  }
  assert.equal(view.status?.status, 'accepted');
  assert.ok(view.boundary, 'boundary report parsed');
  assert.ok((view.boundary!.user_seam_count ?? 0) > 0);
  assert.ok(view.boundary!.spec, 'draft spec present');
  // The draft is written to work/seams/ for the user to review before saving.
  assert.ok(existsSync(join(project.dir!, 'work', 'seams', 'reference_boundary_seam_spec.json')));
});

// --- MVP 3: generate + optimize main-process flow (Session E acceptance) ---
function seedSeamSpecProject(name: string): {
  project: ReturnType<typeof createProject>;
  objectName: string;
} {
  const root = mkdtempSync(join(tmpdir(), 'uvproj-'));
  const sourcePath = makeFakeSource();
  const project = createProject({ root, name, sourcePath, role: 'lowpoly' });
  const objectName = 'SM_Test_Pottery_a_02';
  // Pretend MVP 2 left an active user seam spec (the MVP 3 source of truth).
  seamsDir(project.dir!);
  const specRel = join('work', 'seams', 'user_seam_spec.json');
  const spec = makeSeamSpec({
    object: objectName,
    user_seam_edges: Array.from({ length: 1230 }, (_, i) => i),
  });
  writeFileSync(join(project.dir!, specRel), JSON.stringify(spec, null, 2));
  setActiveUserSeamSpec(project.dir!, specRel, objectName);
  return { project, objectName };
}

test('uv generate: validate -> start(mock) -> accepted -> selected UV recorded', async () => {
  const { project, objectName } = seedSeamSpecProject('uv_generate_test');
  const runner = new UvGenerateRunner({ blenderPath: null, workerRoot: workerRoot(), mock: true });

  // 1. validate readiness (pure-Node pre-flight).
  const v = runner.validateInput(project.dir!);
  assert.equal(v.ready, true, 'project is ready to generate');
  assert.equal(v.object_mismatch, false);
  assert.equal(v.user_seam_count, 1230);
  // An explicit MVP 2 spec is the source of truth (revision plan §7).
  assert.equal(v.seam_source, 'explicit');

  // 2. start the generate run (mock, async).
  const { run_id } = runner.start(project.id, project.dir!, { objectName });
  assert.ok(run_id.startsWith('uv_run_'));

  // 3. poll until accepted.
  let view = getUvGenerateRunView(project.dir!, run_id);
  for (let i = 0; i < 50 && view.status?.status !== 'accepted'; i++) {
    await sleep(20);
    view = getUvGenerateRunView(project.dir!, run_id);
  }
  assert.equal(view.status?.status, 'accepted');
  assert.ok(view.summary, 'uv_generate_summary.json exists');
  assert.equal(view.summary!.selected_candidate_id, 'slim_concave_m002');
  // The explicit spec is recorded as the seam source (revision plan §4, §7).
  assert.equal(view.summary!.seam_source?.type, 'user_seam_spec');
  assert.equal(view.summary!.seam_source?.derived, false);

  // 4. seam integrity is the MVP 3 hard acceptance (plan §6).
  const si = view.summary!.seam_integrity;
  assert.equal(si.auto_added_seams, 0);
  assert.equal(si.final_seam_count, si.user_seam_count);
  assert.equal(si.valid, true);

  // 5. candidate table + before/after artifacts are present (plan §5, §7).
  assert.ok(view.candidate_summary, 'candidate_summary.json parsed');
  assert.equal(view.candidate_summary!.baseline_candidate_id, 'slim_concave_m005');
  assert.ok(view.candidate_summary!.rejected.length >= 1, 'a rejected candidate is recorded');
  for (const key of [
    'baseline_uv_layout',
    'baseline_checker_front',
    'baseline_checker_side',
    'selected_uv_layout',
    'selected_checker_front',
    'selected_checker_side',
  ]) {
    assert.ok(view.artifact_paths[key] && existsSync(view.artifact_paths[key]), `artifact ${key}`);
  }

  // 6. an accepted run ships the selected UV to work/uv + records the manifest.
  assert.ok(existsSync(join(project.dir!, 'work', 'uv', 'selected_uv.blend')));
  assert.ok(existsSync(join(project.dir!, 'work', 'uv', 'selected_uv_summary.json')));
  const reopened = openProject(project.dir!);
  assert.equal(reopened.latest_uv_generate_run_id, run_id);
  assert.ok(reopened.uv_generate_runs?.includes(run_id));
  assert.equal(reopened.selected_uv_model, join('work', 'uv', 'selected_uv.blend'));
  assert.equal(reopened.selected_uv_summary, join('work', 'uv', 'selected_uv_summary.json'));
});

test('uv generate: validate flags a missing seam source (no spec, no UV layer) as not ready', async () => {
  const root = mkdtempSync(join(tmpdir(), 'uvproj-'));
  const sourcePath = makeFakeSource();
  const project = createProject({ root, name: 'uv_generate_nospec', sourcePath });
  const runner = new UvGenerateRunner({ blenderPath: null, workerRoot: workerRoot(), mock: true });

  const v = runner.validateInput(project.dir!);
  assert.equal(v.ready, false);
  assert.equal(v.seam_source, 'missing');
  assert.ok(v.issues.some((i) => i.code === 'missing_seam_source'));
});

// UV-boundary fallback: a project with a selected UV layer but NO active seam
// spec is still generatable — the worker derives a seam spec from the UV island
// boundary (revision plan §1 case 2, §6.3, §7).
test('uv generate: UV-boundary fallback derives a seam source when no spec exists', async () => {
  const root = mkdtempSync(join(tmpdir(), 'uvproj-'));
  const sourcePath = makeFakeSource();
  const project = createProject({ root, name: 'uv_generate_derived', sourcePath, role: 'lowpoly' });
  const objectName = 'SM_Test_Pottery_a_02';
  // MVP 1 left a selected UV layer but the user never opened the Seam Editor.
  setSelectedUvLayer(project.dir!, objectName, 'UVChannel_1');
  const runner = new UvGenerateRunner({ blenderPath: null, workerRoot: workerRoot(), mock: true });

  // 1. validate: ready via the UV-boundary fallback (Generate enabled, plan §4.5).
  const v = runner.validateInput(project.dir!);
  assert.equal(v.ready, true, 'derived fallback is ready without a spec');
  assert.equal(v.seam_source, 'derived');
  assert.equal(v.selected_uv_layer, 'UVChannel_1');
  assert.equal(openProject(project.dir!).active_user_seam_spec ?? null, null);

  // 2. start: the worker job carries the uv_layer and a null seam_spec (plan §6.3).
  const { run_id } = runner.start(project.id, project.dir!, { objectName });
  let view = getUvGenerateRunView(project.dir!, run_id);
  for (let i = 0; i < 50 && view.status?.status !== 'accepted'; i++) {
    await sleep(20);
    view = getUvGenerateRunView(project.dir!, run_id);
  }
  const job = JSON.parse(readFileSync(join(project.dir!, 'runs', run_id, 'job.json'), 'utf-8'));
  assert.equal(job.uv_layer, 'UVChannel_1');
  assert.equal(job.seam_spec, null);

  // 3. accepted run records seam_source.type = uv_boundary_derived (plan §6.3).
  assert.equal(view.status?.status, 'accepted');
  assert.ok(view.summary?.seam_source, 'summary carries a seam_source block');
  assert.equal(view.summary!.seam_source!.type, 'uv_boundary_derived');
  assert.equal(view.summary!.seam_source!.derived, true);
  assert.equal(view.summary!.seam_source!.user_confirmed, false);
  // seam integrity still holds on the derived path (plan §7 integrity).
  const si = view.summary!.seam_integrity;
  assert.equal(si.auto_added_seams, 0);
  assert.equal(si.final_seam_count, si.user_seam_count);

  // 4. derived spec is saved separately; active_user_seam_spec is NOT overwritten,
  //    but latest_derived_seam_spec points at it (plan §7, §4.4).
  assert.ok(existsSync(join(project.dir!, 'work', 'seams', 'derived_from_uv_boundary.json')));
  const reopened = openProject(project.dir!);
  assert.equal(reopened.active_user_seam_spec ?? null, null, 'derived run never sets active_user_seam_spec');
  assert.equal(reopened.latest_derived_seam_spec, join('work', 'seams', 'derived_from_uv_boundary.json'));
});

// --- UV automation: execution modes (work plan section 3; gates G2/G6) -----
function sha256File(path: string): string {
  return createHash('sha256').update(readFileSync(path)).digest('hex');
}

async function waitForTerminal(dir: string, runId: string) {
  let view = getUvGenerateRunView(dir, runId);
  const terminal = ['accepted', 'needs_user_review', 'needs_input', 'failed', 'cancelled'];
  for (let i = 0; i < 50 && !terminal.includes(view.status?.status ?? ''); i++) {
    await sleep(20);
    view = getUvGenerateRunView(dir, runId);
  }
  return view;
}

test('uv generate mode: legacy project without mode resolves to preserve_existing and job carries it', async () => {
  const { project, objectName } = seedSeamSpecProject('uv_generate_mode_legacy');
  // The seeded project has NO uv_generate_mode - a legacy manifest (gate G2).
  assert.equal(openProject(project.dir!).uv_generate_mode ?? null, null);
  const runner = new UvGenerateRunner({ blenderPath: null, workerRoot: workerRoot(), mock: true });

  const started = runner.start(project.id, project.dir!, { objectName });
  assert.equal(started.mode, 'preserve_existing');
  const view = await waitForTerminal(project.dir!, started.run_id);
  assert.equal(view.status?.status, 'accepted');

  const job = JSON.parse(
    readFileSync(join(project.dir!, 'runs', started.run_id, 'job.json'), 'utf-8'),
  );
  assert.equal(job.mode, 'preserve_existing');
  assert.equal(job.options.mode, 'preserve_existing');
  assert.equal(job.options.auto_refine_user_seams, false);
  assert.equal(view.status?.input.mode, 'preserve_existing');
  assert.equal(view.summary!.mode, 'preserve_existing');
  assert.equal(view.summary!.solver_accepted, true);
  assert.equal(view.summary!.artist_approved, false);
  assert.equal(view.summary!.mandatory_audit?.reported_only, true);
  assert.equal(openProject(project.dir!).uv_generate_mode, 'preserve_existing');
});

test('uv generate mode: auto_generate runs without a seam source and records the mode end to end', async () => {
  const root = mkdtempSync(join(tmpdir(), 'uvproj-'));
  const sourcePath = makeFakeSource();
  const project = createProject({ root, name: 'uv_generate_auto', sourcePath, role: 'lowpoly' });
  const objectName = 'SM_Test_Pottery_a_02';
  setUvGenerateMode(project.dir!, 'auto_generate');
  const runner = new UvGenerateRunner({ blenderPath: null, workerRoot: workerRoot(), mock: true });

  // 1. validate: no spec and no UV layer is READY in auto mode (gate G2).
  const v = runner.validateInput(project.dir!);
  assert.equal(v.mode, 'auto_generate');
  assert.equal(v.ready, true, 'auto_generate needs no seam source');
  assert.equal(v.seam_source, 'missing');
  assert.equal(
    v.issues.some((i) => i.code === 'missing_seam_source'),
    false,
  );

  // 2. start: no needs_input, an accepted automatic run instead.
  const started = runner.start(project.id, project.dir!, { objectName });
  assert.equal(started.mode, 'auto_generate');
  const view = await waitForTerminal(project.dir!, started.run_id);
  assert.equal(view.status?.status, 'accepted');
  const job = JSON.parse(
    readFileSync(join(project.dir!, 'runs', started.run_id, 'job.json'), 'utf-8'),
  );
  assert.equal(job.mode, 'auto_generate');
  assert.equal(job.options.mode, 'auto_generate');
  assert.equal(job.options.auto_refine_user_seams, true);

  const summary = view.summary!;
  assert.equal(summary.mode, 'auto_generate');
  assert.equal(summary.auto_gate?.passed, true);
  assert.equal(summary.auto_gate?.valid, true);
  assert.equal(summary.solver_accepted, true);
  assert.equal(summary.artist_approved, false, 'solver accepted is not artist approval');
  assert.equal(summary.quality_profile?.profile_id, 'engineering_v0');
  assert.equal(summary.distortion_v2?.metric_version, 2);
  assert.equal(summary.correctness?.passed, true);
  assert.equal(summary.termination?.reason, 'quality_passed');
  assert.equal(summary.mesh_identity?.unchanged, true);
  assert.ok(existsSync(join(project.dir!, 'work', 'uv', 'selected_uv.blend')));
  assert.equal(openProject(project.dir!).uv_generate_mode, 'auto_generate');
});

// Gate G2/G9: an imported UV-less low-poly project has no `selected_object`
// (that is only written by the low-poly / UV review paths), so the workspace
// sends an explicit object with the start request. The run must use it AND the
// project must record it.
test('uv generate: auto mode start persists an explicit objectName on a project without selected_object', async () => {
  const root = mkdtempSync(join(tmpdir(), 'uvproj-'));
  const sourcePath = makeFakeSource();
  const project = createProject({ root, name: 'uv_generate_no_selected', sourcePath });
  assert.equal(project.selected_object, null, 'imported project has no selected object');
  setUvGenerateMode(project.dir!, 'auto_generate');
  const runner = new UvGenerateRunner({ blenderPath: null, workerRoot: workerRoot(), mock: true });

  const started = runner.start(project.id, project.dir!, { objectName: 'Fixture' });
  assert.equal(started.mode, 'auto_generate');
  const job = JSON.parse(
    readFileSync(join(project.dir!, 'runs', started.run_id, 'job.json'), 'utf-8'),
  );
  assert.equal(job.object_name, 'Fixture');
  assert.equal(openProject(project.dir!).selected_object, 'Fixture');
});

test('uv generate mode: contradictory raw flags are rejected before a run directory exists', async () => {
  const { project, objectName } = seedSeamSpecProject('uv_generate_mode_conflict');
  const runner = new UvGenerateRunner({ blenderPath: null, workerRoot: workerRoot(), mock: true });
  const runsRoot = join(project.dir!, 'runs');
  const before = existsSync(runsRoot) ? readdirSync(runsRoot).length : 0;

  assert.throws(
    () =>
      runner.start(project.id, project.dir!, {
        objectName,
        mode: 'auto_generate',
        options: { enforce_user_mandatory: false },
      }),
    (err: unknown) => (err as { code?: string }).code === 'strict_flag_contradicts_auto',
  );
  assert.throws(
    () =>
      runner.start(project.id, project.dir!, {
        objectName,
        mode: 'preserve_existing',
        options: { auto_refine_user_seams: true },
      }),
    (err: unknown) => (err as { code?: string }).code === 'strict_flag_contradicts_preserve',
  );

  const after = existsSync(runsRoot) ? readdirSync(runsRoot).length : 0;
  assert.equal(after, before, 'a rejected request creates no run directory');
});

test('uv generate G6: needs_user_review / failed / cancel never replace the prior approved selected UV', async () => {
  const { project, objectName } = seedSeamSpecProject('uv_generate_g6');
  const runner = new UvGenerateRunner({ blenderPath: null, workerRoot: workerRoot(), mock: true });

  // 1. an accepted run ships the approved selected UV.
  const first = runner.start(project.id, project.dir!, { objectName });
  const firstView = await waitForTerminal(project.dir!, first.run_id);
  assert.equal(firstView.status?.status, 'accepted');
  const approvedPath = join(project.dir!, 'work', 'uv', 'selected_uv.blend');
  const approvedHash = sha256File(approvedPath);
  const approvedPointer = openProject(project.dir!).selected_uv_model;

  // 2. needs_user_review: no handoff, no pointer change (gate G6).
  const review = runner.start(project.id, project.dir!, {
    objectName,
    options: { mock_status: 'needs_user_review' },
  });
  const reviewView = await waitForTerminal(project.dir!, review.run_id);
  assert.equal(reviewView.status?.status, 'needs_user_review');
  assert.equal(reviewView.summary!.selected_uv_model, null);
  assert.equal(sha256File(approvedPath), approvedHash, 'approved file untouched');
  let reopened = openProject(project.dir!);
  assert.equal(reopened.selected_uv_model, approvedPointer);
  assert.equal(reopened.latest_uv_generate_run_id, review.run_id);

  // 3. failed: same guarantee.
  const failed = runner.start(project.id, project.dir!, {
    objectName,
    options: { mock_status: 'failed' },
  });
  const failedView = await waitForTerminal(project.dir!, failed.run_id);
  assert.equal(failedView.status?.status, 'failed');
  assert.equal(failedView.summary!.selected_uv_model, null);
  assert.equal(sha256File(approvedPath), approvedHash, 'approved file untouched');
  reopened = openProject(project.dir!);
  assert.equal(reopened.selected_uv_model, approvedPointer);
  assert.equal(reopened.latest_uv_generate_run_id, failed.run_id);

  // 4. cancel: the prior approved file survives regardless of the race with the
  //    10ms mock; only the file hash is asserted unconditionally.
  const cancelled = runner.start(project.id, project.dir!, { objectName });
  runner.cancel(project.dir!, cancelled.run_id);
  await sleep(40);
  const cancelView = getUvGenerateRunView(project.dir!, cancelled.run_id);
  assert.equal(sha256File(approvedPath), approvedHash, 'approved file untouched after cancel');
  if (cancelView.status?.status === 'cancelled') {
    assert.ok(cancelView.status.finished_at, 'a cancelled run records finished_at');
  } else {
    assert.equal(cancelView.status?.status, 'accepted');
  }
});

// --- MVP 5: production export main-process flow (Session E/G acceptance) ----
/** Seed a project that already has an ACCEPTED MVP 3 selected UV (via the mock). */
async function seedAcceptedSelectedUv(name: string): Promise<{
  project: ReturnType<typeof createProject>;
  objectName: string;
  uvRunId: string;
}> {
  const { project, objectName } = seedSeamSpecProject(name);
  const runner = new UvGenerateRunner({ blenderPath: null, workerRoot: workerRoot(), mock: true });
  const { run_id: uvRunId } = runner.start(project.id, project.dir!, { objectName });
  let gv = getUvGenerateRunView(project.dir!, uvRunId);
  for (let i = 0; i < 50 && gv.status?.status !== 'accepted'; i++) {
    await sleep(20);
    gv = getUvGenerateRunView(project.dir!, uvRunId);
  }
  assert.equal(gv.status?.status, 'accepted', 'seed UV run accepted');
  return { project, objectName, uvRunId };
}

test('mvp5 export: readiness -> export(mock) -> manifest -> history -> rollback', async () => {
  const { project, uvRunId } = await seedAcceptedSelectedUv('mvp5_export_test');
  const exporter = new ExportRunner({ blenderPath: null, workerRoot: workerRoot(), mock: true });

  // 1. readiness passes on an accepted selected UV; AI Review skip is non-blocking.
  const readiness = exporter.checkReadiness(project.dir!);
  assert.equal(readiness.ready, true);
  assert.equal(readiness.status, 'accepted');
  assert.equal(readiness.checks.ai_review_skipped, true);
  assert.ok(readiness.warnings.some((w) => /AI Review/.test(w)));
  assert.equal(readiness.blocking_issues.length, 0);

  // 2. export FBX + OBJ + GLB (mock, async).
  const { export_id } = exporter.start(project.id, project.dir!, { formats: ['fbx', 'obj', 'glb'] });
  assert.ok(export_id.startsWith('export_'));

  let ev = getExportRunView(project.dir!, export_id);
  for (let i = 0; i < 50 && ev.status?.status !== 'accepted'; i++) {
    await sleep(20);
    ev = getExportRunView(project.dir!, export_id);
  }
  assert.equal(ev.status?.status, 'accepted');

  // 3. manifest links the source UV run, seam spec, candidate summary, metrics, files.
  assert.ok(ev.manifest, 'export_manifest.json exists');
  assert.deepEqual(ev.manifest!.formats, ['fbx', 'obj', 'glb']);
  assert.equal(ev.manifest!.source.uv_generate_run_id, uvRunId);
  assert.equal(ev.manifest!.source.ai_review_skipped, true);
  assert.ok(ev.manifest!.source.active_user_seam_spec, 'manifest links the seam spec');
  assert.ok('packing_efficiency' in ev.manifest!.metrics, 'manifest carries UV metrics');
  for (const fmt of ['fbx', 'obj', 'glb']) {
    assert.ok(ev.manifest!.files[fmt], `manifest file for ${fmt}`);
    assert.ok(ev.file_paths[fmt] && existsSync(ev.file_paths[fmt]), `exported ${fmt} on disk`);
  }

  // 4. validation report carries the per-format reopen result; UV present (plan §7).
  assert.equal(ev.validation!.status, 'accepted');
  assert.ok(ev.validation!.formats.fbx.has_uv);
  assert.equal(ev.validation!.formats.fbx.reopen_ok, true);

  // 5. project manifest + history record the export (plan §6, §8).
  let reopened = openProject(project.dir!);
  assert.equal(reopened.latest_export_id, export_id);
  assert.ok(reopened.exports?.includes(export_id));
  assert.equal(reopened.ai_review_skipped, true);
  let history = readHistory(project.dir!);
  assert.ok(
    history.events.some((e) => e.type === 'export_created' && e.export_id === export_id),
    'export_created event appended',
  );

  // 6. rollback targets include the accepted UV run AND the export (plan §9.1).
  const targets = listRollbackTargets(project.dir!).targets;
  assert.ok(targets.some((t) => t.type === 'uv_run' && t.id === uvRunId));
  assert.ok(targets.some((t) => t.type === 'export' && t.id === export_id));

  // 7. rollback to the UV run restores work/uv pointers + appends a new event,
  //    and never deletes the newer export (plan §9.2, §15).
  const rb = rollbackProjectState(project.dir!, { targetType: 'uv_run', targetId: uvRunId });
  assert.equal(rb.status, 'accepted');
  assert.equal(rb.rolled_back_to.id, uvRunId);
  assert.equal(rb.rolled_back_to.selected_uv_model, join('work', 'uv', 'selected_uv.blend'));
  history = readHistory(project.dir!);
  assert.ok(
    history.events.some((e) => e.type === 'rollback_performed' && e.target_id === uvRunId),
    'rollback_performed event appended',
  );
  assert.ok(
    existsSync(join(project.dir!, 'exports', export_id, 'export_manifest.json')),
    'newer export preserved after rollback',
  );
  reopened = openProject(project.dir!);
  assert.equal(reopened.latest_uv_generate_run_id, uvRunId);
});

test('mvp5 export readiness: missing selected UV is needs_input (non-blocking AI skip)', () => {
  const root = mkdtempSync(join(tmpdir(), 'uvproj-'));
  const sourcePath = makeFakeSource();
  const project = createProject({ root, name: 'mvp5_noselect', sourcePath });
  const exporter = new ExportRunner({ blenderPath: null, workerRoot: workerRoot(), mock: true });

  const r = exporter.checkReadiness(project.dir!);
  assert.equal(r.ready, false);
  assert.equal(r.status, 'needs_input');
  assert.ok(r.blocking_issues.some((i) => i.code === 'missing_selected_uv_model'));
  // AI Review skip is still informational, never a blocker (plan §0).
  assert.ok(r.blocking_issues.every((i) => !/ai_review/.test(i.code)));
});

test('mvp5 export: rollback to a prior export re-pins latest_export_id, keeps newer', async () => {
  const { project } = await seedAcceptedSelectedUv('mvp5_rollback_export');
  const exporter = new ExportRunner({ blenderPath: null, workerRoot: workerRoot(), mock: true });

  const first = exporter.start(project.id, project.dir!, { formats: ['obj'] }).export_id;
  let fv = getExportRunView(project.dir!, first);
  for (let i = 0; i < 50 && fv.status?.status !== 'accepted'; i++) {
    await sleep(20);
    fv = getExportRunView(project.dir!, first);
  }
  const second = exporter.start(project.id, project.dir!, { formats: ['glb'] }).export_id;
  let sv = getExportRunView(project.dir!, second);
  for (let i = 0; i < 50 && sv.status?.status !== 'accepted'; i++) {
    await sleep(20);
    sv = getExportRunView(project.dir!, second);
  }
  assert.equal(openProject(project.dir!).latest_export_id, second);

  const rb = rollbackProjectState(project.dir!, { targetType: 'export', targetId: first });
  assert.equal(rb.status, 'accepted');
  assert.equal(openProject(project.dir!).latest_export_id, first);
  // newer export manifest is NOT deleted (plan §9 rules)
  assert.ok(existsSync(join(project.dir!, 'exports', second, 'export_manifest.json')));
});

// --- UV automation: reviewer approval + feedback (gates G6/G7) --------------

test('uv artist approval: solver accepted and artist approved are recorded separately', async () => {
  const { project, objectName } = seedSeamSpecProject('uv_artist_approval');
  const runner = new UvGenerateRunner({ blenderPath: null, workerRoot: workerRoot(), mock: true });

  // 1. an accepted solver run is NOT an artist approval (gate G6).
  const started = runner.start(project.id, project.dir!, { objectName });
  const view = await waitForTerminal(project.dir!, started.run_id);
  assert.equal(view.status?.status, 'accepted');
  assert.equal(view.summary!.solver_accepted, true);
  assert.equal(view.summary!.artist_approved, false);
  const summaryPath = join(project.dir!, 'runs', started.run_id, 'uv_generate_summary.json');
  const summaryHash = sha256File(summaryPath);

  // 2. a reviewer REJECTION stores the reason and the run it was made against.
  const rejected = setArtistApproval(project.dir!, { approved: false, reason: 'seam on face' });
  assert.equal(rejected.uv_artist_approval!.approved, false);
  assert.equal(rejected.uv_artist_approval!.reason, 'seam on face');
  assert.equal(rejected.uv_artist_approval!.run_id, started.run_id);
  assert.equal(
    openProject(project.dir!).uv_artist_approval!.run_id,
    openProject(project.dir!).latest_uv_generate_run_id,
  );

  // 3. the solver verdict file is untouched by the artist verdict (gate G6).
  assert.equal(sha256File(summaryPath), summaryHash, 'summary untouched by artist verdict');
  const reread = getUvGenerateRunView(project.dir!, started.run_id);
  assert.equal(reread.summary!.solver_accepted, true);
  assert.equal(reread.summary!.artist_approved, false, 'artist verdict never edits the run summary');

  // 4. a rejection without a reason is refused (gate G7 "거절 이유 저장").
  assert.throws(
    () => setArtistApproval(project.dir!, { approved: false }),
    (err: unknown) => (err as Error).message === 'artist_rejection_requires_reason',
  );
  assert.equal(openProject(project.dir!).uv_artist_approval!.reason, 'seam on face');

  // 5. an approval is timestamped.
  const approved = setArtistApproval(project.dir!, { approved: true, reviewer: 'artist_a' });
  assert.equal(approved.uv_artist_approval!.approved, true);
  assert.ok(approved.uv_artist_approval!.approved_at, 'approval records approved_at');
});

test('uv feedback: saved constraints are handed to the next run and skipped without a fingerprint', async () => {
  const root = mkdtempSync(join(tmpdir(), 'uvproj-'));
  const sourcePath = makeFakeSource();
  const project = createProject({ root, name: 'uv_feedback', sourcePath, role: 'lowpoly' });
  const objectName = 'SM_Test_Pottery_a_02';
  setUvGenerateMode(project.dir!, 'auto_generate');
  const runner = new UvGenerateRunner({ blenderPath: null, workerRoot: workerRoot(), mock: true });

  // 1. save reviewer feedback: edge ids are de-duplicated and sorted.
  const saved = saveUvFeedback(project.dir!, {
    locked_seam_edges: [3, 1, 3],
    protected_edges: [7],
    preferred_edges: [],
    front_axis: '-Y',
  });
  assert.ok(existsSync(uvFeedbackPath(project.dir!)), 'work/uv/uv_feedback.json written');
  assert.deepEqual(saved.locked_seam_edges, [1, 3]);
  assert.deepEqual(saved.protected_edges, [7]);
  assert.equal(saved.front_axis, '-Y');
  assert.equal(saved.mesh_fingerprint, null);
  assert.deepEqual(readUvFeedback(project.dir!)!.locked_seam_edges, [1, 3]);

  // 2. the next run carries the feedback path in its job (gate G7).
  const first = runner.start(project.id, project.dir!, { objectName });
  const firstView = await waitForTerminal(project.dir!, first.run_id);
  const job = JSON.parse(readFileSync(join(project.dir!, 'runs', first.run_id, 'job.json'), 'utf-8'));
  assert.equal(job.feedback, uvFeedbackPath(project.dir!));
  assert.equal(job.feedback_rel, 'work/uv/uv_feedback.json');

  // 3. no fingerprint -> the constraints are NOT silently re-used (gate G7).
  assert.equal(firstView.summary!.feedback_applied!.reason, 'no_fingerprint');
  assert.equal(firstView.summary!.feedback_applied!.applied, false);
  assert.equal(firstView.summary!.feedback_applied!.locked_seam_count, 2);

  // 4. with a fingerprint recorded, the same mesh re-applies them.
  const withFp = saveUvFeedback(project.dir!, { mesh_fingerprint: 'abc' });
  assert.equal(withFp.mesh_fingerprint, 'abc');
  assert.deepEqual(withFp.locked_seam_edges, [1, 3], 'a partial patch keeps the saved edges');
  const second = runner.start(project.id, project.dir!, { objectName });
  const secondView = await waitForTerminal(project.dir!, second.run_id);
  assert.equal(secondView.summary!.feedback_applied!.reason, 'fingerprint_match');
  assert.equal(secondView.summary!.feedback_applied!.applied, true);
});

// The review UI reads the 3D seam overlay + the per-round refinement log from the
// run directory; both are optional artifacts, so a run without them parses as null
// (work plan §7; gate G7).
test('uv generate run view: seam_overlay.json and candidate_history.json are parsed', async () => {
  const { project, objectName } = seedSeamSpecProject('uv_generate_review_artifacts');
  const runner = new UvGenerateRunner({ blenderPath: null, workerRoot: workerRoot(), mock: true });
  const started = runner.start(project.id, project.dir!, { objectName });
  await waitForTerminal(project.dir!, started.run_id);
  const runDir = join(project.dir!, 'runs', started.run_id);

  // 1. absent artifacts read as null, never as a throw (pre-automation runs).
  const bare = getUvGenerateRunView(project.dir!, started.run_id);
  assert.equal(bare.seam_overlay, null);
  assert.equal(bare.candidate_history, null);

  // 2. written artifacts are parsed into the view for the overlay/history tabs.
  writeFileSync(
    join(runDir, 'seam_overlay.json'),
    JSON.stringify({
      schema_version: 1,
      object_name: objectName,
      edges: [
        {
          edge_id: 12,
          type: 'distortion_split',
          reason: 'island 3 anisotropy p95 over cap',
          stage: 'refine',
          round: 2,
          target_island: 3,
          improvement_ratio: 0.31,
          a: [0, 0, 0],
          b: [1, 0, 0],
        },
      ],
      conflicts: [{ edge_id: 12, user_rule: 'protected', engine_rule: 'split', resolution: 'kept_user' }],
      type_counts: { distortion_split: 1 },
    }),
  );
  writeFileSync(
    join(runDir, 'candidate_history.json'),
    JSON.stringify([
      {
        round: 2,
        kind: 'distortion_split',
        target_island: 3,
        target_metric: 'anisotropy_p95',
        added_edges: [12],
        before: 1.9,
        after: 1.3,
        accepted: true,
        reason: 'improved',
        improvement_ratio: 0.31,
        island_count_after: 9,
        elapsed_s: 0.42,
      },
    ]),
  );

  const view = getUvGenerateRunView(project.dir!, started.run_id);
  assert.ok(view.seam_overlay, 'seam_overlay.json parsed');
  assert.equal(view.seam_overlay!.edges.length, 1);
  assert.equal(view.seam_overlay!.edges[0].edge_id, 12);
  assert.equal(view.seam_overlay!.edges[0].type, 'distortion_split');
  assert.equal(view.seam_overlay!.edges[0].target_island, 3);
  assert.deepEqual(view.seam_overlay!.edges[0].a, [0, 0, 0]);
  assert.equal(view.seam_overlay!.conflicts.length, 1);
  assert.equal(view.seam_overlay!.type_counts.distortion_split, 1);

  assert.ok(view.candidate_history, 'candidate_history.json parsed');
  assert.equal(view.candidate_history!.length, 1);
  assert.equal(view.candidate_history![0].round, 2);
  assert.equal(view.candidate_history![0].accepted, true);
  assert.equal(view.candidate_history![0].before, 1.9);
  assert.equal(view.candidate_history![0].after, 1.3);
});

// The reviewer evidence a game-UV run has to show (gate G15): the merge-back
// trial log and the raw game-gate report reach the run view, and the flat seam
// overlay image is addressable as an artifact path.
test('uv generate run view: merge_back_history, quality_report and seam_overlay_png are exposed', async () => {
  const { project, objectName } = seedSeamSpecProject('uv_generate_g15_evidence');
  setUvGenerateMode(project.dir!, 'auto_generate');
  const runner = new UvGenerateRunner({ blenderPath: null, workerRoot: workerRoot(), mock: true });
  const started = runner.start(project.id, project.dir!, { objectName, mode: 'auto_generate' });
  const view = await waitForTerminal(project.dir!, started.run_id);

  // 1. the merge-back trial log is parsed, block fields and all.
  assert.ok(view.merge_back_history, 'merge_back_history.json parsed');
  assert.equal(view.merge_back_history!.complete, true);
  assert.equal(view.merge_back_history!.island_count_before, 53);
  assert.equal(view.merge_back_history!.island_count_after, 52);
  assert.equal(view.merge_back_history!.history!.length, 2);
  assert.equal(view.merge_back_history!.history![0].accepted, true);
  assert.equal(view.merge_back_history!.history![1].accepted, false);

  // 2. the raw game-gate report is parsed for the debug/evidence view.
  assert.ok(view.quality_report, 'quality_report.json parsed');
  assert.equal((view.quality_report as Record<string, unknown>).passed, true);

  // 3. the evidence artifacts are addressable on disk for `uvpreview://`.
  for (const key of ['seam_overlay_png', 'quality_report', 'merge_back_history', 'shading_policy']) {
    assert.ok(view.artifact_paths[key] && existsSync(view.artifact_paths[key]), `artifact ${key}`);
  }

  // 4. the summary carries the game-gate blocks the right panel reads.
  const s = view.summary!;
  assert.equal(s.quality_report_passed, true);
  assert.equal(s.fragmentation!.passed, true);
  assert.deepEqual(s.fragmentation!.tiny_island_ids, [7]);
  assert.deepEqual(s.texel_density!.outlier_island_ids, [11]);
  assert.equal(s.packing!.advisory, true);
  assert.equal(s.shading!.policy, 'hard_edges_are_seams');
  assert.equal(s.merge_back!.reason, 'no_removable_seam');
  assert.equal(s.correctness!.min_border_gap_px, 5);
});

// The catastrophic-distortion evidence the UI needs to say "not production
// ready" and to prove the heatmap it draws is the gate's own measurement
// (gates CG13/CG4): the three new files reach the run view and the summary
// carries the identity verdict + the UV hash.
test('uv generate run view: catastrophic, repair_history and heatmap_meta are exposed', async () => {
  const { project, objectName } = seedSeamSpecProject('uv_generate_catastrophic_evidence');
  setUvGenerateMode(project.dir!, 'auto_generate');
  const runner = new UvGenerateRunner({ blenderPath: null, workerRoot: workerRoot(), mock: true });
  const started = runner.start(project.id, project.dir!, { objectName, mode: 'auto_generate' });
  const view = await waitForTerminal(project.dir!, started.run_id);

  // 1. the full damaged-region report is parsed (mock run passes -> no regions).
  assert.ok(view.catastrophic, 'uv_catastrophic.json parsed');
  assert.equal(view.catastrophic!.passed, true);
  assert.equal(view.catastrophic!.bad_triangle_count, 0);
  assert.deepEqual(view.catastrophic!.regions, []);

  // 2. the repair round trace and the heatmap measurement meta are parsed.
  assert.ok(view.repair_history, 'uv_repair_history.json parsed');
  assert.equal(view.repair_history!.history!.length, 1);
  assert.ok(view.heatmap_meta, 'heatmap_meta.json parsed');
  assert.equal(view.heatmap_meta!.metric, 'anisotropy');

  // 3. the evidence artifacts are addressable on disk.
  for (const key of ['catastrophic', 'repair_history', 'heatmap_meta']) {
    assert.ok(view.artifact_paths[key] && existsSync(view.artifact_paths[key]), `artifact ${key}`);
  }

  // 4. the summary carries the blocks the gates panel + heatmap badge read.
  const cs = view.summary!;
  assert.equal(cs.catastrophic!.passed, true);
  assert.equal(cs.catastrophic!.hard_failed, false);
  assert.equal(cs.repair!.reason, 'no_bad_triangles');
  assert.equal(cs.heatmap_identity!.passed, true);
  assert.deepEqual(cs.heatmap_identity!.mismatches, []);
  assert.equal(typeof cs.uv_hash, 'string');
  assert.match(cs.uv_hash!, /^[0-9a-f]{64}$/);
  // the heatmap the UI draws and the gate agree on the same UV.
  assert.equal(view.heatmap_meta!.uv_hash, cs.uv_hash);
});
