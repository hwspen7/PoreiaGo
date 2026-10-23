import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { FormattedAnswer } from './FormattedAnswer';

export type ExportMessage = { role: 'user' | 'assistant'; content: string };
type ExportInput = {
  title: string;
  messages: readonly ExportMessage[];
  status?: string;
  exportedAt?: Date;
};

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[character] ?? character);
}

function filenameStem(title: string): string {
  const name = title.normalize('NFKC').replace(/[<>:"/\\|?*\u0000-\u001f]/g, '-')
    .replace(/\s+/g, ' ').replace(/[.\s]+$/g, '').trim().slice(0, 90);
  return name || 'PoreiaGo-Journey';
}

export function buildConversationHtml({ title, messages, status = 'Conversation', exportedAt = new Date() }: ExportInput): string {
  const readableDate = exportedAt.toISOString().slice(0, 10);
  const documentTitle = escapeHtml(title);
  const sections = messages.map((message, index) => {
    const roleLabel = message.role === 'user' ? 'Traveler' : 'PoreiaGo';
    const html = message.role === 'user'
      ? `<p class="traveler-text">${escapeHtml(message.content)}</p>`
      : renderToStaticMarkup(createElement(FormattedAnswer, { content: message.content }));
    return `<section class="export-message export-${message.role}" aria-label="${roleLabel} message ${index + 1}"><div class="message-speaker">${roleLabel}</div>${html}</section>`;
  }).join('\n');
  // No external assets, scripts, fonts or tracking. The formatted assistant
  // output uses React's built-in HTML escaping and http(s)-only links.
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="referrer" content="no-referrer" />
<title>${documentTitle} - PoreiaGo</title>
<style>
:root { color-scheme: light; font-family: system-ui, -apple-system, "Segoe UI", "Noto Sans", sans-serif; color: #19252f; background: #fff; }
* { box-sizing: border-box; }
body { max-width: 860px; margin: 0 auto; padding: 48px 38px 90px; font-size: 14px; line-height: 1.7; overflow-wrap: anywhere; }
.export-heading { border-bottom: 2px solid #d9e6e5; padding-bottom: 23px; margin-bottom: 28px; }
.export-brand { font-size: 12px; letter-spacing: .14em; font-weight: 750; text-transform: uppercase; color: #236e64; }
h1 { font-size: 27px; line-height: 1.25; margin: 10px 0 13px; overflow-wrap: anywhere; }
.export-meta { color: #697985; font-size: 12px; }
.export-message { padding: 19px 0 20px; border-bottom: 1px solid #e5ebee; break-inside: auto; }
.message-speaker { color: #236e64; font-size: 10px; font-weight: 750; letter-spacing: .13em; text-transform: uppercase; margin-bottom: 7px; }
.export-user { padding-left: 20px; }
.export-user .message-speaker { color: #697985; }
.traveler-text { padding: 12px 16px; border-radius: 8px; background: #f3f6f8; white-space: pre-wrap; margin: 0; }
.formatted-answer h3 { margin: 21px 0 8px; line-height: 1.35; font-size: 18px; break-after: avoid; }
.formatted-answer p { margin: 0 0 12px; }
.formatted-answer ul, .formatted-answer ol { padding-left: 24px; margin: 7px 0 14px; }
.formatted-answer li { margin: 4px 0; }
.formatted-answer hr { border: 0; border-top: 1px solid #dae5e3; margin: 18px 0; }
.formatted-answer pre { padding: 13px; background: #f3f6f8; white-space: pre-wrap; border-radius: 7px; }
.formatted-answer code { font-family: ui-monospace, Consolas, monospace; white-space: pre-wrap; }
a { color: #146a87; text-decoration: underline; }
.itinerary-day-title { padding: 9px 12px; border-left: 4px solid #2c8876; background: #edf6f4; break-after: avoid; }
.itinerary-day-metadata, .itinerary-facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 5px 13px; margin: 9px 0 17px; }
.itinerary-day-metadata > div, .itinerary-facts > div { display: flex; gap: 6px; font-size: 12px; }
.itinerary-day-metadata dt, .itinerary-facts dt { color: #60727c; font-weight: 700; }
.itinerary-day-metadata dd, .itinerary-facts dd { margin: 0; }
.itinerary-activity { margin: 12px 0; padding: 11px 0 11px 18px; border-left: 2px solid #c3d9d4; break-inside: avoid; }
.itinerary-activity-header { display: flex; gap: 14px; align-items: flex-start; }
.itinerary-time { flex: 0 0 72px; font-size: 12px; color: #286f62; font-weight: 750; }
.itinerary-time-separator { margin: 0 3px; }
.itinerary-activity-title-area { flex: 1; min-width: 0; }
.itinerary-activity-title { font-size: 13px; line-height: 1.5; margin: 0; }
.itinerary-activity-location { font-size: 11px; color: #697985; margin-top: 3px; }
.itinerary-activity-description { margin: 7px 0 0 86px !important; }
.itinerary-activity .itinerary-facts { margin: 8px 0 0 86px; }
.itinerary-section-note { font-weight: 500; }
.export-footer { margin-top: 24px; color: #75828d; font-size: 10px; }
@page { size: A4; margin: 15mm; }
@media print {
  html, body { background: white !important; }
  body { padding: 0; max-width: none; font-size: 10.5pt; print-color-adjust: exact; -webkit-print-color-adjust: exact; }
  h1 { font-size: 22pt; }
  .export-heading { margin-bottom: 12pt; }
  .export-message { padding: 12pt 0; }
  a { color: #146a87; }
}
@media (max-width: 550px) {
  body { padding: 25px 17px; }
  .itinerary-day-metadata, .itinerary-facts { grid-template-columns: 1fr; }
  .itinerary-activity-description, .itinerary-activity .itinerary-facts { margin-left: 0 !important; }
}
</style>
</head>
<body>
<header class="export-heading"><div class="export-brand">PoreiaGo / Journey export</div>
<h1>${documentTitle}</h1><div class="export-meta">${escapeHtml(status)} · Exported ${readableDate} · ${messages.length} messages</div></header>
<main>${sections}</main>
<footer class="export-footer">Travel information and availability should be independently verified with the relevant providers.</footer>
</body>
</html>`;
}

export function downloadConversationHtml(html: string, title: string): void {
  const objectUrl = URL.createObjectURL(new Blob([html], { type: 'text/html;charset=utf-8' }));
  const anchor = document.createElement('a');
  anchor.href = objectUrl;
  anchor.download = `${filenameStem(title)}.html`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
}

/** A browser's native Save as PDF preserves Unicode and selectable text. */
export function printConversationPdf(html: string): void {
  // Open synchronously from the actual click to avoid pop-up blockers.
  const popup = window.open('', '_blank');
  if (!popup) throw new Error('The browser blocked the PDF print window. Please allow pop-ups for this site.');
  popup.document.open();
  popup.document.write(html);
  popup.document.close();
  const startPrint = () => {
    const invokePrint = () => { if (!popup.closed) { popup.focus(); popup.print(); } };
    const fontsReady = popup.document.fonts?.ready;
    if (fontsReady) void fontsReady.then(invokePrint, invokePrint);
    else invokePrint();
  };
  if (popup.document.readyState === 'complete') startPrint();
  else popup.addEventListener('load', startPrint, { once: true });
}
