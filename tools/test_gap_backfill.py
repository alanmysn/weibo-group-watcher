import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backfill
import store
import web


class FakeResponse:
    def __init__(self, ids):
        self._ids = ids

    def json(self):
        return {"messages": [{"id": mid, "time": mid} for mid in self._ids]}


class GapBackfillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = store.DB_PATH
        store.DB_PATH = os.path.join(self.tmp.name, "watcher.db")

    def tearDown(self):
        store.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _seed(self):
        store.init_db()
        conn = store.get_conn()
        try:
            base = int(time.time()) - 2000
            for mid, ts in ((100, base + 900), (151, base + 1100),
                            (200, base + 1300), (251, base + 1500)):
                conn.execute(
                    "INSERT INTO messages (id, gid, time) VALUES (?, 'g', ?)",
                    (mid, ts),
                )
            gap_a = conn.execute(
                "INSERT INTO gaps (start_ts, end_ts) VALUES (?, ?)",
                (base + 1000, base + 1090),
            ).lastrowid
            gap_b = conn.execute(
                "INSERT INTO gaps (start_ts, end_ts) VALUES (?, ?)",
                (base + 1400, base + 1490),
            ).lastrowid
            conn.commit()
            return gap_a, gap_b
        finally:
            conn.close()

    def test_old_gap_table_is_migrated(self):
        conn = sqlite3.connect(store.DB_PATH)
        conn.execute(
            "CREATE TABLE gaps (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "start_ts INTEGER NOT NULL, end_ts INTEGER, created_at TEXT)"
        )
        conn.commit()
        conn.close()

        store.init_db()
        conn = store.get_conn()
        try:
            columns = {row[1] for row in conn.execute(
                "PRAGMA table_info(gaps)"
            ).fetchall()}
        finally:
            conn.close()
        self.assertIn("dismissed_at", columns)
        self.assertIn("filled_at", columns)

    def test_latest_alert_can_be_dismissed_without_deleting(self):
        gap_a, gap_b = self._seed()
        self.assertEqual(gap_b, store.get_latest_gap_alert()["id"])
        self.assertTrue(store.dismiss_gap(gap_b, 1600))
        self.assertIsNone(store.get_latest_gap_alert())
        self.assertEqual(2, len(store.list_gaps()))
        conn = store.get_conn()
        try:
            new_gap = conn.execute(
                "INSERT INTO gaps (start_ts) VALUES (?)", (int(time.time()),)
            ).lastrowid
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(new_gap, store.get_latest_gap_alert()["id"])

    def test_gap_api_and_page(self):
        _gap_a, gap_b = self._seed()
        client = web.app.test_client()
        data = client.get("/api/gaps?days=7").get_json()
        self.assertEqual(gap_b, data["latest"]["id"])
        self.assertEqual(2, len(data["gaps"]))
        self.assertEqual(200, client.get("/gaps").status_code)
        self.assertEqual(
            200, client.post(f"/api/gaps/{gap_b}/dismiss").status_code
        )
        self.assertIsNone(client.get("/api/gaps").get_json()["latest"])

    def test_backfill_to_older_gap_fills_it_and_newer_gap(self):
        gap_a, gap_b = self._seed()

        pages = {
            251: range(201, 251),
            201: range(151, 201),
            151: range(101, 151),
            101: range(51, 101),
        }

        def fake_get(_url, params, headers, timeout):
            return FakeResponse(list(pages[params["max_mid"]]))

        with mock.patch.object(backfill, "_headers", return_value={}), \
                mock.patch.object(backfill.requests, "get", side_effect=fake_get), \
                mock.patch.object(backfill, "PAGE_INTERVAL", 0):
            result = backfill.backfill_to_gap(
                {"group_id": "g", "cookie_path": "unused"}, gap_a
            )

        self.assertTrue(result["complete"])
        self.assertEqual(100, result["inserted"])
        self.assertEqual(200, result["scanned"])
        self.assertEqual([gap_b, gap_a], result["filled_ids"])
        gaps = store.list_gaps()
        self.assertTrue(all(g["filled_at"] for g in gaps))

    def test_scan_limit_keeps_unfinished_older_gap_pending(self):
        gap_a, gap_b = self._seed()
        pages = {
            251: range(201, 251),
            201: range(151, 201),
        }

        def fake_get(_url, params, headers, timeout):
            return FakeResponse(list(pages[params["max_mid"]]))

        with mock.patch.object(backfill, "_headers", return_value={}), \
                mock.patch.object(backfill.requests, "get", side_effect=fake_get), \
                mock.patch.object(backfill, "PAGE_INTERVAL", 0), \
                mock.patch.object(backfill, "MAX_PER_RUN", 100), \
                mock.patch.object(backfill.log, "warning"):
            result = backfill.backfill_to_gap(
                {"group_id": "g", "cookie_path": "unused"}, gap_a
            )

        self.assertFalse(result["complete"])
        self.assertEqual([gap_b], result["filled_ids"])
        states = {g["id"]: g["filled_at"] for g in store.list_gaps()}
        self.assertIsNotNone(states[gap_b])
        self.assertIsNone(states[gap_a])


if __name__ == "__main__":
    unittest.main()
