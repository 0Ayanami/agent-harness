import type { Metadata } from "next";
import type { ReactNode } from "react";
import { SITE_NAME } from "@/lib/site";

export const metadata: Metadata = { title: SITE_NAME };

export default function EntryLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh">
      <body>{children}</body>
    </html>
  );
}
