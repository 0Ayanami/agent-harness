"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { ArrowUp, Square, Bot, AlertCircle, CalendarClock, X } from "lucide-react";
import { Sidebar } from "@/components/layout/sidebar";
import { SimulatorMessage } from "@/components/simulator/simulator-message";
import { Card } from "@/components/ui/card";
import { LayerBadge } from "@/components/ui/badge";
import { Tabs } from "@/components/ui/tabs";
import { ApprovalCard } from "./approval-card";
import { ProjectState } from "./project-state";
import { useTranslations } from "@/lib/i18n";
import { useAgentSession } from "@/hooks/use-agent-session";
import type { LibraryQuestion } from "@/types/runtime";
import { LibraryQuestionControls } from "./library-question";
import { LibraryRetrievalStatus, LibrarySources } from "./library-sources";
import { ScheduledTasks } from "./scheduled-tasks";

export function AgentWorkbench() {
  const t = useTranslations("runtime");
  const schedule = useTranslations("schedule");
  const agent = useAgentSession();
  const [draft, setDraft] = useState("");
  const [schedulePanel, setSchedulePanel] = useState<{ prompt?: string } | null>(null);
  const [library, setLibrary] = useState<LibraryQuestion>({ enabled: false, document_ids: null, favorites_only: false, mode: "hybrid" });
  const [libraryValid, setLibraryValid] = useState(true);
  const bottom = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const { view, loading, connection, health } = agent;
  const messages = view?.messages ?? [];
  const toolCount = messages.filter((message) => message.step.type === "tool_call").length;
  const status = view?.cancelling ? "cancelling" : view?.approvals.length ? "approval" : view?.busy ? "running" : "ready";
  const optionsDisabled = loading || !view || view.busy || agent.sessionActionPending;
  const personalized = view?.snapshot.personalized ?? true;
  const temporary = view?.snapshot.temporary ?? false;
  const memoryHint = !personalized ? "memoryDisabledHint" : temporary ? "memoryReadOnlyHint" : "memoryEnabledHint";

  useEffect(() => { setDraft(""); }, [agent.selectedId]);
  useEffect(() => {
    const selected = new URLSearchParams(window.location.search).get("library_file");
    if (selected) setLibrary({ enabled: true, document_ids: [selected], favorites_only: false, mode: "hybrid" });
  }, []);
  useEffect(() => { bottom.current?.scrollIntoView({ block: "nearest", behavior: "smooth" }); }, [messages.length, view?.approvals.length]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (view?.busy) { await agent.cancel(); return; }
    const message = draft.trim();
    if (message && libraryValid && await agent.sendMessage(message, library)) setDraft("");
  }

  return (
    <div className="flex min-w-0 flex-col gap-8 md:flex-row">
      <Sidebar sessions={agent.sessions} archivedSessions={agent.archivedSessions} selectedId={agent.selectedId}
        loading={loading || agent.sessionActionPending} busy={!!view?.busy}
        onSelect={(id) => { agent.openSession(id).catch(agent.reportError); }}
        onCreate={() => { agent.createSession().catch(agent.reportError); }}
        onOpenScheduledTasks={() => setSchedulePanel({})}
        onArchiveChange={(id, archived) => agent.changeSession(id, { archived })}
        onDelete={agent.changeSession} />
      <div className="min-w-0 flex-1">
        <div className="mb-6">
          <div className="mb-2 flex flex-wrap items-center gap-3">
            <h1 className="text-2xl font-bold tracking-tight">{t("workbench")}</h1>
            <LayerBadge layer={view?.busy ? "concurrency" : "tools"}><span id="runtime-status">{t(status)}</span></LayerBadge>
          </div>
          <p className="text-sm text-[var(--color-text-secondary)]">{t("subtitle")}</p>
          <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-[var(--color-text-secondary)]">
            <span id="connection" data-testid="connection" role="status">{t(connection)}</span>
            {health && <><span>{t("model")}: <span id="model" className="font-mono">{health.model}</span></span>
              <span title={health.workdir} className="max-w-full truncate">{t("workspace")}: <span id="workspace-path" className="font-mono">{health.workdir}</span></span></>}
          </div>
        </div>

        {agent.error && <div id="toast" role="alert" className="mb-4 flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">
          <AlertCircle size={16} className="mt-0.5 shrink-0" /><span className="min-w-0 flex-1 break-words">{agent.error}</span>
          {connection === "failed" && <button onClick={agent.reconnect} className="shrink-0 underline">{t("retry")}</button>}
          <button aria-label={t("dismiss")} onClick={agent.dismissError}><X size={16} /></button>
        </div>}

        <Card className="min-w-0 p-4 sm:p-6">
          <LibraryRetrievalStatus retrieval={view?.retrieval} pending={!!view?.busy && library.enabled} />
          <div className="mb-4 flex items-center justify-between gap-4">
            <h2 id="session-title" className="truncate text-sm font-semibold">{view?.snapshot.title || t("newSession")}</h2>
            <span className="shrink-0 text-xs text-[var(--color-text-secondary)]">{t("toolCall")}: <span id="tool-count">{toolCount}</span></span>
          </div>
          <Tabs tabs={[{ id: "chat", label: t("chat") }, { id: "trace", label: `${t("trace")} (${toolCount})` }]}>
            {(tab) => (
              <div id="chat-scroll" className="max-h-[52vh] min-h-56 space-y-3 overflow-y-auto overscroll-contain">
                {loading && <p role="status" className="py-12 text-center text-sm text-[var(--color-text-secondary)]">{t("loading")}</p>}
                {!loading && !messages.some((message) => message.trace === (tab === "trace")) && (
                  tab === "trace" ? <p className="py-12 text-center text-sm text-[var(--color-text-secondary)]">{t("emptyTrace")}</p> :
                  <div id="welcome" className="flex flex-col items-center px-2 py-12 text-center">
                    <Bot size={28} className="mb-4 text-zinc-400" />
                    <h3 className="text-base font-semibold">{t("welcome")}</h3>
                    <p className="mt-2 max-w-md text-sm text-[var(--color-text-secondary)]">{t("welcomeHint")}</p>
                  </div>
                )}
                <div id={tab === "chat" ? "messages" : "timeline"} className="space-y-3">
                  {messages.filter((message) => message.trace === (tab === "trace")).map((message, index) => (
                    <div key={message.id}><SimulatorMessage step={message.step} index={index} /><LibrarySources message={message} /></div>
                  ))}
                </div>
                {view?.busy && <p id="thinking" role="status" className="animate-pulse px-1 text-xs text-[var(--color-text-secondary)]">{t(status)}…</p>}
                <div ref={bottom} />
              </div>
            )}
          </Tabs>
          <div id="approvals" className="mt-4 space-y-3">
            {view?.approvals.map((approval) => <ApprovalCard key={`${view.snapshot.id}-${approval.id}`} approval={approval} onAnswer={agent.answerApproval} onError={agent.reportError} />)}
          </div>
          <form id="composer" onSubmit={submit} className="mt-5 border-t border-[var(--color-border)] pt-4">
            <LibraryQuestionControls value={library} onChange={setLibrary} disabled={optionsDisabled} onValidityChange={setLibraryValid}>
              <label className="inline-flex min-h-8 cursor-pointer items-center gap-2 font-medium">
                <input data-testid="session-personalized" type="checkbox" checked={personalized} disabled={optionsDisabled}
                  aria-describedby="conversation-memory-hint" className="h-4 w-4 accent-zinc-800 dark:accent-zinc-200"
                  onChange={(event) => { if (view) void agent.changeSession(view.snapshot.id, { personalized: event.target.checked }); }} />
                {t("personalized")}
              </label>
              <label className="inline-flex min-h-8 cursor-pointer items-center gap-2 font-medium">
                <input data-testid="session-temporary" type="checkbox" checked={temporary} disabled={optionsDisabled}
                  aria-describedby="conversation-memory-hint" className="h-4 w-4 accent-zinc-800 dark:accent-zinc-200"
                  onChange={(event) => { if (view) void agent.changeSession(view.snapshot.id, { temporary: event.target.checked }); }} />
                {t("temporaryConversation")}
              </label>
              <button type="button" data-testid="scheduled-tasks-compose" aria-haspopup="dialog" disabled={optionsDisabled}
                onClick={() => setSchedulePanel({ prompt: draft })}
                className="inline-flex min-h-8 items-center gap-2 rounded-md px-1 text-sm font-medium hover:bg-[var(--color-bg-secondary)] disabled:opacity-50">
                <CalendarClock size={15} strokeWidth={1.6} />{schedule("title")}
              </button>
              <p id="conversation-memory-hint" className="basis-full text-xs leading-5 text-[var(--color-text-secondary)]">{t(memoryHint)}</p>
            </LibraryQuestionControls>
            <div className="flex items-end gap-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] p-2 focus-within:ring-2 focus-within:ring-blue-500/30">
              <textarea id="message" data-testid="message-input" ref={composer} value={draft}
                onChange={(event) => setDraft(event.target.value)} disabled={loading || !view || view.busy}
                maxLength={30000} rows={2} placeholder={t("placeholder")} aria-label={t("placeholder")}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && !view?.busy) {
                    event.preventDefault(); event.currentTarget.form?.requestSubmit();
                  }
                }}
                className="max-h-40 min-w-0 flex-1 resize-y bg-transparent px-2 py-1 text-sm outline-none disabled:opacity-50" />
              <button id="send" data-testid={view?.busy ? "cancel-run" : "send-message"} type="submit"
                disabled={loading || !view || view.cancelling || (!view.busy && (agent.sessionActionPending || !draft.trim() || !libraryValid))}
                aria-label={view?.busy ? t("stop") : t("send")}
                className="flex min-h-10 shrink-0 items-center gap-1.5 rounded-md bg-zinc-900 px-3 text-sm font-medium text-white disabled:opacity-40 dark:bg-white dark:text-zinc-900">
                {view?.busy ? <Square size={14} /> : <ArrowUp size={15} />}<span>{view?.busy ? t("stop") : t("send")}</span>
              </button>
            </div>
          </form>
        </Card>
        {view && <div className="mt-5"><ProjectState state={view.snapshot.state} onRefresh={agent.refresh} /></div>}
      </div>
      {schedulePanel && <ScheduledTasks session={view?.snapshot ?? null} initialPrompt={schedulePanel.prompt}
        onClose={() => setSchedulePanel(null)} onOpenSession={(id) => {
          setSchedulePanel(null);
          if (id !== agent.selectedId) agent.openSession(id).catch(agent.reportError);
        }} />}
    </div>
  );
}
