import test from 'node:test';
import assert from 'node:assert/strict';
import {
  cleanPresentationText,
  parseItineraryActivity,
  parseItineraryFact,
  parseItineraryFacts,
  parseDayMetadata,
  isItinerarySectionLine,
} from '../../src/features/itinerary/itineraryPresentation.ts';

test('recognizes time ranges, titles, narrative descriptions and locations', () => {
  const result = parseItineraryActivity(
    "- 10:45 - 12:00: Walk to Zhanqiao Pier, Qingdao's most iconic landmark. Enjoy the view of the red lighthouse and sea. (Zhanqiao, Shinan District)",
  );
  assert.deepEqual(result, {
    start: '10:45',
    end: '12:00',
    headline: "Walk to Zhanqiao Pier, Qingdao's most iconic landmark.",
    description: 'Enjoy the view of the red lighthouse and sea.',
    location: 'Zhanqiao, Shinan District',
  });
});

test('single-time and time-range bullets are supported without inventing end time', () => {
  assert.equal(
    parseItineraryActivity('- 04:30: Early morning walk at Jingshan Park. (Dongcheng District)')
      ?.end,
    undefined,
  );
  assert.equal(
    parseItineraryActivity('6:00 - 9:00: Explore a museum. (Central district)')?.start,
    '06:00',
  );
  assert.equal(parseItineraryActivity('9:00 – 12:00: Explore an attraction.')?.end, '12:00');
  assert.equal(parseItineraryActivity('19:30—20:30: Dinner.')?.end, '20:30');
});

test('fullwidth time delimiters work without changing the original itinerary', () => {
  assert.equal(parseItineraryActivity('10:45\uFF1AExplore the waterfront.')?.start, '10:45');
});

test('non-activity prose and metadata cannot be mistaken for timed activities', () => {
  assert.equal(parseItineraryActivity('Budget: moderate'), null);
  assert.equal(parseItineraryActivity('- A list item without a time'), null);
  assert.equal(parseItineraryActivity('25:90: Invalid time'), null);
  assert.deepEqual(parseDayMetadata('Overnight base: Dongcheng District'), {
    label: 'Overnight base',
    value: 'Dongcheng District',
  });
});

test('pipe-separated Why, Transfer, Check, Duration and costs become individual facts', () => {
  const original =
    'Why: A quintessential Qingdao experience. | Transfer: Short taxi ride from Badaguan. | Check: Check for temporary closures. | Allow about 75 minutes | Cost level: free (indicative) | Estimated cost: Free entry (unverified)';
  assert.deepEqual(parseItineraryFacts(original), [
    { label: 'Why', value: 'A quintessential Qingdao experience.' },
    { label: 'Transfer', value: 'Short taxi ride from Badaguan.' },
    { label: 'Check', value: 'Check for temporary closures.' },
    { label: 'Duration', value: 'About 75 minutes' },
    { label: 'Cost level', value: 'free' },
    { label: 'Estimated cost', value: 'Free entry' },
  ]);
  assert.match(original, /\(unverified\)/);
});

test('estimated prices retain their original numbers', () => {
  assert.equal(parseItineraryFact('Estimated cost: ¥60-¥100 (unverified)')?.value, '¥60-¥100');
  assert.equal(
    cleanPresentationText('Estimated cost: Free entry (unverified)'),
    'Estimated cost: Free entry',
  );
});

test('unknown facts are not silently reinterpreted or lost', () => {
  assert.equal(
    parseItineraryFacts('Source: Official booking page | Ticket: reservation required'),
    null,
  );
  assert.equal(parseItineraryFact('Best coffee: local'), null);
  assert.ok(isItinerarySectionLine('Logistics: Use the metro to get around.'));
});
