"""配置模块：加载本地私密配置（config.local.json）与 Cookie。"""
import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

REQUIRED_KEYS = ("group_id", "group_name", "cookie_file")


def load_config():
    """读取并校验 config.local.json，返回配置 dict（附 cookie_path 绝对路径）。"""
    path = os.path.join(BASE_DIR, "config.local.json")
    if not os.path.exists(path):
        raise SystemExit("找不到 config.local.json——私密配置文件缺失，请先创建。")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    for key in REQUIRED_KEYS:
        if not cfg.get(key):
            raise SystemExit(f"配置缺少必填项: {key}（请检查 config.local.json）")
    cookie_path = os.path.join(BASE_DIR, cfg["cookie_file"])
    if not os.path.exists(cookie_path):
        raise SystemExit(f"Cookie 文件不存在: {cfg['cookie_file']}")
    cfg["cookie_path"] = cookie_path
    return cfg


def masked_group_label():
    """群名的隐私掩码：日志中绝不出现群名/群 ID 明文。"""
    return "****"
