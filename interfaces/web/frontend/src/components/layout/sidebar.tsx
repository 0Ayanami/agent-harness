"use client";

import { LAYERS, VERSION_META } from "@/lib/constants";
import { useLocale, useTranslations } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { teachingHref } from "@/lib/teaching";
import { Plus, MessageSquare } from "lucide-react";
import type { SessionInfo } from "@/types/runtime";

const LAYER_DOT_BG: Record<string, string> = {
  tools: "bg-blue-500",
  planning: "bg-emerald-500",
  memory: "bg-purple-500",
  concurrency: "bg-amber-500",
  collaboration: "bg-red-500",
};

interface SidebarProps {
  sessions: SessionInfo[];
  selectedId: string | null;
  loading: boolean;
  onSelect: (sessionId: string) => void;
  onCreate: () => void;
}

// Original chapter grouping is retained; live sessions are an additional section.
export function Sidebar({ sessions, selectedId, loading, onSelect, onCreate }: SidebarProps) {
  const locale = useLocale();
  const t = useTranslations("sessions");
  const tLayer = useTranslations("layer_labels");
  const runtime = useTranslations("runtime");

  return (
    <nav className="w-full shrink-0 md:w-56" aria-label={runtime("sessions")}>
      <div className="space-y-5 md:sticky md:top-[calc(3.5rem+2rem)]">
        <div>
          <button id="new-session" onClick={onCreate} disabled={loading}
            className="flex min-h-11 w-full items-center justify-center gap-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-secondary)] px-3 text-sm font-medium transition-colors hover:bg-zinc-200 disabled:opacity-50 dark:hover:bg-zinc-800">
            <Plus size={15} /> {runtime("newSession")}
          </button>
          <div className="mb-2 mt-5 flex items-center justify-between text-[11px] font-semibold uppercase tracking-wider text-zinc-400 dark:text-zinc-500">
            <span>{runtime("sessions")}</span><span id="session-count">{sessions.length}</span>
          </div>
          <div id="sessions" className="flex max-h-56 gap-1 overflow-auto md:block md:space-y-0.5">
            {!sessions.length && <p className="px-2.5 py-1.5 text-xs text-[var(--color-text-secondary)]">{runtime("noSessions")}</p>}
            {sessions.map((session) => (
              <button key={session.id} data-session-id={session.id} onClick={() => onSelect(session.id)}
                disabled={loading} aria-current={selectedId === session.id ? "page" : undefined}
                className={cn("session-item flex min-h-10 shrink-0 items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-sm transition-colors md:w-full",
                  selectedId === session.id ? "active bg-zinc-100 font-medium text-zinc-900 dark:bg-zinc-800 dark:text-white" : "text-zinc-500 hover:bg-zinc-50 hover:text-zinc-700 dark:text-zinc-400 dark:hover:bg-zinc-800/50 dark:hover:text-zinc-300")}
                title={session.title}>
                <MessageSquare size={13} className="shrink-0" />
                <span className="max-w-40 truncate">{session.title}</span>
              </button>
            ))}
          </div>
        </div>
        <details className="hidden border-t border-[var(--color-border)] pt-4 md:block">
          <summary className="cursor-pointer text-xs font-semibold text-[var(--color-text-secondary)]">{runtime("course")}</summary>
          <div className="mt-4 max-h-[50vh] space-y-5 overflow-y-auto">
        {LAYERS.map((layer) => (
          <div key={layer.id}>
            <div className="flex items-center gap-1.5 pb-1.5">
              <span className={cn("h-2 w-2 rounded-full", LAYER_DOT_BG[layer.id])} />
              <span className="text-[11px] font-semibold uppercase tracking-wider text-zinc-400 dark:text-zinc-500">
                {tLayer(layer.id)}
              </span>
            </div>
            <ul className="space-y-0.5">
              {layer.versions.map((vId) => {
                const meta = VERSION_META[vId];
                const href = teachingHref(locale, `/${vId}`);

                return (
                  <li key={vId}>
                    <a
                      href={href}
                      target="_blank"
                      rel="noopener noreferrer"
                      className={cn(
                        "block rounded-md px-2.5 py-1.5 text-sm transition-colors",
                        "text-zinc-500 hover:bg-zinc-50 hover:text-zinc-700 dark:text-zinc-400 dark:hover:bg-zinc-800/50 dark:hover:text-zinc-300"
                      )}
                    >
                      <span className="font-mono text-xs">{vId}</span>
                      <span className="ml-1.5">{t(vId) || meta?.title}</span>
                    </a>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
          </div>
        </details>
      </div>
    </nav>
  );
}
