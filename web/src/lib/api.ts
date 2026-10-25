export type Evidence = {
  provider: string;
  url?: string | null;
  observed_at: string;
  label?: string | null;
  mode: string;
};
export type Skill = {
  id: string;
  description: string;
  domain: string;
  wave: 'P0' | 'P1' | 'P2' | 'P3';
  release_state: 'implemented' | 'planned';
  required_capabilities: string[];
  keywords: string[];
};
export type SkillResult = {
  name: string;
  status: string;
  text: string;
  data: Record<string, unknown>;
  sources: Evidence[];
  limitations: string[];
};
export type ChatResult = {
  answer: string;
  results: SkillResult[];
  planner_mode: string;
  elapsed_ms: number;
  session_id: string;
  fields: Record<string, unknown>;
  plan?: {
    layers: string[][];
    skills: string[];
    rejected: { name: string; reason: string }[];
  };
  execution_trace?: {
    name?: string;
    status: string;
    elapsed_ms?: number;
    dependencies?: string[];
  }[];
  planner_limitations?: string[];
};
export type Health = {
  status: string;
  version: string;
  skill_count: number;
  implemented_skills: number;
  planned_skills: number;
  models: ModelProfile[];
  providers: Provider[];
  memory_window: number;
  online_enabled: boolean;
  retrieval_mode: string;
};
export type ModelProfile = {
  name: string;
  role: string;
  provider: string;
  model: string;
  configured: boolean;
};
export type Provider = {
  capability: string;
  provider: string;
  configured: boolean;
  requires_key: boolean;
};
let authToken = '';
export function setAuthToken(value: string) {
  authToken = value;
}
export async function apiJSON<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...(options?.body ? { 'Content-Type': 'application/json' } : {}),
      ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
      ...options?.headers,
    },
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${await response.text()}`);
  return response.json() as Promise<T>;
}
export async function streamChat(
  payload: {
    message: string;
    user_id: string;
    session_id: string;
    context?: Record<string, unknown>;
  },
  onEvent: (name: string, payload: unknown) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch('/api/chat/stream', {
    method: 'POST',
    signal,
    headers: {
      'Content-Type': 'application/json',
      ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
    },
    body: JSON.stringify(payload),
  });
  if (!response.ok || !response.body) throw new Error(`Request failed: HTTP ${response.status}`);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    buffer = buffer.replace(/\r\n/g, '\n');
    let position = buffer.indexOf('\n\n');
    while (position !== -1) {
      const packet = buffer.slice(0, position);
      buffer = buffer.slice(position + 2);
      let name = 'message',
        data = '';
      for (const line of packet.split('\n')) {
        if (line.startsWith('event: ')) name = line.slice(7).trim();
        if (line.startsWith('data: ')) data += line.slice(6);
      }
      if (data) onEvent(name, JSON.parse(data) as unknown);
      position = buffer.indexOf('\n\n');
    }
  }
}
