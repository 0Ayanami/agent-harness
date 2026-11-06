"use client";

import { ArrowUpRight, BookOpen, Search } from "lucide-react";
import { useLocale, useTranslations } from "@/lib/i18n";
import { retrievalMethodKey } from "@/i18n/rag-messages";
import { locationLabel } from "@/components/library/content-results";
import type { LibraryRetrieval, RuntimeMessage } from "@/types/runtime";

export function LibrarySources({ message }: { message: RuntimeMessage }) {
  const locale = useLocale();
  const t = useTranslations("rag");
  const library = useTranslations("library");
  if (!message.citations && !message.invalidCitations) return null;
  return <div className="mx-1 mb-4 mt-2 space-y-2">
    {!!message.citations?.length && <>
      <p className="flex items-center gap-1.5 text-[11px] font-medium text-[var(--color-text-secondary)]"><BookOpen size={12} />{t("sources")}</p>
      <div className="flex flex-wrap gap-2">{message.citations.map((source) => <a key={`${source.id}:${source.chunk_id}`} data-testid="rag-source" data-source-id={source.id}
        href={`/${locale}/library/?file=${encodeURIComponent(source.document_id)}&chunk=${encodeURIComponent(source.chunk_id)}`} target="_blank" rel="noopener noreferrer"
        title={`${t("sourceHint")} · ${t("sourceUnavailable")}`} className="inline-flex max-w-full items-center gap-2 rounded-lg border border-[var(--color-border)] px-3 py-2 text-xs transition-colors hover:bg-[var(--color-bg-secondary)]">
        <span className="shrink-0 font-mono text-[10px]">[{source.id}]</span><span className="min-w-0"><span className="block max-w-64 truncate font-medium">{source.name}</span>
          {locationLabel(source.location, library) && <span className="mt-0.5 block max-w-64 truncate text-[10px] text-[var(--color-text-secondary)]">{locationLabel(source.location, library)}</span>}</span><ArrowUpRight size={12} className="shrink-0" />
      </a>)}</div>
    </>}
    {!message.citations?.length && <p className="text-xs text-[var(--color-text-secondary)]">{t("noCitations")}</p>}
    {!!message.invalidCitations?.length && <p role="status" className="text-xs text-amber-700 dark:text-amber-400">{t("invalidCitation")}</p>}
    {message.citationWarning && <p role="status" className="break-words text-xs text-amber-700 dark:text-amber-400">{message.citationWarning}</p>}
  </div>;
}

export function LibraryRetrievalStatus({ retrieval, pending }: { retrieval?: LibraryRetrieval; pending: boolean }) {
  const t = useTranslations("rag");
  if (!retrieval && !pending) return null;
  return <div data-testid="rag-retrieval-status" role="status" className="mb-4 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-secondary)]/60 p-3 text-xs leading-6">
    <div className="flex flex-wrap items-center gap-x-3 text-[var(--color-text-secondary)]">
      <p className="flex items-center gap-2 text-[var(--color-text)]"><Search size={14} className="shrink-0" />{retrieval ? t(retrievalMethodKey(retrieval.mode)) : t("checking")}</p>
      {typeof retrieval?.reranked === "boolean" && <p data-testid="rag-rerank-status">{t(retrieval.reranked ? "reranked" : "notReranked")}</p>}
    </div>
    {retrieval && <p className="text-[var(--color-text-secondary)]">{retrieval.sources.length ? `${t("retrieved")} ${retrieval.sources.length} ${t("passages")}` : t("empty")}</p>}
    {retrieval?.warning && <p className="break-words text-amber-700 dark:text-amber-400">{retrieval.warning}</p>}
  </div>;
}
