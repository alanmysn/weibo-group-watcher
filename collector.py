"""采集器：长连接接入微博聊天服务，实时接收群消息并落库。

配方来源：2026-08-27 探针实测（03-方案设计.md §6.1）。
协议：Bayeux over WebSocket（wss://web.im.weibo.com/im）。
纪律：只读——本模块不存在任何发送类调用。
"""
import json
import logging
import os
import sqlite3
import threading
import time

import websocket

import runtime_state
import store

log = logging.getLogger("watcher.collector")

WS_URL = "wss://web.im.weibo.com/im"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
CONNECT_CYCLE_TIMEOUT = 175  # 服务端 advice timeout=170s，本地稍放宽


class SessionExpired(Exception):
    """会话被服务器作废（402/要求重新握手）——应重新握手而非退出。"""


def load_cookie_header(cookie_path):
    """cookies.json（探针封存格式）→ 请求头字符串。"""
    with open(cookie_path, encoding="utf-8") as f:
        cookies = json.load(f)
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def _recv_json(ws, timeout):
    """收一帧并解析为 list/dict；超时抛 WebSocketTimeoutException。"""
    ws.settimeout(timeout)
    return json.loads(ws.recv())


def _frame_items(frame):
    """Bayeux 帧可能是数组或对象，统一成元素列表。"""
    return frame if isinstance(frame, list) else [frame]


def start(cfg, stop_event=None):
    """阻塞式启动长连接采集。

    外层循环处理"会话被作废"（被更新端挤掉/服务器要求重新握手）：
    原地重连重新握手，温和退避，不自杀退出。
    """
    backoff = 2
    runtime_state.set_status("reconnecting", "启动中")
    # 进程级停机兜底：上次在线时刻与本次启动间隔过大 → 补记缺口
    # （进程被 kill/关机时来不及记缺口开始，靠这里事后认账）
    last_online = store.get_meta("last_online_at")
    if last_online:
        gap_secs = int(time.time()) - int(last_online)
        if gap_secs > 120:
            log.info("检测到停机缺口 %.1f 分钟，面板将标注", gap_secs / 60)
            store.open_gap(int(last_online))
    while True:
        try:
            _run_session(cfg, stop_event)
            runtime_state.set_status("stopped")
            return  # 正常停止
        except SessionExpired as e:
            log.warning("会话被服务器作废（%s），%d 秒后重新握手", e, backoff)
            runtime_state.set_status("reconnecting", f"会话失效: {e}")
            store.open_gap(int(time.time()))  # 记缺口开始（不自动拉取，面板标注）
        except (websocket.WebSocketException, OSError) as e:
            log.warning("连接异常断开（%s），%d 秒后重连", e, backoff)
            runtime_state.set_status("reconnecting", f"连接断开: {e}")
            store.open_gap(int(time.time()))
        if stop_event is not None and stop_event.is_set():
            runtime_state.set_status("stopped")
            return
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


def _run_session(cfg, stop_event=None):
    """单次会话：连接 → 握手 → 订阅 → 收消息循环。"""
    cookie_header = load_cookie_header(cfg["cookie_path"])
    gid = str(cfg["group_id"])
    my_uid = str(cfg.get("uid", ""))

    ws = websocket.create_connection(
        WS_URL,
        header=[f"Cookie: {cookie_header}", f"User-Agent: {USER_AGENT}"],
        suppress_origin=True,
        timeout=20,
        # 微博聊天服务器不发完整证书链（缺 GeoTrust 中间证书），用项目内置名单
        sslopt={"ca_certs": os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "ca_bundle.pem")},
    )
    log.info("已连上消息服务器 %s", WS_URL)

    # ① 握手
    ws.send(json.dumps([{
        "id": "1", "version": "1.0", "minimumVersion": "1.0",
        "channel": "/meta/handshake",
        "supportedConnectionTypes": ["websocket", "long-polling",
                                     "callback-polling"],
        "advice": {"timeout": 60000, "interval": 0},
    }]))
    client_id = None
    deadline = time.time() + 20
    while client_id is None:
        for item in _frame_items(_recv_json(ws, 20)):
            if item.get("channel") == "/meta/handshake":
                if not item.get("successful"):
                    raise RuntimeError(f"握手被拒: {item}")
                client_id = item.get("clientId")
    log.info("握手成功")

    # ② 订阅我的私人消息频道
    ws.send(json.dumps([{
        "id": "2", "channel": "/meta/subscribe",
        "subscription": f"/im/{my_uid}", "clientId": client_id,
    }]))
    subscribed = False
    deadline = time.time() + 20
    while not subscribed:
        for item in _frame_items(_recv_json(ws, 20)):
            if item.get("channel") == "/meta/subscribe":
                if not item.get("successful"):
                    raise RuntimeError(f"订阅失败（钥匙可能过期）: {item}")
                subscribed = True
    log.info("订阅成功，开始监听群消息…")

    # ③ 激活长轮询循环：advice-connect（探针帧序的第③帧，会话激活密钥）
    #    —— 缺了它，服务器不会把会话置入在线状态，首个普通 connect 捏满
    #    170 秒后必被 402（历史 bug：每 ~3 分钟被踢一层的真因）。
    conn_id = 2
    ws.send(json.dumps([{
        "id": "3", "channel": "/meta/connect", "connectionType": "websocket",
        "advice": {"timeout": 0}, "clientId": client_id,
    }]))
    n_stored = 0
    activated = False
    while not activated:
        for item in _frame_items(_recv_json(ws, 20)):
            if item.get("channel") == "/meta/connect":
                if not item.get("successful"):
                    raise SessionExpired(f"激活被拒: {item.get('error','')}")
                activated = True  # 回执已到 → 立即进入正式循环
                break
    log.info("长轮询循环已激活")
    store.close_gap(int(time.time()))  # 重连成功：闭合缺口（若有）
    store.set_meta("last_online_at", int(time.time()))  # 记录在线时刻
    runtime_state.set_status("online", "实时监听中")

    # ④ 普通 connect 循环：服务器捏住 ~170 秒 → 回复 → 立即发下一个。
    #    同一时刻只有一个未决 connect（这是 Bayeux 的规矩）。
    while True:
        if stop_event is not None and stop_event.is_set():
            ws.close()
            log.info("收到停止信号，采集器退出（共入库 %d 条）", n_stored)
            return
        conn_id += 1
        ws.send(json.dumps([{
            "id": str(conn_id), "channel": "/meta/connect",
            "connectionType": "websocket", "clientId": client_id,
        }]))
        # 等这一轮 connect 的回复（最长 ~175 秒），期间到来的数据帧照常入库
        while True:
            try:
                frame = _recv_json(ws, 175)
            except websocket.WebSocketTimeoutException:
                break  # 超时未回复 → 回到外层重发 connect
            replied = False
            for item in _frame_items(frame):
                channel = item.get("channel", "")
                if channel == "/meta/connect":
                    if not item.get("successful"):
                        err = item.get("error", "")
                        log.warning("connect 被拒(%s)——会话失效，将重新握手", err)
                        raise SessionExpired(err)
                    replied = True
                elif channel == "/im/" + my_uid:
                    n_stored += _handle_push(item, gid)
                elif channel.startswith("/meta/"):
                    log.debug("收到服务器状态帧")
            if replied:
                break


def _handle_push(item, gid):
    """处理一条 /im 推送；目标群消息入库，返回入库条数。"""
    data = item.get("data") or {}
    info = data.get("info") or {}
    sub_type = data.get("sub_type")
    if sub_type == 321 and str(info.get("gid")) == gid:
        return _store_message(info, gid)
    if sub_type == 332:
        log.info("收到群事件（撤回/状态类，sub_type=332，第 8 步前仅记录）")
        return 0
    log.debug("忽略推送 sub_type=%s type=%s", sub_type, data.get("type"))
    return 0


def _store_message(info, gid):
    """单条群消息落库（消息 ID 主键天然去重；重复时刷新富字段）。"""
    conn = store.get_conn()
    try:
        avatar_url = (info.get("from_user") or {}).get("profile_image_url", "")
        url_objects = json.dumps(info.get("url_objects") or [],
                                 ensure_ascii=False)
        cur = conn.execute(
            "INSERT OR IGNORE INTO messages "
            "(id, gid, from_uid, from_name, avatar_url, content, url_objects, "
            " type, media_type, time, recall_status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (info.get("id"), gid, str(info.get("from_uid", "")),
             (info.get("from_user") or {}).get("screen_name", ""),
             avatar_url, info.get("content", ""), url_objects,
             info.get("type"), info.get("media_type", 0), info.get("time"),
             info.get("recall_status", 0)),
        )
        # 重复消息也刷新头像/分享包（头像链接 3h 过期，重连回放时顺手续命）
        conn.execute("UPDATE messages SET avatar_url=?, url_objects=?, "
                     "from_name=?, recall_status=? WHERE id=?",
                     (avatar_url, url_objects,
                      (info.get("from_user") or {}).get("screen_name", ""),
                      info.get("recall_status", 0), info.get("id")))
        conn.commit()
        if cur.rowcount:
            name = (info.get("from_user") or {}).get("screen_name", "?")
            log.info("收到并入库 1 条消息")
            runtime_state.touch_message()
            try:
                import web
                web.broadcast({"id": info.get("id"),
                               "from_uid": str(info.get("from_uid", "")),
                               "from_name": name,
                               "content": info.get("content", ""),
                               "type": info.get("type"),
                               "media_type": info.get("media_type", 0),
                               "time": info.get("time"),
                               "recall_status": info.get("recall_status", 0),
                               "avatar_url": avatar_url,
                               "url_objects": url_objects})
            except Exception:
                pass  # 面板广播失败不影响采集主流程
            return 1
        log.debug("重复消息已跳过")
        return 0
    finally:
        conn.close()
