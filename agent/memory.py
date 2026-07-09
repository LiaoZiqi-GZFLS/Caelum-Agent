"""Local memory store: SQLite + ChromaDB vector search."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chromadb import PersistentClient


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
    ) -> None:
        self.db_path = Path(db_path)
        self.skills_dir = Path(skills_dir)
        self.vector_dir = Path(vector_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.vector_dir.mkdir(parents=True, exist_ok=True)
        self._init_sqlite()
        self.chroma = PersistentClient(path=str(self.vector_dir))
        self.skill_collection = self.chroma.get_or_create_collection("skills")
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
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO audit_log (timestamp, level, actor, action, result) VALUES (?, ?, ?, ?, ?)",
                (self._now(), level, actor, action, result),
            )

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
