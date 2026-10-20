"""Explicit runtime configuration and optional live client construction."""

import os
from dataclasses import dataclass
from pathlib import Path


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

    def __post_init__(self):
        object.__setattr__(self, "workdir", Path(self.workdir).resolve())

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
        return cls(Path.cwd(), model, os.getenv("FALLBACK_MODEL_ID"))


def make_client():
    from anthropic import Anthropic

    if os.getenv("ANTHROPIC_BASE_URL"):
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    return Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"), timeout=120.0, max_retries=0)
