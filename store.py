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
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts   INTEGER NOT NULL,          -- 缺口开始（unix 秒）
    end_ts     INTEGER,                   -- 缺口结束；NULL = 仍在缺口期
    created_at TEXT DEFAULT (datetime('now','localtime'))
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
        for col in ("avatar_url TEXT", "url_objects TEXT"):
            try:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # 列已存在
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


def list_gaps():
    """全部缺口（旧→新）；end_ts 为 None 表示仍在缺口期。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, start_ts, end_ts FROM gaps ORDER BY id").fetchall()
        return [{"id": r[0], "start_ts": r[1], "end_ts": r[2]} for r in rows]
    finally:
        conn.close()


def clear_closed_gaps():
    """清除已闭合缺口（补漏成功后调用：缺口已被消息填平）。"""
    conn = get_conn()
    try:
        conn.execute("DELETE FROM gaps WHERE end_ts IS NOT NULL")
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
