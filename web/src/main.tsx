import { useCallback, useEffect, useRef, useState } from 'react';
import 'maplibre-gl/dist/maplibre-gl.css';
import { createRoot } from 'react-dom/client';
import { Activity, ArrowRight, BookOpen, BrainCircuit, CheckCircle2, CloudSun, Compass, Database, ExternalLink, Layers3, Menu, MessageCircle, Plus, RefreshCw, Send, Settings2, ShieldCheck, Sun, Moon, Plane, Lock, AlertCircle } from 'lucide-react';
import { apiJSON, streamChat, setAuthToken } from './api';
import type { Health, Skill, SkillResult, Provider, ModelProfile } from './api';
import { FormattedAnswer } from './FormattedAnswer';
import { TopbarTripControls, CurrentTripTitle } from './TopbarTripControls';
import { itineraryPreviewText } from './versionPreview';
import { BrandTagline, useBrandTagline } from './brandTagline';
import './style.css';

type Page = 'chat' | 'skills' | 'memory' | 'knowledge' | 'settings';
type TripSnapshot = { version: number; itinerary: { title: string; destination: string; days: { day: number; title: string }[] } };
type TripChange = { action: string; changed_days: number[]; changed_preferences?: string[] };
type DraftHistoryEntry = { draft_version: number; base_version: number; updated_at: string };
type SavedSnapshotEntry = { id: number; source_status: string; source_version: number; saved_at: string };
type WorkingDraft = { draft_version: number; base_version: number; status: 'draft'; itinerary: TripSnapshot['itinerary']; issues: { code?: string; detail?: string }[] };
type ChatMessage = { role: 'user' | 'assistant'; content: string; trip_change?: TripChange | null; working_draft?: WorkingDraft | null; results?: SkillResult[]; elapsed_ms?: number; planner_mode?: string; execution_trace?: { name?: string; status: string; elapsed_ms?: number; dependencies?: string[]; input_bindings?: Record<string, string>; recovery_for?: string; recovered_by?: string }[]; planner_limitations?: string[] };
type JourneyUI = { messages: ChatMessage[]; historyLoaded: boolean; loading: boolean; stage: string; active: string[]; activeTrip: TripSnapshot | null; workingDraft: WorkingDraft | null; draftHistory: DraftHistoryEntry[]; savedSnapshots: SavedSnapshotEntry[]; error: string };
const emptyJourney = (): JourneyUI => ({ messages: [], historyLoaded: false, loading: false, stage: '', active: [], activeTrip: null, workingDraft: null, draftHistory: [], savedSnapshots: [], error: '' });
const USER = 'local-demo';
const examples = [
  'Plan a relaxing three-day city trip in Columbus with museums and local food.',
  'Design a five-day Kyoto itinerary focused on culture and local experiences.',
  'Plan a weekend with scenic walks and realistic rainy-day alternatives.',
  'Create an itinerary that balances attractions, meals, time, and budget.'
];

function RouteMap({ geometry }: { geometry: unknown }) {
  const anchor = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!anchor.current || !geometry) return;
    let mapInstance: import('maplibre-gl').Map | undefined;
    let cancelled = false;
    (async () => {
      const maplibre = (await import('maplibre-gl')).default;
      if (cancelled || !anchor.current) return;
      const geo = geometry as { type: string; coordinates: number[][] };
      if (geo.type !== 'LineString' || !geo.coordinates?.length) return;
      const first = geo.coordinates[0];
      mapInstance = new maplibre.Map({ container: anchor.current, center: [first[0], first[1]], zoom: 12, style: { version: 8, sources: { osm: { type: 'raster', tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'], tileSize: 256, attribution: '© OpenStreetMap contributors' } }, layers: [{ id: 'osm', type: 'raster', source: 'osm' }] } });
      mapInstance.addControl(new maplibre.NavigationControl(), 'top-right');
      mapInstance.on('load', () => {
        if (!mapInstance) return;
        mapInstance.addSource('route', { type: 'geojson', data: { type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: geo.coordinates } } });
        mapInstance.addLayer({ id: 'route', type: 'line', source: 'route', paint: { 'line-color': '#22b991', 'line-width': 5 } });
        const bounds = new maplibre.LngLatBounds(); geo.coordinates.forEach(p => bounds.extend([p[0], p[1]])); mapInstance.fitBounds(bounds, { padding: 38, maxZoom: 15 });
      });
    })().catch(() => { });
    return () => { cancelled = true; mapInstance?.remove(); };
  }, [geometry]);
  return <div className="route-map" ref={anchor} aria-label="Verified route map" />;
}
function SkillDetails({ results }: { results: SkillResult[] }) {
  return <details className="skill-details"><summary>Execution details · {results.length} skills</summary><div className="skill-detail-list">{results.map((r, i) => <article key={`${r.name}-${i}`} className="skill-result">
    <div className="result-heading"><span>{r.name}</span><small>{r.status}</small></div><div className="result-body">{r.text}</div>
    {r.data?.geometry ? <RouteMap geometry={r.data.geometry} /> : null}
    {r.limitations?.length > 0 && <div className="limitations">{r.limitations.map((l, j) => <p key={j}>{l}</p>)}</div>}
    {r.sources?.length > 0 && <div className="source-list">{r.sources.map((s, j) => s.url ? <a href={s.url} key={j} target="_blank" rel="noopener noreferrer"><ExternalLink size={12} />{s.provider} · {s.mode}</a> : <span key={j}>{s.provider}</span>)}</div>}
  </article>)}</div></details>
}
function App() {
  const brandTagline = useBrandTagline();
  const [historyPreview, setHistoryPreview] = useState<{ journey: string; label: string; content: string } | null>(null);
  const [page, setPage] = useState<Page>('chat'); const [collapsed, setCollapsed] = useState(false); const [theme, setTheme] = useState<'dark' | 'light'>('dark');
  const [health, setHealth] = useState<Health | null>(null); const [skills, setSkills] = useState<Skill[]>([]); const [sessions, setSessions] = useState<{ session_id: string; preview: string }[]>([]);
  const [sessionId, setSessionId] = useState('session-' + crypto.randomUUID().slice(0, 12));
  const sessionRef = useRef(sessionId);
  const pendingSessions = useRef(new Set<string>());
  const abortControllers = useRef(new Map<string, AbortController>());
  const [journeys, setJourneys] = useState<Record<string, JourneyUI>>({});
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const input = drafts[sessionId] ?? '';
  const setInput = (value: string) => setDrafts(previous => ({ ...previous, [sessionRef.current]: value }));
  const [error, setError] = useState('');
  const selected = journeys[sessionId];
  const messages = selected?.messages ?? [];
  const loading = selected?.loading ?? false;
  const active = selected?.active ?? [];
  const planningStage = selected?.stage ?? '';
  const activeTrip = selected?.activeTrip ?? null;
  const workingDraft = selected?.workingDraft ?? null;
  const draftHistory = selected?.draftHistory ?? [];
  const savedSnapshots = selected?.savedSnapshots ?? [];
  const journeyError = selected?.error ?? '';
  const updateJourney = (id: string, update: (state: JourneyUI) => JourneyUI) =>
    setJourneys(prev => ({ ...prev, [id]: update(prev[id] ?? emptyJourney()) }));
  const [prefs, setPrefs] = useState<Record<string, { value: string; scope: string }>>({}); const [docs, setDocs] = useState<{ id: number; title: string; size: number }[]>([]);
  const [prefCategory, setPrefCategory] = useState('travel_style'); const [prefValue, setPrefValue] = useState(''); const [consent, setConsent] = useState(false);
  const [token, setToken] = useState(''); const [skillFilter, setSkillFilter] = useState('all');
  const bottom = useRef<HTMLDivElement>(null);
  const load = useCallback(async () => {
    try {
      const [h, s, list] = await Promise.all([apiJSON<Health>('/api/health'), apiJSON<{ skills: Skill[] }>('/api/skills'), apiJSON<{ sessions: { session_id: string; preview: string }[] }>('/api/sessions?user_id=' + USER)]);
      setHealth(h); setSkills(s.skills); setSessions(list.sessions); setError('');
    } catch (e) { setError(e instanceof Error ? e.message : 'Backend unavailable.'); }
  }, []);
  useEffect(() => {
    let cancelled = false;
    const target = sessionId;
    if (!pendingSessions.current.has(target)) {
      apiJSON<{ active_itinerary: TripSnapshot | null; working_draft?: WorkingDraft | null; draft_history: DraftHistoryEntry[]; saved_snapshots: SavedSnapshotEntry[] }>('/api/trips/active?user_id=' + USER + '&session_id=' + encodeURIComponent(target))
        .then(data => {
          if (!cancelled && !pendingSessions.current.has(target)) {
            updateJourney(target, state => ({ ...state, activeTrip: data.active_itinerary, workingDraft: data.working_draft ?? null, draftHistory: data.draft_history ?? [], savedSnapshots: data.saved_snapshots ?? [] }));
          }
        }).catch(() => { });
    }
    return () => { cancelled = true; };
  }, [sessionId]);
  useEffect(() => { document.documentElement.setAttribute('data-theme', theme); }, [theme]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    let cancelled = false;
    const target = sessionId;
    if (page === 'memory') {
      setPrefs({});
      apiJSON<{ preferences: typeof prefs }>('/api/preferences?user_id=' + USER + '&session_id=' + encodeURIComponent(target))
        .then(d => { if (!cancelled && sessionRef.current === target) setPrefs(d.preferences); })
        .catch(() => { if (!cancelled) setError('Could not load saved preferences.'); });
    }
    if (page === 'knowledge') {
      apiJSON<{ documents: typeof docs }>('/api/knowledge?user_id=' + USER)
        .then(d => { if (!cancelled) setDocs(d.documents); })
        .catch(() => { if (!cancelled) setError('Could not load knowledge sources.'); });
    }
    return () => { cancelled = true; };
  }, [page, sessionId]);
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages, loading]);
  async function submit(override?: string) {
    const text = (override || input).trim();
    const target = sessionRef.current;
    if (!text || pendingSessions.current.has(target)) return;
    pendingSessions.current.add(target);
    const controller = new AbortController();
    abortControllers.current.set(target, controller);
    updateJourney(target, state => ({
      ...state, historyLoaded: true, messages: [...state.messages, { role: 'user', content: text }],
      loading: true, stage: 'router', active: [], error: ''
    }));
    setDrafts(previous => ({ ...previous, [target]: '' }));
    let completed = false;
    let receivedError = false;
    try {
      await streamChat({
        message: text, user_id: USER, session_id: target,
        context: {}
      }, (name, value) => {
        if (name === 'progress') updateJourney(target, state => ({ ...state, stage: (value as { stage?: string }).stage || state.stage }));
        if (name === 'plan') updateJourney(target, state => ({ ...state, active: (value as { skills: string[] }).skills || [] }));
        if (name === 'done') {
          const result = value as {
            answer: string; results: SkillResult[]; elapsed_ms: number; planner_mode: string;
            active_itinerary?: TripSnapshot | null; working_draft?: WorkingDraft | null; draft_history?: DraftHistoryEntry[]; saved_snapshots?: SavedSnapshotEntry[]; trip_change?: TripChange | null;
            model_issue?: string | null; model_diagnostic?: { stage?: string; code?: string; detail?: string } | null;
            execution_trace?: ChatMessage['execution_trace']; planner_limitations?: string[]
          };
          if (typeof result.answer !== 'string' || !result.answer.trim()) throw new Error('Backend returned an empty assistant response.');
          completed = true;
          const detail = result.model_diagnostic;
          const diagnostic = detail?.code ? `Model diagnostic: ${detail.code}${detail.stage ? ` (${detail.stage})` : ''}${detail.detail ? ` - ${detail.detail}` : ''}` :
            result.model_issue ? `Model diagnostic: ${result.model_issue}` : '';
          updateJourney(target, state => ({
            ...state, activeTrip: result.active_itinerary ?? null, workingDraft: result.working_draft ?? null, draftHistory: result.draft_history ?? state.draftHistory, savedSnapshots: result.saved_snapshots ?? state.savedSnapshots,
            error: diagnostic,
            messages: [...state.messages, {
              role: 'assistant', content: result.answer, results: result.results,
              elapsed_ms: result.elapsed_ms, planner_mode: result.planner_mode, trip_change: result.trip_change, working_draft: result.working_draft,
              execution_trace: result.execution_trace, planner_limitations: result.planner_limitations
            }]
          }));
          load();
        }
        if (name === 'error') {
          receivedError = true;
          updateJourney(target, state => ({ ...state, error: (value as { message?: string }).message || 'Backend request failed.' }));
        }
      }, controller.signal);
      if (!completed && !receivedError && !controller.signal.aborted)
        updateJourney(target, state => ({ ...state, error: 'Stream ended before completion. Review the backend logs.' }));
    } catch (e) {
      if (!controller.signal.aborted)
        updateJourney(target, state => ({ ...state, error: e instanceof Error ? e.message : 'Unexpected request error.' }));
    } finally {
      if (abortControllers.current.get(target) === controller) abortControllers.current.delete(target);
      pendingSessions.current.delete(target);
      updateJourney(target, state => ({ ...state, loading: false, stage: '', active: [] }));
    }
  }
  async function getTripVersion(kind: 'saved' | 'draft', version: number, target: string) {
    const url = kind === 'saved' ? `/api/trips/saved/${version}` : `/api/trips/draft/${version}`;
    return apiJSON<{ itinerary?: unknown }>(`${url}?user_id=${USER}&session_id=${encodeURIComponent(target)}`);
  }
  async function loadTripVersionTitle(kind: 'saved' | 'draft', version: number, target: string): Promise<string | null> {
    const data = await getTripVersion(kind, version, target);
    const trip = data.itinerary;
    if (!trip || typeof trip !== 'object' || !('title' in trip) || typeof trip.title !== 'string') return null;
    return trip.title.trim() || null;
  }
  async function previewTripVersion(kind: 'saved' | 'draft', version: number) {
    const target = sessionRef.current;
    try {
      const data = await getTripVersion(kind, version, target);
      if (sessionRef.current === target) setHistoryPreview({ journey: target, label: kind === 'saved' ? `Saved copy #${version}` : `Working draft #${version}`, content: itineraryPreviewText(data.itinerary) });
    } catch (error) {
      updateJourney(target, previous => ({ ...previous, error: error instanceof Error ? error.message : 'Could not load a saved version.' }));
    }
  }
  async function saveTrip() {
    const target = sessionRef.current;
    const state = journeys[target];
    if (!state || pendingSessions.current.has(target) || state.loading || (!state.workingDraft && !state.activeTrip)) return;
    pendingSessions.current.add(target);
    updateJourney(target, current => ({ ...current, loading: true, stage: 'saving', error: '' }));
    try {
      const snapshot = await apiJSON<{ saved_id: number; source_status: string; source_version: number }>(
        '/api/trips/save', {
        method: 'POST',
        body: JSON.stringify({
          user_id: USER, session_id: target,
          expected_verified_version: state.activeTrip?.version ?? 0,
          expected_draft_version: state.workingDraft?.draft_version ?? 0
        }),
      });
      const provisional = snapshot.source_status === 'draft';
      updateJourney(target, prior => ({
        ...prior, error: '', savedSnapshots: [{ id: snapshot.saved_id, source_status: snapshot.source_status, source_version: snapshot.source_version, saved_at: new Date().toISOString() }, ...prior.savedSnapshots], messages: [...prior.messages, {
          role: 'assistant',
          content: `Saved itinerary snapshot #${snapshot.saved_id} (${provisional ? 'provisional working draft' : 'verified itinerary'}). This snapshot is retained while you continue editing.`,
        }]
      }));
    } catch (error) {
      updateJourney(target, prior => ({ ...prior, error: error instanceof Error ? error.message : 'Unable to save itinerary.' }));
    } finally {
      pendingSessions.current.delete(target);
      updateJourney(target, current => ({ ...current, loading: false, stage: '', active: [] }));
    }
  }
  function cancelCurrent() {
    const target = sessionRef.current;
    const controller = abortControllers.current.get(target);
    if (!controller || controller.signal.aborted) return;
    controller.abort();
    updateJourney(target, state => ({
      ...state, loading: false, stage: '', active: [],
      error: 'Request cancelled locally. A trip version already saved by the server may still be present; refresh to confirm.'
    }));
  }
  async function switchSession(id: string) {
    sessionRef.current = id;
    setSessionId(id);
    setPage('chat');
    if (pendingSessions.current.has(id) || journeys[id]?.historyLoaded) return;
    try {
      const x = await apiJSON<{ messages: ChatMessage[] }>('/api/history?user_id=' + USER + '&session_id=' + encodeURIComponent(id));
      if (!pendingSessions.current.has(id))
        setJourneys(prev => {
          const state = prev[id] ?? emptyJourney();
          return state.historyLoaded ? prev : { ...prev, [id]: { ...state, messages: x.messages, historyLoaded: true } };
        });
    } catch {
      updateJourney(id, state => ({ ...state, error: 'Could not load session history.' }));
    }
  }
  function newSession() {
    const id = 'session-' + crypto.randomUUID().slice(0, 12);
    sessionRef.current = id;
    setSessionId(id);
    setPage('chat');
  }
  async function savePreference() {
    if (!prefValue.trim() || !consent) return;
    const target = sessionRef.current;
    try {
      const out = await apiJSON<{ preferences: typeof prefs }>('/api/preferences', { method: 'POST', body: JSON.stringify({ user_id: USER, session_id: target, category: prefCategory, value: prefValue, scope: 'global', consent: true }) });
      if (sessionRef.current === target) setPrefs(out.preferences); setPrefValue(''); setConsent(false); setError('');
    } catch (e) { setError(e instanceof Error ? e.message : 'Failed to save preference.'); }
  }
  return <div className="app-shell">
    <aside className={'sidebar' + (collapsed ? ' collapsed' : '')}>
      <div className="identity"><div className="logo"><Compass size={23} /></div>{!collapsed && <div className="brand-copy"><strong className="brand-wordmark">PoreiaGo</strong><BrandTagline entry={brandTagline} placement="sidebar" /></div>}</div>
      <button className="primary action-new" onClick={newSession}><Plus size={18} />{!collapsed && 'New journey'}</button>
      {!collapsed && <div className="nav-label">WORKSPACE</div>}
      <nav>{([{ page: 'chat', label: 'Travel assistant', icon: MessageCircle }, { page: 'skills', label: 'Skill catalog', icon: Layers3 }, { page: 'memory', label: 'Memory & preferences', icon: BrainCircuit }, { page: 'knowledge', label: 'Knowledge sources', icon: BookOpen }, { page: 'settings', label: 'Models & providers', icon: Settings2 }] as const).map(item => <button key={item.page} onClick={() => setPage(item.page)} className={'nav-item' + (page === item.page ? ' selected' : '')}><item.icon size={18} />{!collapsed && <span>{item.label}</span>}{item.page === 'skills' && !collapsed && <b>{skills.length}</b>}</button>)}</nav>
      {!collapsed && <div className="session-list"><div className="nav-label">RECENT JOURNEYS</div>{sessions.slice(0, 12).map(s => <button className={'session-item' + (sessionId === s.session_id ? ' active' : '')} key={s.session_id} onClick={() => switchSession(s.session_id)} title={s.preview}>{s.preview}</button>)}</div>}
      <div className="side-bottom">
        <div className="side-status"><span className={'status-dot' + (health ? ' online' : '')} />{!collapsed && <span>{health ? 'System online' : 'Disconnected'}</span>}</div>
        <div className="side-status side-connection" aria-label={health ? 'Connected' : 'Offline'}><span className={'status-dot' + (health ? ' online' : '')} />{!collapsed && <span>{health ? 'Connected' : 'Offline'}</span>}</div>
      </div>
    </aside>
    <main className="main">
      <header className="topbar">
        <div className="top-left">
          <button className="icon-button" aria-label="Toggle sidebar" onClick={() => setCollapsed(!collapsed)}><Menu size={20} /></button>
          <span className="topbar-nav-trail"><span className="brand-wordmark brand-wordmark--compact">PoreiaGo</span><ArrowRight size={13} aria-hidden="true" /></span>
          {page === 'chat'
            ? (workingDraft || activeTrip)
              ? <CurrentTripTitle activeTrip={activeTrip} workingDraft={workingDraft} />
              : <span className="topbar-empty-trip">New journey</span>
            : <span className="topbar-page-summary" aria-label={`Current page: ${page}`}>
              <strong>{({ chat: 'Assistant', skills: 'Skill catalog', memory: 'Memory & preferences', knowledge: 'Knowledge sources', settings: 'Models & providers' })[page]}</strong>
            </span>}
        </div>
        <div className="right-tools">
          {page === 'chat' && <TopbarTripControls activeTrip={activeTrip} workingDraft={workingDraft} draftHistory={draftHistory} savedSnapshots={savedSnapshots} historyPreview={historyPreview} sessionId={sessionId} loading={loading} onSave={saveTrip} onPreview={previewTripVersion} onLoadTitle={loadTripVersionTitle} />}
          <button className="icon-button" onClick={load} aria-label="Refresh status"><RefreshCw size={17} /></button>
          <button className="icon-button" onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')} aria-label="Toggle theme">{theme === 'dark' ? <Sun size={19} /> : <Moon size={19} />}</button>
        </div>
      </header>
      {(error || (page === 'chat' && journeyError)) && <div className="error-message"><AlertCircle size={15} />{page === 'chat' && journeyError ? journeyError : error}<button onClick={() => { setError(''); updateJourney(sessionId, state => ({ ...state, error: '' })); }} aria-label="Dismiss error">×</button></div>}
      {page === 'chat' && <div className="conversation"><div className="message-list">{messages.length === 0 ? <div className="welcome"><div className="welcome-icon"><Plane size={29} /></div><div className="eyebrow">POREIAGO TRAVEL INTELLIGENCE</div><h1>Where should we go next?</h1><BrandTagline entry={brandTagline} placement="welcome" /><p>Describe the trip you have in mind in any language. PoreiaGo can plan directly with your selected model and consult available data providers when needed.</p>{(health?.models.some(m => m.role === 'router' && m.configured) && health?.models.some(m => m.role === 'reasoning' && m.configured)) ? <span className="model-ready">Model profiles configured (connectivity not verified)</span> : <span className="model-offline">Configure a compatible local or remote model to enable planning</span>}<div className="suggestions">{examples.map(ex => <button key={ex} onClick={() => submit(ex)}>{ex}<ArrowRight size={17} /></button>)}</div></div> : <div className="thread">{messages.map((m, i) => <section key={i} className={'message ' + m.role}><div className="message-content"><span className="speaker">{m.role === 'user' ? 'YOU' : 'POREIAGO'}</span>{m.role === 'user' ? <div className="user-text">{m.content}</div> : <><FormattedAnswer content={m.content} />{m.working_draft ? <div className="trip-change provisional"><AlertCircle size={14} />Working draft {m.working_draft.draft_version} · needs review · continue editing</div> : null}{m.trip_change ? <div className="trip-change"><CheckCircle2 size={14} />Trip version saved · {m.trip_change.action} · Days {m.trip_change.changed_days.join(', ') || 'preferences only'}</div> : null}{m.results?.length ? <SkillDetails results={m.results} /> : null}{m.execution_trace?.length ? <details className="skill-details"><summary>Execution trace · {m.planner_mode || 'catalog'}</summary><div className="skill-detail-list">{m.execution_trace.map((step, j) => <div className="info-row" key={j}><strong>{step.name || 'Runtime'} · {step.status}</strong><span>{step.elapsed_ms !== undefined ? `${step.elapsed_ms} ms` : 'Execution deadline'}{step.dependencies?.length ? ` · Depends on: ${step.dependencies.join(', ')}` : ''}{step.recovery_for ? ` · Alternative for: ${step.recovery_for}` : ''}{step.recovered_by ? ` · Recovered by: ${step.recovered_by}` : ''}{step.input_bindings && Object.keys(step.input_bindings).length ? ` · Data: ${Object.entries(step.input_bindings).map(([key, value]) => `${key} ← ${value}`).join(', ')}` : ''}</span></div>)}{m.planner_limitations?.map((note, j) => <p className="limitations" key={j}>{note}</p>)}</div></details> : null}{m.elapsed_ms !== undefined && <div className="duration">{(m.elapsed_ms / 1000).toFixed(1)}s</div>}</>}</div></section>)}{loading && <div className="thinking"><Activity size={18} className="spin" /><div><strong>Planning your journey</strong><span>{active.length ? `Invoking: ${active.join(', ')}` : planningStage === 'itinerary_generation' ? 'Generating and validating the itinerary...' : 'Selecting relevant capabilities...'}</span></div><button className="icon-button" onClick={cancelCurrent} aria-label="Cancel current request" title="Cancel this request">Cancel</button></div>}</div>}<div ref={bottom} /></div><div className="composer-area"><div className="composer"><textarea value={input} onChange={e => setInput(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); } }} placeholder="Describe your destination, interests, and travel preferences..." /><div className="composer-bottom"><span><ShieldCheck size={14} /> Source-aware recommendations</span><button className="send" onClick={() => submit()} disabled={loading || !input.trim()} aria-label="Send message"><Send size={17} /></button></div></div><p className="disclaimer">Route access, availability, prices, and travel requirements must be verified with their providers.</p></div></div>}
      {page === 'skills' && <div className="dashboard"><div className="page-heading"><div className="eyebrow">CAPABILITY REGISTRY</div><h1>Skills</h1><p>{skills.length} independent itinerary-planning Skills support the travel planner.</p></div><div className="stats-grid"><div className="stat-card"><Layers3 /><span>Total documented</span><strong>{skills.length}</strong></div><div className="stat-card"><CheckCircle2 /><span>Implemented</span><strong>{skills.filter(s => s.release_state === 'implemented').length}</strong></div><div className="stat-card"><Compass /><span>Planned</span><strong>{skills.filter(s => s.release_state === 'planned').length}</strong></div></div><div className="catalog-filter"><label htmlFor="filter">Release wave</label><select id="filter" value={skillFilter} onChange={e => setSkillFilter(e.target.value)}><option value="all">All waves</option>{['P0', 'P1', 'P2', 'P3'].map(w => <option key={w} value={w}>{w}</option>)}</select></div><div className="skill-grid">{skills.filter(s => skillFilter === 'all' || s.wave === skillFilter).map((s, i) => <article className="skill-card" key={s.id}><div className="skill-card-top"><span className="skill-number">{s.wave} · {i + 1}</span><span className="tag">{s.release_state}</span></div><h3>{s.id.split('-').map(w => w[0].toUpperCase() + w.slice(1)).join(' ')}</h3><p>{s.description}</p><span className="use-skill">{s.required_capabilities.length ? s.required_capabilities.join(' · ') : 'Local capability'}</span></article>)}</div></div>}
      {page === 'settings' && <div className="dashboard"><div className="page-heading"><div className="eyebrow">EXTENSIBLE INFRASTRUCTURE</div><h1>Models & providers</h1><p>Configure model roles in <code>config/models.json</code> and external capabilities through environment variables and provider adapters.</p></div><div className="provider-columns"><div className="panel"><div className="panel-title"><BrainCircuit /><h3>Model profiles</h3></div>{(health?.models || []).map((m: ModelProfile) => <div className="info-row" key={m.name}><div><strong>{m.name}</strong><div className="meta-sub">{m.role} · {m.model}</div></div><span>{m.provider === 'local-hf' ? 'On-demand download' : m.configured ? 'Configured' : 'Not configured'}</span></div>)}</div><div className="panel"><div className="panel-title"><CloudSun /><h3>Data providers</h3></div>{(health?.providers || []).map((p: Provider) => <div className="info-row" key={p.capability}><div><strong>{p.capability}</strong><div className="meta-sub">{p.provider}</div></div><span>{p.configured ? 'Enabled' : p.requires_key ? 'Requires API key' : 'Disabled'}</span></div>)}</div></div><div className="panel provider-note"><div className="panel-title"><Lock /><h3>Access token for remote deployments</h3></div><p>Leave this empty for localhost development. If the API server uses an access token, paste it here for the current browser session.</p><div className="setting-line"><input type="password" placeholder="Bearer token" value={token} onChange={e => setToken(e.target.value)} /><button className="primary" onClick={() => { setAuthToken(token); load(); }}>Apply</button></div></div></div>}
      {page === 'memory' && <div className="dashboard"><div className="page-heading"><div className="eyebrow">PRIVACY & CONTINUITY</div><h1>Memory & preferences</h1><p>Session history persists locally. Long-term preferences are only saved after explicit consent.</p></div><div className="memory-grid"><div className="panel"><div className="panel-title"><BrainCircuit /><h3>Saved preferences</h3></div>{Object.keys(prefs).length ? Object.entries(prefs).map(([k, v]) => <div key={k} className="info-row"><strong>{k}</strong><span>{v.value} · {v.scope}</span></div>) : <div className="empty-text">No persistent preferences saved.</div>}</div><div className="panel"><div className="panel-title"><Plus /><h3>Add preference</h3></div><div className="pref-form"><input value={prefCategory} onChange={e => setPrefCategory(e.target.value)} placeholder="Category" /><textarea rows={3} value={prefValue} onChange={e => setPrefValue(e.target.value)} placeholder="For example: I prefer a relaxed pace with two attractions per day." /><label className="consent"><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} />I explicitly consent to saving this preference.</label><button className="primary" disabled={!consent || !prefValue.trim()} onClick={savePreference}>Save preference</button></div></div></div></div>}
      {page === 'knowledge' && <div className="dashboard"><div className="page-heading"><div className="eyebrow">PERSONAL KNOWLEDGE</div><h1>Knowledge sources</h1><p>Text documents belong to the active local user. Lexical search works offline; Microsoft Multilingual E5 embeddings and Mixedbread reranking are optional model downloads.</p></div><div className="panel document-panel"><div className="panel-title"><Database /><h3>Stored documents</h3></div>{docs.length ? docs.map(d => <div className="document-row" key={d.id}><BookOpen size={17} />{d.title}<small>{d.size.toLocaleString()} characters</small></div>) : <div className="empty-text">No documents yet. Add documents through <code>POST /api/knowledge/documents</code>.</div>}</div></div>}
    </main>
  </div>;
}
createRoot(document.getElementById('root')!).render(<App />);
