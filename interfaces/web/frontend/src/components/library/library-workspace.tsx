"use client";

import { useEffect, useRef, useState, type ChangeEvent } from "react";
import { AlertCircle, ArrowDownToLine, Check, ChevronLeft, ChevronRight, Clock3, File, FileText, Files, LibraryBig, Loader2, Pencil, Plus, RefreshCw, Search, Star, Trash2, Upload, X } from "lucide-react";
import { useLibrary } from "@/hooks/use-library";
import { useLocale, useTranslations } from "@/lib/i18n";
import { deleteLibraryFile, libraryContentUrl, libraryErrorMessage, LIBRARY_FILE_ACCEPT, patchLibraryFile, uploadLibraryFile, validateLibraryFile } from "@/lib/library-api";
import { cn } from "@/lib/utils";
import type { LibraryFile, LibraryFilters, LibrarySort, LibraryView } from "@/types/library";
import { FilePreview } from "./file-preview";
import { LibraryDialog } from "./library-dialog";
import { formatDate, formatSize } from "./library-format";
import { ContentResults } from "./content-results";
import { VectorStatus } from "./vector-status";
import { IndexStatus } from "./index-status";

type Action = { kind: "rename" | "delete"; file: LibraryFile };
type UploadProgress = { name: string; index: number; total: number; percent: number };

export function LibraryWorkspace() {
  const t = useTranslations("library");
  const rag = useTranslations("rag");
  const locale = useLocale();
  const library = useLibrary();
  const input = useRef<HTMLInputElement>(null);
  const dragDepth = useRef(0);
  const [dragging, setDragging] = useState(false);
  const [upload, setUpload] = useState<UploadProgress | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [action, setAction] = useState<Action | null>(null);
  const [newName, setNewName] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 6000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  async function receiveFiles(files: File[]) {
    if (upload || !files.length) return;
    setError(null);
    setNotice(null);
    const failures: string[] = [];
    let completed = 0;
    for (const [index, file] of files.entries()) {
      const invalid = validateLibraryFile(file);
      if (invalid) { failures.push(`${file.name}: ${t(invalid)}`); continue; }
      setUpload({ name: file.name, index: index + 1, total: files.length, percent: 0 });
      try {
        await uploadLibraryFile(file, (percent) => setUpload({ name: file.name, index: index + 1, total: files.length, percent }));
        completed++;
      } catch (reason) { failures.push(`${file.name}: ${libraryErrorMessage(reason, t)}`); }
    }
    setUpload(null);
    if (completed) { setNotice(`${t("uploadComplete")} · ${completed}`); library.refresh(); }
    if (failures.length) setError(failures.join("\n"));
  }

  async function favorite(file: LibraryFile) {
    if (busyId) return;
    setBusyId(file.id);
    setError(null);
    try { await patchLibraryFile(file.id, { favorite: !file.favorite }); library.refresh(); }
    catch (reason) { setError(libraryErrorMessage(reason, t)); }
    finally { setBusyId(null); }
  }

  function openAction(kind: Action["kind"], file: LibraryFile) {
    setActionError(null);
    setNewName(file.name);
    setAction({ kind, file });
  }

  async function confirmAction() {
    if (!action || busyId) return;
    if (action.kind === "rename") {
      const name = newName.trim();
      if (!name || name.length > 255 || /[\\/\x00-\x1f]/.test(name) || !name.toLowerCase().endsWith(`.${action.file.extension}`)) {
        setActionError(t("invalidName")); return;
      }
    }
    setBusyId(action.file.id);
    setActionError(null);
    try {
      if (action.kind === "rename") await patchLibraryFile(action.file.id, { name: newName.trim() });
      else {
        await deleteLibraryFile(action.file.id);
        if (library.selectedId === action.file.id) library.select(null);
      }
      setAction(null);
      library.refresh();
    } catch (reason) { setActionError(libraryErrorMessage(reason, t)); }
    finally { setBusyId(null); }
  }

  async function replace(file: LibraryFile, replacement: File) {
    if (upload || busyId) return;
    const invalid = validateLibraryFile(replacement);
    if (invalid) { setError(t(invalid)); return; }
    if (!replacement.name.toLowerCase().endsWith(`.${file.extension}`)) { setError(t("invalidName")); return; }
    setError(null);
    setNotice(null);
    setBusyId(file.id);
    setUpload({ name: replacement.name, index: 1, total: 1, percent: 0 });
    try {
      await uploadLibraryFile(replacement, (percent) => setUpload({ name: replacement.name, index: 1, total: 1, percent }), file.id);
      setNotice(t("uploadComplete"));
      library.refresh();
    } catch (reason) { setError(libraryErrorMessage(reason, t)); }
    finally { setUpload(null); setBusyId(null); }
  }

  const filters = library.filters;
  const listing = library.listing;
  const hasFilters = !!filters.query || !!filters.extension || filters.view !== "all";
  const loadError = library.error != null ? libraryErrorMessage(library.error, t) : null;
  const label = filters.view === "favorites" ? "favorites" : filters.view === "recent" ? "recent" : "all";
  const buttonStyle = "inline-flex min-h-10 items-center justify-center gap-2 rounded-lg border border-[var(--color-border)] px-3 text-sm font-medium transition-colors hover:bg-[var(--color-bg-secondary)] disabled:opacity-40";
  const primaryStyle = "inline-flex min-h-10 items-center justify-center gap-2 rounded-lg bg-zinc-900 px-4 text-sm font-medium text-white transition-colors hover:bg-zinc-700 disabled:opacity-40 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-white";

  const semanticSearch = filters.mode === "vector" || filters.mode === "hybrid";

  return <div data-testid="library-page" className="relative min-w-0"
    onDragEnter={(event) => { if (event.dataTransfer.types.includes("Files")) { event.preventDefault(); dragDepth.current++; setDragging(true); } }}
    onDragOver={(event) => { if (event.dataTransfer.types.includes("Files")) event.preventDefault(); }}
    onDragLeave={(event) => { event.preventDefault(); dragDepth.current = Math.max(0, dragDepth.current - 1); if (!dragDepth.current) setDragging(false); }}
    onDrop={(event) => { event.preventDefault(); dragDepth.current = 0; setDragging(false); void receiveFiles(Array.from(event.dataTransfer.files)); }}>
    {dragging && <div className="pointer-events-none absolute inset-0 z-40 flex min-h-80 items-center justify-center rounded-2xl border-2 border-dashed border-zinc-500 bg-[var(--color-bg)]/95"><div className="text-center"><Upload className="mx-auto mb-4" size={32} /><p className="font-medium">{t("uploadDrop")}</p><p className="mt-2 text-xs text-[var(--color-text-secondary)]">{t("uploadHint")}</p></div></div>}
    <input ref={input} data-testid="library-upload" type="file" accept={LIBRARY_FILE_ACCEPT} multiple className="sr-only" tabIndex={-1} aria-label={t("upload")} disabled={!!upload}
      onChange={(event: ChangeEvent<HTMLInputElement>) => { const files = Array.from(event.currentTarget.files || []); event.currentTarget.value = ""; void receiveFiles(files); }} />

    <div className="mb-8 flex flex-wrap items-start justify-between gap-4">
      <div><div className="mb-2 flex items-center gap-3"><LibraryBig size={25} strokeWidth={1.5} /><h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1></div>
        <p className="text-sm text-[var(--color-text-secondary)]">{t("subtitle")}</p></div>
      <button onClick={() => input.current?.click()} disabled={!!upload} className={primaryStyle}><Plus size={17} />{t("upload")}</button>
    </div>

    <div className="flex min-w-0 flex-col gap-6 md:flex-row md:gap-8">
      <aside className="shrink-0 md:w-44 lg:w-48">
        <nav aria-label={t("title")} className="flex gap-1 overflow-x-auto md:sticky md:top-24 md:flex-col">
          {([{ view: "all", icon: Files, key: "all", count: listing?.counts.all }, { view: "favorites", icon: Star, key: "favorites", count: listing?.counts.favorites }, { view: "recent", icon: Clock3, key: "recent", count: undefined }] as const).map(({ view, icon: Icon, key, count }) => (
            <button key={view} data-testid={`library-filter-${view}`} aria-current={filters.view === view ? "page" : undefined}
              onClick={() => library.updateFilters({ view: view as LibraryView, ...(view === "recent" ? { sort: "updated_desc" as const } : {}) })}
              className={cn("flex min-h-11 shrink-0 items-center gap-2.5 rounded-lg px-3 text-left text-sm transition-colors md:w-full", filters.view === view ? "bg-[var(--color-bg-secondary)] font-medium" : "text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-secondary)]")}>
              <Icon size={16} strokeWidth={1.6} /><span className="flex-1 whitespace-nowrap">{t(key)}</span>
              {count !== undefined && <span className="ml-1 text-xs tabular-nums text-[var(--color-text-secondary)]">{count}</span>}
            </button>
          ))}
          <div className="mt-6 hidden border-t border-[var(--color-border)] px-3 pt-4 md:block"><p className="text-[11px] font-medium text-[var(--color-text-secondary)]">{t("storage")}</p><p className="mt-2 text-xs leading-6 text-[var(--color-text-secondary)]">{t("uploadHint")}</p></div>
        </nav>
      </aside>

      <section className="min-w-0 flex-1" aria-labelledby="library-list-title">
        <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
          <h2 id="library-list-title" className="flex items-center gap-2 text-base font-semibold">{t(label)}<span className="rounded-md bg-[var(--color-bg-secondary)] px-1.5 py-0.5 text-[11px] font-normal tabular-nums text-[var(--color-text-secondary)]">{listing?.total ?? "—"}</span></h2>
          <button onClick={library.refresh} disabled={library.loading} aria-label={t("refresh")} className="flex min-h-10 min-w-10 items-center justify-center rounded-lg text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-secondary)] disabled:opacity-40"><RefreshCw size={15} className={cn(library.loading && "animate-spin")} /></button>
        </div>
        <div className="mb-5 flex flex-wrap gap-2">
          <select data-testid="library-search-mode" aria-label={t("searchMode")} value={filters.mode}
            onChange={(event) => library.updateFilters({ mode: event.target.value as LibraryFilters["mode"], extension: "" })}
            className="h-10 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] px-3 text-xs outline-none focus:ring-2 focus:ring-zinc-400/30">
            <option value="filename">{t("filenameMode")}</option><option value="content">{t("contentMode")}</option>
            <option value="vector">{rag("vector")}</option><option value="hybrid">{rag("hybrid")}</option>
          </select>
          <div className="relative min-w-40 flex-1"><Search size={16} className="pointer-events-none absolute left-3 top-3 text-[var(--color-text-secondary)]" /><input data-testid="library-search" type="search" value={filters.query} maxLength={200}
            onChange={(event) => library.updateFilters({ query: event.target.value })} placeholder={semanticSearch ? rag("searchSemantic") : t(filters.mode === "content" ? "contentSearch" : "search")} aria-label={semanticSearch ? rag("searchSemantic") : t(filters.mode === "content" ? "contentSearchLabel" : "searchLabel")}
            className="h-10 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] pl-9 pr-3 text-sm outline-none transition-shadow focus:ring-2 focus:ring-zinc-400/30" /></div>
          {filters.mode === "filename" && <>
            <select value={filters.extension} aria-label={t("types")} data-testid="library-type-filter" onChange={(event) => library.updateFilters({ extension: event.target.value })} className="h-10 min-w-0 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] px-3 text-xs outline-none focus:ring-2 focus:ring-zinc-400/30"><option value="">{t("allTypes")}</option><option value="md">Markdown</option><option value="txt">TXT</option><option value="pdf">PDF</option></select>
            <select value={filters.sort} aria-label={t("sort")} data-testid="library-sort" onChange={(event) => library.updateFilters({ sort: event.target.value as LibrarySort })} className="h-10 min-w-0 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] px-3 text-xs outline-none focus:ring-2 focus:ring-zinc-400/30"><option value="updated_desc">{t("updated")}</option><option value="name_asc">{t("nameSort")}</option><option value="size_desc">{t("sizeSort")}</option></select>
          </>}
        </div>

        {(error || loadError) && <div data-testid="library-error" role="alert" className="mb-4 flex items-start gap-3 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/20 dark:text-red-200"><AlertCircle size={17} className="mt-0.5 shrink-0" /><div className="min-w-0 flex-1"><p className="whitespace-pre-wrap break-words">{error || loadError}</p>{loadError && <button onClick={library.refresh} className="mt-2 underline">{t("retry")}</button>}</div>{error && <button onClick={() => setError(null)} aria-label={t("dismiss")}><X size={16} /></button>}</div>}
        {notice && <p role="status" className="mb-4 flex items-center gap-2 rounded-lg bg-emerald-50 px-3 py-2 text-xs text-emerald-800 dark:bg-emerald-950/20 dark:text-emerald-200"><Check size={14} />{notice}</p>}
        {upload && <div role="status" aria-label={t("progress")} className="mb-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-secondary)] p-4"><div className="mb-2 flex items-center gap-2 text-xs"><Loader2 size={14} className="shrink-0 animate-spin" /><span className="shrink-0">{t("uploading")} {upload.index}/{upload.total}</span><span className="min-w-0 flex-1 truncate">{upload.name}</span><span className="tabular-nums">{upload.percent}%</span></div><progress max={100} value={upload.percent} aria-label={t("progress")} className="h-1.5 w-full accent-zinc-600 dark:accent-zinc-300" /></div>}

        {filters.mode !== "filename" && <ContentResults results={library.results} loading={library.searchLoading} error={library.searchError} query={filters.query} mode={filters.mode} onOpen={library.select} onRetry={library.refresh} />}
        {filters.mode === "filename" && <>
        {library.loading && !listing && <div role="status" className="space-y-3 rounded-xl border border-[var(--color-border)] p-5"><p className="flex items-center gap-2 py-2 text-sm text-[var(--color-text-secondary)]"><Loader2 size={16} className="animate-spin" />{t("loading")}</p>{[1, 2, 3].map((index) => <div key={index} className="h-12 animate-pulse rounded-lg bg-[var(--color-bg-secondary)]" />)}</div>}
        {listing && listing.items.length > 0 && <div role="table" aria-label={t("all")} aria-busy={library.loading} className={cn("overflow-hidden rounded-xl border border-[var(--color-border)]", library.loading && "opacity-60")}>
          <div role="row" className="hidden grid-cols-[minmax(0,1fr)_7.5rem_4.5rem_8.5rem] items-center gap-3 border-b border-[var(--color-border)] bg-[var(--color-bg-secondary)]/60 px-4 py-3 text-[11px] text-[var(--color-text-secondary)] sm:grid"><span role="columnheader">{t("name")}</span><span role="columnheader">{t("modified")}</span><span role="columnheader">{t("size")}</span><span role="columnheader" className="text-right">{t("actions")}</span></div>
          <div className="divide-y divide-[var(--color-border)]">{listing.items.map((file) => <div key={file.id} role="row" data-testid="library-file-row" data-file-id={file.id}
            className="group flex min-w-0 items-center gap-2 px-3 py-3 transition-colors hover:bg-[var(--color-bg-secondary)]/60 sm:grid sm:grid-cols-[minmax(0,1fr)_7.5rem_4.5rem_8.5rem] sm:gap-3 sm:px-4">
            <div role="cell" className="min-w-0 flex-1"><button data-testid="library-open" onClick={() => library.select(file.id)} className="flex min-h-10 w-full min-w-0 items-center gap-3 rounded-md text-left outline-offset-4" aria-label={`${t("open")}: ${file.name}`}>
              <span className={cn("flex h-10 w-9 shrink-0 items-center justify-center rounded-lg border border-[var(--color-border)]", file.extension === "pdf" ? "bg-red-50 text-red-500 dark:bg-red-950/20 dark:text-red-400" : "bg-[var(--color-bg-secondary)] text-[var(--color-text-secondary)]")}><FileText size={18} strokeWidth={1.5} /></span>
              <span className="min-w-0"><span className="block truncate text-sm font-medium" title={file.name}>{file.name}</span>
                <span className="mt-1 block truncate text-[10px] text-[var(--color-text-secondary)]">{file.extension.toUpperCase()}<span className="sm:hidden"> · {formatSize(file.size_bytes, locale)}</span><span className="ml-2 inline-flex items-center gap-1"><span className="h-1 w-1 rounded-full bg-emerald-500" />{t("saved")}</span></span>
                <span className="mt-1 block"><IndexStatus ingestion={file.ingestion} compact /></span>
                <span className="mt-1 block"><VectorStatus vector={file.vector} compact /></span>
              </span>
            </button></div>
            <span role="cell" className="hidden text-xs text-[var(--color-text-secondary)] sm:block">{formatDate(file.updated_at, locale)}</span>
            <span role="cell" className="hidden text-xs tabular-nums text-[var(--color-text-secondary)] sm:block">{formatSize(file.size_bytes, locale)}</span>
            <div role="cell" className="flex shrink-0 items-center justify-end gap-0.5">
              <button data-testid="library-favorite" onClick={() => void favorite(file)} disabled={busyId !== null} aria-label={file.favorite ? t("unfavorite") : t("favorite")} aria-pressed={file.favorite} title={file.favorite ? t("unfavorite") : t("favorite")}
                className={cn("flex min-h-9 min-w-8 items-center justify-center rounded-lg hover:bg-[var(--color-bg-secondary)] disabled:opacity-40", file.favorite ? "text-amber-600 dark:text-amber-400" : "text-[var(--color-text-secondary)]")}><Star size={15} strokeWidth={1.6} fill={file.favorite ? "currentColor" : "none"} /></button>
              <a href={libraryContentUrl(file.id)} download aria-label={t("download")} title={t("download")} className="flex min-h-9 min-w-8 items-center justify-center rounded-lg text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-secondary)]"><ArrowDownToLine size={15} strokeWidth={1.6} /></a>
              <button data-testid="library-rename" onClick={() => openAction("rename", file)} disabled={busyId !== null} aria-label={t("rename")} title={t("rename")} className="flex min-h-9 min-w-8 items-center justify-center rounded-lg text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-secondary)] disabled:opacity-40"><Pencil size={14} strokeWidth={1.6} /></button>
              <button data-testid="library-delete" onClick={() => openAction("delete", file)} disabled={busyId !== null} aria-label={t("delete")} title={t("delete")} className="flex min-h-9 min-w-8 items-center justify-center rounded-lg text-[var(--color-text-secondary)] hover:bg-red-50 hover:text-red-600 disabled:opacity-40 dark:hover:bg-red-950/30 dark:hover:text-red-400"><Trash2 size={14} strokeWidth={1.6} /></button>
            </div>
          </div>)}</div>
        </div>}
        {listing && !listing.items.length && !library.loading && <div className="flex min-h-80 flex-col items-center justify-center rounded-2xl border border-dashed border-[var(--color-border)] px-5 py-12 text-center">
          <div className="mb-5 flex h-16 w-16 items-center justify-center rounded-2xl border border-[var(--color-border)] bg-[var(--color-bg-secondary)] text-zinc-400">{filters.view === "favorites" && !filters.query && !filters.extension ? <Star size={28} strokeWidth={1.2} /> : <Files size={28} strokeWidth={1.2} />}</div>
          <h3 className="text-base font-medium">{t(hasFilters ? filters.view === "favorites" && !filters.query && !filters.extension ? "noFavorites" : "noResults" : "emptyTitle")}</h3>
          <p className="mt-2 max-w-sm text-sm leading-relaxed text-[var(--color-text-secondary)]">{t(hasFilters ? filters.view === "favorites" && !filters.query && !filters.extension ? "favoritesHint" : "resultsHint" : "emptyHint")}</p>
          {hasFilters ? <button onClick={library.clearFilters} className={cn(buttonStyle, "mt-5")}>{t("clear")}</button> : <button onClick={() => input.current?.click()} disabled={!!upload} className={cn(primaryStyle, "mt-5")}><Upload size={15} />{t("upload")}</button>}
        </div>}
        {listing && listing.total > 0 && <div className="mt-4 flex flex-wrap items-center justify-between gap-3 text-xs text-[var(--color-text-secondary)]"><p>{t("shown")} {listing.offset + 1}–{Math.min(listing.offset + listing.items.length, listing.total)} / {listing.total}</p>
          <div className="flex items-center gap-1"><button aria-label={t("previous")} disabled={library.loading || !filters.offset} onClick={() => library.updateFilters({ offset: Math.max(0, filters.offset - listing.limit) })} className="flex min-h-9 min-w-9 items-center justify-center rounded-lg border border-[var(--color-border)] hover:bg-[var(--color-bg-secondary)] disabled:opacity-30"><ChevronLeft size={16} /></button><span className="min-w-12 text-center tabular-nums">{Math.floor(listing.offset / listing.limit) + 1} / {Math.ceil(listing.total / listing.limit)}</span><button aria-label={t("next")} disabled={library.loading || filters.offset + listing.limit >= listing.total} onClick={() => library.updateFilters({ offset: filters.offset + listing.limit })} className="flex min-h-9 min-w-9 items-center justify-center rounded-lg border border-[var(--color-border)] hover:bg-[var(--color-bg-secondary)] disabled:opacity-30"><ChevronRight size={16} /></button></div>
        </div>}
        </>}
        <p className="mt-6 flex items-start gap-2 text-xs leading-6 text-[var(--color-text-secondary)]"><File size={13} className="mt-1 shrink-0" />{rag("savedHint")}</p>
      </section>
    </div>

    {library.selectedId && <FilePreview key={library.selectedId} id={library.selectedId} chunkId={library.selectedChunkId} revision={library.revision} busy={busyId !== null || !!upload} replacementProgress={upload && busyId === library.selectedId ? upload.percent : null} onClose={() => library.select(null)} onClearChunk={() => library.select(library.selectedId)} onReindexed={library.refresh} onRename={(file) => openAction("rename", file)} onDelete={(file) => openAction("delete", file)} onFavorite={(file) => void favorite(file)} onReplace={(file, next) => void replace(file, next)} />}

    {action && <LibraryDialog titleId="library-action-title" onClose={() => { if (!busyId) setAction(null); }} busy={!!busyId}>
      <form onSubmit={(event) => { event.preventDefault(); void confirmAction(); }} className="p-6">
        <div className={cn("mb-4 flex h-11 w-11 items-center justify-center rounded-xl", action.kind === "delete" ? "bg-red-50 text-red-600 dark:bg-red-950/30 dark:text-red-400" : "bg-[var(--color-bg-secondary)]")}>
          {action.kind === "delete" ? <Trash2 size={20} strokeWidth={1.5} /> : <Pencil size={20} strokeWidth={1.5} />}
        </div>
        <h2 id="library-action-title" className="text-lg font-semibold">{t(action.kind === "rename" ? "renameTitle" : "deleteTitle")}</h2>
        <p className="mt-2 text-sm leading-relaxed text-[var(--color-text-secondary)]">{t(action.kind === "rename" ? "renameHint" : "deleteHint")}</p>
        {action.kind === "rename" ? <label className="mt-5 block text-xs font-medium">{t("filename")}<input data-testid="library-rename-input" autoFocus value={newName} onChange={(event) => setNewName(event.target.value)} disabled={!!busyId} maxLength={255} required
          className="mt-2 h-11 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] px-3 text-sm font-normal outline-none focus:ring-2 focus:ring-zinc-400/30" /></label> : <p className="mt-4 break-all rounded-lg bg-[var(--color-bg-secondary)] p-3 text-sm font-medium">{action.file.name}</p>}
        {actionError && <p role="alert" className="mt-4 break-words text-sm text-red-600 dark:text-red-400">{actionError}</p>}
        <div className="mt-6 flex justify-end gap-2"><button type="button" onClick={() => setAction(null)} disabled={!!busyId} className={buttonStyle}>{t("cancel")}</button>
          <button type="submit" data-testid={action.kind === "rename" ? "library-rename-save" : "library-delete-confirm"} disabled={!!busyId}
            className={cn(primaryStyle, action.kind === "delete" && "bg-red-600 hover:bg-red-700 dark:bg-red-600 dark:text-white dark:hover:bg-red-500")}>
            {busyId && <Loader2 size={15} className="animate-spin" />}{t(busyId ? action.kind === "rename" ? "saving" : "deleting" : action.kind === "rename" ? "save" : "deleteConfirm")}
          </button>
        </div>
      </form>
    </LibraryDialog>}
  </div>;
}
