"""EmbeddingStore — semantic memory search using vector embeddings.

Supports multiple embedding providers:
  - ollama:  Local Ollama server (default, no API key needed)
  - voyage:  Voyage AI embedding API
  - openai:  Any OpenAI-compatible embedding endpoint

Uses sqlite-vec for storage. Falls back to hash-based embeddings
when the provider is unreachable.

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


def _resolve_api_key(provider: str, config_key: str = "") -> str | None:
    """Resolve embedding API key for the given provider."""
    if config_key:
        return config_key
    if provider == "ollama":
        return None
    if provider == "voyage":
        key = os.environ.get("VOYAGE_API_KEY")
        if key:
            return key
        fallback = os.environ.get("ANTHROPIC_API_KEY")
        if fallback:
            log.warning(
                "Using ANTHROPIC_API_KEY for Voyage embeddings (deprecated). "
                "Set VOYAGE_API_KEY instead."
            )
            return fallback
        return None
    if provider == "openai":
        return os.environ.get("OPENAI_API_KEY")
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
        self._provider = config.embedding_provider if config else "ollama"
        self._api_base = config.embedding_api_base if config else "http://localhost:11434"
        self._dim = config.embedding_dim if config else DEFAULT_DIM
        self._dim_detected = self._dim > 0
        self._api_key = _resolve_api_key(
            self._provider, config.embedding_api_key if config else "",
        )

        if not config.embeddings_enabled:
            return

        if self._provider in ("voyage", "openai") and not self._api_key:
            log.warning(
                "Embedding provider '%s' requires an API key but none found. "
                "Set embedding_api_key in config or the appropriate env var.",
                self._provider,
            )

        try:
            import sqlite_vec  # noqa: F401
            db.enable_load_extension(True)
            sqlite_vec.load(db)
            db.enable_load_extension(False)
            if self._dim_detected:
                self._init_schema()
            else:
                self._init_meta_only()
            self.available = True
            dim_str = str(self._dim) if self._dim_detected else "auto"
            key_str = "n/a" if self._provider == "ollama" else (
                "set" if self._api_key else "NOT SET"
            )
            log.info(
                "EmbeddingStore initialized (provider=%s, model=%s, dim=%s, key=%s)",
                self._provider, config.embedding_model, dim_str, key_str,
            )
        except (ImportError, Exception) as e:
            log.info("sqlite-vec not available, semantic search disabled: %s", e)

    def _init_meta_only(self) -> None:
        """Create only the metadata table; vector table deferred until dim is known."""
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS embedding_meta ("
            "  path TEXT PRIMARY KEY, content_hash TEXT, model TEXT, indexed_at TEXT"
            ")"
        )
        self._db.commit()

    def _init_schema(self) -> None:
        """Create the vector table if it doesn't exist."""
        try:
            row = self._db.execute(
                "SELECT embedding FROM memory_vec LIMIT 1"
            ).fetchone()
            if row and row[0]:
                existing_dim = len(row[0]) // 4
                if existing_dim != self._dim:
                    log.warning(
                        "Dimension mismatch: table has %d, config wants %d. "
                        "Dropping and recreating memory_vec.",
                        existing_dim, self._dim,
                    )
                    self._db.execute("DROP TABLE IF EXISTS memory_vec")
                    self._db.execute("DROP TABLE IF EXISTS conv_vec")
                    self._db.execute("DELETE FROM embedding_meta")
                    self._db.executescript(
                        "DELETE FROM conv_indexing_watermark;"
                    )
                    self._db.commit()
        except Exception:
            pass

        self._db.executescript(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(
                embedding float[{self._dim}],
                +source TEXT,
                +agent_name TEXT,
                +chunk TEXT,
                +created_at TEXT
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS conv_vec USING vec0(
                embedding float[{self._dim}],
                +conversation_id INTEGER,
                +message_id INTEGER,
                +agent_name TEXT,
                +chunk TEXT,
                +role TEXT,
                +created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS embedding_meta (
                path TEXT PRIMARY KEY,
                content_hash TEXT,
                model TEXT,
                indexed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS conv_indexing_watermark (
                conversation_id INTEGER PRIMARY KEY,
                last_indexed_message_id INTEGER NOT NULL DEFAULT 0,
                agent_name TEXT,
                indexed_at TEXT
            );
        """)
        self._db.commit()

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
                self._db.execute("DELETE FROM conv_vec")
                self._db.execute("DELETE FROM embedding_meta")
                self._db.execute("DELETE FROM conv_indexing_watermark")
                self._db.commit()
        except Exception:
            pass

    def _ensure_dim(self, detected: int) -> None:
        """Set dimension from auto-detection and create the vector table."""
        if self._dim_detected:
            return
        self._dim = detected
        self._dim_detected = True
        log.info("Auto-detected embedding dimension: %d", detected)
        self._init_schema()

    # ------------------------------------------------------------------ #
    # Provider dispatch
    # ------------------------------------------------------------------ #

    async def _embed_via_provider(self, texts: list[str]) -> list[list[float]]:
        """Route embedding request to the configured provider."""
        if self._provider == "ollama":
            return await self._embed_ollama(texts)
        elif self._provider == "voyage":
            return await self._embed_voyage(texts)
        elif self._provider == "openai":
            return await self._embed_openai(texts)
        raise ValueError(f"Unknown embedding provider: {self._provider}")

    async def _embed_ollama(self, texts: list[str]) -> list[list[float]]:
        import httpx
        results: list[list[float]] = []
        async with httpx.AsyncClient(timeout=120) as client:
            for text in texts:
                resp = await client.post(
                    f"{self._api_base}/api/embed",
                    json={"model": self.config.embedding_model, "input": text},
                )
                resp.raise_for_status()
                data = resp.json()
                results.append(data["embeddings"][0])
        return results

    async def _embed_voyage(self, texts: list[str]) -> list[list[float]]:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.voyageai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self.config.embedding_model, "input": texts},
            )
            resp.raise_for_status()
            return [item["embedding"] for item in resp.json()["data"]]

    async def _embed_openai(self, texts: list[str]) -> list[list[float]]:
        import httpx
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self._api_base}/v1/embeddings",
                headers=headers,
                json={"model": self.config.embedding_model, "input": texts},
            )
            resp.raise_for_status()
            return [item["embedding"] for item in resp.json()["data"]]

    # ------------------------------------------------------------------ #
    # Embedding generation
    # ------------------------------------------------------------------ #

    async def embed_text(self, text: str) -> list[float] | None:
        """Generate embedding for a single text chunk."""
        if not self.available:
            return None
        try:
            vecs = await self._embed_via_provider([text[:8000]])
            vec = vecs[0]
            if not self._dim_detected:
                self._ensure_dim(len(vec))
            elif len(vec) != self._dim:
                log.error(
                    "Dimension mismatch: model %s returned %d dims, expected %d. "
                    "Update embedding_dim in config or set to 0 for auto-detect.",
                    self.config.embedding_model, len(vec), self._dim,
                )
                raise ValueError(f"Dimension mismatch: got {len(vec)}, expected {self._dim}")
            return vec
        except Exception as e:
            log.debug("Provider '%s' embedding failed, using hash fallback: %s", self._provider, e)
        return self._embed_hash(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]] | None:
        """Generate embeddings for multiple texts."""
        if not self.available or not texts:
            return None

        batch_size = self.config.embedding_batch_size
        all_embeddings: list[list[float]] = []

        try:
            for i in range(0, len(texts), batch_size):
                batch = [t[:8000] for t in texts[i:i + batch_size]]
                embeddings = await self._embed_via_provider(batch)
                if not self._dim_detected and embeddings:
                    self._ensure_dim(len(embeddings[0]))
                all_embeddings.extend(embeddings)
            return all_embeddings
        except Exception as e:
            log.debug("Batch embedding via '%s' failed, using hash fallback: %s", self._provider, e)

        if not self._dim_detected:
            self._ensure_dim(DEFAULT_DIM)
        return [self._embed_hash(t) for t in texts]

    def _embed_hash(self, text: str) -> list[float]:
        """Hash-based embedding fallback when the provider is unreachable."""
        dim = self._dim if self._dim_detected else DEFAULT_DIM
        vec = [0.0] * dim
        words = text.lower().split()
        for word in words:
            h = int(hashlib.md5(word.encode()).hexdigest(), 16)
            idx = h % dim
            vec[idx] += 1.0
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
        """Batch-embed and store multiple chunks in a single transaction."""
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
        """Chunk and index a markdown memory file."""
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
        """Index a file only if its content has changed since last index."""
        if not self.available or not path.exists():
            return 0

        content = path.read_text()
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        path_key = str(path)

        try:
            row = self._db.execute(
                "SELECT content_hash FROM embedding_meta WHERE path = ?",
                (path_key,),
            ).fetchone()
            if row and row[0] == content_hash:
                return 0
        except Exception:
            pass

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

        results = []
        serialized = _serialize_f32(embedding)

        try:
            rows = self._db.execute(
                "SELECT chunk, source, agent_name, distance "
                "FROM memory_vec "
                "WHERE embedding MATCH ? "
                "ORDER BY distance LIMIT ?",
                (serialized, top_k),
            ).fetchall()
            for row in rows:
                score = 1.0 - row[3]
                if score >= min_score:
                    results.append({
                        "chunk": row[0],
                        "source": row[1],
                        "agent_name": row[2],
                        "score": score,
                    })
        except Exception as e:
            log.debug("Semantic search (memory_vec) failed: %s", e)

        try:
            conv_rows = self._db.execute(
                "SELECT chunk, conversation_id, agent_name, role, created_at, distance "
                "FROM conv_vec "
                "WHERE embedding MATCH ? "
                "ORDER BY distance LIMIT ?",
                (serialized, top_k),
            ).fetchall()
            for row in conv_rows:
                base_score = 1.0 - row[5]
                created_at = row[4]
                score = base_score
                if created_at:
                    try:
                        ts = datetime.fromisoformat(
                            str(created_at).replace("Z", "+00:00")
                        )
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        age_hours = (
                            datetime.now(timezone.utc) - ts
                        ).total_seconds() / 3600
                        recency_boost = max(0.0, 0.15 * (1.0 - age_hours / 168))
                        score = min(1.0, base_score + recency_boost)
                    except (ValueError, TypeError):
                        pass
                if score >= min_score:
                    results.append({
                        "chunk": row[0],
                        "source": "conversation",
                        "agent_name": row[2],
                        "score": score,
                        "conversation_id": row[1],
                    })
        except sqlite3.OperationalError:
            pass
        except Exception as e:
            log.debug("Semantic search (conv_vec) failed: %s", e)

        seen: set[str] = set()
        deduped = []
        for r in sorted(results, key=lambda x: x["score"], reverse=True):
            if r["chunk"] not in seen:
                seen.add(r["chunk"])
                deduped.append(r)
            if len(deduped) >= top_k:
                break
        return deduped

    # ------------------------------------------------------------------ #
    # Bulk reindex
    # ------------------------------------------------------------------ #

    async def reindex_all(self, agents_dir: Path, shared_dir: Path) -> int:
        """Incremental reindex from all memory sources."""
        if not self.available:
            return 0

        total = 0

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

        chunks: list[str] = []
        for section in sections:
            if len(section) <= max_chunk:
                if section.strip():
                    chunks.append(section.strip())
            else:
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

    @staticmethod
    def _chunk_conversation_window(
        messages: list[dict], window_size: int = 3, agent_name: str = "",
    ) -> list[dict]:
        """Sliding window over messages producing embeddable conversation chunks.

        Window size = messages per chunk, stride = window_size - 1 (1-message overlap).
        """
        if len(messages) < window_size:
            if not messages:
                return []
            window_size = len(messages)

        stride = max(1, window_size - 1)
        chunks = []
        for i in range(0, len(messages) - window_size + 1, stride):
            window = messages[i:i + window_size]
            lines = []
            for m in window:
                role = "User" if m["role"] == "user" else "Assistant"
                content = (m.get("content") or "")[:600]
                lines.append(f"{role}: {content}")
            chunk_text = "\n".join(lines)[:1800]
            anchor = window[-1]
            chunks.append({
                "conversation_id": anchor["conversation_id"],
                "message_id": anchor["id"],
                "agent_name": agent_name,
                "chunk": chunk_text,
                "role": anchor["role"],
                "created_at": anchor.get("timestamp", ""),
            })
        return chunks

    def get_unindexed_messages(self, conversation_id: int) -> list[dict]:
        """Fetch messages above the indexing watermark for a conversation."""
        try:
            row = self._db.execute(
                "SELECT last_indexed_message_id FROM conv_indexing_watermark "
                "WHERE conversation_id = ?", (conversation_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return []
        watermark = row[0] if row else 0
        rows = self._db.execute(
            "SELECT id, conversation_id, role, content, timestamp "
            "FROM messages WHERE conversation_id = ? AND id > ? "
            "ORDER BY id ASC LIMIT 50",
            (conversation_id, watermark),
        ).fetchall()
        return [dict(r) for r in rows]

    async def index_conversation_messages(
        self, conversation_id: int, agent_name: str = "",
    ) -> int:
        """Index new messages from a conversation into conv_vec."""
        if not self.available:
            return 0
        if not getattr(self.config, "embedding_conv_enabled", True):
            return 0

        messages = self.get_unindexed_messages(conversation_id)
        window_size = getattr(self.config, "embedding_conv_window_size", 3)
        if not messages:
            return 0
        if len(messages) < window_size:
            row = self._db.execute(
                "SELECT last_indexed_message_id FROM conv_indexing_watermark "
                "WHERE conversation_id = ?", (conversation_id,),
            ).fetchone()
            if not row:
                return 0

        chunks = self._chunk_conversation_window(messages, window_size, agent_name)
        if not chunks:
            return 0

        now = datetime.now(timezone.utc).isoformat()
        indexed = 0
        for c in chunks:
            try:
                embedding = await self.embed_text(c["chunk"])
                if not embedding:
                    continue
                self._db.execute(
                    "INSERT INTO conv_vec (embedding, conversation_id, message_id, "
                    "agent_name, chunk, role, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        _serialize_f32(embedding),
                        c["conversation_id"], c["message_id"],
                        c["agent_name"], c["chunk"], c["role"], c["created_at"],
                    ),
                )
                indexed += 1
            except Exception:
                log.debug("Failed to index conversation chunk", exc_info=True)

        max_id = max(m["id"] for m in messages)
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO conv_indexing_watermark "
                "(conversation_id, last_indexed_message_id, agent_name, indexed_at) "
                "VALUES (?, ?, ?, ?)",
                (conversation_id, max_id, agent_name, now),
            )
            self._db.commit()
        except Exception:
            log.debug("Failed to update conv watermark", exc_info=True)

        return indexed

    async def reindex_conversations(self, days: int = 7) -> int:
        """Reindex recent conversations into conv_vec."""
        if not self.available:
            return 0
        if not getattr(self.config, "embedding_conv_enabled", True):
            return 0

        try:
            rows = self._db.execute(
                "SELECT id, user_id FROM conversations "
                "WHERE last_active >= datetime('now', ? || ' days') AND status = 'active'",
                (f"-{days}",),
            ).fetchall()
        except Exception:
            return 0

        total = 0
        for row in rows:
            user_id = row[1] if isinstance(row, (tuple, list)) else row["user_id"]
            conv_id = row[0] if isinstance(row, (tuple, list)) else row["id"]
            if ":spawn:" in str(user_id):
                continue
            agent_name = str(user_id).rsplit(":", 1)[-1] if ":" in str(user_id) else ""
            try:
                count = await self.index_conversation_messages(conv_id, agent_name)
                total += count
            except Exception:
                log.debug("Reindex failed for conv %d", conv_id, exc_info=True)
        return total
