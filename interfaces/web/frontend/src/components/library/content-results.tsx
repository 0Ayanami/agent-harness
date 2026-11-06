"use client";

import { AlertCircle, ArrowUpRight, FileText, Loader2, Search } from "lucide-react";
import { useTranslations } from "@/lib/i18n";
import { retrievalMethodKey } from "@/i18n/rag-messages";
import { libraryErrorMessage } from "@/lib/library-api";
import type { LibraryFilters, LibraryLocation, LibrarySearchResults } from "@/types/library";

export function locationLabel(location: LibraryLocation, t: (key: string) => string): string {
  const parts: string[] = [];
  if (location.page) parts.push(`${t("page")} ${location.page}`);
  if (location.start_line) parts.push(`${t("lines")} ${location.start_line}${location.end_line && location.end_line !== location.start_line ? `–${location.end_line}` : ""}`);
  if (location.heading) parts.push(location.heading);
  return parts.join(" · ");
}

export function ContentResults({ results, loading, error, query, mode, onOpen, onRetry }: {
  results: LibrarySearchResults | null;
  loading: boolean;
  error: unknown;
  query: string;
  mode: Exclude<LibraryFilters["mode"], "filename">;
  onOpen: (fileId: string, chunkId: string) => void;
  onRetry: () => void;
}) {
  const t = useTranslations("library");
  const rag = useTranslations("rag");
  return <section data-testid="library-search-results" aria-label={t("contentResults")} aria-busy={loading}>
    <p className="mb-4 text-xs leading-6 text-[var(--color-text-secondary)]">{mode === "content" ? t("keywordHint") : rag(mode === "vector" ? "vectorHint" : "hybridHint")}</p>
    {loading && <p role="status" className="flex min-h-36 items-center justify-center gap-2 text-sm text-[var(--color-text-secondary)]"><Loader2 size={16} className="animate-spin" />{t("searchingContent")}</p>}
    {!loading && error != null && <div role="alert" className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/20 dark:text-amber-200"><AlertCircle size={18} className="mb-2" /><p>{libraryErrorMessage(error, t)}</p><button className="mt-3 underline" onClick={onRetry}>{t("retry")}</button></div>}
    {!loading && error == null && !query.trim() && <div className="flex min-h-64 flex-col items-center justify-center rounded-2xl border border-dashed border-[var(--color-border)] px-5 py-10 text-center"><Search size={28} strokeWidth={1.3} className="mb-4 text-zinc-400" /><h3 className="font-medium">{t("contentPrompt")}</h3><p className="mt-2 max-w-sm text-sm leading-6 text-[var(--color-text-secondary)]">{t("contentPromptHint")}</p></div>}
    {!loading && error == null && results && <>
      <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-[var(--color-text-secondary)]">
        <p data-testid="library-retrieval-mode">{rag(retrievalMethodKey(results.mode))}</p>
        {typeof results.reranked === "boolean" && <p data-testid="library-rerank-status">{rag(results.reranked ? "reranked" : "notReranked")}</p>}
      </div>
      <p role="status" className="mb-3 text-xs text-[var(--color-text-secondary)]">{t("contentResults")} · {results.hits.length}{results.hits.length === 20 ? ` · ${t("contentLimit")}` : ""}</p>
      {results.warning && <p role="status" className="mb-3 text-xs text-amber-700 dark:text-amber-400">{results.warning}</p>}
      {!results.hits.length && <div className="rounded-xl border border-dashed border-[var(--color-border)] px-5 py-12 text-center"><h3 className="font-medium">{t("noContentResults")}</h3><p className="mx-auto mt-2 max-w-sm text-sm leading-6 text-[var(--color-text-secondary)]">{t("contentResultsHint")}</p></div>}
      <div className="space-y-3">{results.hits.map((hit) => <button key={hit.chunk_id} data-testid="library-search-hit" data-document-id={hit.document_id} data-chunk-id={hit.chunk_id}
        onClick={() => onOpen(hit.document_id, hit.chunk_id)} className="group block w-full rounded-xl border border-[var(--color-border)] p-4 text-left transition-colors hover:bg-[var(--color-bg-secondary)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-zinc-500 sm:p-5">
        <span className="flex min-w-0 items-center gap-2 text-sm font-medium"><FileText size={16} className="shrink-0 text-[var(--color-text-secondary)]" /><span className="min-w-0 flex-1 truncate">{hit.name}</span><ArrowUpRight size={16} className="shrink-0 text-[var(--color-text-secondary)]" /></span>
        {locationLabel(hit.location, t) && <span className="mt-2 block truncate text-[11px] text-[var(--color-text-secondary)]">{locationLabel(hit.location, t)}</span>}
        <span className="mt-3 line-clamp-4 whitespace-pre-wrap break-words text-sm leading-7 text-[var(--color-text-secondary)]">{hit.text}</span>
      </button>)}</div>
    </>}
  </section>;
}
