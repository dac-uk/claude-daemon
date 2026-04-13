"""EmbeddingStore — semantic memory search using vector embeddings.

Provides cosine similarity search over memory, playbooks, reflections,
and conversation excerpts. Uses sqlite-vec for storage and the Voyage AI
embedding API (with hash-based fallback when no API key is available).

Graceful degradation: if sqlite-vec is not installed, all methods return
empty results and the FTS5 keyword search continues to work as before.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_daemon.core.config import DaemonConfig

log = logging.getLogger(__name__)

# Fallback dimension if config is unavailable
DEFAULT_DIM = 1024


def _serialize_f32(vec: list[float]) -> bytes:
    """Serialize a float vector to bytes for sqlite-vec."""
    return struct.pack(f"{len(vec)}f", *vec)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _resolve_api_key() -> str | None:
    """Resolve Voyage API key from environment with fallback chain."""
    key = os.environ.get("VOYAGE_API_KEY")
    if key:
        return key
    # Backward compatibility: some users may have ANTHROPIC_API_KEY set to a Voyage key
    fallback = os.environ.get("ANTHROPIC_API_KEY")
    if fallback:
        log.warning(
            "Using ANTHROPIC_API_KEY for Voyage embeddings (deprecated). "
            "Set VOYAGE_API_KEY instead for correct behaviour."
        )
        return fallback
    return None


class EmbeddingStore:
    """Vector embedding index for semantic memory search.

    Uses sqlite-vec extension for vector storage. Falls back gracefully
    if the extension is not available.
    """

    def __init__(self, db: sqlite3.Connection, config: DaemonConfig) -> None:
        self._db = db
        self.config = config
        self.available = False
        self._dim = config.embedding_dim if config else DEFAULT_DIM
        self._api_key = _resolve_api_key()

        if not config.embeddings_enabled:
            return

        # Try to load sqlite-vec extension
        try:
            import sqlite_vec  # noqa: F401
            db.enable_load_extension(True)
            sqlite_vec.load(db)
            db.enable_load_extension(False)
            self._init_schema()
            self.available = True
            log.info(
                "EmbeddingStore initialized (model=%s, dim=%d, key=%s)",
                config.embedding_model, self._dim,
                "set" if self._api_key else "NOT SET — using hash fallback",
            )
        except (ImportError, Exception) as e:
            log.info("sqlite-vec not available, semantic search disabled: %s", e)

    def _init_schema(self) -> None:
        """Create the vector table if it doesn't exist.

        Handles dimension mismatches when the embedding model changes by
        detecting incompatible tables and recreating them.
        """
        # Check for dimension mismatch in existing table
        try:
            row = self._db.execute(
                "SELECT embedding FROM memory_vec LIMIT 1"
            ).fetchone()
            if row and row[0]:
                existing_dim = len(row[0]) // 4  # float32 = 4 bytes each
                if existing_dim != self._dim:
                    log.warning(
                        "Dimension mismatch: table has %d, config wants %d. "
                        "Dropping and recreating memory_vec.",
                        existing_dim, self._dim,
                    )
                    self._db.execute("DROP TABLE IF EXISTS memory_vec")
                    self._db.execute("DELETE FROM embedding_meta")
                    self._db.commit()
        except Exception:
            pass  # Table doesn't exist yet — fine

        self._db.executescript(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(
                embedding float[{self._dim}],
                +source TEXT,
                +agent_name TEXT,
                +chunk TEXT,
                +created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS embedding_meta (
                path TEXT PRIMARY KEY,
                content_hash TEXT,
                model TEXT,
                indexed_at TEXT
            );
        """)
        self._db.commit()

        # Check if model changed — force full reindex on next reindex_all
        try:
            row = self._db.execute(
                "SELECT model FROM embedding_meta LIMIT 1"
            ).fetchone()
            if row and row[0] and row[0] != self.config.embedding_model:
                log.info(
                    "Embedding model changed from %s to %s — will force full reindex",
                    row[0], self.config.embedding_model,
                )
                self._db.execute("DELETE FROM memory_vec")
                self._db.execute("DELETE FROM embedding_meta")
                self._db.commit()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Embedding generation
    # ------------------------------------------------------------------ #

    async def embed_text(self, text: str) -> list[float] | None:
        """Generate embedding for a single text chunk.

        Tries Voyage API first, falls back to hash-based embedding.
        """
        if not self.available:
            return None

        if self._api_key:
            try:
                return await self._embed_via_api([text[:8000]])
            except Exception as e:
                log.debug("Voyage API embedding failed, using hash fallback: %s", e)

        return self._embed_hash(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]] | None:
        """Generate embeddings for multiple texts in a single API call.

        Voyage API supports up to 128 inputs per request. Falls back to
        hash-based embedding if the API call fails.
        """
        if not self.available or not texts:
            return None

        batch_size = self.config.embedding_batch_size
        all_embeddings: list[list[float]] = []

        if self._api_key:
            try:
                for i in range(0, len(texts), batch_size):
                    batch = [t[:8000] for t in texts[i:i + batch_size]]
                    embeddings = await self._embed_batch_via_api(batch)
                    all_embeddings.extend(embeddings)
                return all_embeddings
            except Exception as e:
                log.debug("Batch embedding via API failed, using hash fallback: %s", e)

        # Hash fallback for all texts
        return [self._embed_hash(t) for t in texts]

    async def _embed_via_api(self, texts: list[str]) -> list[float]:
        """Generate embedding for a single text via Voyage API. Returns one vector."""
        embeddings = await self._embed_batch_via_api(texts[:1])
        return embeddings[0]

    async def _embed_batch_via_api(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a batch of texts via Voyage API."""
        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.voyageai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self.config.embedding_model,
                    "input": texts,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            embeddings = [item["embedding"] for item in data["data"]]

            # Validate dimensions
            if embeddings and len(embeddings[0]) != self._dim:
                log.error(
                    "Dimension mismatch: model %s returned %d dims, expected %d. "
                    "Update embedding_dim in config.",
                    self.config.embedding_model, len(embeddings[0]), self._dim,
                )
                raise ValueError(
                    f"Embedding dimension mismatch: got {len(embeddings[0])}, "
                    f"expected {self._dim}"
                )
            return embeddings

    def _embed_hash(self, text: str) -> list[float]:
        """Very simple hash-based embedding as a last resort.

        This is NOT a real embedding model -- it just provides stable
        hash-based vectors so the infrastructure works. Quality is poor
        but it enables testing the pipeline without external deps.
        """
        vec = [0.0] * self._dim
        words = text.lower().split()
        for word in words:
            h = int(hashlib.md5(word.encode()).hexdigest(), 16)
            idx = h % self._dim
            vec[idx] += 1.0

        # Normalize
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec

    # ------------------------------------------------------------------ #
    # Indexing
    # ------------------------------------------------------------------ #

    async def index_chunk(
        self, source: str, chunk: str, agent_name: str = "",
    ) -> bool:
        """Embed and store a single memory chunk. Returns True if indexed."""
        if not self.available or not chunk.strip():
            return False

        embedding = await self.embed_text(chunk[:2000])
        if not embedding:
            return False

        now = datetime.now(timezone.utc).isoformat()

        try:
            self._db.execute(
                "INSERT INTO memory_vec (embedding, source, agent_name, chunk, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (_serialize_f32(embedding), source, agent_name, chunk[:2000], now),
            )
            self._db.commit()
            return True
        except Exception as e:
            log.debug("Failed to index chunk: %s", e)
            return False

    async def index_chunks_batch(
        self, items: list[tuple[str, str, str]],
    ) -> int:
        """Batch-embed and store multiple chunks in a single transaction.

        Each item is (source, chunk, agent_name).
        Returns count of chunks successfully indexed.
        """
        if not self.available or not items:
            return 0

        texts = [chunk[:2000] for _, chunk, _ in items]
        embeddings = await self.embed_batch(texts)
        if not embeddings or len(embeddings) != len(items):
            return 0

        now = datetime.now(timezone.utc).isoformat()
        indexed = 0
        try:
            for (source, chunk, agent_name), embedding in zip(items, embeddings):
                self._db.execute(
                    "INSERT INTO memory_vec "
                    "(embedding, source, agent_name, chunk, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (_serialize_f32(embedding), source, agent_name, chunk[:2000], now),
                )
                indexed += 1
            self._db.commit()
        except Exception as e:
            log.debug("Batch index failed: %s", e)
            self._db.rollback()
        return indexed

    async def index_memory_file(self, path: Path, source: str, agent_name: str = "") -> int:
        """Chunk and index a markdown memory file. Returns count of chunks indexed."""
        if not self.available or not path.exists():
            return 0

        content = path.read_text()
        chunks = self._chunk_markdown(content, self.config.embedding_chunk_size)
        if not chunks:
            return 0

        items = [(source, chunk, agent_name) for chunk in chunks]
        return await self.index_chunks_batch(items)

    async def index_file_incremental(
        self, path: Path, source: str, agent_name: str = "",
    ) -> int:
        """Index a file only if its content has changed since last index.

        Uses SHA-256 content hashing to avoid re-embedding unchanged files.
        Returns count of chunks indexed (0 if skipped).
        """
        if not self.available or not path.exists():
            return 0

        content = path.read_text()
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        path_key = str(path)

        # Check if already indexed with same content
        try:
            row = self._db.execute(
                "SELECT content_hash FROM embedding_meta WHERE path = ?",
                (path_key,),
            ).fetchone()
            if row and row[0] == content_hash:
                return 0  # Unchanged — skip
        except Exception:
            pass

        # Content changed — delete old chunks and re-index
        try:
            self._db.execute(
                "DELETE FROM memory_vec WHERE source = ? AND agent_name = ?",
                (source, agent_name),
            )
            self._db.commit()
        except Exception:
            pass

        chunks = self._chunk_markdown(content, self.config.embedding_chunk_size)
        if not chunks:
            return 0

        items = [(source, chunk, agent_name) for chunk in chunks]
        indexed = await self.index_chunks_batch(items)

        # Record the hash
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO embedding_meta (path, content_hash, model, indexed_at) "
                "VALUES (?, ?, ?, ?)",
                (path_key, content_hash, self.config.embedding_model, now),
            )
            self._db.commit()
        except Exception as e:
            log.debug("Failed to update embedding_meta: %s", e)

        return indexed

    # ------------------------------------------------------------------ #
    # Search
    # ------------------------------------------------------------------ #

    async def search(
        self, query: str, top_k: int | None = None, min_score: float | None = None,
    ) -> list[dict]:
        """Semantic search returning ranked results above the similarity threshold."""
        if not self.available:
            return []

        if top_k is None:
            top_k = self.config.embedding_top_k
        if min_score is None:
            min_score = self.config.embedding_similarity_threshold

        embedding = await self.embed_text(query)
        if not embedding:
            return []

        try:
            rows = self._db.execute(
                "SELECT chunk, source, agent_name, distance "
                "FROM memory_vec "
                "WHERE embedding MATCH ? "
                "ORDER BY distance LIMIT ?",
                (_serialize_f32(embedding), top_k),
            ).fetchall()
            results = []
            for row in rows:
                score = 1.0 - row[3]  # Convert distance to similarity
                if score >= min_score:
                    results.append({
                        "chunk": row[0],
                        "source": row[1],
                        "agent_name": row[2],
                        "score": score,
                    })
            return results
        except Exception as e:
            log.debug("Semantic search failed: %s", e)
            return []

    # ------------------------------------------------------------------ #
    # Bulk reindex
    # ------------------------------------------------------------------ #

    async def reindex_all(self, agents_dir: Path, shared_dir: Path) -> int:
        """Incremental reindex from all memory sources.

        Only re-embeds files whose content has changed since the last index.
        Forces a full reindex if the embedding model has changed.
        Returns total chunks indexed.
        """
        if not self.available:
            return 0

        total = 0

        # Index per-agent memories
        if agents_dir.is_dir():
            for agent_dir in agents_dir.iterdir():
                if not agent_dir.is_dir():
                    continue
                name = agent_dir.name
                for fname in ("MEMORY.md", "REFLECTIONS.md"):
                    total += await self.index_file_incremental(
                        agent_dir / fname,
                        fname.lower().replace(".md", ""),
                        name,
                    )

        # Index shared knowledge
        if shared_dir.is_dir():
            for md_file in (shared_dir / "playbooks").glob("*.md"):
                total += await self.index_file_incremental(md_file, "playbook")
            lessons = shared_dir / "failure-lessons.md"
            if lessons.exists():
                total += await self.index_file_incremental(lessons, "failure_lesson")
            learnings = shared_dir / "learnings.md"
            if learnings.exists():
                total += await self.index_file_incremental(learnings, "learnings")

        if total:
            log.info("Reindexed %d memory chunks (incremental)", total)
        else:
            log.debug("Reindex: all files unchanged, nothing to do")
        return total

    # ------------------------------------------------------------------ #
    # Chunking
    # ------------------------------------------------------------------ #

    @staticmethod
    def _chunk_markdown(text: str, max_chunk: int = 500) -> list[str]:
        """Split markdown into chunks at section boundaries."""
        if not text.strip():
            return []

        # Split on markdown headers
        sections: list[str] = []
        current: list[str] = []
        for line in text.split("\n"):
            if line.startswith("## ") and current:
                sections.append("\n".join(current))
                current = [line]
            else:
                current.append(line)
        if current:
            sections.append("\n".join(current))

        # Further split large sections
        chunks: list[str] = []
        for section in sections:
            if len(section) <= max_chunk:
                if section.strip():
                    chunks.append(section.strip())
            else:
                # Split on paragraph boundaries
                paragraphs = section.split("\n\n")
                buf = ""
                for para in paragraphs:
                    if len(buf) + len(para) > max_chunk and buf:
                        chunks.append(buf.strip())
                        buf = para
                    else:
                        buf = f"{buf}\n\n{para}" if buf else para
                if buf.strip():
                    chunks.append(buf.strip())

        return chunks
