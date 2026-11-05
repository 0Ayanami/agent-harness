"""Actual runtime/SQL retrieval and persisted citations; only model output is replaced."""

import io
import json

import pytest

from agent_harness import HarnessConfig
from agent_harness.application.sessions import SessionService
from agent_harness.services.ingestion import IngestionService
from agent_harness.services.library import LibraryService
from agent_harness.services.library_rag import NO_EVIDENCE
from test_services import FakeClient, response, text, tool, wait_until


class KeywordRetrieval:
    """A real lexical retrieval adapter, not a fabricated document result."""

    def __init__(self, library, ingestion):
        self.library = library
        self.ingestion = ingestion
        self.calls = []

    def search(self, query, *, document_ids=None, favorites_only=False, limit=10, mode="hybrid"):
        self.calls.append(
            {
                "query": query,
                "document_ids": document_ids,
                "favorites_only": favorites_only,
                "mode": mode,
            }
        )
        return self.ingestion.search(
            query, document_ids=document_ids, favorites_only=favorites_only, limit=limit
        )

    def read_chunk(self, document_id, chunk_id):
        return self.ingestion.read_chunk(document_id, chunk_id)


@pytest.fixture
def rag(tmp_path):
    config = HarnessConfig(tmp_path, "rag-test-model")
    library = LibraryService(config.data_dir / "library")
    ingestion = IngestionService(library, cache_dir=tmp_path / ".artifacts/cache/jieba")
    retrieval = KeywordRetrieval(library, ingestion)
    model = FakeClient()
    application = SessionService(config, lambda: model, retrieval=retrieval)
    library.start()
    ingestion.start()
    application.start()
    first = library.upload(
        "白鹭手册.txt", io.BytesIO("白鹭协议规定恢复时先检查检查点，再回放日志。".encode())
    )
    second = library.upload(
        "玄武手册.txt", io.BytesIO("玄武协议规定网络请求的超时阈值为九秒。".encode())
    )
    for document in (first, second):
        wait_until(lambda: ingestion.status(document["id"])["status"] == "ready", timeout=15)
    identifier = application.create()["id"]
    yield application, model, retrieval, library, first, second, identifier
    application.close()
    ingestion.close()
    library.close()


def ask(rag, question, documents=None, **options):
    application, _, _, _, _, _, session_id = rag
    scope = {"enabled": True, "mode": "keyword", "document_ids": documents, **options}
    application.submit(session_id, question, library=scope)
    wait_until(lambda: application.get(session_id).status == "idle", timeout=15)
    return application.snapshot(session_id)


def events(rag, kind):
    application, *_, session_id = rag
    return [
        entry["payload"]
        for entry in application.repository.events(session_id)
        if entry["kind"] == kind
    ]


def test_initial_retrieval_reaches_model_before_any_tool_and_citations_persist(rag):
    application, model, retrieval, _, first, _, _ = rag
    model.replies = [response(text("先检查检查点，再回放日志。[S1]"))]
    snapshot = ask(rag, "白鹭协议怎样恢复？", [first["id"]])
    assert retrieval.calls[0]["document_ids"] == [first["id"]]
    assert "白鹭协议规定恢复时先检查检查点，再回放日志。" in model.calls[0]["system"]
    assert "不可信" in model.calls[0]["system"]
    assert {definition["name"] for definition in model.calls[0]["tools"]} == {
        "search_library",
        "read_library_document",
    }
    assert len(model.calls) == 1  # No automatic cross-session memory extraction of documents.
    assert snapshot["history"][-1]["content"][0]["text"].endswith("[S1]")
    answer = events(rag, "library_answer")[-1]
    source = answer["citations"][0]
    assert source["id"] == "S1" and source["document_id"] == first["id"]
    assert source["version_id"] == first["version_id"]
    assert source["location"]["start_line"] == 1
    assert answer["invalid_citations"] == [] and answer["warning"] is None
    assert events(rag, "assistant")[-1]["run_id"] == answer["run_id"]
    assert events(rag, "library_retrieval")[0]["sources"][0]["text"] == source["text"]
    assert not application.get(rag[-1]).runtime.rag.active


def test_real_read_tool_uses_dispatcher_and_matching_tool_result(rag):
    _, model, retrieval, _, first, _, _ = rag
    hit = retrieval.read_chunk(
        first["id"], retrieval.ingestion.search("白鹭")["hits"][0]["chunk_id"]
    )
    model.replies = [
        response(
            tool(
                "read_library_document",
                {"document_id": first["id"], "chunk_id": hit["chunk_id"]},
                "read-original",
            ),
            stop_reason="tool_use",
        ),
        response(text("原文规定先检查检查点，再回放日志。[S1]")),
    ]
    snapshot = ask(rag, "白鹭协议怎样恢复？", [first["id"]])
    assert events(rag, "tool_started")[-1]["name"] == "read_library_document"
    result = model.calls[1]["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "read-original"
    assert json.loads(result["content"])["source"]["chunk_id"] == hit["chunk_id"]
    assert len(model.calls) == 2
    assert [message["role"] for message in snapshot["history"]] == ["user", "assistant"]
    assert events(rag, "tool_finished")[-1]["id"] == "read-original"


def test_model_abstains_when_retrieved_passage_does_not_answer_the_question(rag):
    _, model, _, _, first, _, _ = rag
    model.replies = [response(text(NO_EVIDENCE))]
    ask(rag, "白鹭协议在哪一年制定？", [first["id"]])
    assert len(model.calls) == 1
    assert NO_EVIDENCE in model.calls[0]["system"]
    answer = events(rag, "library_answer")[-1]
    assert events(rag, "library_retrieval")[0]["sources"]
    assert "未找到足够的原文证据" in answer["text"]
    assert NO_EVIDENCE not in answer["text"]
    assert answer["citations"] == []


def test_tool_parameters_cannot_expand_scope_and_invalid_citations_are_removed(rag):
    _, model, retrieval, _, first, second, _ = rag
    foreign = retrieval.ingestion.search("玄武", document_ids=[second["id"]])["hits"][0]
    model.replies = [
        response(
            tool("search_library", {"query": "玄武", "document_ids": [second["id"]]}, "narrow"),
            stop_reason="tool_use",
        ),
        response(
            tool(
                "read_library_document",
                {"document_id": second["id"], "chunk_id": foreign["chunk_id"]},
                "outside",
            ),
            stop_reason="tool_use",
        ),
        response(text("当前范围仅能根据白鹭原文回答。[S1][S999]")),
    ]
    snapshot = ask(rag, "白鹭协议", [first["id"]])
    assert retrieval.calls[-1]["document_ids"] == []
    assert json.loads(model.calls[1]["messages"][-1]["content"][0]["content"])["sources"] == []
    assert "outside" in json.loads(model.calls[2]["messages"][-1]["content"][0]["content"])["error"]
    answer = events(rag, "library_answer")[-1]
    assert answer["invalid_citations"] == ["S999"]
    assert [source["document_id"] for source in answer["citations"]] == [first["id"]]
    assert "[S999]" not in snapshot["history"][-1]["content"][0]["text"]
    assert all("[S999]" not in event["text"] for event in events(rag, "assistant"))
    assert answer["warning"]


def test_empty_scope_and_no_matches_do_not_call_model_or_expand_to_all(rag):
    _, model, retrieval, _, first, _, _ = rag
    snapshot = ask(rag, "白鹭协议", [])
    assert retrieval.calls[-1]["document_ids"] == []
    assert "未找到足够" in snapshot["history"][-1]["content"][0]["text"]
    assert events(rag, "library_answer")[-1]["citations"] == []
    ask(rag, "UNMATCHABLE847ZZ", [first["id"]])
    assert model.calls == []


@pytest.mark.parametrize("change", ["delete", "unfavorite"])
def test_source_is_revalidated_after_model_answer_before_citation_publication(rag, change):
    _, model, _, library, first, _, _ = rag
    library.patch(first["id"], favorite=True)

    def answer(**arguments):
        if change == "delete":
            library.delete(first["id"])
        else:
            library.patch(first["id"], favorite=False)
        return response(text("先检查检查点。[S1]"))

    model.messages.create = answer
    snapshot = ask(rag, "白鹭协议", [first["id"]], favorites_only=True)
    verified = events(rag, "library_answer")[-1]
    assert verified["citations"] == [] and verified["invalid_citations"] == ["S1"]
    assert "[S1]" not in snapshot["history"][-1]["content"][0]["text"]
    assert "先检查检查点" not in events(rag, "assistant")[-1]["text"]
    assert "暂不能给出可核验" in verified["text"]
    assert verified["warning"]


def test_source_namespace_resets_for_each_turn_and_replays_after_service_reopen(rag):
    application, model, retrieval, _, first, second, session_id = rag
    source = retrieval.ingestion.search("白鹭", document_ids=[first["id"]])["hits"][0]
    model.replies = [
        response(
            tool(
                "read_library_document",
                {"document_id": first["id"], "chunk_id": source["chunk_id"]},
            ),
            stop_reason="tool_use",
        ),
        response(text("白鹭恢复依赖检查点。[S1]")),
        response(text("玄武超时是九秒。[S1]")),
    ]
    ask(rag, "白鹭协议", [first["id"]])
    ask(rag, "玄武协议", [second["id"]])
    answers = events(rag, "library_answer")
    assert answers[0]["run_id"] != answers[1]["run_id"]
    assert answers[0]["citations"][0]["document_id"] == first["id"]
    assert answers[1]["citations"][0]["document_id"] == second["id"]
    assert "白鹭协议规定" in model.calls[1]["messages"][-1]["content"][0]["content"]
    assert "白鹭协议规定" not in model.calls[2]["system"]
    assert model.calls[2]["messages"] == [{"role": "user", "content": "玄武协议"}]
    reopened = SessionService(application.config, FakeClient, retrieval=retrieval)
    try:
        assert reopened.repository.events(session_id) == application.repository.events(session_id)
        assert (
            reopened.snapshot(session_id)["history"][-1]["content"][0]["text"]
            == "玄武超时是九秒。[S1]"
        )
    finally:
        reopened.close()


def test_model_output_without_a_verifiable_source_is_not_published_as_a_grounded_answer(rag):
    _, model, _, _, first, _, _ = rag
    model.replies = [response(text("不受原文支持的数字是 777。［s999］"))]
    ask(rag, "白鹭协议", [first["id"]])
    answer = events(rag, "library_answer")[-1]
    assert answer["invalid_citations"] == ["S999"]
    assert answer["citations"] == []
    assert "暂不能给出可核验" in answer["text"]
    assert all("777" not in event["text"] for event in events(rag, "assistant"))


def test_plain_chat_after_library_turn_restores_original_tools_and_no_library_prompt(rag):
    application, model, _, _, first, _, session_id = rag
    model.replies = [response(text("白鹭原文。[S1]")), response(text("普通聊天正常"))]
    ask(rag, "白鹭协议", [first["id"]])
    application.submit(session_id, "普通聊天")
    wait_until(lambda: application.get(session_id).status == "idle", timeout=10)
    plain = next(call for call in model.calls if call["messages"][-1].get("content") == "普通聊天")
    names = {definition["name"] for definition in plain["tools"]}
    assert "write_file" in names and "search_library" not in names
    assert "本轮服务端提供的证据" not in plain["system"]
    assert application.get(session_id).active_library is None


def test_retrieval_iteration_budget_produces_readable_answer_not_tool_json(rag):
    _, model, _, _, first, _, _ = rag
    model.replies = [
        response(
            tool("search_library", {"query": "白鹭"}, f"search-{index}"), stop_reason="tool_use"
        )
        for index in range(8)
    ]
    snapshot = ask(rag, "白鹭协议", [first["id"]])
    answer = events(rag, "library_answer")[-1]
    assert "预算" in answer["text"]
    assert not answer["text"].startswith("{")
    assert snapshot["history"][-1]["role"] == "assistant"
    assert len(model.calls) == 8


def test_unexpected_provider_failure_does_not_leak_exception_body(rag, monkeypatch):
    _, model, retrieval, _, first, _, _ = rag

    def fail(*args, **kwargs):
        raise RuntimeError("TOP_SECRET_PROVIDER_BODY")

    monkeypatch.setattr(retrieval, "search", fail)
    snapshot = ask(rag, "白鹭协议", [first["id"]])
    assert "暂不可用" in snapshot["history"][-1]["content"][0]["text"]
    assert "TOP_SECRET_PROVIDER_BODY" not in json.dumps(events(rag, "library_retrieval"))
    assert "TOP_SECRET_PROVIDER_BODY" not in json.dumps(events(rag, "library_answer"))
    assert model.calls == []
