/**
 * MVP 5 Production Export workspace (plan §10). Exports the MVP 3 accepted
 * `selected_uv_model` to FBX/OBJ/GLB/GLTF with validation, shows export history,
 * and supports rollback to a previous UV run or export.
 *
 * The renderer only reads normalized JSON (`export_manifest.json` /
 * `validation_report.json` / `project_history.json`) + artifact paths; it never
 * parses Blender stdout (plan §3, §10). UI wording stays honest: "production
 * asset ready" only when export validation passed for ≥1 format, partial exports
 * always show which formats failed, and the MVP 4 AI Review skip is shown as an
 * informational line, never a blocker (plan §0, §10).
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import type {
  ExportManifest,
  ExportMetrics,
  ExportOptions,
  ExportReadiness,
  ExportRunView,
  FormatValidation,
  HistoryEvent,
  Project,
  RollbackTarget,
} from '@shared/contracts';
import {
  DEFAULT_EXPORT_OPTIONS,
  EXPORT_TERMINAL_STATUSES,
  GENERATED_UV_LAYER,
  SUPPORTED_EXPORT_FORMATS,
} from '@shared/contracts';
import type { Banner } from '../App';
import { useT, statusLabel, type TKey, type TFunc } from '../i18n';
import { previewUrl } from '../previewUrl';

type CenterTab = 'files' | 'validation' | 'preview';
type PreviewView = 'uv_layout' | 'checker_front' | 'checker_side';
type BottomTab = 'manifest' | 'validation' | 'logs';

const PREVIEW_VIEW_KEY: Record<PreviewView, TKey> = {
  uv_layout: 'view.uv_layout',
  checker_front: 'view.checker_front',
  checker_side: 'view.checker_side',
};

export function ExportWorkspace(props: {
  project: Project | null;
  setProject: (p: Project) => void;
  guard: (label: string, fn: () => Promise<void>) => Promise<void>;
  setBanner: (b: Banner) => void;
}): JSX.Element {
  const t = useT();
  const { project, guard, setBanner } = props;
  const [readiness, setReadiness] = useState<ExportReadiness | null>(null);
  const [exportId, setExportId] = useState<string | null>(null);
  const [runView, setRunView] = useState<ExportRunView | null>(null);
  const [formats, setFormats] = useState<string[]>(['fbx', 'obj', 'glb']);
  const [options, setOptions] = useState<ExportOptions>({ ...DEFAULT_EXPORT_OPTIONS });
  const [history, setHistory] = useState<HistoryEvent[]>([]);
  const [rollbackTargets, setRollbackTargets] = useState<RollbackTarget[]>([]);
  const [centerTab, setCenterTab] = useState<CenterTab>('files');
  const [previewView, setPreviewView] = useState<PreviewView>('checker_front');

  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  // Reset per-project; seed the latest export + UV layer from the manifest (plan §2).
  useEffect(() => {
    setReadiness(null);
    setRunView(null);
    setExportId(project?.latest_export_id ?? null);
    setFormats(['fbx', 'obj', 'glb']);
    // Export must ship the MVP 3 OPTIMIZED layer (`AI_UV`), NOT the original review layer
    // (`project.selected_uv_layer`, e.g. `UVChannel_1`) — pre-filling the latter forced the
    // un-optimized UVs to export (preview ≠ exported OBJ bug). Default to the generated layer.
    setOptions({ ...DEFAULT_EXPORT_OPTIONS, selected_uv_layer: GENERATED_UV_LAYER });
    setHistory([]);
    setRollbackTargets([]);
  }, [project?.id]);

  const refreshHistory = useCallback(async () => {
    if (!project) return;
    setHistory(await window.api.exportListHistory({ projectId: project.id }));
    const targets = await window.api.exportListRollbackTargets({ projectId: project.id });
    setRollbackTargets(targets.targets);
  }, [project]);

  useEffect(() => {
    if (project) refreshHistory();
  }, [project?.id, refreshHistory]);

  const refreshRun = useCallback(async () => {
    if (!project || !exportId) return;
    const view = await window.api.exportGetRun({ projectId: project.id, exportId });
    setRunView(view);
    if (view.status && EXPORT_TERMINAL_STATUSES.has(view.status.status) && pollTimer.current) {
      clearInterval(pollTimer.current);
      pollTimer.current = null;
    }
  }, [project, exportId]);

  useEffect(() => {
    if (!project || !exportId) return;
    refreshRun();
    pollTimer.current = setInterval(refreshRun, 1000);
    const off = window.api.onRunUpdate((p) => {
      if (p.projectId === project.id && p.runId === exportId) {
        refreshRun();
        refreshHistory();
      }
    });
    return () => {
      if (pollTimer.current) clearInterval(pollTimer.current);
      pollTimer.current = null;
      off();
    };
  }, [project, exportId, refreshRun, refreshHistory]);

  const onCheck = () =>
    guard(t('busy.checkingReadiness'), async () => {
      if (!project) {
        setBanner({ kind: 'error', text: t('common.openImportFirst') });
        return;
      }
      const r = await window.api.exportCheckReadiness({ projectId: project.id });
      setReadiness(r);
      setBanner(
        r.ready
          ? { kind: 'info', text: t('export.readyPassed') }
          : { kind: 'error', text: r.blocking_issues[0]?.message ?? t('export.notReady') },
      );
    });

  const onExport = () =>
    guard(t('busy.exportingAsset'), async () => {
      if (!project) {
        setBanner({ kind: 'error', text: t('common.openImportFirst') });
        return;
      }
      if (formats.length === 0) {
        setBanner({ kind: 'error', text: t('export.selectFormat') });
        return;
      }
      const r = readiness ?? (await window.api.exportCheckReadiness({ projectId: project.id }));
      setReadiness(r);
      if (!r.ready) {
        setBanner({ kind: 'error', text: r.blocking_issues[0]?.message ?? t('export.notReady') });
        return;
      }
      const { export_id } = await window.api.exportStart({ projectId: project.id, formats, options });
      setExportId(export_id);
      setRunView(null);
      setCenterTab('files');
      const p = await window.api.projectGet(project.id);
      props.setProject(p);
    });

  const onCancel = () =>
    guard(t('busy.cancellingExport'), async () => {
      if (!project || !exportId) return;
      await window.api.exportCancel({ projectId: project.id, exportId });
      await refreshRun();
    });

  const onReveal = (key: string) =>
    guard(t('busy.revealingFile'), async () => {
      if (!project || !exportId) return;
      const ok = await window.api.exportRevealFile({ projectId: project.id, exportId, key });
      if (!ok) setBanner({ kind: 'error', text: t('export.fileNotFound', { key }) });
    });

  const onRollback = (target: RollbackTarget) =>
    guard(t('busy.rollingBack'), async () => {
      if (!project) return;
      const shortId =
        target.type === 'uv_run'
          ? target.id.replace('uv_run_', '').slice(0, 8)
          : target.id.replace('export_', '').slice(0, 8);
      const label =
        target.type === 'uv_run'
          ? t('export.uvRunLabel', { id: shortId })
          : t('export.exportLabel', { id: shortId });
      // eslint-disable-next-line no-alert
      if (!window.confirm(t('export.rollbackConfirm', { label }))) return;
      const res = await window.api.exportRollback({
        projectId: project.id,
        targetType: target.type,
        targetId: target.id,
      });
      const p = await window.api.projectGet(project.id);
      props.setProject(p);
      await refreshHistory();
      setBanner({
        kind: 'info',
        text: t('export.rolledBack', {
          type: target.type === 'uv_run' ? t('export.targetUvRun') : t('export.targetExport'),
          id: target.id,
          event: res.history_event.slice(0, 14),
        }),
      });
    });

  const status = runView?.status?.status ?? null;
  const running = !!status && !EXPORT_TERMINAL_STATUSES.has(status);

  return (
    <>
      <div className="subtoolbar">
        <button disabled={!project} onClick={onCheck}>{t('export.check')}</button>
        <button disabled={!project || running} className="primary" onClick={onExport}>{t('export.export')}</button>
        <button disabled={!running} onClick={onCancel}>{t('common.cancel')}</button>
        <button disabled={!project} onClick={() => guard(t('busy.refreshingHistory'), refreshHistory)}>
          {t('export.refreshHistory')}
        </button>
        {project && <span className="muted small subtoolbar-hint">{project.name}</span>}
      </div>

      <div className="body">
        <ExportLeftPanel
          project={project}
          readiness={readiness}
          history={history}
          rollbackTargets={rollbackTargets}
          activeExportId={exportId}
          onSelectExport={setExportId}
          onRollback={onRollback}
        />

        <main className="center uv-center">
          <ExportCenter
            runView={runView}
            status={status}
            centerTab={centerTab}
            setCenterTab={setCenterTab}
            previewView={previewView}
            setPreviewView={setPreviewView}
            onReveal={onReveal}
          />
        </main>

        <ExportRightPanel
          runView={runView}
          readiness={readiness}
          formats={formats}
          setFormats={setFormats}
          options={options}
          setOptions={setOptions}
          running={running}
        />
      </div>

      <ExportBottomPanel runView={runView} status={status} />
    </>
  );
}

// ---------------------------------------------------------------------------
// Left panel: project / selected UV / export history / rollback targets
// ---------------------------------------------------------------------------
function ExportLeftPanel(props: {
  project: Project | null;
  readiness: ExportReadiness | null;
  history: HistoryEvent[];
  rollbackTargets: RollbackTarget[];
  activeExportId: string | null;
  onSelectExport: (id: string) => void;
  onRollback: (t: RollbackTarget) => void;
}): JSX.Element {
  const t = useT();
  const { project, readiness, history, rollbackTargets } = props;
  return (
    <aside className="left">
      <section>
        <h3>{t('common.project')}</h3>
        {project ? (
          <div className="kv">
            <div>{project.name}</div>
            <div className="small">{t('common.objectRow')}: <code>{project.selected_object ?? '—'}</code></div>
          </div>
        ) : (
          <div className="muted">{t('common.noProjectOpen')}</div>
        )}
      </section>

      <section>
        <h3>{t('export.selectedUv')}</h3>
        {project?.selected_uv_model ? (
          <div className="small">
            <code>{project.selected_uv_model}</code>
            {readiness && (
              <div className={`tag ${readiness.ready ? 'ok' : 'unknown'}`}>
                {readiness.ready ? t('export.tag.ready') : t('export.tag.notReady')}
              </div>
            )}
            <div className="muted small">{t('export.aiSkipped')}</div>
          </div>
        ) : (
          <div className="muted small">{t('export.noSelectedUv')}</div>
        )}
        {readiness && readiness.blocking_issues.length > 0 && (
          <ul className="issuelist">
            {readiness.blocking_issues.map((iss, i) => (
              <li key={i} className="error">
                <span className="sevdot error" /> {iss.message}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section>
        <h3>{t('export.history')}</h3>
        {history.length ? (
          <ul className="list history">
            {history
              .slice()
              .reverse()
              .map((ev, i) => (
                <li
                  key={ev.id ?? i}
                  className={ev.export_id && ev.export_id === props.activeExportId ? 'sel' : ''}
                  onClick={() => ev.export_id && props.onSelectExport(ev.export_id)}
                >
                  <span className={`tag ${historyTagClass(ev)}`}>{historyLabel(t, ev)}</span>{' '}
                  {ev.export_id && (
                    <code className="small">{ev.export_id.replace('export_', '').slice(0, 8)}</code>
                  )}
                  {ev.type === 'rollback_performed' && (
                    <code className="small">{ev.target_type}:{(ev.target_id ?? '').slice(-8)}</code>
                  )}
                  {ev.summary?.formats && (
                    <div className="muted small">{ev.summary.formats.join(', ')}</div>
                  )}
                </li>
              ))}
          </ul>
        ) : (
          <div className="muted small">{t('export.noHistory')}</div>
        )}
      </section>

      <section>
        <h3>{t('export.rollbackTargets')}</h3>
        {rollbackTargets.length ? (
          <ul className="list rollback">
            {rollbackTargets.map((target) => (
              <li key={`${target.type}_${target.id}`}>
                <div>
                  <span className={`tag ${target.type === 'uv_run' ? 'ok' : 'unknown'}`}>
                    {target.type === 'uv_run' ? t('export.targetUvRun') : t('export.targetExport')}
                  </span>{' '}
                  <code className="small">{target.id.replace(/^(uv_run_|export_)/, '').slice(0, 8)}</code>
                </div>
                {target.type === 'uv_run' && target.selected_candidate_id && (
                  <div className="muted small">{t('export.candidateRow', { id: target.selected_candidate_id })}</div>
                )}
                {target.type === 'export' && target.formats && (
                  <div className="muted small">{target.formats.join(', ')}</div>
                )}
                <button className="small" onClick={() => props.onRollback(target)}>{t('export.rollback')}</button>
              </li>
            ))}
          </ul>
        ) : (
          <div className="muted small">{t('export.noRollback')}</div>
        )}
      </section>
    </aside>
  );
}

function historyLabel(t: TFunc, ev: HistoryEvent): string {
  if (ev.type === 'export_created') return t('export.hist.export');
  if (ev.type === 'export_failed') return t('export.hist.failed');
  if (ev.type === 'rollback_performed') return t('export.hist.rollback');
  return ev.type;
}
function historyTagClass(ev: HistoryEvent): string {
  if (ev.type === 'export_created') return 'ok';
  if (ev.type === 'export_failed') return 'bad';
  return 'unknown';
}

// ---------------------------------------------------------------------------
// Center: status banner + Exported Files | Validation | Preview
// ---------------------------------------------------------------------------
function ExportCenter(props: {
  runView: ExportRunView | null;
  status: string | null;
  centerTab: CenterTab;
  setCenterTab: (t: CenterTab) => void;
  previewView: PreviewView;
  setPreviewView: (v: PreviewView) => void;
  onReveal: (key: string) => void;
}): JSX.Element {
  const t = useT();
  const { runView, status } = props;

  if (!runView || (status && !EXPORT_TERMINAL_STATUSES.has(status))) {
    return (
      <div className="preview">
        <div className="placeholder">
          {status
            ? t('export.exporting', { status: statusLabel(t, status) })
            : t('export.runHint')}
        </div>
      </div>
    );
  }
  if (status === 'failed') {
    return (
      <div className="preview">
        <div className="placeholder nouv">
          <div className="nouv-title">{t('export.failed')}</div>
          <div className="muted">{runView.status?.error?.message ?? t('common.seeLogs')}</div>
          <FailedFormats runView={runView} />
        </div>
      </div>
    );
  }

  const manifest = runView.manifest;
  const validation = runView.validation;
  const paths = runView.artifact_paths;

  return (
    <div className="uv-tabs">
      <ExportStatusBanner status={status} manifest={manifest} runView={runView} />
      <nav className="tabbar">
        <button className={props.centerTab === 'files' ? 'active' : ''} onClick={() => props.setCenterTab('files')}>
          {t('export.exportedFiles')}
        </button>
        <button className={props.centerTab === 'validation' ? 'active' : ''} onClick={() => props.setCenterTab('validation')}>
          {t('export.validation')}
        </button>
        <button className={props.centerTab === 'preview' ? 'active' : ''} onClick={() => props.setCenterTab('preview')}>
          {t('export.preview')}
        </button>
      </nav>

      <div className="uv-tabbody">
        {props.centerTab === 'files' && (
          <ExportedFiles runView={runView} onReveal={props.onReveal} />
        )}
        {props.centerTab === 'validation' && <ValidationTable validation={validation} />}
        {props.centerTab === 'preview' && (
          <div className="checker-wrap">
            <div className="viewtoggle">
              {(['checker_front', 'checker_side', 'uv_layout'] as PreviewView[]).map((v) => (
                <button key={v} className={props.previewView === v ? 'active' : ''} onClick={() => props.setPreviewView(v)}>
                  {t(PREVIEW_VIEW_KEY[v])}
                </button>
              ))}
            </div>
            {paths[props.previewView] ? (
              <img alt={t(PREVIEW_VIEW_KEY[props.previewView])} src={previewUrl(paths[props.previewView])} />
            ) : (
              <div className="placeholder small">{t('export.noPreview', { view: t(PREVIEW_VIEW_KEY[props.previewView]) })}</div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function ExportStatusBanner(props: {
  status: string | null;
  manifest: ExportManifest | null;
  runView: ExportRunView;
}): JSX.Element {
  const t = useT();
  const { status, manifest } = props;
  const succeeded = manifest?.formats ?? [];
  const text =
    status === 'accepted'
      ? t('export.bannerAccepted', { formats: succeeded.join(', ') })
      : status === 'partial'
        ? t('export.bannerPartial', { formats: succeeded.join(', ') || t('export.bannerNone') })
        : status === 'cancelled'
          ? t('export.bannerCancelled')
          : statusLabel(t, status);
  return (
    <div className={`reviewbadge ${status === 'accepted' ? 'clean' : status === 'partial' ? 'high_stretch' : 'has_overlap'}`}>
      {text}
      {status === 'partial' && <FailedFormats runView={props.runView} inline />}
    </div>
  );
}

function FailedFormats(props: { runView: ExportRunView; inline?: boolean }): JSX.Element | null {
  const failed = props.runView.result?.failed_formats ?? [];
  if (failed.length === 0) return null;
  return (
    <ul className={`issuelist ${props.inline ? 'inline' : ''}`}>
      {failed.map((f, i) => (
        <li key={i} className="error">
          <span className="sevdot error" /> <strong>{f.format.toUpperCase()}</strong>: {f.message}
        </li>
      ))}
    </ul>
  );
}

function ExportedFiles(props: { runView: ExportRunView; onReveal: (key: string) => void }): JSX.Element {
  const t = useT();
  const { runView } = props;
  const exports = runView.result?.exports ?? {};
  const entries = Object.entries(exports);
  if (entries.length === 0) {
    return <div className="placeholder">{t('export.noFiles')}</div>;
  }
  return (
    <div className="artifactlist">
      <table className="metrics">
        <thead>
          <tr><th>{t('export.col.format')}</th><th>{t('export.col.file')}</th><th></th></tr>
        </thead>
        <tbody>
          {entries.map(([fmt, rel]) => (
            <tr key={fmt}>
              <td><span className="tag ok">{fmt.toUpperCase()}</span></td>
              <td><code className="small">{String(rel).split(/[\\/]/).pop()}</code></td>
              <td><button className="small" onClick={() => props.onReveal(fmt)}>{t('export.reveal')}</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ValidationTable(props: { validation: ExportRunView['validation'] }): JSX.Element {
  const t = useT();
  const v = props.validation;
  if (!v || Object.keys(v.formats).length === 0) {
    return <div className="placeholder">{t('export.noValidation')}</div>;
  }
  return (
    <div className="candtable-wrap">
      <table className="candtable">
        <thead>
          <tr>
            <th>{t('export.vcol.format')}</th><th>{t('export.vcol.reopen')}</th><th>{t('export.vcol.uv')}</th><th>{t('export.vcol.uvLayers')}</th>
            <th>{t('export.vcol.faces')}</th><th>{t('export.vcol.verts')}</th><th>{t('export.vcol.normals')}</th><th>{t('export.vcol.warnings')}</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(v.formats).map(([fmt, fv]: [string, FormatValidation]) => (
            <tr key={fmt} className={fv.has_uv ? '' : 'rejected'}>
              <td><code className="small">{fmt.toUpperCase()}</code></td>
              <td>{fv.reopen_ok ? <span className="tag ok">{t('export.val.ok')}</span> : <span className="tag unknown">{t('export.val.no')}</span>}</td>
              <td>{fv.has_uv ? <span className="tag ok">{t('export.val.yes')}</span> : <span className="tag unknown">{t('export.val.missing')}</span>}</td>
              <td className="small">{fv.uv_layers.join(', ') || '—'}</td>
              <td>{fv.faces.toLocaleString()}</td>
              <td>{fv.vertices.toLocaleString()}</td>
              <td>{fv.has_normals ? t('common.yes') : t('common.no')}</td>
              <td className="small muted">{fv.warnings.length}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Right panel: export options / source links / metrics / warnings
// ---------------------------------------------------------------------------
const METRIC_LABELS: Record<keyof ExportMetrics, TKey> = {
  stretch_score: 'metric.stretch_score',
  worst_island_distortion: 'metric.worst_island_distortion',
  raster_overlap_ratio: 'metric.raster_overlap_ratio',
  texel_density_variance: 'metric.texel_density_variance',
  packing_efficiency: 'metric.packing_efficiency',
};

function ExportRightPanel(props: {
  runView: ExportRunView | null;
  readiness: ExportReadiness | null;
  formats: string[];
  setFormats: (f: string[]) => void;
  options: ExportOptions;
  setOptions: (o: ExportOptions) => void;
  running: boolean;
}): JSX.Element {
  const t = useT();
  const manifest = props.runView?.manifest ?? null;
  const metrics = manifest?.metrics ?? null;
  const source = manifest?.source ?? null;
  const warnings = props.runView?.result?.warnings ?? [];

  return (
    <aside className="right">
      <section>
        <h3>{t('export.options')}</h3>
        <ExportOptionsControls
          formats={props.formats}
          setFormats={props.setFormats}
          options={props.options}
          setOptions={props.setOptions}
          disabled={props.running}
        />
      </section>

      <section>
        <h3>{t('export.sourceLinks')}</h3>
        {source ? (
          <table className="metrics">
            <tbody>
              <tr><td>{t('export.row.uvRun')}</td><td className="small"><code>{shortId(source.uv_generate_run_id)}</code></td></tr>
              <tr><td>{t('export.row.seamSpec')}</td><td className="small"><code>{baseName(source.active_user_seam_spec)}</code></td></tr>
              <tr><td>{t('export.row.candidate')}</td><td className="small"><code>{baseName(source.candidate_summary)}</code></td></tr>
              <tr><td>{t('export.row.aiReview')}</td><td className="small">{source.ai_review_skipped ? t('export.skipped') : '—'}</td></tr>
            </tbody>
          </table>
        ) : (
          <div className="muted small">{t('export.sourceHint')}</div>
        )}
      </section>

      <section>
        <h3>{t('export.metricsSnapshot')}</h3>
        {metrics ? (
          <table className="metrics">
            <tbody>
              {(Object.keys(METRIC_LABELS) as (keyof ExportMetrics)[]).map((k) => (
                <tr key={k}><td>{t(METRIC_LABELS[k])}</td><td>{fmtNum(metrics[k])}</td></tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="muted small">{t('export.noMetrics')}</div>
        )}
      </section>

      <section>
        <h3>{t('common.warnings')}</h3>
        {warnings.length ? (
          <ul className="issuelist">
            {warnings.map((w, i) => (
              <li key={i} className="warning"><span className="sevdot warning" /> {w}</li>
            ))}
          </ul>
        ) : (
          <div className="ok small">{t('export.noWarnings')}</div>
        )}
      </section>
    </aside>
  );
}

function ExportOptionsControls(props: {
  formats: string[];
  setFormats: (f: string[]) => void;
  options: ExportOptions;
  setOptions: (o: ExportOptions) => void;
  disabled: boolean;
}): JSX.Element {
  const t = useT();
  const { formats, setFormats, options, setOptions, disabled } = props;
  const toggleFormat = (fmt: string) =>
    setFormats(formats.includes(fmt) ? formats.filter((f) => f !== fmt) : [...formats, fmt]);
  const setOpt = (patch: Partial<ExportOptions>) => setOptions({ ...options, ...patch });

  return (
    <div className="runopts">
      <div className="formatrow">
        {(SUPPORTED_EXPORT_FORMATS as readonly string[]).map((fmt) => (
          <label key={fmt} className={`fmtchip ${formats.includes(fmt) ? 'on' : ''}`}>
            <input
              type="checkbox"
              disabled={disabled}
              checked={formats.includes(fmt)}
              onChange={() => toggleFormat(fmt)}
            />
            {fmt.toUpperCase()}
          </label>
        ))}
      </div>

      <label className="optrow">
        <span>{t('export.selectedUvLayer')}</span>
        <input
          type="text"
          disabled={disabled}
          placeholder={t('export.keepActive')}
          value={options.selected_uv_layer ?? ''}
          onChange={(e) => setOpt({ selected_uv_layer: e.target.value || null })}
        />
      </label>
      <label className="optrow">
        <span>{t('export.exportName')}</span>
        <input
          type="text"
          disabled={disabled}
          placeholder={t('export.exportNamePlaceholder')}
          value={options.export_name ?? ''}
          onChange={(e) => setOpt({ export_name: e.target.value || null })}
        />
      </label>

      {([
        ['apply_scale', 'export.opt.applyScale'],
        ['include_materials', 'export.opt.includeMaterials'],
        ['include_normals', 'export.opt.includeNormals'],
        ['copy_textures', 'export.opt.copyTextures'],
        ['triangulate', 'export.opt.triangulate'],
      ] as [keyof ExportOptions, TKey][]).map(([key, label]) => (
        <label key={key} className="optrow">
          <span>{t(label)}</span>
          <input
            type="checkbox"
            disabled={disabled}
            checked={!!options[key]}
            onChange={(e) => setOpt({ [key]: e.target.checked } as Partial<ExportOptions>)}
          />
        </label>
      ))}
      <div className="muted small">{t('export.duplicateHint')}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Bottom: status / raw manifest / raw validation report / logs
// ---------------------------------------------------------------------------
const STATUS_TEXT: Record<string, TKey> = {
  accepted: 'export.statusText.accepted',
  partial: 'export.statusText.partial',
  failed: 'export.statusText.failed',
  cancelled: 'export.statusText.cancelled',
  running: 'export.statusText.running',
  queued: 'export.statusText.queued',
};

function ExportBottomPanel(props: {
  runView: ExportRunView | null;
  status: string | null;
}): JSX.Element {
  const t = useT();
  const [tab, setTab] = useState<BottomTab>('manifest');
  const rv = props.runView;
  const status = props.status;
  return (
    <footer className="bottom">
      <div className="statusrow">
        <span className={`statuspill ${status ?? 'idle'}`}>{statusLabel(t, status)}</span>
        {status && (
          <span className="muted small">{STATUS_TEXT[status] ? t(STATUS_TEXT[status]) : statusLabel(t, status)}</span>
        )}
        {rv?.status?.error && (
          <span className="err">{rv.status.error.code}: {rv.status.error.message}</span>
        )}
        {(rv?.result?.warnings?.length ?? 0) > 0 && (
          <span className="muted small">{t('common.warningsCount', { n: rv!.result!.warnings.length })}</span>
        )}
      </div>
      <div className="reporttabs">
        <nav className="tabbar">
          <button className={tab === 'manifest' ? 'active' : ''} onClick={() => setTab('manifest')}>{t('export.tab.manifest')}</button>
          <button className={tab === 'validation' ? 'active' : ''} onClick={() => setTab('validation')}>{t('export.tab.validation')}</button>
          <button className={tab === 'logs' ? 'active' : ''} onClick={() => setTab('logs')}>{t('common.tab.logs')}</button>
        </nav>
        <div className="tabbody">
          {!rv && <div className="muted">{t('export.noSelected')}</div>}
          {rv && tab === 'manifest' && <Json data={rv.manifest} empty={t('export.noManifest')} />}
          {rv && tab === 'validation' && <Json data={rv.validation} empty={t('export.noValidationReport')} />}
          {rv && tab === 'logs' && (
            <div className="logs">
              <div className="logcol">
                <h4>stdout</h4>
                <pre>{rv.stdout || t('common.empty')}</pre>
              </div>
              <div className="logcol">
                <h4>stderr</h4>
                <pre className={rv.stderr ? 'err' : ''}>{rv.stderr || t('common.empty')}</pre>
              </div>
            </div>
          )}
        </div>
      </div>
    </footer>
  );
}

function Json(props: { data: unknown; empty: string }): JSX.Element {
  return <pre className="json">{props.data ? JSON.stringify(props.data, null, 2) : props.empty}</pre>;
}

// ---------------------------------------------------------------------------
function fmtNum(v: number | null | undefined, digits = 4): string {
  if (v === null || v === undefined) return '—';
  return Number(v).toFixed(digits);
}
function shortId(id: string | null | undefined): string {
  if (!id) return '—';
  return id.replace(/^uv_run_/, '').slice(0, 10);
}
function baseName(p: string | null | undefined): string {
  if (!p) return '—';
  return p.split(/[\\/]/).pop() ?? p;
}
