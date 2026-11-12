"""Milvus SDK contracts plus opt-in real-server library integration.

Set HARNESS_MILVUS_TEST_URI to a test server to run the last two tests. Only
uniquely named test collections are dropped; no .env or online model is used.
"""

import io
import json
import os
from uuid import uuid4

import httpx
import pytest

pytest.importorskip("pymilvus")
from pymilvus import MilvusClient

from agent_harness import HarnessConfig
from agent_harness.infrastructure.library_vectors import VectorIndexError
from agent_harness.infrastructure.milvus_vectors import MilvusLibraryIndex, vector_identifier
from agent_harness.services.ingestion import IngestionService
from agent_harness.services.library import LibraryError, LibraryService
from agent_harness.services.retrieval import RetrievalService
from test_retrieval import FixedEmbeddings
from test_services import wait_until


class RecordingMilvus:
    def __init__(self, *, exists=False, dimensions=3):
        self.exists = exists
        self.dimensions = dimensions
        self.calls = []
        self.failure = None
        self.fields = [
            {"name": name} for name in
            ("id", "job_id", "document_id", "version_id", "section_id", "chunk_id")
        ] + [{"name": "vector", "params": {"dim": dimensions}}]
        self.points = [[{"entity": {"job_id": "job-a", "document_id": "doc-a",
            "version_id": "version-a", "section_id": "section-a", "chunk_id": "chunk-a"},
            "distance": 0.8}]]

    def record(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if self.failure == method:
            raise RuntimeError("untrusted SDK diagnostic secret-test-token")

    def has_collection(self, **kwargs):
        self.record("has_collection", **kwargs)
        return self.exists

    def create_collection(self, **kwargs):
        self.record("create_collection", **kwargs)
        self.exists = True

    def describe_collection(self, **kwargs):
        self.record("describe_collection", **kwargs)
        return {"fields": self.fields}

    def load_collection(self, **kwargs):
        self.record("load_collection", **kwargs)

    def upsert(self, **kwargs):
        self.record("upsert", **kwargs)

    def search(self, **kwargs):
        self.record("search", **kwargs)
        return self.points

    def query(self, **kwargs):
        self.record("query", **kwargs)
        return [{"count(*)": 2}]

    def delete(self, **kwargs):
        self.record("delete", **kwargs)

    def close(self):
        self.record("close")


def chunk(identifier="chunk-a", *, document="doc-a", section="section-a"):
    return {"id": identifier, "document_id": document, "version_id": "version-a",
            "section_id": section}


def test_milvus_sdk_create_batches_stable_ids_and_strong_scoped_search():
    sdk = RecordingMilvus()
    index = MilvusLibraryIndex("http://127.0.0.1:19530", 3, "isolated_test", client=sdk)
    assert index.created
    creation = next(arguments for method, arguments in sdk.calls if method == "create_collection")
    assert creation["consistency_level"] == "Strong"
    fields = {field["name"]: field for field in creation["schema"].to_dict()["fields"]}
    assert fields["id"]["is_primary"]
    assert fields["vector"]["params"]["dim"] == 3
    assert set(fields) == {"id", "job_id", "document_id", "version_id", "section_id", "chunk_id", "vector"}
    assert list(creation["index_params"])[0].to_dict()["metric_type"] == "COSINE"
    chunks = [chunk(f"chunk-{number}") for number in range(129)]
    index.upsert("job-a", chunks, [[1.0, 0.0, 0.0]] * 129)
    batches = [arguments["data"] for method, arguments in sdk.calls if method == "upsert"]
    assert list(map(len, batches)) == [128, 1]
    row = batches[0][0]
    assert row == {"id": vector_identifier("job-a", "chunk-0"), "job_id": "job-a",
                   "document_id": "doc-a", "version_id": "version-a", "section_id": "section-a",
                   "chunk_id": "chunk-0", "vector": [1.0, 0.0, 0.0]}
    assert vector_identifier("job-a", "chunk-0") != vector_identifier("job-b", "chunk-0")
    selected = ['job-a', 'job-"quoted\\value']
    hits = index.query([1.0, 0.0, 0.0], selected, limit=8)
    search = next(arguments for method, arguments in sdk.calls if method == "search")
    assert search["filter"] == "job_id in " + json.dumps(selected)
    assert search["consistency_level"] == "Strong"
    assert search["anns_field"] == "vector" and search["limit"] == 8
    assert search["search_params"] == {"metric_type": "COSINE"}
    assert hits == [{"payload": sdk.points[0][0]["entity"], "score": 0.8}]
    before = len(sdk.calls)
    assert index.query([1.0, 0.0, 0.0], [], limit=8) == []
    assert len(sdk.calls) == before
    assert index.count_job(selected[1]) == 2
    query = next(arguments for method, arguments in sdk.calls if method == "query")
    assert query["filter"] == "job_id == " + json.dumps(selected[1])
    assert query["output_fields"] == ["count(*)"] and query["consistency_level"] == "Strong"
    index.delete_job(selected[1])
    assert sdk.calls[-1][1]["filter"] == "job_id == " + json.dumps(selected[1])
    index.close()
    index.close()
    assert len([method for method, _ in sdk.calls if method == "close"]) == 1


def test_milvus_reopens_valid_schema_and_rejects_mismatched_dimensions_or_fields():
    sdk = RecordingMilvus(exists=True)
    index = MilvusLibraryIndex("http://127.0.0.1:19530", 3, "existing_test", client=sdk)
    assert not index.created
    assert any(method == "load_collection" for method, _ in sdk.calls)
    assert not any(method == "create_collection" for method, _ in sdk.calls)
    index.close()
    for invalid in (RecordingMilvus(exists=True, dimensions=4), RecordingMilvus(exists=True)):
        if invalid.dimensions == 3:
            invalid.fields = [field for field in invalid.fields if field["name"] != "section_id"]
        with pytest.raises(VectorIndexError, match="could not be opened"):
            MilvusLibraryIndex("http://127.0.0.1:19530", 3, "existing_test", client=invalid)


@pytest.mark.parametrize("vector", [[1.0, 0.0], [float("nan"), 0.0, 1.0], [float("inf"), 0.0, 1.0], [True, 0.0, 1.0]])
def test_milvus_rejects_invalid_vectors_before_sdk_call(vector):
    sdk = RecordingMilvus()
    index = MilvusLibraryIndex("http://127.0.0.1:19530", 3, "validation_test", client=sdk)
    with pytest.raises(VectorIndexError, match="dimensions or values"):
        index.upsert("job-a", [chunk()], [vector])
    with pytest.raises(VectorIndexError, match="count does not match"):
        index.upsert("job-a", [chunk()], [])
    assert not any(method == "upsert" for method, _ in sdk.calls)
    index.close()


@pytest.mark.parametrize("method", ["upsert", "search", "delete", "query"])
def test_milvus_provider_errors_are_safe(method):
    sdk = RecordingMilvus()
    index = MilvusLibraryIndex("http://127.0.0.1:19530", 3, "failure_test", client=sdk)
    sdk.failure = method
    operations = {
        "upsert": lambda: index.upsert("job-a", [chunk()], [[1.0, 0.0, 0.0]]),
        "search": lambda: index.query([1.0, 0.0, 0.0], ["job-a"], limit=2),
        "delete": lambda: index.delete_job("job-a"),
        "query": lambda: index.count_job("job-a"),
    }
    with pytest.raises(VectorIndexError) as failure:
        operations[method]()
    assert "secret-test-token" not in str(failure.value)
    index.close()


@pytest.fixture
def real_milvus():
    uri = os.getenv("HARNESS_MILVUS_TEST_URI")
    if not uri:
        pytest.skip("Set HARNESS_MILVUS_TEST_URI for isolated real Milvus integration")
    token = os.getenv("HARNESS_MILVUS_TEST_TOKEN")
    admin = MilvusClient(uri=uri, token=token or "", timeout=20)
    collections = set()
    try:
        yield uri, token, admin, collections
    finally:
        for collection in collections:
            assert collection.startswith("test_ayanami_"), "Never delete a non-test collection"
            if admin.has_collection(collection_name=collection):
                admin.drop_collection(collection_name=collection)
        admin.close()


def test_real_milvus_strong_upsert_filter_delete_reopen_and_dimension_check(real_milvus):
    uri, token, _, collections = real_milvus
    name = "test_ayanami_" + uuid4().hex
    collections.add(name)
    index = MilvusLibraryIndex(uri, 3, name, token=token)
    try:
        assert index.created
        index.upsert("job-a", [chunk("a1"), chunk("a2")], [[1.0, 0.0, 0.0], [0.8, 0.2, 0.0]])
        index.upsert("job-b", [chunk("b1", document="doc-b", section="section-b")], [[0.0, 1.0, 0.0]])
        assert index.count_job("job-a") == 2
        hits = index.query([0.0, 1.0, 0.0], ["job-a"], limit=10)
        assert {hit["payload"]["chunk_id"] for hit in hits} == {"a1", "a2"}
        assert {hit["payload"]["job_id"] for hit in hits} == {"job-a"}
        assert all(hit["payload"]["section_id"] == "section-a" for hit in hits)
        index.upsert("job-a", [chunk("a1")], [[0.0, 0.0, 1.0]])
        assert index.count_job("job-a") == 2, "Upsert uses deterministic IDs"
        assert index.count_job('job-"not-present') == 0
        index.delete_job("job-a")
        assert index.count_job("job-a") == 0
        assert index.query([1.0, 0.0, 0.0], ["job-a"], limit=2) == []
    finally:
        index.close()
    reopened = MilvusLibraryIndex(uri, 3, name, token=token)
    try:
        assert not reopened.created and reopened.count_job("job-b") == 1
        assert reopened.query([0.0, 1.0, 0.0], ["job-b"], limit=1)[0]["payload"]["chunk_id"] == "b1"
    finally:
        reopened.close()
    with pytest.raises(VectorIndexError, match="could not be opened"):
        MilvusLibraryIndex(uri, 4, name, token=token)


def test_real_milvus_bm25_rrf_rerank_structure_and_cached_reopen(tmp_path, real_milvus, monkeypatch):
    uri, token, admin, collections = real_milvus
    prefix = "test_ayanami_" + uuid4().hex[:12]
    config = HarnessConfig(
        tmp_path, "offline-model", embedding_api_key="unused-fixed-provider",
        embedding_base_url="https://embedding.example.test/v1", embedding_dimensions=3,
        vector_backend="milvus", milvus_uri=uri, milvus_token=token,
        milvus_collection_prefix=prefix, rerank_api_key="unused-mock-key",
        rerank_base_url="https://rerank.example.test/mock", rerank_candidates=20,
    )
    library = LibraryService(config.data_dir / "library")
    ingestion = IngestionService(library, cache_dir=tmp_path / "cache")
    provider, sent = FixedEmbeddings(), []

    def rerank(request):
        candidates = json.loads(request.content)["input"]["documents"]
        sent.append(candidates)
        return httpx.Response(200, json={"output": {"results": [
            {"index": i, "relevance_score": (i + 1) / len(candidates)}
            for i in range(len(candidates))
        ]}})

    library.start()
    ingestion.start()
    retrieval = None
    try:
        first = library.upload("operations.md", io.BytesIO(b"# Operations\n## Relays\nThe orbital shuttle uses relays."))
        second = library.upload("maintenance.txt", io.BytesIO(b"Orbital shuttle maintenance uses checkpoints."))
        outside = library.upload("plants.txt", io.BytesIO(b"Green plants require sunlight."))
        for document in (first, second):
            library.patch(document["id"], favorite=True)
        with httpx.Client(transport=httpx.MockTransport(rerank)) as client:
            retrieval = RetrievalService(config, library, ingestion, embedding_client=provider, rerank_client=client)
            retrieval.start()
            wait_until(lambda: retrieval.status()["ready_documents"] == 3, timeout=45)
            collections.add(retrieval.vectors.COLLECTION)
            assert isinstance(retrieval.vectors, MilvusLibraryIndex)
            tree = ingestion.structure(first["id"])
            assert tree["section_count"] >= 3

            def section_chunks(sections):
                return [hit for section in sections for hit in
                        (section["chunks"] + section_chunks(section["children"]))]

            chunks = section_chunks(tree["sections"])
            mappings = retrieval.document_vectors(first["id"])
            assert len(mappings) == len(chunks) > 0
            assert {mapping["chunk_id"] for mapping in mappings} == {hit["chunk_id"] for hit in chunks}
            assert all(mapping["section_id"] and mapping["backend"] == "milvus" and mapping["dimensions"] == 3 for mapping in mappings)
            before = len(provider.query_calls)
            keyword = retrieval.search("orbital", mode="keyword", favorites_only=True)
            assert len(provider.query_calls) == before
            assert keyword["reranked"] and keyword["mode"] == "keyword"
            for mode in ("vector", "hybrid"):
                result = retrieval.search("orbital", mode=mode, favorites_only=True)
                assert result["mode"] == mode and result["warning"] is None and result["reranked"]
                assert {hit["document_id"] for hit in result["hits"]} == {first["id"], second["id"]}
                assert result["hits"][0]["text"] == sent[-1][-1]
                assert all(hit["section_id"] and hit["rerank_score"] > 0 for hit in result["hits"])
            original_search = ingestion.search
            monkeypatch.setattr(ingestion, "search", lambda *args, **kwargs: pytest.fail("Strict vector mode called BM25"))
            scoped = retrieval.search("orbital", mode="vector", document_ids=[first["id"]])
            assert {hit["document_id"] for hit in scoped["hits"]} == {first["id"]}
            monkeypatch.setattr(ingestion, "search", original_search)
            calls_before = len(provider.document_calls)
            collection_name = retrieval.vectors.COLLECTION
            retrieval.close()
            retrieval = RetrievalService(config, library, ingestion, embedding_client=provider, rerank_client=client)
            retrieval.start()
            wait_until(lambda: retrieval.status()["ready_documents"] == 3 and retrieval.vectors is not None, timeout=45)
            assert retrieval.vectors.COLLECTION == collection_name
            assert len(provider.document_calls) == calls_before
            assert retrieval.search("orbital", mode="hybrid", favorites_only=True)["reranked"]
            old = chunks[-1]
            changed = library.replace(first["id"], "operations.md", io.BytesIO(b"# Operations\nUpdated orbital shuttle uses ceramic relays."))
            with pytest.raises(LibraryError):
                retrieval.read_chunk(first["id"], old["chunk_id"])
            wait_until(lambda: (vectors := retrieval.document_vectors(first["id"])) and all(
                mapping["version_id"] == changed["version_id"] for mapping in vectors
            ), timeout=45)
            assert all(hit["version_id"] == changed["version_id"] for hit in retrieval.search(
                "orbital", mode="hybrid", document_ids=[first["id"]]
            )["hits"])
            for document in (first, second, outside):
                library.delete(document["id"])
            assert retrieval.search("orbital", mode="hybrid")["hits"] == []
            wait_until(lambda: admin.query(collection_name=collection_name, filter="", output_fields=["count(*)"], consistency_level="Strong")[0]["count(*)"] == 0, timeout=45)
    finally:
        if retrieval is not None:
            if retrieval.vectors is not None:
                collections.add(retrieval.vectors.COLLECTION)
            retrieval.close()
        collections.update(name for name in admin.list_collections() if name.startswith(prefix + "_"))
        ingestion.close()
        library.close()
