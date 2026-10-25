const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join, dirname } = require('node:path');
const Module = require('node:module');
const ts = require('typescript');

const entry = join(__dirname, '..', '..', 'src', 'features/export/exportConversation.tsx');
const filterEntry = join(__dirname, '..', '..', 'src', 'features/export/exportMessageFilter.ts');
const filterModule = new Module(filterEntry, module);
filterModule.filename = filterEntry;
filterModule.paths = Module._nodeModulePaths(dirname(filterEntry));
filterModule._compile(
  ts.transpileModule(readFileSync(filterEntry, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText,
  filterEntry,
);
const file = new Module(entry, module);
file.filename = entry;
file.paths = Module._nodeModulePaths(dirname(entry));
// These stubs make the export module testable without mounting a browser.
// The real client renders FormattedAnswer through react-dom/server.browser.
file.require = (specifier) => {
  if (specifier === 'react') return { createElement: (type, props) => ({ type, props }) };
  if (specifier === 'react-dom/server')
    return {
      renderToStaticMarkup: (node) =>
        `<div class="formatted-answer">${node.props.content.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')}</div>`,
    };
  if (specifier === '../itinerary/itineraryDisplayLabels') return { formatRenderedItineraryLabels: (html) => html };
  if (specifier === '../itinerary/FormattedAnswer') return { FormattedAnswer: () => {} };
  if (specifier === './exportMessageFilter') return filterModule.exports;
  return Module.prototype.require.call(file, specifier);
};
file._compile(
  ts.transpileModule(readFileSync(entry, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText,
  entry,
);
const { buildConversationHtml } = file.exports;
const output = buildConversationHtml({
  title: 'Qingdao <script>alert(1)</script>',
  status: '<img src=x>',
  exportedAt: new Date('2026-10-22T12:00:00Z'),
  messages: [
    { role: 'user', content: 'Show <script>bad()</script> & tips' },
    { role: 'assistant', content: '### Day 1\n- 09:00: Visit the coast' },
  ],
});
assert.match(output, /<!DOCTYPE html>/);
assert.match(output, /Exported 2026-10-22/);
assert.match(output, /2 messages/);
assert.match(output, /Qingdao &lt;script&gt;alert\(1\)&lt;\/script&gt;/);
assert.match(output, /Show &lt;script&gt;bad\(\)&lt;\/script&gt; &amp; tips/);
assert.doesNotMatch(output, /<script>/);
assert.match(output, /@media print/);
assert.match(output, /formatted-answer/);
assert.match(output, /<meta charset="utf-8"/);
const savedOutput = buildConversationHtml({
  title: 'Visiting \u5317\u4eac and \u0391\u03b8\u03ae\u03bd\u03b1',
  messages: [
    {
      role: 'user',
      content:
        'Plan a trip to \u5317\u4eac, \u0391\u03b8\u03ae\u03bd\u03b1, \u041c\u043e\u0441\u043a\u0432\u0430, \u6771\u4eac and \uc11c\uc6b8.',
    },
    {
      role: 'assistant',
      content: '### Day 1\n- 09:00: Visit \u6771\u4eac and the \u5317\u4eac museum \ud83d\ude86',
    },
    { role: 'user', content: 'Save my itinerary.' },
    {
      role: 'assistant',
      content:
        'Saved verified itinerary as snapshot #2. You can continue editing this plan. Prices, opening hours and availability still require verification.',
    },
    { role: 'user', content: 'Save my itinerary.' },
    {
      role: 'assistant',
      content:
        'Saved verified itinerary as snapshot #3. You can continue editing this plan. Prices, opening hours and availability still require verification.',
    },
    {
      role: 'assistant',
      content:
        'Saved itinerary snapshot #6 (verified itinerary). This snapshot is retained while you continue editing.',
    },
  ],
});
assert.match(savedOutput, /2 messages/);
assert.doesNotMatch(savedOutput, /Save my itinerary/);
assert.doesNotMatch(savedOutput, /Saved verified itinerary as snapshot/);
assert.doesNotMatch(savedOutput, /Saved itinerary snapshot/);
for (const text of [
  '\u5317\u4eac',
  '\u0391\u03b8\u03ae\u03bd\u03b1',
  '\u041c\u043e\u0441\u043a\u0432\u0430',
  '\u6771\u4eac',
  '\uc11c\uc6b8',
  '\ud83d\ude86',
])
  assert.ok(savedOutput.includes(text), `UTF-8 value missing: ${text}`);
assert.match(savedOutput, /charset="utf-8"/);
assert.match(savedOutput, /Noto Sans CJK SC/);
assert.ok(savedOutput.includes('Visiting \u5317\u4eac and \u0391\u03b8\u03ae\u03bd\u03b1'));
console.log(
  'HTML export escaping, save-event filtering, Unicode and print styles: assertions passed.',
);
