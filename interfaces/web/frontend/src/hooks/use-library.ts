"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { hasPendingIndex, listLibraryFiles, searchLibraryContent } from "@/lib/library-api";
import type { LibraryFilters, LibraryListing, LibrarySearchResults } from "@/types/library";

const initialFilters: LibraryFilters = { query: "", mode: "filename", view: "all", extension: "", sort: "updated_desc", offset: 0 };
type Selection = { id: string | null; chunkId: string | null };

function readLocation() {
  const params = new URLSearchParams(window.location.search);
  const view = params.get("view");
  const mode = params.get("mode");
  const sort = params.get("sort");
  const extension = params.get("type") || "";
  const offset = Number(params.get("offset") || "0");
  const filters: LibraryFilters = {
    query: (params.get("q") || "").slice(0, 200),
    mode: mode === "content" || mode === "vector" || mode === "hybrid" ? mode : "filename",
    view: view === "favorites" || view === "recent" ? view : "all",
    extension: ["md", "txt", "pdf"].includes(extension) ? extension : "",
    sort: sort === "name_asc" || sort === "size_desc" ? sort : "updated_desc",
    offset: Number.isSafeInteger(offset) && offset >= 0 && offset <= 1_000_000 ? offset : 0,
  };
  const id = params.get("file");
  return { filters, selection: { id, chunkId: id ? params.get("chunk") : null } };
}

function writeLocation(filters: LibraryFilters, selection: Selection, push = false) {
  const url = new URL(window.location.href);
  for (const key of ["q", "mode", "view", "type", "sort", "offset", "file", "chunk"]) url.searchParams.delete(key);
  if (filters.query) url.searchParams.set("q", filters.query);
  if (filters.mode !== "filename") url.searchParams.set("mode", filters.mode);
  if (filters.view !== "all") url.searchParams.set("view", filters.view);
  if (filters.extension) url.searchParams.set("type", filters.extension);
  if (filters.sort !== "updated_desc") url.searchParams.set("sort", filters.sort);
  if (filters.offset) url.searchParams.set("offset", String(filters.offset));
  if (selection.id) url.searchParams.set("file", selection.id);
  if (selection.id && selection.chunkId) url.searchParams.set("chunk", selection.chunkId);
  window.history[push ? "pushState" : "replaceState"]({}, "", url);
}

export function useLibrary() {
  const [filters, setFilters] = useState(initialFilters);
  const [selection, setSelection] = useState<Selection>({ id: null, chunkId: null });
  const selectedRef = useRef<Selection>({ id: null, chunkId: null });
  const [initialized, setInitialized] = useState(false);
  const [listing, setListing] = useState<LibraryListing | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [revision, setRevision] = useState(0);
  const [results, setResults] = useState<LibrarySearchResults | null>(null);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState<unknown>(null);

  useEffect(() => {
    const restore = () => {
      const state = readLocation();
      setFilters(state.filters);
      selectedRef.current = state.selection;
      setSelection(state.selection);
    };
    restore();
    setInitialized(true);
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, []);

  useEffect(() => {
    if (!initialized) return;
    const controller = new AbortController();
    let timer: number | undefined;
    setLoading(true);
    setError(null);
    const load = async (background: boolean) => {
      try {
        const result = await listLibraryFiles(filters, controller.signal);
        if (controller.signal.aborted) return;
        if (filters.offset && filters.offset >= result.total) {
          const next = { ...filters, offset: Math.max(0, Math.floor((result.total - 1) / result.limit) * result.limit) };
          setFilters(next);
          writeLocation(next, selectedRef.current);
          return;
        }
        setListing(result);
        setError(null);
        if (result.items.some(hasPendingIndex)) {
          timer = window.setTimeout(() => void load(true), 1200);
        }
      } catch (reason: unknown) {
        if (!controller.signal.aborted) {
          if (!background) setListing(null);
          setError(reason);
        }
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    };
    timer = window.setTimeout(() => void load(false), filters.mode === "filename" && filters.query ? 250 : 0);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [filters, initialized, revision]);

  const indexSignature = listing?.items.map((file) => `${file.id}:${file.version_id}:${file.ingestion?.status}:${file.vector?.status}`).join("|") || "";
  useEffect(() => {
    if (!initialized || filters.mode === "filename" || !filters.query.trim()) {
      setResults(null);
      setSearchError(null);
      setSearchLoading(false);
      return;
    }
    const controller = new AbortController();
    setSearchLoading(true);
    setSearchError(null);
    setResults(null);
    const timer = window.setTimeout(() => {
      searchLibraryContent(filters.query.trim(), filters.view === "favorites", controller.signal, filters.mode === "vector" || filters.mode === "hybrid" ? filters.mode : "keyword")
        .then((response) => { if (!controller.signal.aborted) setResults(response); })
        .catch((reason: unknown) => { if (!controller.signal.aborted) setSearchError(reason); })
        .finally(() => { if (!controller.signal.aborted) setSearchLoading(false); });
    }, 250);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [initialized, filters.mode, filters.query, filters.view, revision, indexSignature]);

  const updateFilters = (patch: Partial<LibraryFilters>) => {
    const next = { ...filters, ...patch, offset: patch.offset ?? 0 };
    setFilters(next);
    writeLocation(next, selectedRef.current);
  };
  const select = (id: string | null, chunkId: string | null = null) => {
    const next = { id, chunkId: id ? chunkId : null };
    selectedRef.current = next;
    setSelection(next);
    writeLocation(filters, next, true);
  };
  const refresh = useCallback(() => setRevision((value) => value + 1), []);

  return {
    filters, updateFilters, clearFilters: () => updateFilters(initialFilters), listing, loading, error,
    selectedId: selection.id, selectedChunkId: selection.chunkId, select, revision, refresh,
    results, searchLoading, searchError,
  };
}
