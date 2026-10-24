import { useLayoutEffect, useRef } from 'react';
import { FormattedAnswer } from './FormattedAnswer';
import { DISPLAY_LABELS } from './itineraryDisplayLabels';

/** Presentation adapter: preserve the exact original text for the parser. */
export function ItineraryAnswer({ content }: { content: string }) {
  const root = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    // Semantic labels are created by the existing itinerary formatter.
    // Change only their visible text, leaving raw messages and keys untouched.
    root.current?.querySelectorAll<HTMLElement>('.itinerary-facts dt').forEach((label) => {
      const oldText = label.textContent?.trim() ?? '';
      const display = DISPLAY_LABELS[oldText];
      if (display && display !== oldText) label.textContent = display;
    });
  }, [content]);

  return (
    <div ref={root} className="itinerary-answer">
      <FormattedAnswer content={content} />
    </div>
  );
}
