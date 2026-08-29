# -*- coding: utf-8 -*-
"""实验检查：看指定时刻之后有没有被踢、有没有入库。"""
import sqlite3
import sys

from datetime import datetime

SINCE = sys.argv[1] if len(sys.argv) > 1 else "11:12"
now = datetime.now().strftime("%H:%M:%S")
print(f"检查时刻: {now}（基线 {SINCE}）")

kicked = 0
session_line = ""
for line in open("logs/watcher.log", encoding="utf-8"):
    if len(line) > 8 and line[0:2].isdigit():
        hhmmss = line.split()[0]
        if hhmmss >= SINCE and hhmmss <= now:
            if "握手成功" in line:
                session_line = line.strip()
            if "被拒" in line or "作废" in line:
                kicked += 1
print(f"基线后被踢次数: {kicked}")
print(f"当前会话: {session_line or '（基线后无新握手=一直没死，好事）'}")

conn = sqlite3.connect("data/watcher.db")
n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
print(f"库内消息数: {n}")
for row in conn.execute(
        "SELECT from_name, content, datetime(time,'unixepoch','localtime') "
        "FROM messages ORDER BY time DESC LIMIT 3"):
    print(f"  [{row[0]}] {row[1][:30]}  @{row[2]}")
conn.close()
