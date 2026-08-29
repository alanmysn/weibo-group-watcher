"""面板服务：本地网页界面（Flask + SSE 实时流）。

路由：
  GET /                面板页面（panel.html）
  GET /api/messages    ?before=<消息ID>&limit=<n>  向上翻页取历史
  GET /api/stream      SSE 实时流（新消息逐条推送）
  GET /api/state       运行状态（在线/重连中/钥匙过期 + 库内总数）
"""
import hashlib
import json
import logging
import os
import queue
import re
import threading
import time
from urllib.parse import quote, unquote

import requests
from flask import Flask, Response, abort, jsonify, request, send_file

import runtime_state
import store

log = logging.getLogger("watcher.web")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
AVATAR_DIR = os.path.join(DATA_DIR, "avatars")
EMOJI_DIR = os.path.join(DATA_DIR, "emotions")
EMOJI_MAP_FILE = os.path.join(DATA_DIR, "emojis.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

app = Flask(__name__, static_folder=None)

# --- SSE 广播中心：采集器落库 → broadcast() → 各面板连接 ---
_subscribers = set()
_sub_lock = threading.Lock()

MSG_FIELDS = ("id", "gid", "from_uid", "from_name", "avatar_url",
              "content", "url_objects", "type", "media_type", "time",
              "recall_status")


def broadcast(msg: dict):
    with _sub_lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(msg)
        except queue.Full:
            pass


def _subscribe():
    q = queue.Queue(maxsize=200)
    with _sub_lock:
        _subscribers.add(q)
    return q


def _unsubscribe(q):
    with _sub_lock:
        _subscribers.discard(q)


def _row_to_dict(row):
    return dict(zip(MSG_FIELDS, row))


@app.route("/")
def index():
    with open("panel.html", encoding="utf-8") as f:
        return f.read()


@app.route("/api/messages")
def api_messages():
    before = request.args.get("before", type=int)
    limit = min(request.args.get("limit", 50, type=int), 200)
    conn = store.get_conn()
    try:
        if before:
            rows = conn.execute(
                f"SELECT {','.join(MSG_FIELDS)} FROM messages "
                "WHERE id < ? ORDER BY id DESC LIMIT ?",
                (before, limit)).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {','.join(MSG_FIELDS)} FROM messages "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    msgs = [_row_to_dict(r) for r in rows]
    msgs.reverse()  # 统一为旧→新
    return jsonify({"messages": msgs, "has_more": len(rows) == limit})


@app.route("/api/stream")
def api_stream():
    q = _subscribe()

    def gen():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    msg = q.get(timeout=20)
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"  # 心跳，保持连接不被掐
        finally:
            _unsubscribe(q)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@app.route("/api/state")
def api_state():
    s = runtime_state.snapshot()
    return jsonify(s)


def start(cfg, host="127.0.0.1", port=8765):
    """在后台线程启动面板服务（主线程留给采集器）。"""
    app.config["COOKIE_PATH"] = cfg["cookie_path"]
    os.makedirs(AVATAR_DIR, exist_ok=True)
    os.makedirs(EMOJI_DIR, exist_ok=True)
    t = threading.Thread(
        target=lambda: app.run(host=host, port=port, debug=False,
                               use_reloader=False, threaded=True),
        daemon=True,
    )
    t.start()
    log.info("面板服务已启动: http://%s:%s （仅本机可访问）", host, port)


# --- 头像懒缓存：data/avatars/<uid>.<ext> ---

def _latest_avatar_url(uid):
    conn = store.get_conn()
    try:
        row = conn.execute(
            "SELECT avatar_url FROM messages WHERE from_uid=? "
            "AND avatar_url IS NOT NULL AND avatar_url != '' "
            "ORDER BY time DESC LIMIT 1", (uid,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _download_image(url, path):
    r = requests.get(url, headers={"User-Agent": UA,
                                   "Referer": "https://api.weibo.com/chat"},
                     timeout=15)
    if r.status_code != 200:
        return False
    with open(path, "wb") as f:
        f.write(r.content)
    return True


@app.route("/api/avatar/<uid>")
def api_avatar(uid):
    """按用户 UID 取头像：本地有缓存直接给，没有则按库内最新地址下载。"""
    if not re.fullmatch(r"\d+", uid or ""):
        abort(404)
    cached = None
    for ext in ("png", "jpg", "gif"):
        p = os.path.join(AVATAR_DIR, f"{uid}.{ext}")
        if os.path.exists(p):
            cached = p
            break
    if cached:
        return send_file(cached, max_age=3600)
    url = _latest_avatar_url(uid)
    if not url:
        abort(404)
    ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
    if ext not in ("png", "jpg", "gif"):
        ext = "jpg"
    path = os.path.join(AVATAR_DIR, f"{uid}.{ext}")
    if not _download_image(url, path):
        abort(404)
    return send_file(path, max_age=3600)


# --- 表情：data/emojis.json（映射表，7 天刷新）+ data/emotions/（图片懒缓存）---

def _cookie_header():
    with open(app.config.get("COOKIE_PATH", ""), encoding="utf-8") as f:
        cookies = json.load(f)
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def _emoji_map(force=False):
    """短语 → 原图 URL 映射；本地缓存 7 天，过期自动重拉。"""
    if (not force and os.path.exists(EMOJI_MAP_FILE)
            and time.time() - os.path.getmtime(EMOJI_MAP_FILE) < 7 * 86400):
        with open(EMOJI_MAP_FILE, encoding="utf-8") as f:
            return json.load(f)
    try:
        r = requests.get("https://api.weibo.com/webim/emotions.json",
                         params={"source": "209678993"},
                         headers={"Cookie": _cookie_header(),
                                  "User-Agent": UA,
                                  "Referer": "https://api.weibo.com/chat"},
                         timeout=15)
        items = r.json()
        mp = {it.get("phrase", ""): it.get("url", "")
              for it in items if it.get("phrase") and it.get("url")}
        if mp:
            with open(EMOJI_MAP_FILE, "w", encoding="utf-8") as f:
                json.dump(mp, f, ensure_ascii=False)
            log.info("表情映射表已更新：%d 个", len(mp))
        return mp
    except Exception as e:
        log.warning("表情映射拉取失败（%s），沿用旧表", e)
        if os.path.exists(EMOJI_MAP_FILE):
            with open(EMOJI_MAP_FILE, encoding="utf-8") as f:
                return json.load(f)
        return {}


@app.route("/api/emojis")
def api_emojis():
    """面板用：短语 → 本站表情地址。"""
    mp = _emoji_map()
    return jsonify({p: f"/api/emoji/{quote(p, safe='')}"
                     for p in mp})


@app.route("/api/emoji/<path:name>")
def api_emoji(name):
    """表情图片懒缓存：data/emotions/<md5>.<ext>"""
    phrase = unquote(name)
    mp = _emoji_map()
    url = mp.get(phrase)
    if not url:
        abort(404)
    digest = hashlib.md5(phrase.encode("utf-8")).hexdigest()
    for ext in ("png", "gif", "jpg", "webp"):
        p = os.path.join(EMOJI_DIR, f"{digest}.{ext}")
        if os.path.exists(p):
            return send_file(p, max_age=86400 * 7)
    ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
    if ext not in ("png", "gif", "jpg", "webp"):
        ext = "png"
    path = os.path.join(EMOJI_DIR, f"{digest}.{ext}")
    if not _download_image(url, path):
        abort(404)
    return send_file(path, max_age=86400 * 7)
