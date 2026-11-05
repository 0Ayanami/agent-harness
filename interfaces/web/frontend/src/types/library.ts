export interface LibraryFile {
  id: string;
  name: string;
  original_name: string;
  extension: string;
  mime_type: string;
  size_bytes: number;
  sha256: string;
  created_at: string;
  updated_at: string;
  favorite: boolean;
  version_id: string;
  status: "stored";
  ingestion?: LibraryIngestion | null;
  vector?: LibraryVector | null;
}

export interface LibraryVector {
  status: "not_configured" | "queued" | "processing" | "ready" | "failed";
  version_id: string;
  chunk_count: number;
  error: string | null;
  updated_at: string | null;
}

export interface RetrievalStatus {
  configured: boolean;
  model: string;
  dimensions: number;
  [key: string]: unknown;
}

export type LibraryIngestionStatus = "queued" | "processing" | "ready" | "failed" | "needs_ocr";

export interface LibraryIngestion {
  job_id: string;
  status: LibraryIngestionStatus;
  version_id: string;
  chunk_count: number;
  error: string | null;
  updated_at: string;
}

export interface LibraryIngestionJob {
  id: string;
  document_id: string;
  version_id: string;
  status: LibraryIngestionStatus;
}

export interface LibraryLocation {
  page?: number;
  start_line?: number;
  end_line?: number;
  heading?: string;
}

export interface LibrarySearchHit {
  chunk_id: string;
  document_id: string;
  version_id: string;
  name: string;
  text: string;
  location: LibraryLocation;
  score: number;
}

export interface LibrarySearchResults {
  mode: "keyword" | "hybrid";
  hits: LibrarySearchHit[];
  warning: string | null;
}

export type LibrarySort = "updated_desc" | "name_asc" | "size_desc";
export type LibraryView = "all" | "favorites" | "recent";

export interface LibraryFilters {
  query: string;
  mode: "filename" | "content" | "hybrid";
  view: LibraryView;
  extension: string;
  sort: LibrarySort;
  offset: number;
}

export interface LibraryListing {
  items: LibraryFile[];
  total: number;
  limit: number;
  offset: number;
  counts: { all: number; favorites: number };
}

export interface LibraryPreview {
  text: string;
  truncated: boolean;
  media_type: string;
  version_id: string;
}
