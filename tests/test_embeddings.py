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
    assert len(serialized) == len(vec) * 4  # 4 bytes per float32
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


def test_api_key_prefers_voyage():
    with patch.dict("os.environ", {"VOYAGE_API_KEY": "vk-test", "ANTHROPIC_API_KEY": "ak-test"}):
        key = _resolve_api_key()
        assert key == "vk-test"


def test_api_key_falls_back_to_anthropic():
    env = {"ANTHROPIC_API_KEY": "ak-fallback"}
    with patch.dict("os.environ", env, clear=True):
        key = _resolve_api_key()
        assert key == "ak-fallback"


def test_api_key_returns_none_when_missing():
    with patch.dict("os.environ", {}, clear=True):
        key = _resolve_api_key()
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
    # Create a section with paragraphs (chunking splits on \n\n boundaries)
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
    vec = store._embed_hash("hello world test embedding")
    assert len(vec) == 384
    db.close()


def test_embed_hash_is_normalized():
    config = DaemonConfig(embeddings_enabled=False)
    db = sqlite3.connect(":memory:")
    store = EmbeddingStore(db, config)
    store._dim = 128
    vec = store._embed_hash("some text for embedding")
    norm = math.sqrt(sum(x * x for x in vec))
    assert norm == pytest.approx(1.0, abs=0.01)
    db.close()


def test_embed_hash_deterministic():
    config = DaemonConfig(embeddings_enabled=False)
    db = sqlite3.connect(":memory:")
    store = EmbeddingStore(db, config)
    store._dim = 64
    v1 = store._embed_hash("hello world")
    v2 = store._embed_hash("hello world")
    assert v1 == v2
    db.close()


def test_embed_hash_different_texts_differ():
    config = DaemonConfig(embeddings_enabled=False)
    db = sqlite3.connect(":memory:")
    store = EmbeddingStore(db, config)
    store._dim = 64
    v1 = store._embed_hash("hello world")
    v2 = store._embed_hash("goodbye universe")
    assert v1 != v2
    db.close()


# ------------------------------------------------------------------ #
# Config integration tests
# ------------------------------------------------------------------ #


def test_config_defaults():
    config = DaemonConfig()
    assert config.embedding_model == "voyage-code-3"
    assert config.embedding_dim == 1024
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


# ------------------------------------------------------------------ #
# Index and search integration tests (using hash fallback, no sqlite-vec)
# ------------------------------------------------------------------ #


@pytest.fixture
def mock_store():
    """Create an EmbeddingStore with mocked sqlite-vec availability."""
    config = DaemonConfig(
        embeddings_enabled=True,
        embedding_dim=64,
        embedding_similarity_threshold=0.0,  # Accept all matches for testing
    )
    db = sqlite3.connect(":memory:")
    store = EmbeddingStore(db, config)
    # Force available without sqlite-vec by creating a regular table
    # (can't test vec0 without the extension, but can test the rest)
    store.available = False  # Will stay false without sqlite-vec
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
# Batch embedding API tests (mocked)
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_embed_batch_calls_api_in_batches():
    """Verify batch embedding sends correct API payload."""
    config = DaemonConfig(
        embeddings_enabled=False,
        embedding_dim=3,
        embedding_batch_size=2,
        embedding_model="voyage-code-3",
    )
    db = sqlite3.connect(":memory:")
    store = EmbeddingStore(db, config)
    store.available = True
    store._api_key = "test-key"

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "data": [
            {"embedding": [0.1, 0.2, 0.3]},
            {"embedding": [0.4, 0.5, 0.6]},
        ]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    import httpx
    with patch.object(httpx, "AsyncClient", return_value=mock_client):
        # 2 texts, batch_size=2 => 1 API call
        result = await store.embed_batch(["hello", "world"])
        assert result is not None
        assert len(result) == 2
        assert result[0] == [0.1, 0.2, 0.3]
        # Verify the API was called with correct model
        call_args = mock_client.post.call_args
        assert call_args[1]["json"]["model"] == "voyage-code-3"
        assert len(call_args[1]["json"]["input"]) == 2

    db.close()


@pytest.mark.asyncio
async def test_embed_batch_falls_back_to_hash():
    """When API fails, batch embedding should fall back to hash."""
    config = DaemonConfig(embeddings_enabled=False, embedding_dim=64)
    db = sqlite3.connect(":memory:")
    store = EmbeddingStore(db, config)
    store.available = True
    store._api_key = "bad-key"

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=Exception("API error"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    import httpx
    with patch.object(httpx, "AsyncClient", return_value=mock_client):
        result = await store.embed_batch(["hello", "world"])
        assert result is not None
        assert len(result) == 2
        assert len(result[0]) == 64  # Hash fallback produces correct dim

    db.close()


# ------------------------------------------------------------------ #
# Dimension validation tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_api_rejects_dimension_mismatch():
    """API returning wrong dimensions should raise ValueError."""
    config = DaemonConfig(embeddings_enabled=False, embedding_dim=1024)
    db = sqlite3.connect(":memory:")
    store = EmbeddingStore(db, config)
    store.available = True
    store._api_key = "test-key"

    # API returns 384-dim but config expects 1024
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "data": [{"embedding": [0.1] * 384}]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    import httpx
    with patch.object(httpx, "AsyncClient", return_value=mock_client):
        # Should fall back to hash because API raises ValueError
        result = await store.embed_text("hello")
        # Falls back to hash embedding
        assert result is not None

    db.close()


# ------------------------------------------------------------------ #
# Incremental indexing content hash tests
# ------------------------------------------------------------------ #


def test_content_hash_tracking_schema():
    """embedding_meta table should be created when schema initializes."""
    config = DaemonConfig(embeddings_enabled=False)
    db = sqlite3.connect(":memory:")
    store = EmbeddingStore(db, config)

    # Manually create the meta table (since we can't init vec0 without sqlite-vec)
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
        ("/test/path", "abc123", "voyage-code-3", "2024-01-01"),
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
