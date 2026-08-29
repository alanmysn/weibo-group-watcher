# -*- coding: utf-8 -*-
"""诊断：复现 backfill 的精确请求，对比 count=50 与 count=20 的返回。"""
import json

import requests

cfg = json.load(open("config.local.json", encoding="utf-8"))
cks = json.load(open(cfg["cookie_file"], encoding="utf-8"))
header = "; ".join(f"{c['name']}={c['value']}" for c in cks)
xsrf = next((c["value"] for c in cks if c["name"] == "XSRF-TOKEN"), "")
headers = {"Cookie": header, "Referer": "https://api.weibo.com/chat",
           "Accept": "application/json", "X-Xsrf-Token": xsrf,
           "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/152.0.0.0 Safari/537.36")}
URL = "https://api.weibo.com/webim/groupchat/query_messages.json"

for count in (50, 20):
    r = requests.get(URL, params={"convert_emoji": 1, "query_sender": 1,
                                  "count": count, "id": cfg["group_id"],
                                  "max_mid": 0, "source": "209678993"},
                     headers=headers, timeout=15)
    d = r.json()
    msgs = d.get("messages") or []
    print(f"count={count}: HTTP {r.status_code}, 返回 {len(msgs)} 条, "
          f"keys={list(d.keys())}")
    if msgs:
        import datetime
        for m in msgs[:3] + msgs[-3:]:
            t = datetime.datetime.fromtimestamp(m["time"]).strftime("%H:%M:%S")
            print(f"   id={m.get('id')} {t}")
        print("   首条ID < 末条ID ?", msgs[0]["id"] < msgs[-1]["id"])
    else:
        print("   原始:", r.text[:200])
