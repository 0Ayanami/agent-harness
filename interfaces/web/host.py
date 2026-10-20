"""Serve the migrated export, or launch Next alongside the API for development."""

import os
import shutil
import subprocess
from pathlib import Path

from ...infrastructure.platform import stop_process_group


def frontend_directory() -> Path:
    return Path(__file__).resolve().parents[2] / "frontend"


def frontend_command(port: int) -> tuple[list[str], Path]:
    directory = frontend_directory()
    node = shutil.which("node")
    executable = directory / "node_modules" / "next" / "dist" / "bin" / "next"
    if node is None:
        raise RuntimeError("Node.js is required by the migrated web frontend")
    if not executable.is_file():
        raise RuntimeError(
            "Install frontend dependencies first: npm --prefix agent_harness/frontend ci"
        )
    return [
        node,
        str(executable),
        "dev",
        "--hostname",
        "127.0.0.1",
        "--port",
        str(port),
    ], directory


def serve_web(config, client_factory, *, api_port=8000, web_port=3000, development=False):
    import uvicorn
    from .app import create_app

    if not development:
        output = frontend_directory() / "out"
        if not (output / "index.html").is_file():
            raise RuntimeError(
                "Build the migrated frontend first: npm --prefix agent_harness/frontend ci; npm --prefix agent_harness/frontend run build"
            )
        print(f"Agent workspace: http://127.0.0.1:{api_port}", flush=True)
        uvicorn.run(
            create_app(config, client_factory, frontend_directory=output),
            host="127.0.0.1",
            port=api_port,
            timeout_graceful_shutdown=3,
        )
        return

    command, directory = frontend_command(web_port)
    environment = {
        **os.environ,
        "AGENT_API_URL": f"http://127.0.0.1:{api_port}",
        "NEXT_TELEMETRY_DISABLED": "1",
    }
    process = subprocess.Popen(command, cwd=directory, env=environment, start_new_session=True)
    try:
        print(f"Agent workspace: http://127.0.0.1:{web_port}", flush=True)
        origins = (f"http://127.0.0.1:{web_port}", f"http://localhost:{web_port}")
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(config, client_factory, frontend_origins=origins),
                host="127.0.0.1",
                port=api_port,
                timeout_graceful_shutdown=3,
            )
        )
        import threading

        def watch_frontend():
            process.wait()
            server.should_exit = True

        threading.Thread(target=watch_frontend, daemon=True).start()
        server.run()
        if process.poll() is not None:
            raise RuntimeError(f"Next frontend exited with code {process.returncode}")
    finally:
        stop_process_group(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
