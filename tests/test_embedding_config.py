"""Embedding configuration can reuse a compatible service without exposing its key."""

import pytest

from agent_harness import HarnessConfig


def isolated_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in (
        "EMBEDDING_API_KEY",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIMENSIONS",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "DASHSCOPE_API_KEY",
        "FALLBACK_MODEL_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MODEL_ID", "chat-model-for-test")


def test_shared_compatible_credentials_keep_embedding_model_independent(monkeypatch, tmp_path):
    isolated_environment(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=unit-test-private-key\n"
        "OPENAI_BASE_URL=https://workspace.example.test/compatible-mode/v1\n",
        encoding="utf-8",
    )
    config = HarnessConfig.from_env()
    assert config.embedding_api_key == "unit-test-private-key"
    assert config.embedding_base_url == "https://workspace.example.test/compatible-mode/v1"
    assert config.embedding_model == "text-embedding-v4"
    assert config.embedding_dimensions == 1024
    assert config.model == "chat-model-for-test"
    assert "unit-test-private-key" not in repr(config)


def test_explicit_embedding_settings_override_shared_service(monkeypatch, tmp_path):
    isolated_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "shared-test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://shared.example.test/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "embedding-test-key")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://embedding.example.test/v1/embeddings")
    monkeypatch.setenv("EMBEDDING_MODEL", "alternate-embedding-model")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "512")
    config = HarnessConfig.from_env()
    assert config.embedding_api_key == "embedding-test-key"
    assert config.embedding_base_url == "https://embedding.example.test/v1/embeddings"
    assert config.embedding_model == "alternate-embedding-model"
    assert config.embedding_dimensions == 512
    assert "embedding-test-key" not in repr(config)


@pytest.mark.parametrize(
    "url",
    [
        "http://public.example.test/v1",
        "https://user:private@example.test/v1",
        "https://example.test/v1?api_key=private",
        "https://example.test/v1#private",
    ],
)
def test_embedding_endpoint_rejects_plaintext_or_embedded_credentials(tmp_path, url):
    with pytest.raises(ValueError, match="EMBEDDING_BASE_URL"):
        HarnessConfig(tmp_path, "test-model", embedding_base_url=url)
