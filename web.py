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

import media_cache
import runtime_state
import store
import exporter
import storage_cleanup

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

MESSAGE_FIELDS = ("id", "gid", "from_uid", "from_name", "avatar_url",
                  "content", "url_objects", "type", "media_type", "time",
                  "recall_status")
MSG_FIELDS = MESSAGE_FIELDS + ("attachment_name", "attachment_size",
                               "attachment_status")
MSG_SELECT = (",".join(f"m.{field}" for field in MESSAGE_FIELDS)
              + ",a.file_name,a.size_bytes,a.status")


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


def _like_pattern(text):
    text = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{text}%"


@app.route("/")
def index():
    with open("panel.html", encoding="utf-8") as f:
        return f.read()


@app.route("/app.webmanifest")
def app_manifest():
    return send_file(
        os.path.join(BASE_DIR, "app.webmanifest"),
        mimetype="application/manifest+json",
        max_age=3600,
    )


@app.route("/app-icon-<int:size>.png")
def app_icon(size):
    if size not in (192, 512):
        abort(404)
    return send_file(
        os.path.join(BASE_DIR, f"app-icon-{size}.png"),
        mimetype="image/png",
        max_age=86400,
    )


@app.route("/settings")
def settings():
    with open("settings.html", encoding="utf-8") as f:
        return f.read()


@app.route("/storage")
def storage_page():
    with open("storage.html", encoding="utf-8") as f:
        return f.read()


@app.route("/gaps")
def gaps_page():
    with open("gaps.html", encoding="utf-8") as f:
        return f.read()


@app.route("/export")
def export_page():
    with open("export.html", encoding="utf-8") as f:
        return f.read()


@app.route("/api/export-options")
def api_export_options():
    return jsonify({"users": store.list_users(),
                    "today": time.strftime("%Y-%m-%d")})


@app.route("/api/exports", methods=["POST"])
def api_create_export():
    data = request.get_json(silent=True) or {}
    try:
        result = exporter.create_export(
            data.get("start_date"), data.get("end_date"),
            data.get("uid", ""), data.get("category", "all"),
            data.get("format", "md"),
        )
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({"file_name": result["file_name"],
                    "count": result["count"],
                    "download_url": "/api/exports/" + result["file_name"]})


@app.route("/api/exports/<file_name>")
def api_download_export(file_name):
    if not re.fullmatch(
            r"messages_\d{8}_\d{8}_[a-f0-9]{8}\.(md|jsonl|csv)",
            file_name):
        abort(404)
    path = os.path.join(exporter.EXPORT_DIR, file_name)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=file_name)


@app.route("/api/messages")
def api_messages():
    before = request.args.get("before", type=int)
    limit = min(request.args.get("limit", 50, type=int), 200)
    query = request.args.get("q", "").strip()[:100]
    uid = request.args.get("uid", "").strip()
    if uid and not re.fullmatch(r"\d+", uid):
        abort(400)
    conditions = []
    params = []
    if before:
        conditions.append("m.id < ?")
        params.append(before)
    if query:
        conditions.append("COALESCE(m.content, '') LIKE ? ESCAPE '\\'")
        params.append(_like_pattern(query))
    if uid:
        conditions.append("m.from_uid = ?")
        params.append(uid)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(limit)
    conn = store.get_conn()
    try:
        rows = conn.execute(
            f"SELECT {MSG_SELECT} FROM messages m "
            "LEFT JOIN attachments a ON a.msg_id=m.id"
            f"{where} ORDER BY m.id DESC LIMIT ?", params
        ).fetchall()
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
    s["unread"] = store.count_unread()
    s["last_read_id"] = store.get_last_read_id()
    s["first_unread_id"] = store.get_first_unread_id()
    s["latest_id"] = store.get_max_msg_id()
    s["pid"] = os.getpid()
    # 总数实时查库（meta 里的 stored_total 是补漏时快照，会过期）
    conn = store.get_conn()
    try:
        s["stored_total"] = conn.execute(
            "SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()
    return jsonify(s)


@app.route("/api/special-users", methods=["GET", "POST"])
def api_special_users():
    if request.method == "GET":
        return jsonify({"users": store.list_users(),
                        "special_uids": store.get_special_uids()})
    data = request.get_json(silent=True) or {}
    uid = str(data.get("uid", ""))
    enabled = data.get("enabled")
    if not re.fullmatch(r"\d+", uid) or not isinstance(enabled, bool):
        abort(400)
    store.set_special_user(uid, enabled)
    return jsonify({"ok": True})


@app.route("/api/special-unread")
def api_special_unread():
    return jsonify({"ids": store.list_unread_special_ids()})


@app.route("/api/image-cleanup", methods=["GET", "POST"])
def api_image_cleanup():
    if request.method == "GET":
        return jsonify(media_cache.image_cache_stats())
    data = request.get_json(silent=True) or {}
    months = data.get("months")
    if months not in (0, 1, 2, 3):
        abort(400)
    result = media_cache.cleanup_images(
        days=None if months == 0 else months * 30
    )
    stats = media_cache.image_cache_stats()
    return jsonify({"ok": True, "deleted": result["deleted"],
                    "freed_bytes": result["size_bytes"], **stats})


@app.route("/api/storage-stats")
def api_storage_stats():
    conn = store.get_conn()
    try:
        messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()
    return jsonify({"messages": messages,
                    "images": media_cache.image_cache_stats()})


@app.route("/api/message-cleanup-preview")
def api_message_cleanup_preview():
    months = request.args.get("months", type=int)
    try:
        return jsonify(storage_cleanup.preview(months))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


@app.route("/api/message-cleanup", methods=["POST"])
def api_message_cleanup():
    data = request.get_json(silent=True) or {}
    if data.get("confirmed") is not True:
        return jsonify({"error": "需要明确确认"}), 400
    try:
        result = storage_cleanup.cleanup(data.get("months"))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    broadcast({"type": "message_cleanup_done"})
    return jsonify({"ok": True, **result})


@app.route("/api/prepare-stop", methods=["POST"])
def api_prepare_stop():
    """停止脚本调用：先记下准确停机时刻，再由脚本结束本进程。"""
    now = int(time.time())
    store.set_meta("last_online_at", now)
    store.open_gap(now)
    runtime_state.set_status("stopped", "用户停止")
    return jsonify({"ok": True, "pid": os.getpid()})


@app.route("/api/read", methods=["POST"])
def api_read():
    """面板上报：用户视野扫过某条消息 → 推进已读锚点（只前进不倒退）。"""
    data = request.get_json(silent=True) or {}
    mid = data.get("id")
    if not mid:
        abort(400)
    cur = store.get_last_read_id()
    if cur is None or mid > cur:
        store.set_last_read_id(mid)
    return jsonify({"ok": True, "unread": store.count_unread()})


@app.route("/api/gaps")
def api_gaps():
    """最近缺口账本，以及首页应显示的最新一条提醒。"""
    days = min(max(request.args.get("days", 7, type=int), 1), 30)
    since_ts = int(time.time()) - days * 86400
    return jsonify({"latest": store.get_latest_gap_alert(),
                    "gaps": store.list_gaps(since_ts)})


@app.route("/api/gaps/<int:gap_id>/dismiss", methods=["POST"])
def api_dismiss_gap(gap_id):
    """关闭首页提醒但保留缺口记录。"""
    if not store.dismiss_gap(gap_id, int(time.time())):
        abort(404)
    return jsonify({"ok": True})


_backfilling = threading.Event()


@app.route("/api/backfill-state")
def api_backfill_state():
    return jsonify({"running": _backfilling.is_set()})


@app.route("/api/backfill", methods=["POST"])
def api_backfill():
    """补漏至指定缺口（同时补其后更新的缺口；后台线程防重入）。

    补漏=拉取=会把微博已读位置推到最新（手机端角标清一次）——
    这是用户已知的取舍，故只由手动触发，绝不自动。
    """
    data = request.get_json(silent=True) or {}
    gap_id = data.get("gap_id")
    if not isinstance(gap_id, int) or gap_id <= 0:
        abort(400)
    if _backfilling.is_set():
        return jsonify({"ok": False, "msg": "补漏进行中"}), 409
    if not store.list_unfilled_closed_gaps_from(gap_id):
        return jsonify({"ok": False,
                        "msg": "所选缺口不存在、尚未结束或已经补漏"}), 400
    _backfilling.set()
    cfg = app.config["CFG"]

    def worker():
        import backfill
        try:
            result = backfill.backfill_to_gap(cfg, gap_id)
            broadcast({"type": "backfill_done", **result})
        except Exception as e:
            log.warning("手动补漏失败: %s", e)
            broadcast({"type": "backfill_done", "inserted": 0,
                       "complete": False, "error": str(e)})
        finally:
            _backfilling.clear()

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/image/<int:msg_id>")
def api_image(msg_id):
    path = media_cache.get_image_path(msg_id, cache_if_missing=True)
    if not path:
        abort(404)
    return send_file(path, max_age=86400)


@app.route("/api/attachment-info/<int:msg_id>")
def api_attachment_info(msg_id):
    try:
        info = media_cache.ensure_attachment_info(msg_id)
    except (OSError, requests.RequestException, ValueError,
            json.JSONDecodeError, RuntimeError) as error:
        log.warning("附件信息读取失败（%s）", type(error).__name__)
        return jsonify({"status": "failed", "error": "附件信息读取失败"}), 503
    if not info:
        conn = store.get_conn()
        try:
            row = conn.execute(
                "SELECT content, media_type, media_data FROM messages WHERE id=?",
                (msg_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row or row[1] != 5:
            abort(404)
        status, error = "unavailable", "历史附件尚未回填"
        info = {"file_name": row[0], "size_bytes": None,
                "status": status, "error": error}
    return jsonify({key: info.get(key) for key in
                    ("file_name", "size_bytes", "status", "error")})


@app.route("/api/attachment/<int:msg_id>")
def api_attachment(msg_id):
    try:
        result = media_cache.open_attachment(msg_id)
    except (OSError, requests.RequestException, ValueError,
            json.JSONDecodeError, RuntimeError) as error:
        log.warning("附件下载失败（%s）", type(error).__name__)
        return jsonify({"error": "附件下载失败，请稍后重试"}), 503
    if not result:
        abort(404)
    upstream, file_name = result

    def stream():
        try:
            yield from upstream.iter_content(64 * 1024)
        finally:
            upstream.close()

    headers = {
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(file_name),
    }
    if upstream.headers.get("Content-Length"):
        headers["Content-Length"] = upstream.headers["Content-Length"]
    return Response(stream(), headers=headers,
                    content_type=upstream.headers.get(
                        "Content-Type", "application/octet-stream"))


def start(cfg, host="127.0.0.1", port=8765):
    """在后台线程启动面板服务（主线程留给采集器）。"""
    app.config["COOKIE_PATH"] = cfg["cookie_path"]
    app.config["CFG"] = cfg
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
