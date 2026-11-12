"""Explicit runtime configuration and optional live client construction."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


_RERANK_PATH = "/api/v1/services/rerank/text-rerank/text-rerank"
_BAILIAN_WORKSPACE = re.compile(
    r"[a-z0-9-]+\.(?:cn-beijing|ap-southeast-1|us-east-1)\.maas\.aliyuncs\.com"
)


def _validate_service_url(value, variable):
    if not value:
        return
    address = urlsplit(value)
    local = address.hostname in ("127.0.0.1", "localhost", "::1")
    if (
        not address.hostname
        or (address.scheme != "https" and not (address.scheme == "http" and local))
        or address.username
        or address.password
        or address.query
        or address.fragment
    ):
        raise ValueError(f"{variable} must be an HTTPS URL without credentials or query parameters")


def _bailian_rerank_endpoint(embedding_url):
    address = urlsplit(embedding_url or "")
    if (
        address.scheme == "https"
        and _BAILIAN_WORKSPACE.fullmatch(address.hostname or "")
        and address.port in (None, 443)
        and address.path.rstrip("/") in ("/compatible-mode/v1", "/compatible-mode/v1/embeddings")
    ):
        return f"https://{address.hostname}{_RERANK_PATH}"
    return None


@dataclass(frozen=True)
class HarnessConfig:
    workdir: Path
    model: str
    fallback_model: str | None = None
    context_limit: int = 50000
    keep_recent_results: int = 3
    persist_threshold: int = 30000
    max_tokens: int = 8000
    escalated_max_tokens: int = 16000
    max_retries: int = 3
    max_consecutive_overload: int = 2
    retry_delay_ms: int = 500
    max_recovery_retries: int = 2
    approval_timeout: float = 300.0
    embedding_api_key: str | None = field(default=None, repr=False)
    embedding_base_url: str | None = None
    embedding_model: str = "text-embedding-v4"
    embedding_dimensions: int = 1024
    rerank_api_key: str | None = field(default=None, repr=False)
    rerank_base_url: str | None = None
    rerank_model: str = "qwen3-rerank"
    rerank_candidates: int = 20
    vector_backend: str = "qdrant"
    milvus_uri: str = "http://127.0.0.1:19530"
    milvus_token: str | None = field(default=None, repr=False)
    milvus_collection_prefix: str = "ayanami_library"
    redis_url: str | None = field(default=None, repr=False)
    redis_prefix: str = "ayanami"
    redis_cache_ttl: int = 300

    def __post_init__(self):
        object.__setattr__(self, "workdir", Path(self.workdir).resolve())
        if not 1 <= self.embedding_dimensions <= 8192:
            raise ValueError("EMBEDDING_DIMENSIONS must be between 1 and 8192")
        if not self.embedding_model.strip():
            raise ValueError("EMBEDDING_MODEL must not be empty")
        if not 2 <= self.rerank_candidates <= 50:
            raise ValueError("RERANK_CANDIDATES must be between 2 and 50")
        if not self.rerank_model.strip():
            raise ValueError("RERANK_MODEL must not be empty")
        _validate_service_url(self.embedding_base_url, "EMBEDDING_BASE_URL")
        _validate_service_url(self.rerank_base_url, "RERANK_BASE_URL")
        if self.vector_backend not in ("qdrant", "milvus"):
            raise ValueError("VECTOR_BACKEND must be qdrant or milvus")
        address = urlsplit(self.milvus_uri)
        if (address.scheme not in ("http", "https") or not address.hostname
                or address.username or address.password or address.query or address.fragment):
            raise ValueError("MILVUS_URI must be an HTTP(S) server URL without credentials")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", self.milvus_collection_prefix):
            raise ValueError("MILVUS_COLLECTION_PREFIX must be a short identifier")
        if self.redis_url:
            redis_address = urlsplit(self.redis_url)
            if (redis_address.scheme not in ("redis", "rediss") or not redis_address.hostname
                    or redis_address.query or redis_address.fragment):
                raise ValueError("REDIS_URL must be a Redis URL without query parameters")
        if not re.fullmatch(r"[A-Za-z0-9_:-]{1,64}", self.redis_prefix):
            raise ValueError("REDIS_PREFIX must be a short namespace without wildcards")
        if not 1 <= self.redis_cache_ttl <= 86400:
            raise ValueError("REDIS_CACHE_TTL must be between 1 and 86400 seconds")

    @property
    def data_dir(self) -> Path:
        return self.workdir / ".runtime" / "agent-harness"

    @classmethod
    def from_env(cls):
        from dotenv import load_dotenv

        load_dotenv(Path.cwd() / ".env", override=True)
        model = os.getenv("MODEL_ID")
        if not model:
            raise ValueError("MODEL_ID is required in the environment or .env")
        embedding_key = (
            os.getenv("EMBEDDING_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or os.getenv("DASHSCOPE_API_KEY")
        )
        embedding_url = os.getenv("EMBEDDING_BASE_URL") or os.getenv("OPENAI_BASE_URL") or None
        rerank_enabled = os.getenv("RERANK_ENABLED", "true").strip().lower()
        if rerank_enabled not in ("true", "1", "false", "0"):
            raise ValueError("RERANK_ENABLED must be true or false")
        rerank_key, rerank_url = None, None
        if rerank_enabled in ("true", "1"):
            native_endpoint = _bailian_rerank_endpoint(embedding_url)
            rerank_url = (os.getenv("RERANK_BASE_URL") or "").strip().rstrip("/") or native_endpoint
            rerank_key = os.getenv("RERANK_API_KEY") or (
                embedding_key if native_endpoint and rerank_url == native_endpoint else None
            )
        return cls(
            Path.cwd(),
            model,
            os.getenv("FALLBACK_MODEL_ID"),
            embedding_api_key=embedding_key,
            embedding_base_url=embedding_url,
            embedding_model=os.getenv("EMBEDDING_MODEL") or "text-embedding-v4",
            embedding_dimensions=int(os.getenv("EMBEDDING_DIMENSIONS") or "1024"),
            rerank_api_key=rerank_key,
            rerank_base_url=rerank_url,
            rerank_model=os.getenv("RERANK_MODEL") or "qwen3-rerank",
            rerank_candidates=int(os.getenv("RERANK_CANDIDATES") or "20"),
            vector_backend=os.getenv("VECTOR_BACKEND") or "qdrant",
            milvus_uri=os.getenv("MILVUS_URI") or "http://127.0.0.1:19530",
            milvus_token=os.getenv("MILVUS_TOKEN") or None,
            milvus_collection_prefix=os.getenv("MILVUS_COLLECTION_PREFIX") or "ayanami_library",
            redis_url=os.getenv("REDIS_URL") or None,
            redis_prefix=os.getenv("REDIS_PREFIX") or "ayanami",
            redis_cache_ttl=int(os.getenv("REDIS_CACHE_TTL") or "300"),
        )


def make_client():
    from anthropic import Anthropic

    if os.getenv("ANTHROPIC_BASE_URL"):
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    return Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"), timeout=120.0, max_retries=0)
