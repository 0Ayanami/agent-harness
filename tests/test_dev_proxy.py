"""The optional migrated Next dev host must preserve streaming and write permissions."""

import os
import socket
import subprocess
import threading
import time
from types import SimpleNamespace

import httpx
import uvicorn

from agent_harness import HarnessConfig
from agent_harness.infrastructure.platform import stop_process_group
from agent_harness.interfaces.web.app import create_app
from agent_harness.interfaces.web.host import frontend_command
from test_services import response, text, wait_until


def test_next_development_proxy_streams_before_run_finishes(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    api_port = listener.getsockname()[1]
    with socket.socket() as web_listener:
        web_listener.bind(("127.0.0.1", 0))
        web_port = web_listener.getsockname()[1]
    entered, release = threading.Event(), threading.Event()

    def model_call(**arguments):
        if arguments["max_tokens"] == 1000:
            return response(text("[]"))
        entered.set()
        release.wait(10)
        return response(text("through Next proxy"))

    client = SimpleNamespace(messages=SimpleNamespace(create=model_call))
    origin = f"http://127.0.0.1:{web_port}"
    app = create_app(
        HarnessConfig(tmp_path, "proxy-test"), lambda: client, frontend_origins=(origin,)
    )
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=1))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    wait_until(lambda: server.started)
    command, directory = frontend_command(web_port)
    logs = (tmp_path / "next.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=directory,
        stdout=logs,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env={
            **os.environ,
            "AGENT_API_URL": f"http://127.0.0.1:{api_port}",
            "NEXT_TELEMETRY_DISABLED": "1",
        },
    )
    try:
        with httpx.Client(trust_env=False, timeout=10) as browser:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                assert process.poll() is None, (tmp_path / "next.log").read_text(encoding="utf-8")
                try:
                    health = browser.get(f"{origin}/api/agent/health")
                    if health.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError("Next development proxy did not become ready")
            headers = {"X-Agent-Client": "web", "Origin": origin}
            created = browser.post(f"{origin}/api/agent/sessions", headers=headers)
            assert created.status_code == 201, created.text
            session_id = created.json()["id"]
            assert (
                browser.post(
                    f"{origin}/api/agent/sessions/{session_id}/messages",
                    headers=headers,
                    json={"message": "proxy test"},
                ).status_code
                == 202
            )
            assert entered.wait(3)
            with browser.stream(
                "GET", f"{origin}/api/agent/sessions/{session_id}/events"
            ) as stream:
                assert "text/event-stream" in stream.headers["content-type"]
                assert stream.headers.get("content-encoding", "identity") == "identity"
                first_event = next(
                    line for line in stream.iter_lines() if line.startswith("data: ")
                )
                assert first_event and not release.is_set()
            release.set()
            wait_until(lambda: app.state.application.get(session_id).status == "idle")
            replay = browser.get(f"{origin}/api/agent/sessions/{session_id}/events?once=true")
            assert "through Next proxy" in replay.text
            assert (
                browser.post(
                    f"{origin}/api/agent/sessions",
                    headers={**headers, "Origin": "https://unrelated.invalid"},
                ).status_code
                == 403
            )
    finally:
        release.set()
        stop_process_group(process)
        process.wait(timeout=5)
        logs.close()
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
