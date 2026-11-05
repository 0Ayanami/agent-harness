"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations, useLocale } from "@/lib/i18n";
import { Github, Menu, X, Sun, Moon } from "lucide-react";
import { useState, useEffect } from "react";
import { cn } from "@/lib/utils";
import { REPOSITORY_URL, SITE_NAME } from "@/lib/site";

const LOCALES = [
  { code: "en", label: "EN" },
  { code: "zh", label: "中文" },
  { code: "ja", label: "日本語" },
];

export function Header() {
  const t = useTranslations("nav");
  const runtime = useTranslations("runtime");
  const library = useTranslations("library");
  const pathname = usePathname();
  const locale = useLocale();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [dark, setDark] = useState(false);
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
    setDark(document.documentElement.classList.contains("dark"));
  }, []);

  function toggleDark() {
    const next = !dark;
    setDark(next);
    document.documentElement.classList.toggle("dark", next);
    try { localStorage.setItem("theme", next ? "dark" : "light"); } catch {}
  }

  function switchLocale(newLocale: string) {
    const newPath = pathname.replace(`/${locale}`, `/${newLocale}`);
    window.location.href = newPath + window.location.search + window.location.hash;
  }

  return (
    <header className="sticky top-0 z-50 border-b border-[var(--color-border)] bg-[var(--color-bg)]/80 backdrop-blur-sm">
      <div className="mx-auto flex h-14 max-w-7xl items-center justify-between px-4 sm:px-6 lg:px-8">
        <Link href={`/${locale}/`} className="text-lg font-bold">
          {SITE_NAME}
        </Link>

        {/* Desktop nav */}
        <nav className="hidden items-center gap-5 lg:flex">
          <Link href={`/${locale}`} aria-current={pathname.replace(/\/$/, "") === `/${locale}` ? "page" : undefined}
            className={cn("text-sm font-medium", pathname.replace(/\/$/, "") === `/${locale}` ? "text-zinc-900 dark:text-white" : "text-zinc-500 dark:text-zinc-400")}>
            {runtime("workbench")}
          </Link>
          <Link href={`/${locale}/library/`} data-testid="library-nav" aria-current={pathname.includes("/library") ? "page" : undefined}
            className={cn("text-sm font-medium", pathname.includes("/library") ? "text-zinc-900 dark:text-white" : "text-zinc-500 dark:text-zinc-400")}>
            {library("title")}
          </Link>
          {/* Locale switcher */}
          <div className="flex items-center gap-1 rounded-lg border border-[var(--color-border)] p-0.5">
            {LOCALES.map((l) => (
              <button
                key={l.code}
                onClick={() => switchLocale(l.code)}
                className={cn(
                  "rounded-md px-2 py-1 text-xs font-medium transition-colors",
                  locale === l.code
                    ? "bg-zinc-900 text-white dark:bg-white dark:text-zinc-900"
                    : "text-zinc-500 hover:text-zinc-700 dark:text-zinc-400"
                )}
              >
                {l.label}
              </button>
            ))}
          </div>

          <button
            onClick={toggleDark}
            aria-label={runtime("toggleTheme")}
            className="rounded-md p-1.5 text-zinc-500 hover:text-zinc-700 dark:text-zinc-400 dark:hover:text-white"
          >
            {mounted ? (dark ? <Sun size={16} /> : <Moon size={16} />) : <span className="w-4 h-4 inline-block" />}
          </button>

          <a
            href={REPOSITORY_URL}
            aria-label={t("github")}
            target="_blank"
            rel="noopener"
            className="text-zinc-500 hover:text-zinc-700 dark:text-zinc-400 dark:hover:text-white"
          >
            <Github size={18} />
          </a>
        </nav>

        {/* Mobile hamburger */}
        <button
          onClick={() => setMobileOpen(!mobileOpen)}
          aria-label={runtime("toggleMenu")}
          aria-expanded={mobileOpen}
          className="flex min-h-[44px] min-w-[44px] items-center justify-center lg:hidden"
        >
          {mobileOpen ? <X size={20} /> : <Menu size={20} />}
        </button>
      </div>

      {/* Mobile menu */}
      {mobileOpen && (
        <div className="border-t border-[var(--color-border)] bg-[var(--color-bg)] p-4 lg:hidden">
          <Link href={`/${locale}/`} onClick={() => setMobileOpen(false)}
            aria-current={pathname.replace(/\/$/, "") === `/${locale}` ? "page" : undefined}
            className="flex min-h-[44px] items-center text-sm">{runtime("workbench")}</Link>
          <Link href={`/${locale}/library/`} data-testid="library-nav-mobile" onClick={() => setMobileOpen(false)}
            aria-current={pathname.includes("/library") ? "page" : undefined}
            className="flex min-h-[44px] items-center text-sm">{library("title")}</Link>
          <div className="mt-3 flex items-center justify-between border-t border-[var(--color-border)] pt-3">
            <div className="flex gap-2">
              {LOCALES.map((l) => (
                <button
                  key={l.code}
                  onClick={() => switchLocale(l.code)}
                  className={cn(
                    "min-h-[44px] min-w-[44px] rounded-md px-3 text-xs font-medium",
                    locale === l.code
                      ? "bg-zinc-900 text-white dark:bg-white dark:text-zinc-900"
                      : "border border-[var(--color-border)]"
                  )}
                >
                  {l.label}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={toggleDark}
                aria-label={runtime("toggleTheme")}
                className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded-md text-zinc-500 hover:text-zinc-700 dark:text-zinc-400 dark:hover:text-white"
              >
                {mounted ? (dark ? <Sun size={18} /> : <Moon size={18} />) : <span className="w-[18px] h-[18px] inline-block" />}
              </button>
              <a
                href={REPOSITORY_URL}
                aria-label={t("github")}
                target="_blank"
                rel="noopener"
                className="flex min-h-[44px] min-w-[44px] items-center justify-center text-zinc-500 hover:text-zinc-700 dark:text-zinc-400 dark:hover:text-white"
              >
                <Github size={18} />
              </a>
            </div>
          </div>
        </div>
      )}
    </header>
  );
}
