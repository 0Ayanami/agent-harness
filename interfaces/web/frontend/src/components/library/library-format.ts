export function formatSize(bytes: number, locale: string): string {
  if (bytes < 1024) return `${bytes} B`;
  const unit = bytes < 1024 * 1024 ? "KB" : "MB";
  const amount = bytes / (unit === "KB" ? 1024 : 1024 * 1024);
  return `${new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(amount)} ${unit}`;
}

export function formatDate(value: string, locale: string): string {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "—" : new Intl.DateTimeFormat(locale, { year: "numeric", month: "short", day: "numeric" }).format(date);
}
