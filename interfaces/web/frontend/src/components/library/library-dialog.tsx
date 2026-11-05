"use client";

import { useEffect, useRef, type ReactNode } from "react";
import { cn } from "@/lib/utils";

/** Native modal dialogs provide focus trapping, escape handling, and focus restoration. */
export function LibraryDialog({ children, titleId, onClose, drawer = false, busy = false, testId }: {
  children: ReactNode;
  titleId: string;
  onClose: () => void;
  drawer?: boolean;
  busy?: boolean;
  testId?: string;
}) {
  const element = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = element.current;
    if (dialog && !dialog.open) dialog.showModal();
    return () => { if (dialog?.open) dialog.close(); };
  }, []);

  return <dialog ref={element} aria-labelledby={titleId} data-testid={testId}
    onCancel={(event) => { event.preventDefault(); if (!busy) onClose(); }}
    onClick={(event) => {
      if (busy || event.target !== event.currentTarget) return;
      const bounds = event.currentTarget.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) onClose();
    }}
    className={cn("border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)] shadow-2xl backdrop:bg-black/40 backdrop:backdrop-blur-[2px]",
      drawer ? "fixed inset-y-0 left-auto right-0 m-0 h-dvh max-h-none w-full max-w-3xl p-0" : "m-auto w-[calc(100%_-_2rem)] max-w-md rounded-2xl p-0")}>
    {children}
  </dialog>;
}
