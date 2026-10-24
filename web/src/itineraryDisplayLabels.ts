/** Display-only terminology. Never rewrite canonical itinerary content. */
export const DISPLAY_LABELS: Readonly<Record<string, string>> = {
  Why: 'Planning rationale',
  Transfer: 'Getting there',
  Check: 'Before you go',
  Booking: 'Reservations',
  Duration: 'Time needed',
  'Cost level': 'Cost category',
  'Estimated cost': 'Estimated cost',
};

/** Rename only semantic dt labels after the existing formatter has parsed facts. */
export function formatRenderedItineraryLabels(html: string): string {
  return html.replace(
    /(<dt\b[^>]*>)(Why|Transfer|Check|Booking|Duration|Cost level)(<\/dt>)/gi,
    (_match, opening: string, label: string, closing: string) =>
      `${opening}${DISPLAY_LABELS[label] ?? label}${closing}`,
  );
}
