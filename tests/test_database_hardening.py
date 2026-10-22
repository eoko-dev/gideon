"""Tests for versioned migrations, thread cleanup and the locked connection."""
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIRECTORY", tempfile.mkdtemp())

from src.utils.database import DatabaseManager
from src.utils.database.core import LockedConnection
from src.utils.database.schema import SchemaManager


class _TempDb(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        patcher = mock.patch("src.utils.database.core.DATA_DIRECTORY", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def open(self):
        db = DatabaseManager()
        self.addCleanup(db.close)
        return db


class MigrationTests(_TempDb):
    def test_fresh_database_is_at_latest_version(self):
        db = self.open()
        version = db._conn.execute("PRAGMA user_version;").fetchone()[0]
        self.assertEqual(version, SchemaManager.schema_version())

    def test_old_database_is_migrated_forward(self):
        # A pre-versioning database: THREADS without creator_id, user_version 0
        path = os.path.join(self.dir, "gideon_state.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE THREADS (thread_id TEXT PRIMARY KEY, channel_id TEXT, name TEXT, "
                     "created_at DATETIME NOT NULL, model TEXT, system_prompt TEXT);")
        conn.commit()
        conn.close()

        db = self.open()
        columns = [r[1] for r in db._conn.execute("PRAGMA table_info(THREADS)").fetchall()]
        self.assertIn("creator_id", columns)
        self.assertEqual(db._conn.execute("PRAGMA user_version;").fetchone()[0],
                         SchemaManager.schema_version())

    def test_migrations_run_once(self):
        db = self.open()
        with mock.patch.object(SchemaManager, "_migration_1_baseline") as baseline:
            db.run_migrations()
        baseline.assert_not_called()

    def test_busy_timeout_is_set(self):
        db = self.open()
        self.assertEqual(db._conn.execute("PRAGMA busy_timeout;").fetchone()[0], 5000)


class ThreadCleanupTests(_TempDb):
    def setUp(self):
        super().setUp()
        self.db = self.open()
        old = datetime.now() - timedelta(days=30)
        for thread_id in ("quiet", "busy"):
            self.db.add_thread(thread_id, "c1", thread_id, old)
            self.db.add_message(role="user", content="hello", timestamp=old, thread_id=thread_id)
        # The busy thread had a message yesterday
        self.db.add_message(role="user", content="still here", timestamp=datetime.now() - timedelta(days=1),
                            thread_id="busy")

    def _thread_message_count(self, thread_id):
        return self.db._conn.execute("SELECT COUNT(*) FROM MESSAGES WHERE thread_id = ?",
                                     (thread_id,)).fetchone()[0]

    def test_prune_keeps_active_threads_and_removes_quiet_ones_with_messages(self):
        deleted = self.db.prune_old_threads(datetime.now() - timedelta(days=14))
        self.assertEqual(deleted, 1)
        self.assertIsNone(self.db.get_thread_info("quiet"))
        self.assertIsNotNone(self.db.get_thread_info("busy"))
        self.assertEqual(self._thread_message_count("quiet"), 0)
        self.assertEqual(self._thread_message_count("busy"), 2)

    def test_delete_thread_removes_its_messages(self):
        self.assertTrue(self.db.threads.delete_thread("busy"))
        self.assertEqual(self._thread_message_count("busy"), 0)
        self.assertEqual(self._thread_message_count("quiet"), 1)


class LockedConnectionTests(unittest.TestCase):
    def test_transactions_do_not_interleave_across_threads(self):
        raw = sqlite3.connect(":memory:", check_same_thread=False)
        conn = LockedConnection(raw)
        conn.execute("CREATE TABLE t (v INTEGER)")
        conn.commit()

        inside = threading.Event()
        release = threading.Event()

        def failing_transaction():
            try:
                with conn:
                    conn.execute("INSERT INTO t VALUES (1)")
                    inside.set()
                    release.wait(2)
                    raise RuntimeError("roll back")
            except RuntimeError:
                pass

        worker = threading.Thread(target=failing_transaction)
        worker.start()
        inside.wait(2)

        def committed_write():
            with conn:
                conn.execute("INSERT INTO t VALUES (2)")

        writer = threading.Thread(target=committed_write)
        writer.start()
        # The writer must wait for the failing transaction instead of joining it
        writer.join(0.2)
        self.assertTrue(writer.is_alive())
        release.set()
        worker.join(2)
        writer.join(2)

        self.assertEqual([r[0] for r in conn.execute("SELECT v FROM t").fetchall()], [2])


if __name__ == "__main__":
    unittest.main()
