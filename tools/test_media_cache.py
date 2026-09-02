import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import media_cache
import store
import web


class FakeResponse:
    def __init__(self, data=None, body=b"", content_length=None,
                 content_type=None):
        self._data = data
        self._body = body
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        if content_type is not None:
            self.headers["Content-Type"] = content_type

    def raise_for_status(self):
        return None

    def json(self):
        return self._data

    def iter_content(self, _size):
        yield self._body

    def close(self):
        return None


class MediaCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = store.DB_PATH
        self.old_data_dir = store.DATA_DIR
        self.old_image_dir = media_cache.IMAGE_DIR
        store.DATA_DIR = self.tmp.name
        store.DB_PATH = os.path.join(self.tmp.name, "watcher.db")
        media_cache.IMAGE_DIR = os.path.join(self.tmp.name, "images")
        store.init_db()

    def tearDown(self):
        store.DB_PATH = self.old_db_path
        store.DATA_DIR = self.old_data_dir
        media_cache.IMAGE_DIR = self.old_image_dir
        self.tmp.cleanup()

    def _insert_message(self, msg_id, media_type, media_data, content=""):
        conn = store.get_conn()
        try:
            conn.execute(
                "INSERT INTO messages "
                "(id, gid, media_type, media_data, content, time) "
                "VALUES (?, 'g', ?, ?, ?, ?)",
                (msg_id, media_type, json.dumps(media_data), content, msg_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _insert_cached_image(self, msg_id, downloaded_at, body=b"img"):
        self._insert_message(msg_id, 1, {"fids": [str(msg_id)]})
        os.makedirs(media_cache.IMAGE_DIR, exist_ok=True)
        path = os.path.join(media_cache.IMAGE_DIR, f"{msg_id}.jpg")
        with open(path, "wb") as file:
            file.write(body)
        conn = store.get_conn()
        try:
            conn.execute(
                "INSERT INTO images "
                "(msg_id, file_path, downloaded_at, size_bytes) "
                "VALUES (?, ?, ?, ?)",
                (msg_id, f"images/{msg_id}.jpg", downloaded_at, len(body)),
            )
            conn.commit()
        finally:
            conn.close()
        return path

    def test_extracts_only_required_media_fields(self):
        raw = media_cache.extract_media_data({
            "fids": [123],
            "pic_infos": [{"pid": "p", "original_pic": "https://img/x.gif",
                           "ignored": "private"}],
            "annotations": {"video_pic_fid": 456, "ignored": "private"},
        })
        data = json.loads(raw)
        self.assertEqual(["123"], data["fids"])
        self.assertEqual("456", data["video_pic_fid"])
        self.assertNotIn("ignored", data["pic_infos"][0])

    def test_fid_image_is_cached_atomically(self):
        self._insert_message(1, 1, {"fids": ["123"]})

        def fake_get(url, **_kwargs):
            if url == media_cache.META_URL:
                return FakeResponse({"extension": "jpg", "filesize": 3})
            return FakeResponse(body=b"img", content_length=3,
                                content_type="image/jpeg")

        with mock.patch.object(media_cache.config, "load_config",
                               return_value={"cookie_path": "unused"}), \
                mock.patch.object(media_cache, "_headers", return_value={}), \
                mock.patch.object(media_cache.requests, "get", side_effect=fake_get):
            path = media_cache.cache_message(1)

        self.assertTrue(os.path.isfile(path))
        with open(path, "rb") as f:
            self.assertEqual(b"img", f.read())
        self.assertFalse(any(name.endswith(".part")
                             for name in os.listdir(media_cache.IMAGE_DIR)))

    def test_attachment_is_not_scheduled_for_automatic_download(self):
        with mock.patch.object(media_cache._executor, "submit") as submit:
            media_cache.schedule(2, 5)
        submit.assert_not_called()

    def test_attachment_name_is_sanitized_and_downloaded_on_demand(self):
        self._insert_message(2, 5, {"fids": ["456"]}, "fallback.pdf")

        def fake_get(url, **_kwargs):
            if url == media_cache.META_URL:
                return FakeResponse({"extension": "pdf", "filesize": 4,
                                     "filename": "../../unsafe.pdf"})
            return FakeResponse(body=b"file", content_length=4)

        with mock.patch.object(media_cache.config, "load_config",
                               return_value={"cookie_path": "unused"}), \
                mock.patch.object(media_cache, "_headers", return_value={}), \
                mock.patch.object(media_cache.requests, "get", side_effect=fake_get):
            client = web.app.test_client()
            info_response = client.get("/api/attachment-info/2")
            self.assertEqual(200, info_response.status_code)
            self.assertEqual("unsafe.pdf", info_response.get_json()["file_name"])
            self.assertEqual("available", info_response.get_json()["status"])
            self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "files")))

            response = client.get("/api/attachment/2")
            self.assertEqual(200, response.status_code)
            self.assertEqual(b"file", response.data)
            self.assertIn("attachment", response.headers["Content-Disposition"])
            response.close()
            self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "files")))

    def test_stream_limit_removes_partial_file(self):
        path = os.path.join(media_cache.IMAGE_DIR, "4.bin")
        response = FakeResponse(body=b"123", content_length=0)
        with mock.patch.object(media_cache.requests, "get",
                               return_value=response):
            with self.assertRaises(media_cache.TooLarge):
                media_cache._download("https://example.invalid/file", path,
                                      {}, 2)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(media_cache.IMAGE_DIR) and
                         any(name.endswith(".part")
                             for name in os.listdir(media_cache.IMAGE_DIR)))

    def test_old_attachment_reports_unavailable(self):
        self._insert_message(4, 5, {}, "old.pdf")
        conn = store.get_conn()
        try:
            conn.execute("UPDATE messages SET media_data=NULL WHERE id=4")
            conn.commit()
        finally:
            conn.close()
        data = web.app.test_client().get("/api/attachment-info/4").get_json()
        self.assertEqual("unavailable", data["status"])

    def test_cleanup_removes_only_images_older_than_90_days(self):
        old_path = self._insert_cached_image(10, "2026-05-01 00:00:00")
        new_path = self._insert_cached_image(11, "2026-08-20 00:00:00")
        now = time.mktime((2026, 9, 2, 12, 0, 0, 0, 0, -1))

        result = media_cache.cleanup_images(now=now)

        self.assertEqual({"deleted": 1, "size_bytes": 3}, result)
        self.assertFalse(os.path.exists(old_path))
        self.assertTrue(os.path.exists(new_path))
        conn = store.get_conn()
        try:
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(*) FROM images").fetchone()[0])
            self.assertEqual(2, conn.execute(
                "SELECT COUNT(*) FROM messages WHERE id IN (10,11)"
            ).fetchone()[0])
        finally:
            conn.close()

    def test_cleanup_api_reports_current_cache(self):
        self._insert_cached_image(12, "2026-08-20 00:00:00", b"1234")
        client = web.app.test_client()
        response = client.get("/api/image-cleanup")

        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.get_json()["count"])
        self.assertEqual(4, response.get_json()["size_bytes"])
        self.assertEqual(90, response.get_json()["retention_days"])

        now = time.mktime((2026, 9, 2, 12, 0, 0, 0, 0, -1))
        with mock.patch.object(media_cache.time, "time", return_value=now):
            result = client.post(
                "/api/image-cleanup", json={"months": 3}
            ).get_json()
        self.assertEqual(0, result["deleted"])
        self.assertEqual(0, result["freed_bytes"])
        self.assertEqual(4, result["size_bytes"])

    def test_manual_cleanup_accepts_month_range_or_all(self):
        self._insert_cached_image(13, "2026-05-01 00:00:00")
        self._insert_cached_image(14, "2026-08-20 00:00:00")
        now = time.mktime((2026, 9, 2, 12, 0, 0, 0, 0, -1))
        client = web.app.test_client()

        with mock.patch.object(media_cache.time, "time", return_value=now):
            response = client.post("/api/image-cleanup", json={"months": 2})
        self.assertEqual(1, response.get_json()["deleted"])

        response = client.post("/api/image-cleanup", json={"months": 0})
        self.assertEqual(1, response.get_json()["deleted"])
        self.assertEqual(0, response.get_json()["count"])

    def test_manual_cleanup_rejects_unknown_range(self):
        response = web.app.test_client().post(
            "/api/image-cleanup", json={"months": 6}
        )
        self.assertEqual(400, response.status_code)


if __name__ == "__main__":
    unittest.main()
