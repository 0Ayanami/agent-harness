"""A recoverable local ingestion worker and real lexical document retrieval."""

import logging
import threading

from ..infrastructure.document_parser import DocumentParseError, DocumentParser, KeywordTokenizer
from ..infrastructure.library_index import LibraryIndexRepository
from .library import LibraryError

logger = logging.getLogger(__name__)


class IngestionService:
    def __init__(self, library, *, cache_dir):
        self.library = library
        self.parser = DocumentParser()
        self.index = LibraryIndexRepository(library.repository, KeywordTokenizer(cache_dir))
        self.shutdown = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.lifecycle_lock = threading.Lock()
        library.ingestion = self

    def start(self):
        with self.lifecycle_lock:
            if self.thread and self.thread.is_alive():
                return
            self.index.recover()
            self.shutdown.clear()
            self.thread = threading.Thread(target=self._run, name="library-ingestion", daemon=True)
            self.thread.start()

    def close(self):
        self.shutdown.set()
        self.wake.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)

    def enqueue(self, document_id, *, force=False):
        try:
            result = self.index.enqueue(document_id, force=force)
        except KeyError:
            raise LibraryError(404, "File not found") from None
        self.wake.set()
        return result

    def status(self, document_id):
        return self.index.statuses([document_id]).get(document_id)

    def get_job(self, job_id):
        try:
            return self.index.get_job(job_id)
        except KeyError:
            raise LibraryError(404, "Ingestion job not found") from None

    def decorate(self, records):
        statuses = self.index.statuses([record["id"] for record in records])
        return [{**record, "ingestion": statuses.get(record["id"])} for record in records]

    def invalidate(self, document_id):
        self.index.invalidate(document_id)

    def search(self, query, *, document_ids=None, favorites_only=False, limit=10):
        query = query.strip()
        if not query:
            raise LibraryError(422, "Search query must not be empty")
        hits = self.index.search(
            query, document_ids=document_ids, favorites_only=favorites_only, limit=limit
        )
        return {"mode": "keyword", "hits": hits, "warning": None}

    def read_chunk(self, document_id, chunk_id):
        try:
            return self.index.read_chunk(document_id, chunk_id)
        except KeyError:
            raise LibraryError(
                404, "Chunk is missing or no longer belongs to the current document"
            ) from None

    def read_chunks(self, chunk_ids, *, document_ids=None, favorites_only=False):
        return self.index.read_chunks(
            chunk_ids, document_ids=document_ids, favorites_only=favorites_only
        )

    def _process(self, job):
        key = None
        try:
            record, path, key = self.library.acquire_content(job["document_id"])
            if record["version_id"] != job["version_id"]:
                self.index.abandon(job)
                return
            chunks = self.parser.parse(path, record["extension"])
            # All extracted text is now owned by this job; indexing no longer
            # needs to retain the old original during tokenization or SQL writes.
            self.library.release_content(key)
            key = None
            if self.shutdown.is_set():
                self.index.finish(job, "queued")
                return
            self.index.publish(job, chunks, should_publish=lambda: not self.shutdown.is_set())
        except DocumentParseError as error:
            self.index.finish(job, "needs_ocr" if error.needs_ocr else "failed", str(error))
        except LibraryError as error:
            self.index.finish(job, "failed", str(error))
        except Exception as error:
            logger.warning("Library ingestion failed: %s", type(error).__name__)
            self.index.finish(
                job, "failed", "Document processing failed; retry indexing or check the file"
            )
        finally:
            if key is not None:
                self.library.release_content(key)

    def _run(self):
        while not self.shutdown.is_set():
            try:
                self.index.recover_processing()
                job = self.index.claim()
                if job:
                    self._process(job)
                    continue
            except Exception:
                # Transient database failures do not silently kill the worker;
                # durable queued/processing jobs remain available after recovery.
                logger.exception("Library worker could not access its job queue")
            self.wake.wait(timeout=0.5)
            self.wake.clear()
