from __future__ import annotations

import time
from pathlib import Path

import aiosqlite


class CoverrCache:
    def __init__(self, db_path: Path, ttl: int = 86400) -> None:
        self.db_path = db_path
        self.ttl = ttl
        self._inited = False

    async def init(self) -> None:
        if self._inited and self.db_path.exists():
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS coverr_cache(
                    keyword TEXT PRIMARY KEY,
                    video_id TEXT,
                    preview_url TEXT,
                    download_url TEXT,
                    is_vertical INTEGER,
                    created_at INTEGER
                )
                """
            )
            await db.commit()
        self._inited = True

    async def get(self, keyword: str) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM coverr_cache WHERE keyword=?", (keyword.lower(),)
            ) as cur:
                row = await cur.fetchone()
                if row is None:
                    return None
                created = row["created_at"]
                if time.time() - float(created) > self.ttl:
                    # expired
                    await db.execute("DELETE FROM coverr_cache WHERE keyword=?", (keyword.lower(),))
                    await db.commit()
                    return None
                return dict(row)

    async def set(
        self,
        keyword: str,
        video_id: str,
        preview_url: str,
        download_url: str = "",
        is_vertical: bool = False,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO coverr_cache
                (keyword, video_id, preview_url, download_url, is_vertical, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (keyword.lower(), video_id, preview_url, download_url, int(is_vertical), int(time.time())),
            )
            await db.commit()

    # Sync helpers for Celery (not async)

    def get_sync(self, keyword: str) -> dict | None:
        import sqlite3

        if not self.db_path.exists():
            return None
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM coverr_cache WHERE keyword=?", (keyword.lower(),))
        row = cur.fetchone()
        conn.close()
        if row is None:
            return None
        created = row["created_at"]  # type: ignore[index]
        if time.time() - float(created) > self.ttl:
            conn2 = sqlite3.connect(self.db_path)
            conn2.execute("DELETE FROM coverr_cache WHERE keyword=?", (keyword.lower(),))
            conn2.commit()
            conn2.close()
            return None
        return dict(row)

    def set_sync(
        self,
        keyword: str,
        video_id: str,
        preview_url: str,
        download_url: str = "",
        is_vertical: bool = False,
    ) -> None:
        import sqlite3

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS coverr_cache(
                keyword TEXT PRIMARY KEY,
                video_id TEXT,
                preview_url TEXT,
                download_url TEXT,
                is_vertical INTEGER,
                created_at INTEGER
            )
            """
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO coverr_cache
            (keyword, video_id, preview_url, download_url, is_vertical, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (keyword.lower(), video_id, preview_url, download_url, int(is_vertical), int(time.time())),
        )
        conn.commit()
        conn.close()
