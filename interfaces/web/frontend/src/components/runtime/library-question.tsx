"use client";

import { useEffect, useState, type ReactNode } from "react";
import { BookOpen, Loader2, RefreshCw } from "lucide-react";
import { getRetrievalStatus, listQuestionFiles } from "@/lib/library-api";
import { useTranslations } from "@/lib/i18n";
import type { LibraryFile, RetrievalStatus } from "@/types/library";
import type { LibraryQuestion } from "@/types/runtime";

export function LibraryQuestionControls({ value, onChange, disabled, onValidityChange, children }: {
  value: LibraryQuestion;
  onChange: (value: LibraryQuestion) => void;
  disabled: boolean;
  onValidityChange: (valid: boolean) => void;
  children?: ReactNode;
}) {
  const t = useTranslations("rag");
  const [files, setFiles] = useState<LibraryFile[]>([]);
  const [status, setStatus] = useState<RetrievalStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const [filter, setFilter] = useState("");
  const [attempt, setAttempt] = useState(0);
  const scope = value.document_ids !== null ? "selected" : value.favorites_only ? "favorites" : "all";

  useEffect(() => {
    if (!value.enabled) return;
    const controller = new AbortController();
    setLoading(true);
    setFailed(false);
    Promise.all([listQuestionFiles(controller.signal), getRetrievalStatus(controller.signal).catch(() => null)])
      .then(([items, health]) => { if (!controller.signal.aborted) { setFiles(items); setStatus(health); } })
      .catch(() => { if (!controller.signal.aborted) setFailed(true); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [value.enabled, attempt]);

  const missing = value.document_ids?.some((id) => !files.some((file) => file.id === id)) ?? false;
  const valid = !value.enabled || scope !== "selected" || (!loading && !failed && !!value.document_ids?.length && !missing);
  useEffect(() => { onValidityChange(valid); }, [valid, onValidityChange]);
  const selectStyle = "min-h-10 max-w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] px-3 text-xs disabled:opacity-50";

  return <div className="mb-3 rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-secondary)]/35 p-3 text-sm sm:p-4">
    <div className="flex flex-wrap items-center gap-x-5 gap-y-1">
      <label className="inline-flex min-h-8 cursor-pointer items-center gap-2.5 font-medium">
        <input data-testid="rag-enabled" type="checkbox" checked={value.enabled} disabled={disabled}
          onChange={(event) => onChange({ ...value, enabled: event.target.checked })} className="h-4 w-4 accent-zinc-800 dark:accent-zinc-200" />
        <BookOpen size={15} strokeWidth={1.6} />{t("enabled")}
      </label>
      {children}
    </div>
    {value.enabled && <div className="mt-2 space-y-3">
      <p className="text-xs leading-5 text-[var(--color-text-secondary)]">{t("enabledHint")}</p>
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex min-w-0 flex-wrap items-center gap-2 text-xs"><span>{t("scope")}</span>
          <select data-testid="rag-scope" value={scope} disabled={disabled} aria-label={t("scope")} className={selectStyle}
            onChange={(event) => onChange({ ...value, document_ids: event.target.value === "selected" ? [] : null, favorites_only: event.target.value === "favorites" })}>
            <option value="all">{t("all")}</option><option value="favorites">{t("favorites")}</option><option value="selected">{t("selected")}</option>
          </select>
        </label>
        <label className="flex min-w-0 flex-wrap items-center gap-2 text-xs"><span>{t("mode")}</span>
          <select data-testid="rag-mode" value={value.mode} disabled={disabled} aria-label={t("mode")} className={selectStyle}
            onChange={(event) => onChange({ ...value, mode: event.target.value as LibraryQuestion["mode"] })}>
            <option value="hybrid">{t("hybrid")}</option><option value="keyword">{t("keyword")}</option><option value="vector">{t("vector")}</option>
          </select>
        </label>
        <button type="button" onClick={() => setAttempt((current) => current + 1)} disabled={disabled || loading} aria-label={t("refresh")} className="flex min-h-10 min-w-10 items-center justify-center rounded-lg hover:bg-[var(--color-bg-secondary)] disabled:opacity-40"><RefreshCw size={14} /></button>
      </div>
      {scope === "selected" && <div data-testid="rag-document-picker" className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] p-3">
        <p className="mb-2 text-xs font-medium">{t("picker")}</p>
        <input type="search" value={filter} onChange={(event) => setFilter(event.target.value)} aria-label={t("findFile")} placeholder={t("findFile")} disabled={disabled}
          className="mb-2 min-h-10 w-full rounded-lg border border-[var(--color-border)] bg-transparent px-3 text-xs outline-none focus:ring-2 focus:ring-zinc-400/30" />
        {loading ? <p role="status" className="flex items-center gap-2 py-3 text-xs"><Loader2 size={14} className="animate-spin" />{t("filesLoading")}</p> : failed ? <p role="alert" className="text-xs text-amber-700 dark:text-amber-400">{t("fileLoadFailed")}</p> : <>
          {!files.length && <p className="py-2 text-xs text-[var(--color-text-secondary)]">{t("noFiles")}</p>}
          <div className="max-h-40 space-y-0.5 overflow-auto">{files.filter((file) => file.name.toLocaleLowerCase().includes(filter.toLocaleLowerCase())).map((file) => {
            const checked = value.document_ids?.includes(file.id) ?? false;
            return <label key={file.id} className="flex min-h-10 items-center gap-2 rounded-md px-2 text-xs hover:bg-[var(--color-bg-secondary)]">
              <input type="checkbox" value={file.id} checked={checked} disabled={disabled || (!checked && (value.document_ids?.length ?? 0) >= 100)}
                onChange={() => onChange({ ...value, document_ids: checked ? (value.document_ids || []).filter((id) => id !== file.id) : [...(value.document_ids || []), file.id] })} className="h-4 w-4 shrink-0 accent-zinc-800 dark:accent-zinc-200" />
              <span className="truncate" title={file.name}>{file.name}</span>
            </label>;
          })}</div>
          {!value.document_ids?.length && <p role="status" className="mt-2 text-xs text-amber-700 dark:text-amber-400">{t("noSelection")}</p>}
          {missing && <p role="alert" className="mt-2 text-xs text-amber-700 dark:text-amber-400">{t("selectedMissing")}</p>}
          {(value.document_ids?.length ?? 0) >= 100 && <p className="mt-2 text-xs text-[var(--color-text-secondary)]">{t("scopeLimit")}</p>}
        </>}
      </div>}
      {value.mode !== "keyword" && status?.configured === false && <p className="text-xs leading-5 text-amber-700 dark:text-amber-400">{t(value.mode === "vector" ? "vectorConfigMissing" : "configMissing")}</p>}
    </div>}
  </div>;
}
