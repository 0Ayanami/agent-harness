"""Optional real-browser check against the full HTTP application and worker stack."""

import os
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn

from agent_harness import HarnessConfig
from agent_harness.interfaces.web.app import create_app
from agent_harness.interfaces.web.host import frontend_directory
from test_services import response, text, tool, wait_until


def test_browser_chat_approval_reload_and_responsive_layout(tmp_path):
    browser_api = pytest.importorskip("playwright.sync_api")
    executable = os.getenv(
        "HARNESS_BROWSER_EXECUTABLE",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    )
    if not Path(executable).exists():
        pytest.skip("Set HARNESS_BROWSER_EXECUTABLE to an installed Chromium browser")

    class BrowserModel:
        def __init__(self):
            self.messages = SimpleNamespace(create=self.create)

        def create(self, **arguments):
            if arguments["max_tokens"] == 1000:
                return response(text("[]"))
            if isinstance(arguments["messages"][-1]["content"], list):
                return response(
                    text("验证完成。命令输出为 browser-ready，任务已经保存在当前会话。")
                )
            return response(
                tool("create_task", {"subject": "网页联调验证"}, "create-1"),
                tool("bash", {"command": "echo browser-ready"}, "bash-1"),
                stop_reason="tool_use",
            )

    output = frontend_directory() / "out"
    assert (output / "zh" / "index.html").exists(), (
        "Build the migrated frontend before running browser tests"
    )
    app = create_app(
        HarnessConfig(tmp_path, "browser-test-model"), BrowserModel, frontend_directory=output
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=1))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    wait_until(lambda: server.started, timeout=10)
    artifacts = Path(__file__).parents[1] / ".test-artifacts"
    artifacts.mkdir(exist_ok=True)
    errors = []
    try:
        with browser_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=executable, headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 950}, device_scale_factor=1)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{port}")
            browser_api.expect(page.locator("#connection")).to_have_text("本地服务已连接")
            browser_api.expect(page.locator("h1")).to_be_visible()
            browser_api.expect(
                page.get_by_role("link", name="Learn Claude Code", exact=True)
            ).to_be_visible()
            page.screenshot(path=str(artifacts / "web-desktop.png"), full_page=True)
            page.locator("#message").fill("运行网页联调")
            page.locator("#send").click()
            browser_api.expect(page.locator(".approval")).to_be_visible()
            page.reload()
            browser_api.expect(page.locator(".approval")).to_be_visible()
            page.get_by_role("button", name="允许执行").click()
            browser_api.expect(page.locator(".message.assistant .body")).to_contain_text(
                "browser-ready"
            )
            browser_api.expect(page.locator("#runtime-status")).to_have_text("准备就绪")
            browser_api.expect(page.locator("#tasks")).to_contain_text("网页联调验证")
            browser_api.expect(page.locator("#tool-count")).to_have_text("2")
            page.screenshot(path=str(artifacts / "web-conversation.png"), full_page=True)
            page.reload()
            browser_api.expect(page.locator(".message.assistant .body")).to_contain_text(
                "browser-ready"
            )
            page.set_viewport_size({"width": 390, "height": 844})
            browser_api.expect(page.locator("#composer")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(artifacts / "web-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1440, "height": 950})

            first_id = page.evaluate("localStorage.getItem('harness.session')")
            second_id = page.request.post(
                f"http://127.0.0.1:{port}/api/agent/sessions",
                headers={"X-Agent-Client": "web"},
            ).json()["id"]
            page.reload()
            browser_api.expect(page.locator("#connection")).to_have_text("本地服务已连接")
            held = []

            def delay_send_response(route):
                held.append((route, route.fetch()))

            page.route(f"**/api/agent/sessions/{first_id}/messages", delay_send_response)
            page.locator("#message").fill("延迟响应验证")
            page.locator("#send").click()
            page.locator(f'#sessions button[data-session-id="{second_id}"]').click()
            browser_api.expect(page.locator("#message")).to_be_enabled()
            page.locator("#message").fill("keep this draft")
            assert held
            held[0][0].fulfill(response=held[0][1])
            browser_api.expect(page.locator("#message")).to_have_value("keep this draft")
            page.request.post(
                f"http://127.0.0.1:{port}/api/agent/sessions/{first_id}/cancel",
                headers={"X-Agent-Client": "web"},
            )
            page.locator(f'#sessions button[data-session-id="{first_id}"]').click()
            browser_api.expect(page.locator("#runtime-status")).to_have_text("准备就绪")

            page.get_by_role("button", name="切换主题").click()
            browser_api.expect(page.locator("html")).to_have_class("dark")
            page.get_by_role("button", name="EN", exact=True).click()
            browser_api.expect(page.locator("h1")).to_have_text("Agent Workbench")
            browser_api.expect(page.locator(".message.assistant .body")).to_contain_text(
                "browser-ready"
            )
            assert errors == []
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
