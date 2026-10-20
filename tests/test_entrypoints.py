"""Verify installed-package startup from outside the source directory."""

import os
import socket
import subprocess
import sys
import time

import httpx


def test_cli_from_another_working_directory(tmp_path):
    environment = {**os.environ, "MODEL_ID": "entry-test-model", "ANTHROPIC_API_KEY": "test-key"}
    result = subprocess.run(
        [sys.executable, "-m", "agent_harness", "cli"],
        cwd=tmp_path,
        env=environment,
        input="q\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "Agent Harness" in result.stdout


def test_api_entrypoint_serves_backend(tmp_path):
    environment = {**os.environ, "MODEL_ID": "entry-test-model", "ANTHROPIC_API_KEY": "test-key"}
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "agent_harness", "api", "--port", str(port)],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        with httpx.Client(trust_env=False, timeout=1) as client:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                assert process.poll() is None, process.communicate()[1].decode(errors="replace")
                try:
                    health = client.get(f"http://127.0.0.1:{port}/api/health")
                    break
                except httpx.TransportError:
                    time.sleep(0.1)
            else:
                raise AssertionError("Web server did not become ready")
            assert health.json()["model"] == "entry-test-model"
            assert client.get(f"http://127.0.0.1:{port}").json()["name"] == "Agent Harness API"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_default_entrypoint_serves_migrated_export(tmp_path):
    environment = {**os.environ, "MODEL_ID": "entry-test-model", "ANTHROPIC_API_KEY": "test-key"}
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "agent_harness", "--port", str(port)],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        with httpx.Client(trust_env=False, timeout=1, follow_redirects=True) as client:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                assert process.poll() is None, process.communicate()[1].decode(errors="replace")
                try:
                    health = client.get(f"http://127.0.0.1:{port}/api/agent/health")
                    break
                except httpx.TransportError:
                    time.sleep(0.1)
            else:
                raise AssertionError("Migrated web server did not become ready")
            assert health.json()["model"] == "entry-test-model"
            page = client.get(f"http://127.0.0.1:{port}/zh/")
            assert page.status_code == 200 and "Learn Claude Code" in page.text
            import re

            scripts = re.findall(r'<script[^>]+src="([^"]+)"', page.text)
            assert scripts
            assert client.get(f"http://127.0.0.1:{port}{scripts[0]}").status_code == 200
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
