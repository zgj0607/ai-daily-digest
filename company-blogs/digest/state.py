from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class ArticleRow:
    source_id: str
    url: str
    title: str
    published: str
    hint_kind: str
    status: str
    attempts: int
    filepath: str
    error: str


class State:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._init()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def _init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY,
                bootstrapped INTEGER NOT NULL DEFAULT 0,
                last_run_at TEXT,
                last_error TEXT,
                watermark TEXT
            );
            CREATE TABLE IF NOT EXISTS articles (
                source_id TEXT NOT NULL,
                url TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                published TEXT NOT NULL DEFAULT '',
                hint_kind TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                filepath TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source_id, url)
            );
            """
        )
        self.conn.commit()

    def ensure_source(self, source_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO sources (id, bootstrapped) VALUES (?, 0)",
                (source_id,),
            )
            self.conn.commit()

    def note_discovered(
        self,
        source_id: str,
        url: str,
        title: str = "",
        published: str = "",
        hint_kind: str = "",
    ) -> None:
        with self._lock:
            existing = self.conn.execute(
                "SELECT status, title, published, hint_kind FROM articles WHERE source_id = ? AND url = ?",
                (source_id, url),
            ).fetchone()
            now = utc_now()
            if existing is None:
                self.conn.execute(
                    """
                    INSERT INTO articles (
                        source_id, url, title, published, hint_kind, status, attempts, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?)
                    """,
                    (source_id, url, title, published, hint_kind, now),
                )
                return
            if existing["status"] == "saved":
                return
            self.conn.execute(
                """
                UPDATE articles
                SET title = CASE WHEN ? != '' THEN ? ELSE title END,
                    published = CASE WHEN ? != '' THEN ? ELSE published END,
                    hint_kind = CASE WHEN ? != '' THEN ? ELSE hint_kind END,
                    updated_at = ?
                WHERE source_id = ? AND url = ? AND status != 'saved'
                """,
                (title, title, published, published, hint_kind, hint_kind, now, source_id, url),
            )

    def commit(self) -> None:
        with self._lock:
            self.conn.commit()

    def pending(self, source_id: str, max_attempts: int, limit: int | None) -> list[ArticleRow]:
        sql = """
            SELECT * FROM articles
            WHERE source_id = ?
              AND status != 'saved'
              AND attempts < ?
            ORDER BY CASE WHEN published = '' THEN 1 ELSE 0 END, published DESC, url
        """
        params: list[object] = [source_id, max_attempts]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._article(row) for row in rows]

    def mark_saved(
        self,
        source_id: str,
        url: str,
        title: str,
        published: str,
        filepath: str,
    ) -> None:
        with self._lock:
            self.conn.execute(
                """
                UPDATE articles
                SET status = 'saved', title = ?, published = ?, filepath = ?, error = '',
                    attempts = attempts + 1, updated_at = ?
                WHERE source_id = ? AND url = ?
                """,
                (title, published, filepath, utc_now(), source_id, url),
            )
            self.conn.execute(
                """
                UPDATE sources
                SET watermark = CASE
                    WHEN ? = '' THEN watermark
                    WHEN watermark IS NULL OR watermark = '' OR watermark < ? THEN ?
                    ELSE watermark
                END
                WHERE id = ?
                """,
                (published, published, published, source_id),
            )
            self.conn.commit()

    def mark_failed(self, source_id: str, url: str, error: str) -> None:
        with self._lock:
            self.conn.execute(
                """
                UPDATE articles
                SET status = 'failed', error = ?, attempts = attempts + 1, updated_at = ?
                WHERE source_id = ? AND url = ?
                """,
                (error[:500], utc_now(), source_id, url),
            )
            self.conn.commit()

    def saved_filepath(self, source_id: str, url: str) -> str:
        with self._lock:
            row = self.conn.execute(
                "SELECT filepath FROM articles WHERE source_id = ? AND url = ? AND status = 'saved'",
                (source_id, url),
            ).fetchone()
        return row["filepath"] if row else ""

    def finish_source(self, source_id: str, bootstrapped: bool, error: str = "") -> None:
        with self._lock:
            self.conn.execute(
                """
                UPDATE sources
                SET bootstrapped = CASE WHEN ? THEN 1 ELSE bootstrapped END,
                    last_run_at = ?,
                    last_error = ?
                WHERE id = ?
                """,
                (1 if bootstrapped else 0, utc_now(), error[:500], source_id),
            )
            self.conn.commit()

    def has_unfinished(self, source_id: str, max_attempts: int) -> bool:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT 1 FROM articles
                WHERE source_id = ? AND status != 'saved' AND attempts < ?
                LIMIT 1
                """,
                (source_id, max_attempts),
            ).fetchone()
        return row is not None

    def summary(self) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                """
                SELECT
                    s.id,
                    s.bootstrapped,
                    s.watermark,
                    s.last_run_at,
                    s.last_error,
                    SUM(CASE WHEN a.status = 'saved' THEN 1 ELSE 0 END) AS saved,
                    SUM(CASE WHEN a.status = 'pending' THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN a.status = 'failed' THEN 1 ELSE 0 END) AS failed
                FROM sources s
                LEFT JOIN articles a ON a.source_id = s.id
                GROUP BY s.id
                ORDER BY s.id
                """
            ).fetchall()

    @staticmethod
    def _article(row: sqlite3.Row) -> ArticleRow:
        return ArticleRow(
            source_id=row["source_id"],
            url=row["url"],
            title=row["title"],
            published=row["published"],
            hint_kind=row["hint_kind"],
            status=row["status"],
            attempts=row["attempts"],
            filepath=row["filepath"],
            error=row["error"],
        )
