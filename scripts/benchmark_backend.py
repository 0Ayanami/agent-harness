"""Reproducible loopback benchmark using isolated data and synthetic providers.

Never loads .env or touches the user's runtime. Run with the project Python.
Results describe this host and this workload, not production capacity or RAG quality.
"""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import socket
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import uvicorn

from agent_harness import HarnessConfig
from agent_harness.interfaces.web.app import create_app


HEADERS = {"X-Agent-Client": "web"}


class SyntheticModel:
    def __init__(self):
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **arguments):
        time.sleep(0.05)
        content = "Synthetic answer [S1]" if "search_library" in str(arguments.get("tools")) else "Synthetic answer"
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=content)], stop_reason="end_turn"
        )


class Provider(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    counts = Counter()
    guard = threading.Lock()

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.guard:
            self.counts[self.path] += 1
        if self.path == "/embeddings":
            time.sleep(0.01)
            rows = []
            for index, value in enumerate(body["input"]):
                digest = hashlib.sha256(value.encode()).digest()
                rows.append({"index": index, "embedding": [(digest[i % 32] - 127) / 128 for i in range(64)]})
            value = {"model": body["model"], "data": rows}
        else:
            time.sleep(0.01)
            value = {"output": {"results": [
                {"index": i, "relevance_score": 1 / (i + 1)}
                for i in range(len(body["input"]["documents"]))
            ]}}
        payload = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def percentile(samples, fraction):
    return sorted(samples)[max(0, math.ceil(len(samples) * fraction) - 1)] if samples else None


async def measure(name, count, concurrency, operation):
    gate = asyncio.Semaphore(concurrency)
    samples, errors, statuses = [], [], Counter()

    async def one(index):
        async with gate:
            started = time.perf_counter()
            try:
                status = await operation(index)
                statuses[str(status)] += 1
                if status not in (200, 201, 204):
                    errors.append(str(status))
            except Exception as error:
                errors.append(type(error).__name__ + ": " + str(error)[:160])
            samples.append((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(count)))
    elapsed = time.perf_counter() - started
    result = {
        "name": name, "concurrency": concurrency, "operations": count,
        "seconds": round(elapsed, 4), "operations_per_second": round(count / elapsed, 2),
        "errors": len(errors), "error_examples": errors[:3], "statuses": dict(statuses),
        "successful_operations": count - len(errors),
        "successful_operations_per_second": round((count - len(errors)) / elapsed, 2),
        "error_rate": len(errors) / count,
        "p50_ms": round(percentile(samples, 0.5), 3),
        "p95_ms": round(percentile(samples, 0.95), 3),
        "p99_ms": round(percentile(samples, 0.99), 3),
        "latencies_ms": [round(value, 3) for value in samples],
    }
    print(json.dumps({k: v for k, v in result.items() if k != "latencies_ms"}), flush=True)
    return result


async def workload(base, app, count):
    results = []
    async with httpx.AsyncClient(base_url=base, headers=HEADERS, timeout=60, trust_env=False,
                                 limits=httpx.Limits(max_connections=160, max_keepalive_connections=100,
                                                    keepalive_expiry=1)) as client:
        async def create():
            response = await client.post("/api/agent/sessions", json={"personalized": False})
            response.raise_for_status()
            return response.json()["id"]

        sessions = [await create() for _ in range(32)]
        repository = app.state.application.repository
        for i in range(240):
            repository.emit(sessions[0], "benchmark", {"index": i, "text": "synthetic event"})
        # Thirty documents, approximately 180 chunks. No real user material is used.
        identifiers = []
        for i in range(30):
            value = (f"# Synthetic document {i}\n\n" +
                     (f"知识库 检索 benchmark topic{i} latency storage retrieval.\n" * 100)).encode()
            response = await client.post("/api/agent/library/files", files={"file": (f"note-{i}.md", value)})
            response.raise_for_status()
            identifiers.append(response.json()["id"])
        deadline = time.monotonic() + 90
        while True:
            state = (await client.get("/api/agent/library/retrieval")).json()
            if state.get("ready_documents") == len(identifiers):
                break
            if time.monotonic() > deadline:
                raise RuntimeError(f"Vector indexing did not finish: {state}")
            await asyncio.sleep(0.05)

        async def get(path):
            return (await client.get(path)).status_code

        async def lifecycle(_):
            sid = await create()
            return (await client.delete(f"/api/agent/sessions/{sid}")).status_code

        async def replay(_):
            response = await client.get(f"/api/agent/sessions/{sessions[0]}/events?once=true")
            response.raise_for_status()
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            if len(events) != 240 or [event["payload"]["index"] for event in events] != list(range(240)):
                raise AssertionError("Replay omitted or reordered events")
            return 200

        def restore_fixture_sessions():
            # A failed POST may have committed without delivering its ID. Remove
            # these only after all operations in a phase have drained.
            for record in repository.list():
                if record["id"] not in sessions:
                    app.state.application.delete(record["id"])
            assert {record["id"] for record in repository.list()} == set(sessions)

        for c in (1, 16, 64):
            results.append(await measure("health", count, c, lambda _: get("/api/agent/health")))
            results.append(await measure("session_list", count, c, lambda _: get("/api/agent/sessions")))
            results.append(await measure("event_replay_240", count, c, replay))
        for c in (1, 8, 32):
            results.append(await measure("session_create_delete", max(32, count // 2), c, lifecycle))
            restore_fixture_sessions()

        async def search(mode, _):
            response = await client.post("/api/agent/library/search", json={
                "query": "知识库 检索 latency", "mode": mode, "limit": 8})
            response.raise_for_status()
            result = response.json()
            if not result["hits"] or not result["reranked"] or result["mode"] != mode:
                raise AssertionError(f"Invalid search result in {mode}")
            return response.status_code

        for mode in ("keyword", "vector", "hybrid"):
            for c in (1, 8):
                results.append(await measure("search_" + mode, max(24, count // 2), c,
                                             lambda i, mode=mode: search(mode, i)))

        for rag in (False, True):
            for c in (1, 8):
                # Reused sessions keep the fixture count fixed across variants.
                async def turn(i):
                    sid = sessions[i % 24 + 1]
                    cursor = repository.cursor(sid)
                    body = {"message": "Explain synthetic retrieval latency"}
                    if rag:
                        body["library"] = {"enabled": True, "mode": "hybrid"}
                    response = await client.post(f"/api/agent/sessions/{sid}/messages", json=body)
                    response.raise_for_status()
                    deadline = time.monotonic() + 45
                    observed = []
                    while time.monotonic() < deadline:
                        replay = await client.get(f"/api/agent/sessions/{sid}/events", params={"after": cursor, "once": True})
                        events = [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith("data: ")]
                        if events:
                            cursor = events[-1]["id"]
                            observed.extend(events)
                        if any(event["kind"] == "run_finished" for event in events):
                            finished = next(event for event in events if event["kind"] == "run_finished")
                            if finished["payload"]["outcome"] != "completed":
                                raise AssertionError("Turn failed")
                            if not any(event["kind"] == "assistant" and "Synthetic answer" in event["payload"].get("text", "") for event in observed):
                                raise AssertionError("Turn did not deliver an answer")
                            if rag and not any(event["kind"] == "library_answer" and event["payload"].get("citations") for event in observed):
                                raise AssertionError("RAG answer did not contain a current citation")
                            return 200
                        await asyncio.sleep(0.01)
                    raise TimeoutError("Turn did not finish")
                results.append(await measure("rag_turn" if rag else "chat_turn", 24, c, turn))

        # Open actual long SSE connections, then measure unrelated request latency.
        stop = asyncio.Event()
        ready = [asyncio.Event() for _ in range(32)]

        async def hold_sse(index):
            async with client.stream("GET", f"/api/agent/sessions/{sessions[index]}/events",
                                     params={"after": repository.cursor(sessions[index])}) as stream:
                async for line in stream.aiter_lines():
                    if line.startswith(": keepalive"):
                        ready[index].set()
                    if stop.is_set():
                        return

        tasks = [asyncio.create_task(hold_sse(i)) for i in range(32)]
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in ready)), 20)
        try:
            results.append(await measure("health_with_32_idle_sse", count, 16,
                                         lambda _: get("/api/agent/health")))
            results.append(await measure("lifecycle_with_32_idle_sse", max(32, count // 2), 8, lifecycle))
            restore_fixture_sessions()
        finally:
            stop.set()
            await asyncio.gather(*tasks)
        return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--requests", type=int, default=120)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.requests < 24:
        parser.error("Use at least 24 requests")
    root = Path(__file__).resolve().parents[1]
    workdir = root / ".artifacts" / "performance" / (args.label + "-" + uuid4().hex[:8])
    workdir.mkdir(parents=True)
    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    provider.daemon_threads = True
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{provider.server_port}"
    config = HarnessConfig(workdir, "synthetic-model-50ms", embedding_api_key="synthetic",
                           embedding_base_url=endpoint, embedding_model="synthetic-hash-64",
                           embedding_dimensions=64, rerank_api_key="synthetic",
                           rerank_base_url=endpoint + "/rerank", rerank_model="synthetic-rank-10ms")
    app = create_app(config, SyntheticModel)
    # Local fixtures must never use HTTP(S)_PROXY inherited from the desktop.
    app.state.retrieval.provider.client.close()
    app.state.retrieval.provider.client = httpx.Client(timeout=30, trust_env=False)
    app.state.retrieval.reranker.client.close()
    app.state.retrieval.reranker.client = httpx.Client(
        timeout=httpx.Timeout(8, connect=3), trust_env=False
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=3))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        started = time.monotonic()
        while not server.started:
            if not thread.is_alive() or time.monotonic() - started > 15:
                raise RuntimeError("Benchmark server failed to start")
            time.sleep(0.02)
        results = asyncio.run(workload(f"http://127.0.0.1:{port}", app, args.requests))
        report = {
            "label": args.label, "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "platform": platform.platform(), "python": platform.python_version(),
            "logical_cpus": os.cpu_count(), "cpu": os.environ.get("PROCESSOR_IDENTIFIER"),
            "versions": {name: importlib.metadata.version(name) for name in ("fastapi", "uvicorn", "httpx", "qdrant-client")},
            "method": "closed-loop loopback HTTP, client and server on same host/process, no external API; each scenario starts after previous drains",
            "fixture": {"documents": 30, "sessions": 32, "event_replay_rows": 240,
                        "chat_delay_ms": 50, "embedding_delay_ms": 10, "rerank_delay_ms": 10,
                        "vector_dimensions": 64, "quality_evaluation": False},
            "provider_requests": dict(Provider.counts), "results": results,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Report: {args.output}", flush=True)
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        listener.close()
        provider.shutdown()
        provider.server_close()
        if thread.is_alive():
            raise RuntimeError("Benchmark server did not shut down")


if __name__ == "__main__":
    main()
