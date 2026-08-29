# -*- coding: utf-8 -*-
"""运行状态机：工具当前状态的单点事实（面板与日志共同引用）。

状态：online（在线）/ reconnecting（重连中）/ expired（钥匙过期）/ stopped（已停止）
持久化：状态写入 SQLite meta 表（跨进程可读，面板第 4 步直接查库）。
"""
import time

import store

_STATUS_KEY = "runtime_status"
valid_statuses = ("online", "reconnecting", "expired", "stopped")


def set_status(status, detail=""):
    conn = store.get_conn()
    try:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_STATUS_KEY, f"{status}|{int(time.time())}|{detail}"),
        )
        conn.commit()
    finally:
        conn.close()


def touch_message():
    conn = store.get_conn()
    try:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('last_msg_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(int(time.time())),),
        )
        conn.commit()
    finally:
        conn.close()


def touch_backfill(total=None):
    conn = store.get_conn()
    try:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('last_backfill_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(int(time.time())),),
        )
        if total is not None:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('stored_total', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(total),),
            )
        conn.commit()
    finally:
        conn.close()


def snapshot():
    """读状态（任何进程可调）。缺项时各字段为 None。"""
    conn = store.get_conn()
    try:
        rows = dict(conn.execute(
            "SELECT key, value FROM meta WHERE key IN "
            "('runtime_status','last_msg_at','last_backfill_at','stored_total')"
        ).fetchall())
    finally:
        conn.close()
    out = {"status": None, "since": None, "detail": "",
           "last_msg_at": None, "last_backfill_at": None,
           "stored_total": None}
    raw = rows.get(_STATUS_KEY)
    if raw:
        parts = raw.split("|", 2)
        out["status"] = parts[0]
        out["since"] = int(parts[1]) if len(parts) > 1 and parts[1] else None
        out["detail"] = parts[2] if len(parts) > 2 else ""
    for key in ("last_msg_at", "last_backfill_at"):
        if rows.get(key):
            out[key] = int(rows[key])
    if rows.get("stored_total"):
        out["stored_total"] = int(rows["stored_total"])
    return out
