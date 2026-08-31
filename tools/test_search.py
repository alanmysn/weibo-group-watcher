import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import store
import web


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = store.DB_PATH
        self.old_data_dir = store.DATA_DIR
        store.DATA_DIR = self.tmp.name
        store.DB_PATH = os.path.join(self.tmp.name, "watcher.db")
        store.init_db()
        conn = store.get_conn()
        try:
            conn.executemany(
                "INSERT INTO messages "
                "(id, gid, from_uid, from_name, content, type, media_type, time) "
                "VALUES (?, 'g', ?, ?, ?, 321, 0, ?)",
                [
                    (1, "100", "甲", "普通内容", 1),
                    (2, "200", "乙", "带有百分号 100%", 2),
                    (3, "100", "甲", "目标消息", 3),
                    (4, "200", "乙", "另一条目标消息", 4),
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        store.DB_PATH = self.old_db_path
        store.DATA_DIR = self.old_data_dir
        self.tmp.cleanup()

    def test_keyword_search_returns_matching_messages(self):
        data = web.app.test_client().get("/api/messages?q=目标").get_json()
        self.assertEqual([3, 4], [message["id"] for message in data["messages"]])

    def test_percent_is_searched_as_literal_text(self):
        data = web.app.test_client().get("/api/messages?q=%25").get_json()
        self.assertEqual([2], [message["id"] for message in data["messages"]])

    def test_uid_filter_and_pagination_can_be_combined(self):
        data = web.app.test_client().get(
            "/api/messages?uid=100&before=3"
        ).get_json()
        self.assertEqual([1], [message["id"] for message in data["messages"]])

    def test_invalid_uid_is_rejected(self):
        response = web.app.test_client().get("/api/messages?uid=not-a-uid")
        self.assertEqual(400, response.status_code)


if __name__ == "__main__":
    unittest.main()
