"""
Database helpers: connection pool and bulk-insert helpers.
All writes are buffered and flushed periodically to reduce DB pressure.
"""
import asyncio
import logging
import os
import time
from collections import deque

import asyncpg

log = logging.getLogger(__name__)


async def create_pool(database_url: str | None = None) -> asyncpg.Pool:
    url = database_url or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set. Copy config/secrets.env.example → .env and fill in.")
    return await asyncpg.create_pool(url, min_size=2, max_size=10)


class BufferedWriter:
    """Buffers rows and bulk-inserts them into Postgres on a timer or when full."""

    def __init__(self, pool: asyncpg.Pool, batch_size: int = 100, interval_ms: int = 500):
        self._pool = pool
        self._batch_size = batch_size
        self._interval_ms = interval_ms
        self._buffers: dict[str, deque] = {}
        self._insert_sqls: dict[str, str] = {}
        self._last_flush = time.time()

    def register_table(self, name: str, insert_sql: str) -> None:
        self._buffers[name] = deque()
        self._insert_sqls[name] = insert_sql

    def enqueue(self, table: str, row: tuple) -> None:
        self._buffers[table].append(row)

    async def flush_if_ready(self) -> None:
        now = time.time()
        for table, buf in self._buffers.items():
            should_flush = (
                len(buf) >= self._batch_size
                or (buf and (now - self._last_flush) * 1000 >= self._interval_ms)
            )
            if should_flush:
                await self._flush(table)
        self._last_flush = now

    async def flush_all(self) -> None:
        for table in list(self._buffers.keys()):
            await self._flush(table)

    async def _flush(self, table: str) -> None:
        buf = self._buffers[table]
        if not buf:
            return
        rows = []
        while buf:
            rows.append(buf.popleft())
        sql = self._insert_sqls[table]
        try:
            async with self._pool.acquire() as conn:
                await conn.executemany(sql, rows)
            log.debug("Flushed %d rows to %s", len(rows), table)
        except Exception as exc:
            log.error("DB flush error for %s: %s — %d rows dropped", table, exc, len(rows))

    async def run_flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval_ms / 1000)
            await self.flush_if_ready()
