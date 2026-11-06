import { API_PREFIX } from "./agent-api";
import type { LibraryFile, LibraryFilters, LibraryIngestionJob, LibraryListing, LibraryPreview, LibraryRetrievalMode, LibrarySearchHit, LibrarySearchResults, RetrievalStatus } from "@/types/library";

const root = `${API_PREFIX}/library/files`;
export const MAX_LIBRARY_FILE_SIZE = 25 * 1024 * 1024;
export const LIBRARY_FILE_ACCEPT = ".md,.txt,.pdf";

export class LibraryApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "LibraryApiError";
  }
}

function detail(body: unknown, status: number): string {
  return body && typeof body === "object" && "detail" in body && typeof body.detail === "string"
    ? body.detail : `HTTP ${status}`;
}

async function request<T>(url: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("X-Agent-Client", "web");
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(url, { ...options, headers, cache: "no-store" });
  if (!response.ok) throw new LibraryApiError(response.status, detail(await response.json().catch(() => null), response.status));
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function listLibraryFiles(filters: LibraryFilters, signal: AbortSignal) {
  const query = new URLSearchParams({ limit: "50", offset: String(filters.offset), sort: filters.sort });
  if (filters.query && filters.mode === "filename") query.set("q", filters.query);
  if (filters.view === "favorites") query.set("favorite", "true");
  if (filters.extension) query.set("extension", filters.extension);
  return request<LibraryListing>(`${root}?${query}`, { signal });
}

export const getLibraryFile = (id: string, signal?: AbortSignal) => request<LibraryFile>(`${root}/${encodeURIComponent(id)}`, { signal });
export const getLibraryPreview = (id: string, signal?: AbortSignal) => request<LibraryPreview>(`${root}/${encodeURIComponent(id)}/preview`, { signal });
export const patchLibraryFile = (id: string, patch: { name?: string; favorite?: boolean }) => request<LibraryFile>(`${root}/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(patch) });
export const deleteLibraryFile = (id: string) => request<void>(`${root}/${encodeURIComponent(id)}`, { method: "DELETE" });
export const libraryContentUrl = (id: string, download = true, versionId?: string) => {
  const version = versionId === undefined ? "" : `&version_id=${encodeURIComponent(versionId)}`;
  return `${root}/${encodeURIComponent(id)}/content?download=${download}${version}`;
};
export const reindexLibraryFile = (id: string) => request<LibraryIngestionJob>(`${root}/${encodeURIComponent(id)}/reindex`, { method: "POST" });
export const getLibraryChunk = (id: string, chunkId: string, signal?: AbortSignal) => request<LibrarySearchHit>(`${root}/${encodeURIComponent(id)}/chunks/${encodeURIComponent(chunkId)}`, { signal });

export function searchLibraryContent(query: string, favoritesOnly: boolean, signal: AbortSignal, mode: LibraryRetrievalMode = "keyword") {
  return request<LibrarySearchResults>(`${API_PREFIX}/library/search`, {
    method: "POST", body: JSON.stringify({ query, favorites_only: favoritesOnly, limit: 20, mode }), signal,
  });
}

export const getRetrievalStatus = (signal?: AbortSignal) => request<RetrievalStatus>(`${API_PREFIX}/library/retrieval`, { signal });
export const vectorizeLibraryFile = (id: string) => request<LibraryIngestionJob>(`${root}/${encodeURIComponent(id)}/vectorize`, { method: "POST" });

export function hasPendingIndex(file: LibraryFile) {
  const text = file.ingestion?.status;
  const vector = file.vector?.status;
  return text === "queued" || text === "processing" ||
    (text === "ready" && (vector === "queued" || vector === "processing"));
}

/** Every page is fetched so selected files always refer to the real library. */
export async function listQuestionFiles(signal: AbortSignal) {
  const files: LibraryFile[] = [];
  for (let offset = 0; ; offset += 100) {
    const result = await request<LibraryListing>(`${root}?limit=100&offset=${offset}&sort=name_asc`, { signal });
    files.push(...result.items);
    if (files.length >= result.total || !result.items.length) return files;
  }
}

/** Let the browser set multipart boundaries and report bytes actually sent. */
export function uploadLibraryFile(file: File, onProgress: (percent: number) => void, replaceId?: string): Promise<LibraryFile> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(replaceId ? "PUT" : "POST", replaceId ? `${root}/${encodeURIComponent(replaceId)}/content` : root);
    xhr.setRequestHeader("X-Agent-Client", "web");
    xhr.timeout = 120000;
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(Math.min(99, Math.round(event.loaded / event.total * 100)));
    };
    xhr.onerror = () => reject(new LibraryApiError(0, "Network error"));
    xhr.ontimeout = () => reject(new LibraryApiError(0, "Upload timed out"));
    xhr.onload = () => {
      let body: unknown;
      try { body = JSON.parse(xhr.responseText); } catch { body = null; }
      if (xhr.status >= 200 && xhr.status < 300 && body) { onProgress(100); resolve(body as LibraryFile); }
      else reject(new LibraryApiError(xhr.status, detail(body, xhr.status)));
    };
    const data = new FormData();
    data.append("file", file);
    xhr.send(data);
  });
}

export function validateLibraryFile(file: File): string | null {
  if (!/\.(md|txt|pdf)$/i.test(file.name)) return "invalidType";
  if (!file.size) return "emptyFile";
  if (file.size > MAX_LIBRARY_FILE_SIZE) return "tooLarge";
  return null;
}

export function libraryErrorMessage(error: unknown, t: (key: string) => string): string {
  if (error instanceof LibraryApiError) {
    if (error.status === 409) return t("duplicate");
    if (error.status === 404) return t("missing");
    if (error.status === 413) return t("tooLarge");
    if (error.status === 415) return t("invalidType");
    if (error.status === 422) return `${t("operationFailed")} ${error.message}`;
    return `${t("operationFailed")} ${error.message}`;
  }
  return t("operationFailed");
}
