"""Docker entrypoint for the local Cron scheduler and its private control API."""

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from agent_harness.infrastructure.cron_store import CronStore


def serve(store):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, data):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply(
                200 if self.path == "/health" and time.monotonic() - last_scan < 5 else 503, {}
            )

        def do_POST(self):
            try:
                host = urlsplit("//" + self.headers.get("Host", "")).hostname
            except ValueError:
                host = None
            if (
                self.path != "/cron"
                or host not in {"127.0.0.1", "localhost", "::1"}
                or self.headers.get("Origin")
                or self.headers.get("X-Agent-Harness") != "cron"
                or self.headers.get("Content-Type") != "application/json"
            ):
                self.reply(403, {})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 8 * 1024 * 1024:
                    raise ValueError("Invalid Cron request size")
                self.connection.settimeout(5)
                self.reply(200, {"result": store.rpc(**json.loads(self.rfile.read(size)))})
            except (TypeError, KeyError, ValueError) as error:
                self.reply(422, {"error": str(error)})
            except OSError:
                return
            except Exception:
                logging.exception("Cron state operation failed")
                self.reply(503, {})

        def log_message(self, *_):
            pass

    def scan():
        nonlocal last_scan
        while not stopped.is_set():
            try:
                store.tick()
                last_scan = time.monotonic()
            except Exception:
                logging.exception("Cron scan failed; will retry")
            stopped.wait(0.5)

    stopped = threading.Event()
    last_scan = float("-inf")
    monitor = threading.Thread(target=scan, daemon=True)
    monitor.start()
    try:
        with ThreadingHTTPServer(("0.0.0.0", 8081), Handler) as server:
            server.serve_forever()
    finally:
        stopped.set()


if __name__ == "__main__":
    location = Path("/state/cron.sqlite3")
    serve(CronStore(location, os.environ["CRON_TIMEZONE"]))
