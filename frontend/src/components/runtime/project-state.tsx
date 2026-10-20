"use client";

import { RefreshCw } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Tabs } from "@/components/ui/tabs";
import { useTranslations } from "@/lib/i18n";
import type { RuntimeState } from "@/types/runtime";

export function ProjectState({ state, onRefresh }: { state: RuntimeState; onRefresh: () => void }) {
  const t = useTranslations("runtime");
  const tasks = [...state.tasks, ...state.todos];
  const memories = state.memory_catalog.split("\n").filter(Boolean);
  return (
    <Card className="p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold">{t("context")}</h2>
        <button onClick={onRefresh} aria-label={t("refresh")} title={t("refresh")} className="flex h-9 w-9 items-center justify-center rounded-md hover:bg-zinc-100 dark:hover:bg-zinc-800"><RefreshCw size={14} /></button>
      </div>
      <Tabs tabs={[{ id: "tasks", label: `${t("tasks")} (${tasks.length})` }, { id: "memory", label: t("memory") }, { id: "team", label: t("team") }]}>
        {(tab) => tab === "tasks" ? (
          <div id="tasks" className="space-y-2 text-sm">
            {!tasks.length && <p className="text-[var(--color-text-secondary)]">{t("emptyTasks")}</p>}
            {tasks.map((task, index) => <div key={`${task.id ?? "todo"}-${index}`} className="flex gap-2">
              <span aria-label={t(task.status)} className={task.status === "completed" ? "text-emerald-500" : "text-[var(--color-text-secondary)]"}>
                {task.status === "completed" ? "✓" : task.status === "in_progress" ? "◉" : "○"}
              </span><span className="break-words">{task.subject || task.content}</span>
            </div>)}
          </div>
        ) : tab === "memory" ? (
          <div id="memories" className="space-y-2 text-sm">
            {!memories.length && <p className="text-[var(--color-text-secondary)]">{t("emptyMemory")}</p>}
            {memories.map((line, index) => <p key={index} className="break-words">{line.replace(/^-\s*/, "").replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")}</p>)}
          </div>
        ) : (
          <div className="space-y-2 text-sm">
            {!Object.keys(state.teammates).length && <p className="text-[var(--color-text-secondary)]">{t("emptyTeam")}</p>}
            {Object.entries(state.teammates).map(([name, status]) => <div key={name} className="flex justify-between gap-2"><span>{name}</span><span className="text-[var(--color-text-secondary)]">{status}</span></div>)}
          </div>
        )}
      </Tabs>
    </Card>
  );
}
