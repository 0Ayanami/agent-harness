import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { I18nProvider } from "@/lib/i18n";
import { Header } from "@/components/layout/header";
import "../globals.css";

export const metadata: Metadata = {
  title: "Agent Workbench · Learn Claude Code",
  description: "Run the modular Agent Harness with the original Learn Claude Code interface.",
};

export function generateStaticParams() {
  return ["en", "zh", "ja"].map((locale) => ({ locale }));
}

// Adapted from web/src/app/[locale]/layout.tsx, preserving the theme and shell.
export default async function LocaleLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;
  if (!["en", "zh", "ja"].includes(locale)) notFound();
  return (
    <html lang={locale} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: `
          (function() {
            try {
              var theme = localStorage.getItem('theme');
              if (theme === 'dark' || (!theme && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
                document.documentElement.classList.add('dark');
              }
            } catch (_) {}
          })();
        ` }} />
      </head>
      <body className="min-h-screen bg-[var(--color-bg)] text-[var(--color-text)] antialiased">
        <I18nProvider locale={locale}>
          <Header />
          <main className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
            {children}
          </main>
        </I18nProvider>
      </body>
    </html>
  );
}
