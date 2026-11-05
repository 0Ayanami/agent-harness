"""File-library use cases; saved originals are distinct from future search indexes."""

import io
import sqlite3
import threading
from collections import Counter
from pathlib import Path
from uuid import uuid4

from ..infrastructure.library import (
    LibraryBlobStore,
    LibraryRepository,
    public_record,
    timestamp,
)


class LibraryError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


class LibraryService:
    MAX_BYTES = 25 * 1024 * 1024
    PREVIEW_CHARS = 200_000
    MIME_TYPES = {"md": "text/plain", "txt": "text/plain", "pdf": "application/pdf"}

    def __init__(self, root):
        self.blobs = LibraryBlobStore(root)
        database_path = self.blobs.root / "library.sqlite3"
        self.blobs._assert_plain(database_path)
        self.repository = LibraryRepository(database_path)
        self.lock = threading.RLock()
        self.readers = Counter()
        self.ingestion = None
        self.retrieval = None

    def _decorate(self, record):
        records = self.ingestion.decorate([record]) if self.ingestion else [record]
        return self.retrieval.decorate(records)[0] if self.retrieval else records[0]

    def _queue(self, file_id):
        if self.ingestion:
            self.ingestion.enqueue(file_id)

    def start(self):
        with self.lock:
            self.blobs.clear_staging()
            self._cleanup()

    def close(self):
        with self.lock:
            self._cleanup()

    @staticmethod
    def validate_name(name):
        name = name.strip()
        if (
            not name
            or len(name) > 255
            or name in (".", "..")
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
            or any(character in '<>:"/\\|?*' for character in name)
            or name.endswith(".")
        ):
            raise LibraryError(422, "Provide a file name without paths or special characters")
        return name

    def _record(self, file_id):
        try:
            return self.repository.get(file_id)
        except KeyError:
            raise LibraryError(404, "File not found") from None

    def get(self, file_id):
        with self.lock:
            return self._decorate(public_record(self._record(file_id)))

    def list(self, **filters):
        extension = filters.get("extension")
        if extension and extension not in self.MIME_TYPES:
            raise LibraryError(422, "Unsupported file type filter")
        with self.lock:
            listing = self.repository.list(**filters)
            if self.ingestion:
                listing["items"] = self.ingestion.decorate(listing["items"])
            if self.retrieval:
                listing["items"] = self.retrieval.decorate(listing["items"])
            return listing

    def _stage(self, source, extension):
        if extension not in self.MIME_TYPES:
            raise LibraryError(415, "Supported file types are Markdown, TXT and PDF")
        try:
            staged = self.blobs.stage(source, self.MAX_BYTES)
        except OverflowError as error:
            raise LibraryError(413, str(error)) from error
        except ValueError as error:
            raise LibraryError(422, str(error)) from error
        if extension == "pdf" and not staged.prefix.startswith(b"%PDF-"):
            staged.path.unlink(missing_ok=True)
            raise LibraryError(415, "The uploaded file does not have a PDF header")
        return staged

    def upload(self, name, source):
        name = self.validate_name(name)
        extension = Path(name).suffix.lower().lstrip(".")
        staged = self._stage(source, extension)
        file_id, version_id, now = uuid4().hex, uuid4().hex, timestamp()
        key = f"{file_id}/{version_id}/source.{extension}"
        record = {
            "id": file_id,
            "name": name,
            "original_name": name,
            "extension": extension,
            "mime_type": self.MIME_TYPES[extension],
            "size_bytes": staged.size_bytes,
            "sha256": staged.sha256,
            "created_at": now,
            "updated_at": now,
            "favorite": False,
            "version_id": version_id,
            "storage_key": key,
            "status": "stored",
        }
        try:
            with self.lock:
                self.repository.schedule_cleanup(key)
                self.blobs.publish(staged, key)
                try:
                    self.repository.create(record)
                except sqlite3.IntegrityError:
                    raise LibraryError(409, "A file with the same content already exists") from None
                finally:
                    self._cleanup()
                self._queue(file_id)
        finally:
            staged.path.unlink(missing_ok=True)
        return self.get(file_id)

    def patch(self, file_id, *, name=None, favorite=None):
        if name is None and favorite is None:
            raise LibraryError(422, "Provide a name or favorite value")
        with self.lock:
            record = self._record(file_id)
            if name is not None:
                name = self.validate_name(name)
                if Path(name).suffix.lower().lstrip(".") != record["extension"]:
                    raise LibraryError(422, "Renaming must keep the existing file extension")
            self.repository.patch(file_id, name=name, favorite=favorite)
            return self._decorate(public_record(self._record(file_id)))

    def replace(self, file_id, name, source):
        name = self.validate_name(name)
        extension = Path(name).suffix.lower().lstrip(".")
        if extension not in self.MIME_TYPES:
            raise LibraryError(415, "Supported file types are Markdown, TXT and PDF")
        with self.lock:
            if extension != self._record(file_id)["extension"]:
                raise LibraryError(422, "Replacement must keep the existing file extension")
        staged = self._stage(source, extension)
        version_id = uuid4().hex
        key = f"{file_id}/{version_id}/source.{extension}"
        replacement = {
            "original_name": name,
            "size_bytes": staged.size_bytes,
            "sha256": staged.sha256,
            "version_id": version_id,
            "storage_key": key,
        }
        try:
            with self.lock:
                self._record(file_id)
                self.repository.schedule_cleanup(key)
                self.blobs.publish(staged, key)
                try:
                    self.repository.replace(file_id, replacement)
                except sqlite3.IntegrityError:
                    raise LibraryError(
                        409, "Another file with the same content already exists"
                    ) from None
                finally:
                    self._cleanup()
                self._queue(file_id)
                return self._decorate(public_record(self._record(file_id)))
        finally:
            staged.path.unlink(missing_ok=True)

    def delete(self, file_id):
        with self.lock:
            self._record(file_id)
            self.repository.delete(file_id)
            if self.ingestion:
                self.ingestion.invalidate(file_id)
            self._cleanup()

    def acquire_content(self, file_id, *, version_id=None):
        """Keep old originals alive until a download or PDF range request finishes."""
        with self.lock:
            record = self._record(file_id)
            if version_id is not None and record["version_id"] != version_id:
                raise LibraryError(409, "File version changed; reopen the preview")
            path = self.blobs.path_for(record["storage_key"])
            if not path.is_file():
                raise LibraryError(404, "Original file is missing")
            self.readers[record["storage_key"]] += 1
            return public_record(record), path, record["storage_key"]

    def release_content(self, key):
        with self.lock:
            self.readers[key] -= 1
            if self.readers[key] <= 0:
                del self.readers[key]
            self._cleanup()

    def preview(self, file_id):
        record, path, key = self.acquire_content(file_id)
        try:
            if record["extension"] == "pdf":
                raise LibraryError(415, "Use the original content endpoint to preview PDF files")
            with path.open("rb") as original:
                prefix = original.read(4)
                original.seek(0)
                encoding = (
                    "utf-16" if prefix.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
                )
                with io.TextIOWrapper(original, encoding=encoding) as text:
                    try:
                        content = text.read(self.PREVIEW_CHARS + 1)
                    except UnicodeError:
                        raise LibraryError(
                            422, "Text preview requires UTF-8 or BOM-marked UTF-16"
                        ) from None
            return {
                "text": content[: self.PREVIEW_CHARS],
                "truncated": len(content) > self.PREVIEW_CHARS,
                "media_type": "text/plain",
                "version_id": record["version_id"],
            }
        finally:
            self.release_content(key)

    def _cleanup(self):
        for key in self.repository.pending_cleanup():
            if self.readers.get(key) or self.repository.is_active_blob(key):
                continue
            try:
                self.blobs.remove(key)
            except (OSError, ValueError) as error:
                self.repository.finish_cleanup(key, error)
            else:
                self.repository.finish_cleanup(key)
