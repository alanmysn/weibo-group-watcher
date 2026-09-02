import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import media_cache
import storage_cleanup
import store
import web


class StorageCleanupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = store.DB_PATH
        self.old_data_dir = store.DATA_DIR
        self.old_image_dir = media_cache.IMAGE_DIR
        store.DATA_DIR = self.tmp.name
        store.DB_PATH = os.path.join(self.tmp.name, "watcher.db")
        media_cache.IMAGE_DIR = os.path.join(self.tmp.name, "images")
        store.init_db()
        os.makedirs(media_cache.IMAGE_DIR, exist_ok=True)

    def tearDown(self):
        store.DB_PATH = self.old_db_path
        store.DATA_DIR = self.old_data_dir
        media_cache.IMAGE_DIR = self.old_image_dir
        self.tmp.cleanup()

    def _message(self, msg_id, timestamp, uid, name):
        conn = store.get_conn()
        try:
            conn.execute(
                "INSERT INTO messages (id,gid,from_uid,from_name,time) "
                "VALUES (?,'g',?,?,?)", (msg_id, uid, name, timestamp),
            )
            conn.commit()
        finally:
            conn.close()

    def _image(self, msg_id):
        path = os.path.join(media_cache.IMAGE_DIR, f"{msg_id}.jpg")
        with open(path, "wb") as file:
            file.write(b"img")
        conn = store.get_conn()
        try:
            conn.execute(
                "INSERT INTO images (msg_id,file_path,size_bytes) "
                "VALUES (?,?,3)", (msg_id, f"images/{msg_id}.jpg"),
            )
            conn.commit()
        finally:
            conn.close()
        return path

    def test_partial_cleanup_keeps_recent_data_and_trims_crossing_gap(self):
        now = time.mktime((2026, 9, 2, 12, 0, 0, 0, 0, -1))
        cutoff = int(now - 30 * 24 * 60 * 60)
        self._message(1, cutoff - 100, "100", "旧用户")
        self._message(2, cutoff + 100, "200", "新用户")
        old_image = self._image(1)
        new_image = self._image(2)
        conn = store.get_conn()
        try:
            conn.execute("INSERT INTO special_users (uid) VALUES ('100')")
            conn.execute(
                "INSERT INTO attachments (msg_id,fid) VALUES (1,'123')"
            )
            conn.execute(
                "INSERT INTO gaps (start_ts,end_ts) VALUES (?,?)",
                (cutoff - 300, cutoff - 200),
            )
            conn.execute(
                "INSERT INTO gaps (start_ts,end_ts) VALUES (?,?)",
                (cutoff - 100, cutoff + 100),
            )
            conn.execute(
                "INSERT INTO gaps (start_ts,end_ts) VALUES (?,?)",
                (cutoff + 200, cutoff + 300),
            )
            conn.execute(
                "UPDATE read_state SET last_read_msg_id=1 WHERE id=1"
            )
            conn.commit()
        finally:
            conn.close()

        result = storage_cleanup.cleanup(1, now=now)

        self.assertEqual(1, result["messages"])
        self.assertEqual(1, result["images"])
        self.assertEqual(1, result["attachments"])
        self.assertEqual(1, result["gaps_deleted"])
        self.assertEqual(1, result["gaps_trimmed"])
        self.assertFalse(os.path.exists(old_image))
        self.assertTrue(os.path.exists(new_image))
        conn = store.get_conn()
        try:
            self.assertEqual([(2,)], conn.execute(
                "SELECT id FROM messages ORDER BY id").fetchall())
            self.assertEqual([(cutoff,), (cutoff + 200,)], conn.execute(
                "SELECT start_ts FROM gaps ORDER BY start_ts").fetchall())
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM attachments").fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(1, store.get_last_read_id())
        self.assertEqual(1, store.count_unread())
        special = next(user for user in store.list_users()
                       if user["uid"] == "100")
        self.assertEqual("旧用户", special["name"])
        self.assertTrue(special["special"])

    def test_delete_all_requires_confirmation_and_preserves_special_list(self):
        self._message(3, 100, "300", "关注用户")
        store.set_special_user("300", True)
        conn = store.get_conn()
        try:
            conn.execute("INSERT INTO gaps (start_ts,end_ts) VALUES (10,20)")
            conn.commit()
        finally:
            conn.close()
        client = web.app.test_client()

        self.assertEqual(400, client.post(
            "/api/message-cleanup", json={"months": 0}
        ).status_code)
        response = client.post(
            "/api/message-cleanup", json={"months": 0, "confirmed": True}
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.get_json()["messages"])
        conn = store.get_conn()
        try:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM messages").fetchone()[0])
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM gaps").fetchone()[0])
        finally:
            conn.close()
        users = store.list_users()
        self.assertEqual("300", users[0]["uid"])
        self.assertEqual("关注用户", users[0]["name"])
        self.assertIsNone(store.get_last_read_id())


if __name__ == "__main__":
    unittest.main()
