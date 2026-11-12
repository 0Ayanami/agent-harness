"""Real application BM25 ranking, cache invalidation and scoped current reads."""

import io
import math
from contextlib import contextmanager

import pytest

from agent_harness.infrastructure.application_bm25 import ApplicationBM25Index
from agent_harness.services.ingestion import IngestionService
from agent_harness.services.library import LibraryService
from test_services import wait_until


@pytest.fixture
def sparse_index(tmp_path):
    library = LibraryService(tmp_path / "library")
    ingestion = IngestionService(library, cache_dir=tmp_path / "cache")
    library.start()
    ingestion.start()
    try:
        yield library, ingestion, ApplicationBM25Index(ingestion.index)
    finally:
        ingestion.close()
        library.close()


def upload_ready(parts, name, body):
    library, ingestion, _ = parts
    record = library.upload(name, io.BytesIO(body.encode()))
    wait_until(lambda: ingestion.status(record["id"])["status"] == "ready", timeout=15)
    return record


def test_bm25_formula_and_common_terms_have_positive_scores(sparse_index):
    first = upload_ready(sparse_index, "alpha.txt", "orbital orbital shuttle")
    second = upload_ready(sparse_index, "bravo.txt", "orbital shuttle cargo")
    upload_ready(sparse_index, "charlie.txt", "shuttle cargo")
    _, _, index = sparse_index
    hits = index.search("orbital")
    assert [hit["document_id"] for hit in hits] == [first["id"], second["id"]]
    mean_length = 8 / 3
    idf = math.log1p(1.5 / 2.5)
    expected = idf * 2 * 2.2 / (2 + 1.2 * (0.25 + 0.75 * 3 / mean_length))
    assert hits[0]["score"] == pytest.approx(expected)
    common = index.search("shuttle")
    assert len(common) == 3
    assert all(math.isfinite(hit["score"]) and hit["score"] > 0 for hit in common)
    assert index.search("orbital orbital") == hits, "Repeated query tokens do not inflate rank"


def test_scope_and_short_literals_use_current_metadata(sparse_index):
    library, _, index = sparse_index
    first = upload_ready(sparse_index, "说明.md", "# 冷启动\n蓝鲸协议要求恢复 NEBULA_731 检查点。")
    second = upload_ready(sparse_index, "other.txt", "An orbital startup checklist.")
    library.patch(first["id"], favorite=True)
    assert [hit["document_id"] for hit in index.search("恢复", favorites_only=True)] == [first["id"]]
    assert index.search("startup", document_ids=[first["id"]]) == []
    assert index.search("恢复", document_ids=[]) == []
    assert index.search("startup", favorites_only=True) == []
    assert index.search("鲸")[0]["document_id"] == first["id"]
    assert index.search("NEBULA_731")[0]["document_id"] == first["id"]
    library.patch(first["id"], name="fresh-name.md", favorite=False)
    assert index.search("恢复", favorites_only=True) == []
    assert index.search("fresh-name.md")[0]["name"] == "fresh-name.md"
    library.patch(second["id"], favorite=True)
    assert [hit["document_id"] for hit in index.search("startup", favorites_only=True)] == [second["id"]]


def test_revision_invalidates_for_publish_replace_delete_and_reopen(sparse_index):
    library, ingestion, index = sparse_index
    first = upload_ready(sparse_index, "notes.txt", "Original orbital document.")
    old = index.search("orbital")[0]
    cached = index._snapshot
    assert cached is not None
    replacement = library.replace(first["id"], "notes.txt", io.BytesIO(b"Updated ceramic document."))
    # Replacement invalidates old chunks before asynchronous processing finishes.
    assert index.search("orbital") == []
    wait_until(lambda: ingestion.status(first["id"])["status"] == "ready", timeout=15)
    new = index.search("ceramic")[0]
    assert new["version_id"] == replacement["version_id"] != old["version_id"]
    assert new["chunk_id"] != old["chunk_id"]
    assert index._snapshot is not cached
    reopened = ApplicationBM25Index(ingestion.index)
    assert reopened.search("ceramic") == [new]
    library.delete(first["id"])
    assert index.search("ceramic") == []
    assert reopened.search("ceramic") == []


def test_cached_index_avoids_retokenizing_and_rehydrates_in_one_batch(
    sparse_index, monkeypatch
):
    library, ingestion, index = sparse_index
    for number in range(25):
        upload_ready(sparse_index, f"part-{number}.txt", f"Orbital passage number {number}.")
    ingestion.close()
    original_tokens = index.tokenizer.tokens
    calls = []

    def query_tokens(text, *, query=False):
        calls.append(query)
        assert query, "Document tokens come from the persisted tokenizer output"
        return original_tokens(text, query=query)

    monkeypatch.setattr(index.tokenizer, "tokens", query_tokens)
    assert len(index.search("orbital", limit=20)) == 20
    snapshot = index._snapshot
    connections = 0
    original_connection = library.repository.connection

    @contextmanager
    def counted_connection():
        nonlocal connections
        connections += 1
        with original_connection() as database:
            yield database

    monkeypatch.setattr(library.repository, "connection", counted_connection)
    for _ in range(3):
        before = connections
        hits = index.search("orbital", limit=20)
        assert len(hits) == 20
        assert connections - before == 2, "One revision snapshot and one current passage batch"
        assert index._snapshot is snapshot
    assert calls == [True] * 4


def test_batch_revalidation_filters_mutations_after_cached_scoring(sparse_index, monkeypatch):
    library, ingestion, index = sparse_index
    first = upload_ready(sparse_index, "first.txt", "Orbital original procedure.")
    second = upload_ready(sparse_index, "second.txt", "Orbital current procedure.")
    for document in (first, second):
        library.patch(document["id"], favorite=True)
    index.search("orbital")
    original_read = ingestion.index.read_chunks

    def mutate_then_read(chunk_ids, **kwargs):
        library.patch(first["id"], favorite=False)
        return original_read(chunk_ids, **kwargs)

    monkeypatch.setattr(ingestion.index, "read_chunks", mutate_then_read)
    hits = index.search("orbital", favorites_only=True)
    assert [hit["document_id"] for hit in hits] == [second["id"]]


def test_query_syntax_is_literal_and_empty_inputs_are_safe(sparse_index):
    _, _, index = sparse_index
    document = upload_ready(sparse_index, "literal.txt", "Literal SQL token NEBULA_731 and 100% / safe.")
    assert index.search("NEBULA_731")[0]["document_id"] == document["id"]
    assert index.search("100%")[0]["document_id"] == document["id"]
    assert index.search(" ") == []
    assert index.search("nonexistent") == []
    index.search('" OR NOT * NEAR(SELECT)')
    with pytest.raises(ValueError, match="positive integer"):
        index.search("safe", limit=True)
