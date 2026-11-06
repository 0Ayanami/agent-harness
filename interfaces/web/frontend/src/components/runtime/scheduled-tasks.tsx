"use client";

import { useEffect, useState, type FormEvent } from "react";
import { CalendarClock, Plus, RefreshCw, X } from "lucide-react";
import { LibraryDialog } from "@/components/library/library-dialog";
import { agentApi } from "@/lib/agent-api";
import { useTranslations } from "@/lib/i18n";
import type { SessionInfo } from "@/types/runtime";

interface ScheduledTask {
  id: string;
  cron: string;
  prompt: string;
  recurring: boolean;
  durable: boolean;
  session_id: string;
  session_title: string;
  status: "scheduled" | "queued";
  next_run_at: string | null;
}

interface ScheduledTaskList {
  items: ScheduledTask[];
  server_time: string;
}

type TaskDraft = { cron_expr: string; prompt: string; recurring: boolean };
type Frequency = "daily" | "weekly" | "custom";
type Translate = (key: string) => string;

const weekdays = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];
const controlClass = "min-h-10 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-zinc-400/40 disabled:opacity-50";
const buttonClass = "inline-flex min-h-10 items-center justify-center gap-2 rounded-lg border border-[var(--color-border)] px-3 text-sm font-medium hover:bg-[var(--color-bg-secondary)] disabled:opacity-50";
const primaryClass = "inline-flex min-h-10 items-center justify-center gap-2 rounded-lg bg-zinc-900 px-3 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-white dark:text-zinc-900";

function taskPath(sessionId: string) {
  return `/sessions/${encodeURIComponent(sessionId)}/scheduled-tasks`;
}

function wallTime(iso: string) {
  const offset = iso.match(/[+-]\d{2}:\d{2}$/)?.[0] ?? "";
  return `${iso.slice(0, 16).replace("T", " ")} UTC${offset}`;
}

function cadence(cron: string, t: Translate) {
  const match = /^(\d{1,2}) (\d{1,2}) \* \* (\*|[0-6])$/.exec(cron);
  if (!match) return cron;
  const [, minute, hour, weekday] = match;
  const time = `${hour.padStart(2, "0")}:${minute.padStart(2, "0")}`;
  return weekday === "*" ? `${t("daily")} ${time}` : `${t("weekly")} · ${t(weekdays[Number(weekday)])} ${time}`;
}

function ScheduleForm({ session, initialPrompt, saving, onSave, onBack }: {
  session: SessionInfo;
  initialPrompt: string;
  saving: boolean;
  onSave: (draft: TaskDraft) => void;
  onBack: () => void;
}) {
  const t = useTranslations("schedule");
  const [prompt, setPrompt] = useState(initialPrompt);
  const [frequency, setFrequency] = useState<Frequency>("daily");
  const [time, setTime] = useState("09:00");
  const [weekday, setWeekday] = useState("1");
  const [expression, setExpression] = useState("");
  const [once, setOnce] = useState(false);

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!prompt.trim() || saving) return;
    const [hour, minute] = time.split(":").map(Number);
    const cron = frequency === "custom" ? expression.trim() : `${minute} ${hour} * * ${frequency === "weekly" ? weekday : "*"}`;
    onSave({ cron_expr: cron, prompt: prompt.trim(), recurring: !once });
  }

  return <form onSubmit={submit} data-testid="scheduled-task-form" className="space-y-4 rounded-xl border border-[var(--color-border)] p-4 sm:p-5">
    <div>
      <h3 className="font-semibold">{t("newTask")}</h3>
      <p className="mt-1 break-words text-xs text-[var(--color-text-secondary)]">{t("owner")} · {session.title}</p>
    </div>
    <fieldset disabled={saving} className="space-y-4">
      <label className="block space-y-1.5 text-sm font-medium">{t("prompt")}
        <textarea data-testid="scheduled-task-prompt" value={prompt} onChange={(event) => setPrompt(event.target.value)}
          placeholder={t("promptPlaceholder")} autoFocus required maxLength={30000} rows={5} className={controlClass} />
      </label>
      <div className="grid gap-3 sm:grid-cols-3">
        <label className="block space-y-1.5 text-sm font-medium">{t("frequency")}
          <select data-testid="scheduled-task-frequency" value={frequency} className={controlClass}
            onChange={(event) => setFrequency(event.target.value as Frequency)}>
            <option value="daily">{t("daily")}</option><option value="weekly">{t("weekly")}</option><option value="custom">{t("custom")}</option>
          </select>
        </label>
        {frequency !== "custom" && <label className="block space-y-1.5 text-sm font-medium">{t("time")}
          <input data-testid="scheduled-task-time" type="time" value={time} required className={controlClass} onChange={(event) => setTime(event.target.value)} />
        </label>}
        {frequency === "weekly" && <label className="block space-y-1.5 text-sm font-medium">{t("weekday")}
          <select data-testid="scheduled-task-weekday" value={weekday} className={controlClass} onChange={(event) => setWeekday(event.target.value)}>
            {weekdays.map((day, index) => <option key={day} value={index}>{t(day)}</option>)}
          </select>
        </label>}
      </div>
      {frequency === "custom" && <label className="block space-y-1.5 text-sm font-medium">{t("cron")}
        <input data-testid="scheduled-task-cron" value={expression} required maxLength={120} placeholder="0 9 * * 1-5"
          className={`${controlClass} font-mono`} aria-describedby="scheduled-task-cron-hint" onChange={(event) => setExpression(event.target.value)} />
        <span id="scheduled-task-cron-hint" className="block text-xs font-normal text-[var(--color-text-secondary)]">{t("cronHint")}</span>
      </label>}
      <label className="flex min-h-9 items-center gap-2 text-sm">
        <input data-testid="scheduled-task-once" type="checkbox" checked={once} onChange={(event) => setOnce(event.target.checked)} className="h-4 w-4 accent-zinc-800 dark:accent-zinc-200" />
        {t("once")}
      </label>
      <p className="text-xs text-[var(--color-text-secondary)]">{t("restartHint")}</p>
      <div className="flex flex-wrap justify-end gap-2">
        <button type="button" onClick={onBack} className={buttonClass}>{t("back")}</button>
        <button type="submit" data-testid="scheduled-task-create" disabled={!prompt.trim() || (frequency === "custom" && !expression.trim())} className={primaryClass}>{t("save")}</button>
      </div>
    </fieldset>
  </form>;
}

export function ScheduledTasks({ session, initialPrompt, onClose, onOpenSession }: {
  session: SessionInfo | null;
  initialPrompt?: string;
  onClose: () => void;
  onOpenSession: (id: string) => void;
}) {
  const t = useTranslations("schedule");
  const [list, setList] = useState<ScheduledTaskList | null>(null);
  const [loading, setLoading] = useState(true);
  const [listFailed, setListFailed] = useState(false);
  const [revision, setRevision] = useState(0);
  const [formPrompt, setFormPrompt] = useState<string | null>(initialPrompt ?? null);
  const [cancelTarget, setCancelTarget] = useState<ScheduledTask | null>(null);
  const [saving, setSaving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<"created" | "cancelled" | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setListFailed(false);
    agentApi<ScheduledTaskList>("/scheduled-tasks", { signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) setList(value); })
      .catch(() => { if (!controller.signal.aborted) setListFailed(true); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [revision]);

  function back() {
    setFormPrompt(null);
    setCancelTarget(null);
    setActionError(null);
  }

  async function changeTask(result: "created" | "cancelled", operation: () => Promise<ScheduledTask | void>) {
    if (saving) return;
    setSaving(true);
    setActionError(null);
    setNotice(null);
    try {
      const task = await operation();
      setList((current) => {
        const changed = task ?? cancelTarget;
        if (!current || !changed) return current;
        const items = current.items.filter((item) => item.id !== changed.id || item.session_id !== changed.session_id);
        return { ...current, items: task ? [task, ...items] : items };
      });
      back();
      setNotice(result);
      setRevision((value) => value + 1);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : t("actionError"));
    } finally {
      setSaving(false);
    }
  }

  const showForm = formPrompt !== null && session !== null;
  return <LibraryDialog drawer titleId="scheduled-tasks-title" testId="scheduled-tasks-dialog" busy={saving}
    onClose={cancelTarget ? back : onClose}>
    <div className="flex h-full flex-col">
      <header className="flex shrink-0 items-start justify-between gap-4 border-b border-[var(--color-border)] p-4 sm:p-6">
        <div className="min-w-0">
          <h2 id="scheduled-tasks-title" className="flex items-center gap-2 text-lg font-semibold"><CalendarClock size={19} className="shrink-0" />{t("title")}</h2>
          <p className="mt-1 text-xs text-[var(--color-text-secondary)]">{t("scope")}</p>
        </div>
        <button type="button" autoFocus={!showForm} disabled={saving} aria-label={t("close")} title={t("close")} onClick={onClose}
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg hover:bg-[var(--color-bg-secondary)] disabled:opacity-50"><X size={18} /></button>
      </header>
      <div className="min-h-0 flex-1 space-y-4 overflow-auto p-4 sm:p-6">
        <div className="space-y-1 text-xs leading-5 text-[var(--color-text-secondary)]">
          <p>{t("timeHint")}</p>
          {list && <p>{t("serverTime")} · <time dateTime={list.server_time}>{wallTime(list.server_time)}</time></p>}
        </div>
        {actionError && <p role="alert" className="break-words text-sm text-red-700 dark:text-red-300">{actionError}</p>}
        {notice && <p role="status" className="text-sm text-emerald-700 dark:text-emerald-300">{t(notice)}</p>}
        {showForm ? <ScheduleForm key={session.id} session={session} initialPrompt={formPrompt} saving={saving} onBack={back}
          onSave={(draft) => void changeTask("created", () => agentApi<ScheduledTask>(taskPath(session.id), {
            method: "POST", body: JSON.stringify({ ...draft, persistent: true }),
          }))} /> : cancelTarget ? <section aria-labelledby="scheduled-task-cancel-title" className="space-y-3 rounded-xl border border-[var(--color-border)] p-4 sm:p-5">
          <h3 id="scheduled-task-cancel-title" className="font-semibold">{t("cancelTitle")}</h3>
          <p className="whitespace-pre-wrap break-words text-sm">{cancelTarget.prompt}</p>
          <p className="text-sm text-[var(--color-text-secondary)]">{t("cancelHint")}</p>
          <div className="flex flex-wrap justify-end gap-2">
            <button type="button" disabled={saving} onClick={back} className={buttonClass}>{t("keep")}</button>
            <button type="button" data-testid="scheduled-task-confirm-cancel" disabled={saving}
              onClick={() => void changeTask("cancelled", () => agentApi<void>(`${taskPath(cancelTarget.session_id)}/${encodeURIComponent(cancelTarget.id)}`, { method: "DELETE" }))}
              className="min-h-10 rounded-lg bg-red-700 px-3 text-sm font-medium text-white hover:bg-red-800 disabled:opacity-50">{t("confirmCancel")}</button>
          </div>
        </section> : <section aria-busy={loading} className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <button type="button" data-testid="scheduled-task-new" disabled={!session} onClick={() => { setNotice(null); setFormPrompt(""); }} className={primaryClass}><Plus size={16} />{t("newTask")}</button>
            <button type="button" disabled={loading} onClick={() => setRevision((value) => value + 1)} aria-label={t("refresh")} title={t("refresh")} className={buttonClass}><RefreshCw size={16} /></button>
          </div>
          {!session && <p className="text-xs text-[var(--color-text-secondary)]">{t("noSession")}</p>}
          {listFailed && <p role="alert" className="text-sm text-red-700 dark:text-red-300">{t("loadError")}</p>}
          {loading && <p role="status" className="text-sm text-[var(--color-text-secondary)]">{t("loading")}</p>}
          {!loading && !listFailed && list?.items.length === 0 && <p className="rounded-lg border border-dashed border-[var(--color-border)] p-5 text-sm text-[var(--color-text-secondary)]">{t("empty")}</p>}
          {list?.items.map((task) => <article key={`${task.session_id}:${task.id}`} data-testid="scheduled-task-row" className="space-y-3 rounded-xl border border-[var(--color-border)] p-4">
            <p className="whitespace-pre-wrap [overflow-wrap:anywhere] text-sm font-medium">{task.prompt}</p>
            <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-[var(--color-text-secondary)]">
              <span>{cadence(task.cron, t)}</span><span>{t(task.status)}</span><span>{t(task.recurring ? "recurring" : "oneShot")}</span><span>{t(task.durable ? "persistent" : "ephemeral")}</span>
            </div>
            <div className="space-y-1 text-xs text-[var(--color-text-secondary)]">
              <p className="break-words">{t("owner")} · {task.session_title}</p>
              {task.next_run_at ? <p>{t("nextRun")} · <time dateTime={task.next_run_at}>{wallTime(task.next_run_at)}</time></p> : task.status === "scheduled" && <p>{t("noNextRun")}</p>}
            </div>
            <div className="flex flex-wrap gap-2">
              <button type="button" data-testid="scheduled-task-open-session" onClick={() => onOpenSession(task.session_id)} className={buttonClass}>{t("openSession")}</button>
              <button type="button" data-testid="scheduled-task-cancel" disabled={loading} onClick={() => { setNotice(null); setCancelTarget(task); }} className={buttonClass}>{t("cancelTask")}</button>
            </div>
          </article>)}
        </section>}
      </div>
    </div>
  </LibraryDialog>;
}
