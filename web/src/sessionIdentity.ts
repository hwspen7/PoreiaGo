/** Active conversation identity is stable across reloads and explicit across new journeys. */
const ACTIVE_SESSION_KEY = 'poreiago.activeSession.v1';
type KeyValueStore = Pick<Storage, 'getItem' | 'setItem'>;

function browserStorage(): KeyValueStore | null {
  try { return typeof window === 'undefined' ? null : window.localStorage; }
  catch { return null; }
}

export function createSessionId(): string {
  return `session-${crypto.randomUUID().slice(0, 12)}`;
}

export function getOrCreateSessionId(store: KeyValueStore | null = browserStorage()): string {
  try {
    const remembered = store?.getItem(ACTIVE_SESSION_KEY);
    if (remembered && /^session-[\da-z-]{12,64}$/i.test(remembered)) return remembered;
  } catch { /* Private browsing or storage-disabled mode: use a new session. */ }
  return createSessionId();
}

export function rememberSessionId(id: string, store: KeyValueStore | null = browserStorage()): void {
  if (!/^session-[\da-z-]{12,64}$/i.test(id)) return;
  try { store?.setItem(ACTIVE_SESSION_KEY, id); }
  catch { /* The current tab still retains the session ID. */ }
}
