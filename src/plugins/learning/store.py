"""SQLite-backed learning-state entries (keywords + one-line description)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

MAX_KEYWORDS = 8
MAX_DESCRIPTION = 300


class LearningStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS entries ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "keywords TEXT NOT NULL,"
                "description TEXT NOT NULL,"
                "created_at REAL NOT NULL,"
                "updated_at REAL NOT NULL)"
            )
            self._conn.commit()
        return self._conn

    @staticmethod
    def _clean_keywords(keywords: list[str] | str) -> list[str]:
        items = keywords if isinstance(keywords, list) else str(keywords).split(",")
        cleaned: list[str] = []
        for item in items:
            text = str(item).strip()
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned[:MAX_KEYWORDS]

    def search(self, keywords: list[str] | str, *, limit: int = 20) -> list[dict[str, Any]]:
        cleaned = self._clean_keywords(keywords)
        if not cleaned:
            return []
        with self._lock:
            conn = self._connection()
            clauses = []
            params: list[Any] = []
            for keyword in cleaned:
                clauses.append("keywords LIKE ?")
                params.append(f"%{keyword.lower()}%")
            sql = (
                "SELECT id, keywords, description, updated_at FROM entries WHERE "
                + " OR ".join(clauses)
                + " ORDER BY updated_at DESC LIMIT ?"
            )
            params.append(max(1, min(50, int(limit))))
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "id": row[0],
                "keywords": json.loads(row[1]),
                "description": row[2],
                "updated_at": row[3],
            }
            for row in rows
        ]

    def upsert(
        self, keywords: list[str] | str, description: str, *, mode: str = "insert"
    ) -> dict[str, Any]:
        cleaned = self._clean_keywords(keywords)
        text = " ".join(str(description or "").split())[:MAX_DESCRIPTION]
        if not cleaned or not text:
            return {"ok": False, "message": "keywords 和 description 都不能为空"}
        now = time.time()
        with self._lock:
            conn = self._connection()
            target = None
            if mode == "replace":
                for keyword in cleaned:
                    row = conn.execute(
                        "SELECT id FROM entries WHERE keywords LIKE ? ORDER BY updated_at DESC LIMIT 1",
                        (f"%{keyword.lower()}%",),
                    ).fetchone()
                    if row:
                        target = row[0]
                        break
            if target is not None:
                conn.execute(
                    "UPDATE entries SET keywords=?, description=?, updated_at=? WHERE id=?",
                    (json.dumps(cleaned, ensure_ascii=False), text, now, target),
                )
                conn.commit()
                return {"ok": True, "id": target, "message": f"已更新学情条目 #{target}"}
            cursor = conn.execute(
                "INSERT INTO entries (keywords, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (json.dumps(cleaned, ensure_ascii=False), text, now, now),
            )
            conn.commit()
            return {"ok": True, "id": cursor.lastrowid, "message": "已新增学情条目"}

    def update_description(self, entry_id: int, description: str) -> bool:
        text = " ".join(str(description or "").split())[:MAX_DESCRIPTION]
        if int(entry_id) <= 0 or not text:
            return False
        with self._lock:
            conn = self._connection()
            cursor = conn.execute(
                "UPDATE entries SET description=?, updated_at=? WHERE id=?",
                (text, time.time(), int(entry_id)),
            )
            conn.commit()
            return cursor.rowcount > 0

    def remove(self, entry_id: int) -> bool:
        with self._lock:
            conn = self._connection()
            cursor = conn.execute("DELETE FROM entries WHERE id=?", (int(entry_id),))
            conn.commit()
            return cursor.rowcount > 0

    def items(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection().execute(
                "SELECT id, keywords, description, updated_at FROM entries ORDER BY updated_at DESC"
            ).fetchall()
        return [
            {
                "id": row[0],
                "keywords": json.loads(row[1]),
                "description": row[2],
                "updated_at": row[3],
            }
            for row in rows
        ]


__all__ = ["LearningStore", "MAX_KEYWORDS", "MAX_DESCRIPTION"]
