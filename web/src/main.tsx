import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import 'maplibre-gl/dist/maplibre-gl.css';
import { createRoot } from 'react-dom/client';
import {
  Activity,
  ArrowRight,
  BookOpen,
  BrainCircuit,
  CheckCircle2,
  Compass,
  ExternalLink,
  Layers3,
  Menu,
  MessageCircle,
  Plus,
  RefreshCw,
  Send,
  Settings2,
  ShieldCheck,
  Sun,
  Moon,
  AlertCircle,
} from 'lucide-react';
import { apiJSON, streamChat } from './lib/api';
import { TravelPreferences } from './features/preferences/TravelPreferences';
import { ModelsProvidersOverview } from './features/providers/ModelsProvidersOverview';
import { KnowledgeWorkspace } from './features/library/KnowledgeWorkspace';
import type { Health, Skill, SkillResult } from './lib/api';
import { ItineraryAnswer } from './features/itinerary/ItineraryAnswer';
import { TopbarTripControls, CurrentTripTitle } from './features/trips/TopbarTripControls';
import { itineraryPreviewText } from './features/trips/versionPreview';
import { isVisibleConversationMessage, savedSnapshotForCurrent } from './features/trips/savePresentation';
import { BrandTagline, useBrandTagline } from './features/branding/brandTagline';
import { ExportMenu } from './features/export/ExportMenu';
import {
  buildConversationHtml,
  downloadConversationHtml,
  printConversationPdf,
} from './features/export/exportConversation';
import { getOrCreateSessionId, rememberSessionId, createSessionId } from './lib/sessionIdentity';
import './styles/style.css';
import './features/branding/poreiagoBrand.css';
import './features/skills/skillsOverview.css';
import './styles/chatWide.css';
import './features/itinerary/itineraryFactLayout.css';

type Page = 'chat' | 'skills' | 'memory' | 'knowledge' | 'settings';
type TripSnapshot = {
  version: number;
  itinerary: { title: string; destination: string; days: { day: number; title: string }[] };
};
type TripChange = { action: string; changed_days: number[]; changed_preferences?: string[] };
type DraftHistoryEntry = { draft_version: number; base_version: number; updated_at: string };
type SavedSnapshotEntry = {
  id: number;
  source_status: string;
  source_version: number;
  saved_at: string;
};
type WorkingDraft = {
  draft_version: number;
  base_version: number;
  status: 'draft';
  itinerary: TripSnapshot['itinerary'];
  issues: { code?: string; detail?: string }[];
};

type ChatMessage = {
  role: 'user' | 'assistant';
  content: string;
  trip_change?: TripChange | null;
  working_draft?: WorkingDraft | null;
  results?: SkillResult[];
  elapsed_ms?: number;
  planner_mode?: string;
  execution_trace?: {
    name?: string;
    status: string;
    elapsed_ms?: number;
    dependencies?: string[];
    input_bindings?: Record<string, string>;
    recovery_for?: string;
    recovered_by?: string;
  }[];
  planner_limitations?: string[];
};
type JourneyUI = {
  messages: ChatMessage[];
  historyLoaded: boolean;
  loading: boolean;
  stage: string;
  active: string[];
  activeTrip: TripSnapshot | null;
  workingDraft: WorkingDraft | null;
  draftHistory: DraftHistoryEntry[];
  savedSnapshots: SavedSnapshotEntry[];
  error: string;
};
const emptyJourney = (): JourneyUI => ({
  messages: [],
  historyLoaded: false,
  loading: false,
  stage: '',
  active: [],
  activeTrip: null,
  workingDraft: null,
  draftHistory: [],
  savedSnapshots: [],
  error: '',
});
const USER = 'local-demo';
const examples = [
  'Plan a relaxing three-day city trip in Columbus with museums and local food.',
  'Design a five-day Kyoto itinerary focused on culture and local experiences.',
  'Plan a weekend with scenic walks and realistic rainy-day alternatives.',
  'Create an itinerary that balances attractions, meals, time, and budget.',
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
      mapInstance = new maplibre.Map({
        container: anchor.current,
        center: [first[0], first[1]],
        zoom: 12,
        style: {
          version: 8,
          sources: {
            osm: {
              type: 'raster',
              tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
              tileSize: 256,
              attribution: '© OpenStreetMap contributors',
            },
          },
          layers: [{ id: 'osm', type: 'raster', source: 'osm' }],
        },
      });
      mapInstance.addControl(new maplibre.NavigationControl(), 'top-right');
      mapInstance.on('load', () => {
        if (!mapInstance) return;
        mapInstance.addSource('route', {
          type: 'geojson',
          data: {
            type: 'Feature',
            properties: {},
            geometry: { type: 'LineString', coordinates: geo.coordinates },
          },
        });
        mapInstance.addLayer({
          id: 'route',
          type: 'line',
          source: 'route',
          paint: { 'line-color': '#22b991', 'line-width': 5 },
        });
        const bounds = new maplibre.LngLatBounds();
        geo.coordinates.forEach((p) => bounds.extend([p[0], p[1]]));
        mapInstance.fitBounds(bounds, { padding: 38, maxZoom: 15 });
      });
    })().catch(() => {});
    return () => {
      cancelled = true;
      mapInstance?.remove();
    };
  }, [geometry]);
  return <div className="route-map" ref={anchor} aria-label="Verified route map" />;
}
function SkillDetails({ results }: { results: SkillResult[] }) {
  return (
    <details className="skill-details">
      <summary>Execution details · {results.length} skills</summary>
      <div className="skill-detail-list">
        {results.map((r, i) => (
          <article key={`${r.name}-${i}`} className="skill-result">
            <div className="result-heading">
              <span>{r.name}</span>
              <small>{r.status}</small>
            </div>
            <div className="result-body">{r.text}</div>
            {r.data?.geometry ? <RouteMap geometry={r.data.geometry} /> : null}
            {r.limitations?.length > 0 && (
              <div className="limitations">
                {r.limitations.map((l, j) => (
                  <p key={j}>{l}</p>
                ))}
              </div>
            )}
            {r.sources?.length > 0 && (
              <div className="source-list">
                {r.sources.map((s, j) =>
                  s.url ? (
                    <a href={s.url} key={j} target="_blank" rel="noopener noreferrer">
                      <ExternalLink size={12} />
                      {s.provider} · {s.mode}
                    </a>
                  ) : (
                    <span key={j}>{s.provider}</span>
                  ),
                )}
              </div>
            )}
          </article>
        ))}
      </div>
    </details>
  );
}
const skillOverviewSections = [
  {
    title: 'Places & flavors',
    eyebrow: 'DISCOVER',
    icon: Compass,
    description:
      'Find memorable attractions, cultural experiences, restaurants, and the flavors that make a destination distinctive.',
    examples: 'Attractions · Culture · Restaurants · Local cuisine',
    skillIds: [
      'place-discovery',
      'cultural-experience-search',
      'restaurant-discovery',
      'culinary-specialty-discovery',
    ],
  },
  {
    title: 'Local life & nearby escapes',
    eyebrow: 'EXPLORE',
    icon: Layers3,
    description:
      'Look beyond the main sights with neighborhood discoveries, local events, seasonal experiences, and day trips.',
    examples: 'Neighborhoods · Events · Seasons · Day trips',
    skillIds: [
      'neighborhood-intelligence',
      'local-events-discovery',
      'seasonal-experience-intelligence',
      'day-trip-discovery',
    ],
  },
  {
    title: 'Details you can rely on',
    eyebrow: 'VERIFY',
    icon: ShieldCheck,
    description:
      'Review venue details, opening hours, admission requirements, accessibility information, and weather conditions.',
    examples: 'Places · Hours · Tickets · Accessibility · Weather',
    skillIds: [
      'place-detail-verification',
      'opening-hours-intelligence',
      'ticket-entry-planning',
      'accessibility-suitability',
      'hourly-weather-forecast',
    ],
  },
  {
    title: 'Smarter plans, smoother changes',
    eyebrow: 'PLAN & ADAPT',
    icon: RefreshCw,
    description:
      'Shape an efficient itinerary, balance your budget, spot scheduling issues, and adapt the plan when things change.',
    examples: 'Routes · Budget · Timing · Feasibility · Replanning',
    skillIds: [
      'multi-stop-route-optimizer',
      'trip-budget-allocation',
      'schedule-conflict-validation',
      'itinerary-feasibility-audit',
      'disruption-replanning',
    ],
  },
];

function skillLabel(id: string): string {
  return id
    .split('-')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

const skillOverviewDescriptions: Record<string, string> = {
  'place-discovery': 'Find notable sights that suit your destination and interests',
  'cultural-experience-search': 'Explore local heritage, traditions, and cultural experiences',
  'restaurant-discovery': 'Find restaurants that suit your tastes and schedule',
  'culinary-specialty-discovery': 'Discover regional specialties and signature local dishes',
  'neighborhood-intelligence': 'Get to know the neighborhoods around your planned stops',
  'local-events-discovery': 'Find local events taking place during your visit',
  'seasonal-experience-intelligence': 'Explore seasonal experiences suited to your travel dates',
  'day-trip-discovery': 'Discover nearby places worth exploring on a day trip',
  'place-detail-verification': 'Review available venue information before finalizing a visit',
  'opening-hours-intelligence': 'Check published opening hours against your schedule',
  'ticket-entry-planning': 'Account for admission tickets and timed-entry requirements',
  'accessibility-suitability': 'Assess available accessibility details against your needs',
  'hourly-weather-forecast': 'Use hourly forecasts to plan weather-sensitive activities',
  'multi-stop-route-optimizer': 'Order stops to make travel between locations more efficient',
  'trip-budget-allocation': 'Allocate your budget across the main travel expenses',
  'schedule-conflict-validation': 'Flag overlapping activities and tight transfer windows',
  'itinerary-feasibility-audit': 'Review timing, logistics, and unresolved planning details',
  'disruption-replanning': 'Adjust affected activities while keeping the rest of your plan intact',
};

function App() {
  const brandTagline = useBrandTagline();
  const [historyPreview, setHistoryPreview] = useState<{
    journey: string;
    label: string;
    content: string;
  } | null>(null);
  const [page, setPage] = useState<Page>('chat');
  const [collapsed, setCollapsed] = useState(false);
  const [theme, setTheme] = useState<'dark' | 'light'>('dark');
  const [health, setHealth] = useState<Health | null>(null);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [sessions, setSessions] = useState<{ session_id: string; preview: string }[]>([]);
  const [sessionId, setSessionId] = useState(getOrCreateSessionId);
  const sessionRef = useRef(sessionId);
  useEffect(() => {
    rememberSessionId(sessionId);
  }, [sessionId]);
  const pendingSessions = useRef(new Set<string>());
  const abortControllers = useRef(new Map<string, AbortController>());
  const [journeys, setJourneys] = useState<Record<string, JourneyUI>>({});
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const input = drafts[sessionId] ?? '';
  const setInput = (value: string) =>
    setDrafts((previous) => ({ ...previous, [sessionRef.current]: value }));
  const [error, setError] = useState('');
  const selected = journeys[sessionId];
  const messages = useMemo(
    () => (selected?.messages ?? []).filter(isVisibleConversationMessage),
    [selected?.messages],
  );
  const loading = selected?.loading ?? false;
  const active = selected?.active ?? [];
  const planningStage = selected?.stage ?? '';
  const activeTrip = selected?.activeTrip ?? null;
  const workingDraft = selected?.workingDraft ?? null;
  const draftHistory = selected?.draftHistory ?? [];
  const savedSnapshots = selected?.savedSnapshots ?? [];
  const journeyError = selected?.error ?? '';
  const updateJourney = (id: string, update: (state: JourneyUI) => JourneyUI) =>
    setJourneys((prev) => ({ ...prev, [id]: update(prev[id] ?? emptyJourney()) }));
  const bottom = useRef<HTMLDivElement>(null);
  const load = useCallback(async () => {
    try {
      const [h, s, list] = await Promise.all([
        apiJSON<Health>('/api/health'),
        apiJSON<{ skills: Skill[] }>('/api/skills'),
        apiJSON<{ sessions: { session_id: string; preview: string }[] }>(
          '/api/sessions?user_id=' + USER,
        ),
      ]);
      setHealth(h);
      setSkills(s.skills);
      setSessions(list.sessions);
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Backend unavailable.');
    }
  }, []);
  useEffect(() => {
    let cancelled = false;
    const target = sessionId;
    if (!pendingSessions.current.has(target)) {
      apiJSON<{
        active_itinerary: TripSnapshot | null;
        working_draft?: WorkingDraft | null;
        draft_history: DraftHistoryEntry[];
        saved_snapshots: SavedSnapshotEntry[];
      }>('/api/trips/active?user_id=' + USER + '&session_id=' + encodeURIComponent(target))
        .then((data) => {
          if (!cancelled && !pendingSessions.current.has(target)) {
            updateJourney(target, (state) => ({
              ...state,
              activeTrip: data.active_itinerary,
              workingDraft: data.working_draft ?? null,
              draftHistory: data.draft_history ?? [],
              savedSnapshots: data.saved_snapshots ?? [],
            }));
          }
        })
        .catch(() => {});
    }
    return () => {
      cancelled = true;
    };
  }, [sessionId]);
  // Rehydrate the active conversation after refresh and whenever a prior journey is selected.
  // The same session ID must survive refresh; otherwise one dialogue becomes many records.
  useEffect(() => {
    const target = sessionId;
    if (pendingSessions.current.has(target) || journeys[target]?.historyLoaded) return;
    let cancelled = false;
    apiJSON<{ messages: ChatMessage[] }>(
      `/api/history?user_id=${USER}&session_id=${encodeURIComponent(target)}`,
    )
      .then((data) => {
        if (cancelled || pendingSessions.current.has(target)) return;
        updateJourney(target, (state) =>
          state.historyLoaded
            ? state
            : {
                ...state,
                messages: data.messages,
                historyLoaded: true,
              },
        );
      })
      .catch(() => {
        if (!cancelled && !pendingSessions.current.has(target))
          updateJourney(target, (state) => ({
            ...state,
            error: 'Could not load session history.',
          }));
      });
    return () => {
      cancelled = true;
    };
    // Sessions change only when switching or explicitly starting a new journey.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
  }, [theme]);
  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, loading]);
  async function submit(override?: string) {
    const text = (override || input).trim();
    const target = sessionRef.current;
    if (!text || pendingSessions.current.has(target)) return;
    pendingSessions.current.add(target);
    const controller = new AbortController();
    abortControllers.current.set(target, controller);
    updateJourney(target, (state) => ({
      ...state,
      historyLoaded: true,
      messages: [...state.messages, { role: 'user', content: text }],
      loading: true,
      stage: 'router',
      active: [],
      error: '',
    }));
    setDrafts((previous) => ({ ...previous, [target]: '' }));
    let completed = false;
    let receivedError = false;
    try {
      await streamChat(
        { message: text, user_id: USER, session_id: target, context: {} },
        (name, value) => {
          if (name === 'progress')
            updateJourney(target, (state) => ({
              ...state,
              stage: (value as { stage?: string }).stage || state.stage,
            }));
          if (name === 'plan')
            updateJourney(target, (state) => ({
              ...state,
              active: (value as { skills: string[] }).skills || [],
            }));
          if (name === 'done') {
            const result = value as {
              answer: string;
              results: SkillResult[];
              elapsed_ms: number;
              planner_mode: string;
              active_itinerary?: TripSnapshot | null;
              working_draft?: WorkingDraft | null;
              draft_history?: DraftHistoryEntry[];
              saved_snapshots?: SavedSnapshotEntry[];
              trip_change?: TripChange | null;
              model_issue?: string | null;
              model_diagnostic?: { stage?: string; code?: string; detail?: string } | null;
              execution_trace?: ChatMessage['execution_trace'];
              planner_limitations?: string[];
            };
            if (typeof result.answer !== 'string' || !result.answer.trim())
              throw new Error('Backend returned an empty assistant response.');
            completed = true;
            const detail = result.model_diagnostic;
            const diagnostic = detail?.code
              ? `Model diagnostic: ${detail.code}${detail.stage ? ` (${detail.stage})` : ''}${detail.detail ? ` - ${detail.detail}` : ''}`
              : result.model_issue
                ? `Model diagnostic: ${result.model_issue}`
                : '';
            updateJourney(target, (state) => ({
              ...state,
              activeTrip: result.active_itinerary ?? null,
              workingDraft: result.working_draft ?? null,
              draftHistory: result.draft_history ?? state.draftHistory,
              savedSnapshots: result.saved_snapshots ?? state.savedSnapshots,
              error: diagnostic,
              messages: isVisibleConversationMessage({ role: 'assistant', content: result.answer })
                ? [
                    ...state.messages,
                    {
                      role: 'assistant',
                      content: result.answer,
                      results: result.results,
                      elapsed_ms: result.elapsed_ms,
                      planner_mode: result.planner_mode,
                      trip_change: result.trip_change,
                      working_draft: result.working_draft,
                      execution_trace: result.execution_trace,
                      planner_limitations: result.planner_limitations,
                    },
                  ]
                : state.messages,
            }));
            load();
          }
          if (name === 'error') {
            receivedError = true;
            updateJourney(target, (state) => ({
              ...state,
              error: (value as { message?: string }).message || 'Backend request failed.',
            }));
          }
        },
        controller.signal,
      );
      if (!completed && !receivedError && !controller.signal.aborted)
        updateJourney(target, (state) => ({
          ...state,
          error: 'Stream ended before completion. Review the backend logs.',
        }));
    } catch (e) {
      if (!controller.signal.aborted)
        updateJourney(target, (state) => ({
          ...state,
          error: e instanceof Error ? e.message : 'Unexpected request error.',
        }));
    } finally {
      if (abortControllers.current.get(target) === controller)
        abortControllers.current.delete(target);
      pendingSessions.current.delete(target);
      updateJourney(target, (state) => ({ ...state, loading: false, stage: '', active: [] }));
    }
  }
  async function getTripVersion(kind: 'saved' | 'draft', version: number, target: string) {
    const url = kind === 'saved' ? `/api/trips/saved/${version}` : `/api/trips/draft/${version}`;
    return apiJSON<{ itinerary?: unknown }>(
      `${url}?user_id=${USER}&session_id=${encodeURIComponent(target)}`,
    );
  }
  async function loadTripVersionTitle(
    kind: 'saved' | 'draft',
    version: number,
    target: string,
  ): Promise<string | null> {
    const data = await getTripVersion(kind, version, target);
    const trip = data.itinerary;
    if (!trip || typeof trip !== 'object' || !('title' in trip) || typeof trip.title !== 'string')
      return null;
    return trip.title.trim() || null;
  }
  async function previewTripVersion(kind: 'saved' | 'draft', version: number) {
    const target = sessionRef.current;
    try {
      const data = await getTripVersion(kind, version, target);
      if (sessionRef.current === target)
        setHistoryPreview({
          journey: target,
          label: kind === 'saved' ? `Saved copy #${version}` : `Working draft #${version}`,
          content: itineraryPreviewText(data.itinerary),
        });
    } catch (error) {
      updateJourney(target, (previous) => ({
        ...previous,
        error: error instanceof Error ? error.message : 'Could not load a saved version.',
      }));
    }
  }
  async function saveTrip() {
    const target = sessionRef.current;
    const state = journeys[target];
    if (
      !state ||
      pendingSessions.current.has(target) ||
      state.loading ||
      (!state.workingDraft && !state.activeTrip) ||
      savedSnapshotForCurrent({
        activeTrip: state.activeTrip,
        workingDraft: state.workingDraft,
        savedSnapshots: state.savedSnapshots,
      }) !== null
    )
      return;
    pendingSessions.current.add(target);
    updateJourney(target, (current) => ({ ...current, loading: true, stage: 'saving', error: '' }));
    try {
      const snapshot = await apiJSON<{
        saved_id: number;
        source_status: string;
        source_version: number;
        saved_at: string;
      }>('/api/trips/save', {
        method: 'POST',
        body: JSON.stringify({
          user_id: USER,
          session_id: target,
          expected_verified_version: state.activeTrip?.version ?? 0,
          expected_draft_version: state.workingDraft?.draft_version ?? 0,
        }),
      });
      updateJourney(target, (prior) => ({
        ...prior,
        error: '',
        savedSnapshots: [
          {
            id: snapshot.saved_id,
            source_status: snapshot.source_status,
            source_version: snapshot.source_version,
            saved_at: snapshot.saved_at,
          },
          ...prior.savedSnapshots.filter((item) => item.id !== snapshot.saved_id),
        ],
      }));
    } catch (error) {
      updateJourney(target, (prior) => ({
        ...prior,
        error: error instanceof Error ? error.message : 'Unable to save itinerary.',
      }));
    } finally {
      pendingSessions.current.delete(target);
      updateJourney(target, (current) => ({ ...current, loading: false, stage: '', active: [] }));
    }
  }
  function cancelCurrent() {
    const target = sessionRef.current;
    const controller = abortControllers.current.get(target);
    if (!controller || controller.signal.aborted) return;
    controller.abort();
    updateJourney(target, (state) => ({
      ...state,
      loading: false,
      stage: '',
      active: [],
      error:
        'Request cancelled locally. A trip version already saved by the server may still be present; refresh to confirm.',
    }));
  }
  function switchSession(id: string) {
    if (id === sessionRef.current) {
      setPage('chat');
      return;
    }
    sessionRef.current = id;
    rememberSessionId(id);
    setSessionId(id);
    setPage('chat');
  }
  function newSession() {
    const id = createSessionId();
    sessionRef.current = id;
    rememberSessionId(id);
    setSessionId(id);
    setPage('chat');
  }
  function exportConversation(format: 'html' | 'pdf') {
    if (!messages.length) return;
    try {
      const trip = workingDraft?.itinerary ?? activeTrip?.itinerary;
      const documentTitle = trip?.title || 'Travel conversation';
      const html = buildConversationHtml({
        title: documentTitle,
        messages,
        status: workingDraft
          ? 'Working draft'
          : activeTrip
            ? 'Itinerary available'
            : 'Conversation',
      });
      if (format === 'html') downloadConversationHtml(html, documentTitle);
      else printConversationPdf(html);
    } catch (exception) {
      updateJourney(sessionRef.current, (state) => ({
        ...state,
        error: exception instanceof Error ? exception.message : 'Could not export conversation.',
      }));
    }
  }
  return (
    <div className="app-shell">
      <aside className={'sidebar' + (collapsed ? ' collapsed' : '')}>
        <div className="identity">
          <div className="poreiago-mark-wrap" aria-hidden="true">
            <img
              className="poreiago-symbol"
              src={
                theme === 'light'
                  ? '/brand/poreiago-symbol-light.png'
                  : '/brand/poreiago-symbol.png'
              }
              alt=""
            />
          </div>
          {!collapsed && (
            <div className="brand-copy poreiago-brand-copy">
              <img
                className="poreiago-wordmark"
                src={
                  theme === 'light'
                    ? '/brand/poreiago-wordmark-light.png'
                    : '/brand/poreiago-wordmark-dark.png'
                }
                alt="PoreiaGo"
              />
              <BrandTagline entry={brandTagline} placement="sidebar" />
            </div>
          )}
        </div>
        <button className="primary action-new" onClick={newSession}>
          <Plus size={18} />
          {!collapsed && 'New journey'}
        </button>
        {!collapsed && <div className="nav-label">WORKSPACE</div>}
        <nav>
          {(
            [
              { page: 'chat', label: 'Travel Assistant', icon: MessageCircle },
              { page: 'skills', label: 'Skill Catalog', icon: Layers3 },
              { page: 'memory', label: 'Travel Preferences', icon: BrainCircuit },
              { page: 'knowledge', label: 'Travel Library', icon: BookOpen },
              { page: 'settings', label: 'Models & Data Services', icon: Settings2 },
            ] as const
          ).map((item) => (
            <button
              key={item.page}
              onClick={() => setPage(item.page)}
              className={'nav-item' + (page === item.page ? ' selected' : '')}
            >
              <item.icon size={18} />
              {!collapsed && <span>{item.label}</span>}
            </button>
          ))}
        </nav>
        {!collapsed && (
          <div className="session-list">
            <div className="nav-label">RECENT JOURNEYS</div>
            {sessions.slice(0, 12).map((s) => (
              <button
                className={'session-item' + (sessionId === s.session_id ? ' active' : '')}
                key={s.session_id}
                onClick={() => switchSession(s.session_id)}
                title={s.preview}
              >
                {s.preview}
              </button>
            ))}
          </div>
        )}
        <div className="side-bottom">
          <div className="side-status">
            <span className={'status-dot' + (health ? ' online' : '')} />
            {!collapsed && <span>{health ? 'System online' : 'Disconnected'}</span>}
          </div>
          <div
            className="side-status side-connection"
            aria-label={health ? 'Connected' : 'Offline'}
          >
            <span className={'status-dot' + (health ? ' online' : '')} />
            {!collapsed && <span>{health ? 'Connected' : 'Offline'}</span>}
          </div>
        </div>
      </aside>
      <main className="main">
        <header className="topbar">
          <div className="top-left">
            <button
              className="icon-button"
              aria-label="Toggle sidebar"
              onClick={() => setCollapsed(!collapsed)}
            >
              <Menu size={20} />
            </button>
            <span className="topbar-nav-trail">
              <span>PoreiaGo</span>
              <ArrowRight size={13} aria-hidden="true" />
            </span>
            {page === 'chat' ? (
              workingDraft || activeTrip ? (
                <CurrentTripTitle
                  activeTrip={activeTrip}
                  workingDraft={workingDraft}
                  savedSnapshots={savedSnapshots}
                />
              ) : (
                <span className="topbar-empty-trip">New journey</span>
              )
            ) : (
              <span className="topbar-page-summary" aria-label={`Current page: ${page}`}>
                <strong>
                  {
                    {
                      chat: 'Assistant',
                      skills: 'Skill Catalog',
                      memory: 'Travel Preferences',
                      knowledge: 'Travel Library',
                      settings: 'Models & Data Services',
                    }[page]
                  }
                </strong>
              </span>
            )}
          </div>
          <div className="right-tools">
            {page === 'chat' && (
              <TopbarTripControls
                activeTrip={activeTrip}
                workingDraft={workingDraft}
                draftHistory={draftHistory}
                savedSnapshots={savedSnapshots}
                historyPreview={historyPreview}
                sessionId={sessionId}
                loading={loading}
                onSave={saveTrip}
                onPreview={previewTripVersion}
                onLoadTitle={loadTripVersionTitle}
              />
            )}
            {page === 'chat' && (
              <ExportMenu disabled={!messages.length} onExport={exportConversation} />
            )}
            <button className="icon-button" onClick={load} aria-label="Refresh status">
              <RefreshCw size={17} />
            </button>
            <button
              className="icon-button"
              onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
              aria-label="Toggle theme"
            >
              {theme === 'dark' ? <Sun size={19} /> : <Moon size={19} />}
            </button>
          </div>
        </header>
        {(error || (page === 'chat' && journeyError)) && (
          <div className="error-message">
            <AlertCircle size={15} />
            {page === 'chat' && journeyError ? journeyError : error}
            <button
              onClick={() => {
                setError('');
                updateJourney(sessionId, (state) => ({ ...state, error: '' }));
              }}
              aria-label="Dismiss error"
            >
              ×
            </button>
          </div>
        )}
        {page === 'chat' && (
          <div className="conversation">
            <div className="message-list">
              {messages.length === 0 ? (
                <div className="welcome">
                  <div className="welcome-icon poreiago-welcome-icon">
                    <img
                      src={
                        theme === 'light'
                          ? '/brand/poreiago-symbol-light.png'
                          : '/brand/poreiago-symbol.png'
                      }
                      alt=""
                      aria-hidden="true"
                    />
                  </div>
                  <div className="eyebrow">POREIAGO TRAVEL INTELLIGENCE</div>
                  <h1>Where should we go next?</h1>
                  <BrandTagline entry={brandTagline} placement="welcome" />
                  <p>
                    Describe the trip you have in mind in any language. PoreiaGo can plan directly
                    with your selected model and consult available data providers when needed.
                  </p>
                  {health?.models.some((m) => m.role === 'router' && m.configured) &&
                  health?.models.some((m) => m.role === 'reasoning' && m.configured) ? (
                    <span className="model-ready">
                      Model profiles configured (connectivity not verified)
                    </span>
                  ) : (
                    <span className="model-offline">
                      Configure a compatible local or remote model to enable planning
                    </span>
                  )}
                  <div className="suggestions">
                    {examples.map((ex) => (
                      <button key={ex} onClick={() => submit(ex)}>
                        {ex}
                        <ArrowRight size={17} />
                      </button>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="thread">
                  {messages.map((m, i) => (
                    <section key={i} className={'message ' + m.role}>
                      <div className="message-content">
                        <span className="speaker">{m.role === 'user' ? 'YOU' : 'POREIAGO'}</span>
                        {m.role === 'user' ? (
                          <div className="user-text">{m.content}</div>
                        ) : (
                          <>
                            <ItineraryAnswer content={m.content} />
                            {m.working_draft ? (
                              <div className="trip-change provisional">
                                <AlertCircle size={14} />
                                Working draft {m.working_draft.draft_version} · needs review ·
                                continue editing
                              </div>
                            ) : null}
                            {m.trip_change ? (
                              <div className="trip-change">
                                <CheckCircle2 size={14} />
                                Trip version saved · {m.trip_change.action} · Days{' '}
                                {m.trip_change.changed_days.join(', ') || 'preferences only'}
                              </div>
                            ) : null}
                            {m.results?.length ? <SkillDetails results={m.results} /> : null}
                            {m.execution_trace?.length ? (
                              <details className="skill-details">
                                <summary>Execution trace · {m.planner_mode || 'catalog'}</summary>
                                <div className="skill-detail-list">
                                  {m.execution_trace.map((step, j) => (
                                    <div className="info-row" key={j}>
                                      <strong>
                                        {step.name || 'Runtime'} · {step.status}
                                      </strong>
                                      <span>
                                        {step.elapsed_ms !== undefined
                                          ? `${step.elapsed_ms} ms`
                                          : 'Execution deadline'}
                                        {step.dependencies?.length
                                          ? ` · Depends on: ${step.dependencies.join(', ')}`
                                          : ''}
                                        {step.recovery_for
                                          ? ` · Alternative for: ${step.recovery_for}`
                                          : ''}
                                        {step.recovered_by
                                          ? ` · Recovered by: ${step.recovered_by}`
                                          : ''}
                                        {step.input_bindings &&
                                        Object.keys(step.input_bindings).length
                                          ? ` · Data: ${Object.entries(step.input_bindings)
                                              .map(([key, value]) => `${key} ← ${value}`)
                                              .join(', ')}`
                                          : ''}
                                      </span>
                                    </div>
                                  ))}
                                  {m.planner_limitations?.map((note, j) => (
                                    <p className="limitations" key={j}>
                                      {note}
                                    </p>
                                  ))}
                                </div>
                              </details>
                            ) : null}
                            {m.elapsed_ms !== undefined && (
                              <div className="duration">{(m.elapsed_ms / 1000).toFixed(1)}s</div>
                            )}
                          </>
                        )}
                      </div>
                    </section>
                  ))}
                  {loading && (
                    <div className="thinking">
                      <Activity size={18} className="spin" />
                      <div>
                        <strong>Planning your journey</strong>
                        <span>
                          {active.length
                            ? `Invoking: ${active.join(', ')}`
                            : planningStage === 'itinerary_generation'
                              ? 'Generating and validating the itinerary...'
                              : 'Selecting relevant capabilities...'}
                        </span>
                      </div>
                      <button
                        className="icon-button"
                        onClick={cancelCurrent}
                        aria-label="Cancel current request"
                        title="Cancel this request"
                      >
                        Cancel
                      </button>
                    </div>
                  )}
                </div>
              )}
              <div ref={bottom} />
            </div>
            <div className="composer-area">
              <div className="composer">
                <textarea
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault();
                      submit();
                    }
                  }}
                  placeholder="Describe your destination, interests, and travel preferences..."
                />
                <div className="composer-bottom">
                  <span>
                    <ShieldCheck size={14} /> Source-aware recommendations
                  </span>
                  <button
                    className="send"
                    onClick={() => submit()}
                    disabled={loading || !input.trim()}
                    aria-label="Send message"
                  >
                    <Send size={17} />
                  </button>
                </div>
              </div>
              <p className="disclaimer">
                Route access, availability, prices, and travel requirements must be verified with
                their providers.
              </p>
            </div>
          </div>
        )}
        {page === 'skills' && (
          <div className="dashboard skills-overview-page">
            <div className="page-heading skills-overview-heading">
              <div className="eyebrow">TRAVEL INTELLIGENCE</div>
              <h1>Skills</h1>
              <p>
                PoreiaGo combines specialized travel capabilities to help you discover places, check
                the details, build realistic plans, and adjust when circumstances change.
              </p>
            </div>
            <div className="skills-overview-grid">
              {skillOverviewSections
                .filter(
                  (section) =>
                    !skills.length ||
                    section.skillIds.some((id) => skills.some((skill) => skill.id === id)),
                )
                .map((section) => (
                  <article
                    className="skills-overview-card"
                    key={section.title}
                    tabIndex={0}
                    aria-label={`${section.title}. Hover or focus to explore its Skills.`}
                  >
                    <div className="skills-overview-front">
                      <div className="skills-overview-front-top">
                        <div className="skills-overview-icon" aria-hidden="true">
                          <section.icon size={20} strokeWidth={1.9} />
                        </div>
                        <span className="skills-overview-category">{section.eyebrow}</span>
                      </div>
                      <h2>{section.title}</h2>
                      <p>{section.description}</p>
                      <div className="skills-overview-front-footer">
                        <span className="skills-overview-examples">{section.examples}</span>
                        <span className="skills-overview-discover" aria-hidden="true">
                          Explore <ArrowRight size={14} />
                        </span>
                      </div>
                    </div>
                    <div className="skills-overview-back">
                      <div className="skills-overview-back-heading">
                        <div>
                          <span className="skills-overview-back-eyebrow">{section.eyebrow}</span>
                          <strong>Explore the Skills</strong>
                        </div>
                        <span
                          className="skills-overview-back-count"
                          aria-label={`${section.skillIds.length} Skills`}
                        >
                          {String(section.skillIds.length).padStart(2, '0')}
                        </span>
                      </div>
                      <div className="skills-overview-skill-list">
                        {section.skillIds
                          .filter((id) => !skills.length || skills.some((skill) => skill.id === id))
                          .map((id, skillIndex) => (
                            <div
                              className="skills-overview-skill"
                              key={id}
                              tabIndex={0}
                              aria-label={`${skillLabel(id)}: ${skillOverviewDescriptions[id]}`}
                            >
                              <span className="skills-overview-skill-default" aria-hidden="true">
                                <span className="skills-overview-skill-index">
                                  {String(skillIndex + 1).padStart(2, '0')}
                                </span>
                                <span className="skills-overview-skill-name">{skillLabel(id)}</span>
                              </span>
                              <span
                                className="skills-overview-skill-description"
                                aria-hidden="true"
                              >
                                {skillOverviewDescriptions[id]}
                              </span>
                            </div>
                          ))}
                      </div>
                    </div>
                  </article>
                ))}
            </div>
            <div className="skills-overview-note">
              <div className="skills-overview-note-icon" aria-hidden="true">
                <Layers3 size={18} strokeWidth={1.8} />
              </div>
              <div>
                <strong>One connected planning experience</strong>
                <p>
                  Relevant skills are used when a trip needs them. Details that cannot be verified
                  remain clearly identified, so you can keep planning with the right context.
                </p>
              </div>
            </div>
          </div>
        )}
        {page === 'settings' && <ModelsProvidersOverview health={health} onRefresh={load} />}
        {page === 'memory' && <TravelPreferences userId={USER} />}
        {page === 'knowledge' && <KnowledgeWorkspace userId={USER} />}
      </main>
    </div>
  );
}
createRoot(document.getElementById('root')!).render(<App />);
