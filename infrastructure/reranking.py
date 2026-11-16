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
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(config.rerank_read_timeout, connect=3.0, pool=1.0)
        )

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
        response = self._post(
            {
                "model": self.model,
                "input": {"query": query, "documents": documents},
                "parameters": {"top_n": len(documents)},
            }
        )
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

    def _post(self, payload):
        for attempt in (1, 2):
            try:
                return self.client.post(
                    self.endpoint, headers={"Authorization": f"Bearer {self._key}"}, json=payload
                )
            except (httpx.ConnectTimeout, httpx.ConnectError) as error:
                # No request was sent during connection setup, so a second attempt is safe.
                if attempt == 2:
                    cause = (
                        "connection timed out"
                        if isinstance(error, httpx.ConnectTimeout)
                        else "connection failed"
                    )
                    raise RerankError(f"Reranking service {cause} after 2 attempts") from None
            except httpx.ReadTimeout:
                raise RerankError("Reranking service response timed out") from None
            except httpx.PoolTimeout:
                raise RerankError("Reranking client has no available connection") from None
            except httpx.WriteTimeout:
                raise RerankError("Reranking request upload timed out") from None
            except (httpx.HTTPError, RuntimeError):
                raise RerankError("Reranking HTTP exchange failed") from None
