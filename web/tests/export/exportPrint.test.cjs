const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join, dirname } = require('node:path');
const Module = require('node:module');
const ts = require('typescript');
const root = join(__dirname, '..', '..', 'src');
const entry = join(root, 'features/export/exportConversation.tsx');
const loadTS = (filename) => {
  const loaded = new Module(filename, module);
  loaded.filename = filename;
  loaded.paths = Module._nodeModulePaths(dirname(filename));
  loaded._compile(
    ts.transpileModule(readFileSync(filename, 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    }).outputText,
    filename,
  );
  return loaded.exports;
};
const filter = loadTS(join(root, 'features/export/exportMessageFilter.ts'));
const loaded = new Module(entry, module);
loaded.filename = entry;
loaded.paths = Module._nodeModulePaths(dirname(entry));
loaded.require = (spec) => {
  if (spec === 'react') return { createElement: () => ({}) };
  if (spec === 'react-dom/server') return { renderToStaticMarkup: () => '' };
  if (spec === '../itinerary/itineraryDisplayLabels') return { formatRenderedItineraryLabels: (html) => html };
  if (spec === '../itinerary/FormattedAnswer') return { FormattedAnswer: () => {} };
  if (spec === './exportMessageFilter') return filter;
  return Module.prototype.require.call(loaded, spec);
};
loaded._compile(
  ts.transpileModule(readFileSync(entry, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText,
  entry,
);
const { printConversationPdf } = loaded.exports;
let interval;
let printCount = 0;
let popupHref = '';
let revoked = 0;
let documentBlob;
const pdfPopup = {
  closed: false,
  location: {
    get href() {
      return popupHref;
    },
  },
  document: { readyState: 'complete', fonts: { ready: Promise.resolve() } },
  focus() {},
  print() {
    printCount++;
  },
  addEventListener(event, cb) {
    if (event === 'afterprint') this.afterPrint = cb;
  },
};
const priorWindow = global.window;
const priorURL = global.URL;
const priorBlob = global.Blob;
global.window = {
  open(url) {
    popupHref = url;
    return pdfPopup;
  },
  setInterval(cb) {
    interval = cb;
    return 42;
  },
  clearInterval() {},
  setTimeout() {},
};
global.URL = {
  createObjectURL(blob) {
    documentBlob = blob;
    return 'blob:http://localhost/utf8-pdf';
  },
  revokeObjectURL() {
    revoked++;
  },
};
const sample = '<html><head><meta charset="utf-8"></head><body>\\u5317\\u4eac</body></html>';
printConversationPdf(sample);
assert.ok(documentBlob);
assert.match(documentBlob.type, /charset=utf-8/i);
assert.equal(printCount, 0);
interval();
Promise.resolve()
  .then(async () => {
    assert.equal(printCount, 1);
    assert.deepEqual(
      Array.from(new Uint8Array(await documentBlob.arrayBuffer()).slice(0, 3)),
      [0xef, 0xbb, 0xbf],
    );
    pdfPopup.afterPrint();
    assert.equal(revoked, 1);
    global.window = priorWindow;
    global.URL = priorURL;
    global.Blob = priorBlob;
    console.log('PDF export: UTF-8 Blob navigation and print lifecycle assertions passed.');
  })
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
