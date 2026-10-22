import type { ReactNode } from 'react';

/** A dependency-free, escaped Markdown subset for model-generated prose. */
function inline(text: string, keyBase: string): ReactNode[] {
  const tokens: ReactNode[] = [];
  const pattern = /(\*\*[^*\n]+\*\*|`[^`\n]+`|\[[^\]\n]+\]\(https?:\/\/[^)\s]+\))/g;
  let previous = 0, index = 0;
  for (const match of text.matchAll(pattern)) {
    const start = match.index ?? 0;
    if (start > previous) tokens.push(text.slice(previous, start));
    const value = match[0];
    if (value.startsWith('**')) tokens.push(<strong key={`${keyBase}-${index++}`}>{value.slice(2, -2)}</strong>);
    else if (value.startsWith('`')) tokens.push(<code key={`${keyBase}-${index++}`}>{value.slice(1, -1)}</code>);
    else {
      const parts = /^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/.exec(value);
      if (parts) tokens.push(<a key={`${keyBase}-${index++}`} href={parts[2]} target="_blank" rel="noopener noreferrer">{parts[1]}</a>);
      else tokens.push(value);
    }
    previous = start + value.length;
  }
  if (previous < text.length) tokens.push(text.slice(previous));
  return tokens;
}

export function FormattedAnswer({ content }: { content: string }) {
  const lines = content.split(/\r?\n/);
  const blocks: ReactNode[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (/^```/.test(line)) {
      i++; const snippet: string[] = [];
      while (i < lines.length && !/^```/.test(lines[i])) snippet.push(lines[i++]);
      if (i < lines.length) i++;
      blocks.push(<pre key={`c-${i}`}><code>{snippet.join('\n')}</code></pre>);
      continue;
    }
    const heading = /^(#{1,4})\s+(.+)/.exec(line);
    if (heading) { blocks.push(<h3 key={`h-${i}`}>{inline(heading[2], `h-${i}`)}</h3>); i++; continue; }
    if (/^\s*[-*]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const items: ReactNode[] = [];
      while (i < lines.length && (ordered ? /^\s*\d+[.)]\s+/.test(lines[i]) : /^\s*[-*]\s+/.test(lines[i]))) {
        items.push(<li key={i}>{inline(lines[i].replace(/^\s*(?:[-*]|\d+[.)])\s+/, ''), `li-${i}`)}</li>); i++;
      }
      blocks.push(ordered ? <ol key={`l-${i}`}>{items}</ol> : <ul key={`l-${i}`}>{items}</ul>);
      continue;
    }
    if (/^[-*_]{3,}\s*$/.test(line.trim())) { blocks.push(<hr key={`r-${i}`} />); i++; continue; }
    const body: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s+|```|\s*[-*]\s+|\s*\d+[.)]\s+|[-*_]{3,}\s*$)/.test(lines[i])) body.push(lines[i++]);
    if (body.length) blocks.push(<p key={`p-${i}`}>{body.map((part, j) => <span key={j}>{j > 0 ? <br /> : null}{inline(part, `p-${i}-${j}`)}</span>)}</p>);
    else i++;
  }
  return <div className="answer-text formatted-answer">{blocks}</div>;
}
