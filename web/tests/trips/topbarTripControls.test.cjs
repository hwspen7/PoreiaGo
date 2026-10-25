const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join, dirname } = require('node:path');
const Module = require('node:module');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

const src = join(__dirname, '..', '..', 'src');
const entry = join(src, 'features/trips/TopbarTripControls.tsx');
const moduleUnderTest = new Module(entry, module);
moduleUnderTest.filename = entry;
moduleUnderTest.paths = Module._nodeModulePaths(dirname(entry));
// Handle the adjacent TypeScript utility after moving the component to features/trips.
const previousTsHandler = require.extensions['.ts'];
require.extensions['.ts'] = (loaded, filename) => {
  loaded._compile(ts.transpileModule(readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText, filename);
};
const previousCssHandler = require.extensions['.css'];
require.extensions['.css'] = () => {};
try {
  const generated = ts.transpileModule(readFileSync(entry, 'utf8'), {
    compilerOptions: {
      jsx: ts.JsxEmit.ReactJSX,
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  moduleUnderTest._compile(generated, entry);
} finally {
  if (previousCssHandler) require.extensions['.css'] = previousCssHandler;
  else delete require.extensions['.css'];
  if (previousTsHandler) require.extensions['.ts'] = previousTsHandler;
  else delete require.extensions['.ts'];
}

const { TopbarTripControls, CurrentTripTitle } = moduleUnderTest.exports;
const props = {
  activeTrip: { version: 2, itinerary: { title: 'Qingdao Coastal Day', days: [{}] } },
  workingDraft: null,
  draftHistory: [{ draft_version: 1 }],
  savedSnapshots: [{ id: 9, source_status: 'verified', source_version: 2 }],
  historyPreview: { journey: 'a', label: 'saved #9', content: 'preview content' },
  sessionId: 'a',
  loading: false,
  onSave() {},
  onPreview() {},
  async onLoadTitle() {
    return 'Real archived title';
  },
};
const html = renderToStaticMarkup(React.createElement(TopbarTripControls, props));
const title = renderToStaticMarkup(React.createElement(CurrentTripTitle, props));
assert.match(title, /Qingdao Coastal Day/);
assert.doesNotMatch(title, /Verified v2/);
assert.doesNotMatch(title, /1 days/);
assert.doesNotMatch(html, /Qingdao Coastal Day/);
assert.match(html, /aria-label="Current itinerary already saved"/);
assert.match(html, /Saved itinerary versions, 2 items/);
assert.match(html, /Saved itinerary #9/);
assert.match(html, /Itinerary history/);
assert.match(html, /Working draft #1/);
assert.match(html, /preview content/);
const draftHtml = renderToStaticMarkup(
  React.createElement(TopbarTripControls, {
    ...props,
    workingDraft: { draft_version: 3, itinerary: { title: 'Revised Day', days: [{}, {}] } },
  }),
);
const draftTitle = renderToStaticMarkup(
  React.createElement(CurrentTripTitle, {
    activeTrip: props.activeTrip,
    workingDraft: { draft_version: 3, itinerary: { title: 'Revised Day', days: [{}, {}] } },
  }),
);
assert.match(draftTitle, /Revised Day/);
assert.doesNotMatch(draftTitle, /Draft 3/);
assert.doesNotMatch(draftTitle, /needs review/);
assert.doesNotMatch(draftHtml, /Revised Day/);
const emptyHtml = renderToStaticMarkup(
  React.createElement(TopbarTripControls, {
    ...props,
    activeTrip: null,
    workingDraft: null,
    savedSnapshots: [],
    draftHistory: [],
  }),
);
assert.equal(emptyHtml, '');
const main = readFileSync(join(src, 'main.tsx'), 'utf8');
assert.ok(main.includes('<TopbarTripControls'));
assert.ok(main.includes('<CurrentTripTitle'));
assert.match(main, /className="topbar-nav-trail"/);
assert.match(main, /<span>PoreiaGo<\/span>\s*<ArrowRight size=\{13\}/);
assert.match(main, /className="topbar-page-summary"/);
assert.match(
  main,
  /skills: 'Skill Catalog',\s*memory: 'Travel Preferences',\s*knowledge: 'Travel Library',\s*settings: 'Models & Data Services'/,
);
assert.doesNotMatch(main, /Knowledge Sources|Models & Data Services/);
const libraryPage = readFileSync(join(src, 'features/library/KnowledgeWorkspace.tsx'), 'utf8');
const servicesPage = readFileSync(join(src, 'features/providers/ModelsProvidersOverview.tsx'), 'utf8');
assert.match(libraryPage, /<h1>Travel Library<\/h1>/);
assert.match(servicesPage, /<h1>Models & Data Services<\/h1>/);
assert.ok(!main.includes('<span className="crumb">PoreiaGo'));
const css = readFileSync(join(src, 'features/trips/topbarTripControls.css'), 'utf8');
assert.match(css, /\.topbar-nav-trail\s*\{[^}]*align-items:\s*center/s);
assert.match(css, /\.top-left > \.icon-button\s*\{[^}]*align-items:\s*center/s);
assert.ok(main.includes('side-status side-connection'));
assert.ok(main.includes('onLoadTitle={loadTripVersionTitle}'));
assert.ok(!main.includes('className="system-indicator"'));
assert.ok(!main.includes('<select className="locale-select"'));
assert.ok(!main.includes('languagePreference'));
const composer = main.slice(main.indexOf('<div className="composer-area">'));
assert.ok(!composer.includes('active-trip-banner'));
assert.ok(!composer.includes('saved-trip-history'));
console.log('Header layout and real saved titles: assertions passed.');
