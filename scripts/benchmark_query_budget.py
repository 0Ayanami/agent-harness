"""Count real SQLite connections in scoped keyword retrieval with mocked reranking.

Uses a temporary synthetic library, real parser/FTS5/indexes and a MockTransport
reranker. Does not load .env, call an external provider or read user documents.
Background ingestion is stopped before timing so the connection budget is exact.
"""

import argparse
import io
import json
import math
import platform
import sqlite3
import statistics
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from agent_harness import HarnessConfig
from agent_harness.services.ingestion import IngestionService
from agent_harness.services.library import LibraryService
from agent_harness.services.retrieval import RetrievalService


def run(iterations, artifact_root):
    with TemporaryDirectory(prefix="query-budget-", dir=artifact_root) as directory:
        workdir = Path(directory)
        config = HarnessConfig(
            workdir,
            "synthetic-model-no-requests",
            rerank_api_key="synthetic-key-never-send",
            rerank_base_url="https://rerank.invalid/mock",
            rerank_candidates=20,
        )
        library = LibraryService(config.data_dir / "library")
        ingestion = IngestionService(library, cache_dir=workdir / "cache")
        library.start()
        ingestion.start()
        retrieval = None
        rerank_calls = 0

        def rerank(request):
            nonlocal rerank_calls
            body = json.loads(request.content)
            candidates = body["input"]["documents"]
            if len(candidates) != 20:
                raise AssertionError(f"Expected 20 real candidates, got {len(candidates)}")
            rerank_calls += 1
            return httpx.Response(
                200,
                json={"output": {"results": [
                    {"index": index, "relevance_score": (20 - index) / 20}
                    for index in range(20)
                ]}},
            )

        try:
            identifiers = []
            for index in range(20):
                body = f"Synthetic orbital retrieval passage number {index}.".encode()
                record = library.upload(f"synthetic-{index}.txt", io.BytesIO(body))
                identifiers.append(record["id"])
            deadline = time.monotonic() + 30
            while not all(ingestion.status(identifier)["status"] == "ready" for identifier in identifiers):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Synthetic indexing did not finish within 30 seconds")
                time.sleep(0.02)
            ingestion.close()
            if ingestion.thread.is_alive():
                raise RuntimeError("Ingestion worker must be stopped before connection counting")

            with httpx.Client(transport=httpx.MockTransport(rerank)) as client:
                retrieval = RetrievalService(config, library, ingestion, rerank_client=client)

                def search():
                    result = retrieval.search("orbital", mode="keyword", limit=8)
                    if not result["reranked"] or len(result["hits"]) != 8 or result["warning"]:
                        raise AssertionError("Expected eight verified and reranked real passages")
                    return result

                for _ in range(3):
                    search()

                original_connection = library.repository.connection
                connections = 0

                @contextmanager
                def counted_connection():
                    nonlocal connections
                    connections += 1
                    with original_connection() as database:
                        yield database

                library.repository.connection = counted_connection
                samples, counts = [], []
                try:
                    for _ in range(iterations):
                        before = connections
                        started = time.perf_counter()
                        search()
                        samples.append((time.perf_counter() - started) * 1000)
                        counts.append(connections - before)
                finally:
                    library.repository.connection = original_connection

            ordered = sorted(samples)
            return {
                "captured_at_utc": datetime.now(timezone.utc).isoformat(),
                "python": platform.python_version(),
                "sqlite": sqlite3.sqlite_version,
                "platform": platform.platform(),
                "workload": "keyword + mocked qwen3-rerank, 20 real candidates, limit=8",
                "documents": 20,
                "chunks": 20,
                "warmup_requests": 3,
                "measured_requests": iterations,
                "background_workers_running": 0,
                "external_requests": 0,
                "mock_rerank_requests": rerank_calls,
                "connections_per_request": counts,
                "connections_min": min(counts),
                "connections_max": max(counts),
                "mean_ms": round(statistics.mean(samples), 3),
                "p50_ms": round(ordered[math.ceil(iterations * 0.50) - 1], 3),
                "p95_ms": round(ordered[math.ceil(iterations * 0.95) - 1], 3),
                "p99_ms": round(ordered[math.ceil(iterations * 0.99) - 1], 3),
                "latencies_ms": [round(value, 3) for value in samples],
                "scope": "Isolated in-process connection-budget probe; not HTTP QPS or model quality",
            }
        finally:
            if retrieval is not None:
                retrieval.close()
            ingestion.close()
            library.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if not 1 <= arguments.iterations <= 10000:
        parser.error("--iterations must be between 1 and 10000")
    artifact_root = Path(__file__).resolve().parents[1] / ".artifacts" / "benchmarks"
    artifact_root.mkdir(parents=True, exist_ok=True)
    result = run(arguments.iterations, artifact_root)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
