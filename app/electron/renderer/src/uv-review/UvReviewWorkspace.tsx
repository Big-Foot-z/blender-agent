/**
 * MVP 1 UV Review workspace (plan §8). Read-only review of an existing UV layer:
 * pick an object + UV layer, run a review, and inspect the layout / checker /
 * metrics. No-UV models show a clear empty state instead of failing (plan §15).
 *
 * The renderer only reads normalized JSON + artifact paths; it never parses
 * Blender stdout (plan §4).
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import type {
  InspectUvResult,
  Project,
  ReviewIssue,
  UvLayerInfo,
  UvMetrics,
  UvObjectSummary,
  UvReviewRunView,
} from '@shared/contracts';
import { ReviewStatus, UV_TERMINAL_STATUSES } from '@shared/contracts';
import type { Banner } from '../App';
import { useT, statusLabel, type TKey } from '../i18n';
import { previewUrl } from '../previewUrl';

type CenterTab = 'checker' | 'layout';
type CheckerView = 'front' | 'side' | '3q';

const CHECKER_VIEW_KEY: Record<CheckerView, TKey> = {
  front: 'view.front',
  side: 'view.side',
  '3q': 'view.q3',
};

export function UvReviewWorkspace(props: {
  project: Project | null;
  setProject: (p: Project) => void;
  guard: (label: string, fn: () => Promise<void>) => Promise<void>;
  setBanner: (b: Banner) => void;
}): JSX.Element {
  const t = useT();
  const { project, guard, setBanner } = props;
  const [inspect, setInspect] = useState<InspectUvResult | null>(null);
  const [selectedObject, setSelectedObject] = useState<string>('');
  const [selectedLayer, setSelectedLayer] = useState<string>('');
  const [runId, setRunId] = useState<string | null>(null);
  const [runView, setRunView] = useState<UvReviewRunView | null>(null);
  const [centerTab, setCenterTab] = useState<CenterTab>('layout');
  const [checkerView, setCheckerView] = useState<CheckerView>('front');

  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  // Reset per-project; seed selection + latest run from the manifest (plan §9).
  useEffect(() => {
    setInspect(null);
    setSelectedObject(project?.selected_object ?? '');
    setSelectedLayer(project?.selected_uv_layer ?? '');
    setRunId(project?.latest_uv_review_run_id ?? null);
    setRunView(null);
  }, [project?.id]);

  const refreshRun = useCallback(async () => {
    if (!project || !runId) return;
    const view = await window.api.uvGetReviewRun({ projectId: project.id, runId });
    setRunView(view);
    if (view.status && UV_TERMINAL_STATUSES.has(view.status.status) && pollTimer.current) {
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

  const objects = inspect?.objects ?? [];
  const currentObject = objects.find((o) => o.name === selectedObject) ?? null;
  const layers = currentObject?.uv_layers ?? [];

  const selectObject = (name: string) => {
    setSelectedObject(name);
    const obj = objects.find((o) => o.name === name);
    setSelectedLayer(obj?.active_uv_layer ?? obj?.uv_layers[0]?.name ?? '');
  };

  const onInspect = () =>
    guard(t('busy.inspectingUv'), async () => {
      if (!project) {
        setBanner({ kind: 'error', text: t('common.openImportFirst') });
        return;
      }
      const res = await window.api.uvInspectLayers({ projectId: project.id });
      setInspect(res);
      if (res.status === 'failed') {
        setBanner({ kind: 'error', text: res.error?.message ?? t('common.inspectFailed') });
        return;
      }
      const first = res.objects?.[0];
      if (first) {
        setSelectedObject(first.name);
        setSelectedLayer(first.active_uv_layer ?? first.uv_layers[0]?.name ?? '');
      }
      if (res.status === 'no_uv') {
        setBanner({ kind: 'info', text: t('review.noUvFound') });
      }
    });

  const onSelectLayer = (layer: string) => {
    setSelectedLayer(layer);
    if (project && selectedObject) {
      window.api.uvSetActiveLayer({ projectId: project.id, objectName: selectedObject, uvLayer: layer });
    }
  };

  const onReview = () =>
    guard(t('busy.reviewingUv'), async () => {
      if (!project || !selectedObject || !selectedLayer) {
        setBanner({ kind: 'error', text: t('review.selectObjLayer') });
        return;
      }
      const { run_id } = await window.api.uvReviewExisting({
        projectId: project.id,
        objectName: selectedObject,
        uvLayer: selectedLayer,
      });
      setRunId(run_id);
      setRunView(null);
      const p = await window.api.projectGet(project.id);
      props.setProject(p);
    });

  const status = runView?.status?.status ?? null;
  const summary = runView?.summary ?? null;
  const objectHasUv = currentObject ? currentObject.has_uv : true;
  const canReview = !!selectedObject && !!selectedLayer && objectHasUv;

  return (
    <>
      <div className="subtoolbar">
        <button disabled={!project} onClick={onInspect}>{t('review.inspectUv')}</button>
        <button disabled={!canReview} className="primary" onClick={onReview}>{t('review.reviewUv')}</button>
        <button disabled title={t('review.nextGenerateTitle')}>{t('review.nextGenerate')}</button>
        {project && <span className="muted small subtoolbar-hint">{project.name}</span>}
      </div>

      <div className="body">
        <UvLeftPanel
          project={project}
          objects={objects}
          inspectStatus={inspect?.status ?? null}
          selectedObject={selectedObject}
          onSelectObject={selectObject}
          layers={layers}
          selectedLayer={selectedLayer}
          onSelectLayer={onSelectLayer}
          activeRunId={runId}
          onSelectRun={setRunId}
        />

        <main className="center uv-center">
          <UvCenter
            runView={runView}
            summary={summary}
            centerTab={centerTab}
            setCenterTab={setCenterTab}
            checkerView={checkerView}
            setCheckerView={setCheckerView}
            status={status}
          />
        </main>

        <UvRightPanel object={currentObject} selectedLayer={selectedLayer} summary={summary} />
      </div>

      <UvBottomPanel runView={runView} status={status} />
    </>
  );
}

// ---------------------------------------------------------------------------
// Left panel: project / objects / UV layers / review runs
// ---------------------------------------------------------------------------
function UvLeftPanel(props: {
  project: Project | null;
  objects: UvObjectSummary[];
  inspectStatus: string | null;
  selectedObject: string;
  onSelectObject: (n: string) => void;
  layers: UvLayerInfo[];
  selectedLayer: string;
  onSelectLayer: (n: string) => void;
  activeRunId: string | null;
  onSelectRun: (id: string) => void;
}): JSX.Element {
  const t = useT();
  const { project } = props;
  const reviewRuns = project?.uv_review_runs ?? [];
  return (
    <aside className="left">
      <section>
        <h3>{t('common.project')}</h3>
        {project ? (
          <div className="kv">
            <div>{project.name}</div>
            <div className="small">
              {t('common.model')}:{' '}
              <code>{project.working_model ?? project.working_model_fbx ?? project.source_model}</code>
            </div>
          </div>
        ) : (
          <div className="muted">{t('common.noProjectOpen')}</div>
        )}
      </section>

      <section>
        <h3>{t('common.objects')}</h3>
        {props.objects.length ? (
          <ul className="list">
            {props.objects.map((o) => (
              <li
                key={o.name}
                className={o.name === props.selectedObject ? 'sel' : ''}
                onClick={() => props.onSelectObject(o.name)}
              >
                {o.name}{' '}
                <span className={`tag ${o.has_uv ? 'ok' : 'unknown'}`}>
                  {o.has_uv ? t('review.tag.uv') : t('review.tag.noUv')}
                </span>
                <div className="muted small">{t('common.facesCount', { n: o.faces.toLocaleString() })}</div>
              </li>
            ))}
          </ul>
        ) : (
          <div className="muted">{t('review.runInspect')}</div>
        )}
      </section>

      <section>
        <h3>{t('review.uvLayers')}</h3>
        {props.layers.length ? (
          <ul className="list">
            {props.layers.map((l) => (
              <li
                key={l.name}
                className={l.name === props.selectedLayer ? 'sel' : ''}
                onClick={() => props.onSelectLayer(l.name)}
              >
                {l.name}
                {l.active && <span className="tag ok">{t('common.tag.active')}</span>}
                {l.empty && <span className="tag unknown">{t('common.tag.empty')}</span>}
                <div className="muted small">{t('review.loopsCount', { n: l.loop_count.toLocaleString() })}</div>
              </li>
            ))}
          </ul>
        ) : (
          <div className="muted">
            {props.selectedObject ? t('review.objNoUv') : t('common.selectObject')}
          </div>
        )}
      </section>

      <section>
        <h3>{t('review.reviewRuns')}</h3>
        {reviewRuns.length ? (
          <ul className="list">
            {reviewRuns
              .slice()
              .reverse()
              .map((r) => (
                <li
                  key={r}
                  className={r === props.activeRunId ? 'sel' : ''}
                  onClick={() => props.onSelectRun(r)}
                >
                  <code className="small">{r.replace('review_run_', '').slice(0, 8)}</code>
                  {r === project?.latest_uv_review_run_id && <span className="tag ok">{t('common.tag.latest')}</span>}
                </li>
              ))}
          </ul>
        ) : (
          <div className="muted">{t('review.noReviewRuns')}</div>
        )}
      </section>
    </aside>
  );
}

// ---------------------------------------------------------------------------
// Center: Checker | UV Layout tabs (+ no-UV empty state)
// ---------------------------------------------------------------------------
function UvCenter(props: {
  runView: UvReviewRunView | null;
  summary: UvReviewRunView['summary'];
  centerTab: CenterTab;
  setCenterTab: (t: CenterTab) => void;
  checkerView: CheckerView;
  setCheckerView: (v: CheckerView) => void;
  status: string | null;
}): JSX.Element {
  const t = useT();
  const { runView, summary } = props;

  if (summary?.status === 'no_uv') {
    return <NoUvState />;
  }
  if (!runView || (props.status && !UV_TERMINAL_STATUSES.has(props.status))) {
    return (
      <div className="preview">
        <div className="placeholder">
          {props.status
            ? t('review.reviewing', { status: statusLabel(t, props.status) })
            : t('review.runReviewHint')}
        </div>
      </div>
    );
  }

  const paths = runView.artifact_paths;
  const hasChecker = !!(paths.checker_front || paths.checker_side);
  const has3q = !!paths.checker_3q;

  return (
    <div className="uv-tabs">
      <nav className="tabbar">
        <button className={props.centerTab === 'layout' ? 'active' : ''} onClick={() => props.setCenterTab('layout')}>
          {t('common.uvLayout')}
        </button>
        <button className={props.centerTab === 'checker' ? 'active' : ''} onClick={() => props.setCenterTab('checker')}>
          {t('review.checker')}
        </button>
      </nav>

      <div className="uv-tabbody">
        {props.centerTab === 'layout' &&
          (paths.uv_layout ? (
            <ZoomPanImage src={previewUrl(paths.uv_layout)} alt={t('common.uvLayout')} />
          ) : (
            <div className="placeholder">{t('review.noLayoutImg')}</div>
          ))}

        {props.centerTab === 'checker' &&
          (hasChecker ? (
            <div className="checker-wrap">
              <div className="viewtoggle">
                {(['front', 'side', ...(has3q ? ['3q'] : [])] as CheckerView[]).map((v) => (
                  <button
                    key={v}
                    className={props.checkerView === v ? 'active' : ''}
                    onClick={() => props.setCheckerView(v)}
                  >
                    {t(CHECKER_VIEW_KEY[v])}
                  </button>
                ))}
              </div>
              <CheckerImage paths={paths} view={props.checkerView} />
            </div>
          ) : (
            <div className="placeholder">{t('review.noChecker')}</div>
          ))}
      </div>
    </div>
  );
}

function CheckerImage(props: { paths: Record<string, string>; view: CheckerView }): JSX.Element {
  const t = useT();
  const key = props.view === 'front' ? 'checker_front' : props.view === 'side' ? 'checker_side' : 'checker_3q';
  const path = props.paths[key];
  const viewLabel = t(CHECKER_VIEW_KEY[props.view]);
  return (
    <div className="preview">
      {path ? (
        <img alt={`${t('review.checker')} ${viewLabel}`} src={previewUrl(path)} />
      ) : (
        <div className="placeholder">{t('review.noViewChecker', { view: viewLabel })}</div>
      )}
    </div>
  );
}

function NoUvState(): JSX.Element {
  const t = useT();
  return (
    <div className="preview">
      <div className="placeholder nouv">
        <div className="nouv-title">{t('review.noUvTitle')}</div>
        <div className="muted">{t('review.noUvHint')}</div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Zoom/pan image (UV layout)
// ---------------------------------------------------------------------------
function ZoomPanImage(props: { src: string; alt: string }): JSX.Element {
  const t = useT();
  const [scale, setScale] = useState(1);
  const [tx, setTx] = useState(0);
  const [ty, setTy] = useState(0);
  const drag = useRef<{ x: number; y: number; tx: number; ty: number } | null>(null);

  // Reset transform when the image changes.
  useEffect(() => {
    setScale(1);
    setTx(0);
    setTy(0);
  }, [props.src]);

  const onWheel = (e: React.WheelEvent) => {
    e.preventDefault();
    const next = Math.min(8, Math.max(0.25, scale * (e.deltaY < 0 ? 1.1 : 1 / 1.1)));
    setScale(next);
  };
  const onDown = (e: React.MouseEvent) => {
    drag.current = { x: e.clientX, y: e.clientY, tx, ty };
  };
  const onMove = (e: React.MouseEvent) => {
    if (!drag.current) return;
    setTx(drag.current.tx + (e.clientX - drag.current.x));
    setTy(drag.current.ty + (e.clientY - drag.current.y));
  };
  const onUp = () => {
    drag.current = null;
  };
  const reset = () => {
    setScale(1);
    setTx(0);
    setTy(0);
  };

  return (
    <div className="zoompan" onWheel={onWheel} onMouseDown={onDown} onMouseMove={onMove} onMouseUp={onUp} onMouseLeave={onUp}>
      <img
        alt={props.alt}
        src={props.src}
        draggable={false}
        style={{ transform: `translate(${tx}px, ${ty}px) scale(${scale})` }}
      />
      <div className="zoomctl">
        <button onClick={() => setScale((s) => Math.min(8, s * 1.2))}>+</button>
        <button onClick={() => setScale((s) => Math.max(0.25, s / 1.2))}>−</button>
        <button onClick={reset}>{t('common.reset')}</button>
        <span className="muted small">{Math.round(scale * 100)}%</span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Right panel: mesh summary / UV summary / issues / metrics
// ---------------------------------------------------------------------------
const METRIC_LABELS: Record<keyof UvMetrics, TKey> = {
  stretch_score: 'metric.stretch_score',
  worst_island_distortion: 'metric.worst_island_distortion',
  overlap_ratio: 'metric.overlap_ratio',
  raster_overlap_ratio: 'metric.raster_overlap_ratio',
  self_overlap_ratio: 'metric.self_overlap_ratio',
  cross_overlap_ratio: 'metric.cross_overlap_ratio',
  texel_density_variance: 'metric.texel_density_variance',
  packing_efficiency: 'metric.packing_efficiency',
};

const REVIEW_STATUS_TEXT: Record<string, TKey> = {
  [ReviewStatus.Clean]: 'review.status.clean',
  [ReviewStatus.HasOverlap]: 'review.status.has_overlap',
  [ReviewStatus.HighStretch]: 'review.status.high_stretch',
  [ReviewStatus.DensityVariance]: 'review.status.density_variance',
  [ReviewStatus.OutOfBounds]: 'review.status.out_of_bounds',
  [ReviewStatus.NoUv]: 'review.status.no_uv',
  [ReviewStatus.Unknown]: 'review.status.unknown',
};

function UvRightPanel(props: {
  object: UvObjectSummary | null;
  selectedLayer: string;
  summary: UvReviewRunView['summary'];
}): JSX.Element {
  const t = useT();
  const { object, summary } = props;
  const m = summary?.metrics ?? null;
  const uv = summary?.uv ?? null;
  const issues = summary?.issues ?? [];
  const reviewStatus = summary?.review_status ?? null;
  const reviewStatusKey = reviewStatus ? REVIEW_STATUS_TEXT[reviewStatus] : null;

  return (
    <aside className="right">
      <section>
        <h3>{t('review.meshSummary')}</h3>
        {object ? (
          <table className="metrics">
            <tbody>
              <tr><td>{t('common.name')}</td><td>{object.name}</td></tr>
              <tr><td>{t('common.vertices')}</td><td>{object.vertices.toLocaleString()}</td></tr>
              <tr><td>{t('common.edges')}</td><td>{object.edges.toLocaleString()}</td></tr>
              <tr><td>{t('common.faces')}</td><td>{object.faces.toLocaleString()}</td></tr>
              <tr><td>{t('review.uvLayerRow')}</td><td>{props.selectedLayer || '—'}</td></tr>
            </tbody>
          </table>
        ) : (
          <div className="muted">{t('common.selectObject')}</div>
        )}
      </section>

      {reviewStatus && (
        <section>
          <h3>{t('review.reviewStatus')}</h3>
          <div className={`reviewbadge ${reviewStatus}`}>
            {reviewStatusKey ? t(reviewStatusKey) : reviewStatus}
          </div>
        </section>
      )}

      <section>
        <h3>{t('review.uvSummary')}</h3>
        {uv ? (
          <table className="metrics">
            <tbody>
              <tr><td>{t('review.row.islands')}</td><td>{uv.island_count}</td></tr>
              <tr><td>{t('review.row.inTile')}</td><td>{uv.uv_bounds.in_0_1 ? t('common.yes') : t('common.no')}</td></tr>
              <tr><td>{t('review.row.negativeUv')}</td><td>{uv.has_negative_uv ? t('common.yes') : t('common.no')}</td></tr>
              <tr>
                <td>{t('review.row.uvBounds')}</td>
                <td>
                  [{uv.uv_bounds.min.map((n) => n.toFixed(2)).join(', ')}] – [
                  {uv.uv_bounds.max.map((n) => n.toFixed(2)).join(', ')}]
                </td>
              </tr>
            </tbody>
          </table>
        ) : (
          <div className="muted">{t('review.runReview')}</div>
        )}
      </section>

      <section>
        <h3>{t('common.issues')}</h3>
        {summary ? (
          issues.length ? (
            <ul className="issuelist">
              {issues.map((iss: ReviewIssue, i: number) => (
                <li key={i} className={iss.severity}>
                  <span className={`sevdot ${iss.severity}`} /> {iss.message}
                  {iss.value !== undefined && <span className="muted small"> ({iss.value})</span>}
                </li>
              ))}
            </ul>
          ) : (
            <div className="ok small">{t('review.noIssues')}</div>
          )
        ) : (
          <div className="muted">{t('review.runReview')}</div>
        )}
      </section>

      <section>
        <h3>{t('common.metrics')}</h3>
        {m ? (
          <table className="metrics">
            <tbody>
              {(Object.keys(METRIC_LABELS) as (keyof UvMetrics)[]).map((k) => (
                <tr key={k}>
                  <td>{t(METRIC_LABELS[k])}</td>
                  <td>{fmt(m[k])}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="muted">{t('review.runReviewMetrics')}</div>
        )}
      </section>
    </aside>
  );
}

// ---------------------------------------------------------------------------
// Bottom: run status / artifact links / raw JSON + logs
// ---------------------------------------------------------------------------
type BottomTab = 'report' | 'artifacts' | 'logs';

function UvBottomPanel(props: { runView: UvReviewRunView | null; status: string | null }): JSX.Element {
  const t = useT();
  const [tab, setTab] = useState<BottomTab>('report');
  const rv = props.runView;
  return (
    <footer className="bottom">
      <div className="statusrow">
        <span className={`statuspill ${props.status ?? 'idle'}`}>{statusLabel(t, props.status)}</span>
        {rv?.status?.error && (
          <span className="err">
            {rv.status.error.code}: {rv.status.error.message}
          </span>
        )}
        {(rv?.summary?.warnings?.length ?? 0) > 0 && (
          <span className="muted small">{t('common.warningsCount', { n: rv!.summary!.warnings.length })}</span>
        )}
      </div>
      <div className="reporttabs">
        <nav className="tabbar">
          <button className={tab === 'report' ? 'active' : ''} onClick={() => setTab('report')}>{t('review.tab.report')}</button>
          <button className={tab === 'artifacts' ? 'active' : ''} onClick={() => setTab('artifacts')}>{t('review.tab.artifacts')}</button>
          <button className={tab === 'logs' ? 'active' : ''} onClick={() => setTab('logs')}>{t('common.tab.logs')}</button>
        </nav>
        <div className="tabbody">
          {!rv && <div className="muted">{t('review.noRunSelected')}</div>}
          {rv && tab === 'report' && (
            <pre className="json">{rv.summary ? JSON.stringify(rv.summary, null, 2) : t('common.noSummary')}</pre>
          )}
          {rv && tab === 'artifacts' && <ArtifactList paths={rv.artifact_paths} />}
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

function ArtifactList(props: { paths: Record<string, string> }): JSX.Element {
  const t = useT();
  const entries = Object.entries(props.paths);
  if (!entries.length) return <div className="muted">{t('review.noArtifacts')}</div>;
  return (
    <ul className="artifactlist">
      {entries.map(([key, path]) => (
        <li key={key}>
          <span className="akey">{key}</span>
          <code className="small">{path}</code>
        </li>
      ))}
    </ul>
  );
}

function fmt(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—';
  return Number(v).toFixed(4);
}
