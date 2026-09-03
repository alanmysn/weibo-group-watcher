"""第 10 步：PWA 静态资源与页面元数据自测。"""
import json
import os
import struct
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import web


class PwaTest(unittest.TestCase):
    def setUp(self):
        self.client = web.app.test_client()

    def test_manifest(self):
        with self.client.get("/app.webmanifest") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "application/manifest+json")
            data = json.loads(response.data)
        self.assertEqual(data["start_url"], "/")
        self.assertEqual(data["display"], "standalone")
        self.assertEqual(
            {icon["sizes"] for icon in data["icons"]},
            {"192x192", "512x512"},
        )

    def test_icons_are_expected_png_sizes(self):
        for size in (192, 512):
            with self.client.get(f"/app-icon-{size}.png") as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, "image/png")
                self.assertEqual(response.data[:8], b"\x89PNG\r\n\x1a\n")
                width, height = struct.unpack(">II", response.data[16:24])
            self.assertEqual((width, height), (size, size))
        self.assertEqual(self.client.get("/app-icon-256.png").status_code, 404)

    def test_all_pages_link_the_manifest(self):
        for path in ("/", "/settings", "/storage", "/gaps", "/export"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn('rel="manifest" href="/app.webmanifest"', html)
            self.assertIn('name="theme-color"', html)


if __name__ == "__main__":
    unittest.main()
