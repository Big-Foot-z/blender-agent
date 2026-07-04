/**
 * MVP 0 high->low preparation workspace (inspect / generate / approve).
 *
 * Extracted from the original MVP 0 App so MVP 1 can default to the UV review
 * workspace while keeping the preparation flow available behind a tab (plan §8).
 * Open/Import live in the shared app header; this owns the inspect/generate/approve
 * state and the run polling.
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import type {
  AppSettings,
  InspectResult,
  MeshObjectSummary,
  Project,
  RunView,
} from '@shared/contracts';
import { ReportTabs } from './ReportTabs';
import { ModelViewport } from './viewport/ModelViewport';
import type { Banner } from './App';
import { useT, statusLabel, type TFunc } from './i18n';
import { previewUrl } from './previewUrl';

const TERMINAL = new Set(['accepted', 'rejected', 'failed', 'cancelled']);

export function PrepareWorkspace(props: {
  project: Project | null;
  setProject: (p: Project) => void;
  settings: AppSettings | null;
  guard: (label: string, fn: () => Promise<void>) => Promise<void>;
  setBanner: (b: Banner) => void;
}): JSX.Element {
  const t = useT();
  const { project, setProject, guard, setBanner } = props;
  const [inspect, setInspect] = useState<InspectResult | null>(null);
  const [selectedObject, setSelectedObject] = useState<string>('');
  const [targetFaces, setTargetFaces] = useState<number>(12000);
  const [preserveFeatures, setPreserveFeatures] = useState<boolean>(true);
  const [proxyTargetFaces, setProxyTargetFaces] = useState<number>(1_000_000);
  const [retryLadderAttempts, setRetryLadderAttempts] = useState<number>(1);
  const [runId, setRunId] = useState<string | null>(null);
  const [runView, setRunView] = useState<RunView | null>(null);

  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  // Reset per-project state when the active project changes.
  useEffect(() => {
    setInspect(null);
    setSelectedObject('');
    setRunId(project?.approved_lowpoly_run_id ?? (project?.runs.length ? project.runs[project.runs.length - 1] : null));
    setRunView(null);
  }, [project?.id]);

  const refreshRun = useCallback(async () => {
    if (!project || !runId) return;
    const view = await window.api.runGet({ projectId: project.id, runId });
    setRunView(view);
    if (view.status && TERMINAL.has(view.status.status) && pollTimer.current) {
      clearInterval(pollTimer.current);
      pollTimer.current = null;
    }
  }, [project, runId]);

  useEffect(() => {
    if (!project || !runId) return;
    refreshRun();
    pollTimer.current = setInterval(refreshRun, 1000);
    const off = window.api.onRunUpdate((p) => {
      if (p.projectId === project.id && p.runId === runId) refreshRun();
    });
    return () => {
      if (pollTimer.current) clearInterval(pollTimer.current);
      pollTimer.current = null;
      off();
    };
  }, [project, runId, refreshRun]);

  const onInspect = () =>
    guard(t('busy.inspecting'), async () => {
      if (!project) return;
      const res = await window.api.modelInspect({ projectId: project.id });
      setInspect(res);
      if (res.status === 'accepted' && res.objects?.length) {
        setSelectedObject(res.objects[0].name);
      } else if (res.status === 'failed') {
        setBanner({ kind: 'error', text: res.error?.message ?? t('common.inspectFailed') });
      }
    });

  const onGenerate = () =>
    guard(t('busy.generating'), async () => {
      if (!project || !selectedObject) {
        setBanner({ kind: 'error', text: t('prepare.selectObjectFirst') });
        return;
      }
      const { run_id } = await window.api.lowpolyGenerate({
        projectId: project.id,
        objectName: selectedObject,
        targetFaces,
        options: {
          preserve_features: preserveFeatures,
          render_preview: true,
          proxy_target_faces: proxyTargetFaces,
          retry_ladder_max_attempts: retryLadderAttempts,
        },
      });
      setRunId(run_id);
      setRunView(null);
    });

  const onApprove = () =>
    guard(t('busy.approving'), async () => {
      if (!project || !runId) return;
      const res = await window.api.lowpolyApprove({ projectId: project.id, runId });
      const p = await window.api.projectGet(project.id);
      setProject(p);
      let text = t('prepare.approved', { model: res.working_model });
      // Bridge auto-refresh outcome (bridge plan §4.3): silent when no session.
      const refresh = res.blender_refresh;
      if (refresh?.attempted) {
        text += refresh.ok
          ? ` — ${t('prepare.blenderRefreshed')}`
          : ` — ${refresh.reason === 'dirty' ? t('prepare.blenderDirty') : t('prepare.blenderRefreshFailed', { reason: refresh.reason ?? '' })}`;
      }
      setBanner({ kind: 'info', text });
    });

  const onOpenInBlender = () =>
    guard(t('busy.openingBlender'), async () => {
      if (!project) return;
      const res = await window.api.blenderOpen({ projectId: project.id });
      setBanner({ kind: 'info', text: t('prepare.openedInBlender', { target: res.target }) });
    });

  const status = runView?.status?.status ?? null;
  const isApproved = !!project?.approved_lowpoly_run_id;
  const canApprove = status === 'accepted' && !!runView?.summary?.artifacts?.lowpoly_blend;

  return (
    <>
      <div className="subtoolbar">
        <button disabled={!project} onClick={onInspect}>{t('common.inspect')}</button>
        <button disabled={!selectedObject} onClick={onGenerate}>{t('prepare.generate')}</button>
        <button disabled={!canApprove} className="primary" onClick={onApprove}>{t('common.approve')}</button>
      </div>
      <div className="body">
        <LeftPanel
          project={project}
          inspect={inspect}
          selectedObject={selectedObject}
          onSelectObject={setSelectedObject}
          activeRunId={runId}
          onSelectRun={setRunId}
        />
        <main className="center">
          <PreviewPane runView={runView} />
        </main>
        <RightPanel
          objects={inspect?.objects ?? []}
          selectedObject={selectedObject}
          targetFaces={targetFaces}
          onTargetFaces={setTargetFaces}
          preserveFeatures={preserveFeatures}
          onPreserveFeatures={setPreserveFeatures}
          proxyTargetFaces={proxyTargetFaces}
          onProxyTargetFaces={setProxyTargetFaces}
          retryLadderAttempts={retryLadderAttempts}
          onRetryLadderAttempts={setRetryLadderAttempts}
          runView={runView}
          isApproved={isApproved}
          approvedRunId={project?.approved_lowpoly_run_id ?? null}
          onOpenInBlender={onOpenInBlender}
          projectId={project?.id ?? null}
          setBanner={setBanner}
        />
      </div>
      <BottomPanel runView={runView} status={status} />
    </>
  );
}

function LeftPanel(props: {
  project: Project | null;
  inspect: InspectResult | null;
  selectedObject: string;
  onSelectObject: (n: string) => void;
  activeRunId: string | null;
  onSelectRun: (id: string) => void;
}): JSX.Element {
  const t = useT();
  const { project, inspect } = props;
  return (
    <aside className="left">
      <section>
        <h3>{t('common.project')}</h3>
        {project ? (
          <div className="kv">
            <div>{project.name}</div>
            <div className="muted small">{project.dir}</div>
            <div className="small">
              {t('prepare.source')}: <code>{project.source_model}</code>
            </div>
            {project.working_model && (
              <div className="small ok">{t('prepare.working')}: {project.working_model}</div>
            )}
          </div>
        ) : (
          <div className="muted">{t('prepare.noProject')}</div>
        )}
      </section>

      <section>
        <h3>{t('common.objects')}</h3>
        {inspect?.objects?.length ? (
          <ul className="list">
            {inspect.objects.map((o: MeshObjectSummary) => (
              <li
                key={o.name}
                className={o.name === props.selectedObject ? 'sel' : ''}
                onClick={() => props.onSelectObject(o.name)}
              >
                {o.name} <span className={`tag ${o.mesh_role_hint}`}>{o.mesh_role_hint}</span>
                <div className="muted small">{t('common.facesCount', { n: o.faces.toLocaleString() })}</div>
              </li>
            ))}
          </ul>
        ) : (
          <div className="muted">{t('prepare.runInspect')}</div>
        )}
      </section>

      <section>
        <h3>{t('prepare.runs')}</h3>
        {project?.runs?.length ? (
          <ul className="list">
            {project.runs.map((r) => (
              <li
                key={r}
                className={r === props.activeRunId ? 'sel' : ''}
                onClick={() => props.onSelectRun(r)}
              >
                <code className="small">{r.replace('run_', '').slice(0, 8)}</code>
                {r === project.approved_lowpoly_run_id && <span className="tag ok">{t('common.tag.approved')}</span>}
              </li>
            ))}
          </ul>
        ) : (
          <div className="muted">{t('prepare.noRuns')}</div>
        )}
      </section>
    </aside>
  );
}

/** Absolute path of the run's `lowpoly.glb` (3D viewer plan §2.1), or null. */
function glbPathOf(runView: RunView | null): string | null {
  const rel = runView?.summary?.artifacts?.['lowpoly_glb'];
  if (!rel || !runView?.dir) return null;
  const sep = runView.dir.includes('\\') ? '\\' : '/';
  return `${runView.dir}${sep}${rel}`;
}

function PreviewPane(props: { runView: RunView | null }): JSX.Element {
  const t = useT();
  const preview = props.runView?.preview_path;
  const glbPath = glbPathOf(props.runView);
  // 3D view is the default when the run shipped a GLB; older runs fall back to
  // the rendered PNG (3D viewer plan §2.3).
  const [tab, setTab] = useState<'3d' | 'image'>('3d');
  const effectiveTab = glbPath ? tab : 'image';

  if (!preview && !glbPath) {
    return (
      <div className="preview">
        <div className="placeholder">
          {t('prepare.previewHint')}
          <div className="muted small">{t('prepare.previewSub')}</div>
        </div>
      </div>
    );
  }
  return (
    <div className="preview preview-tabs">
      <div className="tabbar">
        <button
          className={effectiveTab === '3d' ? 'active' : ''}
          disabled={!glbPath}
          title={glbPath ? '' : t('prepare.no3dArtifact')}
          onClick={() => setTab('3d')}
        >
          {t('prepare.tab3d')}
        </button>
        <button className={effectiveTab === 'image' ? 'active' : ''} onClick={() => setTab('image')}>
          {t('prepare.tabImage')}
        </button>
      </div>
      {effectiveTab === '3d' ? (
        <ModelViewport glbPath={glbPath} />
      ) : preview ? (
        <img alt={t('prepare.previewAlt')} src={previewUrl(preview)} />
      ) : (
        <div className="placeholder">{t('prepare.previewHint')}</div>
      )}
    </div>
  );
}

function RightPanel(props: {
  objects: MeshObjectSummary[];
  selectedObject: string;
  targetFaces: number;
  onTargetFaces: (n: number) => void;
  preserveFeatures: boolean;
  onPreserveFeatures: (b: boolean) => void;
  proxyTargetFaces: number;
  onProxyTargetFaces: (n: number) => void;
  retryLadderAttempts: number;
  onRetryLadderAttempts: (n: number) => void;
  runView: RunView | null;
  isApproved: boolean;
  approvedRunId: string | null;
  onOpenInBlender: () => void;
  projectId: string | null;
  setBanner: (b: Banner) => void;
}): JSX.Element {
  const t = useT();
  const obj = props.objects.find((o) => o.name === props.selectedObject) ?? null;
  const m = props.runView?.summary?.metrics ?? null;
  const warnings = props.runView?.summary?.warnings ?? [];
  return (
    <aside className="right">
      <section>
        <h3>{t('prepare.targetSetup')}</h3>
        <label className="field">
          {t('prepare.targetFaceCount')}
          <input
            type="number"
            min={100}
            step={500}
            value={props.targetFaces}
            onChange={(e) => props.onTargetFaces(Number(e.target.value))}
          />
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={props.preserveFeatures}
            onChange={(e) => props.onPreserveFeatures(e.target.checked)}
          />
          {t('prepare.preserveFeatures')}
        </label>
        <label className="field">
          {t('prepare.voxelProxy')}
          <input
            type="number"
            min={50000}
            step={100000}
            value={props.proxyTargetFaces}
            onChange={(e) => props.onProxyTargetFaces(Number(e.target.value))}
          />
          <span className="muted small">{t('prepare.voxelProxyHint')}</span>
        </label>
        <label className="field">
          {t('prepare.retryLadder')}
          <input
            type="number"
            min={0}
            max={6}
            step={1}
            value={props.retryLadderAttempts}
            onChange={(e) => props.onRetryLadderAttempts(Number(e.target.value))}
          />
          <span className="muted small">{t('prepare.retryLadderHint')}</span>
        </label>
      </section>

      <section>
        <h3>{t('prepare.meshSummary')}</h3>
        {obj ? (
          <table className="metrics">
            <tbody>
              <tr><td>{t('common.name')}</td><td>{obj.name}</td></tr>
              <tr><td>{t('common.role')}</td><td>{obj.mesh_role_hint}</td></tr>
              <tr><td>{t('common.vertices')}</td><td>{obj.vertices.toLocaleString()}</td></tr>
              <tr><td>{t('common.edges')}</td><td>{obj.edges.toLocaleString()}</td></tr>
              <tr><td>{t('common.faces')}</td><td>{obj.faces.toLocaleString()}</td></tr>
              <tr><td>{t('common.uvLayers')}</td><td>{obj.uv_layers.join(', ') || '—'}</td></tr>
            </tbody>
          </table>
        ) : (
          <div className="muted">{t('common.selectObject')}</div>
        )}
      </section>

      <section>
        <h3>{t('prepare.resultMetrics')}</h3>
        {m ? (
          <table className="metrics">
            <tbody>
              <tr><td>{t('prepare.row.targetFaces')}</td><td>{m.target_faces ?? '—'}</td></tr>
              <tr className="hl"><td>{t('prepare.row.actualFaces')}</td><td>{m.actual_faces ?? '—'}</td></tr>
              <tr><td>{t('prepare.row.sourceFaces')}</td><td>{m.source_faces ?? '—'}</td></tr>
              <tr><td>{t('prepare.row.nonManifold')}</td><td>{m.non_manifold_edges ?? '—'}</td></tr>
              <tr><td>{t('prepare.row.surfDistMean')}</td><td>{fmt(m.surface_distance_mean_ratio)}</td></tr>
              <tr><td>{t('prepare.row.normalDev')}</td><td>{fmt(m.normal_deviation_mean_deg)}</td></tr>
            </tbody>
          </table>
        ) : (
          <div className="muted">{t('prepare.runGenMetrics')}</div>
        )}
      </section>

      {warnings.length > 0 && (
        <section>
          <h3>{t('common.warnings')}</h3>
          <ul className="warnlist">
            {warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </section>
      )}

      {props.isApproved && (
        <section>
          <div className="ok">{t('prepare.approvedWorking')}</div>
          <div className="muted small">{props.approvedRunId}</div>
          <BlenderSection
            projectId={props.projectId}
            onOpenInBlender={props.onOpenInBlender}
            setBanner={props.setBanner}
            t={t}
          />
        </section>
      )}
    </aside>
  );
}

/**
 * Approved-model Blender actions (bridge plan §3.2, §4.3): one-click open in
 * Blender, live bridge connection indicator, and a manual scene refresh.
 */
function BlenderSection(props: {
  projectId: string | null;
  onOpenInBlender: () => void;
  setBanner: (b: Banner) => void;
  t: TFunc;
}): JSX.Element {
  const { t, projectId, setBanner } = props;
  const [bridge, setBridge] = useState<{ connected: boolean; file?: string | null } | null>(null);

  useEffect(() => {
    let alive = true;
    const poll = () => {
      window.api.bridgeStatus().then((s) => {
        if (alive) setBridge(s);
      }).catch(() => {
        if (alive) setBridge({ connected: false });
      });
    };
    poll();
    const timer = setInterval(poll, 5000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [projectId]);

  const onRefresh = async () => {
    if (!projectId) return;
    try {
      const res = await window.api.bridgeRefresh({ projectId });
      if (res.ok) {
        setBanner({ kind: 'info', text: t('prepare.blenderRefreshed') });
      } else if (res.reason === 'dirty') {
        setBanner({ kind: 'error', text: t('prepare.blenderDirty') });
      } else if (res.reason === 'not_loaded') {
        setBanner({ kind: 'error', text: t('prepare.blenderNotLoaded') });
      } else {
        setBanner({ kind: 'error', text: t('prepare.blenderRefreshFailed', { reason: res.reason ?? '' }) });
      }
    } catch (err) {
      setBanner({ kind: 'error', text: String((err as Error)?.message ?? err) });
    }
  };

  return (
    <div className="blender-actions">
      <button onClick={props.onOpenInBlender}>{t('prepare.openInBlender')}</button>
      <div className="small">
        <span className={bridge?.connected ? 'ok' : 'muted'}>
          {bridge?.connected ? t('prepare.bridgeConnected') : t('prepare.bridgeOffline')}
        </span>
        {bridge?.connected && (
          <button className="small" onClick={onRefresh} style={{ marginLeft: 8 }}>
            {t('prepare.refreshBlender')}
          </button>
        )}
      </div>
    </div>
  );
}

function BottomPanel(props: { runView: RunView | null; status: string | null }): JSX.Element {
  const t = useT();
  return (
    <footer className="bottom">
      <div className="statusrow">
        <span className={`statuspill ${props.status ?? 'idle'}`}>
          {statusLabel(t, props.status)}
        </span>
        {props.runView?.status?.error && (
          <span className="err">
            {props.runView.status.error.code}: {props.runView.status.error.message}
          </span>
        )}
      </div>
      <ReportTabs runView={props.runView} />
    </footer>
  );
}

function fmt(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—';
  return Number(v).toFixed(4);
}
