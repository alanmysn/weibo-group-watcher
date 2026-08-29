# -*- coding: utf-8 -*-
"""深挖：历史接口的 max_mid 翻页行为——13:08-13:15 的消息到底在不在服务器。"""
import json

import requests

cfg = json.load(open("config.local.json", encoding="utf-8"))
cks = json.load(open(cfg["cookie_file"], encoding="utf-8"))
header = "; ".join(f"{c['name']}={c['value']}" for c in cks)
xsrf = next((c["value"] for c in cks if c["name"] == "XSRF-TOKEN"), "")
headers = {"Cookie": header, "Referer": "https://api.weibo.com/chat",
           "Accept": "application/json", "X-Xsrf-Token": xsrf,
           "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
URL = "https://api.weibo.com/webim/groupchat/query_messages.json"
gid = cfg["group_id"]

import datetime
max_mid = 0
for page in range(5):
    r = requests.get(URL, params={"convert_emoji": 1, "query_sender": 1,
                                  "count": 20, "id": gid, "max_mid": max_mid,
                                  "source": "209678993"},
                     headers=headers, timeout=15)
    msgs = r.json().get("messages") or []
    print(f"--- 第{page+1}页: {len(msgs)} 条 ---")
    for m in msgs:
        t = datetime.datetime.fromtimestamp(m["time"]).strftime("%m-%d %H:%M:%S")
        print(f"  id={m.get('id')} {t} [{m.get('from_user',{}).get('screen_name','?')}] {m.get('content','')[:24]}")
    if not msgs:
        break
    max_mid = min(m.get("id") for m in msgs)
