import { useEffect, useRef, useState } from 'react';
import { AlertCircle, CheckCircle2, ChevronDown, Compass, History, Save } from 'lucide-react';
import './topbarTripControls.css';

type ItinerarySummary = { title: string; days: unknown[] };
type VerifiedTrip = { version: number; itinerary: ItinerarySummary };
type WorkingTrip = { draft_version: number; itinerary: ItinerarySummary };
type DraftVersion = { draft_version: number };
type SavedVersion = { id: number; source_status: string; source_version: number; saved_at?: string };
type HistoryPreview = { journey: string; label: string; content: string } | null;
type VersionKind = 'draft' | 'saved';

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

export function CurrentTripTitle({ activeTrip, workingDraft }: Pick<Props, 'activeTrip' | 'workingDraft'>) {
  const trip = workingDraft?.itinerary ?? activeTrip?.itinerary;
  if (!trip) return null;
  return <div className={`topbar-trip-summary${workingDraft ? ' is-draft' : ''}`} aria-label={`Current itinerary: ${trip.title}`} title={trip.title}>
    {workingDraft ? <AlertCircle size={15} aria-label="Working draft" /> : <Compass size={15} aria-label="Saved itinerary" />}
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

  const count = draftHistory.length + savedSnapshots.length;
  const hasTrip = Boolean(workingDraft || activeTrip);
  if (!hasTrip && !count) return null;

  // The history endpoint returns metadata only. Request the actual itinerary
  // title when the user opens the menu; do not copy the current trip title to
  // unrelated saved snapshots. Limit concurrent requests for large histories.
  const populateTitles = async () => {
    const target = sessionId;
    const versions = [
      ...savedSnapshots.map(item => ({ kind: 'saved' as const, version: item.id })),
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
    {hasTrip && <button type="button" className="topbar-trip-save" onClick={onSave} disabled={loading} aria-label="Save current itinerary" title="Save current itinerary">
      <Save size={15} aria-hidden="true" /><span>Save</span>
    </button>}
    {count > 0 && <details className="topbar-history" ref={historyMenu} onToggle={event => { if (event.currentTarget.open) void populateTitles(); }}>
      <summary aria-label={`Saved itinerary versions, ${count} items`}><History size={16} aria-hidden="true" /><span>Versions</span><span className="topbar-history-count">{count}</span><ChevronDown size={13} className="topbar-history-chevron" aria-hidden="true" /></summary>
      <div className="topbar-history-panel">
        <div className="topbar-history-heading"><strong>Itinerary history</strong><small>{draftHistory.length} drafts · {savedSnapshots.length} saved copies</small></div>
        <div className="topbar-history-items">
          {savedSnapshots.map(item => <button key={`saved-${item.id}`} type="button" onClick={() => onPreview('saved', item.id)}>
            <CheckCircle2 size={15} aria-hidden="true" />
            <span className="topbar-version-label"><strong>{titles[`saved:${item.id}`] ?? `Saved itinerary #${item.id}`}</strong><small>Saved copy{item.saved_at ? ` · ${item.saved_at.slice(0, 10)}` : ''}{item.source_status === 'draft' ? ' · draft' : ''}</small></span>
          </button>)}
          {draftHistory.map(item => <button key={`draft-${item.draft_version}`} type="button" onClick={() => onPreview('draft', item.draft_version)}>
            <AlertCircle size={15} aria-hidden="true" /><span className="topbar-version-label"><strong>{titles[`draft:${item.draft_version}`] ?? `Working draft #${item.draft_version}`}</strong><small>Working draft · needs review</small></span>
          </button>)}
        </div>
        {historyPreview?.journey === sessionId && <div className="topbar-history-preview">
          <strong>{historyPreview.label} · read-only</strong>
          <pre className="saved-trip-preview">{historyPreview.content}</pre>
        </div>}
      </div>
    </details>}
  </div>;
}
