"""存储模块：SQLite 建库、建表、取连接。

表结构约定见 03-方案设计.md §4。
"""
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "watcher.db")

SCHEMA = """
-- 消息主表：全部消息的唯一事实来源
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY,   -- 微博消息 ID
    gid           TEXT NOT NULL,         -- 群 ID（存于本地库，不入 git）
    from_uid      TEXT,                  -- 发言人 UID
    from_name     TEXT,                  -- 发言人昵称（采集时刻快照）
    avatar_url    TEXT,                  -- 发言人头像地址（3h 时效，本地缓存）
    content       TEXT,                  -- 文本内容 / 链接
    url_objects   TEXT,                  -- 分享消息的原帖数据包（JSON）
    type          INTEGER,               -- 消息类型（321 正文 / 344 系统通知…）
    media_type    INTEGER DEFAULT 0,     -- 媒体类型（0 文字 / 1 图片 / 14 链接分享…）
    media_data    TEXT,                  -- fid/pic_infos 等媒体下载字段（JSON）
    time          INTEGER,               -- 微博侧时间戳（秒）
    recall_status INTEGER DEFAULT 0,     -- 撤回状态
    created_at    TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(time);
CREATE INDEX IF NOT EXISTS idx_messages_from ON messages(from_uid);

-- 图片对应表：消息 ↔ 本地缓存文件
CREATE TABLE IF NOT EXISTS images (
    msg_id        INTEGER PRIMARY KEY,   -- 对应 messages.id
    file_path     TEXT NOT NULL,         -- data/images/ 下的相对路径
    downloaded_at TEXT DEFAULT (datetime('now','localtime')),
    size_bytes    INTEGER
);

-- 聊天消息中的文件附件元数据（文件本体按需下载，不在工具内缓存）
CREATE TABLE IF NOT EXISTS attachments (
    msg_id        INTEGER PRIMARY KEY,
    fid           TEXT NOT NULL,
    file_name     TEXT,
    extension     TEXT,
    file_path     TEXT,
    size_bytes    INTEGER,
    status        TEXT NOT NULL DEFAULT 'pending',
    downloaded_at TEXT,
    error         TEXT
);

-- 已读锚点：单行表，续读/待读数的计算基准
CREATE TABLE IF NOT EXISTS read_state (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    last_read_msg_id INTEGER
);
INSERT OR IGNORE INTO read_state (id, last_read_msg_id) VALUES (1, NULL);

-- 特别关注名单（面板设置页维护）
CREATE TABLE IF NOT EXISTS special_users (
    uid  TEXT PRIMARY KEY,
    note TEXT
);

-- 杂项状态（锚点、清理配置、上次心跳等）
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- 缺口账本：工具休眠/断线/停机时段（不自动拉取，面板如实标注）
CREATE TABLE IF NOT EXISTS gaps (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts     INTEGER NOT NULL,          -- 缺口开始（unix 秒）
    end_ts       INTEGER,                   -- 缺口结束；NULL = 仍在缺口期
    dismissed_at INTEGER,                   -- 首页提醒关闭时间；记录仍保留
    filled_at    INTEGER,                   -- 补漏完成时间；NULL = 尚未补漏
    created_at   TEXT DEFAULT (datetime('now','localtime'))
);
"""


def get_conn():
    """取得数据库连接（自动建 data 目录，开启 WAL）。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    """建库建表（幂等：已存在则跳过）；老库自动补新增列。"""
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        for col in ("avatar_url TEXT", "url_objects TEXT", "media_data TEXT"):
            try:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # 列已存在
        gap_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(gaps)").fetchall()
        }
        if "dismissed_at" not in gap_columns:
            conn.execute("ALTER TABLE gaps ADD COLUMN dismissed_at INTEGER")
        if "filled_at" not in gap_columns:
            conn.execute("ALTER TABLE gaps ADD COLUMN filled_at INTEGER")
        conn.commit()
    finally:
        conn.close()


def get_max_msg_id():
    """当前库内最新消息 ID（补漏锚点）；空库返回 None。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT MAX(id) FROM messages").fetchone()
        return row[0]
    finally:
        conn.close()


def get_last_read_id():
    """已读锚点（用户看到的最新一条消息 ID）；从未读过返回 None。"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT last_read_msg_id FROM read_state WHERE id=1").fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def set_last_read_id(msg_id):
    """推进已读锚点（只会向前，不会倒退）。"""
    conn = get_conn()
    try:
        conn.execute("UPDATE read_state SET last_read_msg_id=? WHERE id=1",
                     (msg_id,))
        conn.commit()
    finally:
        conn.close()


def count_unread():
    """待读条数 = 库内比已读锚点新的消息数；从未读过按全部计。"""
    conn = get_conn()
    try:
        last = get_last_read_id()
        if last is None:
            n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        else:
            n = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE id > ?", (last,)
            ).fetchone()[0]
        return n
    finally:
        conn.close()


def get_first_unread_id():
    """当前第一条待读消息 ID；没有待读时返回 None。"""
    conn = get_conn()
    try:
        last = get_last_read_id()
        if last is None:
            row = conn.execute(
                "SELECT id FROM messages ORDER BY id LIMIT 1"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM messages WHERE id > ? ORDER BY id LIMIT 1",
                (last,),
            ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def list_users():
    """按最近发言时间列出群成员，并标注是否特别关注。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "WITH ranked AS ("
            " SELECT from_uid, from_name, avatar_url, time,"
            " ROW_NUMBER() OVER (PARTITION BY from_uid"
            " ORDER BY time DESC, id DESC) AS rn"
            " FROM messages WHERE from_uid IS NOT NULL AND from_uid != ''"
            ")"
            " SELECT r.from_uid, r.from_name, r.avatar_url, r.time,"
            " CASE WHEN s.uid IS NULL THEN 0 ELSE 1 END AS special"
            " FROM ranked r LEFT JOIN special_users s ON s.uid=r.from_uid"
            " WHERE r.rn=1"
            " UNION ALL"
            " SELECT s.uid, s.note, '', NULL, 1 FROM special_users s"
            " WHERE NOT EXISTS (SELECT 1 FROM ranked r"
            " WHERE r.rn=1 AND r.from_uid=s.uid)"
        ).fetchall()
        return [
            {"uid": r[0], "name": r[1], "avatar_url": r[2],
             "last_time": r[3], "special": bool(r[4])}
            for r in rows
        ]
    finally:
        conn.close()


def set_special_user(uid, enabled, note=""):
    """按稳定 UID 添加或取消特别关注。"""
    conn = get_conn()
    try:
        if enabled:
            conn.execute(
                "INSERT INTO special_users (uid, note) VALUES (?, ?) "
                "ON CONFLICT(uid) DO UPDATE SET note=excluded.note",
                (str(uid), note),
            )
        else:
            conn.execute("DELETE FROM special_users WHERE uid=?", (str(uid),))
        conn.commit()
    finally:
        conn.close()


def get_special_uids():
    conn = get_conn()
    try:
        return [r[0] for r in conn.execute(
            "SELECT uid FROM special_users ORDER BY uid"
        ).fetchall()]
    finally:
        conn.close()


def list_unread_special_ids():
    """当前待读范围内，特别关注用户的消息 ID（旧→新）。"""
    conn = get_conn()
    try:
        last = get_last_read_id()
        if last is None:
            rows = conn.execute(
                "SELECT m.id FROM messages m JOIN special_users s "
                "ON s.uid=m.from_uid ORDER BY m.id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT m.id FROM messages m JOIN special_users s "
                "ON s.uid=m.from_uid WHERE m.id>? ORDER BY m.id",
                (last,),
            ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def open_gap(start_ts):
    """记录缺口开始（幂等：已有未闭合缺口则不新建）。"""
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM gaps WHERE end_ts IS NULL").fetchone()
        if cur[0] == 0:
            conn.execute("INSERT INTO gaps (start_ts) VALUES (?)", (start_ts,))
            conn.commit()
    finally:
        conn.close()


def close_gap(end_ts):
    """闭合当前缺口（把未闭合的补上结束时刻）。"""
    conn = get_conn()
    try:
        conn.execute("UPDATE gaps SET end_ts=? WHERE end_ts IS NULL",
                     (end_ts,))
        conn.commit()
    finally:
        conn.close()


def list_gaps(since_ts=None):
    """列出缺口（新→旧）；可限制开始时间，历史记录永不因补漏删除。"""
    conn = get_conn()
    try:
        sql = ("SELECT id, start_ts, end_ts, dismissed_at, filled_at "
               "FROM gaps")
        params = ()
        if since_ts is not None:
            sql += " WHERE end_ts IS NULL OR end_ts>=?"
            params = (since_ts,)
        rows = conn.execute(sql + " ORDER BY id DESC", params).fetchall()
        return [
            {"id": r[0], "start_ts": r[1], "end_ts": r[2],
             "dismissed_at": r[3], "filled_at": r[4]}
            for r in rows
        ]
    finally:
        conn.close()


def get_latest_gap_alert():
    """首页只看账本最新一条；关闭后不回溯提醒更早缺口。"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, start_ts, end_ts, dismissed_at, filled_at "
            "FROM gaps ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row or row[3] is not None or row[4] is not None:
            return None
        return {"id": row[0], "start_ts": row[1], "end_ts": row[2]}
    finally:
        conn.close()


def dismiss_gap(gap_id, dismissed_at):
    """永久关闭某条缺口的首页提醒，不删除账本记录。"""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE gaps SET dismissed_at=? WHERE id=? AND filled_at IS NULL",
            (dismissed_at, gap_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_unfilled_closed_gaps_from(target_gap_id):
    """目标缺口及其后更新的全部未补缺口（新→旧）。"""
    conn = get_conn()
    try:
        target = conn.execute(
            "SELECT id FROM gaps WHERE id=? AND end_ts IS NOT NULL "
            "AND filled_at IS NULL",
            (target_gap_id,),
        ).fetchone()
        if not target:
            return []
        rows = conn.execute(
            "SELECT id, start_ts, end_ts FROM gaps "
            "WHERE id>=? AND end_ts IS NOT NULL AND filled_at IS NULL "
            "ORDER BY id DESC",
            (target_gap_id,),
        ).fetchall()
        return [{"id": r[0], "start_ts": r[1], "end_ts": r[2]}
                for r in rows]
    finally:
        conn.close()


def get_gap_anchors(start_ts, end_ts):
    """返回缺口前最后一条、缺口后第一条本地消息 ID。"""
    conn = get_conn()
    try:
        before = conn.execute(
            "SELECT id FROM messages WHERE time<=? "
            "ORDER BY time DESC, id DESC LIMIT 1",
            (start_ts,),
        ).fetchone()
        after = conn.execute(
            "SELECT id FROM messages WHERE time>=? "
            "ORDER BY time, id LIMIT 1",
            (end_ts,),
        ).fetchone()
        return (before[0] if before else None, after[0] if after else None)
    finally:
        conn.close()


def mark_gap_filled(gap_id, filled_at):
    """标记缺口补漏成功；记录保留供以后追查。"""
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE gaps SET filled_at=? WHERE id=?", (filled_at, gap_id)
        )
        conn.commit()
    finally:
        conn.close()


def get_meta(key, default=None):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default
    finally:
        conn.close()


def set_meta(key, value):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))
        conn.commit()
    finally:
        conn.close()
