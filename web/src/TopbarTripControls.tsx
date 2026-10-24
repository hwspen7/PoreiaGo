import { useEffect, useRef, useState } from 'react';
import { AlertCircle, CheckCircle2, ChevronDown, Compass, History, Save } from 'lucide-react';
import { savedSnapshotForCurrent } from './savePresentation';
import './topbarTripControls.css';

type ItinerarySummary = { title: string; days: unknown[] };
type VerifiedTrip = { version: number; itinerary: ItinerarySummary };
type WorkingTrip = { draft_version: number; itinerary: ItinerarySummary };
type DraftVersion = { draft_version: number; updated_at?: string };
type SavedVersion = { id: number; source_status: string; source_version: number; saved_at?: string };
type HistoryPreview = { journey: string; label: string; content: string } | null;
type VersionKind = 'draft' | 'saved';

// Saved timestamps come from the server in UTC and are displayed in the
// browser's local time zone. Invalid or legacy missing values stay unlabeled.
function formatHistoryTime(value?: string): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  return new Intl.DateTimeFormat('en-US', {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit',
  }).format(parsed);
}

type Props = {
  activeTrip: VerifiedTrip | null;
  workingDraft: WorkingTrip | null;
  draftHistory: DraftVersion[];
  savedSnapshots: SavedVersion[];
  historyPreview: HistoryPreview;
  sessionId: string;
  loading: boolean;
  onSave: () => void;
  onPreview: (kind: VersionKind, version: number) => void;
  onLoadTitle: (kind: VersionKind, version: number, sessionId: string) => Promise<string | null>;
};

export function CurrentTripTitle({
  activeTrip, workingDraft, savedSnapshots = [],
}: Pick<Props, 'activeTrip' | 'workingDraft'> & { savedSnapshots?: SavedVersion[] }) {
  const trip = workingDraft?.itinerary ?? activeTrip?.itinerary;
  if (!trip) return null;
  const saved = savedSnapshotForCurrent({ activeTrip, workingDraft, savedSnapshots });
  const isDraft = Boolean(workingDraft);
  const label = saved
    ? (isDraft ? 'Saved working draft (review required)' : 'Saved verified itinerary')
    : (isDraft ? 'Unsaved working draft' : 'Current itinerary');
  return <div
    className={`topbar-trip-summary${isDraft ? ' is-draft' : ''}${saved ? ' is-saved' : ''}`}
    aria-label={`${label}: ${trip.title}`}
    title={`${label}: ${trip.title}${saved ? ` · Snapshot #${saved.id}` : ''}`}
  >
    {saved ? <CheckCircle2 size={15} aria-label="Saved copy" /> :
      isDraft ? <AlertCircle size={15} aria-label="Working draft" /> :
      <Compass size={15} aria-label="Current itinerary" />}
    <strong>{trip.title}</strong>
  </div>;
}

export function TopbarTripControls({
  activeTrip, workingDraft, draftHistory, savedSnapshots, historyPreview,
  sessionId, loading, onSave, onPreview, onLoadTitle,
}: Props) {
  const historyMenu = useRef<HTMLDetailsElement>(null);
  const sessionRef = useRef(sessionId);
  sessionRef.current = sessionId;
  const requested = useRef(new Set<string>());
  const [titles, setTitles] = useState<Record<string, string>>({});

  useEffect(() => {
    setTitles({});
    requested.current.clear();
    if (historyMenu.current) historyMenu.current.open = false;
  }, [sessionId]);
  useEffect(() => {
    const dismissOutside = (event: PointerEvent) => {
      if (historyMenu.current && !historyMenu.current.contains(event.target as Node)) historyMenu.current.open = false;
    };
    const dismissEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && historyMenu.current) historyMenu.current.open = false;
    };
    document.addEventListener('pointerdown', dismissOutside);
    document.addEventListener('keydown', dismissEscape);
    return () => {
      document.removeEventListener('pointerdown', dismissOutside);
      document.removeEventListener('keydown', dismissEscape);
    };
  }, []);

  // Collapse legacy duplicate saves in the menu without deleting database history.
  // Saved items arrive newest first; retain the most recent copy of each version.
  const seenSnapshotVersions = new Set<string>();
  const visibleSnapshots = savedSnapshots.filter(item => {
    const key = `${item.source_status}:${item.source_version}`;
    if (seenSnapshotVersions.has(key)) return false;
    seenSnapshotVersions.add(key);
    return true;
  });
  const count = draftHistory.length + visibleSnapshots.length;
  const hasTrip = Boolean(workingDraft || activeTrip);
  const saved = savedSnapshotForCurrent({ activeTrip, workingDraft, savedSnapshots });
  if (!hasTrip && !count) return null;

  // The history endpoint returns metadata only. Request the actual itinerary
  // title when the user opens the menu; do not copy the current trip title to
  // unrelated saved snapshots. Limit concurrent requests for large histories.
  const populateTitles = async () => {
    const target = sessionId;
    const versions = [
      ...visibleSnapshots.map(item => ({ kind: 'saved' as const, version: item.id })),
      ...draftHistory.map(item => ({ kind: 'draft' as const, version: item.draft_version })),
    ];
    const pending = versions.filter(item => {
      const key = `${item.kind}:${item.version}`;
      if (requested.current.has(key)) return false;
      requested.current.add(key);
      return true;
    });
    let cursor = 0;
    async function worker() {
      while (cursor < pending.length) {
        const item = pending[cursor++];
        const key = `${item.kind}:${item.version}`;
        try {
          const title = await onLoadTitle(item.kind, item.version, target);
          if (sessionRef.current === target && title) {
            setTitles(previous => ({ ...previous, [key]: title }));
          }
        } catch {
          // Keep the archived item usable even when its title cannot be loaded.
        }
      }
    }
    await Promise.all(Array.from({ length: Math.min(3, pending.length) }, () => worker()));
  };

  return <div className="topbar-trip-controls" aria-label="Current itinerary actions">
    {hasTrip && <button type="button"
      className={`topbar-trip-save${saved ? ' is-saved' : ''}`}
      onClick={onSave} disabled={loading || Boolean(saved)}
      aria-label={saved ? 'Current itinerary already saved' : 'Save current itinerary'}
      title={saved ? `Already saved as copy #${saved.id} (${workingDraft ? 'working draft' : 'verified itinerary'})` : 'Save current itinerary'}>
      {saved ? <CheckCircle2 size={15} aria-hidden="true" /> : <Save size={15} aria-hidden="true" />}
      <span>{saved ? 'Saved' : 'Save'}</span>
    </button>}
    {count > 0 && <details className="topbar-history" ref={historyMenu} onToggle={event => { if (event.currentTarget.open) void populateTitles(); }}>
      <summary aria-label={`Saved itinerary versions, ${count} items`}><History size={16} aria-hidden="true" /><span>Versions</span><span className="topbar-history-count">{count}</span><ChevronDown size={13} className="topbar-history-chevron" aria-hidden="true" /></summary>
      <div className="topbar-history-panel">
        <div className="topbar-history-heading"><strong>Itinerary history</strong><small>{draftHistory.length} drafts · {visibleSnapshots.length} saved copies</small></div>
        <div className="topbar-history-items">
          {visibleSnapshots.map(item => {
            const savedAt = formatHistoryTime(item.saved_at);
            return <button key={`saved-${item.id}`} type="button" onClick={() => onPreview('saved', item.id)}>
              <CheckCircle2 size={15} aria-hidden="true" />
              <span className="topbar-version-label">
                <strong>{titles[`saved:${item.id}`] ?? `Saved itinerary #${item.id}`}</strong>
                <small>
                  Saved copy #{item.id}
                  {savedAt && <> · <time dateTime={item.saved_at}>{savedAt}</time></>}
                  {item.source_status === 'draft' ? ' · Working draft (review required)' : ''}
                </small>
              </span>
            </button>;
          })}
          {draftHistory.map(item => {
            const updatedAt = formatHistoryTime(item.updated_at);
            return <button key={`draft-${item.draft_version}`} type="button" onClick={() => onPreview('draft', item.draft_version)}>
              <AlertCircle size={15} aria-hidden="true" />
              <span className="topbar-version-label">
                <strong>{titles[`draft:${item.draft_version}`] ?? `Working draft #${item.draft_version}`}</strong>
                <small>Working draft · needs review{updatedAt && <> · Updated <time dateTime={item.updated_at}>{updatedAt}</time></>}</small>
              </span>
            </button>;
          })}
        </div>
        {historyPreview?.journey === sessionId && <div className="topbar-history-preview">
          <strong>{historyPreview.label} · read-only</strong>
          <pre className="saved-trip-preview">{historyPreview.content}</pre>
        </div>}
      </div>
    </details>}
  </div>;
}
