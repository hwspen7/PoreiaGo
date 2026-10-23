const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join, dirname } = require('node:path');
const Module = require('node:module');
const ts = require('typescript');

const entry = join(__dirname, '..', 'src', 'exportConversation.tsx');
const file = new Module(entry, module);
file.filename = entry;
file.paths = Module._nodeModulePaths(dirname(entry));
// These stubs make the export module testable without mounting a browser.
// The real client renders FormattedAnswer through react-dom/server.browser.
file.require = specifier => {
  if (specifier === 'react') return { createElement: (type, props) => ({ type, props }) };
  if (specifier === 'react-dom/server') return { renderToStaticMarkup: node => `<div class="formatted-answer">${node.props.content.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}</div>` };
  if (specifier === './FormattedAnswer') return { FormattedAnswer: () => {} };
  return Module.prototype.require.call(file, specifier);
};
file._compile(ts.transpileModule(readFileSync(entry, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, entry);
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
console.log('HTML export escaping and print styles: assertions passed.');
