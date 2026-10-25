/** Display state derived only from a persisted snapshot of this exact trip version. */
export type SnapshotReference = {
  id: number;
  source_status: string;
  source_version: number;
};
export type CurrentVersionReference = {
  activeTrip: { version: number } | null;
  workingDraft: { draft_version: number } | null;
  savedSnapshots: readonly SnapshotReference[];
};

export function savedSnapshotForCurrent({
  activeTrip,
  workingDraft,
  savedSnapshots,
}: CurrentVersionReference): SnapshotReference | null {
  const sourceStatus = workingDraft ? 'draft' : 'verified';
  const sourceVersion = workingDraft?.draft_version ?? activeTrip?.version;
  if (sourceVersion === undefined) return null;
  return (
    savedSnapshots.find(
      (snapshot) =>
        snapshot.source_status === sourceStatus && snapshot.source_version === sourceVersion,
    ) ?? null
  );
}

/**
 * Save acknowledgments are UI events, not planning answers.
 * Suppress only known administrative messages, leaving itinerary prose and
 * validation feedback untouched. Existing database history is not altered.
 */
export function isVisibleConversationMessage(message: { role: string; content: string }): boolean {
  const content = message.content.trim();
  if (message.role === 'user') {
    return !/^(?:please\s+)?save my itinerary[.!?\s]*$/i.test(content);
  }
  if (message.role !== 'assistant') return true;
  return !(
    /^Saved\s+(?:working draft|verified itinerary)\s+as\s+snapshot\s*#\d+\b/i.test(content) ||
    /^Saved\s+itinerary\s+snapshot\s*#\d+\b/i.test(content)
  );
}
