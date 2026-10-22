/**
 * Remove internal draft-review diagnostics from the chat presentation only.
 * The original reply and its structured issues remain unchanged in the API,
 * session history, and persistence layer.
 */
export function presentAnswer(content: string): string {
  const text = content.replace(/\r\n?/g, "\n");
  const lines = text.split("\n");
  const first = (lines[0] ?? "").trim();
  const savedDraft =
    /^Working draft \d+ saved\. You can continue editing this plan\.$/.test(
      first,
    );
  const legacyDraft =
    /^Working draft\s*[—-]\s*changes are provisional and need review\. You can continue editing this plan\.$/.test(
      first,
    );
  if (!savedDraft && !legacyDraft) return content;

  let cursor = 1;
  while (cursor < lines.length && !lines[cursor].trim()) cursor++;

  if (savedDraft && lines[cursor]?.trim() === "Review needed:") {
    cursor++;
    // These lines come from the server's draft preface, not the itinerary.
    while (cursor < lines.length) {
      const current = lines[cursor].trim();
      const isIssue = /^(?:-\s*)?(?:Day\s+\d+\b|Trip\s*:|[A-Z][A-Z_]+:)/.test(
        current,
      );
      const isListIssue =
        current.startsWith("- ") && !/^-\s*\d{1,2}:\d{2}\b/.test(current);
      if (
        !current ||
        isIssue ||
        isListIssue ||
        /^\d+ additional issues are listed in the draft review\.$/.test(current)
      ) {
        cursor++;
        continue;
      }
      break;
    }
  }

  if (
    lines[cursor]?.trim() ===
    "Saved status does not verify opening hours, ticket availability or event times."
  ) {
    cursor++;
  }
  while (cursor < lines.length && !lines[cursor].trim()) cursor++;

  const itinerary = lines.slice(cursor).join("\n").trim();
  return itinerary || "Your working draft is saved and remains editable.";
}
