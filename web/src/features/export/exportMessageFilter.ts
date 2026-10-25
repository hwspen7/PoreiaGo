/**
 * Exclude UI-only save operations from published journey documents.
 * The stored conversation and itinerary snapshots are left untouched.
 */
export type ExportableMessage = { role: 'user' | 'assistant'; content: string };

const saveRequest =
  /^(?:please\s+)?(?:save|store|bookmark)(?:\s+(?:my|our|this|the|current|latest))?\s+(?:itinerary|travel\s+plan|trip|journey|plan)(?:\s+(?:now|please|for\s+later))?[.!?\s]*$/i;

const saveConfirmation = [
  /^Saved\s+(?:verified\s+itinerary|provisional\s+(?:working\s+)?draft)\s+as\s+snapshot\s*#\s*\d+\b/i,
  /^Saved\s+itinerary\s+snapshot\s*#\s*\d+\b/i,
  /^Itinerary\s+(?:successfully\s+)?saved\s+as\s+snapshot\s*#\s*\d+\b/i,
];

/** A response must be a standalone confirmation to be excluded. */
function isStandaloneSaveConfirmation(content: string): boolean {
  const normalized = content.trim().replace(/\s+/g, ' ');
  if (!saveConfirmation.some((pattern) => pattern.test(normalized))) return false;
  // Protect a genuine itinerary returned alongside a save acknowledgment.
  if (
    /\bday\s*\d+\s*:/i.test(normalized) ||
    /\b\d{1,2}:\d{2}\b/.test(normalized) ||
    /\n\s*[-*]\s/.test(content)
  )
    return false;
  return normalized.length <= 500;
}

export function isJourneyExportMessage(message: ExportableMessage): boolean {
  const content = message.content.trim();
  if (!content) return false;
  if (message.role === 'user') return !saveRequest.test(content);
  return !isStandaloneSaveConfirmation(content);
}

export function selectJourneyExportMessages<T extends ExportableMessage>(
  messages: readonly T[],
): T[] {
  return messages.filter(isJourneyExportMessage);
}
