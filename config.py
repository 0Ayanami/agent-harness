"""Explicit runtime configuration and optional live client construction."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


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

    def __post_init__(self):
        object.__setattr__(self, "workdir", Path(self.workdir).resolve())
        if not 1 <= self.embedding_dimensions <= 8192:
            raise ValueError("EMBEDDING_DIMENSIONS must be between 1 and 8192")
        if not self.embedding_model.strip():
            raise ValueError("EMBEDDING_MODEL must not be empty")
        if self.embedding_base_url:
            address = urlsplit(self.embedding_base_url)
            local = address.hostname in ("127.0.0.1", "localhost", "::1")
            if (
                not address.hostname
                or (address.scheme != "https" and not (address.scheme == "http" and local))
                or address.username
                or address.password
                or address.query
                or address.fragment
            ):
                raise ValueError(
                    "EMBEDDING_BASE_URL must be an HTTPS URL without credentials or query parameters"
                )

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
        return cls(
            Path.cwd(),
            model,
            os.getenv("FALLBACK_MODEL_ID"),
            embedding_api_key=(
                os.getenv("EMBEDDING_API_KEY")
                or os.getenv("OPENAI_API_KEY")
                or os.getenv("DASHSCOPE_API_KEY")
            ),
            embedding_base_url=(
                os.getenv("EMBEDDING_BASE_URL") or os.getenv("OPENAI_BASE_URL") or None
            ),
            embedding_model=os.getenv("EMBEDDING_MODEL") or "text-embedding-v4",
            embedding_dimensions=int(os.getenv("EMBEDDING_DIMENSIONS") or "1024"),
        )


def make_client():
    from anthropic import Anthropic

    if os.getenv("ANTHROPIC_BASE_URL"):
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    return Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"), timeout=120.0, max_retries=0)
