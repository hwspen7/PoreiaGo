const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join, dirname } = require('node:path');
const Module = require('node:module');
const ts = require('typescript');

const entry = join(__dirname, '..', '..', 'src', 'lib/sessionIdentity.ts');
const file = new Module(entry, module);
file.filename = entry;
file.paths = Module._nodeModulePaths(dirname(entry));
file._compile(
  ts.transpileModule(readFileSync(entry, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText,
  entry,
);
const { getOrCreateSessionId, rememberSessionId, createSessionId } = file.exports;
const db = new Map();
const storage = {
  getItem: (k) => db.get(k) ?? null,
  setItem: (k, v) => {
    db.set(k, v);
  },
};
const first = getOrCreateSessionId(storage);
assert.match(first, /^session-[a-z0-9-]{12,64}$/i);
rememberSessionId(first, storage);
assert.equal(getOrCreateSessionId(storage), first, 'Reload should reuse the active ID');
const second = createSessionId();
assert.notEqual(first, second, 'New journey should have a distinct ID');
rememberSessionId(second, storage);
assert.equal(
  getOrCreateSessionId(storage),
  second,
  'Switching journeys should persist the selected ID',
);
rememberSessionId('invalid', storage);
assert.equal(getOrCreateSessionId(storage), second, 'Invalid IDs cannot overwrite the active ID');
console.log('Session identity persistence: assertions passed.');
