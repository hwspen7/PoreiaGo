import type { ReactNode } from 'react';
import { presentAnswer } from './presentAnswer';
import {
  cleanPresentationText,
  isItinerarySectionLine,
  parseDayMetadata,
  parseItineraryActivity,
  parseItineraryFacts,
  type ItineraryActivity,
  type ItineraryFact,
} from './itineraryPresentation';
import './fixedLayout.css';
import './itineraryPresentation.css';

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

function FactRows({ facts }: { facts: ItineraryFact[] }) {
  return <dl className="itinerary-facts">{facts.map((fact, i) => <div className="itinerary-fact" key={`${fact.label}-${i}`}>
    <dt>{fact.label}</dt><dd>{inline(fact.value, `fact-${i}`)}</dd>
  </div>)}</dl>;
}

function Activity({ activity, facts, extra, index }: {
  activity: ItineraryActivity;
  facts: ItineraryFact[];
  extra: string[];
  index: number;
}) {
  return <article className="itinerary-activity" aria-label={`Activity starting at ${activity.start}`}>
    <header className="itinerary-activity-header">
      <span className="itinerary-time">{activity.start}{activity.end ? <><span className="itinerary-time-separator">–</span>{activity.end}</> : null}</span>
      <div className="itinerary-activity-title-area">
        <h4 className="itinerary-activity-title">{inline(activity.headline, `activity-${index}`)}</h4>
        {activity.location ? <div className="itinerary-activity-location">{inline(activity.location, `location-${index}`)}</div> : null}
      </div>
    </header>
    {activity.description ? <p className="itinerary-activity-description">{inline(activity.description, `description-${index}`)}</p> : null}
    {extra.map((line, i) => <p className="itinerary-activity-description" key={`extra-${i}`}>{inline(line, `extra-${index}-${i}`)}</p>)}
    {facts.length ? <FactRows facts={facts} /> : null}
  </article>;
}

export function FormattedAnswer({ content }: { content: string }) {
  const lines = presentAnswer(content).replace(/\r\n?/g, '\n').split('\n');
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
    if (heading) {
      const day = /\bday\s+\d+\s*:/i.test(heading[2]);
      blocks.push(<h3 className={day ? 'itinerary-day-title' : undefined} key={`h-${i}`}>{inline(cleanPresentationText(heading[2]), `h-${i}`)}</h3>);
      i++;
      continue;
    }
    const activity = parseItineraryActivity(line);
    if (activity) {
      const index = i;
      const facts: ItineraryFact[] = [];
      const extra: string[] = [];
      i++;
      while (i < lines.length) {
        const detail = lines[i].trim();
        if (!detail) { i++; continue; }
        if (parseItineraryActivity(detail) || /^#{1,4}\s+/.test(detail) || /^```/.test(detail)
          || /^[-*_]{3,}\s*$/.test(detail) || isItinerarySectionLine(detail)
          || /^(?:[-*]|\d+[.)])\s+/.test(detail)) break;
        const parsed = parseItineraryFacts(detail);
        if (parsed) { facts.push(...parsed); i++; continue; }
        // Only attach a continuation line when it clearly belongs to this
        // activity; normal itinerary prose must remain a separate block.
        if (/^\s{2,}\S/.test(lines[i])) { extra.push(cleanPresentationText(detail)); i++; continue; }
        break;
      }
      blocks.push(<Activity activity={activity} facts={facts} extra={extra} index={index} key={`activity-${index}`} />);
      continue;
    }
    const dayMeta = parseDayMetadata(line);
    if (dayMeta) {
      const facts = [dayMeta];
      const start = i;
      i++;
      while (i < lines.length) {
        const next = parseDayMetadata(lines[i]);
        if (!next) break;
        facts.push(next); i++;
      }
      blocks.push(<dl key={`meta-${start}`} className="itinerary-day-metadata">{facts.map((fact, j) => <div key={j}>
        <dt>{fact.label}</dt><dd>{inline(fact.value, `meta-${start}-${j}`)}</dd>
      </div>)}</dl>);
      continue;
    }
    if (/^\s*[-*]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const items: ReactNode[] = [];
      while (i < lines.length && (ordered ? /^\s*\d+[.)]\s+/.test(lines[i]) : /^\s*[-*]\s+/.test(lines[i]))) {
        items.push(<li key={i}>{inline(cleanPresentationText(lines[i].replace(/^\s*(?:[-*]|\d+[.)])\s+/, '')), `li-${i}`)}</li>);
        i++;
      }
      blocks.push(ordered ? <ol key={`l-${i}`}>{items}</ol> : <ul key={`l-${i}`}>{items}</ul>);
      continue;
    }
    if (/^[-*_]{3,}\s*$/.test(line.trim())) { blocks.push(<hr key={`r-${i}`} />); i++; continue; }
    const factLine = parseItineraryFacts(line);
    if (factLine && factLine.length > 1) {
      blocks.push(<FactRows key={`fact-${i}`} facts={factLine} />);
      i++; continue;
    }
    const body: string[] = [];
    const start = i;
    while (i < lines.length && lines[i].trim()
      && !/^(#{1,4}\s+|```|\s*[-*]\s+|\s*\d+[.)]\s+|[-*_]{3,}\s*$)/.test(lines[i])
      && !parseItineraryActivity(lines[i]) && !parseDayMetadata(lines[i])) {
      body.push(cleanPresentationText(lines[i++]));
    }
    if (body.length) blocks.push(<p className={isItinerarySectionLine(body[0]) ? 'itinerary-section-note' : undefined} key={`p-${start}`}>{body.map((part, j) => {
      const label = /^(Logistics|Alternative|Budget|Tip|Budget approach|Arrival and departure)\s*:\s*(.*)$/i.exec(part);
      return <span key={j}>{j > 0 ? <br /> : null}{label ? <><strong>{label[1]}:</strong> {inline(label[2], `p-${start}-${j}`)}</> : inline(part, `p-${start}-${j}`)}</span>;
    })}</p>);
    else i++;
  }
  return <div className="answer-text formatted-answer">{blocks}</div>;
}
