const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const ts = require('typescript');
const filename = join(__dirname, '..', '..', 'src', 'features/trips/versionPreview.ts');
const Module = require('node:module');
const mod = new Module(filename, module);
mod._compile(
  ts.transpileModule(readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText,
  filename,
);
const { itineraryPreviewText } = mod.exports;
const trip = {
  title: 'Qingdao Coastal Day',
  summary: 'Historic walk',
  days: [
    {
      day: 1,
      title: 'Coastal Heritage',
      activities: [
        { start_time: '10:45', description: 'Visit Zhanqiao Pier.', area: 'Shinan' },
        { time: '12:30', description: 'Lunch by the sea.' },
      ],
    },
  ],
};
const rendered = itineraryPreviewText(trip);
assert.match(rendered, /Qingdao Coastal Day/);
assert.match(rendered, /10:45 — Visit Zhanqiao Pier. \(Shinan\)/);
assert.match(rendered, /12:30 — Lunch by the sea./);
assert.doesNotMatch(rendered, /"days"|"activities"/);
assert.match(itineraryPreviewText(null), /could not be displayed/);
assert.match(itineraryPreviewText({}), /No saved itinerary details/);
console.log('Readable saved itinerary previews: assertions passed.');
