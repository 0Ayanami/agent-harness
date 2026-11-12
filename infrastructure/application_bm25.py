"""Application-side BM25 over the library's persisted, pre-tokenized chunks.

SQLite stores the source of truth and the tokenizer output; it does not calculate
the ranking here. The in-process sparse index uses BM25 with positive Robertson
IDF, k1=1.2 and b=0.75. Corpus statistics cover all current chunks, while document
and favorite scope is applied before selecting candidates.

A durable revision counter, updated by SQL triggers in the writer transaction,
invalidates the cache on chunk or file changes. Results are batch-read again
from the current-version repository before returning to the caller.
"""

import math
import threading
from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class _Chunk:
    identifier: str
    document_id: str
    version_id: str
    ordinal: int
    normalized_text: str
    normalized_name: str
    favorite: bool


@dataclass(frozen=True)
class _Snapshot:
    revision: int
    chunks: tuple
    lengths: tuple
    average_length: float
    postings: dict


class ApplicationBM25Index:
    """One bounded-by-corpus sparse index per application, without model calls."""

    K1 = 1.2
    B = 0.75

    def __init__(self, index):
        self.index = index
        self.repository = index.repository
        self.tokenizer = index.tokenizer
        self.lock = threading.RLock()
        self._snapshot = None
        with self.repository.connection() as database:
            database.executescript("""
                CREATE TABLE IF NOT EXISTS library_bm25_revision (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    revision INTEGER NOT NULL);
                INSERT OR IGNORE INTO library_bm25_revision VALUES (1,0);
            """)
            for table in ("library_chunks", "library_files"):
                for operation in ("INSERT", "UPDATE", "DELETE"):
                    # These identifiers are static constants, never request data.
                    database.execute(
                        f"CREATE TRIGGER IF NOT EXISTS bm25_{table}_{operation.lower()} "
                        f"AFTER {operation} ON {table} BEGIN "
                        "UPDATE library_bm25_revision SET revision=revision+1 WHERE singleton=1; "
                        "END"
                    )

    def _current_snapshot(self):
        # Read the revision and rows in one snapshot. Token counting happens
        # after releasing the repository lock so ingestion can keep progressing.
        with self.repository.connection() as database:
            database.execute("BEGIN")
            revision = database.execute(
                "SELECT revision FROM library_bm25_revision WHERE singleton=1"
            ).fetchone()[0]
            if self._snapshot is not None and self._snapshot.revision == revision:
                return self._snapshot
            rows = database.execute(
                "SELECT c.id,c.document_id,c.version_id,c.ordinal,c.search_text,"
                "f.name,f.favorite,x.tokens FROM library_chunks c "
                "JOIN library_files f ON f.id=c.document_id "
                "JOIN library_chunk_fts x ON x.rowid=c.rowid "
                "WHERE f.deleted_at IS NULL AND c.version_id=f.version_id "
                "ORDER BY c.document_id,c.ordinal,c.id"
            ).fetchall()
        chunks, lengths, postings = [], [], {}
        for ordinal, row in enumerate(rows):
            tokens = row["tokens"].split()
            lengths.append(len(tokens))
            for word, frequency in Counter(tokens).items():
                postings.setdefault(word, {})[ordinal] = frequency
            chunks.append(_Chunk(
                row["id"], row["document_id"], row["version_id"], row["ordinal"],
                row["search_text"], self.tokenizer.normalize(row["name"]), bool(row["favorite"]),
            ))
        self._snapshot = _Snapshot(
            revision, tuple(chunks), tuple(lengths),
            sum(lengths) / len(lengths) if lengths else 0.0, postings,
        )
        return self._snapshot

    def search(self, query, *, document_ids=None, favorites_only=False, limit=10):
        query = query.strip()
        if not query or document_ids == []:
            return []
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("BM25 result limit must be a positive integer")
        words = self.tokenizer.tokens(query, query=True)
        selected = None if document_ids is None else set(document_ids)

        def allowed(chunk):
            return (selected is None or chunk.document_id in selected) and (
                not favorites_only or chunk.favorite
            )

        with self.lock:
            snapshot = self._current_snapshot()
            count = len(snapshot.chunks)
            if not count:
                return []
            scores = {}
            for word in words:
                posting = snapshot.postings.get(word, {})
                if not posting:
                    continue
                # log1p keeps common terms useful, including df == N.
                idf = math.log1p((count - len(posting) + 0.5) / (len(posting) + 0.5))
                for ordinal, frequency in posting.items():
                    if not allowed(snapshot.chunks[ordinal]):
                        continue
                    denominator = frequency + self.K1 * (
                        1 - self.B + self.B * snapshot.lengths[ordinal] / snapshot.average_length
                    )
                    scores[ordinal] = scores.get(ordinal, 0.0) + (
                        idf * frequency * (self.K1 + 1) / denominator
                    )
            ranking = sorted(scores, key=lambda ordinal: (-scores[ordinal], ordinal))[:limit]
            if len(ranking) < limit and len(query) <= 64:
                # Match the previous short-query behavior for a Chinese single
                # character or a literal filename that is not a complete token.
                literal = self.tokenizer.normalize(query)
                seen = set(ranking)
                for ordinal, chunk in enumerate(snapshot.chunks):
                    if ordinal in seen or not allowed(chunk):
                        continue
                    if literal in chunk.normalized_text or literal in chunk.normalized_name:
                        ranking.append(ordinal)
                        if len(ranking) == limit:
                            break
            current = self.index.read_chunks(
                [snapshot.chunks[ordinal].identifier for ordinal in ranking],
                document_ids=document_ids, favorites_only=favorites_only,
            )
            results = []
            for ordinal in ranking:
                cached = snapshot.chunks[ordinal]
                hit = current.get(cached.identifier)
                if (
                    hit is not None and hit["document_id"] == cached.document_id
                    and hit["version_id"] == cached.version_id
                ):
                    results.append({**hit, "score": float(scores.get(ordinal, 0.0))})
            return results
