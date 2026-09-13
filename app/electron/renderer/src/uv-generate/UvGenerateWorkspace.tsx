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
  ArtistApproval,
  CandidateHistoryEntry,
  CandidateRow,
  CandidateSummary,
  CatastrophicReport,
  DistortionIslandRow,
  GenerateMetrics,
  GenerateUvOptions,
  MeshObjectSummary,
  Project,
  SeamIntegrity,
  SeamOverlayEdge,
  SeamSource,
  UvFeedback,
  UvGenerateMode,
  UvGenerateRunView,
  ValidateGenerateInput,
} from '@shared/contracts';
import {
  DEFAULT_UV_GENERATE_MODE,
  MODE_GENERATE_OPTIONS,
  STRICT_FLAGS,
  UV_GENERATE_TERMINAL_STATUSES,
  UvGenerateMode as UvGenerateModeValues,
  validateModeRequest,
} from '@shared/contracts';
import type { Banner } from '../App';
import { useT, statusLabel, type TKey } from '../i18n';
import { previewUrl } from '../previewUrl';
import {
  REASON_CODE_ORDER,
  SEAM_TYPE_ORDER,
  SeamOverlayView,
  reasonCodeColor,
  seamEdgeColor,
  seamTypeColor,
} from './SeamOverlayView';

/** i18n label per G15 reason code — the legend enumerates all seven. */
const REASON_CODE_LABELS: Record<string, TKey> = {
  mandatory_90: 'generate.rc.mandatory_90',
  boundary_topology: 'generate.rc.boundary_topology',
  user: 'generate.rc.user',
  shading: 'generate.rc.shading',
  material: 'generate.rc.material',
  distortion_added: 'generate.rc.distortion_added',
  rejected_candidate: 'generate.rc.rejected_candidate',
};

type CenterTab = 'checker' | 'layout' | 'candidates' | 'heatmap' | 'seams';
type CheckerView = 'front' | 'side';

/** Reviewer-feedback form state — raw text, parsed to edge ids only on save. */
interface FeedbackForm {
  locked: string;
  protectedEdges: string;
  preferred: string;
  front_axis: string;
  notes: string;
}

const EMPTY_FEEDBACK: FeedbackForm = {
  locked: '',
  protectedEdges: '',
  preferred: '',
  front_axis: '',
  notes: '',
};

const FRONT_AXES = ['+X', '-X', '+Y', '-Y', '+Z', '-Z'] as const;

/** Parse a comma/space separated edge-id list into unique sorted integers. */
function parseEdgeIds(raw: string): number[] {
  const out = new Set<number>();
  for (const tok of raw.split(/[\s,]+/)) {
    if (!tok) continue;
    const n = Number(tok);
    if (Number.isInteger(n) && n >= 0) out.add(n);
  }
  return Array.from(out).sort((a, b) => a - b);
}

function joinEdgeIds(ids: number[] | undefined | null): string {
  return (ids ?? []).join(', ');
}

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
  const [mode, setMode] = useState<UvGenerateMode>(DEFAULT_UV_GENERATE_MODE);
  const [options, setOptions] = useState<GenerateUvOptions>({
    ...MODE_GENERATE_OPTIONS[DEFAULT_UV_GENERATE_MODE],
  });
  const [selectedEdgeId, setSelectedEdgeId] = useState<number | null>(null);
  // Gate G2/G9: a project imported straight from a UV-less low-poly has no
  // `selected_object` yet (that is only written by the low-poly / UV review
  // paths), so the object is picked here and sent with the start request.
  const [objectName, setObjectName] = useState<string>(project?.selected_object ?? '');
  const [objectChoices, setObjectChoices] = useState<MeshObjectSummary[]>([]);
  const [objectListFailed, setObjectListFailed] = useState(false);
  const [feedbackForm, setFeedbackForm] = useState<FeedbackForm>(EMPTY_FEEDBACK);
  const [savedFeedback, setSavedFeedback] = useState<UvFeedback | null>(null);
  const [reviewer, setReviewer] = useState('');
  const [rejectReason, setRejectReason] = useState('');

  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  // Reset per-project; seed the latest run + the persisted mode (plan §9; gate G2).
  useEffect(() => {
    const m = project?.uv_generate_mode ?? DEFAULT_UV_GENERATE_MODE;
    setValidation(null);
    setRunView(null);
    setRunId(project?.latest_uv_generate_run_id ?? null);
    setMode(m);
    setOptions({ ...MODE_GENERATE_OPTIONS[m] });
    setSelectedEdgeId(null);
    setRejectReason('');
    setObjectName(project?.selected_object ?? '');
    setObjectChoices([]);
    setObjectListFailed(false);
  }, [project?.id]);

  // Gate G2/G9: with no `selected_object` recorded, ask the worker for the mesh
  // object list so the left panel can offer a choice (default: the first one)
  // instead of showing "—" and blocking Generate. A failure is a short panel
  // hint, not a banner — the project is still usable.
  useEffect(() => {
    let cancelled = false;
    if (!project) return;
    if (project.selected_object) {
      setObjectName(project.selected_object);
      return;
    }
    window.api
      .modelInspect({ projectId: project.id })
      .then((res) => {
        if (cancelled) return;
        const objects = res.status === 'accepted' ? res.objects ?? [] : [];
        setObjectChoices(objects);
        setObjectListFailed(objects.length === 0);
        if (objects.length) setObjectName((cur) => cur || objects[0].name);
      })
      .catch(() => {
        if (cancelled) return;
        setObjectChoices([]);
        setObjectListFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [project?.id, project?.selected_object]);

  // Load the saved reviewer feedback so the form opens pre-filled (gate G7).
  useEffect(() => {
    let cancelled = false;
    if (!project) {
      setSavedFeedback(null);
      setFeedbackForm(EMPTY_FEEDBACK);
      return;
    }
    window.api
      .uvGenerateGetFeedback({ projectId: project.id })
      .then((fb) => {
        if (cancelled) return;
        setSavedFeedback(fb);
        setFeedbackForm(
          fb
            ? {
                locked: joinEdgeIds(fb.locked_seam_edges),
                protectedEdges: joinEdgeIds(fb.protected_edges),
                preferred: joinEdgeIds(fb.preferred_edges),
                front_axis: fb.front_axis ?? '',
                notes: fb.notes ?? '',
              }
            : EMPTY_FEEDBACK,
        );
      })
      .catch(() => {
        if (!cancelled) {
          setSavedFeedback(null);
          setFeedbackForm(EMPTY_FEEDBACK);
        }
      });
    return () => {
      cancelled = true;
    };
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

  /**
   * Run the SHARED mode/flag contradiction check in the renderer too (work plan
   * §3; gate G2) so a contradictory request never reaches IPC. Returns false and
   * banners the first error when the combination is rejected.
   */
  const checkModeRequest = useCallback((): boolean => {
    const res = validateModeRequest(mode, options);
    if (!res.ok) {
      setBanner({ kind: 'error', text: res.errors.map((e) => e.message).join(' · ') });
      return false;
    }
    return true;
  }, [mode, options, setBanner]);

  const onChangeMode = (next: UvGenerateMode) =>
    guard(t('busy.settingMode'), async () => {
      if (!project) {
        setBanner({ kind: 'error', text: t('common.openImportFirst') });
        return;
      }
      const p = await window.api.uvGenerateSetMode({ projectId: project.id, mode: next });
      setMode(next);
      setOptions({ ...MODE_GENERATE_OPTIONS[next] });
      setValidation(null);
      props.setProject(p);
    });

  const onValidate = () =>
    guard(t('busy.validatingSpec'), async () => {
      if (!project) {
        setBanner({ kind: 'error', text: t('common.openImportFirst') });
        return;
      }
      if (!checkModeRequest()) return;
      const v = await window.api.uvGenerateValidateInput({ projectId: project.id, mode });
      setValidation(v);
      if (!objectName && v.object_name) setObjectName(v.object_name);
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
      if (!checkModeRequest()) return;
      const v =
        validation ?? (await window.api.uvGenerateValidateInput({ projectId: project.id, mode }));
      setValidation(v);
      // In auto mode a missing seam source is not a blocker — the solver cuts
      // from scratch (work plan §3) — so only preserve mode gates on readiness.
      if (!v.ready && mode === UvGenerateModeValues.PreserveExisting) {
        setBanner({
          kind: 'error',
          text: v.issues[0]?.message ?? t('generate.cannotGenerate'),
        });
        return;
      }
      let run_id: string;
      try {
        // A contradictory flag set or an unsupported Blender build is rejected by
        // main; surface the reason verbatim instead of a generic failure.
        ({ run_id } = await window.api.uvGenerateStart({
          projectId: project.id,
          // The locally chosen object wins: `validate` reports null for a
          // project that has no `selected_object` recorded yet (gate G2/G9).
          objectName: objectName || v.object_name || undefined,
          options,
          mode,
        }));
      } catch (e) {
        setBanner({ kind: 'error', text: (e as Error).message });
        return;
      }
      setRunId(run_id);
      setRunView(null);
      setSelectedEdgeId(null);
      setCenterTab('checker');
      const p = await window.api.projectGet(project.id);
      props.setProject(p);
    });

  const onSetApproval = (approved: boolean) =>
    guard(t('busy.savingApproval'), async () => {
      if (!project || !runId) {
        setBanner({ kind: 'error', text: t('generate.noRunSelected') });
        return;
      }
      if (!approved && !rejectReason.trim()) {
        setBanner({ kind: 'error', text: t('generate.rejectNeedsReason') });
        return;
      }
      const approval: ArtistApproval = {
        approved,
        run_id: runId,
        reviewer: reviewer.trim() || null,
        reason: rejectReason.trim() || null,
        approved_at: new Date().toISOString(),
      };
      const p = await window.api.uvGenerateSetArtistApproval({ projectId: project.id, approval });
      props.setProject(p);
      setBanner({
        kind: 'info',
        text: approved ? t('generate.approvalSaved') : t('generate.rejectionSaved'),
      });
    });

  const onSaveFeedback = () =>
    guard(t('busy.savingFeedback'), async () => {
      if (!project) {
        setBanner({ kind: 'error', text: t('common.openImportFirst') });
        return;
      }
      const ident = runView?.summary?.mesh_identity ?? null;
      const fingerprint = ident?.after_sha256 ?? ident?.before_sha256 ?? null;
      const saved = await window.api.uvGenerateSaveFeedback({
        projectId: project.id,
        feedback: {
          mesh_fingerprint: fingerprint,
          object_name: runView?.summary?.object_name ?? project.selected_object ?? null,
          locked_seam_edges: parseEdgeIds(feedbackForm.locked),
          protected_edges: parseEdgeIds(feedbackForm.protectedEdges),
          preferred_edges: parseEdgeIds(feedbackForm.preferred),
          front_axis: feedbackForm.front_axis,
          notes: feedbackForm.notes,
          source_run_id: runId,
        },
      });
      setSavedFeedback(saved);
      setBanner({
        kind: fingerprint ? 'info' : 'error',
        text: fingerprint ? t('generate.feedbackSaved') : t('generate.feedbackNoFingerprint'),
      });
    });

  /** Append the picked seam edge id to one of the three constraint fields. */
  const addSelectedEdgeTo = (field: keyof Omit<FeedbackForm, 'front_axis' | 'notes'>) => {
    if (selectedEdgeId === null) return;
    setFeedbackForm((f) => {
      const ids = parseEdgeIds(f[field]);
      if (!ids.includes(selectedEdgeId)) ids.push(selectedEdgeId);
      return { ...f, [field]: joinEdgeIds(ids.sort((a, b) => a - b)) };
    });
  };

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
  // Auto mode cuts from scratch, so it only needs a model + an object; preserve
  // mode still needs a seam SOURCE to preserve (work plan §3; gate G2).
  const canGenerate =
    !!project &&
    hasModel &&
    !!objectName &&
    (mode === UvGenerateModeValues.AutoGenerate || hasSeamSource);

  return (
    <>
      <div className="subtoolbar">
        <ModeSelect mode={mode} disabled={!project || running} onChange={onChangeMode} />
        <span className="sep" />
        <button disabled={!project} onClick={onValidate}>{t('generate.validate')}</button>
        <button disabled={!canGenerate} className="primary" onClick={onGenerate}>
          {t('generate.generate')}
        </button>
        <button disabled={!running} onClick={onCancel}>{t('common.cancel')}</button>
        <button disabled title={t('generate.nextAiTitle')}>{t('generate.nextAi')}</button>
        {project && <span className="muted small subtoolbar-hint">{project.name}</span>}
      </div>

      <VerdictBanner summary={summary} status={status} project={project} runId={runId} />

      <div className="body">
        <GenerateLeftPanel
          project={project}
          validation={validation}
          activeRunId={runId}
          onSelectRun={setRunId}
          objectName={objectName}
          objectChoices={objectChoices}
          objectListFailed={objectListFailed}
          onSelectObject={setObjectName}
        />

        <main className="center uv-center">
          <GenerateCenter
            runView={runView}
            summary={summary}
            status={status}
            centerTab={centerTab}
            setCenterTab={setCenterTab}
            checkerView={checkerView}
            setCheckerView={setCheckerView}
            selectedEdgeId={selectedEdgeId}
            setSelectedEdgeId={setSelectedEdgeId}
          />
        </main>

        <GenerateRightPanel
          summary={summary}
          candidateSummary={runView?.candidate_summary ?? null}
          options={options}
          setOptions={setOptions}
          accepted={accepted}
          mode={mode}
          project={project}
          runId={runId}
          reviewer={reviewer}
          setReviewer={setReviewer}
          rejectReason={rejectReason}
          setRejectReason={setRejectReason}
          onSetApproval={onSetApproval}
          feedbackForm={feedbackForm}
          setFeedbackForm={setFeedbackForm}
          savedFeedback={savedFeedback}
          onSaveFeedback={onSaveFeedback}
          selectedEdgeId={selectedEdgeId}
          addSelectedEdgeTo={addSelectedEdgeTo}
        />
      </div>

      <GenerateBottomPanel runView={runView} status={status} />
    </>
  );
}

// ---------------------------------------------------------------------------
// Mode selection (work plan §3; gate G2) — persisted on the project.
// ---------------------------------------------------------------------------
function ModeSelect(props: {
  mode: UvGenerateMode;
  disabled: boolean;
  onChange: (m: UvGenerateMode) => void;
}): JSX.Element {
  const t = useT();
  return (
    <label className="modeselect">
      <span className="small muted">{t('generate.mode')}</span>
      <select
        value={props.mode}
        disabled={props.disabled}
        onChange={(e) => props.onChange(e.target.value as UvGenerateMode)}
      >
        <option value={UvGenerateModeValues.PreserveExisting}>
          {t('generate.mode.preserve_existing')}
        </option>
        <option value={UvGenerateModeValues.AutoGenerate}>
          {t('generate.mode.auto_generate')}
        </option>
      </select>
    </label>
  );
}

/**
 * The honest verdict banner (gates G6/G7): `solver accepted` and `artist
 * approved` are SEPARATE tags, a preserve-mode pass says "seams preserved (not
 * an automatic-rule pass)", and an auto-mode pass on an uncalibrated profile is
 * tagged as engineering-only.
 */
function VerdictBanner(props: {
  summary: UvGenerateRunView['summary'];
  status: string | null;
  project: Project | null;
  runId: string | null;
}): JSX.Element | null {
  const t = useT();
  const { summary, status, project, runId } = props;
  if (!status || !UV_GENERATE_TERMINAL_STATUSES.has(status)) return null;

  const mode = summary?.mode ?? UvGenerateModeValues.PreserveExisting;
  const solverAccepted = summary?.solver_accepted ?? status === 'accepted';
  const approval = project?.uv_artist_approval ?? null;
  const approvedThisRun = !!approval?.approved && (!approval.run_id || approval.run_id === runId);
  const gate = summary?.auto_gate ?? null;
  const calibrated = summary?.quality_profile?.calibrated ?? null;

  const reasons: string[] = [];
  if (status === 'needs_user_review') {
    for (const f of gate?.failures ?? []) reasons.push(f);
    for (const r of gate?.invalid_reasons ?? []) reasons.push(r);
    if (summary?.acceptance_reason) reasons.push(summary.acceptance_reason);
  }

  return (
    <div className={`gen-verdict ${status}`}>
      <span className={`tag ${solverAccepted ? 'ok' : 'bad'}`}>
        {solverAccepted ? t('generate.solverAccepted') : t('generate.solverNotAccepted')}
      </span>
      <span className={`tag ${approvedThisRun ? 'ok' : 'unknown'}`}>
        {approvedThisRun ? t('generate.artistApproved') : t('generate.artistNotApproved')}
      </span>
      {solverAccepted && mode === UvGenerateModeValues.PreserveExisting && (
        <span className="small">{t('generate.preserveAcceptedNote')}</span>
      )}
      {solverAccepted && mode === UvGenerateModeValues.AutoGenerate && (
        <span className="small">{t('generate.autoGatePassed')}</span>
      )}
      {solverAccepted && mode === UvGenerateModeValues.AutoGenerate && calibrated === false && (
        <span className="tag bad">{t('generate.profileUncalibrated')}</span>
      )}
      {reasons.length > 0 && (
        <span className="small warn">{t('generate.reviewReasons', { list: reasons.join(' · ') })}</span>
      )}
    </div>
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
  objectName: string;
  objectChoices: MeshObjectSummary[];
  objectListFailed: boolean;
  onSelectObject: (name: string) => void;
}): JSX.Element {
  const t = useT();
  const { project, validation } = props;
  const runs = project?.uv_generate_runs ?? [];
  // The working model falls back to `source_model` in main (resolveWorkingModel),
  // so the panel must show that same fact instead of "—" (gate G2/G9).
  const workingModel = project?.working_model ?? project?.working_model_fbx ?? null;
  const shownModel = workingModel ?? project?.source_model ?? null;
  return (
    <aside className="left">
      <section>
        <h3>{t('common.project')}</h3>
        {project ? (
          <div className="kv">
            <div>{project.name}</div>
            <div className="small">
              {t('common.objectRow')}:{' '}
              {props.objectChoices.length ? (
                <select
                  value={props.objectName}
                  onChange={(e) => props.onSelectObject(e.target.value)}
                >
                  {props.objectChoices.map((o) => (
                    <option key={o.name} value={o.name}>
                      {o.name}
                    </option>
                  ))}
                </select>
              ) : (
                <code>{props.objectName || '—'}</code>
              )}
            </div>
            {!props.objectName && props.objectListFailed && (
              <div className="muted small">{t('generate.objectListFailed')}</div>
            )}
          </div>
        ) : (
          <div className="muted">{t('common.noProjectOpen')}</div>
        )}
      </section>

      <section>
        <h3>{t('generate.workingModel')}</h3>
        <div className="small">
          <code>{shownModel ?? '—'}</code>
          {!workingModel && shownModel && <span className="tag">{t('generate.tag.source')}</span>}
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
  selectedEdgeId: number | null;
  setSelectedEdgeId: (id: number | null) => void;
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
        <button className={props.centerTab === 'heatmap' ? 'active' : ''} onClick={() => props.setCenterTab('heatmap')}>
          {t('generate.tab.heatmap')}
        </button>
        <button className={props.centerTab === 'seams' ? 'active' : ''} onClick={() => props.setCenterTab('seams')}>
          {t('generate.tab.seams')}
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
          <LayoutTab
            beforeSrc={paths.baseline_uv_layout}
            afterSrc={paths.selected_uv_layout}
            overlaySrc={paths.seam_overlay_png}
          />
        )}

        {props.centerTab === 'candidates' && (
          <div className="candidates-tab">
            <CandidateTable candidateSummary={runView.candidate_summary} summary={summary} />
            <MergeBackList history={runView.merge_back_history} />
          </div>
        )}

        {props.centerTab === 'heatmap' && (
          <HeatmapTab
            summary={summary}
            catastrophic={runView.catastrophic ?? null}
            src={paths.selected_heatmap_anisotropy}
          />
        )}

        {props.centerTab === 'seams' && (
          <SeamOverlayPanel
            overlay={runView.seam_overlay}
            selectedEdgeId={props.selectedEdgeId}
            setSelectedEdgeId={props.setSelectedEdgeId}
          />
        )}
      </div>
    </div>
  );
}

/**
 * The anisotropy heatmap with its gate identity (gates CG4/CG13).
 *
 * The picture is only evidence if it was measured from the SAME UV the gate
 * scored, so the badge states that relationship explicitly — "heatmap <-> gate
 * identity OK", or the mismatching fields — and the run's `uv_hash` prefix is
 * shown next to it. A failed catastrophic gate turns the tab into a red banner
 * plus the worst damaged regions, because the layout is not shippable and the
 * reviewer must not read the heatmap as a merely-cosmetic warning.
 */
function HeatmapTab(props: {
  summary: UvGenerateRunView['summary'];
  catastrophic: CatastrophicReport | null;
  src?: string;
}): JSX.Element {
  const t = useT();
  const identity = props.summary?.heatmap_identity ?? null;
  const hash = props.summary?.uv_hash ?? null;
  const cat = props.summary?.catastrophic ?? null;
  const mismatches = identity?.mismatches ?? [];
  const failed = cat?.passed === false;
  const regions = (props.catastrophic?.regions ?? []).slice(0, 10);

  return (
    <div className="heatmap-tab">
      <div className="heatmap-identity">
        {identity ? (
          identity.passed ? (
            <span className="tag ok">{t('generate.hm.identityOk')}</span>
          ) : (
            <span className="tag bad">
              {t('generate.hm.identityMismatch', {
                fields: mismatches.length ? mismatches.join(', ') : t('generate.hm.unknownFields'),
              })}
            </span>
          )
        ) : (
          <span className="tag unknown">{t('generate.hm.identityUnknown')}</span>
        )}{' '}
        <span className="muted small">
          {t('generate.hm.uvHash')} {hash ? hash.slice(0, 12) : '—'}
        </span>
      </div>

      {failed && (
        <div className="banner error catastrophic-banner">
          <div>
            {t('generate.cat.banner', {
              triangles: cat?.bad_triangle_count ?? 0,
              regions: cat?.bad_region_count ?? 0,
            })}
          </div>
          {regions.length > 0 && (
            <table className="metrics catastrophic-regions">
              <tbody>
                {regions.map((r) => (
                  <tr key={r.region_id}>
                    <td>
                      {t('generate.cat.region')} {r.region_id}
                    </td>
                    <td className="muted small">
                      {t('generate.cat.island')} {r.island_id ?? '—'} ·{' '}
                      {t('generate.cat.faces')} {(r.face_ids ?? []).length} ·{' '}
                      {t('generate.cat.areaFraction')} {fmtPct(r.area_fraction)} ·{' '}
                      {t('generate.cat.reasons')}{' '}
                      {(r.reasons ?? []).length ? (r.reasons ?? []).join(', ') : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {props.src ? (
        <div className="preview">
          <img alt={t('generate.tab.heatmap')} src={previewUrl(props.src)} />
        </div>
      ) : (
        <div className="placeholder">{t('generate.noHeatmap')}</div>
      )}
    </div>
  );
}

/**
 * 3D seam overlay + per-type legend + picked-edge detail (gate G7): every seam
 * carries the reason/round/target island/improvement that produced it.
 */
function SeamOverlayPanel(props: {
  overlay: UvGenerateRunView['seam_overlay'];
  selectedEdgeId: number | null;
  setSelectedEdgeId: (id: number | null) => void;
}): JSX.Element {
  const t = useT();
  // Gate G15: rejected candidate cuts are shown by default — "what the engine
  // decided NOT to do" is part of the evidence, not an opt-in extra.
  const [showRejected, setShowRejected] = useState(true);
  const ov = props.overlay;
  if (!ov || ov.edges.length === 0) {
    return <div className="placeholder">{t('generate.noSeamOverlay')}</div>;
  }
  const selected = ov.edges.find((e) => e.edge_id === props.selectedEdgeId) ?? null;
  const counts = ov.type_counts ?? {};
  const types = Array.from(
    new Set<string>([...SEAM_TYPE_ORDER, ...Object.keys(counts), ...ov.edges.map((e) => e.type)]),
  ).filter((ty) => (counts[ty] ?? ov.edges.filter((e) => e.type === ty).length) > 0);

  // Gate G15: the reason-code legend. Counts come from the artifact when the
  // engine reported them, else they are recomputed from the edges themselves so
  // an older overlay still shows an honest tally.
  const rejected = ov.rejected_candidates ?? [];
  const codeCounts: Record<string, number> = { ...(ov.reason_code_counts ?? {}) };
  if (!ov.reason_code_counts) {
    for (const e of ov.edges) {
      const c = String(e.reason_code ?? '');
      if (c) codeCounts[c] = (codeCounts[c] ?? 0) + 1;
    }
    if (rejected.length) codeCounts.rejected_candidate = rejected.length;
  }
  const hasReasonCodes = Object.keys(codeCounts).length > 0;

  return (
    <div className="seamoverlay-wrap">
      <SeamOverlayView
        edges={ov.edges}
        rejected={rejected}
        showRejected={showRejected}
        selectedEdgeId={props.selectedEdgeId}
        onPick={props.setSelectedEdgeId}
      />
      <div className="seamoverlay-side">
        <h4>{t('generate.reasonLegend')}</h4>
        <ul className="seamlegend">
          {REASON_CODE_ORDER.map((code) => (
            <li key={code} className={code === 'rejected_candidate' ? 'dashed' : ''}>
              <span
                className="dot"
                style={{
                  background:
                    code === 'rejected_candidate' ? 'transparent' : reasonCodeColor(code),
                  border:
                    code === 'rejected_candidate'
                      ? `2px dashed ${reasonCodeColor(code)}`
                      : undefined,
                }}
              />
              <span className="small">{t(REASON_CODE_LABELS[code])}</span>
              <span className="muted small">{codeCounts[code] ?? 0}</span>
            </li>
          ))}
        </ul>
        {!hasReasonCodes && <div className="muted small">{t('generate.noReasonCodes')}</div>}
        <label className="small">
          <input
            type="checkbox"
            checked={showRejected}
            onChange={(e) => setShowRejected(e.target.checked)}
          />{' '}
          {t('generate.showRejectedCuts', { n: rejected.length })}
        </label>

        <h4>{t('generate.seamLegend')}</h4>
        <ul className="seamlegend">
          {types.map((ty) => (
            <li key={ty}>
              <span className="dot" style={{ background: seamTypeColor(ty) }} />
              <span className="small">{ty}</span>
              <span className="muted small">
                {counts[ty] ?? ov.edges.filter((e) => e.type === ty).length}
              </span>
            </li>
          ))}
        </ul>

        <h4>{t('generate.seamEdges')}</h4>
        <ul className="list seamedgelist">
          {ov.edges.slice(0, 400).map((e) => (
            <li
              key={e.edge_id}
              className={e.edge_id === props.selectedEdgeId ? 'sel' : ''}
              onClick={() => props.setSelectedEdgeId(e.edge_id)}
            >
              <span className="dot" style={{ background: seamEdgeColor(e) }} />
              <code className="small">#{e.edge_id}</code>{' '}
              <span className="muted small">{e.reason_code ?? e.type}</span>
            </li>
          ))}
        </ul>

        {showRejected && rejected.length > 0 && (
          <>
            <h4>{t('generate.rejectedCuts')}</h4>
            <ul className="list seamedgelist">
              {rejected.slice(0, 200).map((r) => (
                <li key={r.edge_id} className="rejected">
                  <span
                    className="dot"
                    style={{
                      background: 'transparent',
                      border: `2px dashed ${reasonCodeColor('rejected_candidate')}`,
                    }}
                  />
                  <code className="small">#{r.edge_id}</code>{' '}
                  <span className="muted small">
                    {r.reject_reason ?? r.kind ?? '—'}
                    {r.round === null || r.round === undefined ? '' : ` · r${r.round}`}
                    {r.cost_total === null || r.cost_total === undefined
                      ? ''
                      : ` · ${fmtNum(r.cost_total, 3)}`}
                  </span>
                </li>
              ))}
            </ul>
          </>
        )}

        <h4>{t('generate.seamEdgeDetail')}</h4>
        {selected ? (
          <SeamEdgeDetail edge={selected} />
        ) : (
          <div className="muted small">{t('generate.seamPickHint')}</div>
        )}

        {ov.conflicts.length > 0 && (
          <>
            <h4>{t('generate.seamConflicts')}</h4>
            <ul className="issuelist">
              {ov.conflicts.map((c, i) => (
                <li key={i} className="warning">
                  <span className="sevdot warning" /> #{c.edge_id} {c.user_rule ?? '—'} /{' '}
                  {c.engine_rule ?? '—'} → {c.resolution ?? '—'}
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </div>
  );
}

function SeamEdgeDetail(props: { edge: SeamOverlayEdge }): JSX.Element {
  const t = useT();
  const e = props.edge;
  return (
    <table className="metrics">
      <tbody>
        <tr><td>{t('generate.edge.id')}</td><td>#{e.edge_id}</td></tr>
        <tr><td>{t('generate.edge.type')}</td><td>{e.type}</td></tr>
        <tr>
          <td>{t('generate.edge.reasonCode')}</td>
          <td>
            <span className="dot" style={{ background: seamEdgeColor(e) }} />{' '}
            {e.reason_code ?? '—'}
          </td>
        </tr>
        <tr><td>{t('generate.edge.cutReason')}</td><td>{e.cut_reason ?? '—'}</td></tr>
        <tr><td>{t('generate.edge.cost')}</td><td>{fmtNum(e.cost_total, 3)}</td></tr>
        <tr><td>{t('generate.edge.reason')}</td><td>{e.reason ?? '—'}</td></tr>
        <tr><td>{t('generate.edge.stage')}</td><td>{e.stage ?? '—'}</td></tr>
        <tr><td>{t('generate.edge.round')}</td><td>{e.round ?? '—'}</td></tr>
        <tr><td>{t('generate.edge.targetIsland')}</td><td>{e.target_island ?? '—'}</td></tr>
        <tr>
          <td>{t('generate.edge.improvement')}</td>
          <td>{fmtNum(e.improvement_ratio)}</td>
        </tr>
      </tbody>
    </table>
  );
}

/**
 * UV layout tab: the before/after layout pair, with a toggle to the flat seam
 * overlay image when the run produced one (gate G15 `seam_overlay_png`).
 */
function LayoutTab(props: {
  beforeSrc?: string;
  afterSrc?: string;
  overlaySrc?: string;
}): JSX.Element {
  const t = useT();
  const [view, setView] = useState<'layout' | 'overlay'>('layout');
  const showOverlay = !!props.overlaySrc && view === 'overlay';
  return (
    <div className="layout-tab">
      {props.overlaySrc && (
        <div className="viewtoggle">
          <button className={view === 'layout' ? 'active' : ''} onClick={() => setView('layout')}>
            {t('common.uvLayout')}
          </button>
          <button className={view === 'overlay' ? 'active' : ''} onClick={() => setView('overlay')}>
            {t('generate.seamOverlayImage')}
          </button>
        </div>
      )}
      {showOverlay ? (
        <div className="preview">
          <img alt={t('generate.seamOverlayImage')} src={previewUrl(props.overlaySrc as string)} />
        </div>
      ) : (
        <BeforeAfter
          beforeSrc={props.beforeSrc}
          afterSrc={props.afterSrc}
          label={t('common.uvLayout')}
        />
      )}
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

/**
 * Merge-back trial log (gate G15): every seam group the engine tried to dissolve,
 * whether it was kept, and what the island count did. Absent artifact -> a hint.
 */
function MergeBackList(props: { history: UvGenerateRunView['merge_back_history'] }): JSX.Element {
  const t = useT();
  const mb = props.history;
  const rows = mb?.history ?? [];
  return (
    <div className="mergeback-wrap">
      <h4>{t('generate.mergeBack')}</h4>
      {!mb ? (
        <div className="muted small">{t('generate.noMergeBack')}</div>
      ) : (
        <>
          <div className="small">
            <span className={`tag ${mb.complete ? 'ok' : 'unknown'}`}>
              {mb.complete ? t('generate.mb.complete') : t('generate.mb.incomplete')}
            </span>{' '}
            <span className="muted">
              {t('generate.mb.summary', {
                trials: mb.trials ?? 0,
                accepted: mb.accepted ?? 0,
                before: mb.island_count_before ?? '—',
                after: mb.island_count_after ?? '—',
                reason: mb.reason ?? '—',
              })}
            </span>
          </div>
          {rows.length === 0 ? (
            <div className="muted small">{t('generate.noMergeBackTrials')}</div>
          ) : (
            <table className="candtable">
              <thead>
                <tr>
                  <th>{t('generate.mb.trial')}</th>
                  <th>{t('generate.mb.islands')}</th>
                  <th>{t('generate.mb.edges')}</th>
                  <th>{t('generate.mb.accepted')}</th>
                  <th>{t('generate.mb.reason')}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={i} className={r.accepted ? '' : 'rejected'}>
                    <td>{r.trial ?? i + 1}</td>
                    <td>
                      {r.island_a ?? '—'} / {r.island_b ?? '—'}
                    </td>
                    <td>{(r.edges ?? []).length}</td>
                    <td>
                      <span className={`tag ${r.accepted ? 'ok' : 'unknown'}`}>
                        {r.accepted ? t('common.yes') : t('common.no')}
                      </span>
                    </td>
                    <td>{r.reason ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
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
  mode: UvGenerateMode;
  project: Project | null;
  runId: string | null;
  reviewer: string;
  setReviewer: (v: string) => void;
  rejectReason: string;
  setRejectReason: (v: string) => void;
  onSetApproval: (approved: boolean) => void;
  feedbackForm: FeedbackForm;
  setFeedbackForm: React.Dispatch<React.SetStateAction<FeedbackForm>>;
  savedFeedback: UvFeedback | null;
  onSaveFeedback: () => void;
  selectedEdgeId: number | null;
  addSelectedEdgeTo: (field: keyof Omit<FeedbackForm, 'front_axis' | 'notes'>) => void;
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
        <h3>{t('generate.qualityV2')}</h3>
        <QualityV2Block summary={summary} />
      </section>

      <section>
        <h3>{t('generate.correctness')}</h3>
        <CorrectnessSection summary={summary} />
      </section>

      <section>
        <h3>
          {t('generate.gameGates')} <GameGateHeaderTag summary={summary} />
        </h3>
        <GameGatesSection summary={summary} />
      </section>

      <section>
        <h3>{t('generate.termination')}</h3>
        <TerminationSection summary={summary} />
      </section>

      <section>
        <h3>{t('generate.seamLength')}</h3>
        <SeamLengthSection summary={summary} />
      </section>

      <section>
        <h3>{t('generate.constraints')}</h3>
        <ConstraintsSection summary={summary} />
      </section>

      <section>
        <h3>{t('generate.artistReview')}</h3>
        <ArtistReviewSection
          project={props.project}
          runId={props.runId}
          reviewer={props.reviewer}
          setReviewer={props.setReviewer}
          rejectReason={props.rejectReason}
          setRejectReason={props.setRejectReason}
          onSetApproval={props.onSetApproval}
        />
      </section>

      <section>
        <h3>{t('generate.feedback')}</h3>
        <FeedbackSection
          form={props.feedbackForm}
          setForm={props.setFeedbackForm}
          saved={props.savedFeedback}
          onSave={props.onSaveFeedback}
          selectedEdgeId={props.selectedEdgeId}
          addSelectedEdgeTo={props.addSelectedEdgeTo}
        />
      </section>

      <section>
        <h3>{t('generate.runOptions')}</h3>
        <RunOptions options={props.options} setOptions={props.setOptions} mode={props.mode} summary={summary} />
      </section>
    </aside>
  );
}

// --- Automation report sections (work plan §4, §5; gates G1/G3/G5) ---------

/** Distortion v2: global figures + the worst islands by anisotropy p95. */
function QualityV2Block(props: { summary: UvGenerateRunView['summary'] }): JSX.Element {
  const t = useT();
  const d = props.summary?.distortion_v2 ?? null;
  if (!d) return <div className="muted">{t('generate.noQualityV2')}</div>;
  const g = d.global ?? {};
  const worst = (d.islands ?? [])
    .slice()
    .sort((a: DistortionIslandRow, b: DistortionIslandRow) =>
      (b.anisotropy_p95 ?? 0) - (a.anisotropy_p95 ?? 0),
    )
    .slice(0, 5);
  return (
    <>
      {!d.valid && <div className="tag bad">{t('generate.metricsInvalid')}</div>}
      <table className="metrics">
        <tbody>
          <tr><td>{t('generate.q.anisoP95')}</td><td>{fmtNum(g.anisotropy_p95)}</td></tr>
          <tr><td>{t('generate.q.anisoMax')}</td><td>{fmtNum(g.anisotropy_max)}</td></tr>
          <tr><td>{t('generate.q.areaStretchMean')}</td><td>{fmtNum(g.area_stretch_mean)}</td></tr>
          <tr><td>{t('generate.q.exceedArea')}</td><td>{fmtNum(g.exceed_area_fraction)}</td></tr>
          <tr><td>{t('generate.q.islandCount')}</td><td>{(d.islands ?? []).length}</td></tr>
        </tbody>
      </table>
      {worst.length > 0 && (
        <>
          <div className="muted small">{t('generate.q.worstIslands')}</div>
          <table className="metrics">
            <tbody>
              {worst.map((i) => (
                <tr key={i.island_id}>
                  <td>#{i.island_id}</td>
                  <td>
                    {fmtNum(i.anisotropy_p95)} · {t('common.faces')} {i.face_count}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </>
  );
}

/** Hard UV correctness checks with the measured value vs its limit (gate G1). */
function CorrectnessSection(props: { summary: UvGenerateRunView['summary'] }): JSX.Element {
  const t = useT();
  const c = props.summary?.correctness ?? null;
  if (!c) return <div className="muted">{t('generate.noCorrectness')}</div>;
  return (
    <>
      <div className={`reviewbadge ${c.passed ? 'clean' : 'has_overlap'}`}>
        {c.passed ? t('generate.correctnessPassed') : t('generate.correctnessFailed')}
      </div>
      <table className="metrics">
        <tbody>
          {/* Gate G15: WHERE padding failed — the smallest island/border gaps. */}
          <tr>
            <td>{t('generate.cr.minIslandGap')}</td>
            <td>{fmtNum(c.min_island_gap_px, 2)}</td>
          </tr>
          <tr>
            <td>{t('generate.cr.minBorderGap')}</td>
            <td>{fmtNum(c.min_border_gap_px, 2)}</td>
          </tr>
          {(c.checks ?? []).map((ck) => (
            <tr key={ck.name} className={ck.passed ? '' : 'bad'}>
              <td>{ck.name}</td>
              <td>
                <span className={`tag ${ck.passed ? 'ok' : 'bad'}`}>
                  {ck.passed ? t('generate.check.pass') : t('generate.check.fail')}
                </span>{' '}
                {fmtNum(ck.value)} / {fmtNum(ck.limit)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

// --- Game gates (T14; gate G15) --------------------------------------------

/** Pass/fail/not-evaluable tag for one gate block. */
function GateTag(props: { passed?: boolean | null; valid?: boolean | null }): JSX.Element {
  const t = useT();
  if (props.valid === false) return <span className="tag unknown">{t('generate.gate.invalid')}</span>;
  if (props.passed === true) return <span className="tag ok">{t('generate.check.pass')}</span>;
  if (props.passed === false) return <span className="tag bad">{t('generate.check.fail')}</span>;
  return <span className="tag unknown">{t('generate.gate.na')}</span>;
}

/** The combined game-quality verdict shown next to the section header. */
function GameGateHeaderTag(props: { summary: UvGenerateRunView['summary'] }): JSX.Element {
  const t = useT();
  const passed = props.summary?.quality_report_passed;
  if (passed === true) return <span className="tag ok">{t('generate.gate.passed')}</span>;
  if (passed === false) return <span className="tag bad">{t('generate.gate.failed')}</span>;
  return <span className="tag unknown">{t('generate.gate.na')}</span>;
}

function idList(ids: number[] | null | undefined, limit = 12): string {
  const list = ids ?? [];
  if (list.length === 0) return '—';
  const head = list.slice(0, limit).join(', ');
  return list.length > limit ? `${head}, …(${list.length})` : head;
}

/**
 * Every game gate with its pass/fail tag and the evidence a reviewer needs to
 * act: which islands are tiny/sliver, which break texel density, how packing
 * compares to its floor, and what merge-back did (gate G15).
 */
function GameGatesSection(props: { summary: UvGenerateRunView['summary'] }): JSX.Element {
  const t = useT();
  const s = props.summary;
  const frag = s?.fragmentation ?? null;
  const td = s?.texel_density ?? null;
  const pk = s?.packing ?? null;
  const sh = s?.shading ?? null;
  const mb = s?.merge_back ?? null;
  const dist = s?.distortion_v2 ?? null;
  const corr = s?.correctness ?? null;
  const cat = s?.catastrophic ?? null;
  const rp = s?.repair ?? null;
  if (!frag && !td && !pk && !sh && !mb && !dist && !corr && !cat && !rp) {
    return <div className="muted">{t('generate.noGameGates')}</div>;
  }
  const fm = frag?.metrics ?? {};
  return (
    <table className="metrics gamegates">
      <tbody>
        {/* Catastrophic distortion FIRST (gate CG13): the un-shippable class
            outranks every other gate, so a reviewer sees it before anything. */}
        <tr>
          <td>{t('generate.gg.catastrophic')}</td>
          <td>
            <GateTag passed={cat?.passed} valid={cat?.valid} />{' '}
            <span className="muted small">
              {t('generate.cat.badTriangles')} {cat?.bad_triangle_count ?? '—'} ·{' '}
              {t('generate.cat.badRegions')} {cat?.bad_region_count ?? '—'} ·{' '}
              {t('generate.cat.badArea')} {fmtPct(cat?.bad_area_fraction)}
            </span>
            {cat && (
              <div className="muted small">
                {t('generate.cat.maxAniso')} {fmtNum(cat.max_anisotropy, 2)} ·{' '}
                {t('generate.cat.maxAspect')} {fmtNum(cat.max_uv_triangle_aspect, 2)} ·{' '}
                {t('generate.cat.nearCollapse')} {cat.near_collapse_count ?? '—'}
              </div>
            )}
          </td>
        </tr>
        <tr>
          <td>{t('generate.gg.repair')}</td>
          <td>
            <span className="muted small">
              {rp
                ? t('generate.rp.summary', {
                    rounds: rp.rounds ?? 0,
                    reunwrap: rp.reunwrap_accepted ?? 0,
                    relief: rp.relief_accepted ?? 0,
                    rejected: rp.rejected ?? 0,
                    before: rp.bad_triangles_before ?? '—',
                    after: rp.bad_triangles_after ?? '—',
                    reason: rp.reason ?? '—',
                  })
                : t('generate.rp.none')}
            </span>
          </td>
        </tr>
        {/* distortion — the existing v2 block, restated as a gate row. */}
        <tr>
          <td>{t('generate.gg.distortion')}</td>
          <td>
            <GateTag
              passed={dist ? (dist.global?.exceed_area_fraction ?? 0) <= 0 : null}
              valid={dist?.valid}
            />{' '}
            <span className="muted small">
              {t('generate.q.anisoP95')} {fmtNum(dist?.global?.anisotropy_p95)}
            </span>
          </td>
        </tr>
        <tr>
          <td>{t('generate.gg.correctness')}</td>
          <td>
            <GateTag passed={corr?.passed} />{' '}
            <span className="muted small">
              {t('generate.cr.minIslandGap')} {fmtNum(corr?.min_island_gap_px, 2)} ·{' '}
              {t('generate.cr.minBorderGap')} {fmtNum(corr?.min_border_gap_px, 2)}
            </span>
          </td>
        </tr>
        <tr>
          <td>{t('generate.gg.fragmentation')}</td>
          <td>
            <GateTag passed={frag?.passed} valid={frag?.valid} />{' '}
            <span className="muted small">
              {t('generate.gg.tiny')} {fm.tiny_island_count ?? '—'} · {t('generate.gg.sliver')}{' '}
              {fm.sliver_island_count ?? '—'} · {t('generate.gg.oneTwoFace')}{' '}
              {fm.one_two_face_island_count ?? '—'} · {t('generate.gg.aspectP95')}{' '}
              {fmtNum(fm.island_aspect_p95, 2)}
            </span>
            {frag && (
              <div className="muted small">
                {t('generate.gg.tinyIds')} {idList(frag.tiny_island_ids)} ·{' '}
                {t('generate.gg.sliverIds')} {idList(frag.sliver_island_ids)} ·{' '}
                {t('generate.gg.exemptIds')} {idList(frag.exempt_islands)}
              </div>
            )}
          </td>
        </tr>
        <tr>
          <td>{t('generate.gg.texelDensity')}</td>
          <td>
            <GateTag passed={td?.passed} valid={td?.valid} />{' '}
            <span className="muted small">
              cv {fmtNum(td?.density_cv, 4)} · {t('generate.gg.outliers')} {td?.outlier_count ?? '—'}
            </span>
            {td && (
              <div className="muted small">
                {t('generate.gg.outlierIds')} {idList(td.outlier_island_ids)}
              </div>
            )}
          </td>
        </tr>
        <tr>
          <td>{t('generate.gg.packing')}</td>
          <td>
            <GateTag passed={pk?.passed} />{' '}
            <span className="muted small">
              {fmtNum(pk?.efficiency)} / {fmtNum(pk?.limit)}
              {pk?.advisory ? ` · ${t('generate.gg.advisory')}` : ''}
            </span>
          </td>
        </tr>
        <tr>
          <td>{t('generate.gg.shading')}</td>
          <td>
            <GateTag passed={sh?.passed} valid={sh?.valid} />{' '}
            <span className="muted small">
              {sh?.policy ?? '—'}
              {sh?.failures?.length ? ` · ${sh.failures.join(', ')}` : ''}
            </span>
          </td>
        </tr>
        <tr>
          <td>{t('generate.gg.mergeBack')}</td>
          <td>
            <GateTag passed={mb ? mb.complete : null} />{' '}
            <span className="muted small">
              {t('generate.mb.summary', {
                trials: mb?.trials ?? 0,
                accepted: mb?.accepted ?? 0,
                before: mb?.island_count_before ?? '—',
                after: mb?.island_count_after ?? '—',
                reason: mb?.reason ?? '—',
              })}
            </span>
          </td>
        </tr>
      </tbody>
    </table>
  );
}

/** Why the search stopped and what it spent (gate G5). */
function TerminationSection(props: { summary: UvGenerateRunView['summary'] }): JSX.Element {
  const t = useT();
  const tm = props.summary?.termination ?? null;
  if (!tm) return <div className="muted">{t('generate.noTermination')}</div>;
  return (
    <table className="metrics">
      <tbody>
        <tr><td>{t('generate.term.reason')}</td><td>{tm.reason}</td></tr>
        <tr><td>{t('generate.term.iterations')}</td><td>{tm.iterations}</td></tr>
        <tr><td>{t('generate.term.candidates')}</td><td>{tm.candidates_evaluated}</td></tr>
        <tr><td>{t('generate.term.elapsed')}</td><td>{fmtNum(tm.elapsed_s, 2)}</td></tr>
      </tbody>
    </table>
  );
}

/** Seam length by origin — the auxiliary-cut budget (work plan §5). */
function SeamLengthSection(props: { summary: UvGenerateRunView['summary'] }): JSX.Element {
  const t = useT();
  const s = props.summary?.seam_length ?? null;
  if (!s) return <div className="muted">{t('generate.noSeamLength')}</div>;
  return (
    <table className="metrics">
      <tbody>
        <tr><td>{t('generate.sl.mandatory')}</td><td>{fmtNum(s.mandatory, 3)}</td></tr>
        <tr><td>{t('generate.sl.user')}</td><td>{fmtNum(s.user, 3)}</td></tr>
        <tr><td>{t('generate.sl.auxiliary')}</td><td>{fmtNum(s.auxiliary, 3)}</td></tr>
        <tr><td>{t('generate.sl.total')}</td><td>{fmtNum(s.total, 3)}</td></tr>
        <tr><td>{t('generate.sl.auxNormalized')}</td><td>{fmtNum(s.auxiliary_normalized, 4)}</td></tr>
      </tbody>
    </table>
  );
}

/** Locked/protected constraint accounting + whether saved feedback applied. */
function ConstraintsSection(props: { summary: UvGenerateRunView['summary'] }): JSX.Element {
  const t = useT();
  const ac = props.summary?.auto_constraints ?? null;
  const fa = props.summary?.feedback_applied ?? null;
  if (!ac && !fa) return <div className="muted">{t('generate.noConstraints')}</div>;
  return (
    <>
      {ac && (
        <table className="metrics">
          <tbody>
            <tr><td>{t('generate.cn.locked')}</td><td>{ac.locked_seam_count ?? 0}</td></tr>
            <tr><td>{t('generate.cn.protected')}</td><td>{ac.protected_count ?? 0}</td></tr>
            <tr className={(ac.conflict_count ?? 0) > 0 ? 'bad' : ''}>
              <td>{t('generate.cn.conflicts')}</td><td>{ac.conflict_count ?? 0}</td>
            </tr>
          </tbody>
        </table>
      )}
      {fa && (
        <div className="small">
          <span className={`tag ${fa.applied ? 'ok' : 'unknown'}`}>
            {fa.applied ? t('generate.fb.applied') : t('generate.fb.notApplied')}
          </span>{' '}
          <span className="muted">{fa.reason}</span>
        </div>
      )}
    </>
  );
}

/**
 * Artist sign-off (gates G6/G7): kept apart from the solver verdict, a rejection
 * REQUIRES a reason, and the stored verdict always names the run it applies to.
 */
function ArtistReviewSection(props: {
  project: Project | null;
  runId: string | null;
  reviewer: string;
  setReviewer: (v: string) => void;
  rejectReason: string;
  setRejectReason: (v: string) => void;
  onSetApproval: (approved: boolean) => void;
}): JSX.Element {
  const t = useT();
  const ap = props.project?.uv_artist_approval ?? null;
  const isLatest =
    !!props.runId && props.runId === (props.project?.latest_uv_generate_run_id ?? null);
  return (
    <div className="runopts">
      {!isLatest && props.runId && (
        <div className="muted small">{t('generate.reviewingRun', { id: props.runId })}</div>
      )}
      <label className="optrow">
        <span>{t('generate.reviewer')}</span>
        <input
          type="text"
          value={props.reviewer}
          onChange={(e) => props.setReviewer(e.target.value)}
        />
      </label>
      <label className="optrow">
        <span>{t('generate.rejectReason')}</span>
        <input
          type="text"
          value={props.rejectReason}
          onChange={(e) => props.setRejectReason(e.target.value)}
        />
      </label>
      <div className="markrow">
        <button disabled={!props.runId} onClick={() => props.onSetApproval(true)}>
          {t('generate.approve')}
        </button>
        <button disabled={!props.runId} onClick={() => props.onSetApproval(false)}>
          {t('generate.reject')}
        </button>
      </div>
      {ap ? (
        <table className="metrics">
          <tbody>
            <tr>
              <td>{t('generate.ap.verdict')}</td>
              <td>
                <span className={`tag ${ap.approved ? 'ok' : 'bad'}`}>
                  {ap.approved ? t('generate.ap.approved') : t('generate.ap.rejected')}
                </span>
              </td>
            </tr>
            <tr><td>{t('generate.ap.runId')}</td><td><code className="small">{ap.run_id ?? '—'}</code></td></tr>
            <tr><td>{t('generate.ap.reviewer')}</td><td>{ap.reviewer ?? '—'}</td></tr>
            <tr><td>{t('generate.ap.reason')}</td><td>{ap.reason ?? '—'}</td></tr>
            <tr><td>{t('generate.ap.at')}</td><td className="small">{ap.approved_at ?? '—'}</td></tr>
          </tbody>
        </table>
      ) : (
        <div className="muted small">{t('generate.noApproval')}</div>
      )}
    </div>
  );
}

/**
 * Reviewer constraints saved against the mesh fingerprint (gate G7): locked /
 * protected / preferred edge ids, the front axis and free-form notes.
 */
function FeedbackSection(props: {
  form: FeedbackForm;
  setForm: React.Dispatch<React.SetStateAction<FeedbackForm>>;
  saved: UvFeedback | null;
  onSave: () => void;
  selectedEdgeId: number | null;
  addSelectedEdgeTo: (field: keyof Omit<FeedbackForm, 'front_axis' | 'notes'>) => void;
}): JSX.Element {
  const t = useT();
  const { form, setForm } = props;
  const fields: { key: keyof Omit<FeedbackForm, 'front_axis' | 'notes'>; label: TKey }[] = [
    { key: 'locked', label: 'generate.fb.locked' },
    { key: 'protectedEdges', label: 'generate.fb.protected' },
    { key: 'preferred', label: 'generate.fb.preferred' },
  ];
  return (
    <div className="runopts">
      {fields.map((f) => (
        <div key={f.key} className="field">
          <span className="small">{t(f.label)}</span>
          <input
            type="text"
            value={form[f.key]}
            placeholder={t('generate.fb.idsPlaceholder')}
            onChange={(e) => setForm((s) => ({ ...s, [f.key]: e.target.value }))}
          />
          <button
            className="small"
            disabled={props.selectedEdgeId === null}
            onClick={() => props.addSelectedEdgeTo(f.key)}
          >
            {t('generate.fb.addSelected', { id: props.selectedEdgeId ?? '—' })}
          </button>
        </div>
      ))}
      <label className="optrow">
        <span>{t('generate.fb.frontAxis')}</span>
        <select
          value={form.front_axis}
          onChange={(e) => setForm((s) => ({ ...s, front_axis: e.target.value }))}
        >
          <option value="">{t('generate.fb.axisUnset')}</option>
          {FRONT_AXES.map((a) => (
            <option key={a} value={a}>{a}</option>
          ))}
        </select>
      </label>
      <div className="field">
        <span className="small">{t('generate.fb.notes')}</span>
        <input
          type="text"
          value={form.notes}
          onChange={(e) => setForm((s) => ({ ...s, notes: e.target.value }))}
        />
      </div>
      <button onClick={props.onSave}>{t('generate.fb.save')}</button>
      {props.saved && (
        <div className="muted small">
          {t('generate.fb.savedAt', {
            at: props.saved.updated_at,
            fp: (props.saved.mesh_fingerprint ?? '—').slice(0, 12),
          })}
        </div>
      )}
    </div>
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

/** Budget fields where an EMPTY box means "use the quality profile's value" —
 *  never a silent 0 (work plan §3; gate G6). */
const BUDGET_FIELDS: {
  key: 'max_iterations' | 'max_candidates_per_round' | 'time_budget_s' | 'island_cap';
  label: TKey;
}[] = [
  { key: 'max_iterations', label: 'generate.opt.maxIterations' },
  { key: 'max_candidates_per_round', label: 'generate.opt.maxCandidatesPerRound' },
  { key: 'time_budget_s', label: 'generate.opt.timeBudget' },
  { key: 'island_cap', label: 'generate.opt.islandCap' },
];

const STRICT_FLAG_LABELS: Record<string, TKey> = {
  auto_refine_user_seams: 'generate.flag.auto_refine_user_seams',
  repair_user_seams: 'generate.flag.repair_user_seams',
  enforce_user_mandatory: 'generate.flag.enforce_user_mandatory',
  gate_user_mandatory: 'generate.flag.gate_user_mandatory',
};

function RunOptions(props: {
  options: GenerateUvOptions;
  setOptions: (o: GenerateUvOptions) => void;
  mode: UvGenerateMode;
  summary: UvGenerateRunView['summary'];
}): JSX.Element {
  const t = useT();
  const { options, setOptions, mode } = props;
  // Strict flags are locked in preserve mode: flipping one there is exactly the
  // contradiction `validateModeRequest` rejects (work plan §3).
  const strictLocked = mode === UvGenerateModeValues.PreserveExisting;
  const profileId =
    props.summary?.quality_profile?.profile_id ?? options.quality_profile ?? '—';
  return (
    <div className="runopts">
      <div className="optrow">
        <span>{t('generate.opt.qualityProfile')}</span>
        <code className="small">{profileId}</code>
      </div>
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
      <label className="optrow">
        <span>{t('generate.opt.seed')}</span>
        <input
          type="number"
          min={0}
          value={options.seed ?? 0}
          onChange={(e) => setOptions({ ...options, seed: Number(e.target.value) || 0 })}
        />
      </label>
      <label className="optrow">
        <span>{t('generate.opt.marginPx')}</span>
        <input
          type="number"
          min={0}
          value={options.margin_px ?? 4}
          onChange={(e) => setOptions({ ...options, margin_px: Number(e.target.value) || 0 })}
        />
      </label>
      {BUDGET_FIELDS.map((f) => (
        <label key={f.key} className="optrow">
          <span>{t(f.label)}</span>
          <input
            type="number"
            min={0}
            value={options[f.key] ?? ''}
            placeholder={t('generate.opt.profileDefault')}
            onChange={(e) =>
              setOptions({
                ...options,
                [f.key]: e.target.value === '' ? null : Number(e.target.value),
              })
            }
          />
        </label>
      ))}
      {STRICT_FLAGS.map((flag) => (
        <label key={flag} className="optrow">
          <span>{t(STRICT_FLAG_LABELS[flag])}</span>
          <input
            type="checkbox"
            disabled={strictLocked}
            checked={!!options[flag]}
            onChange={(e) => setOptions({ ...options, [flag]: e.target.checked })}
          />
        </label>
      ))}
      <div className="muted small">
        {strictLocked ? t('generate.strictHint') : t('generate.autoHint')}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Bottom: run status / raw reports (summary, p5_gate, seam_report) / logs
// ---------------------------------------------------------------------------
type BottomTab = 'summary' | 'candidate' | 'candidates' | 'p5_gate' | 'seam_report' | 'logs';

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
          <button className={tab === 'candidates' ? 'active' : ''} onClick={() => setTab('candidates')}>{t('generate.tab.candidateHistory')}</button>
          <button className={tab === 'p5_gate' ? 'active' : ''} onClick={() => setTab('p5_gate')}>p5_gate</button>
          <button className={tab === 'seam_report' ? 'active' : ''} onClick={() => setTab('seam_report')}>seam_report</button>
          <button className={tab === 'logs' ? 'active' : ''} onClick={() => setTab('logs')}>{t('common.tab.logs')}</button>
        </nav>
        <div className="tabbody">
          {!rv && <div className="muted">{t('generate.noRunSelected')}</div>}
          {rv && tab === 'summary' && <Json data={rv.summary} empty={t('common.noSummary')} />}
          {rv && tab === 'candidate' && <Json data={rv.candidate_summary} empty={t('generate.noCandSummary')} />}
          {rv && tab === 'candidates' && <CandidateHistoryTable rows={rv.candidate_history} />}
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

/** Per-round refinement log: what was cut, what it measured, was it kept (G5/G7). */
function CandidateHistoryTable(props: { rows: CandidateHistoryEntry[] | null }): JSX.Element {
  const t = useT();
  const rows = props.rows ?? [];
  if (rows.length === 0) return <div className="muted">{t('generate.noCandidateHistory')}</div>;
  return (
    <div className="candtable-wrap">
      <table className="candtable">
        <thead>
          <tr>
            <th>{t('generate.ch.round')}</th>
            <th>{t('generate.ch.kind')}</th>
            <th>{t('generate.ch.target')}</th>
            <th>{t('generate.ch.beforeAfter')}</th>
            <th>{t('generate.ch.accepted')}</th>
            <th>{t('generate.ch.reason')}</th>
            <th>{t('generate.ch.improvement')}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className={r.accepted ? '' : 'rejected'}>
              <td>{r.round}</td>
              <td>{r.kind}</td>
              <td>
                {r.target_island ?? '—'}
                {r.target_metric ? ` · ${r.target_metric}` : ''}
              </td>
              <td>{fmtNum(r.before)} → {fmtNum(r.after)}</td>
              <td>
                <span className={`tag ${r.accepted ? 'ok' : 'unknown'}`}>
                  {r.accepted ? t('common.yes') : t('common.no')}
                </span>
              </td>
              <td>{r.reason}</td>
              <td>{fmtNum(r.improvement_ratio)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
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

/** A 0..1 fraction as a percent — how much UV area is catastrophically bad. */
function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return '—';
  return `${(Number(v) * 100).toFixed(digits)}%`;
}

function fmtMetric(key: keyof GenerateMetrics, v: number | boolean | null | undefined): string {
  if (v === null || v === undefined) return '—';
  if (key === 'uv_bounds_ok') return v ? 'yes' : 'no';
  if (key === 'island_count') return String(v);
  return Number(v).toFixed(4);
}
