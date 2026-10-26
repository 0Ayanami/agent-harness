import type { RuntimeEvent } from "@/types/runtime";

export const API_PREFIX = "/api/agent";

export async function agentApi<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_PREFIX}${path}`, {
    ...options,
    cache: "no-store",
    headers: { "Content-Type": "application/json", "X-Agent-Client": "web", ...options.headers },
  });
  if (!response.ok) {
    const detail: unknown = await response.json().catch(() => null);
    const message = detail && typeof detail === "object" && "detail" in detail && typeof detail.detail === "string"
      ? detail.detail : `HTTP ${response.status}: ${response.statusText}`;
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

/** Replay all persisted events, including sessions longer than the API's page size. */
export async function replayEvents(sessionId: string, signal: AbortSignal): Promise<RuntimeEvent[]> {
  const events: RuntimeEvent[] = [];
  let cursor = 0;
  while (!signal.aborted) {
    const response = await fetch(`${API_PREFIX}/sessions/${sessionId}/events?once=true&after=${cursor}`, { signal, cache: "no-store" });
    if (!response.ok) throw new Error(`Event replay failed (${response.status})`);
    const body = await response.text();
    const batch = body.split(/\r?\n/).filter((line) => line.startsWith("data: "))
      .map((line) => JSON.parse(line.slice(6)) as RuntimeEvent);
    if (!batch.length) break;
    events.push(...batch);
    const next = batch[batch.length - 1].id;
    if (next <= cursor) throw new Error("Event replay cursor did not advance");
    cursor = next;
    if (batch.length < 200) break;
  }
  return events;
}
