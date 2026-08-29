# -*- coding: utf-8 -*-
"""诊断：query_messages 直调 10012 的变量矩阵实验。"""
import json

import requests

cfg = json.load(open("config.local.json", encoding="utf-8"))
cks = json.load(open(cfg["cookie_file"], encoding="utf-8"))
cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cks)
gid = cfg["group_id"]
BASE = ("https://api.weibo.com/webim/groupchat/query_messages.json"
        f"?convert_emoji=1&query_sender=1&id={gid}&max_mid=0&source=209678993")

xsrf = next((c["value"] for c in cks if c["name"] == "XSRF-TOKEN"), "")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"

cases = [
    ("A 裸UA+无Referer", {"Cookie": cookie_header, "User-Agent": "Mozilla/5.0"},
     "&count=20"),
    ("B 全UA+无Referer", {"Cookie": cookie_header, "User-Agent": UA}, "&count=20"),
    ("C 全UA+Referer+count20",
     {"Cookie": cookie_header, "User-Agent": UA,
      "Referer": "https://api.weibo.com/chat", "Accept": "application/json"},
     "&count=20"),
    ("D 全UA+Referer+XSRF头+count50",
     {"Cookie": cookie_header, "User-Agent": UA,
      "Referer": "https://api.weibo.com/chat", "X-Xsrf-Token": xsrf,
      "Accept": "application/json"},
     "&count=50"),
]

for name, headers, extra in cases:
    try:
        r = requests.get(BASE + extra, headers=headers, timeout=15)
        body = r.text[:120].replace("\n", "")
        ok = '"messages"' in r.text
        print(f"{name}: HTTP {r.status_code} {'✅有数据' if ok else '❌'} {body}")
    except Exception as e:
        print(f"{name}: 异常 {e}")
