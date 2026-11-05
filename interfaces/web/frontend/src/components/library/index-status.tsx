"use client";

import { AlertCircle, Check, Clock3, Loader2 } from "lucide-react";
import { useTranslations } from "@/lib/i18n";
import type { LibraryIngestion } from "@/types/library";
import { cn } from "@/lib/utils";

export function IndexStatus({ ingestion, compact = false }: { ingestion?: LibraryIngestion | null; compact?: boolean }) {
  const t = useTranslations("library");
  const state = ingestion?.status;
  const pending = state === "queued" || state === "processing";
  const failed = state === "failed" || state === "needs_ocr";
  const label = state ? {
    queued: "indexQueued", processing: "indexProcessing", ready: "indexReady", failed: "indexFailed", needs_ocr: "indexOcr",
  }[state] : "indexUnavailable";
  const Icon = pending ? state === "queued" ? Clock3 : Loader2 : failed ? AlertCircle : state === "ready" ? Check : Clock3;
  return <span data-testid="library-index-status" data-status={state || "unavailable"}
    className={cn("inline-flex max-w-full items-center gap-1.5", compact ? "text-[10px]" : "text-xs", failed ? "text-amber-700 dark:text-amber-400" : "text-[var(--color-text-secondary)]")}
    title={ingestion?.error || t(label)}>
    <Icon size={compact ? 11 : 13} className={cn("shrink-0", state === "processing" && "animate-spin")} />
    <span className="truncate">{t(label)}{state === "ready" && ingestion && ` · ${ingestion.chunk_count} ${t("chunks")}`}</span>
  </span>;
}
