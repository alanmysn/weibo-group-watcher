"""微博登录态刷新：打开独立 Chrome 扫码，并在本机更新 Cookie。"""
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.request import urlopen

import websocket

log = logging.getLogger("watcher.reauth")

LOGIN_URL = "https://weibo.com/"
REQUIRED_COOKIE_NAMES = {"SUB", "SUBP", "SSOLoginState"}
POLL_INTERVAL = 1
START_TIMEOUT = 20


class ReauthError(RuntimeError):
    """扫码刷新无法完成。"""


def _find_chrome():
    candidates = [
        os.path.join(os.environ.get("PROGRAMFILES", ""),
                     "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""),
                     "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     "Google", "Chrome", "Application", "chrome.exe"),
        shutil.which("chrome"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    raise ReauthError("未找到 Google Chrome")


def _free_local_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _read_json(url):
    with urlopen(url, timeout=2) as response:
        return json.loads(response.read())


def _cdp_call(ws_url, method):
    ws = websocket.create_connection(ws_url, timeout=5, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": method}))
        while True:
            message = json.loads(ws.recv())
            if message.get("id") == 1:
                if "error" in message:
                    raise ReauthError("浏览器拒绝读取登录状态")
                return message.get("result", {})
    finally:
        ws.close()


def _authenticated(cookies):
    present = {cookie.get("name") for cookie in cookies
               if cookie.get("value")}
    return REQUIRED_COOKIE_NAMES <= present


def _slim_weibo_cookies(cookies):
    return [
        {
            "domain": cookie["domain"],
            "name": cookie["name"],
            "value": cookie["value"],
            "path": cookie.get("path", "/"),
            "expires": cookie.get("expires", -1),
        }
        for cookie in cookies
        if "weibo" in cookie.get("domain", "")
    ]


def _write_cookies_atomic(cookie_path, cookies):
    temporary_path = cookie_path + ".refresh"
    try:
        with open(temporary_path, "w", encoding="utf-8") as file:
            json.dump(cookies, file, ensure_ascii=False, indent=1)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, cookie_path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


def wait_for_cookie_change(cookie_path, previous_mtime, stop_event=None):
    """扫码窗口被关闭后保持进程存活，直到 Cookie 被其他方式更新。"""
    while True:
        if stop_event is not None and stop_event.is_set():
            return False
        try:
            if os.path.getmtime(cookie_path) != previous_mtime:
                return True
        except OSError:
            pass
        time.sleep(5)


def refresh_cookies(cookie_path):
    """打开一次扫码窗口；登录成功后原子替换 cookie_path。"""
    chrome_path = _find_chrome()
    port = _free_local_port()
    browser_url = f"http://127.0.0.1:{port}"

    with tempfile.TemporaryDirectory(
            prefix="weibo-login-", ignore_cleanup_errors=True) as profile_dir:
        process = subprocess.Popen(
            [
                chrome_path,
                f"--remote-debugging-port={port}",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-mode",
                LOGIN_URL,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        browser_ws_url = None
        try:
            deadline = time.time() + START_TIMEOUT
            while time.time() < deadline:
                if process.poll() is not None:
                    raise ReauthError("扫码窗口启动后立即退出")
                try:
                    browser_ws_url = _read_json(
                        browser_url + "/json/version"
                    )["webSocketDebuggerUrl"]
                    break
                except (OSError, KeyError, ValueError):
                    time.sleep(POLL_INTERVAL)
            if not browser_ws_url:
                raise ReauthError("扫码窗口启动超时")

            log.info("登录钥匙失效，已打开微博扫码窗口")
            while process.poll() is None:
                try:
                    cookies = _cdp_call(
                        browser_ws_url, "Storage.getCookies"
                    ).get("cookies", [])
                except (OSError, ValueError,
                        websocket.WebSocketException):
                    time.sleep(POLL_INTERVAL)
                    continue
                if _authenticated(cookies):
                    slim = _slim_weibo_cookies(cookies)
                    _write_cookies_atomic(cookie_path, slim)
                    log.info("扫码登录完成，钥匙已在本机更新")
                    return
                time.sleep(POLL_INTERVAL)
            raise ReauthError("扫码窗口已关闭，登录尚未完成")
        finally:
            if browser_ws_url:
                try:
                    _cdp_call(browser_ws_url, "Browser.close")
                except (OSError, ValueError, ReauthError,
                        websocket.WebSocketException):
                    pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
