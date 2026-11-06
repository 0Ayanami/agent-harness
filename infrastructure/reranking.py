"""DashScope text reranking with bounded latency and safe diagnostics."""

import httpx


class RerankError(RuntimeError):
    """A diagnostic without provider response bodies or credentials."""


class BailianReranker:
    MAX_CANDIDATES = 50

    def __init__(self, config, *, client=None):
        self.endpoint = config.rerank_base_url
        self.model = config.rerank_model
        self._key = config.rerank_api_key
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=httpx.Timeout(8.0, connect=3.0))

    def close(self):
        if self._owns_client:
            self.client.close()

    def rerank(self, query, documents):
        if (
            not isinstance(query, str)
            or not query.strip()
            or not (2 <= len(documents) <= self.MAX_CANDIDATES)
        ):
            raise RerankError("Reranking requires a query and between 2 and 50 candidates")
        if any(not isinstance(text, str) or not text.strip() for text in documents):
            raise RerankError("Reranking candidates must contain text")
        try:
            response = self.client.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {self._key}"},
                json={
                    "model": self.model,
                    "input": {"query": query, "documents": documents},
                    "parameters": {"top_n": len(documents)},
                },
            )
        except (httpx.HTTPError, RuntimeError):
            raise RerankError("Reranking service could not be reached") from None
        if not response.is_success:
            raise RerankError(
                f"Reranking service rejected the request (HTTP {response.status_code})"
            )
        try:
            body = response.json()
            rows = body["output"]["results"]
            if body.get("code") or not isinstance(rows, list) or len(rows) != len(documents):
                raise ValueError("Incomplete ranking")
            scores = {}
            for row in rows:
                index, score = row["index"], row["relevance_score"]
                if type(index) is not int or not 0 <= index < len(documents) or index in scores:
                    raise ValueError("Invalid candidate index")
                if type(score) not in (int, float) or not 0 <= score <= 1:
                    raise ValueError("Invalid relevance score")
                scores[index] = float(score)
        except (AttributeError, KeyError, TypeError, ValueError):
            raise RerankError("Reranking service returned an invalid result") from None
        return sorted(scores.items(), key=lambda item: (-item[1], item[0]))
