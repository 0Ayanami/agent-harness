"""Offline embedding protocol and persistent Qdrant integration for the library."""

import io
import json
import threading
from contextlib import contextmanager
from dataclasses import replace

import httpx
import pytest

from agent_harness import HarnessConfig
from agent_harness.infrastructure.embeddings import BailianEmbeddings, EmbeddingError
from agent_harness.infrastructure.reranking import BailianReranker, RerankError
from agent_harness.services.ingestion import IngestionService
from agent_harness.services.library import LibraryError, LibraryService
from agent_harness.services.retrieval import RetrievalService
from test_services import wait_until


def embedding_config(workdir):
    return HarnessConfig(
        workdir,
        "offline-test-model",
        embedding_api_key="test-key-never-send",
        embedding_base_url="https://embedding.example.test/compatible-mode/v1",
        embedding_dimensions=3,
    )


RERANK_ENDPOINT = "https://rank.example.test/api/v1/services/rerank/text-rerank/text-rerank"


def rerank_config(config, **settings):
    return replace(
        config, rerank_api_key="rank-test-secret", rerank_base_url=RERANK_ENDPOINT, **settings
    )


def test_dashscope_rerank_contract_and_deterministic_order(tmp_path):
    def send(request):
        assert str(request.url) == RERANK_ENDPOINT
        assert request.headers["authorization"] == "Bearer rank-test-secret"
        assert json.loads(request.content) == {
            "model": "qwen3-rerank",
            "input": {"query": "filter", "documents": ["alpha", "bravo", "charlie"]},
            "parameters": {"top_n": 3},
        }
        return httpx.Response(
            200,
            json={
                "output": {
                    "results": [
                        {"index": 2, "relevance_score": 0.9},
                        {"index": 0, "relevance_score": 0.1},
                        {"index": 1, "relevance_score": 0.9},
                    ]
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        reranker = BailianReranker(rerank_config(embedding_config(tmp_path)), client=client)
        assert reranker.rerank("filter", ["alpha", "bravo", "charlie"]) == [
            (1, 0.9),
            (2, 0.9),
            (0, 0.1),
        ]
        assert "rank-test-secret" not in repr(reranker)


@pytest.mark.parametrize(
    "status,body,diagnostic",
    [
        (401, {"message": "provider echoed rank-test-secret"}, "HTTP 401"),
        (200, [{"message": "rank-test-secret"}], "invalid result"),
        (200, {"output": {"results": [{"index": 0, "relevance_score": 0.1}]}}, "invalid result"),
        (
            200,
            {
                "output": {
                    "results": [
                        {"index": 0, "relevance_score": 0.5},
                        {"index": 0, "relevance_score": 0.4},
                    ]
                }
            },
            "invalid result",
        ),
        (
            200,
            {
                "output": {
                    "results": [
                        {"index": 0, "relevance_score": 0.5},
                        {"index": 1, "relevance_score": True},
                    ]
                }
            },
            "invalid result",
        ),
    ],
)
def test_dashscope_rerank_rejects_untrusted_results(tmp_path, status, body, diagnostic):
    def send(_):
        return httpx.Response(status, json=body)

    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        reranker = BailianReranker(rerank_config(embedding_config(tmp_path)), client=client)
        with pytest.raises(RerankError, match=diagnostic) as failure:
            reranker.rerank("filter", ["alpha", "bravo"])
        assert "rank-test-secret" not in str(failure.value)


def test_embedding_http_batches_and_restores_response_order(tmp_path):
    config = embedding_config(tmp_path)
    calls, delays = [], []

    def send(request):
        calls.append(json.loads(request.content))
        assert request.url.path == "/compatible-mode/v1/embeddings"
        assert request.headers["authorization"] == "Bearer test-key-never-send"
        assert calls[-1]["model"] == "text-embedding-v4"
        assert calls[-1]["dimensions"] == 3
        assert calls[-1]["encoding_format"] == "float"
        if len(calls) == 1:
            return httpx.Response(429)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [3, 4, 0] if word.endswith("0") else [0, 0, 2]}
                    for index, word in reversed(list(enumerate(calls[-1]["input"])))
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        provider = BailianEmbeddings(config, client=client, sleep=delays.append)
        normalized = provider.embed([f"text-{index}" for index in range(11)])
        assert [len(call["input"]) for call in calls] == [10, 10, 1]
        assert len(delays) == 1
        assert normalized[0] == [0.6, 0.8, 0.0]
        assert normalized[1] == [0.0, 0.0, 1.0]
        assert normalized[10] == normalized[0]
        assert "test-key-never-send" not in repr(provider)
        endpoint_config = replace(
            config, embedding_base_url=config.embedding_base_url + "/embeddings"
        )
        assert BailianEmbeddings(endpoint_config, client=client).profile == provider.profile


@pytest.mark.parametrize(
    "status,body,diagnostic",
    [
        (401, {"message": "provider echoed test-key-never-send"}, "HTTP 401"),
        (200, {"data": [{"index": 0, "embedding": [1, 2]}]}, "invalid vectors"),
        (200, {"data": [{"index": 0, "embedding": [0, 0, 0]}]}, "invalid vectors"),
        (200, {"data": [{"index": 1, "embedding": [1, 2, 3]}]}, "invalid vectors"),
    ],
)
def test_embedding_invalid_responses_fail_safely_without_retries(
    tmp_path, status, body, diagnostic
):
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(status, json=body)

    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        provider = BailianEmbeddings(embedding_config(tmp_path), client=client)
        with pytest.raises(EmbeddingError, match=diagnostic) as failure:
            provider.embed(["test query"], kind="query")
    assert len(calls) == 1
    assert "test-key-never-send" not in str(failure.value)


class FixedEmbeddings:
    """Deliberately fixed topic vectors, never a network or chat call."""

    def __init__(self):
        self.document_calls = []
        self.query_calls = []
        self.fail_document = False
        self.fail_query = False
        self.block_document = False
        self.entered = threading.Event()
        self.resume = threading.Event()

    def embed(self, texts, *, kind):
        calls = self.document_calls if kind == "document" else self.query_calls
        calls.append(list(texts))
        if kind == "document" and self.block_document:
            self.entered.set()
            if not self.resume.wait(timeout=8):
                raise AssertionError("test did not resume document indexing")
        if self.fail_document and kind == "document" or self.fail_query and kind == "query":
            raise EmbeddingError("Embedding service could not be reached")
        return [
            [1.0, 0.0, 0.0]
            if any(term in value for term in ("shuttle", "spacecraft", "orbital"))
            else [0.0, 1.0, 0.0]
            for value in texts
        ]


@pytest.fixture
def library_components(tmp_path):
    config = embedding_config(tmp_path)
    library = LibraryService(config.data_dir / "library")
    ingestion = IngestionService(library, cache_dir=tmp_path / ".artifacts/cache/jieba")
    library.start()
    ingestion.start()
    try:
        yield config, library, ingestion
    finally:
        ingestion.close()
        library.close()


@contextmanager
def running_retrieval(components, provider, **options):
    retrieval = RetrievalService(*components, embedding_client=provider, **options)
    retrieval.start()
    try:
        yield retrieval
    finally:
        provider.resume.set()
        retrieval.close()
        assert not retrieval.thread or not retrieval.thread.is_alive()


def upload(library, filename, body):
    return library.upload(filename, io.BytesIO(body.encode()))


def wait_vector(library, file_id, status="ready"):
    wait_until(lambda: library.get(file_id)["vector"]["status"] == status, timeout=15)
    return library.get(file_id)["vector"]


def test_real_qdrant_hybrid_lookup_fusion_scope_and_reopen(library_components):
    config, library, _ = library_components
    provider = FixedEmbeddings()
    primary = upload(library, "operations.txt", "The shuttle uses orbital relays.")
    other = upload(library, "plants.txt", "Green plants require sunlight.")
    library.patch(primary["id"], favorite=True)
    with running_retrieval(library_components, provider) as retrieval:
        for document in (primary, other):
            assert wait_vector(library, document["id"])["chunk_count"] == 1
        keyword = retrieval.search("spacecraft", mode="keyword")
        assert keyword == {"mode": "keyword", "hits": [], "warning": None, "reranked": False}
        assert provider.query_calls == []
        hybrid = retrieval.search("spacecraft", limit=1)
        assert hybrid["mode"] == "hybrid" and hybrid["warning"] is None
        hit = hybrid["hits"][0]
        assert hit["document_id"] == primary["id"]
        assert hit["text"] == "The shuttle uses orbital relays."
        assert hit["location"] == {"start_line": 1, "end_line": 1}
        both = retrieval.search("orbital", limit=2)
        assert both["hits"][0]["chunk_id"] == hit["chunk_id"]
        assert both["hits"][0]["score"] > both["hits"][1]["score"]
        assert {
            item["document_id"] for item in retrieval.search("orbital", favorites_only=True)["hits"]
        } == {primary["id"]}
        assert retrieval.search("orbital", document_ids=[], favorites_only=True)["hits"] == []
        assert (
            retrieval.search("orbital", document_ids=[other["id"]], favorites_only=True)["hits"]
            == []
        )
        assert retrieval.status()["ready_documents"] == 2
        assert retrieval.vectors.path.is_relative_to(config.data_dir)
        calls_before = len(provider.document_calls)
        retrieval.enqueue(primary["id"])
        wait_vector(library, primary["id"])
        assert len(provider.document_calls) == calls_before
    with running_retrieval(library_components, provider) as reopened:
        result = reopened.search("spacecraft", document_ids=[primary["id"]], limit=1)
        assert result["mode"] == "hybrid"
        assert result["hits"][0]["chunk_id"] == hit["chunk_id"]
        assert len(provider.document_calls) == calls_before
        # Simulate losing the Qdrant collection while SQLite and its cache survive.
        with reopened.vectors.lock:
            reopened.vectors.client.delete_collection(reopened.vectors.COLLECTION)
    with running_retrieval(library_components, provider) as recovered:

        def index_restored():
            result = recovered.search("spacecraft", document_ids=[primary["id"]], limit=1)
            return bool(result["hits"] and result["hits"][0]["chunk_id"] == hit["chunk_id"])

        wait_until(index_restored, timeout=15)
        assert len(provider.document_calls) == calls_before


def test_retry_and_explicit_keyword_fallback(library_components):
    config, library, ingestion = library_components
    record = upload(library, "guide.txt", "An orbital shuttle carries supplies.")
    wait_until(lambda: ingestion.status(record["id"])["status"] == "ready", timeout=15)
    unconfigured = replace(config, embedding_api_key=None)
    provider = FixedEmbeddings()
    with running_retrieval((unconfigured, library, ingestion), provider) as retrieval:
        assert library.get(record["id"])["vector"]["status"] == "not_configured"
        result = retrieval.search("orbital")
        assert result["mode"] == "keyword" and "not configured" in result["warning"]
        assert result["hits"][0]["document_id"] == record["id"]
        strict = retrieval.search("orbital", mode="vector")
        assert strict["mode"] == "vector" and "not configured" in strict["warning"]
        assert strict["hits"] == []
        assert not provider.document_calls and not provider.query_calls
        assert not (config.data_dir / "library" / "vectors").exists()
    provider.fail_document = True
    with running_retrieval(library_components, provider) as retrieval:
        failed = wait_vector(library, record["id"], "failed")
        assert "could not be reached" in failed["error"]
        assert retrieval.search("orbital")["mode"] == "keyword"
        provider.fail_document = False
        retry = retrieval.enqueue(record["id"])
        assert retry["status"] == "queued"
        wait_vector(library, record["id"])
        assert len(provider.document_calls) == 2
        provider.fail_query = True
        result = retrieval.search("orbital")
        assert result["mode"] == "keyword" and "temporarily unavailable" in result["warning"]
        assert result["hits"][0]["document_id"] == record["id"]
        strict = retrieval.search("orbital", mode="vector")
        assert strict["mode"] == "vector" and "temporarily unavailable" in strict["warning"]
        assert strict["hits"] == []


def test_model_reranks_before_top_k_in_each_scoped_retrieval_mode(library_components, monkeypatch):
    config, library, ingestion = library_components
    config = rerank_config(config, rerank_candidates=2)
    provider = FixedEmbeddings()
    documents = [
        upload(library, f"passage-{index}.txt", f"Orbital shuttle maintenance passage {index}.")
        for index in range(4)
    ]
    for document in documents[:3]:
        library.patch(document["id"], favorite=True)
    permitted = {document["id"] for document in documents[:3]}
    sent = []
    fail = False

    def send(request):
        candidates = json.loads(request.content)["input"]["documents"]
        sent.append(candidates)
        if fail:
            raise httpx.ReadTimeout("provider diagnostic rank-test-secret", request=request)
        return httpx.Response(
            200,
            json={
                "output": {
                    "results": [
                        {"index": 1, "relevance_score": 0.9},
                        {"index": 0, "relevance_score": 0.1},
                    ]
                }
            },
        )

    with (
        httpx.Client(transport=httpx.MockTransport(send)) as client,
        running_retrieval(
            (config, library, ingestion), provider, rerank_client=client
        ) as retrieval,
    ):
        for document in documents:
            wait_vector(library, document["id"])
        assert retrieval.status()["rerank_configured"]
        assert retrieval.status()["rerank_model"] == "qwen3-rerank"
        for mode in ("keyword", "hybrid", "vector"):
            result = retrieval.search("shuttle", favorites_only=True, mode=mode, limit=1)
            assert result["mode"] == mode and result["reranked"] and not result["warning"]
            assert len(sent[-1]) == 2 and all("passage 3" not in text for text in sent[-1])
            [hit] = result["hits"]
            assert hit["text"] == sent[-1][1] and hit["rerank_score"] == 0.9
            assert hit["document_id"] in permitted

        fail = True
        fallback = retrieval.search("shuttle", favorites_only=True, mode="keyword", limit=3)
        assert not fallback["reranked"] and "Initial ranking is shown" in fallback["warning"]
        assert "rank-test-secret" not in fallback["warning"]
        coarse = ingestion.search("shuttle", favorites_only=True, limit=3)["hits"]
        assert [hit["chunk_id"] for hit in fallback["hits"]] == [hit["chunk_id"] for hit in coarse]
        fail = False
        bounded = retrieval.search("shuttle", favorites_only=True, mode="keyword", limit=3)
        assert len(bounded["hits"]) == 3
        assert [hit.get("rerank_score") for hit in bounded["hits"]] == [0.9, 0.1, None]

        def fail_if_lexical(*args, **kwargs):
            raise AssertionError("Vector mode must not run BM25")

        monkeypatch.setattr(ingestion, "search", fail_if_lexical)
        before = len(sent)
        single = retrieval.search("shuttle", document_ids=[documents[0]["id"]], mode="vector")
        assert single["hits"][0]["document_id"] == documents[0]["id"]
        assert not single["reranked"] and len(sent) == before
        assert retrieval.search("shuttle", document_ids=[], mode="vector")["hits"] == []


def test_rerank_revalidates_scope_and_active_version_after_network(library_components):
    config, library, ingestion = library_components
    config = rerank_config(replace(config, embedding_api_key=None))
    provider = FixedEmbeddings()
    changed = upload(library, "maintenance.txt", "Original orbital mission checklist.")
    removed = upload(library, "cancelled.txt", "Cancelled orbital mission checklist.")
    kept = upload(library, "safety.txt", "Safe orbital mission checklist.")
    documents = (changed, removed, kept)
    for document in documents:
        library.patch(document["id"], favorite=True)
        wait_until(lambda: ingestion.status(document["id"])["status"] == "ready", timeout=15)

    def send(request):
        candidates = json.loads(request.content)["input"]["documents"]
        assert len(candidates) == 3
        library.replace(
            changed["id"], "maintenance.txt", io.BytesIO(b"Updated orbital mission checklist.")
        )
        library.patch(removed["id"], favorite=False)
        return httpx.Response(
            200,
            json={
                "output": {
                    "results": [
                        {"index": index, "relevance_score": 0.7} for index in range(len(candidates))
                    ]
                }
            },
        )

    with (
        httpx.Client(transport=httpx.MockTransport(send)) as client,
        running_retrieval(
            (config, library, ingestion), provider, rerank_client=client
        ) as retrieval,
    ):
        result = retrieval.search("orbital", favorites_only=True, mode="keyword")
        assert result["reranked"]
        [remaining] = result["hits"]
        assert remaining["document_id"] == kept["id"]


def test_replace_and_delete_never_return_old_chunk_or_in_flight_vectors(library_components):
    _, library, _ = library_components
    provider = FixedEmbeddings()
    document = upload(library, "operations.txt", "The orbital shuttle uses a copper antenna.")
    with running_retrieval(library_components, provider) as retrieval:
        wait_vector(library, document["id"])
        old = retrieval.search("orbital", document_ids=[document["id"]])["hits"][0]
        assert "copper antenna" in old["text"]
        replaced = library.replace(
            document["id"],
            "operations.txt",
            io.BytesIO(b"The orbital shuttle uses a ceramic antenna."),
        )
        with pytest.raises(LibraryError) as missing:
            retrieval.read_chunk(document["id"], old["chunk_id"])
        assert missing.value.status_code == 404
        wait_vector(library, document["id"])
        updated = retrieval.search("spacecraft", document_ids=[document["id"]])["hits"][0]
        assert updated["version_id"] == replaced["version_id"] != old["version_id"]
        assert "ceramic antenna" in updated["text"]

        provider.block_document = True
        in_flight = upload(
            library, "disposable.txt", "A shuttle with an obsolete tungsten antenna."
        )
        assert provider.entered.wait(timeout=15)
        library.delete(in_flight["id"])
        library.delete(document["id"])
        provider.resume.set()
        assert retrieval.search("spacecraft")["hits"] == []

        def vectors_removed():
            with retrieval.vectors.lock:
                return retrieval.vectors.client.count(retrieval.vectors.COLLECTION).count == 0

        wait_until(vectors_removed, timeout=15)
        assert retrieval.search("orbital")["hits"] == []
        assert retrieval.status()["ready_documents"] == 0
