"""Durable library metadata and versioned original-file storage."""

import hashlib
import os
import re
import sqlite3
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def timestamp():
    return datetime.now(timezone.utc).isoformat()


PUBLIC_FIELDS = (
    "id",
    "name",
    "original_name",
    "extension",
    "mime_type",
    "size_bytes",
    "sha256",
    "created_at",
    "updated_at",
    "favorite",
    "version_id",
    "status",
)


def public_record(record):
    result = {key: record[key] for key in PUBLIC_FIELDS}
    result["favorite"] = bool(result["favorite"])
    return result


class LibraryRepository:
    """Each operation owns a short SQLite transaction; no connection crosses threads."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connection() as database:
            database.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS library_files (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    name_search TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    favorite INTEGER NOT NULL DEFAULT 0 CHECK (favorite IN (0, 1)),
                    version_id TEXT NOT NULL,
                    storage_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'stored' CHECK (status = 'stored'),
                    deleted_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS library_active_hash
                    ON library_files(sha256) WHERE deleted_at IS NULL;
                CREATE INDEX IF NOT EXISTS library_updated
                    ON library_files(deleted_at, updated_at);
                CREATE TABLE IF NOT EXISTS library_cleanup (
                    storage_key TEXT PRIMARY KEY,
                    last_error TEXT NOT NULL DEFAULT ''
                );
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connection(self):
        with self.lock:
            database = sqlite3.connect(self.path, timeout=30)
            database.row_factory = sqlite3.Row
            try:
                with database:
                    yield database
            finally:
                database.close()

    def get(self, file_id):
        with self.connection() as database:
            row = database.execute(
                "SELECT * FROM library_files WHERE id = ? AND deleted_at IS NULL", (file_id,)
            ).fetchone()
        if row is None:
            raise KeyError(file_id)
        return dict(row)

    def list(self, *, q="", favorite=None, extension=None, sort="updated_desc", limit=50, offset=0):
        conditions, values = ["deleted_at IS NULL"], []
        if q:
            escaped = q.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append("name_search LIKE ? ESCAPE '\\'")
            values.append(f"%{escaped}%")
        if favorite is not None:
            conditions.append("favorite = ?")
            values.append(int(favorite))
        if extension:
            conditions.append("extension = ?")
            values.append(extension)
        where = " AND ".join(conditions)
        order = {
            "updated_desc": "updated_at DESC, id ASC",
            "name_asc": "name_search ASC, id ASC",
            "size_desc": "size_bytes DESC, id ASC",
        }[sort]
        with self.connection() as database:
            counts = database.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(favorite), 0) AS favorites "
                "FROM library_files WHERE deleted_at IS NULL"
            ).fetchone()
            total = database.execute(
                f"SELECT COUNT(*) FROM library_files WHERE {where}", values
            ).fetchone()[0]
            rows = database.execute(
                f"SELECT * FROM library_files WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
        return {
            "items": [public_record(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "counts": {"all": counts["total"], "favorites": counts["favorites"]},
        }

    def create(self, record):
        columns = (*PUBLIC_FIELDS, "name_search", "storage_key")
        record = {**record, "name_search": record["name"].casefold()}
        with self.connection() as database:
            database.execute(
                f"INSERT INTO library_files ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                [record[key] for key in columns],
            )
            database.execute(
                "DELETE FROM library_cleanup WHERE storage_key = ?", (record["storage_key"],)
            )

    def patch(self, file_id, *, name=None, favorite=None):
        assignments, values = ["updated_at = ?"], [timestamp()]
        if name is not None:
            assignments.extend(["name = ?", "name_search = ?"])
            values.extend([name, name.casefold()])
        if favorite is not None:
            assignments.append("favorite = ?")
            values.append(int(favorite))
        with self.connection() as database:
            changed = database.execute(
                f"UPDATE library_files SET {', '.join(assignments)} "
                "WHERE id = ? AND deleted_at IS NULL",
                [*values, file_id],
            ).rowcount
            if not changed:
                raise KeyError(file_id)

    def replace(self, file_id, record):
        with self.connection() as database:
            old = database.execute(
                "SELECT storage_key FROM library_files WHERE id = ? AND deleted_at IS NULL",
                (file_id,),
            ).fetchone()
            if old is None:
                raise KeyError(file_id)
            database.execute(
                "UPDATE library_files SET original_name = ?, size_bytes = ?, sha256 = ?, "
                "version_id = ?, storage_key = ?, updated_at = ? WHERE id = ?",
                [
                    record[key]
                    for key in (
                        "original_name",
                        "size_bytes",
                        "sha256",
                        "version_id",
                        "storage_key",
                    )
                ]
                + [timestamp(), file_id],
            )
            database.execute(
                "INSERT OR IGNORE INTO library_cleanup(storage_key) VALUES (?)",
                (old["storage_key"],),
            )
            database.execute(
                "DELETE FROM library_cleanup WHERE storage_key = ?", (record["storage_key"],)
            )

    def delete(self, file_id):
        with self.connection() as database:
            row = database.execute(
                "SELECT storage_key FROM library_files WHERE id = ? AND deleted_at IS NULL",
                (file_id,),
            ).fetchone()
            if row is None:
                raise KeyError(file_id)
            database.execute(
                "UPDATE library_files SET deleted_at = ? WHERE id = ?", (timestamp(), file_id)
            )
            database.execute(
                "INSERT OR IGNORE INTO library_cleanup(storage_key) VALUES (?)",
                (row["storage_key"],),
            )

    def schedule_cleanup(self, storage_key):
        with self.connection() as database:
            database.execute(
                "INSERT OR IGNORE INTO library_cleanup(storage_key) VALUES (?)", (storage_key,)
            )

    def pending_cleanup(self):
        with self.connection() as database:
            return [row[0] for row in database.execute("SELECT storage_key FROM library_cleanup")]

    def finish_cleanup(self, storage_key, error=None):
        with self.connection() as database:
            if error is None:
                database.execute(
                    "DELETE FROM library_cleanup WHERE storage_key = ?", (storage_key,)
                )
            else:
                database.execute(
                    "UPDATE library_cleanup SET last_error = ? WHERE storage_key = ?",
                    (str(error)[:1000], storage_key),
                )

    def is_active_blob(self, storage_key):
        with self.connection() as database:
            return (
                database.execute(
                    "SELECT 1 FROM library_files WHERE storage_key = ? AND deleted_at IS NULL",
                    (storage_key,),
                ).fetchone()
                is not None
            )


@dataclass(frozen=True)
class StagedUpload:
    path: Path
    size_bytes: int
    sha256: str
    prefix: bytes


class LibraryBlobStore:
    KEY = re.compile(r"[0-9a-f]{32}/[0-9a-f]{32}/source\.(?:md|txt|pdf)\Z")

    def __init__(self, root):
        self.root = Path(root).absolute()
        self._assert_plain(self.root)
        self.originals = self.root / "originals"
        self.staging = self.root / ".staging"
        self._assert_plain(self.originals)
        self._assert_plain(self.staging)
        self.originals.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _assert_plain(path):
        for item in (path, *path.parents):
            try:
                details = item.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(details.st_mode) or (
                getattr(details, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise ValueError("Library storage cannot contain symbolic links or junctions")

    def path_for(self, key):
        if not self.KEY.fullmatch(key):
            raise ValueError("Invalid library storage key")
        path = self.originals.joinpath(*key.split("/"))
        self._assert_plain(path)
        return path

    def stage(self, source, max_bytes):
        path = self.staging / f"{uuid4().hex}.part"
        digest, size, prefix = hashlib.sha256(), 0, b""
        try:
            with path.open("xb") as output:
                while chunk := source.read(64 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise OverflowError("File exceeds the 25 MiB upload limit")
                    if len(prefix) < 8:
                        prefix = (prefix + chunk)[:8]
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if not size:
                raise ValueError("Empty files cannot be uploaded")
            return StagedUpload(path, size, digest.hexdigest(), prefix)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def publish(self, staged, key):
        destination = self.path_for(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged.path, destination)

    def remove(self, key):
        path = self.path_for(key)
        path.unlink(missing_ok=True)
        for directory in (path.parent, path.parent.parent):
            try:
                directory.rmdir()
            except (FileNotFoundError, OSError):
                break

    def clear_staging(self):
        for path in self.staging.iterdir():
            if re.fullmatch(r"[0-9a-f]{32}\.part", path.name):
                self._assert_plain(path)
                path.unlink(missing_ok=True)
