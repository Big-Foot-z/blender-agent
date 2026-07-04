/**
 * Context-isolated preload bridge (plan §3: renderer never calls Blender/fs).
 *
 * Exposes a typed `window.api` matching `RendererApi`; every method is a thin
 * `ipcRenderer.invoke` over the channels in `@shared/contracts`.
 */

import { contextBridge, ipcRenderer } from 'electron';
import { Ipc, UvIpc, SeamIpc, UvGenerateIpc, ExportIpc, type RendererApi } from '@shared/contracts';

const api: RendererApi = {
  projectCreate: (input) => ipcRenderer.invoke(Ipc.ProjectCreate, input),
  projectOpen: (dir) => ipcRenderer.invoke(Ipc.ProjectOpen, dir),
  projectGet: (projectId) => ipcRenderer.invoke(Ipc.ProjectGet, projectId),
  modelInspect: (input) => ipcRenderer.invoke(Ipc.ModelInspect, input),
  lowpolyGenerate: (input) => ipcRenderer.invoke(Ipc.LowpolyGenerate, input),
  lowpolyApprove: (input) => ipcRenderer.invoke(Ipc.LowpolyApprove, input),
  runGet: (input) => ipcRenderer.invoke(Ipc.RunGet, input),
  runList: (projectId) => ipcRenderer.invoke(Ipc.RunList, projectId),
  uvInspectLayers: (input) => ipcRenderer.invoke(UvIpc.InspectLayers, input),
  uvSetActiveLayer: (input) => ipcRenderer.invoke(UvIpc.SetActiveLayer, input),
  uvReviewExisting: (input) => ipcRenderer.invoke(UvIpc.ReviewExisting, input),
  uvGetReviewRun: (input) => ipcRenderer.invoke(UvIpc.GetReviewRun, input),
  seamExportEdgeGeometry: (input) => ipcRenderer.invoke(SeamIpc.ExportEdgeGeometry, input),
  seamExtractUvBoundary: (input) => ipcRenderer.invoke(SeamIpc.ExtractUvBoundary, input),
  seamGetEditorRun: (input) => ipcRenderer.invoke(SeamIpc.GetEditorRun, input),
  seamLoadSpec: (input) => ipcRenderer.invoke(SeamIpc.LoadSpec, input),
  seamValidateSpec: (input) => ipcRenderer.invoke(SeamIpc.ValidateSpec, input),
  seamSaveSpec: (input) => ipcRenderer.invoke(SeamIpc.SaveSpec, input),
  uvGenerateValidateInput: (input) => ipcRenderer.invoke(UvGenerateIpc.ValidateInput, input),
  uvGenerateStart: (input) => ipcRenderer.invoke(UvGenerateIpc.Start, input),
  uvGenerateCancel: (input) => ipcRenderer.invoke(UvGenerateIpc.Cancel, input),
  uvGenerateGetRun: (input) => ipcRenderer.invoke(UvGenerateIpc.GetRun, input),
  uvGenerateGetCandidateSummary: (input) => ipcRenderer.invoke(UvGenerateIpc.GetCandidateSummary, input),
  exportCheckReadiness: (input) => ipcRenderer.invoke(ExportIpc.CheckReadiness, input),
  exportStart: (input) => ipcRenderer.invoke(ExportIpc.Start, input),
  exportCancel: (input) => ipcRenderer.invoke(ExportIpc.Cancel, input),
  exportGetRun: (input) => ipcRenderer.invoke(ExportIpc.GetRun, input),
  exportListHistory: (input) => ipcRenderer.invoke(ExportIpc.ListHistory, input),
  exportListRollbackTargets: (input) => ipcRenderer.invoke(ExportIpc.ListRollbackTargets, input),
  exportRollback: (input) => ipcRenderer.invoke(ExportIpc.Rollback, input),
  exportRevealFile: (input) => ipcRenderer.invoke(ExportIpc.RevealFile, input),
  settingsGet: () => ipcRenderer.invoke(Ipc.SettingsGet),
  settingsSet: (patch) => ipcRenderer.invoke(Ipc.SettingsSet, patch),
  pickFile: () => ipcRenderer.invoke(Ipc.PickFile),
  pickProjectDir: () => ipcRenderer.invoke(Ipc.PickProjectDir),
  pickBlender: () => ipcRenderer.invoke(Ipc.PickBlender),
  blenderOpen: (input) => ipcRenderer.invoke(Ipc.BlenderOpen, input),
  bridgeStatus: () => ipcRenderer.invoke(Ipc.BridgeStatus),
  bridgeRefresh: (input) => ipcRenderer.invoke(Ipc.BridgeRefresh, input),
  bridgeInstall: () => ipcRenderer.invoke(Ipc.BridgeInstall),
  onRunUpdate: (cb) => {
    const listener = (_e: unknown, payload: { projectId: string; runId: string }) => cb(payload);
    ipcRenderer.on(Ipc.RunUpdate, listener);
    return () => ipcRenderer.removeListener(Ipc.RunUpdate, listener);
  },
};

contextBridge.exposeInMainWorld('api', api);
