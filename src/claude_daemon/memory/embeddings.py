"""EmbeddingStore — semantic memory search using vector embeddings.

Provides cosine similarity search over memory, playbooks, reflections,
and conversation excerpts. Uses sqlite-vec for storage and either
Anthropic's embedding API or a local model for embedding generation.

Graceful degradation: if sqlite-vec is not installed, all methods return
empty results and the FTS5 keyword search continues to work as before.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import struct
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_daemon.core.config import DaemonConfig

log = logging.getLogger(__name__)

# Default embedding dimension (Voyage-3-lite / all-MiniLM-L6-v2)
DEFAULT_DIM = 384


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


class EmbeddingStore:
    """Vector embedding index for semantic memory search.

    Uses sqlite-vec extension for vector storage. Falls back gracefully
    if the extension is not available.
    """

    def __init__(self, db: sqlite3.Connection, config: DaemonConfig) -> None:
        self._db = db
        self.config = config
        self.available = False
        self._dim = DEFAULT_DIM
        self._api_key: str | None = None

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
            log.info("EmbeddingStore initialized with sqlite-vec (dim=%d)", self._dim)
        except (ImportError, Exception) as e:
            log.info("sqlite-vec not available, semantic search disabled: %s", e)

    def _init_schema(self) -> None:
        """Create the vector table if it doesn't exist."""
        self._db.executescript(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(
                embedding float[{self._dim}],
                +source TEXT,
                +agent_name TEXT,
                +chunk TEXT,
                +created_at TEXT
            );
        """)
        self._db.commit()

    async def embed_text(self, text: str) -> list[float] | None:
        """Generate embedding for a text chunk.

        Tries Anthropic API first, falls back to simple TF hashing
        (not ideal but works without any dependencies).
        """
        if not self.available:
            return None

        # Try Anthropic API
        try:
            import os
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if api_key:
                return await self._embed_via_api(text, api_key)
        except Exception:
            pass

        # Fallback: simple hash-based embedding (poor quality but functional)
        return self._embed_hash(text)

    async def _embed_via_api(self, text: str, api_key: str) -> list[float]:
        """Generate embedding via Anthropic/Voyage API."""
        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.voyageai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "voyage-3-lite",
                    "input": [text[:8000]],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            embedding = data["data"][0]["embedding"]
            self._dim = len(embedding)
            return embedding

    def _embed_hash(self, text: str) -> list[float]:
        """Very simple hash-based embedding as a last resort.

        This is NOT a real embedding model — it just provides stable
        hash-based vectors so the infrastructure works. Quality is poor
        but it enables testing the pipeline without external deps.
        """
        import hashlib

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

    async def index_chunk(
        self, source: str, chunk: str, agent_name: str = "",
    ) -> bool:
        """Embed and store a memory chunk. Returns True if indexed."""
        if not self.available or not chunk.strip():
            return False

        embedding = await self.embed_text(chunk[:2000])
        if not embedding:
            return False

        from datetime import datetime, timezone
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

    async def search(self, query: str, top_k: int = 5) -> list[dict]:
        """Semantic search returning ranked results."""
        if not self.available:
            return []

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
            return [
                {
                    "chunk": row[0],
                    "source": row[1],
                    "agent_name": row[2],
                    "score": 1.0 - row[3],  # Convert distance to similarity
                }
                for row in rows
            ]
        except Exception as e:
            log.debug("Semantic search failed: %s", e)
            return []

    async def index_memory_file(self, path: Path, source: str, agent_name: str = "") -> int:
        """Chunk and index a markdown memory file. Returns count of chunks indexed."""
        if not self.available or not path.exists():
            return 0

        content = path.read_text()
        chunks = self._chunk_markdown(content)
        indexed = 0
        for chunk in chunks:
            if await self.index_chunk(source, chunk, agent_name):
                indexed += 1
        return indexed

    async def reindex_all(self, agents_dir: Path, shared_dir: Path) -> int:
        """Full reindex from all memory sources. Returns total chunks indexed."""
        if not self.available:
            return 0

        # Clear existing index
        try:
            self._db.execute("DELETE FROM memory_vec")
            self._db.commit()
        except Exception:
            pass

        total = 0

        # Index per-agent memories
        if agents_dir.is_dir():
            for agent_dir in agents_dir.iterdir():
                if not agent_dir.is_dir():
                    continue
                name = agent_dir.name
                for fname in ("MEMORY.md", "REFLECTIONS.md"):
                    total += await self.index_memory_file(
                        agent_dir / fname, fname.lower().replace(".md", ""), name,
                    )

        # Index shared knowledge
        if shared_dir.is_dir():
            for md_file in (shared_dir / "playbooks").glob("*.md"):
                total += await self.index_memory_file(md_file, "playbook")
            lessons = shared_dir / "failure-lessons.md"
            if lessons.exists():
                total += await self.index_memory_file(lessons, "failure_lesson")
            learnings = shared_dir / "learnings.md"
            if learnings.exists():
                total += await self.index_memory_file(learnings, "learnings")

        log.info("Reindexed %d memory chunks", total)
        return total

    @staticmethod
    def _chunk_markdown(text: str, max_chunk: int = 500) -> list[str]:
        """Split markdown into chunks at section boundaries."""
        if not text.strip():
            return []

        # Split on markdown headers
        sections = []
        current = []
        for line in text.split("\n"):
            if line.startswith("## ") and current:
                sections.append("\n".join(current))
                current = [line]
            else:
                current.append(line)
        if current:
            sections.append("\n".join(current))

        # Further split large sections
        chunks = []
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
