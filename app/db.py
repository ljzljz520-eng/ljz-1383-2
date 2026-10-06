import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


class Database:
    """sqlite3 连接封装：autocommit + 显式事务；所有并发守卫用条件 UPDATE 实现。"""

    def __init__(self, path: str = ":memory:"):
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def tx(self):
        """BEGIN IMMEDIATE：写事务，保证读-改-写之间不被其他写者插入。"""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()
