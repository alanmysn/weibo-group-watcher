# -*- coding: utf-8 -*-
"""诊断脚本：复刻采集器的连接流程，把收到的每一帧原始内容都打出来。"""
import json
import logging
import sys
import time

import websocket

import config
import collector

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("diag")

cfg = config.load_config()
cookie_header = collector.load_cookie_header(cfg["cookie_path"])
my_uid = str(cfg["uid"])

ws = websocket.create_connection(
    collector.WS_URL,
    header=[f"Cookie: {cookie_header}",
            f"User-Agent: {collector.USER_AGENT}"],
    suppress_origin=True, timeout=20,
    sslopt={"ca_certs": "ca_bundle.pem"},
)
log.info("已连接，开始握手")

ws.send(json.dumps([{
    "id": "1", "version": "1.0", "minimumVersion": "1.0",
    "channel": "/meta/handshake",
    "supportedConnectionTypes": ["websocket", "long-polling",
                                 "callback-polling"],
    "advice": {"timeout": 60000, "interval": 0},
}]))
client_id = None
while client_id is None:
    for item in collector._frame_items(collector._recv_json(ws, 20)):
        log.info("<< %s", json.dumps(item, ensure_ascii=False)[:200])
        if item.get("channel") == "/meta/handshake":
            client_id = item["clientId"]

ws.send(json.dumps([{
    "id": "2", "channel": "/meta/subscribe",
    "subscription": f"/im/{my_uid}", "clientId": client_id,
}]))
subscribed = False
while not subscribed:
    for item in collector._frame_items(collector._recv_json(ws, 20)):
        log.info("<< %s", json.dumps(item, ensure_ascii=False)[:200])
        if item.get("channel") == "/meta/subscribe":
            subscribed = item.get("successful", False)

log.info("=== 订阅完成，进入监听 120 秒，打印所有原始帧 ===")
cid = 2
end = time.time() + 120
while time.time() < end:
    cid += 1
    ws.send(json.dumps([{
        "id": str(cid), "channel": "/meta/connect",
        "connectionType": "websocket", "clientId": client_id,
    }]))
    while True:
        try:
            frame = collector._recv_json(ws, 170)
        except websocket.WebSocketTimeoutException:
            log.info("（本轮 connect 170 秒无任何帧，服务器超时放行）")
            break
        log.info("<< %s", json.dumps(frame, ensure_ascii=False)[:500])
        for item in collector._frame_items(frame):
            if item.get("channel") == "/meta/connect":
                break
        else:
            continue
        break
ws.close()
log.info("诊断结束")
