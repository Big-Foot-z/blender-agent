/**
 * App shell (plan §8). MVP 1 opens on the UV Review workspace; the MVP 0
 * preparation flow stays available behind a workspace tab. Open Project / Import
 * Low-poly are shared in the top bar; each workspace owns its own panels + runs.
 */

import React, { useCallback, useEffect, useState } from 'react';
import type { AppSettings, Project } from '@shared/contracts';
import { PrepareWorkspace } from './PrepareWorkspace';
import { UvReviewWorkspace } from './uv-review/UvReviewWorkspace';
import { SeamEditorWorkspace } from './seam-editor/SeamEditorWorkspace';
import { UvGenerateWorkspace } from './uv-generate/UvGenerateWorkspace';
import { ExportWorkspace } from './export/ExportWorkspace';
import { useI18n, type Lang, type TFunc, type TKey } from './i18n';
import logoUrl from './assets/logo.png';

export type Banner = { kind: 'error' | 'info'; text: string } | null;
type Mode = 'review' | 'seam' | 'generate' | 'export' | 'prepare';
type FlowState = 'done' | 'ready' | 'blocked';

// NOTE: the Seam Editor step is intentionally HIDDEN from the flow nav for now — the
// editor's quality isn't presentable yet, and Generate + Optimize works without it (it
// derives a seam source from the existing UV layer selected in Review, see getFlowState's
// hasSeamSource). To restore, re-add the `seam` entry below; the render block + workspace
// are still wired in App, only the tab is hidden.
const FLOW: { mode: Mode; tabKey: TKey; prerequisiteKey: TKey }[] = [
  { mode: 'prepare', tabKey: 'app.tab.prepare', prerequisiteKey: 'app.flow.needProject' },
  { mode: 'review', tabKey: 'app.tab.review', prerequisiteKey: 'app.flow.needModel' },
  { mode: 'generate', tabKey: 'app.tab.generate', prerequisiteKey: 'app.flow.needSeams' },
  { mode: 'export', tabKey: 'app.tab.export', prerequisiteKey: 'app.flow.needGeneratedUv' },
];

export function App(): JSX.Element {
  const { t, lang, setLang } = useI18n();
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [project, setProject] = useState<Project | null>(null);
  const [mode, setMode] = useState<Mode>('review');
  const [busy, setBusy] = useState<string | null>(null);
  const [banner, setBanner] = useState<Banner>(null);

  useEffect(() => {
    window.api.settingsGet().then(setSettings);
  }, []);

  const guard = useCallback(async (label: string, fn: () => Promise<void>) => {
    setBusy(label);
    setBanner(null);
    try {
      await fn();
    } catch (err) {
      setBanner({ kind: 'error', text: String((err as Error)?.message ?? err) });
    } finally {
      setBusy(null);
    }
  }, []);

  const onImport = () =>
    guard(t('busy.importing'), async () => {
      const sourcePath = await window.api.pickFile();
      if (!sourcePath) return;
      const name = sourcePath.split(/[\\/]/).pop()?.replace(/\.[^.]+$/, '') ?? 'project';
      const p = await window.api.projectCreate({ name, sourcePath });
      setProject(p);
      setBanner({ kind: 'info', text: t('app.projectCreated', { dir: p.dir ?? '' }) });
    });

  const onOpen = () =>
    guard(t('busy.opening'), async () => {
      const dir = await window.api.pickProjectDir();
      if (!dir) return;
      const p = await window.api.projectOpen(dir);
      setProject(p);
    });

  const flowState = getFlowState(project);

  return (
    <div className="shell">
      <header className="topbar">
        <span className="brand">
          <img className="brand-logo" src={logoUrl} alt="" aria-hidden="true" />
          {t('app.brand')}
        </span>
        <button onClick={onImport}>{t('app.importLowpoly')}</button>
        <button onClick={onOpen}>{t('common.openProject')}</button>
        <nav className="modetabs" aria-label={t('app.flowLabel')}>
          {FLOW.map((step, index) => {
            const state = flowState[step.mode];
            const title =
              state === 'done'
                ? t('app.flow.done')
                : state === 'ready'
                  ? t('app.flow.ready')
                  : t(step.prerequisiteKey);
            return (
              <button
                key={step.mode}
                className={`${mode === step.mode ? 'active' : ''} ${state}`}
                title={title}
                onClick={() => setMode(step.mode)}
              >
                <span className="stepno">{index + 1}</span>
                <span className="steplabel">{t(step.tabKey)}</span>
                <span className="stepstate">{stateLabel(t, state)}</span>
              </button>
            );
          })}
        </nav>
        <span className="busy">{busy ? `${busy}…` : ''}</span>
        <LanguageSwitcher lang={lang} setLang={setLang} t={t} />
      </header>

      {settings && (
        <SettingsBar settings={settings} onChange={setSettings} t={t} setBanner={setBanner} />
      )}
      {banner && <div className={`banner ${banner.kind}`}>{banner.text}</div>}

      {mode === 'prepare' && (
        <PrepareWorkspace
          project={project}
          setProject={setProject}
          settings={settings}
          guard={guard}
          setBanner={setBanner}
        />
      )}
      {mode === 'review' && (
        <UvReviewWorkspace
          project={project}
          setProject={setProject}
          guard={guard}
          setBanner={setBanner}
        />
      )}
      {mode === 'seam' && (
        <SeamEditorWorkspace
          project={project}
          setProject={setProject}
          guard={guard}
          setBanner={setBanner}
        />
      )}
      {mode === 'generate' && (
        <UvGenerateWorkspace
          project={project}
          setProject={setProject}
          guard={guard}
          setBanner={setBanner}
        />
      )}
      {mode === 'export' && (
        <ExportWorkspace
          project={project}
          setProject={setProject}
          guard={guard}
          setBanner={setBanner}
        />
      )}
    </div>
  );
}

function getFlowState(project: Project | null): Record<Mode, FlowState> {
  const hasProject = !!project;
  const hasModel = !!(
    project?.working_model ||
    project?.working_model_fbx ||
    project?.source_model
  );
  const hasObject = !!project?.selected_object;
  const hasSeamSource = !!(
    project?.active_user_seam_spec ||
    project?.latest_derived_seam_spec ||
    project?.selected_uv_layer
  );
  const hasGeneratedUv = !!project?.selected_uv_model;

  return {
    prepare: project?.approved_lowpoly_run_id || project?.working_model ? 'done' : hasProject ? 'ready' : 'blocked',
    review: project?.latest_uv_review_run_id ? 'done' : hasModel ? 'ready' : 'blocked',
    seam: project?.active_user_seam_spec ? 'done' : hasObject ? 'ready' : 'blocked',
    generate: hasGeneratedUv ? 'done' : hasObject && hasSeamSource ? 'ready' : 'blocked',
    export: project?.latest_export_id ? 'done' : hasGeneratedUv ? 'ready' : 'blocked',
  };
}

function stateLabel(t: TFunc, state: FlowState): string {
  if (state === 'done') return t('app.flow.doneShort');
  if (state === 'ready') return t('app.flow.readyShort');
  return t('app.flow.blockedShort');
}

function SettingsBar(props: {
  settings: AppSettings;
  onChange: (s: AppSettings) => void;
  t: TFunc;
  setBanner: (b: Banner) => void;
}): JSX.Element {
  const { settings, t, setBanner } = props;
  const [installing, setInstalling] = useState(false);
  const setBlender = async () => {
    // Native file picker (OS-aware) instead of typing a long path — especially on
    // Windows. Cancelling leaves the current path unchanged.
    const picked = await window.api.pickBlender();
    if (!picked) return;
    const next = await window.api.settingsSet({ blenderPath: picked });
    props.onChange(next);
  };
  // Headless bridge-addon install into the configured Blender (bridge plan §4.2).
  const installBridge = async () => {
    setInstalling(true);
    try {
      await window.api.bridgeInstall();
      setBanner({ kind: 'info', text: t('app.bridgeInstalled') });
    } catch (err) {
      setBanner({ kind: 'error', text: String((err as Error)?.message ?? err) });
    } finally {
      setInstalling(false);
    }
  };
  const toggleAutoRefresh = async (on: boolean) => {
    const next = await window.api.settingsSet({ autoRefreshBlender: on });
    props.onChange(next);
  };
  return (
    <div className={`settingsbar ${settings.blenderPath ? '' : 'warn'}`}>
      <span>
        {t('app.settingsBlender')}:&nbsp;
        {settings.blenderPath ? (
          <code>{settings.blenderPath}</code>
        ) : (
          <strong>{t('app.settingsNotConfigured')}</strong>
        )}
      </span>
      <button onClick={setBlender}>{t('app.settingsSetPath')}</button>
      <button disabled={!settings.blenderPath || installing} onClick={installBridge}>
        {installing ? t('app.bridgeInstalling') : t('app.installBridge')}
      </button>
      <label className="check" title={t('app.autoRefreshHint')}>
        <input
          type="checkbox"
          checked={settings.autoRefreshBlender !== false}
          onChange={(e) => toggleAutoRefresh(e.target.checked)}
        />
        {t('app.autoRefreshBlender')}
      </label>
    </div>
  );
}

function LanguageSwitcher(props: { lang: Lang; setLang: (l: Lang) => void; t: TFunc }): JSX.Element {
  const { lang, setLang, t } = props;
  return (
    <select
      className="langselect"
      title={t('app.language')}
      aria-label={t('app.language')}
      value={lang}
      onChange={(e) => setLang(e.target.value as Lang)}
    >
      <option value="en">{t('app.langEnglish')}</option>
      <option value="ko">{t('app.langKorean')}</option>
    </select>
  );
}
