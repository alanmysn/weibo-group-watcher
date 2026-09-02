import csv
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import exporter
import store
import web


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = store.DB_PATH
        self.old_data_dir = store.DATA_DIR
        self.old_export_dir = exporter.EXPORT_DIR
        store.DATA_DIR = self.tmp.name
        store.DB_PATH = os.path.join(self.tmp.name, "watcher.db")
        exporter.EXPORT_DIR = os.path.join(self.tmp.name, "exports")
        store.init_db()
        conn = store.get_conn()
        try:
            conn.executemany(
                "INSERT INTO messages "
                "(id, gid, from_uid, from_name, content, type, media_type, time) "
                "VALUES (?, 'g', ?, ?, ?, ?, ?, ?)",
                [
                    (1, "100", "甲", "第一条正文", 321, 0,
                     self._ts(2026, 8, 30, 9)),
                    (2, "", "", "系统通知", 344, 0,
                     self._ts(2026, 8, 30, 10)),
                    (3, "200", "乙", "", 321, 1,
                     self._ts(2026, 8, 31, 11)),
                    (4, "100", "甲", "一条链接", 321, 14,
                     self._ts(2026, 8, 30, 12)),
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        store.DB_PATH = self.old_db_path
        store.DATA_DIR = self.old_data_dir
        exporter.EXPORT_DIR = self.old_export_dir
        self.tmp.cleanup()

    @staticmethod
    def _ts(year, month, day, hour):
        return int(datetime(year, month, day, hour).timestamp())

    def _message_count(self):
        conn = store.get_conn()
        try:
            return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            conn.close()

    def test_markdown_only_user_messages_excludes_system(self):
        result = exporter.create_export(
            "2026-08-30", "2026-08-30", category="user", fmt="md"
        )
        self.assertEqual(2, result["count"])
        with open(result["path"], encoding="utf-8") as file:
            text = file.read()
        self.assertIn("第一条正文", text)
        self.assertIn("一条链接", text)
        self.assertNotIn("系统通知", text)

    def test_csv_can_filter_by_stable_uid(self):
        result = exporter.create_export(
            "2026-08-30", "2026-08-31", uid="100", fmt="csv"
        )
        with open(result["path"], encoding="utf-8-sig", newline="") as file:
            rows = list(csv.DictReader(file))
        self.assertEqual(["1", "4"], [row["id"] for row in rows])
        self.assertTrue(all(row["from_uid"] == "100" for row in rows))

    def test_jsonl_can_filter_image_messages(self):
        result = exporter.create_export(
            "2026-08-31", "2026-08-31", category="image", fmt="jsonl"
        )
        with open(result["path"], encoding="utf-8") as file:
            rows = [json.loads(line) for line in file]
        self.assertEqual([3], [row["id"] for row in rows])
        self.assertEqual("图片", rows[0]["message_type"])

    def test_api_creates_and_downloads_export(self):
        client = web.app.test_client()
        response = client.post("/api/exports", json={
            "start_date": "2026-08-30", "end_date": "2026-08-30",
            "category": "all", "format": "md",
        })
        self.assertEqual(200, response.status_code)
        data = response.get_json()
        download = client.get(data["download_url"])
        self.assertEqual(200, download.status_code)
        self.assertIn("attachment", download.headers["Content-Disposition"])
        download.close()

    def test_invalid_filters_are_rejected(self):
        client = web.app.test_client()
        response = client.post("/api/exports", json={
            "start_date": "2026-08-31", "end_date": "2026-08-30",
        })
        self.assertEqual(400, response.status_code)
        self.assertEqual(404, client.get("/api/exports/../watcher.db").status_code)

    def test_deleting_export_does_not_change_database(self):
        before = self._message_count()
        result = exporter.create_export("2026-08-30", "2026-08-31")
        os.remove(result["path"])
        self.assertFalse(os.path.exists(result["path"]))
        self.assertEqual(before, self._message_count())


if __name__ == "__main__":
    unittest.main()
