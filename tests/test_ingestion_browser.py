"""Real browser ingestion states, body search and original-source navigation."""

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
from test_ingestion import blank_or_encrypted_pdf
from test_services import FakeClient, wait_until


def test_browser_ingestion_body_search_source_reload_and_retry(tmp_path):
    browser_api = pytest.importorskip("playwright.sync_api")
    executable = os.getenv(
        "HARNESS_BROWSER_EXECUTABLE",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    )
    if not Path(executable).is_file():
        pytest.skip("Set HARNESS_BROWSER_EXECUTABLE to an installed Chromium browser")
    output = frontend_directory() / "out"
    assert (output / "zh" / "library" / "index.html").is_file(), (
        "Build the ingestion frontend before running this browser test"
    )
    model = FakeClient()
    app = create_app(
        HarnessConfig(tmp_path, "ingestion-browser-model"), lambda: model, frontend_directory=output
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=1))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    wait_until(lambda: server.started, timeout=10)
    artifacts = Path(__file__).parents[1] / ".artifacts" / "test-results" / "browser"
    artifacts.mkdir(parents=True, exist_ok=True)
    errors = []
    try:
        with browser_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=executable, headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 950}, device_scale_factor=1)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin + "/zh/library/")
            first_body = (
                "# 容灾手册\n\n## 检查点恢复\n\n虹桥容灾规程要求先恢复检查点，再重放事务日志。"
            )
            second_body = "虹桥容灾规程的审计附录要求记录恢复结果，与第一份资料内容不同。"
            document_ids = {}
            for name, content in (("部署笔记.md", first_body), ("运行记录.txt", second_body)):
                page.get_by_test_id("library-upload").set_input_files(
                    {"name": name, "mimeType": "text/plain", "buffer": content.encode()}
                )
                row = page.get_by_test_id("library-file-row").filter(has_text=name)
                browser_api.expect(row).to_be_visible()
                browser_api.expect(row.get_by_test_id("library-index-status")).to_have_attribute(
                    "data-status", "ready", timeout=15000
                )
                document_ids[name] = row.get_attribute("data-file-id")
                assert document_ids[name]

            first_row = page.get_by_test_id("library-file-row").filter(has_text="部署笔记.md")
            identifier = first_row.get_attribute("data-file-id")
            assert identifier
            first_row.get_by_test_id("library-favorite").click()
            browser_api.expect(first_row.get_by_test_id("library-favorite")).to_have_attribute(
                "aria-pressed", "true"
            )

            mode = page.get_by_test_id("library-search-mode")
            mode.select_option("content")
            page.get_by_test_id("library-search").fill("虹桥容灾规程")
            hits = page.get_by_test_id("library-search-hit")
            for document_id in document_ids.values():
                browser_api.expect(
                    page.locator(
                        f'[data-testid="library-search-hit"][data-document-id="{document_id}"]'
                    ).first
                ).to_be_visible()
            assert set(
                hits.evaluate_all("nodes => nodes.map(node => node.dataset.documentId)")
            ) == set(document_ids.values())
            browser_api.expect(page.get_by_test_id("library-search-results")).to_contain_text(
                "虹桥容灾规程"
            )
            # Results are chunks: one document may contribute a title and a body hit.
            # Open the actual supporting paragraph rather than assuming one hit per file.
            first_hit = hits.filter(has_text="部署笔记.md").filter(
                has_text="先恢复检查点，再重放事务日志"
            )
            browser_api.expect(first_hit).to_have_count(1)
            first_hit.click()
            browser_api.expect(page.get_by_test_id("library-matched-chunk")).to_contain_text(
                "虹桥容灾规程"
            )
            assert parse_qs(urlparse(page.url).query)["file"] == [identifier]
            assert parse_qs(urlparse(page.url).query)["chunk"]
            page.reload()
            browser_api.expect(page.get_by_test_id("library-matched-chunk")).to_contain_text(
                "先恢复检查点，再重放事务日志"
            )
            browser_api.expect(page.get_by_test_id("library-preview-text")).to_contain_text(
                "检查点恢复"
            )
            page.screenshot(path=str(artifacts / "ingestion-source-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            browser_api.expect(page.get_by_test_id("library-matched-chunk")).to_be_visible()
            page.screenshot(path=str(artifacts / "ingestion-source-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1440, "height": 950})
            page.get_by_test_id("library-close").click()

            page.get_by_test_id("library-filter-favorites").click()
            excluded_hits = page.locator(
                '[data-testid="library-search-hit"]'
                f'[data-document-id="{document_ids["运行记录.txt"]}"]'
            )
            browser_api.expect(excluded_hits).to_have_count(0)
            browser_api.expect(first_hit).to_have_count(1)
            assert set(
                hits.evaluate_all("nodes => nodes.map(node => node.dataset.documentId)")
            ) == {identifier}
            page.reload()
            browser_api.expect(page.get_by_test_id("library-search-mode")).to_have_value("content")
            browser_api.expect(page.get_by_test_id("library-search")).to_have_value("虹桥容灾规程")
            browser_api.expect(excluded_hits).to_have_count(0)
            browser_api.expect(first_hit).to_have_count(1)
            assert set(
                hits.evaluate_all("nodes => nodes.map(node => node.dataset.documentId)")
            ) == {identifier}

            # A saved image-only PDF needs OCR; it must not pretend its text is ready.
            page.get_by_test_id("library-filter-all").click()
            page.get_by_test_id("library-search-mode").select_option("filename")
            page.get_by_test_id("library-search").fill("")
            page.get_by_test_id("library-upload").set_input_files(
                {
                    "name": "扫描图片.pdf",
                    "mimeType": "application/pdf",
                    "buffer": blank_or_encrypted_pdf(),
                }
            )
            blank_row = page.get_by_test_id("library-file-row").filter(has_text="扫描图片.pdf")
            browser_api.expect(blank_row.get_by_test_id("library-index-status")).to_have_attribute(
                "data-status", "needs_ocr", timeout=15000
            )
            blank_row.get_by_test_id("library-open").click()
            preview = page.get_by_test_id("library-preview")
            retry = preview.get_by_test_id("library-reindex")
            browser_api.expect(retry).to_be_visible()
            with page.expect_response(
                lambda response: (
                    response.url.endswith("/reindex") and response.request.method == "POST"
                )
            ) as pending:
                retry.click()
            assert pending.value.status == 202
            browser_api.expect(preview.get_by_test_id("library-index-status")).to_have_attribute(
                "data-status", "needs_ocr", timeout=15000
            )
            assert errors == []
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
    assert model.calls == []
