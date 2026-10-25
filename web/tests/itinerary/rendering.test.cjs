// Smoke test the real React renderer without a browser or Vite dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

for (const extension of ['.ts', '.tsx']) {
  require.extensions[extension] = (module, filename) => {
    const source = fs.readFileSync(filename, 'utf8');
    const code = ts.transpileModule(source, {
      compilerOptions: {
        jsx: ts.JsxEmit.ReactJSX,
        target: ts.ScriptTarget.ES2022,
        module: ts.ModuleKind.CommonJS,
      },
    }).outputText;
    module._compile(code, filename);
  };
}
require.extensions['.css'] = () => {};

const { FormattedAnswer } = require('../../src/features/itinerary/FormattedAnswer.tsx');
const userMessage = [
  '### Day 1: Coastal Heritage',
  'Theme: Heritage',
  'Area: Shinan District',
  'City: Qingdao',
  'Overnight base: Downtown',
  '',
  "- 10:45 - 12:00: Walk to Zhanqiao Pier, Qingdao's most iconic landmark. Enjoy the view of the red lighthouse and the sea. (Zhanqiao, Shinan District)",
  '',
  "Why: A quintessential Qingdao experience, offering stunning coastal views and a strong sense of the city's maritime history. | Transfer: Short taxi ride or walk from Badaguan (approx. 15-20 mins). | Check: Check for any temporary closures or special events. | Allow about 75 minutes | Cost level: free (indicative) | Estimated cost: Free entry (unverified)",
  '',
  'Logistics: Stay flexible with local transit.',
].join('\n');

const markup = renderToStaticMarkup(React.createElement(FormattedAnswer, { content: userMessage }));
assert.match(markup, /itinerary-activity/);
assert.match(markup, /itinerary-time/);
assert.match(markup, /10:45/);
assert.match(markup, /12:00/);
assert.match(markup, /itinerary-activity-title/);
assert.match(markup, /Enjoy the view of the red lighthouse/);
assert.match(markup, /Zhanqiao, Shinan District/);
for (const label of ['Why', 'Transfer', 'Check', 'Duration', 'Cost level', 'Estimated cost']) {
  assert.match(markup, new RegExp(`<dt>${label}</dt>`));
}
assert.doesNotMatch(markup, /\(unverified\)|\(indicative\)/);
assert.match(markup, /Free entry/);
assert.match(markup, /Logistics/);
assert.ok(markup.indexOf('<dt>Why</dt>') < markup.indexOf('<dt>Transfer</dt>'));

const draft = [
  'Working draft 1 saved. You can continue editing this plan.',
  'Review needed:',
  '- Day 1: incoming transfer/buffer...',
  'Trip: MODEL_GENERATION_FAILED:OUTPUT_TRUNCATED',
  '',
  'Saved status does not verify opening hours, ticket availability or event times.',
  '',
  userMessage,
].join('\n');
const draftMarkup = renderToStaticMarkup(React.createElement(FormattedAnswer, { content: draft }));
assert.doesNotMatch(draftMarkup, /Review needed|MODEL_GENERATION_FAILED/);
assert.match(draftMarkup, /itinerary-activity/);

console.log('React SSR rendering tests passed.');
