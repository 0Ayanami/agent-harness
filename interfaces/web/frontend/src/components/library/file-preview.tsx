"use client";

import { useEffect, useRef, useState } from "react";
import { AlertCircle, Download, FileText, Loader2, Pencil, RefreshCw, Star, Trash2, X } from "lucide-react";
import { getLibraryChunk, getLibraryFile, getLibraryPreview, hasPendingIndex, LibraryApiError, libraryContentUrl, libraryErrorMessage, LIBRARY_FILE_ACCEPT, reindexLibraryFile } from "@/lib/library-api";
import { useLocale, useTranslations } from "@/lib/i18n";
import type { LibraryFile, LibraryPreview, LibrarySearchHit } from "@/types/library";
import { LibraryDialog } from "./library-dialog";
import { formatSize, formatDate } from "./library-format";
import { cn } from "@/lib/utils";
import { IndexStatus } from "./index-status";
import { locationLabel } from "./content-results";
import { VectorActions } from "./vector-status";

export function FilePreview({ id, chunkId, revision, busy, replacementProgress, onClose, onClearChunk, onReindexed, onRename, onDelete, onFavorite, onReplace }: {
  id: string;
  chunkId: string | null;
  revision: number;
  busy: boolean;
  replacementProgress: number | null;
  onClose: () => void;
  onClearChunk: () => void;
  onReindexed: () => void;
  onRename: (file: LibraryFile) => void;
  onDelete: (file: LibraryFile) => void;
  onFavorite: (file: LibraryFile) => void;
  onReplace: (file: LibraryFile, replacement: File) => void;
}) {
  const t = useTranslations("library");
  const rag = useTranslations("rag");
  const locale = useLocale();
  const replacement = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<LibraryFile | null>(null);
  const [preview, setPreview] = useState<LibraryPreview | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const [matched, setMatched] = useState<LibrarySearchHit | null>(null);
  const [matchError, setMatchError] = useState<unknown>(null);
  const [reindexing, setReindexing] = useState(false);
  const [indexError, setIndexError] = useState<unknown>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setPreview(null);
    setIndexError(null);
    setMatched(null);
    setMatchError(null);
    const load = async () => {
      // Replacement in another tab may happen between the two requests.
      // Only publish text with the metadata of the same content version.
      for (let check = 0; check < 3; check++) {
        const record = await getLibraryFile(id, controller.signal);
        if (controller.signal.aborted) return;
        const source = chunkId ? await getLibraryChunk(id, chunkId, controller.signal).catch((reason: unknown) => {
          if (!controller.signal.aborted) setMatchError(reason);
          return null;
        }) : null;
        if (controller.signal.aborted) return;
        if (source && source.version_id !== record.version_id) continue;
        const text = record.extension === "pdf" ? null : await getLibraryPreview(id, controller.signal);
        if (controller.signal.aborted) return;
        if (!text || text.version_id === record.version_id) {
          setFile(record);
          setPreview(text);
          setMatched(source);
          if (source) setMatchError(null);
          return;
        }
      }
      throw new Error("Content changed while loading preview");
    };
    load().catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [id, chunkId, revision, attempt]);

  const textIndexing = file?.ingestion?.status === "queued" || file?.ingestion?.status === "processing";
  const indexing = !!file && hasPendingIndex(file);
  const contentVersion = file?.version_id;
  useEffect(() => {
    if (!indexing || !contentVersion) return;
    const controller = new AbortController();
    let timer: number;
    const poll = async () => {
      try {
        const record = await getLibraryFile(id, controller.signal);
        if (controller.signal.aborted) return;
        if (record.version_id !== contentVersion) { setAttempt((value) => value + 1); return; }
        setFile(record);
        setIndexError(null);
        if (hasPendingIndex(record)) timer = window.setTimeout(() => void poll(), 1200);
      } catch (reason: unknown) {
        if (!controller.signal.aborted) setIndexError(reason);
      }
    };
    timer = window.setTimeout(() => void poll(), 1200);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [id, contentVersion, indexing, attempt]);

  async function reindex() {
    if (!file || reindexing || textIndexing) return;
    setReindexing(true);
    setIndexError(null);
    try {
      await reindexLibraryFile(file.id);
      setAttempt((value) => value + 1);
      onReindexed();
    } catch (reason) { setIndexError(reason); }
    finally { setReindexing(false); }
  }

  const page = matched?.location.page;
  const pdfFragment = page && Number.isInteger(page) && page > 0 ? `#page=${page}` : "";

  const iconButton = "inline-flex min-h-10 min-w-10 items-center justify-center rounded-lg border border-[var(--color-border)] transition-colors hover:bg-[var(--color-bg-secondary)] disabled:opacity-40";

  return <LibraryDialog drawer testId="library-preview" titleId="library-preview-title" onClose={onClose}>
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between gap-3 border-b border-[var(--color-border)] px-5 py-4 sm:px-7">
        <div className="flex min-w-0 items-center gap-3">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-[var(--color-bg-secondary)]"><FileText size={20} strokeWidth={1.5} /></span>
          <div className="min-w-0"><p className="text-[11px] text-[var(--color-text-secondary)]">{t("preview")}</p>
            <h2 id="library-preview-title" className="truncate text-sm font-semibold" title={file?.name}>{file?.name || t("previewLoading")}</h2></div>
        </div>
        <button data-testid="library-close" onClick={onClose} aria-label={t("close")} className="flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded-lg hover:bg-[var(--color-bg-secondary)]"><X size={20} /></button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-5 sm:p-7">
        {replacementProgress !== null && <div role="status" className="mb-4 rounded-xl bg-[var(--color-bg-secondary)] p-4 text-xs"><p className="mb-2 flex items-center gap-2"><Loader2 size={14} className="animate-spin" />{t("replacing")} {replacementProgress}%</p><progress max={100} value={replacementProgress} aria-label={t("progress")} className="h-1.5 w-full accent-zinc-600 dark:accent-zinc-300" /></div>}
        {file && <>
          <div className="mb-5 flex flex-wrap items-center gap-2">
            {file.ingestion?.status === "ready" ? <a data-testid="library-ask" href={`/${locale}/?library_file=${encodeURIComponent(file.id)}`} className="inline-flex min-h-10 items-center gap-2 rounded-lg border border-[var(--color-border)] px-3 text-xs font-medium hover:bg-[var(--color-bg-secondary)]">{rag("ask")}</a> : <button type="button" disabled title={rag("textFirst")} className="min-h-10 rounded-lg border border-[var(--color-border)] px-3 text-xs opacity-40">{rag("ask")}</button>}
            <a data-testid="library-download" href={libraryContentUrl(file.id)} download className="inline-flex min-h-10 items-center gap-2 rounded-lg bg-zinc-900 px-4 text-sm font-medium text-white dark:bg-zinc-100 dark:text-zinc-900"><Download size={15} />{t("download")}</a>
            <button aria-label={file.favorite ? t("unfavorite") : t("favorite")} aria-pressed={file.favorite} disabled={busy} onClick={() => onFavorite(file)} className={cn(iconButton, file.favorite && "text-amber-600 dark:text-amber-400")}><Star size={16} fill={file.favorite ? "currentColor" : "none"} /></button>
            <button aria-label={t("rename")} disabled={busy} onClick={() => onRename(file)} className={iconButton}><Pencil size={16} /></button>
            <button aria-label={t("delete")} disabled={busy} onClick={() => onDelete(file)} className={cn(iconButton, "text-red-600 dark:text-red-400")}><Trash2 size={16} /></button>
          </div>
          <dl className="mb-6 grid grid-cols-2 gap-x-4 gap-y-4 rounded-xl border border-[var(--color-border)] p-4 text-xs">
            <div><dt className="mb-1 text-[var(--color-text-secondary)]">{t("status")}</dt><dd className="flex items-center gap-1.5"><span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />{t("saved")}</dd></div>
            <div><dt className="mb-1 text-[var(--color-text-secondary)]">{t("size")}</dt><dd>{file.extension.toUpperCase()} · {formatSize(file.size_bytes, locale)}</dd></div>
            <div><dt className="mb-1 text-[var(--color-text-secondary)]">{t("uploaded")}</dt><dd>{formatDate(file.created_at, locale)}</dd></div>
            <div><dt className="mb-1 text-[var(--color-text-secondary)]">{t("modified")}</dt><dd>{formatDate(file.updated_at, locale)}</dd></div>
            <div className="col-span-2 min-w-0"><dt className="mb-1 text-[var(--color-text-secondary)]">{t("originalName")}</dt><dd className="break-all">{file.original_name}</dd></div>
            <div className="col-span-2 min-w-0"><dt className="mb-2 text-[var(--color-text-secondary)]">{t("textIndex")}</dt><dd><IndexStatus ingestion={file.ingestion} /></dd></div>
          </dl>
          <VectorActions key={`${file.id}:${file.version_id}`} file={file} disabled={busy} onQueued={() => { setAttempt((value) => value + 1); onReindexed(); }} />
          {file.ingestion?.error && <p role="status" className="mb-4 break-words text-xs leading-6 text-amber-700 dark:text-amber-400">{file.ingestion.error}</p>}
          <div className="mb-5"><button data-testid="library-reindex" onClick={() => void reindex()} disabled={busy || reindexing || textIndexing} className="inline-flex min-h-10 items-center gap-2 rounded-lg border border-[var(--color-border)] px-3 text-xs font-medium hover:bg-[var(--color-bg-secondary)] disabled:opacity-40"><RefreshCw size={14} className={cn(reindexing && "animate-spin")} />{t(reindexing || textIndexing ? "indexProcessing" : file.ingestion?.status === "ready" ? "rebuildIndex" : "retryIndex")}</button>
            {indexError != null && <p role="alert" className="mt-2 text-xs text-amber-700 dark:text-amber-400">{libraryErrorMessage(indexError, t)} <button className="underline" onClick={() => setAttempt((value) => value + 1)}>{t("retry")}</button></p>}
          </div>
        </>}
        {loading && <div role="status" className="flex min-h-40 items-center justify-center gap-2 text-sm text-[var(--color-text-secondary)]"><Loader2 size={17} className="animate-spin" />{t("previewLoading")}</div>}
        {!loading && error != null && <div role="alert" className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/20 dark:text-amber-200"><AlertCircle size={18} className="mb-2" /><p>{t("previewFailed")}</p><p className="mt-1 break-words text-xs">{libraryErrorMessage(error, t)}</p><button onClick={() => setAttempt((value) => value + 1)} className="mt-3 underline">{t("retry")}</button></div>}
        {!loading && !error && matched && <section data-testid="library-matched-chunk" aria-label={t("matchedPassage")} className="mb-5 rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-secondary)] p-4"><div className="mb-2 flex items-start justify-between gap-3"><div><h3 className="text-xs font-semibold">{t("matchedPassage")}</h3><p className="mt-1 text-[11px] text-[var(--color-text-secondary)]">{locationLabel(matched.location, t)}</p></div><button onClick={onClearChunk} className="shrink-0 text-[11px] underline">{t("clearMatch")}</button></div><p className="max-h-72 overflow-auto whitespace-pre-wrap break-words text-sm leading-7">{matched.text}</p></section>}
        {!loading && !error && chunkId && matchError != null && <p role="alert" className="mb-4 rounded-lg border border-amber-200 p-3 text-xs leading-6 text-amber-800 dark:border-amber-900 dark:text-amber-300">{t(matchError instanceof LibraryApiError && matchError.status === 404 ? "matchUnavailable" : "matchLoadFailed")} <button onClick={() => setAttempt((value) => value + 1)} className="mr-2 underline">{t("retry")}</button><button onClick={onClearChunk} className="underline">{t("clearMatch")}</button></p>}
        {!loading && !error && file?.extension === "pdf" && <>
          <iframe key={`${file.version_id}${pdfFragment}`} data-testid="library-preview-pdf" title={`${t("preview")}: ${file.name}`} src={`${libraryContentUrl(file.id, false, file.version_id)}${pdfFragment}`} className="h-[55vh] min-h-80 w-full rounded-xl border border-[var(--color-border)] bg-white" />
          <p className="mt-3 text-xs leading-relaxed text-[var(--color-text-secondary)]">{t("pdfFallback")}</p>
        </>}
        {!loading && !error && preview && <>
          <pre data-testid="library-preview-text" className="overflow-x-auto whitespace-pre-wrap break-words rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-secondary)] p-4 font-mono text-[13px] leading-7 sm:p-5">{preview.text}</pre>
          {preview.truncated && <p className="mt-3 text-xs text-[var(--color-text-secondary)]">{t("truncated")}</p>}
        </>}
        {file && <div className="mt-6 border-t border-[var(--color-border)] pt-5">
          <input ref={replacement} data-testid="library-replace" type="file" accept={file ? `.${file.extension}` : LIBRARY_FILE_ACCEPT} className="sr-only" tabIndex={-1}
            aria-label={t("replace")} disabled={busy} onChange={(event) => { const next = event.currentTarget.files?.[0]; event.currentTarget.value = ""; if (next) onReplace(file, next); }} />
          <button onClick={() => replacement.current?.click()} disabled={busy} className="inline-flex min-h-10 items-center gap-2 rounded-lg border border-[var(--color-border)] px-3 text-xs font-medium hover:bg-[var(--color-bg-secondary)] disabled:opacity-40"><RefreshCw size={14} />{t("replace")}</button>
          <p className="mt-2 text-xs leading-relaxed text-[var(--color-text-secondary)]">{t("replaceHint")}</p>
          <p className="mt-4 text-xs leading-relaxed text-[var(--color-text-secondary)]">{rag("savedHint")}</p>
        </div>}
      </div>
    </div>
  </LibraryDialog>;
}
