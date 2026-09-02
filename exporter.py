"""从本地 SQLite 按条件生成可删除、可重建的消息导出文件。"""
import csv
import json
import os
import re
import uuid
from datetime import datetime, timedelta

import store

EXPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports")
FORMATS = {"md", "jsonl", "csv"}
CATEGORIES = {"all", "user", "image", "file", "video", "link", "system"}


def parse_date_range(start_date, end_date):
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
    except (TypeError, ValueError) as error:
        raise ValueError("日期格式不正确") from error
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    return int(start.timestamp()), int((end + timedelta(days=1)).timestamp())


def create_export(start_date, end_date, uid="", category="all", fmt="md"):
    uid = str(uid or "").strip()
    if uid and not re.fullmatch(r"\d+", uid):
        raise ValueError("发言人 UID 不正确")
    if category not in CATEGORIES:
        raise ValueError("消息类型不正确")
    if fmt not in FORMATS:
        raise ValueError("导出格式不正确")

    start_ts, end_ts = parse_date_range(start_date, end_date)
    messages = _query_messages(start_ts, end_ts, uid, category)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    name = (f"messages_{start_date.replace('-', '')}_"
            f"{end_date.replace('-', '')}_{uuid.uuid4().hex[:8]}.{fmt}")
    path = os.path.join(EXPORT_DIR, name)
    temp_path = f"{path}.part"
    try:
        if fmt == "md":
            _write_markdown(temp_path, messages, start_date, end_date)
        elif fmt == "jsonl":
            _write_jsonl(temp_path, messages)
        else:
            _write_csv(temp_path, messages)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    return {"file_name": name, "path": path, "count": len(messages)}


def _query_messages(start_ts, end_ts, uid, category):
    conditions = ["time>=?", "time<?"]
    params = [start_ts, end_ts]
    if uid:
        conditions.append("from_uid=?")
        params.append(uid)
    category_sql = {
        "all": None,
        "user": "COALESCE(type, 0) != 344",
        "image": "media_type IN (1, 15)",
        "file": "media_type=5",
        "video": "media_type=10",
        "link": "media_type=14",
        "system": "type=344",
    }[category]
    if category_sql:
        conditions.append(category_sql)
    conn = store.get_conn()
    try:
        rows = conn.execute(
            "SELECT id, from_uid, from_name, content, type, media_type, "
            "time, recall_status FROM messages WHERE "
            + " AND ".join(conditions) + " ORDER BY time, id",
            params,
        ).fetchall()
    finally:
        conn.close()
    fields = ("id", "from_uid", "from_name", "content", "type",
              "media_type", "time", "recall_status")
    return [_message_dict(dict(zip(fields, row))) for row in rows]


def _message_dict(message):
    message["datetime"] = datetime.fromtimestamp(message["time"]).isoformat(
        sep=" ", timespec="seconds"
    )
    message["message_type"] = _type_label(message["type"],
                                           message["media_type"])
    return message


def _type_label(msg_type, media_type):
    if msg_type == 344:
        return "系统通知"
    return {
        1: "图片", 5: "文件", 10: "视频", 14: "链接", 15: "GIF",
    }.get(media_type, "文本")


def _write_markdown(path, messages, start_date, end_date):
    with open(path, "w", encoding="utf-8", newline="\n") as file:
        file.write("# 群聊消息导出\n\n")
        file.write(f"- 日期：{start_date} 至 {end_date}\n")
        file.write(f"- 消息数：{len(messages)}\n\n")
        for message in messages:
            name = _markdown_text(message["from_name"] or "未知")
            file.write(f"## {message['datetime']} · {name}\n\n")
            file.write(f"- UID：{message['from_uid'] or ''}\n")
            file.write(f"- 类型：{message['message_type']}\n")
            if message["recall_status"]:
                file.write("- 状态：已撤回\n")
            file.write("\n")
            content = message["content"] or "[无文字内容]"
            file.write("\n".join(f"> {line}" for line in content.splitlines()))
            file.write("\n\n")


def _write_jsonl(path, messages):
    with open(path, "w", encoding="utf-8", newline="\n") as file:
        for message in messages:
            file.write(json.dumps(message, ensure_ascii=False) + "\n")


def _write_csv(path, messages):
    fields = ("id", "datetime", "from_uid", "from_name", "message_type",
              "content", "recall_status")
    with open(path, "w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(messages)


def _markdown_text(text):
    return str(text).replace("\\", "\\\\").replace("#", "\\#")
