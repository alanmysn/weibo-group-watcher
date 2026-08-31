"""聊天图片缓存与文件附件按需下载。"""
import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

import config
import store

log = logging.getLogger("watcher.media")

SOURCE = "209678993"
META_URL = "https://upload.api.weibo.com/2/mss/meta_query.json"
DOWNLOAD_URL = "https://upload.api.weibo.com/2/mss/msget"
IMAGE_MAX_BYTES = 25 * 1024 * 1024
IMAGE_TYPES = {1, 10, 15}
SUPPORTED_TYPES = IMAGE_TYPES

IMAGE_DIR = os.path.join(store.DATA_DIR, "images")

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="media-cache")
_pending = set()
_pending_lock = threading.Lock()


class TooLarge(RuntimeError):
    pass


def extract_media_data(info):
    """只保留下载所需媒体字段，不复制整条原始消息。"""
    data = {}
    fids = info.get("fids") or []
    if fids:
        data["fids"] = [str(fid) for fid in fids if fid is not None]

    pic_infos = []
    for pic in info.get("pic_infos") or []:
        if not isinstance(pic, dict):
            continue
        pic_infos.append({
            key: pic.get(key) for key in
            ("pid", "original_pic", "bmiddle_pic", "thumbnail_pic",
             "thumbnail_size", "expire_time") if pic.get(key) is not None
        })
    if pic_infos:
        data["pic_infos"] = pic_infos

    annotations = info.get("annotations") or {}
    if isinstance(annotations, str):
        try:
            annotations = json.loads(annotations)
        except json.JSONDecodeError:
            annotations = {}
    video_pic_fid = annotations.get("video_pic_fid") \
        if isinstance(annotations, dict) else None
    if video_pic_fid:
        data["video_pic_fid"] = str(video_pic_fid)
    return json.dumps(data, ensure_ascii=False) if data else None


def schedule(msg_id, media_type):
    """后台缓存单条媒体；同一消息在进程内防重复排队。"""
    if media_type not in SUPPORTED_TYPES or not msg_id:
        return
    msg_id = int(msg_id)
    with _pending_lock:
        if msg_id in _pending:
            return
        _pending.add(msg_id)

    future = _executor.submit(cache_message, msg_id)

    def done(_future):
        with _pending_lock:
            _pending.discard(msg_id)

    future.add_done_callback(done)


def cache_message(msg_id):
    """同步确保一条媒体已缓存；供后台任务和下载路由共同调用。"""
    try:
        row = _message_media(msg_id)
        if not row or row[0] not in SUPPORTED_TYPES or not row[1]:
            return None
        media_type, raw_data, _content = row
        data = json.loads(raw_data)
        cfg = config.load_config()
        if media_type == 15:
            pics = data.get("pic_infos") or []
            url = pics[0].get("original_pic") if pics else None
            return _cache_image_url(msg_id, url, "gif", cfg)
        fid = data.get("video_pic_fid") if media_type == 10 else None
        if not fid:
            fids = data.get("fids") or []
            fid = fids[0] if fids else None
        return _cache_image_fid(msg_id, fid, cfg)
    except TooLarge:
        log.warning("图片超过 25 MB，未自动缓存")
    except (OSError, requests.RequestException, ValueError,
            json.JSONDecodeError) as error:
        log.warning("媒体缓存失败（%s）", type(error).__name__)
    return None


def get_image_path(msg_id, cache_if_missing=False):
    path = _cached_image_path(msg_id)
    if path or not cache_if_missing:
        return path
    cache_message(msg_id)
    return _cached_image_path(msg_id)


def get_attachment_info(msg_id):
    conn = store.get_conn()
    try:
        row = conn.execute(
            "SELECT file_name, size_bytes, status, file_path, error "
            "FROM attachments WHERE msg_id=?", (msg_id,)
        ).fetchone()
        if not row:
            return None
        return {"file_name": row[0], "size_bytes": row[1],
                "status": row[2], "file_path": row[3], "error": row[4]}
    finally:
        conn.close()


def ensure_attachment_info(msg_id):
    info = get_attachment_info(msg_id)
    if info and info["status"] == "available":
        return info
    row = _message_media(msg_id)
    if not row or row[0] != 5 or not row[1]:
        return None
    data = json.loads(row[1])
    fids = data.get("fids") or []
    if not fids:
        return None
    fid = _valid_fid(fids[0])
    meta = _meta(fid, config.load_config())
    extension = _safe_extension(meta.get("extension"), "bin")
    file_name = _safe_display_name(meta.get("filename") or row[2], extension)
    size_bytes = int(meta.get("filesize") or 0)
    _save_attachment_state(
        msg_id, fid, file_name, extension, None, size_bytes, "available", None
    )
    return get_attachment_info(msg_id)


def open_attachment(msg_id):
    """按需打开微博附件响应；调用方负责关闭响应，不写入本地文件。"""
    info = ensure_attachment_info(msg_id)
    if not info:
        return None
    conn = store.get_conn()
    try:
        row = conn.execute(
            "SELECT fid FROM attachments WHERE msg_id=?", (msg_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    response = requests.get(
        DOWNLOAD_URL,
        params={"source": SOURCE, "fid": _valid_fid(row[0])},
        headers=_headers(config.load_config()["cookie_path"]),
        timeout=30, stream=True,
    )
    response.raise_for_status()
    return response, info["file_name"]


def _message_media(msg_id):
    conn = store.get_conn()
    try:
        return conn.execute(
            "SELECT media_type, media_data, content FROM messages WHERE id=?",
            (msg_id,),
        ).fetchone()
    finally:
        conn.close()


def _headers(cookie_path):
    with open(cookie_path, encoding="utf-8") as f:
        cookies = json.load(f)
    return {
        "Cookie": "; ".join(f"{c['name']}={c['value']}" for c in cookies),
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 Chrome/152.0.0.0 Safari/537.36"),
        "Referer": "https://api.weibo.com/chat",
    }


def _meta(fid, cfg):
    fid = _valid_fid(fid)
    response = requests.get(
        META_URL,
        params={"source": SOURCE, "fid": fid, "replace": "false"},
        headers=_headers(cfg["cookie_path"]), timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("error"):
        raise RuntimeError("微博媒体元数据接口返回错误")
    return data


def _cache_image_fid(msg_id, fid, cfg):
    existing = _cached_image_path(msg_id)
    if existing:
        return existing
    if not fid:
        return None
    meta = _meta(fid, cfg)
    extension = _safe_extension(meta.get("extension"), "jpg", image=True)
    path = os.path.join(IMAGE_DIR, f"{int(msg_id)}.{extension}")
    size = _download(
        DOWNLOAD_URL, path, _headers(cfg["cookie_path"]), IMAGE_MAX_BYTES,
        params={"source": SOURCE, "fid": _valid_fid(fid),
                "imageType": "origin"}, expected_content_type="image/",
    )
    return _save_image(msg_id, path, size)


def _cache_image_url(msg_id, url, fallback_extension, cfg):
    existing = _cached_image_path(msg_id)
    if existing:
        return existing
    if not url or urlparse(url).scheme not in ("http", "https"):
        return None
    tail = urlparse(url).path.rsplit("/", 1)[-1]
    extension = tail.rsplit(".", 1)[-1] if "." in tail else fallback_extension
    extension = _safe_extension(extension, fallback_extension, image=True)
    path = os.path.join(IMAGE_DIR, f"{int(msg_id)}.{extension}")
    size = _download(
        url, path, _headers(cfg["cookie_path"]), IMAGE_MAX_BYTES,
        expected_content_type="image/",
    )
    return _save_image(msg_id, path, size)


def _save_image(msg_id, path, size):
    relative = os.path.relpath(path, store.DATA_DIR).replace(os.sep, "/")
    conn = store.get_conn()
    try:
        conn.execute(
            "INSERT INTO images (msg_id, file_path, size_bytes) VALUES (?,?,?) "
            "ON CONFLICT(msg_id) DO UPDATE SET file_path=excluded.file_path, "
            "size_bytes=excluded.size_bytes, "
            "downloaded_at=datetime('now','localtime')",
            (msg_id, relative, size),
        )
        conn.commit()
    finally:
        conn.close()
    log.info("图片已缓存")
    return path


def _save_attachment_state(msg_id, fid, file_name, extension, file_path,
                           size_bytes, status, error):
    conn = store.get_conn()
    try:
        conn.execute(
            "INSERT INTO attachments "
            "(msg_id, fid, file_name, extension, file_path, size_bytes, "
            "status, downloaded_at, error) VALUES (?,?,?,?,?,?,?,"
            "CASE WHEN ?='ready' THEN datetime('now','localtime') END,?) "
            "ON CONFLICT(msg_id) DO UPDATE SET fid=excluded.fid, "
            "file_name=excluded.file_name, extension=excluded.extension, "
            "file_path=excluded.file_path, size_bytes=excluded.size_bytes, "
            "status=excluded.status, downloaded_at=excluded.downloaded_at, "
            "error=excluded.error",
            (msg_id, fid, file_name, extension, file_path, size_bytes,
             status, status, error),
        )
        conn.commit()
    finally:
        conn.close()


def _download(url, path, headers, max_bytes, params=None,
              expected_content_type=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    response = requests.get(
        url, params=params, headers=headers, timeout=30, stream=True
    )
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "").lower()
    if expected_content_type and not content_type.startswith(expected_content_type):
        response.close()
        raise ValueError("媒体响应类型不符")
    declared = int(response.headers.get("Content-Length") or 0)
    if declared > max_bytes:
        response.close()
        raise TooLarge()

    temp_path = f"{path}.{threading.get_ident()}.part"
    total = 0
    try:
        with open(temp_path, "wb") as f:
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise TooLarge()
                f.write(chunk)
        os.replace(temp_path, path)
        return total
    finally:
        response.close()
        if os.path.exists(temp_path):
            os.remove(temp_path)


def _cached_image_path(msg_id):
    conn = store.get_conn()
    try:
        row = conn.execute(
            "SELECT file_path FROM images WHERE msg_id=?", (msg_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    path = _absolute_data_path(row[0])
    return path if os.path.isfile(path) else None


def _absolute_data_path(relative):
    path = os.path.abspath(os.path.join(store.DATA_DIR, relative))
    if os.path.commonpath((store.DATA_DIR, path)) != os.path.abspath(store.DATA_DIR):
        raise ValueError("非法缓存路径")
    return path


def _valid_fid(fid):
    fid = str(fid or "")
    if not re.fullmatch(r"\d+", fid):
        raise ValueError("非法媒体 fid")
    return fid


def _safe_extension(extension, fallback, image=False):
    extension = str(extension or "").lower().lstrip(".")
    if not re.fullmatch(r"[a-z0-9]{1,10}", extension):
        extension = fallback
    if image and extension not in {"jpg", "jpeg", "png", "gif", "webp"}:
        extension = fallback
    return extension


def _safe_display_name(name, extension):
    name = re.split(r"[/\\]", str(name or ""))[-1]
    name = "".join(ch for ch in name if ch >= " " and ch not in '<>:"|?*')
    name = name.strip(" .")[:180]
    return name or f"附件.{extension}"
