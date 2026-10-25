const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const ts = require('typescript');
const entry = path.join(__dirname, '../../src/features/export/exportMessageFilter.ts');
const loaded = new Module(entry, module);
loaded.filename = entry;
loaded._compile(
  ts.transpileModule(readFileSync(entry, 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText,
  entry,
);
const { selectJourneyExportMessages, isJourneyExportMessage } = loaded.exports;
const messages = [
  { role: 'user', content: 'Plan a three-day trip to Beijing.' },
  { role: 'assistant', content: '### Day 1: Forbidden City\n- 09:00: Visit the palace' },
  { role: 'user', content: 'Save my itinerary.' },
  {
    role: 'assistant',
    content:
      'Saved verified itinerary as snapshot #2. You can continue editing this plan. Prices, opening hours and availability still require verification.',
  },
  { role: 'user', content: 'save this itinerary' },
  {
    role: 'assistant',
    content:
      'Saved itinerary snapshot #7 (verified itinerary). This snapshot is retained while you continue editing.',
  },
  { role: 'user', content: 'Can I save my itinerary as a PDF?' },
  { role: 'assistant', content: 'You can export the itinerary as PDF.' },
  {
    role: 'assistant',
    content:
      'Saved verified itinerary as snapshot #8.\n### Day 2: Tiananmen Square\n- 10:00: Visit the square.',
  },
];
const filtered = selectJourneyExportMessages(messages);
assert.equal(filtered.length, 5);
assert.equal(filtered[0].content, messages[0].content);
assert.equal(filtered[1].content, messages[1].content);
assert.equal(filtered[2].content, messages[6].content);
assert.equal(filtered[3].content, messages[7].content);
assert.equal(filtered[4].content, messages[8].content);
assert.equal(isJourneyExportMessage({ role: 'user', content: '   ' }), false);
console.log('Save-operation export filtering: assertions passed.');
