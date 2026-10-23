/// <reference types="vite/client" />
import { useEffect, useState } from 'react';
import './brandTagline.css';

export type BrandTaglineEntry = { code: string; language: string; text: string };
const FALLBACK: BrandTaglineEntry = { code: 'en', language: 'English', text: 'Your journey starts here.' };
const LANGUAGE_CODES = ['en', 'el', 'zh-Hant', 'es', 'de', 'ja', 'ko', 'fr', 'it', 'ru'];
const SWITCH_INTERVAL_MS = 60_000;

function validatedEntries(payload: unknown): BrandTaglineEntry[] | null {
  if (!payload || typeof payload !== 'object') return null;
  const source = (payload as { taglines?: unknown }).taglines;
  if (!Array.isArray(source) || source.length !== LANGUAGE_CODES.length) return null;
  const found = new Map<string, BrandTaglineEntry>();
  for (const value of source) {
    if (!value || typeof value !== 'object') return null;
    const entry = value as Partial<BrandTaglineEntry>;
    if (typeof entry.code !== 'string' || !LANGUAGE_CODES.includes(entry.code) ||
      typeof entry.language !== 'string' || !entry.language.trim() ||
      typeof entry.text !== 'string' || !entry.text.trim() || found.has(entry.code)) return null;
    found.set(entry.code, { code: entry.code, language: entry.language, text: entry.text });
  }
  return LANGUAGE_CODES.map(code => found.get(code)!).filter(Boolean);
}

// A shuffle bag uses each language once per cycle and excludes the previous
// language at the start of every new cycle, including cycle boundaries.
export function shuffledLanguageCodes(codes: readonly string[], previousCode: string, random = Math.random): string[] {
  const sequence = Array.from(new Set(codes));
  for (let i = sequence.length - 1; i > 0; i -= 1) {
    const j = Math.min(i, Math.floor(Math.max(0, random()) * (i + 1)));
    [sequence[i], sequence[j]] = [sequence[j], sequence[i]];
  }
  if (sequence.length > 1 && sequence[0] === previousCode) {
    const swapIndex = sequence.findIndex(code => code !== previousCode);
    [sequence[0], sequence[swapIndex]] = [sequence[swapIndex], sequence[0]];
  }
  return sequence;
}

export function useBrandTagline(): BrandTaglineEntry {
  const [entries, setEntries] = useState<BrandTaglineEntry[]>([FALLBACK]);
  const [activeCode, setActiveCode] = useState(FALLBACK.code);

  useEffect(() => {
    const controller = new AbortController();
    const base = import.meta.env.BASE_URL;
    fetch(`${base}i18n/brand-taglines.json`, { signal: controller.signal })
      .then(response => { if (!response.ok) throw new Error('Tagline localization unavailable'); return response.json() as Promise<unknown>; })
      .then(payload => {
        if (controller.signal.aborted) return;
        const complete = validatedEntries(payload);
        if (complete) setEntries(complete);
      })
      .catch(() => { /* Keep the English fallback if localization is unavailable. */ });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (entries.length !== LANGUAGE_CODES.length) return;
    // The first minute always displays English. The first shuffle contains the
    // other nine languages; later cycles contain all ten languages.
    let previousCode = FALLBACK.code;
    let remaining = shuffledLanguageCodes(LANGUAGE_CODES.filter(code => code !== FALLBACK.code), previousCode);
    const timer = window.setInterval(() => {
      if (remaining.length === 0) remaining = shuffledLanguageCodes(LANGUAGE_CODES, previousCode);
      const next = remaining.shift();
      if (!next) return;
      previousCode = next;
      setActiveCode(next);
    }, SWITCH_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [entries]);

  return entries.find(entry => entry.code === activeCode) ?? FALLBACK;
}

export function BrandTagline({ entry, placement }: { entry: BrandTaglineEntry; placement: 'sidebar' | 'welcome' }) {
  return <span className={`brand-tagline brand-tagline--${placement}`} aria-label={`Brand tagline in ${entry.language}`}>
    <span className="brand-tagline__text" key={entry.code} lang={entry.code} dir="auto">{entry.text}</span>
  </span>;
}
