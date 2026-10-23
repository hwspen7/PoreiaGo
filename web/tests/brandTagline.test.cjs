const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join, dirname } = require('node:path');
const Module = require('node:module');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

const root = join(__dirname, '..');
const locale = JSON.parse(readFileSync(join(root, 'public/i18n/brand-taglines.json'), 'utf8'));
const entries = locale.taglines;
const codes = ['en', 'el', 'zh-Hant', 'es', 'de', 'ja', 'ko', 'fr', 'it', 'ru'];
assert.deepEqual(entries.map(entry => entry.code), codes);
assert.equal(entries.length, 10);
assert.equal(new Set(entries.map(entry => entry.code)).size, 10);
assert.equal(entries[0].text, 'Your journey starts here.');
assert.equal(entries[2].text, '\u4f60\u7684\u65c5\u7a0b\uff0c\u5f9e\u9019\u88e1\u958b\u59cb\u3002');
for (const entry of entries) assert.ok(entry.text && entry.language);
assert.doesNotMatch(readFileSync(join(root, 'public/i18n/brand-taglines.json'), 'utf8'), /[\u4e00-\u9fff]/);

const entryPoint = join(root, 'src/brandTagline.tsx');
const mod = new Module(entryPoint, module);
mod.filename = entryPoint;
mod.paths = Module._nodeModulePaths(dirname(entryPoint));
const previousCssHandler = require.extensions['.css'];
require.extensions['.css'] = () => { };
try {
  const source = readFileSync(entryPoint, 'utf8').replaceAll('import.meta.env.BASE_URL', '"/"');
  const out = ts.transpileModule(source, {
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  mod._compile(out, entryPoint);
} finally {
  if (previousCssHandler) require.extensions['.css'] = previousCssHandler;
  else delete require.extensions['.css'];
}

const { shuffledLanguageCodes, BrandTagline } = mod.exports;
const testShuffle = random => {
  let previous = 'en';
  let current = shuffledLanguageCodes(codes.slice(1), previous, random);
  assert.equal(current.length, 9);
  assert.ok(!current.includes('en'));
  assert.equal(new Set(current).size, 9);
  for (const code of current) {
    assert.notEqual(previous, code);
    previous = code;
  }
  for (let round = 0; round < 30; round++) {
    current = shuffledLanguageCodes(codes, previous, random);
    assert.equal(current.length, 10);
    assert.equal(new Set(current).size, 10);
    assert.deepEqual([...current].sort(), [...codes].sort());
    for (const code of current) {
      assert.notEqual(previous, code);
      previous = code;
    }
  }
};
testShuffle(() => 0);
testShuffle(() => 0.9999);
testShuffle(() => 0.5);
let seeded = 1234567;
testShuffle(() => ((seeded = (seeded * 48271) % 2147483647) / 2147483647));

const greek = renderToStaticMarkup(React.createElement(BrandTagline, { entry: entries[1], placement: 'sidebar' }));
assert.match(greek, /lang="el"/);
assert.match(greek, /brand-tagline--sidebar/);
assert.match(greek, /\u03a4\u03bf \u03c4\u03b1\u03be\u03af\u03b4\u03b9/);
const tc = renderToStaticMarkup(React.createElement(BrandTagline, { entry: entries[2], placement: 'welcome' }));
assert.match(tc, /lang="zh-Hant"/);
assert.match(tc, /brand-tagline--welcome/);
const moduleSource = readFileSync(entryPoint, 'utf8');
assert.match(moduleSource, /SWITCH_INTERVAL_MS\s*=\s*60_000/);
assert.match(moduleSource, /setInterval\(/);
assert.match(moduleSource, /clearInterval\(/);
assert.match(moduleSource, /FALLBACK\.code/);
const css = readFileSync(join(root, 'src/brandTagline.css'), 'utf8');
assert.match(css, /prefers-reduced-motion/);
assert.match(css, /400ms/);
assert.match(css, /min-height: 43px/);
const main = readFileSync(join(root, 'src/main.tsx'), 'utf8');
assert.match(main, /const brandTagline = useBrandTagline\(\)/);
assert.match(main, /BrandTagline entry=\{brandTagline\} placement="sidebar"/);
assert.match(main, /BrandTagline entry=\{brandTagline\} placement="welcome"/);
assert.match(main, /<span>PoreiaGo<\/span><ArrowRight/);
assert.doesNotMatch(main, /\bAligo\b/);
const html = readFileSync(join(root, 'index.html'), 'utf8');
assert.match(html, /<title>PoreiaGo \| Your journey starts here\.<\/title>/);
console.log('PoreiaGo localization, shuffle bag, SSR rendering, and branding: assertions passed.');
