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
    content       TEXT,                  -- 文本内容 / 链接
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
"""


def get_conn():
    """取得数据库连接（自动建 data 目录，开启 WAL）。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    """建库建表（幂等：已存在则跳过）。"""
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
