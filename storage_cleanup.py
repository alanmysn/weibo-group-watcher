"""本地消息记录清理：预览、确认后执行，永不自动运行。"""
import os
import time

import media_cache
import store


VALID_MONTHS = (0, 1, 2, 3)


def _scope(months, now=None):
    if type(months) is not int or months not in VALID_MONTHS:
        raise ValueError("清理范围无效")
    if months == 0:
        return "1=1", (), None
    cutoff = int((now or time.time()) - months * 30 * 24 * 60 * 60)
    return "time < ?", (cutoff,), cutoff


def _qualified(where, alias):
    return where if where == "1=1" else f"{alias}.{where}"


def preview(months, now=None):
    where, params, cutoff = _scope(months, now)
    conn = store.get_conn()
    try:
        messages = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE {where}", params
        ).fetchone()[0]
        images = conn.execute(
            "SELECT COUNT(*) FROM images i JOIN messages m ON m.id=i.msg_id "
            f"WHERE {_qualified(where, 'm')}", params,
        ).fetchone()[0]
        attachments = conn.execute(
            "SELECT COUNT(*) FROM attachments a "
            "JOIN messages m ON m.id=a.msg_id "
            f"WHERE {_qualified(where, 'm')}", params,
        ).fetchone()[0]
        if cutoff is None:
            gaps_deleted = conn.execute(
                "SELECT COUNT(*) FROM gaps"
            ).fetchone()[0]
            gaps_trimmed = 0
        else:
            gaps_deleted = conn.execute(
                "SELECT COUNT(*) FROM gaps "
                "WHERE end_ts IS NOT NULL AND end_ts<=?", (cutoff,),
            ).fetchone()[0]
            gaps_trimmed = conn.execute(
                "SELECT COUNT(*) FROM gaps WHERE start_ts<? "
                "AND (end_ts IS NULL OR end_ts>?)", (cutoff, cutoff),
            ).fetchone()[0]
        return {
            "messages": messages,
            "images": images,
            "attachments": attachments,
            "gaps_deleted": gaps_deleted,
            "gaps_trimmed": gaps_trimmed,
            "cutoff": cutoff,
        }
    finally:
        conn.close()


def cleanup(months, now=None):
    effective_now = now or time.time()
    where, params, cutoff = _scope(months, effective_now)
    result = preview(months, effective_now)
    conn = store.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        paths = conn.execute(
            "SELECT i.file_path FROM images i "
            "JOIN messages m ON m.id=i.msg_id "
            f"WHERE {_qualified(where, 'm')}", params,
        ).fetchall()
        for (relative,) in paths:
            path = media_cache._absolute_data_path(relative)
            if os.path.isfile(path):
                os.remove(path)

        # 特别关注名单保留；删除其历史消息前保存最近昵称供设置页显示。
        conn.execute(
            "UPDATE special_users SET note=COALESCE(("
            "SELECT m.from_name FROM messages m "
            "WHERE m.from_uid=special_users.uid "
            "ORDER BY m.time DESC, m.id DESC LIMIT 1), note)"
        )
        conn.execute(
            "DELETE FROM images WHERE msg_id IN "
            f"(SELECT id FROM messages WHERE {where})", params,
        )
        conn.execute(
            "DELETE FROM attachments WHERE msg_id IN "
            f"(SELECT id FROM messages WHERE {where})", params,
        )
        conn.execute(f"DELETE FROM messages WHERE {where}", params)
        if cutoff is None:
            conn.execute("DELETE FROM gaps")
        else:
            conn.execute(
                "DELETE FROM gaps WHERE end_ts IS NOT NULL AND end_ts<=?",
                (cutoff,),
            )
            conn.execute(
                "UPDATE gaps SET start_ts=? WHERE start_ts<? "
                "AND (end_ts IS NULL OR end_ts>?)",
                (cutoff, cutoff, cutoff),
            )
        if months == 0:
            conn.execute(
                "UPDATE read_state SET last_read_msg_id=NULL WHERE id=1"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return result
