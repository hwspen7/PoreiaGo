type Trip = {
  title?: string;
  summary?: string;
  days?: Array<{
    day?: number;
    title?: string;
    activities?: Array<{
      start_time?: string;
      time?: string;
      description?: string;
      area?: string;
    }>;
  }>;
};

/** Show archived trip contents as a readable, immutable preview, never raw JSON. */
export function itineraryPreviewText(raw: unknown): string {
  if (!raw || typeof raw !== 'object') return 'This itinerary could not be displayed.';
  const trip = raw as Trip;
  const lines: string[] = [];
  if (typeof trip.title === 'string' && trip.title.trim()) lines.push(trip.title.trim());
  if (typeof trip.summary === 'string' && trip.summary.trim()) lines.push(trip.summary.trim());
  for (const day of Array.isArray(trip.days) ? trip.days : []) {
    lines.push(`Day ${day.day ?? ''}: ${day.title ?? ''}`.trim());
    for (const activity of Array.isArray(day.activities) ? day.activities : []) {
      if (typeof activity.description !== 'string' || !activity.description.trim()) continue;
      const at = activity.start_time || activity.time;
      lines.push(
        `${at ? `${at} — ` : '• '}${activity.description}${activity.area ? ` (${activity.area})` : ''}`,
      );
    }
  }
  return lines.length ? lines.join('\n\n') : 'No saved itinerary details are available.';
}
