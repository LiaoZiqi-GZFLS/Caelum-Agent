"""Local memory store: SQLite + ChromaDB vector search."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger("caelum.memory")

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_preferences (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reflections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_summary TEXT NOT NULL,
    failure_reason TEXT,
    fix_action TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    level TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    result TEXT
);
CREATE TABLE IF NOT EXISTS state_persistence (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class MemoryStore:
    def __init__(
        self,
        db_path: Path | str,
        skills_dir: Path | str,
        vector_dir: Path | str,
        audit_log_path: Path | str | None = None,
        kimi: Any | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.skills_dir = Path(skills_dir)
        self.vector_dir = Path(vector_dir)
        self.audit_log_path = Path(audit_log_path) if audit_log_path else None
        self.kimi = kimi
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.vector_dir.mkdir(parents=True, exist_ok=True)
        self._init_sqlite()
        # chromadb (~1s import + ONNX embedding model on first query) is
        # imported lazily so test collection and memory-free tasks stay cheap.
        from chromadb import PersistentClient
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

        self.chroma = PersistentClient(path=str(self.vector_dir))
        # Pin embeddings to CPUExecutionProvider: after setup.py swaps
        # onnxruntime for onnxruntime-directml, ChromaDB's default provider
        # list puts DmlExecutionProvider first, and two concurrent DirectML
        # sessions (this embedding model + RapidOCR's during perception)
        # crash onnxruntime natively with an access violation (0xc0000005).
        # Reproduced by scripts/repro_dml_crash.py --dml-embedding. The
        # all-MiniLM-L6-v2 model is tiny, so CPU embedding costs ~nothing.
        embedding_function = ONNXMiniLM_L6_V2(
            preferred_providers=["CPUExecutionProvider"]
        )
        try:
            self.skill_collection = self.chroma.get_or_create_collection(
                "skills",
                embedding_function=embedding_function,
            )
        except ValueError as exc:
            # One-time migration: collections created before the CPU pin
            # persist the "default" (DML-capable) EF and ChromaDB refuses to
            # open them with an explicit EF. The collection is just a cache
            # of skills/**/*.md — drop and recreate; sync_skills() below
            # repopulates it.
            if "Embedding function conflict" not in str(exc):
                raise
            logger.warning(
                "Recreating skills collection with CPU-pinned embedding "
                "function (previous collection used the default/DML-capable "
                "one); skills will be re-indexed"
            )
            self.chroma.delete_collection("skills")
            self.skill_collection = self.chroma.get_or_create_collection(
                "skills",
                embedding_function=embedding_function,
            )
        self.sync_skills()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _init_sqlite(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(SCHEMA)

    def set_preference(self, key: str, value: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO user_preferences (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, self._now()),
            )

    def get_preference(self, key: str, default: str | None = None) -> str | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM user_preferences WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else default

    async def aset_preference(self, key: str, value: str) -> None:
        if self.kimi is not None:
            try:
                await self.kimi.set_memory(key, value)
                return
            except Exception as exc:  # pragma: no cover - fallback path
                logger.warning("Kimi memory set failed, falling back to SQLite: %s", exc)
        self.set_preference(key, value)

    async def aget_preference(self, key: str, default: str | None = None) -> str | None:
        if self.kimi is not None:
            try:
                value = await self.kimi.get_memory_exact(key)
                if value is not None:
                    return value
            except Exception as exc:  # pragma: no cover - fallback path
                logger.warning("Kimi memory get failed, falling back to SQLite: %s", exc)
        return self.get_preference(key, default)

    def add_reflection(
        self, task_summary: str, failure_reason: str | None, fix_action: str | None
    ) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO reflections (task_summary, failure_reason, fix_action, created_at) VALUES (?, ?, ?, ?)",
                (task_summary, failure_reason, fix_action, self._now()),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    def list_reflections(self, limit: int = 20) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM reflections ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def audit(
        self, level: str, actor: str, action: str, result: str | None = None
    ) -> None:
        ts = self._now()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO audit_log (timestamp, level, actor, action, result) VALUES (?, ?, ?, ?, ?)",
                (ts, level, actor, action, result),
            )
        if self.audit_log_path is not None:
            self._append_audit_file(ts, level, actor, action, result)

    def _append_audit_file(
        self,
        ts: str,
        level: str,
        actor: str,
        action: str,
        result: str | None,
    ) -> None:
        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        line = f"{ts}\t{level}\t{actor}\t{action}\t{result or ''}\n"
        with self.audit_log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    def set_state(self, key: str, value: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO state_persistence (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, self._now()),
            )

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM state_persistence WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else default

    def delete_state(self, key: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM state_persistence WHERE key = ?", (key,))

    def sync_skills(self) -> None:
        if not self.skills_dir.exists():
            return
        docs: list[str] = []
        ids: list[str] = []
        metadatas: list[dict[str, Any]] = []
        with sqlite3.connect(self.db_path) as conn:
            for path in self.skills_dir.rglob("*.md"):
                name = path.relative_to(self.skills_dir).with_suffix("").as_posix()
                content = path.read_text(encoding="utf-8")
                conn.execute(
                    "INSERT OR REPLACE INTO skills (name, content, updated_at) VALUES (?, ?, ?)",
                    (name, content, self._now()),
                )
                docs.append(content)
                ids.append(name)
                metadatas.append({"name": name})
        if docs:
            self.skill_collection.upsert(documents=docs, ids=ids, metadatas=metadatas)

    def search_skills(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        results = self.skill_collection.query(query_texts=[query], n_results=top_k)
        items = []
        for idx, doc_id in enumerate(results.get("ids", [[]])[0]):
            items.append(
                {
                    "name": doc_id,
                    "content": results["documents"][0][idx],
                    "distance": results.get("distances", [[]])[0][idx] if results.get("distances") else None,
                }
            )
        return items
