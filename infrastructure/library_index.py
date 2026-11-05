"""Persistent ingestion jobs and an atomic, current-version FTS5 index."""

import hashlib
import json
from uuid import NAMESPACE_URL, uuid4, uuid5

from .library import timestamp


class LibraryIndexRepository:
    def __init__(self, repository, tokenizer):
        self.repository = repository
        self.tokenizer = tokenizer
        with repository.connection() as database:
            database.executescript("""
                CREATE TABLE IF NOT EXISTS library_index_schema(version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO library_index_schema VALUES (1);
                CREATE TABLE IF NOT EXISTS library_ingestion_jobs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, version_id TEXT NOT NULL,
                    status TEXT NOT NULL, lease_id TEXT,
                    chunk_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS library_job_document
                    ON library_ingestion_jobs(document_id,seq DESC);
                CREATE INDEX IF NOT EXISTS library_job_status
                    ON library_ingestion_jobs(status,seq);
                CREATE TABLE IF NOT EXISTS library_chunks (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL, version_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL, text TEXT NOT NULL, search_text TEXT NOT NULL,
                    location TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS library_chunk_version
                    ON library_chunks(document_id,version_id,ordinal);
                CREATE VIRTUAL TABLE IF NOT EXISTS library_chunk_fts USING fts5(tokens);
            """)
            columns = {
                row[1] for row in database.execute("PRAGMA table_info(library_ingestion_jobs)")
            }
            if "lease_id" not in columns:
                database.execute("ALTER TABLE library_ingestion_jobs ADD COLUMN lease_id TEXT")

    @staticmethod
    def _job(row):
        if row is None:
            return None
        result = {
            key: row[key]
            for key in (
                "id",
                "document_id",
                "version_id",
                "status",
                "chunk_count",
                "error",
                "created_at",
                "updated_at",
            )
        }
        if result["status"] == "cancelled":
            result["status"] = "failed"
            result["error"] = "This ingestion was superseded by a newer document or retry"
        return result

    def enqueue(self, document_id, *, force=False):
        with self.repository.connection() as database:
            record = database.execute(
                "SELECT version_id FROM library_files WHERE id=? AND deleted_at IS NULL",
                (document_id,),
            ).fetchone()
            if record is None:
                raise KeyError(document_id)
            latest = database.execute(
                "SELECT * FROM library_ingestion_jobs WHERE document_id=? ORDER BY seq DESC LIMIT 1",
                (document_id,),
            ).fetchone()
            if latest and latest["version_id"] == record["version_id"] and not force:
                return self._job(latest)
            now, identifier = timestamp(), uuid4().hex
            database.execute(
                "UPDATE library_ingestion_jobs SET status='cancelled',updated_at=? "
                "WHERE document_id=? AND status IN ('queued','processing')",
                (now, document_id),
            )
            database.execute(
                "INSERT INTO library_ingestion_jobs "
                "(id,document_id,version_id,status,created_at,updated_at) VALUES (?,?,?,'queued',?,?)",
                (identifier, document_id, record["version_id"], now, now),
            )
            return self._job(
                database.execute(
                    "SELECT * FROM library_ingestion_jobs WHERE id=?", (identifier,)
                ).fetchone()
            )

    def recover(self):
        with self.repository.connection() as database:
            database.execute(
                "UPDATE library_ingestion_jobs SET status='cancelled',updated_at=? "
                "WHERE status IN ('queued','processing') AND (NOT EXISTS "
                "(SELECT 1 FROM library_files f WHERE f.id=document_id "
                "AND f.version_id=library_ingestion_jobs.version_id AND f.deleted_at IS NULL) "
                "OR seq<>(SELECT MAX(k.seq) FROM library_ingestion_jobs k "
                "WHERE k.document_id=library_ingestion_jobs.document_id))",
                (timestamp(),),
            )
            database.execute(
                "UPDATE library_ingestion_jobs SET status='queued',lease_id=NULL,updated_at=? "
                "WHERE status='processing'",
                (timestamp(),),
            )
            records = database.execute(
                "SELECT id,version_id FROM library_files WHERE deleted_at IS NULL"
            ).fetchall()
        for record in records:
            self.enqueue(record["id"])

    def claim(self):
        with self.repository.connection() as database:
            row = database.execute(
                "SELECT j.* FROM library_ingestion_jobs j JOIN library_files f "
                "ON f.id=j.document_id AND f.version_id=j.version_id AND f.deleted_at IS NULL "
                "WHERE j.status='queued' AND j.seq=(SELECT MAX(k.seq) FROM "
                "library_ingestion_jobs k WHERE k.document_id=j.document_id) ORDER BY j.seq LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            now, lease_id = timestamp(), uuid4().hex
            database.execute(
                "UPDATE library_ingestion_jobs SET status='processing',lease_id=?,updated_at=? WHERE id=?",
                (lease_id, now, row["id"]),
            )
            result = self._job(row)
            result.update(status="processing", updated_at=now, lease_id=lease_id)
            return result

    def get_job(self, job_id):
        with self.repository.connection() as database:
            row = database.execute(
                "SELECT * FROM library_ingestion_jobs WHERE id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._job(row)

    def statuses(self, document_ids):
        if not document_ids:
            return {}
        placeholders = ",".join("?" for _ in document_ids)
        with self.repository.connection() as database:
            rows = database.execute(
                f"SELECT j.* FROM library_ingestion_jobs j JOIN library_files f "
                f"ON f.id=j.document_id AND f.version_id=j.version_id AND f.deleted_at IS NULL "
                f"WHERE j.document_id IN ({placeholders}) AND j.seq=(SELECT MAX(k.seq) "
                f"FROM library_ingestion_jobs k WHERE k.document_id=j.document_id)",
                document_ids,
            ).fetchall()
        return {
            row["document_id"]: {
                "job_id": row["id"],
                "status": row["status"],
                "version_id": row["version_id"],
                "chunk_count": row["chunk_count"],
                "error": row["error"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        }

    def _active(self, database, job):
        return (
            database.execute(
                "SELECT 1 FROM library_files f JOIN library_ingestion_jobs j ON j.document_id=f.id "
                "WHERE f.id=? AND f.version_id=? AND f.deleted_at IS NULL AND j.id=? "
                "AND j.status='processing' AND j.lease_id=? AND j.seq=(SELECT MAX(k.seq) "
                "FROM library_ingestion_jobs k WHERE k.document_id=f.id)",
                (job["document_id"], job["version_id"], job["id"], job["lease_id"]),
            ).fetchone()
            is not None
        )

    def publish(self, job, chunks, *, should_publish=lambda: True):
        prepared = [(chunk, self.tokenizer.encode(chunk["text"])) for chunk in chunks]
        with self.repository.connection() as database:
            if not should_publish():
                if self._active(database, job):
                    database.execute(
                        "UPDATE library_ingestion_jobs SET status='queued',updated_at=? WHERE id=?",
                        (timestamp(), job["id"]),
                    )
                return False
            if not self._active(database, job):
                return False
            self._delete_chunks(database, job["document_id"])
            for ordinal, (chunk, tokens) in enumerate(prepared):
                digest = hashlib.sha256(chunk["text"].encode()).hexdigest()
                identifier = uuid5(
                    NAMESPACE_URL,
                    f"library:{job['document_id']}:{job['version_id']}:{ordinal}:{digest}",
                ).hex
                result = database.execute(
                    "INSERT INTO library_chunks(id,document_id,version_id,ordinal,text,search_text,location) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        identifier,
                        job["document_id"],
                        job["version_id"],
                        ordinal,
                        chunk["text"],
                        self.tokenizer.normalize(chunk["text"]),
                        json.dumps(chunk["location"], ensure_ascii=False),
                    ),
                )
                database.execute(
                    "INSERT INTO library_chunk_fts(rowid,tokens) VALUES (?,?)",
                    (result.lastrowid, tokens),
                )
            database.execute(
                "UPDATE library_ingestion_jobs SET status='ready',chunk_count=?,error=NULL,updated_at=? "
                "WHERE id=?",
                (len(chunks), timestamp(), job["id"]),
            )
        return True

    def finish(self, job, status, error=None):
        with self.repository.connection() as database:
            if self._active(database, job):
                database.execute(
                    "UPDATE library_ingestion_jobs SET status=?,error=?,updated_at=? WHERE id=?",
                    (status, error, timestamp(), job["id"]),
                )

    def abandon(self, job):
        with self.repository.connection() as database:
            database.execute(
                "UPDATE library_ingestion_jobs SET status='cancelled',updated_at=? "
                "WHERE id=? AND status='processing' AND lease_id=?",
                (timestamp(), job["id"], job["lease_id"]),
            )

    def recover_processing(self):
        """Only called by our single worker when it has no in-flight parser."""
        with self.repository.connection() as database:
            database.execute(
                "UPDATE library_ingestion_jobs SET status='queued',lease_id=NULL,updated_at=? "
                "WHERE status='processing'",
                (timestamp(),),
            )

    @staticmethod
    def _delete_chunks(database, document_id):
        database.execute(
            "DELETE FROM library_chunk_fts WHERE rowid IN "
            "(SELECT rowid FROM library_chunks WHERE document_id=?)",
            (document_id,),
        )
        database.execute("DELETE FROM library_chunks WHERE document_id=?", (document_id,))

    def invalidate(self, document_id):
        with self.repository.connection() as database:
            self._delete_chunks(database, document_id)
            database.execute(
                "UPDATE library_ingestion_jobs SET status='cancelled',updated_at=? "
                "WHERE document_id=? AND status IN ('queued','processing')",
                (timestamp(), document_id),
            )

    @staticmethod
    def _hit(row, score=0.0):
        return {
            "chunk_id": row["id"],
            "document_id": row["document_id"],
            "version_id": row["version_id"],
            "name": row["name"],
            "text": row["text"],
            "location": json.loads(row["location"]),
            "score": float(score),
        }

    def read_chunk(self, document_id, chunk_id):
        with self.repository.connection() as database:
            row = database.execute(
                "SELECT c.*,f.name FROM library_chunks c JOIN library_files f "
                "ON f.id=c.document_id AND f.version_id=c.version_id AND f.deleted_at IS NULL "
                "WHERE c.document_id=? AND c.id=?",
                (document_id, chunk_id),
            ).fetchone()
        if row is None:
            raise KeyError(chunk_id)
        return self._hit(row)

    def search(self, query, *, document_ids=None, favorites_only=False, limit=10):
        if document_ids is not None and not document_ids:
            return []
        clauses, parameters = ["f.deleted_at IS NULL", "c.version_id=f.version_id"], []
        if document_ids is not None:
            clauses.append(f"f.id IN ({','.join('?' for _ in document_ids)})")
            parameters.extend(document_ids)
        if favorites_only:
            clauses.append("f.favorite=1")
        where = " AND ".join(clauses)
        words = self.tokenizer.tokens(query, query=True)
        expression = " OR ".join('"' + word.replace('"', '""') + '"' for word in words)
        hits, seen = [], set()
        with self.repository.connection() as database:
            database.create_function("library_normalize", 1, self.tokenizer.normalize)
            if expression:
                rows = database.execute(
                    "SELECT c.*,f.name,bm25(library_chunk_fts) AS rank FROM library_chunk_fts "
                    "JOIN library_chunks c ON c.rowid=library_chunk_fts.rowid "
                    f"JOIN library_files f ON f.id=c.document_id WHERE {where} "
                    "AND library_chunk_fts MATCH ? ORDER BY rank,c.document_id,c.ordinal LIMIT ?",
                    [*parameters, expression, limit],
                ).fetchall()
                for row in rows:
                    hits.append(self._hit(row, -row["rank"]))
                    seen.add(row["id"])
            if len(hits) < limit and len(query) <= 64:
                escaped = (
                    self.tokenizer.normalize(query)
                    .replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                rows = database.execute(
                    "SELECT c.*,f.name FROM library_chunks c JOIN library_files f ON f.id=c.document_id "
                    f"WHERE {where} AND (c.search_text LIKE ? ESCAPE '\\' "
                    "OR library_normalize(f.name) LIKE ? ESCAPE '\\') ORDER BY c.document_id,c.ordinal LIMIT ?",
                    [*parameters, f"%{escaped}%", f"%{escaped}%", limit],
                ).fetchall()
                for row in rows:
                    if row["id"] not in seen:
                        hits.append(self._hit(row))
                        seen.add(row["id"])
                        if len(hits) == limit:
                            break
        return hits
