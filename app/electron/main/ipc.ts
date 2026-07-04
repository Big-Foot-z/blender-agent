/**
 * IPC handler registration (plan §5 IPC API, Session C).
 *
 * Maps the renderer-visible IPC channels onto the project service + worker runner.
 * The renderer never touches the filesystem or spawns Blender directly (plan §3).
 */

import { app, dialog, ipcMain, shell, BrowserWindow } from 'electron';
import { existsSync, mkdirSync } from 'fs';
import { join, resolve } from 'path';
import {
  Ipc,
  UvIpc,
  SeamIpc,
  UvGenerateIpc,
  ExportIpc,
  type ExportOptions,
  type GenerateOptions,
  type GenerateUvOptions,
  type MeshRole,
  type ReviewOptions,
  type RollbackTargetType,
  type SeamSpec,
} from '@shared/contracts';
import {
  approveLowpoly,
  absSourcePath,
  createProject,
  getCandidateSummary,
  getExportRunView,
  getRunView,
  getSeamEditorRunView,
  getUvGenerateRunView,
  getUvReviewRunView,
  listRollbackTargets,
  listRuns,
  openProject,
  readHistory,
  readProject,
  resolveWorkingModel,
  rollbackProjectState,
  setSelectedUvLayer,
  writeProject,
} from './project-service';
import { WorkerRunner } from './worker-runner';
import { UvReviewRunner } from './uvReview';
import { SeamEditorRunner } from './seamEditor';
import { UvGenerateRunner } from './uvGenerate';
import { ExportRunner } from './exportRunner';
import { getSettings, setSettings } from './settings';
import { blenderOpen, installBridge } from './blenderLauncher';
import { bridgeRefresh, bridgeStatus } from './blenderBridge';
import type { ApproveBlenderRefresh } from '@shared/contracts';

/** Resolve the `worker/` directory holding the Python/Blender worker scripts.
 *
 * Packaged: the Python source tree is shipped as `extraResources` under
 * `<resources>/pysrc/` (NOT inside asar — Blender runs as a separate process and
 * can't read asar). The workers `sys.path.insert(dirname(worker_dir))`, so the
 * agent packages must sit next to `worker/` (i.e. `pysrc/worker`, `pysrc/uv_agent`,
 * …) — which the electron-builder `extraResources` mapping preserves.
 *
 * Dev/build-from-repo: the app dir is `<repo>/app`; workers live at `<repo>/worker`
 * and `__dirname` is `<repo>/app/out/main`. */
function resolveWorkerRoot(): string {
  if (app.isPackaged) {
    return join(process.resourcesPath, 'pysrc', 'worker');
  }
  const candidates = [
    resolve(__dirname, '../../../worker'),
    resolve(__dirname, '../../worker'),
    resolve(process.cwd(), '../worker'),
    resolve(process.cwd(), 'worker'),
  ];
  for (const c of candidates) {
    if (existsSync(join(c, 'run_app_retopo_job.py'))) return c;
  }
  return candidates[0];
}

/** Index of created/opened projects by id -> absolute dir, for this session. */
const projectDirs = new Map<string, string>();

function dirForProject(projectId: string): string {
  const dir = projectDirs.get(projectId);
  if (!dir) throw new Error(`unknown project id: ${projectId} (open it first)`);
  return dir;
}

function broadcastRunUpdate(projectId: string, runId: string): void {
  for (const win of BrowserWindow.getAllWindows()) {
    win.webContents.send(Ipc.RunUpdate, { projectId, runId });
  }
}

function makeRunner(): WorkerRunner {
  const settings = getSettings();
  return new WorkerRunner({
    blenderPath: settings.blenderPath,
    workerRoot: resolveWorkerRoot(),
    onRunUpdate: broadcastRunUpdate,
  });
}

function makeUvRunner(): UvReviewRunner {
  const settings = getSettings();
  return new UvReviewRunner({
    blenderPath: settings.blenderPath,
    workerRoot: resolveWorkerRoot(),
    onRunUpdate: broadcastRunUpdate,
  });
}

function makeSeamRunner(): SeamEditorRunner {
  const settings = getSettings();
  return new SeamEditorRunner({
    blenderPath: settings.blenderPath,
    workerRoot: resolveWorkerRoot(),
    onRunUpdate: broadcastRunUpdate,
  });
}

// MVP 3 generate runs are long-lived (cancellable) — keep ONE runner per session
// so `cancel` can reach the spawned child it started (plan §8, §11 Session E).
let uvGenerateRunner: UvGenerateRunner | null = null;
function makeUvGenerateRunner(): UvGenerateRunner {
  if (!uvGenerateRunner) {
    uvGenerateRunner = new UvGenerateRunner({
      blenderPath: getSettings().blenderPath,
      workerRoot: resolveWorkerRoot(),
      onRunUpdate: broadcastRunUpdate,
    });
  } else {
    uvGenerateRunner.setBlenderPath(getSettings().blenderPath);
  }
  return uvGenerateRunner;
}

// MVP 5 export runs are long-lived (cancellable) — keep ONE runner per session
// so `cancel` can reach the spawned child it started (plan §12 Session E).
let exportRunner: ExportRunner | null = null;
function makeExportRunner(): ExportRunner {
  if (!exportRunner) {
    exportRunner = new ExportRunner({
      blenderPath: getSettings().blenderPath,
      workerRoot: resolveWorkerRoot(),
      onRunUpdate: broadcastRunUpdate,
    });
  } else {
    exportRunner.setBlenderPath(getSettings().blenderPath);
  }
  return exportRunner;
}

export function registerIpc(): void {
  ipcMain.handle(Ipc.SettingsGet, () => getSettings());
  ipcMain.handle(Ipc.SettingsSet, (_e, patch) => setSettings(patch));

  ipcMain.handle(Ipc.PickFile, async () => {
    const res = await dialog.showOpenDialog({
      properties: ['openFile'],
      filters: [{ name: 'Models', extensions: ['fbx', 'obj', 'glb', 'gltf'] }],
    });
    return res.canceled ? null : res.filePaths[0];
  });

  ipcMain.handle(Ipc.PickProjectDir, async () => {
    const res = await dialog.showOpenDialog({ properties: ['openDirectory'] });
    return res.canceled ? null : res.filePaths[0];
  });

  // Pick the Blender executable with OS-aware filters. On macOS the user selects
  // `Blender.app`; we resolve it to the inner CLI binary the worker actually spawns.
  ipcMain.handle(Ipc.PickBlender, async () => {
    const filters =
      process.platform === 'win32'
        ? [{ name: 'Blender', extensions: ['exe'] }]
        : process.platform === 'darwin'
          ? [{ name: 'Blender', extensions: ['app'] }]
          : [{ name: 'All files', extensions: ['*'] }];
    const res = await dialog.showOpenDialog({
      title: 'Select the Blender executable',
      properties: ['openFile'],
      filters,
    });
    if (res.canceled || !res.filePaths[0]) return null;
    let p = res.filePaths[0];
    if (process.platform === 'darwin' && p.endsWith('.app')) {
      p = join(p, 'Contents', 'MacOS', 'Blender');
    }
    return p;
  });

  ipcMain.handle(Ipc.ProjectCreate, (_e, input: { name: string; sourcePath: string; role?: MeshRole }) => {
    const settings = getSettings();
    const root = settings.projectsRoot ?? process.cwd();
    if (!existsSync(root)) mkdirSync(root, { recursive: true });
    const project = createProject({ root, name: input.name, sourcePath: input.sourcePath, role: input.role });
    projectDirs.set(project.id, project.dir as string);
    return project;
  });

  ipcMain.handle(Ipc.ProjectOpen, (_e, dir: string) => {
    const project = openProject(dir);
    projectDirs.set(project.id, dir);
    return project;
  });

  ipcMain.handle(Ipc.ProjectGet, (_e, projectId: string) => readProject(dirForProject(projectId)));

  ipcMain.handle(Ipc.ModelInspect, async (_e, input: { projectId: string; path?: string }) => {
    const dir = dirForProject(input.projectId);
    const project = readProject(dir);
    const sourcePath = input.path ?? absSourcePath(project);
    return makeRunner().inspect(input.projectId, dir, sourcePath);
  });

  ipcMain.handle(
    Ipc.LowpolyGenerate,
    (_e, input: { projectId: string; objectName: string; targetFaces: number; options?: GenerateOptions }) => {
      const dir = dirForProject(input.projectId);
      const project = readProject(dir);
      // Persist the chosen object on the project manifest.
      if (project.selected_object !== input.objectName) {
        project.selected_object = input.objectName;
        writeProject(dir, project);
      }
      return makeRunner().generate(input.projectId, dir, {
        sourceModel: absSourcePath(project),
        objectName: input.objectName,
        targetFaces: input.targetFaces,
        options: input.options,
      });
    },
  );

  ipcMain.handle(Ipc.LowpolyApprove, async (_e, input: { projectId: string; runId: string }) => {
    const dir = dirForProject(input.projectId);
    const result = approveLowpoly(dir, input.runId);
    // Bridge auto-refresh hook (bridge plan §4.3): best-effort, never fails the
    // approval. No bridge session -> silently skip (blender_refresh = null).
    let blenderRefresh: ApproveBlenderRefresh | null = null;
    if (getSettings().autoRefreshBlender !== false) {
      try {
        const status = await bridgeStatus();
        if (status.connected) {
          const res = await bridgeRefresh(dir);
          blenderRefresh = { attempted: true, ok: res.ok, mode: res.mode, reason: res.reason };
        }
      } catch (err) {
        blenderRefresh = { attempted: true, ok: false, reason: String((err as Error).message) };
      }
    }
    return { ...result, blender_refresh: blenderRefresh };
  });

  // --- Blender launcher + bridge (3D viewer + bridge plan §3, §4) --------
  ipcMain.handle(Ipc.BlenderOpen, (_e, input: { projectId: string }) =>
    blenderOpen({
      projectDir: dirForProject(input.projectId),
      blenderPath: getSettings().blenderPath,
      workerRoot: resolveWorkerRoot(),
    }),
  );

  ipcMain.handle(Ipc.BridgeStatus, () => bridgeStatus());

  ipcMain.handle(Ipc.BridgeRefresh, (_e, input: { projectId: string }) =>
    bridgeRefresh(dirForProject(input.projectId)),
  );

  ipcMain.handle(Ipc.BridgeInstall, () =>
    installBridge({ blenderPath: getSettings().blenderPath, workerRoot: resolveWorkerRoot() }),
  );

  ipcMain.handle(Ipc.RunGet, (_e, input: { projectId: string; runId: string }) =>
    getRunView(dirForProject(input.projectId), input.runId),
  );

  ipcMain.handle(Ipc.RunList, (_e, projectId: string) => listRuns(dirForProject(projectId)));

  // --- MVP 1 UV review (plan §5 IPC API, Session D) ---------------------
  ipcMain.handle(UvIpc.InspectLayers, (_e, input: { projectId: string; modelPath?: string }) => {
    const dir = dirForProject(input.projectId);
    const project = readProject(dir);
    if (input.modelPath) {
      return makeUvRunner().inspectLayers(input.projectId, input.modelPath, input.modelPath);
    }
    const { abs, rel } = resolveWorkingModel(project);
    return makeUvRunner().inspectLayers(input.projectId, abs, rel);
  });

  ipcMain.handle(
    UvIpc.SetActiveLayer,
    (_e, input: { projectId: string; objectName: string; uvLayer: string }) =>
      setSelectedUvLayer(dirForProject(input.projectId), input.objectName, input.uvLayer),
  );

  ipcMain.handle(
    UvIpc.ReviewExisting,
    (
      _e,
      input: { projectId: string; objectName: string; uvLayer: string; options?: ReviewOptions },
    ) => {
      const dir = dirForProject(input.projectId);
      const project = readProject(dir);
      // Persist the chosen object + layer on the manifest (plan §5.3, §9).
      setSelectedUvLayer(dir, input.objectName, input.uvLayer);
      const { abs, rel } = resolveWorkingModel(project);
      return makeUvRunner().reviewExisting(input.projectId, dir, {
        modelAbs: abs,
        modelRel: rel,
        objectName: input.objectName,
        uvLayer: input.uvLayer,
        options: input.options,
      });
    },
  );

  ipcMain.handle(UvIpc.GetReviewRun, (_e, input: { projectId: string; runId: string }) =>
    getUvReviewRunView(dirForProject(input.projectId), input.runId),
  );

  // --- MVP 2 seam editor (plan §11 Session D IPC API) -------------------
  ipcMain.handle(
    SeamIpc.ExportEdgeGeometry,
    (_e, input: { projectId: string; objectName: string }) => {
      const dir = dirForProject(input.projectId);
      const project = readProject(dir);
      // Persist the chosen object so MVP 3 has model + object + spec (plan §10).
      if (project.selected_object !== input.objectName) {
        project.selected_object = input.objectName;
        writeProject(dir, project);
      }
      const { abs, rel } = resolveWorkingModel(project);
      return makeSeamRunner().exportEdgeGeometry(input.projectId, dir, {
        modelAbs: abs,
        modelRel: rel,
        objectName: input.objectName,
      });
    },
  );

  ipcMain.handle(
    SeamIpc.ExtractUvBoundary,
    (_e, input: { projectId: string; objectName: string; uvLayer?: string }) => {
      const dir = dirForProject(input.projectId);
      const project = readProject(dir);
      const { abs, rel } = resolveWorkingModel(project);
      return makeSeamRunner().extractUvBoundary(input.projectId, dir, {
        modelAbs: abs,
        modelRel: rel,
        objectName: input.objectName,
        uvLayer: input.uvLayer,
      });
    },
  );

  ipcMain.handle(SeamIpc.GetEditorRun, (_e, input: { projectId: string; runId: string }) =>
    getSeamEditorRunView(dirForProject(input.projectId), input.runId),
  );

  ipcMain.handle(
    SeamIpc.ValidateSpec,
    (_e, input: { projectId: string; spec: SeamSpec; objectName: string; edgeCount?: number | null }) =>
      makeSeamRunner().validateSpec(input),
  );

  ipcMain.handle(
    SeamIpc.SaveSpec,
    (_e, input: { projectId: string; spec: SeamSpec; objectName: string; edgeCount?: number | null }) =>
      makeSeamRunner().saveSpec(dirForProject(input.projectId), input),
  );

  ipcMain.handle(
    SeamIpc.LoadSpec,
    (
      _e,
      input: { projectId: string; path?: string; objectName: string; edgeCount?: number | null },
    ) => makeSeamRunner().loadSpec(dirForProject(input.projectId), input),
  );

  // --- MVP 3 generate + optimize (plan §11 Session E IPC API) -----------
  ipcMain.handle(UvGenerateIpc.ValidateInput, (_e, input: { projectId: string }) =>
    makeUvGenerateRunner().validateInput(dirForProject(input.projectId)),
  );

  ipcMain.handle(
    UvGenerateIpc.Start,
    (_e, input: { projectId: string; objectName?: string; options?: GenerateUvOptions }) => {
      const dir = dirForProject(input.projectId);
      // Persist the chosen object so the run + manifest agree (plan §9).
      if (input.objectName) {
        const project = readProject(dir);
        if (project.selected_object !== input.objectName) {
          project.selected_object = input.objectName;
          writeProject(dir, project);
        }
      }
      return makeUvGenerateRunner().start(input.projectId, dir, {
        objectName: input.objectName,
        options: input.options,
      });
    },
  );

  ipcMain.handle(UvGenerateIpc.Cancel, (_e, input: { projectId: string; runId: string }) =>
    makeUvGenerateRunner().cancel(dirForProject(input.projectId), input.runId),
  );

  ipcMain.handle(UvGenerateIpc.GetRun, (_e, input: { projectId: string; runId: string }) =>
    getUvGenerateRunView(dirForProject(input.projectId), input.runId),
  );

  ipcMain.handle(
    UvGenerateIpc.GetCandidateSummary,
    (_e, input: { projectId: string; runId: string }) =>
      getCandidateSummary(dirForProject(input.projectId), input.runId),
  );

  // --- MVP 5 production export (plan §12 Session E IPC API) -------------
  ipcMain.handle(ExportIpc.CheckReadiness, (_e, input: { projectId: string }) =>
    makeExportRunner().checkReadiness(dirForProject(input.projectId)),
  );

  ipcMain.handle(
    ExportIpc.Start,
    (_e, input: { projectId: string; formats: string[]; options?: ExportOptions }) =>
      makeExportRunner().start(input.projectId, dirForProject(input.projectId), {
        formats: input.formats,
        options: input.options,
      }),
  );

  ipcMain.handle(ExportIpc.Cancel, (_e, input: { projectId: string; exportId: string }) =>
    makeExportRunner().cancel(dirForProject(input.projectId), input.exportId),
  );

  ipcMain.handle(ExportIpc.GetRun, (_e, input: { projectId: string; exportId: string }) =>
    getExportRunView(dirForProject(input.projectId), input.exportId),
  );

  ipcMain.handle(ExportIpc.ListHistory, (_e, input: { projectId: string }) =>
    readHistory(dirForProject(input.projectId)).events,
  );

  ipcMain.handle(ExportIpc.ListRollbackTargets, (_e, input: { projectId: string }) =>
    listRollbackTargets(dirForProject(input.projectId)),
  );

  ipcMain.handle(
    ExportIpc.Rollback,
    (_e, input: { projectId: string; targetType: RollbackTargetType; targetId: string }) =>
      rollbackProjectState(dirForProject(input.projectId), {
        targetType: input.targetType,
        targetId: input.targetId,
      }),
  );

  ipcMain.handle(
    ExportIpc.RevealFile,
    (_e, input: { projectId: string; exportId: string; key: string }) => {
      const view = getExportRunView(dirForProject(input.projectId), input.exportId);
      const abs = view.file_paths[input.key] ?? view.artifact_paths[input.key];
      if (abs && existsSync(abs)) {
        shell.showItemInFolder(abs);
        return true;
      }
      return false;
    },
  );
}
