"""Tests for EmbeddingStore — semantic memory search with vector embeddings."""

from __future__ import annotations

import math
import sqlite3
import struct
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_daemon.core.config import DaemonConfig
from claude_daemon.memory.embeddings import (
    DEFAULT_DIM,
    EmbeddingStore,
    _cosine_similarity,
    _resolve_api_key,
    _serialize_f32,
)


# ------------------------------------------------------------------ #
# Helper function tests
# ------------------------------------------------------------------ #


def test_serialize_f32_roundtrip():
    vec = [1.0, 2.0, 3.0, -0.5]
    serialized = _serialize_f32(vec)
    assert len(serialized) == len(vec) * 4
    unpacked = struct.unpack(f"{len(vec)}f", serialized)
    for a, b in zip(vec, unpacked):
        assert a == pytest.approx(b)


def test_cosine_similarity_identical():
    vec = [1.0, 0.0, 1.0, 0.5]
    assert _cosine_similarity(vec, vec) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal():
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]
    assert _cosine_similarity(a, b) == pytest.approx(0.0)


def test_cosine_similarity_opposite():
    a = [1.0, 0.0]
    b = [-1.0, 0.0]
    assert _cosine_similarity(a, b) == pytest.approx(-1.0)


def test_cosine_similarity_zero_vector():
    assert _cosine_similarity([0, 0], [1, 1]) == 0.0
    assert _cosine_similarity([1, 1], [0, 0]) == 0.0


# ------------------------------------------------------------------ #
# API key resolution tests
# ------------------------------------------------------------------ #


def test_api_key_prefers_config():
    key = _resolve_api_key("voyage", config_key="cfg-key")
    assert key == "cfg-key"


def test_api_key_voyage_from_env():
    with patch.dict("os.environ", {"VOYAGE_API_KEY": "vk-test", "ANTHROPIC_API_KEY": "ak-test"}):
        key = _resolve_api_key("voyage")
        assert key == "vk-test"


def test_api_key_voyage_falls_back_to_anthropic():
    env = {"ANTHROPIC_API_KEY": "ak-fallback"}
    with patch.dict("os.environ", env, clear=True):
        key = _resolve_api_key("voyage")
        assert key == "ak-fallback"


def test_api_key_ollama_returns_none():
    key = _resolve_api_key("ollama")
    assert key is None


def test_api_key_openai_from_env():
    with patch.dict("os.environ", {"OPENAI_API_KEY": "ok-test"}):
        key = _resolve_api_key("openai")
        assert key == "ok-test"


def test_api_key_returns_none_when_missing():
    with patch.dict("os.environ", {}, clear=True):
        key = _resolve_api_key("voyage")
        assert key is None


# ------------------------------------------------------------------ #
# Chunking tests
# ------------------------------------------------------------------ #


def test_chunk_markdown_empty():
    assert EmbeddingStore._chunk_markdown("") == []
    assert EmbeddingStore._chunk_markdown("   ") == []


def test_chunk_markdown_single_section():
    text = "## Title\nSome content here."
    chunks = EmbeddingStore._chunk_markdown(text)
    assert len(chunks) == 1
    assert "Title" in chunks[0]
    assert "content" in chunks[0]


def test_chunk_markdown_splits_on_headers():
    text = "## Section A\nContent A.\n\n## Section B\nContent B."
    chunks = EmbeddingStore._chunk_markdown(text)
    assert len(chunks) == 2
    assert "Content A" in chunks[0]
    assert "Content B" in chunks[1]


def test_chunk_markdown_respects_max_chunk():
    text = "## Title\n\n" + "\n\n".join(f"Paragraph {i} content." for i in range(20))
    chunks = EmbeddingStore._chunk_markdown(text, max_chunk=100)
    assert len(chunks) > 1


def test_chunk_markdown_splits_large_sections_on_paragraphs():
    text = "## Big Section\n\n" + "Para one. " * 20 + "\n\n" + "Para two. " * 20
    chunks = EmbeddingStore._chunk_markdown(text, max_chunk=100)
    assert len(chunks) >= 2


def test_chunk_markdown_preserves_content():
    text = "## A\nHello world\n## B\nGoodbye world"
    chunks = EmbeddingStore._chunk_markdown(text)
    combined = " ".join(chunks)
    assert "Hello world" in combined
    assert "Goodbye world" in combined


# ------------------------------------------------------------------ #
# Graceful degradation tests
# ------------------------------------------------------------------ #


def test_disabled_store_not_available():
    config = DaemonConfig(embeddings_enabled=False)
    db = sqlite3.connect(":memory:")
    store = EmbeddingStore(db, config)
    assert store.available is False
    db.close()


@pytest.mark.asyncio
async def test_disabled_store_search_returns_empty():
    config = DaemonConfig(embeddings_enabled=False)
    db = sqlite3.connect(":memory:")
    store = EmbeddingStore(db, config)
    result = await store.search("test query")
    assert result == []
    db.close()


@pytest.mark.asyncio
async def test_disabled_store_embed_returns_none():
    config = DaemonConfig(embeddings_enabled=False)
    db = sqlite3.connect(":memory:")
    store = EmbeddingStore(db, config)
    result = await store.embed_text("hello")
    assert result is None
    db.close()


@pytest.mark.asyncio
async def test_disabled_store_index_returns_zero():
    config = DaemonConfig(embeddings_enabled=False)
    db = sqlite3.connect(":memory:")
    store = EmbeddingStore(db, config)
    result = await store.index_chunk("source", "some chunk")
    assert result is False
    db.close()


# ------------------------------------------------------------------ #
# Hash embedding tests
# ------------------------------------------------------------------ #


def test_embed_hash_produces_correct_dim():
    config = DaemonConfig(embeddings_enabled=False)
    db = sqlite3.connect(":memory:")
    store = EmbeddingStore(db, config)
    store._dim = 384
    store._dim_detected = True
    vec = store._embed_hash("hello world test embedding")
    assert len(vec) == 384
    db.close()


def test_embed_hash_is_normalized():
    config = DaemonConfig(embeddings_enabled=False)
    db = sqlite3.connect(":memory:")
    store = EmbeddingStore(db, config)
    store._dim = 128
    store._dim_detected = True
    vec = store._embed_hash("some text for embedding")
    norm = math.sqrt(sum(x * x for x in vec))
    assert norm == pytest.approx(1.0, abs=0.01)
    db.close()


def test_embed_hash_deterministic():
    config = DaemonConfig(embeddings_enabled=False)
    db = sqlite3.connect(":memory:")
    store = EmbeddingStore(db, config)
    store._dim = 64
    store._dim_detected = True
    v1 = store._embed_hash("hello world")
    v2 = store._embed_hash("hello world")
    assert v1 == v2
    db.close()


def test_embed_hash_different_texts_differ():
    config = DaemonConfig(embeddings_enabled=False)
    db = sqlite3.connect(":memory:")
    store = EmbeddingStore(db, config)
    store._dim = 64
    store._dim_detected = True
    v1 = store._embed_hash("hello world")
    v2 = store._embed_hash("goodbye universe")
    assert v1 != v2
    db.close()


# ------------------------------------------------------------------ #
# Config integration tests
# ------------------------------------------------------------------ #


def test_config_defaults():
    config = DaemonConfig()
    assert config.embedding_provider == "ollama"
    assert config.embedding_api_base == "http://localhost:11434"
    assert config.embedding_model == "nomic-embed-text"
    assert config.embedding_dim == 0
    assert config.embedding_top_k == 3
    assert config.embedding_similarity_threshold == 0.3
    assert config.embedding_chunk_size == 500
    assert config.embedding_batch_size == 128


def test_config_dim_used_by_store():
    config = DaemonConfig(embeddings_enabled=False, embedding_dim=512)
    db = sqlite3.connect(":memory:")
    store = EmbeddingStore(db, config)
    assert store._dim == 512
    db.close()


def test_config_provider_used_by_store():
    config = DaemonConfig(embeddings_enabled=False, embedding_provider="voyage")
    db = sqlite3.connect(":memory:")
    store = EmbeddingStore(db, config)
    assert store._provider == "voyage"
    db.close()


# ------------------------------------------------------------------ #
# Provider dispatch tests (mocked)
# ------------------------------------------------------------------ #


def _make_store(provider="ollama", dim=3, model="test-model"):
    config = DaemonConfig(
        embeddings_enabled=False,
        embedding_provider=provider,
        embedding_api_base="http://localhost:11434",
        embedding_dim=dim,
        embedding_model=model,
        embedding_batch_size=2,
    )
    db = sqlite3.connect(":memory:")
    store = EmbeddingStore(db, config)
    store.available = True
    store._dim_detected = dim > 0
    return store, db


def _mock_httpx_client(response_json):
    mock_response = MagicMock()
    mock_response.json.return_value = response_json
    mock_response.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


@pytest.mark.asyncio
async def test_ollama_provider_dispatch():
    store, db = _make_store("ollama", dim=3)
    mock_client = _mock_httpx_client({"embeddings": [[0.1, 0.2, 0.3]]})
    import httpx
    with patch.object(httpx, "AsyncClient", return_value=mock_client):
        result = await store.embed_text("hello")
        assert result == [0.1, 0.2, 0.3]
        call_url = mock_client.post.call_args[0][0]
        assert "/api/embed" in call_url
    db.close()


@pytest.mark.asyncio
async def test_voyage_provider_dispatch():
    store, db = _make_store("voyage", dim=3, model="voyage-code-3")
    store._api_key = "test-key"
    mock_client = _mock_httpx_client({"data": [{"embedding": [0.4, 0.5, 0.6]}]})
    import httpx
    with patch.object(httpx, "AsyncClient", return_value=mock_client):
        result = await store.embed_text("hello")
        assert result == [0.4, 0.5, 0.6]
        call_url = mock_client.post.call_args[0][0]
        assert "voyageai.com" in call_url
    db.close()


@pytest.mark.asyncio
async def test_openai_provider_dispatch():
    store, db = _make_store("openai", dim=3)
    store._api_key = "test-key"
    mock_client = _mock_httpx_client({"data": [{"embedding": [0.7, 0.8, 0.9]}]})
    import httpx
    with patch.object(httpx, "AsyncClient", return_value=mock_client):
        result = await store.embed_text("hello")
        assert result == [0.7, 0.8, 0.9]
        call_url = mock_client.post.call_args[0][0]
        assert "/v1/embeddings" in call_url
    db.close()


@pytest.mark.asyncio
async def test_provider_fallback_to_hash():
    store, db = _make_store("ollama", dim=64)
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=Exception("connection refused"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    import httpx
    with patch.object(httpx, "AsyncClient", return_value=mock_client):
        result = await store.embed_text("hello")
        assert result is not None
        assert len(result) == 64
    db.close()


@pytest.mark.asyncio
async def test_batch_embedding_with_ollama():
    store, db = _make_store("ollama", dim=3)
    mock_client = _mock_httpx_client({"embeddings": [[0.1, 0.2, 0.3]]})
    import httpx
    with patch.object(httpx, "AsyncClient", return_value=mock_client):
        result = await store.embed_batch(["hello", "world"])
        assert result is not None
        assert len(result) == 2
    db.close()


@pytest.mark.asyncio
async def test_batch_falls_back_to_hash():
    store, db = _make_store("ollama", dim=64)
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=Exception("API error"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    import httpx
    with patch.object(httpx, "AsyncClient", return_value=mock_client):
        result = await store.embed_batch(["hello", "world"])
        assert result is not None
        assert len(result) == 2
        assert len(result[0]) == 64
    db.close()


# ------------------------------------------------------------------ #
# Auto-dimension detection tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_auto_dim_detection():
    store, db = _make_store("ollama", dim=0)
    assert store._dim_detected is False
    mock_client = _mock_httpx_client({"embeddings": [[0.1, 0.2, 0.3, 0.4]]})
    import httpx
    with patch.object(httpx, "AsyncClient", return_value=mock_client):
        result = await store.embed_text("hello")
        assert result is not None
        assert len(result) == 4
        assert store._dim == 4
        assert store._dim_detected is True
    db.close()


# ------------------------------------------------------------------ #
# Dimension validation tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_api_rejects_dimension_mismatch():
    store, db = _make_store("voyage", dim=1024, model="voyage-code-3")
    store._api_key = "test-key"
    mock_response = MagicMock()
    mock_response.json.return_value = {"data": [{"embedding": [0.1] * 384}]}
    mock_response.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    import httpx
    with patch.object(httpx, "AsyncClient", return_value=mock_client):
        result = await store.embed_text("hello")
        assert result is not None  # falls back to hash
    db.close()


# ------------------------------------------------------------------ #
# Index and search integration tests
# ------------------------------------------------------------------ #


@pytest.fixture
def mock_store():
    config = DaemonConfig(
        embeddings_enabled=True,
        embedding_dim=64,
        embedding_similarity_threshold=0.0,
    )
    db = sqlite3.connect(":memory:")
    store = EmbeddingStore(db, config)
    store.available = False
    yield store
    db.close()


@pytest.mark.asyncio
async def test_index_chunk_when_unavailable(mock_store):
    result = await mock_store.index_chunk("test", "some content")
    assert result is False


@pytest.mark.asyncio
async def test_embed_batch_when_unavailable(mock_store):
    result = await mock_store.embed_batch(["hello", "world"])
    assert result is None


@pytest.mark.asyncio
async def test_index_file_incremental_missing_file(mock_store, tmp_path):
    result = await mock_store.index_file_incremental(
        tmp_path / "nonexistent.md", "test"
    )
    assert result == 0


# ------------------------------------------------------------------ #
# Content hash tracking tests
# ------------------------------------------------------------------ #


def test_content_hash_tracking_schema():
    config = DaemonConfig(embeddings_enabled=False)
    db = sqlite3.connect(":memory:")
    store = EmbeddingStore(db, config)
    db.execute("""
        CREATE TABLE IF NOT EXISTS embedding_meta (
            path TEXT PRIMARY KEY,
            content_hash TEXT,
            model TEXT,
            indexed_at TEXT
        )
    """)
    db.execute(
        "INSERT INTO embedding_meta VALUES (?, ?, ?, ?)",
        ("/test/path", "abc123", "nomic-embed-text", "2024-01-01"),
    )
    row = db.execute("SELECT * FROM embedding_meta WHERE path = ?", ("/test/path",)).fetchone()
    assert row is not None
    assert row[1] == "abc123"
    db.close()


# ------------------------------------------------------------------ #
# Memory file indexing tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_index_memory_file_skips_missing(tmp_path):
    config = DaemonConfig(embeddings_enabled=False)
    db = sqlite3.connect(":memory:")
    store = EmbeddingStore(db, config)
    result = await store.index_memory_file(tmp_path / "no.md", "test")
    assert result == 0
    db.close()


@pytest.mark.asyncio
async def test_reindex_all_when_unavailable(tmp_path):
    config = DaemonConfig(embeddings_enabled=False)
    db = sqlite3.connect(":memory:")
    store = EmbeddingStore(db, config)
    result = await store.reindex_all(tmp_path / "agents", tmp_path / "shared")
    assert result == 0
    db.close()
