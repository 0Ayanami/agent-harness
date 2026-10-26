"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { API_PREFIX, agentApi, replayEvents } from "@/lib/agent-api";
import type { Approval, Health, RuntimeEvent, RuntimeMessage, SessionInfo, SessionSnapshot } from "@/types/runtime";

type Connection = "connecting" | "connected" | "reconnecting" | "failed";
interface SessionView {
  snapshot: SessionSnapshot;
  messages: RuntimeMessage[];
  approvals: Approval[];
  busy: boolean;
  cancelling: boolean;
  cursor: number;
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
    return { id: `event-${id}`, trace: payload.scope === "subagent", step: {
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
  const sessionListRequest = useRef(0);

  const reportError = useCallback((issue: unknown) => {
    if (mounted.current && !(issue instanceof DOMException && issue.name === "AbortError")) setError(errorText(issue));
  }, []);

  const refreshSessions = useCallback(async () => {
    const request = ++sessionListRequest.current;
    const list = await agentApi<SessionInfo[]>("/sessions");
    if (mounted.current && request === sessionListRequest.current) setSessions(list);
    return list;
  }, []);

  const refreshSnapshot = useCallback(async (sessionId: string, version: number) => {
    const snapshot = await agentApi<SessionSnapshot>(`/sessions/${sessionId}`);
    if (!mounted.current || selectedId.current !== sessionId || generation.current !== version) return;
    setView((current) => {
      if (!current || current.snapshot.id !== sessionId) return current;
      const latest = snapshot.cursor >= current.cursor;
      return { ...current, snapshot,
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
      const hasTranscript = events.some((event) => event.kind === "user" || event.kind === "assistant");
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

  const sendMessage = async (message: string) => {
    const sessionId = selectedId.current;
    if (!sessionId || posting.current) return false;
    posting.current = true;
    const version = generation.current;
    setView((current) => current ? { ...current, busy: true } : current);
    try {
      await agentApi(`/sessions/${sessionId}/messages`, { method: "POST", body: JSON.stringify({ message }) });
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
    health, sessions, view, loading, connection, error, selectedId: selectedId.current,
    openSession, createSession, sendMessage, cancel, answerApproval, reportError,
    dismissError: () => setError(null), reconnect: initialize,
    refresh: () => selectedId.current && refreshSnapshot(selectedId.current, generation.current).catch(reportError),
  };
}
