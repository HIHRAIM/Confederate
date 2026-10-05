"""Ensure short appeal history retention cannot erase ordinary relay rows."""
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from db import messages
from db.ranges import bridge_range

class AppealRetentionTests(unittest.TestCase):
    """Use an in-memory database, never the production bridge database."""

    def test_only_old_appeal_mappings_are_removed(self):
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        for table in ("message_copies", "media_group_members", "gallery_uploads"):
            cur.execute(f"CREATE TABLE {table} (message_id INTEGER)")
        cur.execute("CREATE TABLE messages (id INTEGER, bridge_id INTEGER, created_at INTEGER)")
        cur.execute("CREATE TABLE sponsor_appeal_usage (id INTEGER)")
        cur.execute("CREATE TABLE sponsor_appeal_reservations (id INTEGER)")
        floor, _ceiling = bridge_range("appeal")
        old = int(time.time()) - 9 * 86400
        recent = int(time.time()) - 86400
        cur.executemany("INSERT INTO messages VALUES (?,?,?)", (
            (1, floor, old), (2, floor, recent), (3, floor - 1, old)))
        for table in ("message_copies", "media_group_members", "gallery_uploads"):
            cur.executemany(f"INSERT INTO {table} VALUES (?)", ((1,), (2,), (3,)))
        with patch.object(messages, "conn", conn), patch.object(messages, "cur", cur):
            self.assertEqual(messages.cleanup_appeal_message_history(days=7), 1)
        for table in ("messages", "message_copies", "media_group_members", "gallery_uploads"):
            key = "id" if table == "messages" else "message_id"
            self.assertEqual([row[0] for row in cur.execute(
                f"SELECT {key} FROM {table} ORDER BY {key}")], [2, 3])
        conn.close()

if __name__ == "__main__":
    unittest.main()
