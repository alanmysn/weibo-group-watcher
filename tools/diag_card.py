# -*- coding: utf-8 -*-
"""查证：分享消息 url_objects[0] 的 status/info 完整字段（找标题与缩略图）。"""
import json

import requests

cfg = json.load(open("config.local.json", encoding="utf-8"))
cks = json.load(open(cfg["cookie_file"], encoding="utf-8"))
header = "; ".join(f"{c['name']}={c['value']}" for c in cks)
xsrf = next((c["value"] for c in cks if c["name"] == "XSRF-TOKEN"), "")
H = {"Cookie": header, "Referer": "https://api.weibo.com/chat",
     "Accept": "application/json", "X-Xsrf-Token": xsrf,
     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

r = requests.get("https://api.weibo.com/webim/groupchat/query_messages.json",
                 params={"convert_emoji": 1, "query_sender": 1, "count": 50,
                         "id": cfg["group_id"], "max_mid": 0,
                         "source": "209678993"},
                 headers=H, timeout=15)
msgs = r.json().get("messages") or []
share = next((m for m in msgs if m.get("media_type") == 14), None)
if not share:
    print("最近50条无分享消息")
    raise SystemExit
uo = share["url_objects"][0]
status = uo.get("status") or {}
info = uo.get("info") or {}
print("=== status 字段（找标题/正文/图）===")
for k, v in status.items():
    print(f"  {k}: {str(v)[:100]}")
print("=== info 字段 ===")
for k, v in info.items():
    print(f"  {k}: {str(v)[:100]}")
