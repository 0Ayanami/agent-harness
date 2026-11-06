"""Browser RAG with real ingestion, retrieval, persisted events and source navigation."""

import os
import socket
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import uvicorn
from agent_harness import HarnessConfig
from agent_harness.interfaces.web.app import create_app
from agent_harness.interfaces.web.host import frontend_directory
from test_services import FakeClient, response, text, tool, wait_until


def test_browser_library_question_scope_fallback_citations_and_replay(tmp_path):
    browser_api = pytest.importorskip("playwright.sync_api")
    executable = os.getenv(
        "HARNESS_BROWSER_EXECUTABLE",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    )
    if not Path(executable).is_file():
        pytest.skip("Set HARNESS_BROWSER_EXECUTABLE to an installed Chromium browser")
    output = frontend_directory() / "out"
    assert (output / "zh" / "library" / "index.html").is_file(), "Build the RAG frontend first"

    first = "紫杉规程 KITE_482 规定先核对校验点，再重放事务日志。"
    other = "紫杉规程 KITE_482 的历史附录要求先重启冷备节点。"
    answer = "先核对校验点，再重放事务日志。[S1]"
    favorite_answer = "收藏资料也要求先核对校验点。[S1]"
    model = FakeClient(
        [
            response(
                tool("search_library", {"query": "KITE_482"}, "browser-search"),
                stop_reason="tool_use",
            ),
            response(text(answer)),
            response(text(f"{favorite_answer}[S999]")),
        ]
    )
    app = create_app(
        HarnessConfig(tmp_path, "rag-browser-model"), lambda: model, frontend_directory=output
    )

    class BrowserReranker:
        def rerank(self, query, documents):
            return [(index, 1 - index / len(documents)) for index in range(len(documents))]

    app.state.retrieval.reranker = BrowserReranker()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=1))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    artifacts = Path(__file__).parents[1] / ".artifacts" / "test-results" / "browser"
    artifacts.mkdir(parents=True, exist_ok=True)
    try:
        wait_until(lambda: server.started, timeout=10)
        with browser_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=executable, headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 950})
            errors = []
            posts = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "request",
                lambda request: (
                    posts.append(request.post_data_json)
                    if request.method == "POST" and request.url.endswith("/messages")
                    else None
                ),
            )
            page.goto(origin + "/zh/library/")
            for name, body in (("恢复手册.txt", first), ("历史附录.txt", other)):
                page.get_by_test_id("library-upload").set_input_files(
                    {"name": name, "mimeType": "text/plain", "buffer": body.encode()}
                )
                row = page.get_by_test_id("library-file-row").filter(has_text=name)
                browser_api.expect(row.get_by_test_id("library-index-status")).to_have_attribute(
                    "data-status", "ready", timeout=15000
                )
            row = page.get_by_test_id("library-file-row").filter(has_text="恢复手册.txt")
            first_id = row.get_attribute("data-file-id")
            assert first_id
            row.get_by_test_id("library-favorite").click()
            browser_api.expect(row.get_by_test_id("library-favorite")).to_have_attribute(
                "aria-pressed", "true"
            )

            # The offline reranker can reorder keyword candidates after a hybrid fallback.
            page.get_by_test_id("library-search-mode").select_option("hybrid")
            with page.expect_response(
                lambda result: (
                    result.url.endswith("/library/search") and result.request.method == "POST"
                )
            ) as searched:
                page.get_by_test_id("library-search").fill("KITE_482")
            result = searched.value.json()
            assert result["mode"] == "keyword" and result["warning"]
            assert result["reranked"] is True
            browser_api.expect(page.get_by_test_id("library-retrieval-mode")).to_contain_text(
                "关键词"
            )
            browser_api.expect(page.get_by_test_id("library-rerank-status")).to_have_text("已重排")
            browser_api.expect(page.get_by_test_id("library-search-results")).to_contain_text(
                result["warning"]
            )
            mode = page.get_by_test_id("library-search-mode")
            with page.expect_response(
                lambda result: (
                    result.url.endswith("/library/search") and result.request.method == "POST"
                )
            ) as searched:
                mode.select_option("vector")
            vector_result = searched.value.json()
            assert vector_result["mode"] == "vector" and vector_result["warning"]
            assert vector_result["hits"] == [] and vector_result["reranked"] is False
            page.reload()
            browser_api.expect(mode).to_have_value("vector")
            browser_api.expect(page.get_by_test_id("library-retrieval-mode")).to_have_text(
                "向量检索"
            )
            browser_api.expect(page.get_by_test_id("library-rerank-status")).to_have_text(
                "未使用重排"
            )
            browser_api.expect(page.get_by_test_id("library-search-results")).to_contain_text(
                vector_result["warning"]
            )
            browser_api.expect(page.get_by_test_id("library-search-hit")).to_have_count(0)
            mode.select_option("hybrid")
            page.get_by_test_id("library-search-hit").filter(has_text="恢复手册.txt").click()
            preview = page.get_by_test_id("library-preview")
            browser_api.expect(preview.get_by_test_id("library-vector-status")).to_have_attribute(
                "data-status", "not_configured"
            )
            browser_api.expect(preview.get_by_test_id("library-vectorize")).to_be_disabled()
            page.get_by_test_id("library-ask").click()
            assert parse_qs(urlparse(page.url).query)["library_file"] == [first_id]
            browser_api.expect(page.get_by_test_id("rag-enabled")).to_be_checked()
            browser_api.expect(page.get_by_test_id("rag-scope")).to_have_value("selected")
            browser_api.expect(page.locator("#connection")).to_have_text("本地服务已连接")
            checked = page.get_by_test_id("rag-document-picker").get_by_role(
                "checkbox", name="恢复手册.txt"
            )
            browser_api.expect(checked).to_be_checked()
            page.get_by_test_id("rag-mode").select_option("keyword")
            checked.uncheck()
            page.get_by_test_id("message-input").fill("KITE_482 如何恢复？")
            browser_api.expect(page.get_by_test_id("send-message")).to_be_disabled()
            checked.check()
            page.get_by_test_id("send-message").click()

            assistants = page.locator(".message.assistant")
            browser_api.expect(assistants).to_have_count(1)
            browser_api.expect(assistants.first.locator(".body")).to_have_text(answer)
            source = page.get_by_test_id("rag-source")
            browser_api.expect(source).to_have_count(1)
            browser_api.expect(source).to_contain_text("恢复手册.txt")
            browser_api.expect(page.get_by_test_id("rag-retrieval-status")).to_contain_text(
                "关键词"
            )
            assert posts[0]["library"] == {
                "enabled": True,
                "document_ids": [first_id],
                "favorites_only": False,
                "mode": "keyword",
            }
            assert first in model.calls[0]["system"] and other not in model.calls[0]["system"]
            assert model.calls[1]["messages"][-1]["content"][0]["tool_use_id"] == "browser-search"

            page.reload()
            browser_api.expect(source).to_have_count(1)
            browser_api.expect(assistants).to_have_count(1)
            assert len(model.calls) == 2
            href = source.get_attribute("href")
            assert href and parse_qs(urlparse(href).query)["file"] == [first_id]
            assert parse_qs(urlparse(href).query)["chunk"]
            with page.expect_popup() as opened:
                source.click()
            original = opened.value
            browser_api.expect(original.get_by_test_id("library-matched-chunk")).to_contain_text(
                first
            )
            original.close()

            page.get_by_test_id("rag-scope").select_option("favorites")
            page.get_by_test_id("rag-mode").select_option("hybrid")
            page.get_by_test_id("message-input").fill("KITE_482 恢复规程")
            page.get_by_test_id("send-message").click()
            browser_api.expect(assistants).to_have_count(2)
            browser_api.expect(assistants.last.locator(".body")).to_have_text(favorite_answer)
            browser_api.expect(source).to_have_count(2)
            browser_api.expect(
                page.get_by_text("部分来源编号未通过校验", exact=False)
            ).to_be_visible()
            assert posts[1]["library"]["favorites_only"] is True
            assert posts[1]["library"]["document_ids"] is None
            assert posts[1]["library"]["mode"] == "hybrid"
            assert first in model.calls[2]["system"] and other not in model.calls[2]["system"]
            browser_api.expect(page.get_by_test_id("rag-retrieval-status")).to_contain_text(
                "关键词"
            )

            page.get_by_test_id("rag-mode").select_option("vector")
            page.get_by_test_id("message-input").fill("KITE_482 仅向量检索")
            page.get_by_test_id("send-message").click()
            browser_api.expect(assistants).to_have_count(3)
            retrieval = page.get_by_test_id("rag-retrieval-status")
            browser_api.expect(retrieval).to_contain_text("向量检索")
            browser_api.expect(retrieval).to_contain_text(vector_result["warning"])
            browser_api.expect(page.get_by_test_id("rag-rerank-status")).to_have_text("未使用重排")
            browser_api.expect(
                page.get_by_text("本条回答没有可验证的来源引用。", exact=True)
            ).to_be_visible()
            assert posts[2]["library"]["mode"] == "vector"
            assert len(model.calls) == 3
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(artifacts / "library-rag-mobile.png"), full_page=True)
            assert errors == []
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
