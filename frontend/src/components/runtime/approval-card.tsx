"use client";

import { useState } from "react";
import { ShieldCheck } from "lucide-react";
import { Card } from "@/components/ui/card";
import { useTranslations } from "@/lib/i18n";
import type { Approval } from "@/types/runtime";

export function ApprovalCard({ approval, onAnswer, onError }: {
  approval: Approval;
  onAnswer: (id: string, allow: boolean) => Promise<void>;
  onError: (error: unknown) => void;
}) {
  const t = useTranslations("runtime");
  const [pending, setPending] = useState(false);
  async function answer(allow: boolean) {
    setPending(true);
    try { await onAnswer(approval.id, allow); }
    catch (issue) { onError(issue); setPending(false); }
  }
  return (
    <Card id={`approval-${approval.id}`} data-testid="approval" className="approval border-amber-300 bg-amber-50 p-4 dark:border-amber-800 dark:bg-amber-950/30">
      <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold"><ShieldCheck size={16} /> {t("approvalTitle")} · {approval.name}</h3>
      <pre className="max-h-60 overflow-auto whitespace-pre-wrap break-words rounded-md bg-zinc-900 p-3 text-xs text-zinc-100">
        {typeof approval.input.command === "string" ? approval.input.command : JSON.stringify(approval.input, null, 2)}
      </pre>
      <div className="mt-3 flex gap-2">
        <button data-testid="approval-allow" disabled={pending} onClick={() => answer(true)} className="min-h-10 rounded-md bg-zinc-900 px-4 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-zinc-900">{t("allow")}</button>
        <button data-testid="approval-deny" disabled={pending} onClick={() => answer(false)} className="min-h-10 rounded-md border border-[var(--color-border)] px-4 text-sm disabled:opacity-50">{t("deny")}</button>
      </div>
    </Card>
  );
}
