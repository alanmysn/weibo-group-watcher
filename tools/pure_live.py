# -*- coding: utf-8 -*-
"""实验脚本：纯长连接模式（方案 A 隔离实验）。

与 main.py run 的差异：**不调用任何拉取接口**——
- 无启动补漏
- 无每小时对账（start_periodic）
- 无面板服务
只做：连接 → 订阅 → 激活 → 实时收消息 → 入库。

用途：验证"长连接接收消息是否会把微博服务器已读位置推到最新"。
如果手机端角标在纯长连接下正常累计 → 长连接清白，罪魁是定时拉取；
如果角标依然消失 → 长连接本身也推进已读，需走外部推送方案。

用法：.venv/Scripts/python.exe tools/pure_live.py
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import collector
import store


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(
                os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "logs", "watcher.log"),
                encoding="utf-8",
            ),
            logging.StreamHandler(sys.stdout),
        ],
    )
    cfg = config.load_config()
    store.init_db()
    log = logging.getLogger("watcher")
    log.info("【纯长连接实验】仅订阅+收消息，不调用任何拉取接口")
    log.info("采集器启动（群：%s）", config.masked_group_label())
    collector.start(cfg, None)


if __name__ == "__main__":
    main()
