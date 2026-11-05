"""Explicit, bounded Bailian-compatible embedding HTTP calls; credentials stay private."""

import base64
import hashlib
import json
import math
import struct
import time
from urllib.parse import urlsplit

import httpx


class EmbeddingError(RuntimeError):
    """A safe diagnostic: never includes request headers, provider bodies or secrets."""


class BailianEmbeddings:
    BATCH_SIZE = 10

    def __init__(self, config, *, client=None, sleep=time.sleep):
        base = (config.embedding_base_url or "").strip().rstrip("/")
        address = urlsplit(base)
        local = address.hostname in ("localhost", "127.0.0.1", "::1")
        if (
            not config.embedding_api_key
            or not address.hostname
            or (address.scheme != "https" and not (local and address.scheme == "http"))
            or address.username
            or address.password
            or address.query
            or address.fragment
        ):
            raise EmbeddingError("Configure a valid embedding API key and explicit HTTPS base URL")
        self.endpoint = base if base.endswith("/embeddings") else base + "/embeddings"
        self.model = config.embedding_model
        self.dimensions = config.embedding_dimensions
        self._key = config.embedding_api_key
        self._owned_client = client is None
        self.client = client or httpx.Client(timeout=30.0)
        self.sleep = sleep
        self.profile = hashlib.sha256(
            json.dumps(
                {
                    "endpoint": self.endpoint,
                    "model": self.model,
                    "dimensions": self.dimensions,
                    "normalization": "l2",
                    "protocol": "compatible-float-v1",
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def __repr__(self):
        return f"BailianEmbeddings(model={self.model!r}, dimensions={self.dimensions})"

    def close(self):
        if self._owned_client:
            self.client.close()

    def embed(self, texts, *, kind="document"):
        if kind not in ("query", "document"):
            raise EmbeddingError("Embedding input kind must be query or document")
        if not texts:
            return []
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise EmbeddingError("Embedding input must contain non-empty text")
        result = []
        for first in range(0, len(texts), self.BATCH_SIZE):
            result.extend(self._batch(texts[first : first + self.BATCH_SIZE]))
        return result

    def _batch(self, texts):
        for attempt in range(3):
            try:
                response = self.client.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {self._key}"},
                    json={
                        "model": self.model,
                        "input": texts,
                        "dimensions": self.dimensions,
                        "encoding_format": "float",
                    },
                    timeout=30.0,
                )
            except httpx.HTTPError:
                if attempt == 2:
                    raise EmbeddingError("Embedding service could not be reached") from None
                self.sleep(0.5 * 2**attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    self.sleep(0.5 * 2**attempt)
                    continue
            if not response.is_success:
                raise EmbeddingError(
                    f"Embedding service rejected the request (HTTP {response.status_code})"
                ) from None
            try:
                body = response.json()
                if body.get("model") not in (None, self.model):
                    raise ValueError("unexpected model")
                rows = body["data"]
                if not isinstance(rows, list) or len(rows) != len(texts):
                    raise ValueError("incorrect result count")
                output = [None] * len(texts)
                for row in rows:
                    index = row["index"]
                    if (
                        type(index) is not int
                        or not 0 <= index < len(texts)
                        or output[index] is not None
                    ):
                        raise ValueError("incorrect result index")
                    vector = row["embedding"]
                    if isinstance(vector, str):
                        data = base64.b64decode(vector, validate=True)
                        if len(data) != self.dimensions * 4:
                            raise ValueError("incorrect encoded dimensions")
                        vector = struct.unpack(f"<{self.dimensions}f", data)
                    if not isinstance(vector, (list, tuple)) or len(vector) != self.dimensions:
                        raise ValueError("incorrect dimensions")
                    if any(
                        type(value) not in (int, float) or not math.isfinite(value)
                        for value in vector
                    ):
                        raise ValueError("non-numeric or non-finite vector")
                    norm = math.hypot(*vector)
                    if not math.isfinite(norm) or not norm:
                        raise ValueError("invalid vector magnitude")
                    output[index] = [float(value) / norm for value in vector]
                return output
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                raise EmbeddingError(
                    "Embedding service returned invalid vectors or result ordering"
                ) from None
        raise EmbeddingError("Embedding request could not be completed")
