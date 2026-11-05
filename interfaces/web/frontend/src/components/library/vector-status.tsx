"use client";

import { useState } from "react";
import { AlertCircle, Check, Clock3, Loader2, RefreshCw } from "lucide-react";
import { useTranslations } from "@/lib/i18n";
import { libraryErrorMessage, vectorizeLibraryFile } from "@/lib/library-api";
import type { LibraryFile, LibraryVector } from "@/types/library";
import { cn } from "@/lib/utils";

export function VectorStatus({ vector, compact = false }: { vector?: LibraryVector | null; compact?: boolean }) {
  const t = useTranslations("rag");
  const status = vector?.status;
  const label = status ? { not_configured: "vectorMissing", queued: "vectorQueued", processing: "vectorProcessing", ready: "vectorReady", failed: "vectorFailed" }[status] : "vectorUnknown";
  const Icon = status === "processing" ? Loader2 : status === "ready" ? Check : status === "failed" ? AlertCircle : Clock3;
  return <span data-testid="library-vector-status" data-status={status || "unavailable"} title={vector?.error || t(label)}
    className={cn("inline-flex max-w-full items-center gap-1.5", compact ? "text-[10px]" : "text-xs", status === "failed" ? "text-amber-700 dark:text-amber-400" : "text-[var(--color-text-secondary)]")}>
    <Icon size={compact ? 11 : 13} className={cn("shrink-0", status === "processing" && "animate-spin")} /><span className="truncate">{t(label)}</span>
  </span>;
}

export function VectorActions({ file, disabled, onQueued }: { file: LibraryFile; disabled: boolean; onQueued: () => void }) {
  const t = useTranslations("rag");
  const lib = useTranslations("library");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = file.vector?.status === "queued" || file.vector?.status === "processing";
  const configured = file.vector?.status && file.vector.status !== "not_configured";
  const readyText = file.ingestion?.status === "ready";
  async function vectorize() {
    if (sending || !configured || !readyText || pending) return;
    setSending(true); setError(null);
    try { await vectorizeLibraryFile(file.id); onQueued(); }
    catch (reason) { setError(libraryErrorMessage(reason, lib)); }
    finally { setSending(false); }
  }
  return <div className="mb-5 rounded-xl border border-[var(--color-border)] p-4">
    <p className="mb-2 text-xs font-medium">{t("semanticIndex")}</p><VectorStatus vector={file.vector} />
    <p className="mt-2 text-xs leading-5 text-[var(--color-text-secondary)]">{!configured ? t("configMissing") : !readyText ? t("textFirst") : t("vectorizeHint")}</p>
    {file.vector?.error && <p className="mt-2 break-words text-xs text-amber-700 dark:text-amber-400">{file.vector.error}</p>}
    {error && <p role="alert" className="mt-2 break-words text-xs text-amber-700 dark:text-amber-400">{error}</p>}
    <button data-testid="library-vectorize" type="button" disabled={disabled || sending || pending || !configured || !readyText} onClick={() => void vectorize()}
      className="mt-3 inline-flex min-h-10 items-center gap-2 rounded-lg border border-[var(--color-border)] px-3 text-xs font-medium hover:bg-[var(--color-bg-secondary)] disabled:opacity-40">
      <RefreshCw size={13} className={cn(sending && "animate-spin")} />{t(file.vector?.status === "failed" ? "vectorizeRetry" : "vectorize")}
    </button>
  </div>;
}
