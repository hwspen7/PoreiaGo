/**
 * Presentation-only parsing for itinerary prose.
 * No source content is modified, verified, or written back to persistence.
 */
export interface ItineraryActivity {
  start: string;
  end?: string;
  headline: string;
  description: string;
  location?: string;
}

export interface ItineraryFact {
  label: string;
  value: string;
}

const activityPattern =
  /^\s*(?:[-*]\s+|\d+[.)]\s+)?([01]?\d|2[0-3]):([0-5]\d)(?:\s*(?:-|\u2013|\u2014|to)\s*([01]?\d|2[0-3]):([0-5]\d))?\s*[:\uFF1A]\s*(.+)$/i;
const factLabelPattern =
  /^(Why|Transfer|Booking|Check|Duration|Allow|Cost level|Estimated cost|Accessibility|Reservation|Opening hours|Hours|Note)\s*:\s*(.+)$/i;
const sectionLabelPattern =
  /^(Logistics|Alternative|Budget|Tip|Budget approach|Arrival and departure|Planning assumptions|Choices and trade-offs|Check before travel)\s*:?\s*/i;
const locationPattern = /^(.*)\s+\(([^()]+)\)\s*$/;

export function cleanPresentationText(text: string): string {
  return text
    .replace(/\s*\((?:unverified|indicative)\)/gi, "")
    .replace(/\s+([,.;:])/g, "$1")
    .trim();
}

export function parseItineraryActivity(line: string): ItineraryActivity | null {
  const match = activityPattern.exec(line);
  if (!match) return null;
  const start = `${match[1].padStart(2, "0")}:${match[2]}`;
  const end =
    match[3] !== undefined && match[4] !== undefined
      ? `${match[3].padStart(2, "0")}:${match[4]}`
      : undefined;
  let body = cleanPresentationText(match[5]);
  let location: string | undefined;
  const locationMatch = locationPattern.exec(body);
  if (locationMatch) {
    body = locationMatch[1].trim();
    location = locationMatch[2].trim();
  }
  // A short leading sentence makes an identifiable activity title without
  // discarding the remainder of the model's original description.
  const firstSentence = /^(.+?[.!?])(?:\s+(.+))$/.exec(body);
  const useFirstSentence = firstSentence && firstSentence[1].length <= 165;
  return {
    start,
    end,
    headline: useFirstSentence ? firstSentence[1] : body,
    description: useFirstSentence ? firstSentence[2] : "",
    location,
  };
}

export function parseItineraryFact(part: string): ItineraryFact | null {
  const cleaned = cleanPresentationText(part);
  if (!cleaned) return null;
  const allow = /^Allow\s+(?:about\s+)?(.+)$/i.exec(cleaned);
  if (allow) return { label: "Duration", value: `About ${allow[1]}` };
  const match = factLabelPattern.exec(cleaned);
  if (!match) return null;
  const rawLabel = match[1].toLowerCase();
  const label =
    rawLabel === "allow" || rawLabel === "duration"
      ? "Duration"
      : rawLabel === "cost level"
        ? "Cost level"
        : rawLabel === "estimated cost"
          ? "Estimated cost"
          : rawLabel.charAt(0).toUpperCase() + rawLabel.slice(1);
  return { label, value: match[2].trim() };
}

export function parseItineraryFacts(line: string): ItineraryFact[] | null {
  const parts = line
    .split(/\s*\|\s*/)
    .map((part) => part.trim())
    .filter(Boolean);
  if (!parts.length) return null;
  const facts = parts.map(parseItineraryFact);
  return facts.every((fact): fact is ItineraryFact => fact !== null)
    ? facts
    : null;
}

export function isItinerarySectionLine(line: string): boolean {
  return sectionLabelPattern.test(line.trim());
}

export function parseDayMetadata(line: string): ItineraryFact | null {
  const match = /^(Theme|Area|City|Overnight base)\s*:\s*(.+)$/i.exec(
    line.trim(),
  );
  if (!match) return null;
  return { label: match[1], value: cleanPresentationText(match[2]) };
}
