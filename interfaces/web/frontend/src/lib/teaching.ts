/** Original course remains independently runnable in ../../web. */
export const TEACHING_URL = (
  process.env.NEXT_PUBLIC_TEACHING_URL || "https://learn.shareai.run"
).replace(/\/$/, "");

export function teachingHref(locale: string, path = "") {
  return `${TEACHING_URL}/${locale}${path}`;
}
