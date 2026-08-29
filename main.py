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

    cfg = config.load_config()
    log = logging.getLogger("watcher")
    log.info("采集器启动（群：%s）", config.masked_group_label())
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
