"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { LibraryDialog } from "@/components/library/library-dialog";
import { useLocale, useTranslations } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { Archive, ArchiveRestore, LayoutDashboard, LibraryBig, MessageSquare, Plus, Trash2 } from "lucide-react";
import type { SessionInfo } from "@/types/runtime";

interface SidebarProps {
  sessions: SessionInfo[];
  archivedSessions: SessionInfo[];
  selectedId: string | null;
  loading: boolean;
  busy: boolean;
  onSelect: (sessionId: string) => void;
  onCreate: () => void;
  onArchiveChange: (sessionId: string, archived: boolean) => Promise<boolean>;
  onDelete: (sessionId: string) => Promise<boolean>;
}

export function Sidebar({ sessions, archivedSessions, selectedId, loading, busy, onSelect, onCreate, onArchiveChange, onDelete }: SidebarProps) {
  const locale = useLocale();
  const runtime = useTranslations("runtime");
  const library = useTranslations("library");
  const pathname = usePathname().replace(/\/$/, "");
  const [deleteTarget, setDeleteTarget] = useState<SessionInfo | null>(null);
  const [deleteFailed, setDeleteFailed] = useState(false);
  const destinations = [
    { href: `/${locale}/`, label: runtime("workbench"), icon: LayoutDashboard },
    { href: `/${locale}/library/`, label: library("title"), icon: LibraryBig },
  ];
  const listClass = "flex max-h-56 gap-1 overflow-auto md:block md:space-y-0.5";
  const actionClass = "flex min-h-9 min-w-9 shrink-0 items-center justify-center rounded-md text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-secondary)] disabled:opacity-40";

  function rows(list: SessionInfo[], archived = false) {
    const ArchiveIcon = archived ? ArchiveRestore : Archive;
    const archiveLabel = runtime(archived ? "restoreConversation" : "archiveConversation");
    const deleteLabel = runtime("deleteConversation");
    return list.map((session) => {
      const active = !archived && selectedId === session.id;
      const actionsDisabled = loading || (active && busy);
      return (
        <div key={session.id} data-session-row={session.id}
          className={cn("flex w-52 shrink-0 items-center rounded-md md:w-full", active && "bg-zinc-100 dark:bg-zinc-800")}>
          {archived ? <span className="min-w-0 flex-1 truncate px-2.5 text-sm text-[var(--color-text-secondary)]" title={session.title}>{session.title}</span> :
            <button type="button" data-session-id={session.id} onClick={() => onSelect(session.id)}
              disabled={loading} aria-current={active ? "page" : undefined}
              className={cn("session-item flex min-h-10 min-w-0 flex-1 items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-sm transition-colors",
                active ? "active font-medium text-zinc-900 dark:text-white" : "text-zinc-500 hover:bg-zinc-50 hover:text-zinc-700 dark:text-zinc-400 dark:hover:bg-zinc-800/50 dark:hover:text-zinc-300")}
              title={session.title}>
              <MessageSquare size={13} className="shrink-0" />
              <span className="truncate">{session.title}</span>
            </button>}
          <button type="button" data-testid={archived ? "restore-conversation" : "archive-conversation"}
            className={actionClass} disabled={actionsDisabled} title={archiveLabel} aria-label={`${archiveLabel} · ${session.title}`}
            onClick={() => { void onArchiveChange(session.id, !archived); }}><ArchiveIcon size={15} /></button>
          <button type="button" data-testid="delete-conversation" className={actionClass} disabled={actionsDisabled}
            title={deleteLabel} aria-label={`${deleteLabel} · ${session.title}`}
            onClick={() => { setDeleteFailed(false); setDeleteTarget(session); }}><Trash2 size={15} /></button>
        </div>
      );
    });
  }

  return (
    <aside className="w-full shrink-0 md:w-56">
      <div className="space-y-5 md:sticky md:top-[calc(3.5rem+2rem)]">
        <nav aria-label={runtime("sessions")}>
          <button id="new-session" onClick={onCreate} disabled={loading}
            className="flex min-h-11 w-full items-center justify-center gap-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-secondary)] px-3 text-sm font-medium transition-colors hover:bg-zinc-200 disabled:opacity-50 dark:hover:bg-zinc-800">
            <Plus size={15} /> {runtime("newSession")}
          </button>
          <div className="mb-2 mt-5 flex items-center justify-between text-[11px] font-semibold uppercase tracking-wider text-zinc-400 dark:text-zinc-500">
            <span>{runtime("sessions")}</span><span id="session-count">{sessions.length}</span>
          </div>
          <div id="sessions" className={listClass}>
            {!sessions.length && <p className="px-2.5 py-1.5 text-xs text-[var(--color-text-secondary)]">{runtime("noSessions")}</p>}
            {rows(sessions)}
          </div>
          <details className="mt-3" data-testid="archived-conversations">
            <summary className="cursor-pointer rounded-md py-1.5 text-xs font-semibold text-[var(--color-text-secondary)]">
              {runtime("archivedConversations")}
              <span className="float-right" data-testid="archived-conversation-count">{archivedSessions.length}</span>
            </summary>
            <div className={listClass} data-testid="archived-conversation-list">
              {!archivedSessions.length && <p className="px-2.5 py-1.5 text-xs text-[var(--color-text-secondary)]">{runtime("noArchivedConversations")}</p>}
              {rows(archivedSessions, true)}
            </div>
          </details>
        </nav>
        <details data-testid="workspace-menu" className="hidden border-t border-[var(--color-border)] pt-4 md:block">
          <summary className="cursor-pointer rounded-md py-1 text-xs font-semibold text-[var(--color-text-secondary)]">{runtime("workspace")}</summary>
          <nav aria-label={runtime("workspace")} className="mt-2">
            <ul className="space-y-0.5">
              {destinations.map(({ href, label, icon: Icon }) => {
                const active = pathname === href.replace(/\/$/, "");
                return (
                  <li key={href}>
                    <Link
                      href={href}
                      aria-current={active ? "page" : undefined}
                      className={cn(
                        "flex min-h-10 items-center gap-2 rounded-md px-2.5 py-1.5 text-sm transition-colors",
                        active ? "bg-zinc-100 font-medium text-zinc-900 dark:bg-zinc-800 dark:text-white" : "text-zinc-500 hover:bg-zinc-50 hover:text-zinc-700 dark:text-zinc-400 dark:hover:bg-zinc-800/50 dark:hover:text-zinc-300"
                      )}
                    >
                      <Icon size={15} className="shrink-0" />
                      <span>{label}</span>
                    </Link>
                  </li>
                );
              })}
            </ul>
          </nav>
        </details>
      </div>
      {deleteTarget && <LibraryDialog titleId="delete-conversation-title" testId="delete-conversation-dialog"
        busy={loading} onClose={() => setDeleteTarget(null)}>
        <div className="space-y-3 p-5">
          <h2 id="delete-conversation-title" className="text-lg font-semibold">{runtime("deleteConversation")}</h2>
          <p className="break-words text-sm font-medium">{deleteTarget.title}</p>
          <p className="text-sm text-[var(--color-text-secondary)]">{runtime("deleteConversationHint")}</p>
          {deleteFailed && <p role="alert" className="text-sm text-red-700 dark:text-red-300">{runtime("deleteConversationFailed")}</p>}
          <div className="flex flex-wrap justify-end gap-2 pt-2">
            <button type="button" autoFocus disabled={loading} onClick={() => setDeleteTarget(null)}
              className="min-h-10 rounded-lg border border-[var(--color-border)] px-3 text-sm disabled:opacity-40">{runtime("cancelAction")}</button>
            <button type="button" data-testid="confirm-delete-conversation" disabled={loading} onClick={async () => {
              setDeleteFailed(false);
              if (await onDelete(deleteTarget.id)) setDeleteTarget(null);
              else setDeleteFailed(true);
            }} className="min-h-10 rounded-lg bg-red-700 px-3 text-sm font-medium text-white hover:bg-red-800 disabled:opacity-40">
              {runtime("confirmDeleteConversation")}
            </button>
          </div>
        </div>
      </LibraryDialog>}
    </aside>
  );
}
