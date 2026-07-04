/**
 * MVP 3 Generate + Optimize workspace (plan §8). The MVP 2 `active_user_seam_spec`
 * is the source of truth: validate it, run a strict user/reference unwrap +
 * layout-optimization sweep, then compare the baseline vs the selected candidate.
 *
 * The renderer only reads normalized JSON (`uv_generate_summary.json` /
 * `candidate_summary.json`) + artifact paths; it never parses Blender stdout and
 * never depends on the raw `p5_gate.json` shape (plan §3, §5). UI wording stays
 * honest: "selected candidate" / "no blocking overlap detected", never
 * "production-ready" from metrics alone (plan §8).
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import type {
  CandidateRow,
  CandidateSummary,
  GenerateMetrics,
  GenerateUvOptions,
  Project,
  SeamIntegrity,
  SeamSource,
  UvGenerateRunView,
  ValidateGenerateInput,
} from '@shared/contracts';
import { STRICT_GENERATE_OPTIONS, UV_GENERATE_TERMINAL_STATUSES } from '@shared/contracts';
import type { Banner } from '../App';
import { useT, statusLabel, type TKey } from '../i18n';
import { previewUrl } from '../previewUrl';

type CenterTab = 'checker' | 'layout' | 'candidates';
type CheckerView = 'front' | 'side';

export function UvGenerateWorkspace(props: {
  project: Project | null;
  setProject: (p: Project) => void;
  guard: (label: string, fn: () => Promise<void>) => Promise<void>;
  setBanner: (b: Banner) => void;
}): JSX.Element {
  const t = useT();
  const { project, guard, setBanner } = props;
  const [validation, setValidation] = useState<ValidateGenerateInput | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [runView, setRunView] = useState<UvGenerateRunView | null>(null);
  const [centerTab, setCenterTab] = useState<CenterTab>('checker');
  const [checkerView, setCheckerView] = useState<CheckerView>('front');
  const [options, setOptions] = useState<GenerateUvOptions>({ ...STRICT_GENERATE_OPTIONS });

  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  // Reset per-project; seed the latest run from the manifest (plan §9).
  useEffect(() => {
    setValidation(null);
    setRunView(null);
    setRunId(project?.latest_uv_generate_run_id ?? null);
    setOptions({ ...STRICT_GENERATE_OPTIONS });
  }, [project?.id]);

  const refreshRun = useCallback(async () => {
    if (!project || !runId) return;
    const view = await window.api.uvGenerateGetRun({ projectId: project.id, runId });
    setRunView(view);
    if (view.status && UV_GENERATE_TERMINAL_STATUSES.has(view.status.status) && pollTimer.current) {
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

  const onValidate = () =>
    guard(t('busy.validatingSpec'), async () => {
      if (!project) {
        setBanner({ kind: 'error', text: t('common.openImportFirst') });
        return;
      }
      const v = await window.api.uvGenerateValidateInput({ projectId: project.id });
      setValidation(v);
      if (!v.ready) {
        setBanner({
          kind: 'error',
          text: v.issues[0]?.message ?? t('generate.notReady'),
        });
      } else {
        setBanner({ kind: 'info', text: t('generate.specValid', { n: v.user_seam_count ?? 0 }) });
      }
    });

  const onGenerate = () =>
    guard(t('busy.generatingUv'), async () => {
      if (!project) {
        setBanner({ kind: 'error', text: t('common.openImportFirst') });
        return;
      }
      const v = validation ?? (await window.api.uvGenerateValidateInput({ projectId: project.id }));
      setValidation(v);
      if (!v.ready) {
        setBanner({
          kind: 'error',
          text: v.issues[0]?.message ?? t('generate.cannotGenerate'),
        });
        return;
      }
      const { run_id } = await window.api.uvGenerateStart({
        projectId: project.id,
        objectName: v.object_name ?? undefined,
        options,
      });
      setRunId(run_id);
      setRunView(null);
      setCenterTab('checker');
      const p = await window.api.projectGet(project.id);
      props.setProject(p);
    });

  const onCancel = () =>
    guard(t('busy.cancelling'), async () => {
      if (!project || !runId) return;
      await window.api.uvGenerateCancel({ projectId: project.id, runId });
      await refreshRun();
    });

  const status = runView?.status?.status ?? null;
  const summary = runView?.summary ?? null;
  const running = !!status && !UV_GENERATE_TERMINAL_STATUSES.has(status);
  const accepted = status === 'accepted';

  // Generate is enabled when there is a seam SOURCE — an active spec OR a selected
  // UV layer to derive one from — not just an active spec (revision plan §4.5).
  const hasSeamSource = !!(project?.active_user_seam_spec || project?.selected_uv_layer);
  const hasModel = !!(
    project && (project.working_model || project.working_model_fbx || project.source_model)
  );
  const canGenerate = !!project && hasModel && !!project.selected_object && hasSeamSource;

  return (
    <>
      <div className="subtoolbar">
        <button disabled={!project} onClick={onValidate}>{t('generate.validate')}</button>
        <button disabled={!canGenerate} className="primary" onClick={onGenerate}>
          {t('generate.generate')}
        </button>
        <button disabled={!running} onClick={onCancel}>{t('common.cancel')}</button>
        <button disabled title={t('generate.nextAiTitle')}>{t('generate.nextAi')}</button>
        {project && <span className="muted small subtoolbar-hint">{project.name}</span>}
      </div>

      <div className="body">
        <GenerateLeftPanel project={project} validation={validation} activeRunId={runId} onSelectRun={setRunId} />

        <main className="center uv-center">
          <GenerateCenter
            runView={runView}
            summary={summary}
            status={status}
            centerTab={centerTab}
            setCenterTab={setCenterTab}
            checkerView={checkerView}
            setCheckerView={setCheckerView}
          />
        </main>

        <GenerateRightPanel
          summary={summary}
          candidateSummary={runView?.candidate_summary ?? null}
          options={options}
          setOptions={setOptions}
          accepted={accepted}
        />
      </div>

      <GenerateBottomPanel runView={runView} status={status} />
    </>
  );
}

// ---------------------------------------------------------------------------
// Left panel: project / working model / active seam spec / UV runs
// ---------------------------------------------------------------------------
function GenerateLeftPanel(props: {
  project: Project | null;
  validation: ValidateGenerateInput | null;
  activeRunId: string | null;
  onSelectRun: (id: string) => void;
}): JSX.Element {
  const t = useT();
  const { project, validation } = props;
  const runs = project?.uv_generate_runs ?? [];
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
        <h3>{t('generate.workingModel')}</h3>
        <div className="small">
          <code>{project?.working_model ?? project?.working_model_fbx ?? '—'}</code>
        </div>
      </section>

      <section>
        <h3>{t('generate.seamSource')}</h3>
        <SeamSourceInfo project={project} validation={validation} />
        {validation && validation.issues.length > 0 && (
          <ul className="issuelist">
            {validation.issues.map((iss, i) => (
              <li key={i} className="warning">
                <span className="sevdot warning" /> {iss.message}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section>
        <h3>{t('generate.uvRuns')}</h3>
        {runs.length ? (
          <ul className="list">
            {runs
              .slice()
              .reverse()
              .map((r) => (
                <li
                  key={r}
                  className={r === props.activeRunId ? 'sel' : ''}
                  onClick={() => props.onSelectRun(r)}
                >
                  <code className="small">{r.replace('uv_run_', '').slice(0, 8)}</code>
                  {r === project?.latest_uv_generate_run_id && <span className="tag ok">{t('common.tag.latest')}</span>}
                </li>
              ))}
          </ul>
        ) : (
          <div className="muted">{t('generate.noRuns')}</div>
        )}
      </section>
    </aside>
  );
}

/**
 * Seam Source readiness panel (revision plan §4.5). Shows one of three states —
 * an explicit MVP 2 spec, a UV-boundary-derived source, or a missing source —
 * so the user is not forced into the Seam Editor for already-UV'd assets. Prefers
 * the fresh `validateInput` result; falls back to inferring from the manifest.
 */
function SeamSourceInfo(props: {
  project: Project | null;
  validation: ValidateGenerateInput | null;
}): JSX.Element {
  const t = useT();
  const { project, validation } = props;
  const kind =
    validation?.seam_source ??
    (project?.active_user_seam_spec
      ? 'explicit'
      : project?.selected_uv_layer
        ? 'derived'
        : 'missing');
  const specRel = validation?.seam_spec ?? project?.active_user_seam_spec ?? null;
  const uvLayer = validation?.selected_uv_layer ?? project?.selected_uv_layer ?? null;
  const seamCount = validation?.user_seam_count ?? null;

  if (kind === 'explicit') {
    return (
      <div className="small">
        <div className="tag ok">{t('generate.seamSourceExplicit')}</div>
        <div>
          <code>{specRel ?? '—'}</code>
        </div>
        {validation && (
          <div className={`tag ${validation.ready ? 'ok' : 'unknown'}`}>
            {validation.ready ? t('generate.tag.ready') : t('generate.tag.notReady')}
          </div>
        )}
        {seamCount != null && (
          <div className="muted small">
            {t('generate.userSeamsCount', { n: seamCount.toLocaleString() })}
          </div>
        )}
      </div>
    );
  }
  if (kind === 'derived') {
    return (
      <div className="small">
        <div className="tag ok">{t('generate.seamSourceDerived')}</div>
        <div>
          <code>{uvLayer ?? '—'}</code>
        </div>
        <div className="muted small">{t('generate.seamSourceDerivedHint')}</div>
      </div>
    );
  }
  return (
    <div className="small">
      <div className="tag unknown">{t('generate.seamSourceMissing')}</div>
      <div className="muted small">{t('generate.seamSourceMissingHint')}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Center: Before/After Checker | UV Layout | Candidate Table
// ---------------------------------------------------------------------------
function GenerateCenter(props: {
  runView: UvGenerateRunView | null;
  summary: UvGenerateRunView['summary'];
  status: string | null;
  centerTab: CenterTab;
  setCenterTab: (t: CenterTab) => void;
  checkerView: CheckerView;
  setCheckerView: (v: CheckerView) => void;
}): JSX.Element {
  const t = useT();
  const { runView, summary, status } = props;

  if (!runView || (status && !UV_GENERATE_TERMINAL_STATUSES.has(status))) {
    return (
      <div className="preview">
        <div className="placeholder">
          {status
            ? t('generate.generating', { status: statusLabel(t, status) })
            : t('generate.runHint')}
        </div>
      </div>
    );
  }
  if (status === 'failed') {
    return (
      <div className="preview">
        <div className="placeholder nouv">
          <div className="nouv-title">{t('generate.failed')}</div>
          <div className="muted">{runView.status?.error?.message ?? t('common.seeLogs')}</div>
        </div>
      </div>
    );
  }
  if (status === 'needs_input') {
    return (
      <div className="preview">
        <div className="placeholder nouv">
          <div className="nouv-title">{t('generate.needsInput')}</div>
          <div className="muted">
            {runView.status?.error?.message ?? t('generate.seamSourceMissingHint')}
          </div>
        </div>
      </div>
    );
  }

  const paths = runView.artifact_paths;
  const ck = props.checkerView;

  return (
    <div className="uv-tabs">
      <nav className="tabbar">
        <button className={props.centerTab === 'checker' ? 'active' : ''} onClick={() => props.setCenterTab('checker')}>
          {t('generate.beforeAfterChecker')}
        </button>
        <button className={props.centerTab === 'layout' ? 'active' : ''} onClick={() => props.setCenterTab('layout')}>
          {t('common.uvLayout')}
        </button>
        <button className={props.centerTab === 'candidates' ? 'active' : ''} onClick={() => props.setCenterTab('candidates')}>
          {t('generate.candidateTable')}
        </button>
      </nav>

      <div className="uv-tabbody">
        {props.centerTab === 'checker' && (
          <div className="checker-wrap">
            <div className="viewtoggle">
              {(['front', 'side'] as CheckerView[]).map((v) => (
                <button key={v} className={ck === v ? 'active' : ''} onClick={() => props.setCheckerView(v)}>
                  {t(v === 'front' ? 'view.front' : 'view.side')}
                </button>
              ))}
            </div>
            <BeforeAfter
              beforeSrc={paths[`baseline_checker_${ck}`]}
              afterSrc={paths[`selected_checker_${ck}`]}
              label={`${t('review.checker')} · ${t(ck === 'front' ? 'view.front' : 'view.side')}`}
            />
          </div>
        )}

        {props.centerTab === 'layout' && (
          <BeforeAfter
            beforeSrc={paths.baseline_uv_layout}
            afterSrc={paths.selected_uv_layout}
            label={t('common.uvLayout')}
          />
        )}

        {props.centerTab === 'candidates' && (
          <CandidateTable candidateSummary={runView.candidate_summary} summary={summary} />
        )}
      </div>
    </div>
  );
}

/** Side-by-side baseline (before) vs selected (after) image comparison (plan §7). */
function BeforeAfter(props: { beforeSrc?: string; afterSrc?: string; label: string }): JSX.Element {
  const t = useT();
  return (
    <div className="beforeafter">
      <figure>
        <figcaption>{t('generate.baseline')}</figcaption>
        {props.beforeSrc ? (
          <img alt={`${t('generate.baseline')} ${props.label}`} src={previewUrl(props.beforeSrc)} />
        ) : (
          <div className="placeholder small">{t('generate.noBaseline')}</div>
        )}
      </figure>
      <figure>
        <figcaption>{t('generate.selected')}</figcaption>
        {props.afterSrc ? (
          <img alt={`${t('generate.selected')} ${props.label}`} src={previewUrl(props.afterSrc)} />
        ) : (
          <div className="placeholder small">{t('generate.noSelected')}</div>
        )}
      </figure>
    </div>
  );
}

// --- Candidate table (plan §8 columns) ------------------------------------
const CAND_COLS: { key: string; label: TKey }[] = [
  { key: 'sel', label: 'generate.col.sel' },
  { key: 'id', label: 'generate.col.id' },
  { key: 'unwrap_method', label: 'generate.col.unwrap' },
  { key: 'minimize_iters', label: 'generate.col.minIters' },
  { key: 'margin', label: 'generate.col.margin' },
  { key: 'pack_shape', label: 'generate.col.pack' },
  { key: 'rotate', label: 'generate.col.rotate' },
  { key: 'stretch', label: 'generate.col.stretch' },
  { key: 'worst', label: 'generate.col.worst' },
  { key: 'texel', label: 'generate.col.texel' },
  { key: 'raster', label: 'generate.col.raster' },
  { key: 'packing', label: 'generate.col.packing' },
  { key: 'score', label: 'generate.col.score' },
  { key: 'reason', label: 'generate.col.reason' },
];

function CandidateTable(props: {
  candidateSummary: CandidateSummary | null;
  summary: UvGenerateRunView['summary'];
}): JSX.Element {
  const t = useT();
  const cs = props.candidateSummary;
  if (!cs || cs.candidates.length === 0) {
    return <div className="placeholder">{t('generate.noCandidates')}</div>;
  }
  const selected = cs.selected_candidate_id;
  return (
    <div className="candtable-wrap">
      <table className="candtable">
        <thead>
          <tr>{CAND_COLS.map((c) => <th key={c.key}>{t(c.label)}</th>)}</tr>
        </thead>
        <tbody>
          {cs.candidates.map((c: CandidateRow) => {
            const m = c.metrics ?? {};
            const isSel = c.id === selected;
            return (
              <tr key={c.id ?? Math.random()} className={isSel ? 'sel' : c.accepted ? '' : 'rejected'}>
                <td>{isSel ? '●' : ''}</td>
                <td><code className="small">{c.id}</code></td>
                <td>{c.unwrap_method === 'MINIMUM_STRETCH' ? 'SLIM' : 'ABF'}</td>
                <td>{c.minimize_iters}</td>
                <td>{fmtNum(c.margin, 3)}</td>
                <td>
                  {c.pack_backend && c.pack_backend !== 'blender'
                    ? `${c.pack_backend}${c.orient_long_islands ? '+orient' : ''}`
                    : c.pack_shape}
                </td>
                <td>{c.rotate ? t('common.yes') : t('common.no')}</td>
                <td>{fmtNum(m.stretch_score)}</td>
                <td>{fmtNum(m.worst_island_distortion)}</td>
                <td>{fmtNum(m.texel_density_variance)}</td>
                <td>{fmtNum(m.raster_overlap_ratio)}</td>
                <td>{fmtNum(m.packing_efficiency)}</td>
                <td>{fmtNum(c.score)}</td>
                <td>
                  {c.accepted ? (
                    <span className="tag ok">{isSel ? c.reason || t('generate.candSelected') : t('generate.candOk')}</span>
                  ) : (
                    <span className="tag unknown">{c.reason || t('generate.candRejected')}</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Right panel: seam integrity / selected candidate / metrics / run options
// ---------------------------------------------------------------------------
const METRIC_LABELS: Record<keyof GenerateMetrics, TKey> = {
  stretch_score: 'metric.stretch_score',
  worst_island_distortion: 'metric.worst_island_distortion',
  raster_overlap_ratio: 'metric.raster_overlap_ratio',
  overlap_ratio: 'metric.overlap_ratio',
  texel_density_variance: 'metric.texel_density_variance',
  packing_efficiency: 'metric.packing_efficiency',
  island_count: 'metric.island_count',
  uv_bounds_ok: 'metric.uv_bounds_ok',
};

function GenerateRightPanel(props: {
  summary: UvGenerateRunView['summary'];
  candidateSummary: CandidateSummary | null;
  options: GenerateUvOptions;
  setOptions: (o: GenerateUvOptions) => void;
  accepted: boolean;
}): JSX.Element {
  const t = useT();
  const { summary } = props;
  const integrity = summary?.seam_integrity ?? null;
  const lo = summary?.layout_optimization ?? null;
  const metrics = summary?.metrics ?? null;

  return (
    <aside className="right">
      <section>
        <h3>{t('generate.seamIntegrity')}</h3>
        {integrity ? (
          <SeamIntegrityBlock integrity={integrity} seamSource={summary?.seam_source ?? null} />
        ) : (
          <div className="muted">{t('generate.runIntegrity')}</div>
        )}
      </section>

      <section>
        <h3>{t('generate.selectedCandidate')}</h3>
        {summary ? (
          <div className="kv">
            <div>
              <code>{summary.selected_candidate_id ?? '—'}</code>{' '}
              {lo?.kept_baseline && <span className="tag unknown">{t('generate.baselineRetained')}</span>}
            </div>
            {lo?.kept_baseline ? (
              <div className="muted small">{t('generate.baselineRetainedHint')}</div>
            ) : lo?.enabled ? (
              <div className="muted small">
                {t('generate.candScore', {
                  n: lo.candidate_count ?? 0,
                  before: fmtNum(lo.score_before),
                  after: fmtNum(lo.score_after),
                })}
              </div>
            ) : null}
          </div>
        ) : (
          <div className="muted">{t('generate.noRun')}</div>
        )}
      </section>

      {lo?.enabled && (
        <section>
          <h3>{t('generate.optimization')}</h3>
          <OptimizationSummary lo={lo} seamSource={summary?.seam_source ?? null} integrity={integrity} />
        </section>
      )}

      <section>
        <h3>{t('common.metrics')}</h3>
        {metrics ? (
          <table className="metrics">
            <tbody>
              {(Object.keys(METRIC_LABELS) as (keyof GenerateMetrics)[]).map((k) => (
                <tr key={k}>
                  <td>{t(METRIC_LABELS[k])}</td>
                  <td>{fmtMetric(k, metrics[k])}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="muted">{t('generate.runMetrics')}</div>
        )}
      </section>

      <section>
        <h3>{t('common.issues')}</h3>
        {summary ? (
          summary.warnings.length ? (
            <ul className="issuelist">
              {summary.warnings.map((w, i) => (
                <li key={i} className="warning"><span className="sevdot warning" /> {w}</li>
              ))}
            </ul>
          ) : (
            <div className="ok small">{t('generate.noOverlap')}</div>
          )
        ) : (
          <div className="muted">{t('generate.runGenerate')}</div>
        )}
      </section>

      <section>
        <h3>{t('generate.runOptions')}</h3>
        <RunOptions options={props.options} setOptions={props.setOptions} />
      </section>
    </aside>
  );
}

function SeamIntegrityBlock(props: {
  integrity: SeamIntegrity;
  seamSource: SeamSource | null;
}): JSX.Element {
  const t = useT();
  const i = props.integrity;
  const src = props.seamSource;
  return (
    <>
      <div className={`reviewbadge ${i.valid ? 'clean' : 'has_overlap'}`}>
        {i.valid ? t('generate.seamPreserved') : t('generate.seamChanged')}
      </div>
      {src && (
        <div className="muted small">
          {t('generate.seamSourceLabel')}:{' '}
          {src.derived ? t('generate.seamSourceDerived') : t('generate.seamSourceExplicit')}
          {src.uv_layer ? ` · ${src.uv_layer}` : ''}
        </div>
      )}
      <table className="metrics">
        <tbody>
          <tr><td>{t('generate.row.userSeams')}</td><td>{i.user_seam_count.toLocaleString()}</td></tr>
          <tr><td>{t('generate.row.protected')}</td><td>{i.user_protected_count.toLocaleString()}</td></tr>
          <tr className={i.final_seam_count === i.user_seam_count ? '' : 'bad'}>
            <td>{t('generate.row.finalSeams')}</td><td>{i.final_seam_count.toLocaleString()}</td>
          </tr>
          <tr className={i.auto_added_seams === 0 ? '' : 'bad'}>
            <td>{t('generate.row.autoAdded')}</td><td>{i.auto_added_seams}</td>
          </tr>
          <tr><td>{t('generate.row.mandatoryRule')}</td><td>{i.mandatory_rule_enabled ? t('common.on') : t('generate.offReportOnly')}</td></tr>
        </tbody>
      </table>
    </>
  );
}

/**
 * Honest optimization summary (plan §2 Goal D). Shows the seam source + derived seam
 * count, the packing / stretch / texel before→after deltas with a per-row verdict tag,
 * and one overall result line so the user can tell whether the optimization actually
 * helped — never implying a win the metrics do not support.
 */
function OptimizationSummary(props: {
  lo: NonNullable<UvGenerateRunView['summary']>['layout_optimization'];
  seamSource: SeamSource | null;
  integrity: SeamIntegrity | null;
}): JSX.Element {
  const t = useT();
  const { lo, seamSource, integrity } = props;
  const verdict = (lo.verdict ?? (lo.kept_baseline ? 'baseline_retained' : 'minor_packing_only')) as string;
  const verdictKey = `generate.verdict.${verdict}` as TKey;
  const seamCount = integrity?.final_seam_count ?? integrity?.user_seam_count ?? null;
  const derived = !!seamSource?.derived;
  return (
    <div className="optsummary">
      <div className="muted small">
        {derived
          ? t('generate.sourceExistingUv', { layer: seamSource?.uv_layer ?? '—' })
          : t('generate.sourceExplicitSpec')}
      </div>
      {seamCount != null && (
        <div className="muted small">{t('generate.derivedSeams', { n: seamCount.toLocaleString() })}</div>
      )}
      <table className="metrics optdeltas">
        <tbody>
          <DeltaRow label={t('generate.row.packing')} before={lo.packing_efficiency_before} after={lo.packing_efficiency_after} higherBetter />
          <DeltaRow label={t('generate.row.stretch')} before={lo.stretch_before} after={lo.stretch_after} higherBetter={false} />
          <DeltaRow label={t('generate.row.texel')} before={lo.texel_density_before} after={lo.texel_density_after} higherBetter={false} />
        </tbody>
      </table>
      <div className={`optverdict v-${verdict}`}>
        {t('generate.optResult', { verdict: t(verdictKey) })}
      </div>
    </div>
  );
}

/** One before→after row with a percentage change and an honest delta tag. */
function DeltaRow(props: {
  label: string;
  before: number | null | undefined;
  after: number | null | undefined;
  higherBetter: boolean;
}): JSX.Element {
  const t = useT();
  const { before, after, higherBetter } = props;
  const has = typeof before === 'number' && typeof after === 'number';
  const delta = has ? (after as number) - (before as number) : 0;
  const pct = has && Math.abs(before as number) > 1e-9 ? (delta / (before as number)) * 100 : 0;
  // Classify: unchanged (~0), improved (in the better direction & non-trivial), else worse.
  let tag: 'unchanged' | 'improved' | 'minor' | 'worse' = 'unchanged';
  if (has && Math.abs(delta) > 1e-5) {
    const better = higherBetter ? delta > 0 : delta < 0;
    if (better) tag = Math.abs(pct) >= 5 ? 'improved' : 'minor';
    else tag = 'worse';
  }
  const tagKey = `generate.delta.${tag}` as TKey;
  return (
    <tr>
      <td>{props.label}</td>
      <td>
        {fmtNum(before)} → {fmtNum(after)}
        {has && Math.abs(delta) > 1e-5 && (
          <span className="muted small"> ({pct > 0 ? '+' : ''}{pct.toFixed(1)}%)</span>
        )}{' '}
        <span className={`tag ${tag === 'improved' || tag === 'minor' ? 'ok' : tag === 'worse' ? 'bad' : 'unknown'}`}>
          {t(tagKey)}
        </span>
      </td>
    </tr>
  );
}

function RunOptions(props: {
  options: GenerateUvOptions;
  setOptions: (o: GenerateUvOptions) => void;
}): JSX.Element {
  const t = useT();
  const { options, setOptions } = props;
  return (
    <div className="runopts">
      <label className="optrow">
        <span>{t('generate.optimizeLayout')}</span>
        <input
          type="checkbox"
          checked={options.optimize_layout ?? true}
          onChange={(e) => setOptions({ ...options, optimize_layout: e.target.checked })}
        />
      </label>
      <label className="optrow">
        <span>{t('generate.maxCandidates')}</span>
        <input
          type="number"
          min={1}
          max={48}
          value={options.layout_opt_max_candidates ?? 24}
          onChange={(e) =>
            setOptions({ ...options, layout_opt_max_candidates: Number(e.target.value) || 24 })
          }
        />
      </label>
      <div className="muted small">{t('generate.strictHint')}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Bottom: run status / raw reports (summary, p5_gate, seam_report) / logs
// ---------------------------------------------------------------------------
type BottomTab = 'summary' | 'candidate' | 'p5_gate' | 'seam_report' | 'logs';

const STATUS_TEXT: Record<string, TKey> = {
  accepted: 'generate.statusText.accepted',
  needs_user_review: 'generate.statusText.needs_user_review',
  needs_input: 'generate.statusText.needs_input',
  failed: 'generate.statusText.failed',
  cancelled: 'generate.statusText.cancelled',
  running: 'generate.statusText.running',
  queued: 'generate.statusText.queued',
};

function GenerateBottomPanel(props: {
  runView: UvGenerateRunView | null;
  status: string | null;
}): JSX.Element {
  const t = useT();
  const [tab, setTab] = useState<BottomTab>('summary');
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
          <button className={tab === 'summary' ? 'active' : ''} onClick={() => setTab('summary')}>{t('common.tab.summary')}</button>
          <button className={tab === 'candidate' ? 'active' : ''} onClick={() => setTab('candidate')}>{t('generate.tab.candidates')}</button>
          <button className={tab === 'p5_gate' ? 'active' : ''} onClick={() => setTab('p5_gate')}>p5_gate</button>
          <button className={tab === 'seam_report' ? 'active' : ''} onClick={() => setTab('seam_report')}>seam_report</button>
          <button className={tab === 'logs' ? 'active' : ''} onClick={() => setTab('logs')}>{t('common.tab.logs')}</button>
        </nav>
        <div className="tabbody">
          {!rv && <div className="muted">{t('generate.noRunSelected')}</div>}
          {rv && tab === 'summary' && <Json data={rv.summary} empty={t('common.noSummary')} />}
          {rv && tab === 'candidate' && <Json data={rv.candidate_summary} empty={t('generate.noCandSummary')} />}
          {rv && tab === 'p5_gate' && <Json data={rv.p5_gate} empty={t('generate.noP5')} />}
          {rv && tab === 'seam_report' && <Json data={rv.seam_report} empty={t('generate.noSeamReport')} />}
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

function fmtMetric(key: keyof GenerateMetrics, v: number | boolean | null | undefined): string {
  if (v === null || v === undefined) return '—';
  if (key === 'uv_bounds_ok') return v ? 'yes' : 'no';
  if (key === 'island_count') return String(v);
  return Number(v).toFixed(4);
}
