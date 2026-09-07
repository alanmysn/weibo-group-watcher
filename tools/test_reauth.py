import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collector
import reauth


class ReauthTest(unittest.TestCase):
    def test_cookie_detection_and_filtering(self):
        cookies = [
            {"domain": ".weibo.com", "name": name, "value": "value"}
            for name in ("SUB", "SUBP", "SSOLoginState")
        ]
        cookies.append(
            {"domain": ".example.com", "name": "other", "value": "secret"}
        )

        self.assertTrue(reauth._authenticated(cookies))
        slim = reauth._slim_weibo_cookies(cookies)
        self.assertEqual(3, len(slim))
        self.assertTrue(all("weibo" in cookie["domain"] for cookie in slim))

    def test_cookie_file_is_replaced_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "cookies.json")
            with open(path, "w", encoding="utf-8") as file:
                json.dump([{"name": "old"}], file)

            reauth._write_cookies_atomic(path, [{"name": "new"}])

            with open(path, encoding="utf-8") as file:
                self.assertEqual([{"name": "new"}], json.load(file))
            self.assertFalse(os.path.exists(path + ".refresh"))

    def test_successful_scan_saves_cookies_and_closes_browser(self):
        cookies = [
            {"domain": ".weibo.com", "name": name, "value": "value"}
            for name in ("SUB", "SUBP", "SSOLoginState")
        ]
        process = mock.Mock()
        process.poll.return_value = None

        def cdp_call(_url, method):
            if method == "Storage.getCookies":
                return {"cookies": cookies}
            return {}

        with mock.patch.object(reauth, "_find_chrome",
                               return_value="chrome.exe"), \
                mock.patch.object(reauth, "_free_local_port",
                                  return_value=9222), \
                mock.patch.object(reauth.subprocess, "Popen",
                                  return_value=process), \
                mock.patch.object(reauth, "_read_json", return_value={
                    "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/browser"
                }), \
                mock.patch.object(reauth, "_cdp_call",
                                  side_effect=cdp_call) as call, \
                mock.patch.object(reauth, "_write_cookies_atomic") as write:
            reauth.refresh_cookies("cookies.json")

        write.assert_called_once_with(
            "cookies.json", reauth._slim_weibo_cookies(cookies)
        )
        self.assertEqual("Browser.close", call.call_args_list[-1].args[1])
        process.wait.assert_called_once_with(timeout=5)

    def test_cookie_wait_can_be_stopped(self):
        stop_event = mock.Mock()
        stop_event.is_set.return_value = True
        self.assertFalse(
            reauth.wait_for_cookie_change("missing", 0, stop_event)
        )

    def test_auth_failure_refreshes_then_reconnects(self):
        with mock.patch.object(
                collector, "_run_session",
                side_effect=[collector.AuthExpired("403"), None]
        ) as run_session, \
                mock.patch.object(collector.reauth, "refresh_cookies") as refresh, \
                mock.patch.object(collector.runtime_state, "set_status") as status, \
                mock.patch.object(collector.store, "get_meta", return_value=None), \
                mock.patch.object(collector.store, "open_gap"):
            collector.start({"cookie_path": "cookies.json"})

        self.assertEqual(2, run_session.call_count)
        refresh.assert_called_once_with("cookies.json")
        status.assert_any_call("expired", "需重新扫码")
        status.assert_any_call("reconnecting", "扫码完成，正在重连")


if __name__ == "__main__":
    unittest.main()
