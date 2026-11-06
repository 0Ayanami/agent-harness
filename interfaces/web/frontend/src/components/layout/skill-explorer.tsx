"use client";

import { useEffect, useState } from "react";
import { FileText, X } from "lucide-react";
import { LibraryDialog } from "@/components/library/library-dialog";
import { agentApi } from "@/lib/agent-api";
import { useTranslations } from "@/lib/i18n";

interface SkillSummary {
  id: string;
  name: string;
  category: string | null;
  description: string;
}

interface SkillDocument extends SkillSummary {
  markdown: string;
}

function SkillViewer({ skill, onClose }: { skill: SkillSummary; onClose: () => void }) {
  const t = useTranslations("skills");
  const [skillDocument, setSkillDocument] = useState<SkillDocument | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setFailed(false);
    agentApi<SkillDocument>(`/skills/${encodeURIComponent(skill.id)}`, { signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) setSkillDocument(value); })
      .catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, [skill.id, attempt]);

  return <LibraryDialog drawer titleId="skill-viewer-title" testId="skill-viewer" onClose={onClose}>
    <div className="flex h-full flex-col">
      <header className="flex shrink-0 items-start justify-between gap-4 border-b border-[var(--color-border)] p-4 sm:p-6">
        <div className="min-w-0">
          <p className="mb-1 text-xs text-[var(--color-text-secondary)]">{t("readOnly")}</p>
          <h2 id="skill-viewer-title" className="break-words text-lg font-semibold">{skillDocument?.name ?? skill.name}</h2>
        </div>
        <button type="button" autoFocus aria-label={t("close")} title={t("close")} onClick={onClose}
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg hover:bg-[var(--color-bg-secondary)]"><X size={18} /></button>
      </header>
      <div className="min-h-0 flex-1 overflow-auto p-4 sm:p-6">
        {failed ? <div role="alert" className="text-sm text-[var(--color-text-secondary)]">
          <p>{t("readFailed")}</p><button type="button" onClick={() => setAttempt((value) => value + 1)} className="mt-2 underline">{t("retry")}</button>
        </div> : skillDocument ?
          <pre tabIndex={0} data-testid="skill-markdown" className="whitespace-pre-wrap [overflow-wrap:anywhere] rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-secondary)] p-4 text-xs leading-6 sm:text-sm">{skillDocument.markdown}</pre> :
          <p role="status" className="text-sm text-[var(--color-text-secondary)]">{t("loading")}</p>}
      </div>
    </div>
  </LibraryDialog>;
}

export function SkillExplorer() {
  const t = useTranslations("skills");
  const [expanded, setExpanded] = useState(false);
  const [skills, setSkills] = useState<SkillSummary[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [selected, setSelected] = useState<SkillSummary | null>(null);

  useEffect(() => {
    if (!expanded) return;
    const controller = new AbortController();
    setSkills(null);
    setFailed(false);
    agentApi<{ skills: SkillSummary[] }>("/skills", { signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) setSkills(value.skills); })
      .catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, [expanded, attempt]);

  const groups = new Map<string | null, SkillSummary[]>();
  for (const skill of skills ?? []) {
    const entries = groups.get(skill.category) ?? [];
    entries.push(skill);
    groups.set(skill.category, entries);
  }

  return <>
    <details data-testid="skill-menu" onToggle={(event) => setExpanded(event.currentTarget.open)} className="border-t border-[var(--color-border)] pt-4">
      <summary className="cursor-pointer rounded-md py-1 text-xs font-semibold text-[var(--color-text-secondary)]">{t("title")}</summary>
      <div className="mt-2 max-h-64 space-y-2 overflow-auto">
        {failed ? <div role="alert" className="px-2.5 py-1.5 text-xs text-[var(--color-text-secondary)]">
          <p>{t("loadFailed")}</p><button type="button" onClick={() => setAttempt((value) => value + 1)} className="mt-2 underline">{t("retry")}</button>
        </div> : !skills ? <p role="status" className="px-2.5 py-1.5 text-xs text-[var(--color-text-secondary)]">{t("loading")}</p> :
          !skills.length && <p className="px-2.5 py-1.5 text-xs text-[var(--color-text-secondary)]">{t("empty")}</p>}
        {[...groups].map(([category, entries]) => <section key={category ?? ""} data-testid="skill-category">
          <h3 className="break-words px-2.5 py-1.5 text-[11px] font-semibold text-[var(--color-text-secondary)]">{category ?? t("projectSkills")}</h3>
          <ul className="space-y-0.5">{entries.map((skill) => <li key={skill.id}>
            <button type="button" data-testid="skill-entry" title={skill.description || skill.name} onClick={() => setSelected(skill)}
              className="flex min-h-10 w-full min-w-0 items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-sm hover:bg-[var(--color-bg-secondary)]">
              <FileText size={15} className="shrink-0 text-[var(--color-text-secondary)]" /><span className="truncate">{skill.name}</span>
            </button>
          </li>)}</ul>
        </section>)}
      </div>
    </details>
    {selected && <SkillViewer key={selected.id} skill={selected} onClose={() => setSelected(null)} />}
  </>;
}
