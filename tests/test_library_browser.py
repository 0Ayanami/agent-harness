"""Real-browser library management against the built static site and real storage."""

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
from test_library import PDF
from test_services import FakeClient, wait_until


def test_browser_library_crud_preview_reload_and_mobile(tmp_path):
    browser_api = pytest.importorskip("playwright.sync_api")
    executable = os.getenv(
        "HARNESS_BROWSER_EXECUTABLE",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    )
    if not Path(executable).is_file():
        pytest.skip("Set HARNESS_BROWSER_EXECUTABLE to an installed Chromium browser")

    output = frontend_directory() / "out"
    assert (output / "zh" / "library" / "index.html").is_file(), (
        "Build the library frontend before running this browser test"
    )
    model = FakeClient()
    app = create_app(
        HarnessConfig(tmp_path, "library-browser-model"), lambda: model, frontend_directory=output
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
            page.goto(origin + "/zh/")
            page.get_by_role("link", name="资料库", exact=True).click()
            browser_api.expect(page.get_by_test_id("library-page")).to_be_visible()

            note = "# 知识库笔记\n\n上传文件后，先保存原件，再建立检索索引。"
            page.get_by_test_id("library-upload").set_input_files(
                {"name": "知识库笔记.txt", "mimeType": "text/plain", "buffer": note.encode()}
            )
            row = page.get_by_test_id("library-file-row")
            browser_api.expect(row).to_have_count(1)
            browser_api.expect(row).to_contain_text("知识库笔记.txt")
            browser_api.expect(row).to_contain_text("已保存")
            identifier = row.get_attribute("data-file-id")
            assert identifier
            row.get_by_test_id("library-favorite").click()
            browser_api.expect(row.get_by_test_id("library-favorite")).to_have_attribute(
                "aria-pressed", "true"
            )
            page.get_by_test_id("library-filter-favorites").click()
            browser_api.expect(row).to_have_count(1)
            search = page.get_by_test_id("library-search")
            search.fill("没有这个文件")
            browser_api.expect(row).to_have_count(0)
            search.fill("知识")
            browser_api.expect(row).to_have_count(1)
            row.get_by_test_id("library-open").click()
            browser_api.expect(page.get_by_test_id("library-preview-text")).to_have_text(note)
            assert parse_qs(urlparse(page.url).query)["file"] == [identifier]
            with page.expect_download() as pending_download:
                page.get_by_test_id("library-download").click()
            download = pending_download.value
            destination = tmp_path / "downloaded-note.txt"
            download.save_as(destination)
            assert destination.read_text(encoding="utf-8") == note
            page.get_by_test_id("library-close").click()

            search.fill("")
            browser_api.expect(row).to_have_count(1)
            row.get_by_test_id("library-rename").click()
            page.get_by_test_id("library-rename-input").fill("面试资料.txt")
            page.get_by_test_id("library-rename-save").click()
            browser_api.expect(row).to_contain_text("面试资料.txt")
            row.get_by_test_id("library-open").click()
            updated = "替换后的资料仍然保留收藏，并且刷新后可以继续阅读。"
            page.get_by_test_id("library-replace").set_input_files(
                {"name": "更新资料.txt", "mimeType": "text/plain", "buffer": updated.encode()}
            )
            browser_api.expect(page.get_by_test_id("library-preview-text")).to_have_text(updated)
            page.reload()
            browser_api.expect(page.get_by_test_id("library-preview-text")).to_have_text(updated)
            browser_api.expect(page.get_by_test_id("library-file-row")).to_contain_text(
                "面试资料.txt"
            )
            browser_api.expect(
                page.get_by_test_id("library-file-row").get_by_test_id("library-favorite")
            ).to_have_attribute("aria-pressed", "true")
            page.screenshot(path=str(artifacts / "library-desktop.png"), full_page=True)

            page.get_by_test_id("library-close").click()
            page.get_by_test_id("library-filter-all").click()
            page.get_by_test_id("library-upload").set_input_files(
                {"name": "系统设计.pdf", "mimeType": "application/pdf", "buffer": PDF}
            )
            pdf_row = page.get_by_test_id("library-file-row").filter(has_text="系统设计.pdf")
            browser_api.expect(pdf_row).to_be_visible()
            pdf_id = pdf_row.get_attribute("data-file-id")
            assert pdf_id
            pdf_metadata = page.request.get(f"{origin}/api/agent/library/files/{pdf_id}").json()
            pdf_row.get_by_test_id("library-open").click()
            frame = page.get_by_test_id("library-preview-pdf")
            browser_api.expect(frame).to_be_visible()
            frame_url = urlparse(frame.get_attribute("src"))
            assert frame_url.netloc in ("", urlparse(origin).netloc)
            assert frame_url.path.endswith("/content")
            assert parse_qs(frame_url.query)["download"] == ["false"]
            assert parse_qs(frame_url.query)["version_id"] == [pdf_metadata["version_id"]]
            download = page.get_by_test_id("library-download")
            browser_api.expect(download).to_be_visible()
            assert parse_qs(urlparse(download.get_attribute("href")).query) == {
                "download": ["true"]
            }

            page.set_viewport_size({"width": 390, "height": 844})
            browser_api.expect(page.get_by_test_id("library-preview")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(artifacts / "library-mobile.png"), full_page=True)
            page.get_by_test_id("library-close").click()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.set_viewport_size({"width": 1440, "height": 950})

            # Language switching must preserve filtering and the persisted theme.
            search = page.get_by_test_id("library-search")
            search.fill("面试")
            row = page.get_by_test_id("library-file-row")
            browser_api.expect(row).to_have_count(1)
            page.get_by_role("button", name="切换主题", exact=True).click()
            browser_api.expect(page.locator("html")).to_have_class("dark")
            page.get_by_role("button", name="EN", exact=True).click()
            browser_api.expect(page.get_by_test_id("library-page")).to_be_visible()
            browser_api.expect(page.get_by_test_id("library-search")).to_have_value("面试")
            browser_api.expect(page.locator("html")).to_have_class("dark")
            browser_api.expect(page.get_by_test_id("library-file-row")).to_have_count(1)
            page.get_by_test_id("library-file-row").get_by_test_id("library-delete").click()
            page.get_by_test_id("library-delete-confirm").click()
            browser_api.expect(page.get_by_test_id("library-file-row")).to_have_count(0)
            page.reload()
            browser_api.expect(page.get_by_test_id("library-file-row")).to_have_count(0)
            assert page.request.get(f"{origin}/api/agent/library/files/{identifier}").status == 404
            assert errors == []
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
    assert model.calls == []
