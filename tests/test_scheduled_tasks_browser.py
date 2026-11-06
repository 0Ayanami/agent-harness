"""Exercise the real task drawer, scheduler, and scheduled event replay in Chromium."""

import os
import socket
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import uvicorn
from agent_harness import HarnessConfig
from agent_harness.interfaces.web.app import create_app
from agent_harness.interfaces.web.host import frontend_directory
from test_services import FakeClient, response, text, wait_until


def test_browser_schedules_create_navigate_cancel_and_replay(tmp_path):
    browser_api = pytest.importorskip("playwright.sync_api")
    executable = os.getenv(
        "HARNESS_BROWSER_EXECUTABLE",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    )
    if not Path(executable).is_file():
        pytest.skip("Set HARNESS_BROWSER_EXECUTABLE to an installed Chromium browser")

    output = frontend_directory() / "out"
    assert (output / "zh" / "index.html").is_file(), "Build the frontend before browser tests"
    model = FakeClient([response(text("scheduled-browser-result"))])
    app = create_app(
        HarnessConfig(tmp_path, "scheduled-browser-model"), lambda: model, frontend_directory=output
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=1))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        wait_until(lambda: server.started, timeout=10)
        with browser_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=executable, headless=True)
            page = browser.new_page(base_url=origin, viewport={"width": 1440, "height": 950})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto("/zh/")
            browser_api.expect(page.get_by_test_id("connection")).to_have_text("本地服务已连接")
            first_id = page.evaluate("localStorage.getItem('harness.session')")
            browser_api.expect(page.locator("#message")).to_be_enabled()

            weekly_prompt = "每周整理资料库索引"
            page.locator("#message").fill(weekly_prompt)
            page.get_by_test_id("scheduled-tasks-compose").click()
            drawer = page.get_by_test_id("scheduled-tasks-dialog")
            task_prompt = drawer.get_by_test_id("scheduled-task-prompt")
            browser_api.expect(task_prompt).to_have_value(weekly_prompt)
            drawer.get_by_test_id("scheduled-task-frequency").select_option("weekly")
            server_clock = page.request.get("/api/agent/scheduled-tasks").json()["server_time"]
            tomorrow = datetime.fromisoformat(server_clock) + timedelta(days=1)
            weekday = (tomorrow.weekday() + 1) % 7
            drawer.get_by_test_id("scheduled-task-weekday").select_option(str(weekday))
            drawer.get_by_test_id("scheduled-task-time").fill("09:20")
            drawer.get_by_test_id("scheduled-task-create").click()
            row = drawer.get_by_test_id("scheduled-task-row").filter(has_text=weekly_prompt)
            browser_api.expect(row).to_be_visible()
            browser_api.expect(row).to_contain_text("重启后保留")
            browser_api.expect(row.locator("time")).to_contain_text("09:20")
            item = page.request.get("/api/agent/scheduled-tasks").json()["items"][0]
            assert item["session_id"] == first_id and item["cron"] == f"20 9 * * {weekday}"
            assert item["durable"] and item["recurring"]
            drawer.get_by_role("button", name="关闭").click()
            browser_api.expect(page.locator("#message")).to_have_value(weekly_prompt)

            page.locator("#new-session").click()
            browser_api.expect(page.locator("#session-count")).to_have_text("2")
            browser_api.expect(page.locator("#message")).to_be_enabled()
            assert page.evaluate("localStorage.getItem('harness.session')") != first_id
            page.get_by_test_id("scheduled-tasks-open").click()
            row.get_by_test_id("scheduled-task-open-session").click()
            browser_api.expect(drawer).to_have_count(0)
            active = page.locator(f'#sessions [data-session-id="{first_id}"]')
            browser_api.expect(active).to_have_attribute("aria-current", "page")

            page.get_by_test_id("scheduled-tasks-open").click()
            row.get_by_test_id("scheduled-task-cancel").click()
            browser_api.expect(
                drawer.get_by_test_id("scheduled-task-confirm-cancel")
            ).to_be_visible()
            drawer.get_by_role("button", name="保留任务").click()
            browser_api.expect(row).to_be_visible()
            row.get_by_test_id("scheduled-task-cancel").click()
            drawer.get_by_test_id("scheduled-task-confirm-cancel").click()
            browser_api.expect(row).to_have_count(0)
            browser_api.expect(drawer).to_contain_text("项目中还没有定时任务。")
            assert page.request.get("/api/agent/scheduled-tasks").json()["items"] == []

            page.set_viewport_size({"width": 390, "height": 844})
            drawer.get_by_test_id("scheduled-task-new").click()
            prompt = "输出定时任务执行记录"
            task_prompt.fill(prompt)
            drawer.get_by_test_id("scheduled-task-frequency").select_option("custom")
            cron = drawer.get_by_test_id("scheduled-task-cron")
            cron.fill("not a cron")
            drawer.get_by_test_id("scheduled-task-create").click()
            browser_api.expect(drawer.get_by_role("alert")).to_contain_text("Expected 5 fields")
            cron.fill("* * * * *")
            drawer.get_by_test_id("scheduled-task-once").check()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            drawer.get_by_test_id("scheduled-task-create").click()
            browser_api.expect(drawer).to_contain_text("已创建定时任务。")
            drawer.get_by_role("button", name="关闭").click()

            marker = page.locator("#messages .message.system_event").filter(has_text="定时任务触发")
            answer = page.locator("#messages .message.assistant .body")
            browser_api.expect(marker).to_contain_text(prompt)
            browser_api.expect(answer).to_have_text("scheduled-browser-result")
            page.reload()
            browser_api.expect(marker).to_have_count(1)
            browser_api.expect(answer).to_have_text("scheduled-browser-result")
            assert sum(call["max_tokens"] != 1000 for call in model.calls) == 1
            assert errors == []
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
