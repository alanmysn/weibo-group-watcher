"""补漏模块：断线/停机/被踢窗口期间漏掉的消息，用历史接口对账补齐。

策略（03-方案设计.md §6.2）：
- 每个缺口用「缺口后第一条本地消息」作为向前翻页游标；
- 翻到「缺口前最后一条本地消息」即停止，只写入两锚点之间的消息；
- 点击较早缺口时，依次处理它及其后更新的全部未补缺口；
- 三保险：页间隔 0.5s / 单次最多扫描 2000 条 / 未到锚点不标成功。
"""
import json
import logging
import time

import requests

import runtime_state
import store

log = logging.getLogger("watcher.backfill")

API_URL = "https://api.weibo.com/webim/groupchat/query_messages.json"
SOURCE = "209678993"
PAGE_SIZE = 50
PAGE_INTERVAL = 0.5          # 页间隔（秒），动作温和
MAX_PER_RUN = 2000           # 单次补漏上限


def _headers(cookie_path):
    with open(cookie_path, encoding="utf-8") as f:
        cookies = json.load(f)
    return {
        "Cookie": "; ".join(f"{c['name']}={c['value']}" for c in cookies),
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/152.0.0.0 Safari/537.36"),
        # 必带：服务器按 Referer 校验来源，缺失则 10012 拒绝（实测 2026-08-29）
        "Referer": "https://api.weibo.com/chat",
        "Accept": "application/json",
        "X-Xsrf-Token": next((c["value"] for c in cookies
                              if c["name"] == "XSRF-TOKEN"), ""),
    }


def backfill_to_gap(cfg, target_gap_id):
    """补到目标缺口；同时处理它之后更新的未补缺口。"""
    gaps = store.list_unfilled_closed_gaps_from(target_gap_id)
    if not gaps:
        raise ValueError("所选缺口不存在、尚未结束或已经补漏")

    gid = str(cfg["group_id"])
    headers = _headers(cfg["cookie_path"])
    total_inserted = 0
    total_scanned = 0
    filled_ids = []
    error = ""

    for gap in gaps:
        before_id, after_id = store.get_gap_anchors(
            gap["start_ts"], gap["end_ts"]
        )
        if before_id is None:
            error = "缺口前没有本地消息，无法确定停止位置"
            break
        remaining = MAX_PER_RUN - total_scanned
        if remaining <= 0:
            error = f"达到单次 {MAX_PER_RUN} 条扫描上限"
            break
        inserted, scanned, complete, reason = _backfill_range(
            gid, headers, before_id, after_id, remaining
        )
        total_inserted += inserted
        total_scanned += scanned
        if not complete:
            error = reason
            break
        store.mark_gap_filled(gap["id"], int(time.time()))
        filled_ids.append(gap["id"])

    runtime_state.touch_backfill(_count_total())
    complete = len(filled_ids) == len(gaps)
    if complete:
        log.info("补漏完成：处理 %d 个缺口，新增 %d 条消息",
                 len(filled_ids), total_inserted)
    else:
        log.warning("补漏未完成：已处理 %d 个缺口，%s",
                    len(filled_ids), error)
    return {"inserted": total_inserted, "scanned": total_scanned,
            "filled_ids": filled_ids, "complete": complete,
            "error": error}


def _backfill_range(gid, headers, before_id, after_id, scan_limit):
    """从缺口后锚点向前翻到缺口前锚点。"""
    before_id = int(before_id)
    after_id = int(after_id) if after_id is not None else None
    if after_id is not None and after_id <= before_id:
        return 0, 0, False, "缺口前后锚点顺序异常"

    max_mid = after_id or 0
    inserted = 0
    scanned = 0
    while scanned < scan_limit:
        page_count = min(PAGE_SIZE, scan_limit - scanned)
        resp = requests.get(
            API_URL,
            params={"convert_emoji": 1, "query_sender": 1,
                    "count": page_count, "id": gid,
                    "max_mid": max_mid, "source": SOURCE},
            headers=headers, timeout=20,
        )
        data = resp.json()
        if "error" in data:
            if data.get("error_code") == 100000:  # 约定：登录态失效
                from collector import SessionExpired
                raise SessionExpired("历史接口登录态失效")
            raise RuntimeError(
                f"补漏接口异常（错误码 {data.get('error_code', '未知')}）"
            )
        msgs = data.get("messages") or []
        if not msgs:
            return inserted, scanned, False, "服务器历史已到底，仍未到达停止锚点"

        ids = [int(m["id"]) for m in msgs if m.get("id") is not None]
        if len(ids) != len(msgs):
            return inserted, scanned, False, "服务器返回的消息缺少 ID"
        scanned += len(ids)
        reached_before = min(ids) <= before_id
        for m in msgs:
            mid = int(m.get("id", 0))
            if before_id < mid and (after_id is None or mid < after_id):
                inserted += _store(m, gid)
        if reached_before:
            return inserted, scanned, True, ""
        if len(ids) < page_count:
            return inserted, scanned, False, "服务器历史已到底，仍未到达停止锚点"
        max_mid = min(ids)
        time.sleep(PAGE_INTERVAL)
    return inserted, scanned, False, f"达到单次 {MAX_PER_RUN} 条扫描上限"


def _count_total():
    conn = store.get_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()


def _store(m, gid):
    """单条落库（幂等去重，重复时刷新头像/分享包），返回是否新入库。"""
    conn = store.get_conn()
    try:
        avatar_url = (m.get("from_user") or {}).get("profile_image_url", "")
        url_objects = json.dumps(m.get("url_objects") or [],
                                 ensure_ascii=False)
        cur = conn.execute(
            "INSERT OR IGNORE INTO messages "
            "(id, gid, from_uid, from_name, avatar_url, content, url_objects, "
            " type, media_type, time, recall_status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (m.get("id"), gid, str(m.get("from_uid", "")),
             (m.get("from_user") or {}).get("screen_name", ""),
             avatar_url, m.get("content", ""), url_objects,
             m.get("type"), m.get("media_type", 0), m.get("time"),
             m.get("recall_status", 0)),
        )
        conn.execute("UPDATE messages SET avatar_url=?, url_objects=?, "
                     "from_name=?, recall_status=? WHERE id=?",
                     (avatar_url, url_objects,
                      (m.get("from_user") or {}).get("screen_name", ""),
                      m.get("recall_status", 0), m.get("id")))
        conn.commit()
        return 1 if cur.rowcount else 0
    finally:
        conn.close()
