# -*- coding: utf-8 -*-
"""查证：①完整分享消息（media_type=14）的 url_objects 字段结构；
②/webim/emotions.json 表情接口的返回格式。"""
import json

import requests

cfg = json.load(open("config.local.json", encoding="utf-8"))
cks = json.load(open(cfg["cookie_file"], encoding="utf-8"))
header = "; ".join(f"{c['name']}={c['value']}" for c in cks)
xsrf = next((c["value"] for c in cks if c["name"] == "XSRF-TOKEN"), "")
H = {"Cookie": header, "Referer": "https://api.weibo.com/chat",
     "Accept": "application/json", "X-Xsrf-Token": xsrf,
     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# ① 找一条分享消息
r = requests.get("https://api.weibo.com/webim/groupchat/query_messages.json",
                 params={"convert_emoji": 1, "query_sender": 1, "count": 50,
                         "id": cfg["group_id"], "max_mid": 0,
                         "source": "209678993"},
                 headers=H, timeout=15)
msgs = r.json().get("messages") or []
share = next((m for m in msgs if m.get("media_type") == 14), None)
if share:
    uo = share.get("url_objects") or []
    print("=== 分享消息 media_type=14 ===")
    print("content:", share.get("content"))
    print("url_objects[0] 全部字段:")
    for k, v in (uo[0].items() if uo else []):
        s = str(v)
        print(f"  {k}: {s[:90]}")
else:
    print("最近 50 条里没有分享消息")

# ② 表情接口
try:
    r2 = requests.get("https://api.weibo.com/webim/emotions.json",
                      params={"source": "209678993"},
                      headers=H, timeout=15)
    d2 = r2.json()
    print("\n=== emotions.json ===")
    if isinstance(d2, dict):
        print("顶层 keys:", list(d2.keys())[:10])
        for k in list(d2.keys())[:3]:
            print(f"  {k}: {str(d2[k])[:120]}")
    else:
        print("返回类型:", type(d2), str(d2)[:200])
except Exception as e:
    print("\nemotions.json 请求失败:", e)
