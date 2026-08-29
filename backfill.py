"""补漏模块：断线/停机/被踢窗口期间漏掉的消息，用历史接口对账补齐。

策略（03-方案设计.md §6.2）：
- 锚点 = 库内最新消息 ID；从服务器最新一页往回翻，翻到锚点为止；
- 只收录"比锚点新"的消息（anchor_lookback 天然排除旧消息与重复）；
- 三保险：页间隔 0.5s / 单次上限 2000 条 / 超限标注不静默丢弃。
"""
import json
import logging
import time

import requests

import config
import runtime_state
import store

log = logging.getLogger("watcher.backfill")

API_URL = "https://api.weibo.com/webim/groupchat/query_messages.json"
SOURCE = "209678993"
PAGE_SIZE = 50
PAGE_INTERVAL = 0.5          # 页间隔（秒），动作温和
MAX_PER_RUN = 2000           # 单次补漏上限
RUN_INTERVAL = 3600          # 常态对账间隔（秒）


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


def backfill_once(cfg):
    """补漏一轮。返回 (新入库条数, 是否补全)。Cookie 失效抛 SessionExpired。"""
    gid = str(cfg["group_id"])
    anchor_id = store.get_max_msg_id()
    if anchor_id is None:
        log.info("库为空（首启），不回溯历史，从现在开始积累")
        return 0, True

    headers = _headers(cfg["cookie_path"])
    max_mid = 0          # 0 = 从最新开始
    inserted = 0
    hit_anchor = False

    while inserted < MAX_PER_RUN and not hit_anchor:
        resp = requests.get(
            API_URL,
            params={"convert_emoji": 1, "query_sender": 1,
                    "count": PAGE_SIZE, "id": gid,
                    "max_mid": max_mid, "source": SOURCE},
            headers=headers, timeout=20,
        )
        data = resp.json()
        if "error" in data:
            if data.get("error_code") == 100000:  # 约定：登录态失效
                from collector import SessionExpired
                raise SessionExpired(f"历史接口: {data.get('error')}")
            raise RuntimeError(f"补漏接口异常: {data}")
        msgs = data.get("messages") or []
        if not msgs:
            break  # 翻到底了
        # 实测页面内为【时间正序】（旧→新）。自愈型对账：
        # 窗口内每条都尝试入库（INSERT OR IGNORE 幂等，已存自动跳过），
        # 这样不仅能补"锚点之后"的缺口，也能自愈库中部的历史残缺
        # （2026-08-29 实验：锚点被新消息抬走后，中间的洞靠此机制愈合）。
        # 见到锚点只记号不停手——正序时锚点身后还有更新的消息。
        for m in msgs:
            if m.get("id") == anchor_id:
                hit_anchor = True
            inserted += _store(m, gid)
        if len(msgs) < PAGE_SIZE:
            break  # 没有更早的了
        max_mid = min(m.get("id") for m in msgs)
        time.sleep(PAGE_INTERVAL)

    if inserted:
        log.info("补漏完成：新增 %d 条（锚点 %s）", inserted, anchor_id)
    else:
        log.info("对账完成：无缺口")
    import runtime_state
    runtime_state.touch_backfill(_count_total())
    if not hit_anchor and inserted >= MAX_PER_RUN:
        log.warning("补漏达到单次上限 %d 条仍未追到锚点——缺口过深，"
                    "面板将在第 4 步起标注「缺口未补全」", MAX_PER_RUN)
        return inserted, False
    return inserted, True


def _count_total():
    conn = store.get_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()


def _store(m, gid):
    """单条落库（复用主键去重），返回是否新入库。"""
    conn = store.get_conn()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO messages "
            "(id, gid, from_uid, from_name, content, type, media_type, "
            " time, recall_status) VALUES (?,?,?,?,?,?,?,?,?)",
            (m.get("id"), gid, str(m.get("from_uid", "")),
             (m.get("from_user") or {}).get("screen_name", ""),
             m.get("content", ""), m.get("type"),
             m.get("media_type", 0), m.get("time"),
             m.get("recall_status", 0)),
        )
        conn.commit()
        return 1 if cur.rowcount else 0
    finally:
        conn.close()


def start_periodic(cfg, stop_event=None):
    """常态对账：每小时兜底核对一次（长连接才是主力，这是保险丝）。"""
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            backfill_once(cfg)
        except Exception as e:  # 对账失败不致命，下小时再试
            log.warning("常态对账失败（%s），下轮再试", e)
        if stop_event is not None and stop_event.wait(RUN_INTERVAL):
            return
