const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const src = fs.readFileSync(path.join(__dirname, '../src/itineraryDisplayLabels.ts'), 'utf8');
const labels = {Why:'Planning rationale',Transfer:'Getting there',Check:'Before you go',Booking:'Reservations',Duration:'Time needed','Cost level':'Cost category'};
const semantic = /(<dt\b[^>]*>)(Why|Transfer|Check|Booking|Duration|Cost level)(<\/dt>)/gi;
const format = s => s.replace(semantic, (_m, opening, key, closing) => opening + labels[key] + closing);
test('rename only the rendered fact labels, preserving the original itinerary', () => {
  const html = '<div><dt>Why</dt><dd>Why visit this venue?</dd><dt>Transfer</dt><dd>Walk here</dd><dt>Cost level</dt><dd>low</dd></div>';
  assert.equal(format(html), '<div><dt>Planning rationale</dt><dd>Why visit this venue?</dd><dt>Getting there</dt><dd>Walk here</dd><dt>Cost category</dt><dd>low</dd></div>');
  assert.match(src, /formatRenderedItineraryLabels/);
});
test('existing formatter receives unchanged message content', () => {
  const main = fs.readFileSync(path.join(__dirname, '../src/main.tsx'),'utf8');
  const react = fs.readFileSync(path.join(__dirname, '../src/ItineraryAnswer.tsx'),'utf8');
  const exportSource = fs.readFileSync(path.join(__dirname, '../src/exportConversation.tsx'),'utf8');
  assert.match(main, /<ItineraryAnswer content=\{m\.content\}/);
  assert.match(react, /<FormattedAnswer content=\{content\}/);
  assert.match(exportSource, /content: message\.content/);
  assert.match(exportSource, /formatRenderedItineraryLabels\(renderToStaticMarkup/);
});
test('long labels never split into single characters in the print stylesheet', () => {
 const exportSource=fs.readFileSync(path.join(__dirname, '../src/exportConversation.tsx'),'utf8');
 assert.match(exportSource,/white-space: nowrap/);
 assert.match(exportSource,/word-break: normal/);
 assert.match(exportSource,/grid-template-columns: 125px minmax\(0, 1fr\)/);
});
