"""weibo-group-watcher 入口。

用法：
    python main.py init    初始化：检查配置、建库建表
（后续步骤将增加 run / serve 等子命令）
"""
import argparse
import logging
import os
import sys

import config
import store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(
                os.path.join(LOG_DIR, "watcher.log"), encoding="utf-8"
            ),
            logging.StreamHandler(sys.stdout),
        ],
    )


def cmd_init(_args):
    cfg = config.load_config()
    store.init_db()
    log = logging.getLogger("watcher")
    log.info("配置加载成功（群：%s）", config.masked_group_label())
    log.info("Cookie 文件已就位：%s", os.path.basename(cfg["cookie_path"]))
    log.info("数据库就绪：%s", store.DB_PATH)


def cmd_run(_args):
    import collector
    import web

    cfg = config.load_config()
    store.init_db()  # 幂等建表+老库自动补列
    log = logging.getLogger("watcher")
    log.info("采集器启动（群：%s）", config.masked_group_label())

    # 面板服务（后台线程，仅本机）
    web.start(cfg)

    # 长连接采集（只收不拉——拉取会推进微博已读位置、清掉手机端角标，
    # 已由 2026-08-30 对照实验实锤；缺口不自动补，面板如实标注，
    # 由用户手动触发补漏，见 web /api/backfill）
    collector.start(cfg)


def main():
    setup_logging()
    parser = argparse.ArgumentParser(description="weibo-group-watcher")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="初始化：检查配置、建库建表")
    sub.add_parser("run", help="启动采集器（长连接监听群消息）")
    args = parser.parse_args()
    if args.command == "init":
        cmd_init(args)
    elif args.command == "run":
        cmd_run(args)


if __name__ == "__main__":
    main()
