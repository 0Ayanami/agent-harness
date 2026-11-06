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
        "RERANK_ENABLED",
        "RERANK_API_KEY",
        "RERANK_BASE_URL",
        "RERANK_MODEL",
        "RERANK_CANDIDATES",
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
    assert config.rerank_base_url is None
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


def test_bailian_workspace_infers_native_rerank_endpoint_and_credentials(monkeypatch, tmp_path):
    isolated_environment(monkeypatch, tmp_path)
    base = "https://workspace.cn-beijing.maas.aliyuncs.com"
    monkeypatch.setenv("OPENAI_API_KEY", "shared-test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", base + "/compatible-mode/v1/embeddings")
    config = HarnessConfig.from_env()
    assert config.rerank_base_url == base + "/api/v1/services/rerank/text-rerank/text-rerank"
    assert config.rerank_api_key == "shared-test-key"
    assert config.rerank_model == "qwen3-rerank"
    assert config.rerank_candidates == 20
    assert "shared-test-key" not in repr(config)

    monkeypatch.setenv("RERANK_ENABLED", "false")
    config = HarnessConfig.from_env()
    assert config.rerank_base_url is None and config.rerank_api_key is None


def test_custom_rerank_endpoint_needs_its_own_key(monkeypatch, tmp_path):
    isolated_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "embedding-only-test-key")
    monkeypatch.setenv(
        "OPENAI_BASE_URL", "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    )
    monkeypatch.setenv("RERANK_BASE_URL", "https://independent.example.test/rerank")
    assert HarnessConfig.from_env().rerank_api_key is None

    monkeypatch.setenv("RERANK_API_KEY", "independent-test-key")
    monkeypatch.setenv("RERANK_MODEL", "alternate-rerank")
    monkeypatch.setenv("RERANK_CANDIDATES", "12")
    config = HarnessConfig.from_env()
    assert config.rerank_api_key == "independent-test-key"
    assert config.rerank_model == "alternate-rerank" and config.rerank_candidates == 12
    assert "independent-test-key" not in repr(config)
