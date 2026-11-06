"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { API_PREFIX, agentApi, replayEvents } from "@/lib/agent-api";
import type { Approval, Health, LibraryQuestion, LibraryRetrieval, LibrarySource, RuntimeEvent, RuntimeMessage, SessionChanges, SessionInfo, SessionSnapshot } from "@/types/runtime";

type Connection = "connecting" | "connected" | "reconnecting" | "failed";
interface SessionView {
  snapshot: SessionSnapshot;
  messages: RuntimeMessage[];
  approvals: Approval[];
  busy: boolean;
  cancelling: boolean;
  cursor: number;
  retrieval?: LibraryRetrieval;
}

function sourceList(input: unknown): LibrarySource[] {
  if (!Array.isArray(input)) return [];
  return input.filter((source): source is LibrarySource => source && typeof source === "object" &&
    /^S\d+$/.test(source.id) && ["document_id", "version_id", "chunk_id", "name", "text"].every((key) => typeof source[key] === "string") &&
    source.location && typeof source.location === "object");
}

function value(payload: Record<string, unknown>, key: string) {
  const item = payload[key];
  return typeof item === "string" ? item : "";
}

function eventMessage(event: RuntimeEvent): RuntimeMessage | null {
  const { payload, kind, id } = event;
  const content = value(payload, "text");
  if (kind === "user" || kind === "assistant") {
    if (!content) return null;
    return { id: `event-${id}`, runId: value(payload, "run_id") || undefined, trace: payload.scope === "subagent", step: {
      type: kind === "user" ? "user_message" : "assistant_text",
      content, annotation: payload.scope === "subagent" ? "subagent" : "",
    } };
  }
  if (kind === "tool_started" || kind === "tool_finished") {
    return { id: `event-${id}`, trace: true, step: {
      type: kind === "tool_started" ? "tool_call" : "tool_result",
      toolName: value(payload, "name"),
      content: kind === "tool_started" ? JSON.stringify(payload.input ?? {}, null, 2) : value(payload, "output"),
      annotation: "",
    } };
  }
  if (kind === "scheduled") {
    return { id: `event-${id}`, trace: false, step: {
      type: "system_event", content: value(payload, "prompt"), annotation: "scheduledTriggered",
    } };
  }
  if (kind === "error" || kind === "subagent_started" || kind === "subagent_finished" ||
      (kind === "run_finished" && payload.outcome !== "completed")) {
    const text = kind === "error" ? value(payload, "message") :
      kind === "subagent_started" ? value(payload, "description") :
      kind === "subagent_finished" ? value(payload, "summary") : "";
    const annotation = kind === "run_finished" ? (payload.outcome === "cancelled" ? "cancelled" : "runFailed") :
      kind === "subagent_started" ? "subagentStarted" : kind === "subagent_finished" ? "subagentFinished" : "";
    return { id: `event-${id}`, trace: kind.startsWith("subagent_"), step: { type: "system_event", content: text, annotation } };
  }
  return null;
}

function historyMessages(snapshot: SessionSnapshot): RuntimeMessage[] {
  return snapshot.history.flatMap<RuntimeMessage>((message, index) => {
    if (message.role === "user" && typeof message.content === "string" &&
      !/^(\[Scheduled\]|\[Team events\]|\[Compacted\]|\[Reactive compact\])/.test(message.content)) {
      return [{ id: `history-${index}`, trace: false, step: { type: "user_message" as const, content: message.content, annotation: "" } }];
    }
    if (message.role === "assistant") {
      const text = typeof message.content === "string" ? message.content :
        message.content.filter((block) => block.type === "text").map((block) => block.text ?? "").join("\n");
      if (text) return [{ id: `history-${index}`, trace: false, step: { type: "assistant_text" as const, content: text, annotation: "" } }];
    }
    return [];
  });
}

function applyEvent(view: SessionView, event: RuntimeEvent, replay = false): SessionView {
  let next = { ...view, cursor: Math.max(view.cursor, event.id) };
  const entry = eventMessage(event);
  // Persisted events provide one ordered transcript, including errors and cancellations.
  if (entry) next.messages = [...next.messages, entry];
  if (event.kind === "library_retrieval") {
    next.retrieval = { run_id: value(event.payload, "run_id"), mode: value(event.payload, "mode"),
      warning: value(event.payload, "warning") || null, sources: sourceList(event.payload.sources),
      reranked: typeof event.payload.reranked === "boolean" ? event.payload.reranked : undefined };
  }
  if (event.kind === "library_answer") {
    const runId = value(event.payload, "run_id");
    if (!runId) return next;
    const text = value(event.payload, "text");
    const invalid = event.payload.invalid_citations;
    const citations = sourceList(event.payload.citations);
    const update = { runId, citations, citationWarning: value(event.payload, "warning") || undefined,
      invalidCitations: Array.isArray(invalid) ? invalid.filter((item): item is string => typeof item === "string") : [] };
    const index = next.messages.findLastIndex((message) => message.runId === runId && message.step.type === "assistant_text" && !message.trace);
    if (index >= 0) next.messages = next.messages.map((message, position) => position === index
      ? { ...message, ...update, step: { ...message.step, content: typeof event.payload.text === "string" ? text : message.step.content } } : message);
    else if (text) next.messages = [...next.messages, { id: `event-${event.id}`, trace: false, ...update, step: { type: "assistant_text", content: text, annotation: "" } }];
  }
  if (event.kind === "run_started") next.retrieval = undefined;
  if (replay) return next;
  if (event.kind === "run_started") { next.busy = true; next.cancelling = false; }
  if (event.kind === "run_finished") { next.busy = false; next.cancelling = false; }
  if (event.kind === "approval_requested") {
    const payload = event.payload;
    if (typeof payload.id === "string" && typeof payload.name === "string" && payload.input && typeof payload.input === "object") {
      const approval = { id: payload.id, name: payload.name, input: payload.input as Record<string, unknown> };
      next.approvals = [...next.approvals.filter((item) => item.id !== approval.id), approval];
    }
  }
  if (event.kind === "approval_resolved") next.approvals = next.approvals.filter((item) => item.id !== event.payload.id);
  return next;
}

function errorText(error: unknown) { return error instanceof Error ? error.message : String(error); }
function savedSession() { try { return localStorage.getItem("harness.session"); } catch { return null; } }

export function useAgentSession() {
  const [health, setHealth] = useState<Health | null>(null);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [archivedSessions, setArchivedSessions] = useState<SessionInfo[]>([]);
  const [sessionActionPending, setSessionActionPending] = useState(false);
  const [view, setView] = useState<SessionView | null>(null);
  const [loading, setLoading] = useState(true);
  const [connection, setConnection] = useState<Connection>("connecting");
  const [error, setError] = useState<string | null>(null);
  const selectedId = useRef<string | null>(null);
  const generation = useRef(0);
  const source = useRef<EventSource | null>(null);
  const loadController = useRef<AbortController | null>(null);
  const seen = useRef(new Set<number>());
  const mounted = useRef(false);
  const posting = useRef(false);
  const changingSession = useRef(false);
  const settingsRevision = useRef(0);
  const sessionListRequest = useRef(0);

  const reportError = useCallback((issue: unknown) => {
    if (mounted.current && !(issue instanceof DOMException && issue.name === "AbortError")) setError(errorText(issue));
  }, []);

  const refreshSessions = useCallback(async () => {
    const request = ++sessionListRequest.current;
    const [list, archived] = await Promise.all([
      agentApi<SessionInfo[]>("/sessions"), agentApi<SessionInfo[]>("/sessions?archived=true"),
    ]);
    if (mounted.current && request === sessionListRequest.current) {
      setSessions(list);
      setArchivedSessions(archived);
    }
    return list;
  }, []);

  const refreshSnapshot = useCallback(async (sessionId: string, version: number) => {
    const revision = settingsRevision.current;
    const snapshot = await agentApi<SessionSnapshot>(`/sessions/${sessionId}`);
    if (!mounted.current || selectedId.current !== sessionId || generation.current !== version) return;
    setView((current) => {
      if (!current || current.snapshot.id !== sessionId) return current;
      const latest = snapshot.cursor >= current.cursor;
      const preferences = revision === settingsRevision.current ? {} : {
        personalized: current.snapshot.personalized, temporary: current.snapshot.temporary,
        state: { ...snapshot.state, memory_catalog: current.snapshot.state.memory_catalog },
      };
      return { ...current, snapshot: { ...snapshot, ...preferences },
        ...(latest ? { busy: snapshot.status === "running", approvals: snapshot.approvals, cursor: snapshot.cursor } : {}),
      };
    });
    await refreshSessions();
  }, [refreshSessions]);

  const openSession = useCallback(async (sessionId: string) => {
    const version = ++generation.current;
    source.current?.close();
    loadController.current?.abort();
    const controller = new AbortController();
    loadController.current = controller;
    selectedId.current = sessionId;
    setLoading(true);
    setView(null);
    setError(null);
    setConnection("connecting");
    try {
      const snapshot = await agentApi<SessionSnapshot>(`/sessions/${sessionId}`, { signal: controller.signal });
      const events = await replayEvents(sessionId, controller.signal);
      if (!mounted.current || version !== generation.current) return;
      seen.current = new Set(events.map((event) => event.id));
      // history/cursor are not an atomic read while a worker is running. Rebuilding
      // messages from events avoids skipping a newly emitted assistant response.
      // Snapshot remains authoritative for already-resolved approvals after restart.
      const hasTranscript = events.some((event) => event.kind === "user" || event.kind === "assistant" || event.kind === "scheduled");
      let restored: SessionView = {
        snapshot, messages: hasTranscript ? [] : historyMessages(snapshot), approvals: snapshot.approvals,
        busy: snapshot.status === "running", cancelling: false, cursor: snapshot.cursor,
      };
      for (const event of events) restored = applyEvent(restored, event, event.id <= snapshot.cursor);
      setView(restored);
      try { localStorage.setItem("harness.session", sessionId); } catch {}
      const live = new EventSource(`${API_PREFIX}/sessions/${sessionId}/events?after=${restored.cursor}`);
      source.current = live;
      live.onopen = () => { if (generation.current === version) setConnection("connected"); };
      live.onerror = () => { if (generation.current === version) setConnection("reconnecting"); };
      live.onmessage = (message) => {
        if (!mounted.current || generation.current !== version) return;
        try {
          const event = JSON.parse(message.data) as RuntimeEvent;
          if (seen.current.has(event.id)) return;
          seen.current.add(event.id);
          setView((current) => current ? applyEvent(current, event) : current);
          if (event.kind === "run_finished") refreshSnapshot(sessionId, version).catch(reportError);
        } catch (issue) { reportError(issue); }
      };
      await refreshSessions();
    } catch (issue) {
      if (version === generation.current && mounted.current && !controller.signal.aborted) {
        setConnection("failed"); reportError(issue);
      }
    } finally {
      if (version === generation.current && mounted.current) setLoading(false);
    }
  }, [refreshSessions, refreshSnapshot, reportError]);

  const createSession = useCallback(async () => {
    if (posting.current) return;
    posting.current = true;
    setLoading(true);
    try {
      const snapshot = await agentApi<SessionSnapshot>("/sessions", { method: "POST" });
      if (mounted.current) await openSession(snapshot.id);
    } catch (issue) { reportError(issue); if (mounted.current) setLoading(false); }
    finally { posting.current = false; }
  }, [openSession, reportError]);

  const initialize = useCallback(async () => {
    const version = generation.current;
    setLoading(true); setError(null); setConnection("connecting");
    try {
      const [status, list] = await Promise.all([agentApi<Health>("/health"), refreshSessions()]);
      if (!mounted.current || version !== generation.current) return;
      setHealth(status);
      const saved = savedSession();
      if (list.length) await openSession(list.find((session) => session.id === saved)?.id ?? list[0].id);
      else await createSession();
    } catch (issue) {
      if (mounted.current && version === generation.current) {
        setConnection("failed"); setLoading(false); reportError(issue);
      }
    }
  }, [createSession, openSession, refreshSessions, reportError]);

  useEffect(() => {
    mounted.current = true;
    initialize();
    return () => {
      mounted.current = false;
      generation.current++;
      source.current?.close();
      loadController.current?.abort();
    };
  }, [initialize]);

  const changeSession = async (sessionId: string, patch?: SessionChanges) => {
    if (posting.current || changingSession.current) return false;
    changingSession.current = true;
    setSessionActionPending(true);
    setError(null);
    let committed = false;
    try {
      const path = `/sessions/${sessionId}`;
      let updated: SessionSnapshot | null = null;
      if (patch) updated = await agentApi<SessionSnapshot>(path, { method: "PATCH", body: JSON.stringify(patch) });
      else await agentApi<void>(path, { method: "DELETE" });
      committed = true;
      if (!mounted.current) return true;

      if (updated && patch?.archived === undefined) {
        settingsRevision.current++;
        const { personalized, temporary, state } = updated;
        setView((current) => current?.snapshot.id === sessionId
          ? { ...current, snapshot: { ...current.snapshot, personalized, temporary,
            state: { ...current.snapshot.state, memory_catalog: state.memory_catalog },
          } } : current);
        return true;
      }

      // Invalidate list responses started before the mutation reached the server.
      sessionListRequest.current++;
      if (updated && patch?.archived === false) {
        setSessions((list) => [updated, ...list.filter((session) => session.id !== sessionId)]);
        setArchivedSessions((list) => list.filter((session) => session.id !== sessionId));
        await openSession(sessionId);
        return true;
      }

      setSessions((list) => list.filter((session) => session.id !== sessionId));
      setArchivedSessions((list) => {
        const remaining = list.filter((session) => session.id !== sessionId);
        return updated ? [updated, ...remaining] : remaining;
      });
      const removedSelection = selectedId.current === sessionId;
      if (removedSelection) {
        generation.current++;
        source.current?.close();
        loadController.current?.abort();
        selectedId.current = null;
        setView(null);
        setLoading(true);
        setConnection("connecting");
        try { if (savedSession() === sessionId) localStorage.removeItem("harness.session"); } catch {}
      }
      const active = await refreshSessions();
      if (mounted.current && removedSelection && !selectedId.current) {
        if (active.length) await openSession(active[0].id);
        else await createSession();
      }
    } catch (issue) {
      reportError(issue);
      if (mounted.current && !selectedId.current) {
        setConnection("failed");
        setLoading(false);
      }
    } finally {
      changingSession.current = false;
      if (mounted.current) setSessionActionPending(false);
    }
    return committed;
  };

  const sendMessage = async (message: string, library?: LibraryQuestion) => {
    const sessionId = selectedId.current;
    if (!sessionId || posting.current || changingSession.current) return false;
    posting.current = true;
    const version = generation.current;
    setView((current) => current ? { ...current, busy: true } : current);
    try {
      await agentApi(`/sessions/${sessionId}/messages`, { method: "POST", body: JSON.stringify({ message, ...(library?.enabled ? { library } : {}) }) });
      // A delayed POST response must not clear the draft in a different session.
      return mounted.current && version === generation.current;
    } catch (issue) {
      if (version === generation.current) {
        setView((current) => current ? { ...current, busy: false } : current);
        reportError(issue);
        await refreshSnapshot(sessionId, version).catch(reportError);
      }
      return false;
    } finally { posting.current = false; }
  };

  const cancel = async () => {
    const sessionId = selectedId.current;
    if (!sessionId) return;
    const version = generation.current;
    try {
      await agentApi(`/sessions/${sessionId}/cancel`, { method: "POST" });
      if (generation.current === version) setView((current) => current?.busy ? { ...current, cancelling: true } : current);
    } catch (issue) { reportError(issue); }
  };

  const answerApproval = async (approvalId: string, allow: boolean) => {
    const sessionId = selectedId.current;
    if (!sessionId) return;
    const version = generation.current;
    await agentApi(`/sessions/${sessionId}/approvals/${approvalId}`, { method: "POST", body: JSON.stringify({ allow }) });
    if (generation.current === version) setView((current) => current ? {
      ...current, approvals: current.approvals.filter((approval) => approval.id !== approvalId),
    } : current);
  };

  return {
    health, sessions, archivedSessions, sessionActionPending, view, loading, connection, error, selectedId: selectedId.current,
    openSession, createSession, changeSession, sendMessage, cancel, answerApproval, reportError,
    dismissError: () => setError(null), reconnect: initialize,
    refresh: () => selectedId.current && refreshSnapshot(selectedId.current, generation.current).catch(reportError),
  };
}
