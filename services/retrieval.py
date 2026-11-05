"""Durable semantic indexing and source-checked reciprocal-rank-fusion retrieval."""

import hashlib
import json
import logging
import math
import threading
from uuid import uuid4

from ..infrastructure.embeddings import BailianEmbeddings, EmbeddingError
from ..infrastructure.library import timestamp
from ..infrastructure.library_vectors import QdrantLibraryIndex, VectorIndexError
from .library import LibraryError

logger = logging.getLogger(__name__)


class RetrievalService:
    """One application-owned vector client/worker; all returned text comes from SQLite."""

    def __init__(self, config, library, ingestion, *, embedding_client=None, vector_index=None):
        self.config = config
        self.library = library
        self.ingestion = ingestion
        self.repository = library.repository
        self.configured = bool(config.embedding_api_key and config.embedding_base_url)
        self.provider = None
        self.profile = None
        self.vectors = vector_index
        self._own_vectors = vector_index is None
        self._own_provider = False
        self._error = None
        self._closed = False
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.lifecycle_lock = threading.Lock()
        self.index_lock = threading.RLock()
        self.thread = None
        if self.configured:
            if embedding_client is not None and hasattr(embedding_client, "embed"):
                self.provider = embedding_client
            else:
                self.provider = BailianEmbeddings(config, client=embedding_client)
                self._own_provider = True
            self.profile = (
                getattr(self.provider, "profile", None)
                or hashlib.sha256(
                    json.dumps(
                        {
                            "endpoint": config.embedding_base_url,
                            "model": config.embedding_model,
                            "dimensions": config.embedding_dimensions,
                            "normalization": "l2",
                        },
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
            )
        with self.repository.connection() as database:
            database.executescript("""
                CREATE TABLE IF NOT EXISTS library_embedding_cache (
                    profile TEXT NOT NULL, text_hash TEXT NOT NULL, vector TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY(profile,text_hash));
                CREATE TABLE IF NOT EXISTS library_vector_jobs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                    profile TEXT NOT NULL, document_id TEXT NOT NULL, version_id TEXT NOT NULL,
                    generation TEXT NOT NULL, status TEXT NOT NULL, lease_id TEXT,
                    chunk_count INTEGER NOT NULL DEFAULT 0, error TEXT,
                    cleaned INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS library_vectors_document
                    ON library_vector_jobs(profile,document_id,seq DESC);
                CREATE INDEX IF NOT EXISTS library_vectors_state
                    ON library_vector_jobs(profile,status,seq);
            """)
        library.retrieval = self

    def start(self):
        with self.lifecycle_lock:
            if not self.configured or (self.thread and self.thread.is_alive()):
                return
            if self._closed:
                raise RuntimeError("A closed retrieval service cannot be restarted")
            self.stop.clear()
            self._recover()
            self.thread = threading.Thread(target=self._run, name="library-vectors", daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        thread = self.thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)
        if not thread or not thread.is_alive():
            self._close_resources()

    def _close_resources(self):
        with self.index_lock:
            if self._closed:
                return
            if self.vectors is not None and self._own_vectors:
                self.vectors.close()
            if self.provider is not None and self._own_provider:
                self.provider.close()
            self._closed = True

    def _ensure_vectors(self):
        with self.index_lock:
            if self._closed:
                raise VectorIndexError("Semantic retrieval has stopped")
            if self.vectors is None:
                index = QdrantLibraryIndex(
                    self.config.data_dir / "library" / "vectors" / self.profile,
                    self.config.embedding_dimensions,
                )
                # SQLite may survive loss of a vector directory. Restore from
                # cached embeddings instead of trusting an empty/new collection.
                try:
                    with self.repository.connection() as database:
                        rows = database.execute(
                            "SELECT id,chunk_count FROM library_vector_jobs WHERE profile=? AND status='ready'",
                            (self.profile,),
                        ).fetchall()
                    for row in rows:
                        if index.created or index.count_job(row["id"]) != row["chunk_count"]:
                            with self.repository.connection() as database:
                                database.execute(
                                    "UPDATE library_vector_jobs SET status='cancelled',updated_at=? WHERE id=?",
                                    (timestamp(), row["id"]),
                                )
                except Exception:
                    index.close()
                    raise
                self.vectors = index
            return self.vectors

    def _documents(self, database, document_ids=None, favorites_only=False):
        if document_ids is not None and not document_ids:
            return {}
        clauses = [
            "f.deleted_at IS NULL",
            "j.status='ready'",
            "c.version_id=f.version_id",
            "j.seq=(SELECT MAX(k.seq) FROM library_ingestion_jobs k WHERE k.document_id=f.id)",
        ]
        values = []
        if document_ids is not None:
            clauses.append(f"f.id IN ({','.join('?' for _ in document_ids)})")
            values.extend(document_ids)
        if favorites_only:
            clauses.append("f.favorite=1")
        rows = database.execute(
            "SELECT f.id,f.version_id,COUNT(c.id) AS chunk_count,GROUP_CONCAT(c.id) AS chunk_ids "
            "FROM library_files f JOIN library_ingestion_jobs j ON j.document_id=f.id "
            "AND j.version_id=f.version_id JOIN library_chunks c ON c.document_id=f.id "
            f"WHERE {' AND '.join(clauses)} GROUP BY f.id,f.version_id",
            values,
        ).fetchall()
        return {
            row["id"]: {
                "id": row["id"],
                "version_id": row["version_id"],
                "chunk_count": row["chunk_count"],
                "generation": hashlib.sha256(
                    ",".join(sorted(row["chunk_ids"].split(","))).encode()
                ).hexdigest(),
            }
            for row in rows
        }

    def _latest(self, database, document_ids=None):
        clauses, values = ["j.profile=?"], [self.profile]
        if document_ids is not None:
            if not document_ids:
                return {}
            clauses.append(f"j.document_id IN ({','.join('?' for _ in document_ids)})")
            values.extend(document_ids)
        rows = database.execute(
            "SELECT j.* FROM library_vector_jobs j WHERE "
            + " AND ".join(clauses)
            + " AND j.seq=(SELECT MAX(k.seq) FROM library_vector_jobs k "
            "WHERE k.profile=j.profile AND k.document_id=j.document_id)",
            values,
        ).fetchall()
        return {row["document_id"]: dict(row) for row in rows}

    @staticmethod
    def _matches(job, document):
        return bool(
            job
            and document
            and job["version_id"] == document["version_id"]
            and job["generation"] == document["generation"]
        )

    def decorate(self, records):
        if not records:
            return records
        documents, jobs = {}, {}
        if self.configured:
            identifiers = [record["id"] for record in records]
            with self.repository.connection() as database:
                documents = self._documents(database, identifiers)
                jobs = self._latest(database, identifiers)
        result = []
        for record in records:
            job = jobs.get(record["id"])
            valid = self._matches(job, documents.get(record["id"]))
            state = job["status"] if valid else "queued"
            error = job["error"] if valid else None
            if state == "cancelled":
                state = "queued"
            if self._error and (state in ("queued", "processing") or self.vectors is None):
                state, error = "failed", self._error
            result.append(
                {
                    **record,
                    "vector": {
                        "status": state if self.configured else "not_configured",
                        "version_id": record["version_id"],
                        "chunk_count": job["chunk_count"] if valid and state == "ready" else 0,
                        "error": error,
                        "updated_at": job["updated_at"] if valid else record["updated_at"],
                    },
                }
            )
        return result

    def status(self):
        ready, total = 0, 0
        if self.configured:
            with self.repository.connection() as database:
                documents, jobs = self._documents(database), self._latest(database)
            total = len(documents)
            ready = sum(
                self._matches(jobs.get(identifier), doc) and jobs[identifier]["status"] == "ready"
                for identifier, doc in documents.items()
            )
        return {
            "configured": self.configured,
            "model": self.config.embedding_model,
            "dimensions": self.config.embedding_dimensions,
            "ready_documents": ready,
            "indexed_documents": ready,
            "total_documents": total,
            "status": "not_configured"
            if not self.configured
            else "failed"
            if self._error
            else "ready",
            "error": self._error,
        }

    def _recover(self):
        with self.repository.connection() as database:
            database.execute(
                "UPDATE library_vector_jobs SET status='queued',lease_id=NULL,updated_at=? "
                "WHERE profile=? AND status='processing'",
                (timestamp(), self.profile),
            )

    def _insert_job(self, database, document):
        now, identifier = timestamp(), uuid4().hex
        database.execute(
            "UPDATE library_vector_jobs SET status='cancelled',lease_id=NULL,updated_at=? "
            "WHERE profile=? AND document_id=? AND status<>'cancelled'",
            (now, self.profile, document["id"]),
        )
        database.execute(
            "INSERT INTO library_vector_jobs(id,profile,document_id,version_id,generation,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,'queued',?,?)",
            (
                identifier,
                self.profile,
                document["id"],
                document["version_id"],
                document["generation"],
                now,
                now,
            ),
        )
        return {
            "id": identifier,
            "document_id": document["id"],
            "version_id": document["version_id"],
            "status": "queued",
            "chunk_count": 0,
            "error": None,
            "updated_at": now,
        }

    def enqueue(self, document_id, *, force=True):
        if not self.configured:
            raise LibraryError(503, "Configure the embedding API key and base URL first")
        with self.repository.connection() as database:
            existing = database.execute(
                "SELECT 1 FROM library_files WHERE id=? AND deleted_at IS NULL",
                (document_id,),
            ).fetchone()
            if existing is None:
                raise LibraryError(404, "File not found")
            document = self._documents(database, [document_id]).get(document_id)
            if document is None:
                raise LibraryError(409, "Text indexing must finish before semantic indexing")
            latest = self._latest(database, [document_id]).get(document_id)
            if not force and self._matches(latest, document) and latest["status"] != "cancelled":
                return latest
            result = self._insert_job(database, document)
        self.wake.set()
        return result

    def _discover(self):
        with self.repository.connection() as database:
            documents, jobs = self._documents(database), self._latest(database)
            for identifier, job in jobs.items():
                if (
                    not self._matches(job, documents.get(identifier))
                    and job["status"] != "cancelled"
                ):
                    database.execute(
                        "UPDATE library_vector_jobs SET status='cancelled',lease_id=NULL,updated_at=? WHERE id=?",
                        (timestamp(), job["id"]),
                    )
            for identifier, document in documents.items():
                job = jobs.get(identifier)
                if not self._matches(job, document) or job["status"] == "cancelled":
                    self._insert_job(database, document)

    def _claim(self):
        with self.repository.connection() as database:
            row = database.execute(
                "SELECT * FROM library_vector_jobs j WHERE profile=? AND status='queued' "
                "AND j.seq=(SELECT MAX(k.seq) FROM library_vector_jobs k "
                "WHERE k.profile=j.profile AND k.document_id=j.document_id) ORDER BY seq LIMIT 1",
                (self.profile,),
            ).fetchone()
            if row is None:
                return None
            job = dict(row)
            lease = uuid4().hex
            database.execute(
                "UPDATE library_vector_jobs SET status='processing',lease_id=?,updated_at=? WHERE id=?",
                (lease, timestamp(), job["id"]),
            )
            return {**job, "lease_id": lease, "status": "processing"}

    def _active(self, database, job):
        latest = self._latest(database, [job["document_id"]]).get(job["document_id"])
        document = self._documents(database, [job["document_id"]]).get(job["document_id"])
        return bool(
            latest
            and latest["id"] == job["id"]
            and latest["status"] == "processing"
            and latest["lease_id"] == job["lease_id"]
            and self._matches(job, document)
        )

    def _finish(self, job, state, error=None, count=0):
        with self.repository.connection() as database:
            if not self._active(database, job):
                return False
            database.execute(
                "UPDATE library_vector_jobs SET status=?,error=?,chunk_count=?,lease_id=NULL,updated_at=? "
                "WHERE id=?",
                (state, error, count, timestamp(), job["id"]),
            )
            return True

    def _cached_embeddings(self, chunks, job):
        values, missing = {}, {}
        for chunk in chunks:
            digest = hashlib.sha256(chunk["text"].encode()).hexdigest()
            chunk["text_hash"] = digest
            missing[digest] = chunk["text"]
        with self.repository.connection() as database:
            for digest in list(missing):
                row = database.execute(
                    "SELECT vector FROM library_embedding_cache WHERE profile=? AND text_hash=?",
                    (self.profile, digest),
                ).fetchone()
                if row:
                    try:
                        vector = json.loads(row[0])
                        if (
                            len(vector) != self.config.embedding_dimensions
                            or any(
                                type(value) not in (float, int) or not math.isfinite(value)
                                for value in vector
                            )
                            or not math.hypot(*vector)
                        ):
                            raise ValueError("Invalid cached vector")
                        values[digest] = vector
                        missing.pop(digest)
                    except (ValueError, TypeError, OverflowError):
                        database.execute(
                            "DELETE FROM library_embedding_cache WHERE profile=? AND text_hash=?",
                            (self.profile, digest),
                        )
        pending = list(missing.items())
        for first in range(0, len(pending), 10):
            if self.stop.is_set():
                raise InterruptedError("Semantic indexing stopped")
            with self.repository.connection() as database:
                if not self._active(database, job):
                    raise InterruptedError("Semantic indexing was superseded")
            batch = pending[first : first + 10]
            vectors = self.provider.embed([text for _, text in batch], kind="document")
            if len(vectors) != len(batch):
                raise EmbeddingError("Embedding service returned an incorrect result count")
            with self.repository.connection() as database:
                for (digest, _), vector in zip(batch, vectors):
                    database.execute(
                        "INSERT OR REPLACE INTO library_embedding_cache VALUES (?,?,?,?)",
                        (self.profile, digest, json.dumps(vector, allow_nan=False), timestamp()),
                    )
                    values[digest] = vector
        return [values[chunk["text_hash"]] for chunk in chunks]

    def _process(self, job):
        try:
            with self.repository.connection() as database:
                if not self._active(database, job):
                    return
                chunks = [
                    dict(row)
                    for row in database.execute(
                        "SELECT * FROM library_chunks WHERE document_id=? AND version_id=? ORDER BY ordinal",
                        (job["document_id"], job["version_id"]),
                    )
                ]
            vectors = self._cached_embeddings(chunks, job)
            if self.stop.is_set():
                raise InterruptedError("Semantic indexing stopped")
            with self.repository.connection() as database:
                active = self._active(database, job)
            if not active:
                return
            self._ensure_vectors().upsert(job["id"], chunks, vectors)
            if not self._finish(job, "ready", count=len(chunks)):
                self._ensure_vectors().delete_job(job["id"])
        except InterruptedError:
            self._finish(job, "queued")
        except (EmbeddingError, VectorIndexError) as error:
            self._finish(job, "failed", str(error))
        except Exception:
            logger.warning("Semantic document indexing failed")
            self._finish(job, "failed", "Semantic indexing failed; check configuration and retry")

    def _cleanup(self):
        with self.repository.connection() as database:
            retired = [
                row[0]
                for row in database.execute(
                    "SELECT id FROM library_vector_jobs WHERE profile=? AND status='cancelled' AND cleaned=0 LIMIT 50",
                    (self.profile,),
                )
            ]
        for identifier in retired:
            try:
                self._ensure_vectors().delete_job(identifier)
            except VectorIndexError:
                # A retired job remains excluded from lookup. Retry deletion
                # without preventing other documents from becoming searchable.
                continue
            with self.repository.connection() as database:
                database.execute(
                    "UPDATE library_vector_jobs SET cleaned=1 WHERE id=?", (identifier,)
                )

    def _run(self):
        try:
            while not self.stop.is_set():
                try:
                    self._ensure_vectors()
                    self._recover()
                    self._discover()
                    self._cleanup()
                    self._error = None
                    job = self._claim()
                    if job:
                        self._process(job)
                        continue
                except (VectorIndexError, EmbeddingError) as error:
                    self._error = str(error)
                except Exception:
                    self._error = "Semantic indexing worker could not access its persistent queue"
                self.wake.wait(timeout=1)
                self.wake.clear()
        finally:
            self._close_resources()

    def read_chunk(self, document_id, chunk_id):
        return self.ingestion.read_chunk(document_id, chunk_id)

    def _current_hits(self, hits, document_ids, favorites_only):
        if document_ids is not None and not document_ids:
            return []
        conditions, values = ["deleted_at IS NULL"], []
        if document_ids is not None:
            conditions.append(f"id IN ({','.join('?' for _ in document_ids)})")
            values.extend(document_ids)
        if favorites_only:
            conditions.append("favorite=1")
        with self.repository.connection() as database:
            allowed = {
                row[0]
                for row in database.execute(
                    "SELECT id FROM library_files WHERE " + " AND ".join(conditions),
                    values,
                )
            }
        result = []
        for hit in hits:
            if hit["document_id"] not in allowed:
                continue
            try:
                current = self.read_chunk(hit["document_id"], hit["chunk_id"])
            except LibraryError:
                continue
            if current["version_id"] == hit["version_id"]:
                result.append({**current, "score": hit["score"]})
        return result

    def _ready_context(self, document_ids, favorites_only):
        with self.repository.connection() as database:
            documents = self._documents(database, document_ids, favorites_only)
            jobs = self._latest(database, list(documents))
        return {
            job["id"]: job
            for identifier, job in jobs.items()
            if job["status"] == "ready" and self._matches(job, documents.get(identifier))
        }, documents

    def _hydrate(self, points, document_ids, favorites_only):
        jobs, _ = self._ready_context(document_ids, favorites_only)
        hits = []
        for point in points:
            payload = point.get("payload") or {}
            job = jobs.get(payload.get("job_id"))
            if (
                not job
                or job["document_id"] != payload.get("document_id")
                or job["version_id"] != payload.get("version_id")
            ):
                continue
            try:
                hit = self.read_chunk(job["document_id"], payload["chunk_id"])
            except (LibraryError, KeyError):
                continue
            score = point.get("score")
            if (
                hit["version_id"] != job["version_id"]
                or type(score) not in (int, float)
                or not math.isfinite(score)
            ):
                continue
            hits.append({**hit, "score": float(score)})
        return hits

    def search(self, query, *, document_ids=None, favorites_only=False, limit=10, mode="hybrid"):
        query = query.strip()
        if not query or len(query) > 2000 or not 1 <= limit <= 50:
            raise LibraryError(422, "Provide a query and a result limit between 1 and 50")
        if mode not in ("keyword", "hybrid"):
            raise LibraryError(422, "Search mode must be keyword or hybrid")
        if document_ids is not None and (
            not isinstance(document_ids, list)
            or len(document_ids) > 100
            or any(not isinstance(identifier, str) for identifier in document_ids)
        ):
            raise LibraryError(422, "Document scope must be a list of file IDs")
        depth = max(20, limit * 3)
        lexical = self.ingestion.search(
            query,
            document_ids=document_ids,
            favorites_only=favorites_only,
            limit=limit if mode == "keyword" else depth,
        )
        if mode == "keyword":
            return lexical
        warning = None
        semantic = []
        if not self.configured:
            warning = "Semantic retrieval is not configured; keyword results are shown"
        else:
            jobs, documents = self._ready_context(document_ids, favorites_only)
            if not jobs:
                warning = "Semantic indexes are not ready in this scope; keyword results are shown"
            else:
                try:
                    index = self._ensure_vectors()
                    # Opening a missing/incomplete store can retire persisted
                    # readiness records. Refresh before a paid query request.
                    jobs, documents = self._ready_context(document_ids, favorites_only)
                    if not jobs:
                        raise VectorIndexError("Semantic indexes are being restored")
                    vector = self.provider.embed([query], kind="query")[0]
                    candidate_limit = depth
                    while candidate_limit <= 2048:
                        points = index.query(vector, list(jobs), limit=candidate_limit)
                        semantic = self._hydrate(points, document_ids, favorites_only)
                        if len(semantic) >= depth or len(points) < candidate_limit:
                            break
                        candidate_limit *= 2
                    if len(jobs) < len(documents):
                        warning = "Some semantic indexes are still pending; keyword retrieval covers available text"
                except (EmbeddingError, VectorIndexError, IndexError):
                    warning = (
                        "Semantic retrieval is temporarily unavailable; keyword results are shown"
                    )
        if warning and not semantic:
            return {
                "mode": "keyword",
                "hits": self._current_hits(lexical["hits"], document_ids, favorites_only)[:limit],
                "warning": warning,
            }
        lexical["hits"] = self._current_hits(lexical["hits"], document_ids, favorites_only)
        by_id, scores = {}, {}
        for ranking in (lexical["hits"], semantic):
            for rank, hit in enumerate(ranking, 1):
                identifier = hit["chunk_id"]
                by_id[identifier] = hit
                scores[identifier] = scores.get(identifier, 0.0) + 1.0 / (60 + rank)
        identifiers = sorted(by_id, key=lambda identifier: (-scores[identifier], identifier))[
            :limit
        ]
        return {
            "mode": "hybrid",
            "hits": [
                {**by_id[identifier], "score": scores[identifier]} for identifier in identifiers
            ],
            "warning": warning,
        }
