"""
配置模块 — 自动发现微信数据库路径，管理解密目录
"""
import json
import os
from pathlib import Path
from typing import Optional


# 微信 Mac 版数据根目录
WECHAT_ROOT = Path(
    "~/Library/Containers/com.tencent.xinWeChat/Data/Library/"
    "Application Support/com.tencent.xinWeChat/2.0b4.0.9"
).expanduser()

# 解密后数据库的默认输出目录（相对于本项目根）
PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_DECRYPTED_DIR = PROJECT_ROOT / "decrypted"

# keys.json 默认路径（由 extract_key.py 生成）
DEFAULT_KEYS_FILE = PROJECT_ROOT / "keys.json"


def find_wechat_account_dirs() -> list[Path]:
    """
    扫描 WECHAT_ROOT 下所有账号 Hash 目录（包含 Message/ 子目录的才算）。
    返回按修改时间降序排列的列表（最近使用的账号优先）。
    """
    if not WECHAT_ROOT.exists():
        return []

    account_dirs = []
    for d in WECHAT_ROOT.iterdir():
        if d.is_dir() and (d / "Message").exists():
            account_dirs.append(d)

    # 按最后修改时间降序（最近登录的账号排前）
    account_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return account_dirs


def get_primary_account_dir() -> Optional[Path]:
    """返回最近使用的微信账号目录"""
    dirs = find_wechat_account_dirs()
    return dirs[0] if dirs else None


def get_db_paths(account_dir: Path) -> dict[str, list[Path]]:
    """
    返回指定账号目录下各类数据库文件路径。

    结构：
      {
        "contact": [Path, ...],
        "session": [Path, ...],
        "message": [Path, ...],  # msg_0.db ~ msg_N.db
        "group":   [Path, ...],
      }
    """
    return {
        "contact": list((account_dir / "Contact").glob("*.db")),
        "session": list((account_dir / "Session").glob("*.db")),
        "message": sorted((account_dir / "Message").glob("msg_*.db")),
        "group":   list((account_dir / "Group").glob("*.db")),
    }


def load_keys(keys_file: Optional[Path] = None) -> dict[str, str]:
    """
    读取 keys.json（由 scripts/extract_key.py 生成）。
    格式示例：
      {
        "contact/wccontact_new2.db": "x'aabbcc...64hex...salt32hex'",
        "Message/msg_0.db": "x'aabbcc...'",
        ...
      }
    返回 {db相对路径: raw_key_hex_string}
    """
    path = keys_file or DEFAULT_KEYS_FILE
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_keys(keys: dict[str, str], keys_file: Optional[Path] = None) -> None:
    """保存密钥到 keys.json"""
    path = keys_file or DEFAULT_KEYS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(keys, f, indent=2, ensure_ascii=False)


def get_decrypted_dir(custom: Optional[str] = None) -> Path:
    """
    返回解密数据库目录。
    优先级：环境变量 WECHAT_DECRYPTED_DIR > custom 参数 > 默认值
    """
    env_val = os.environ.get("WECHAT_DECRYPTED_DIR")
    if env_val:
        return Path(env_val).expanduser()
    if custom:
        return Path(custom).expanduser()
    return DEFAULT_DECRYPTED_DIR


def check_decrypted_dir(decrypted_dir: Optional[Path] = None) -> tuple[bool, str]:
    """
    检查解密目录是否就绪，返回 (is_ready, message)。
    """
    d = decrypted_dir or get_decrypted_dir()
    if not d.exists():
        return False, (
            f"解密目录不存在: {d}\n"
            "请先运行:\n"
            "  1. python scripts/extract_key.py   # 提取密钥\n"
            "  2. python scripts/decrypt_db.py    # 解密数据库"
        )

    contact_db = d / "contact" / "contact.db"
    msg_dir = d / "message"

    if not contact_db.exists():
        return False, f"联系人数据库不存在: {contact_db}"
    if not msg_dir.exists() or not list(msg_dir.glob("message_*.db")):
        # 兼容旧版路径名
        if not list(msg_dir.glob("*.db")) if msg_dir.exists() else True:
            return False, f"消息数据库目录为空: {msg_dir}"

    return True, f"解密目录就绪: {d}"


def get_account_wxid() -> str:
    """推断本机登录账号的 wxid，用于把「谁发的」标成「我」还是具体的人。

    优先级：WECHAT_MY_WXID 环境变量 > WECHAT_FILES_DIR 下的账号目录名
    > 常见微信目录探测（CLI 也走这条路，不依赖 MCP 的 env）。
    目录名形如 wxid_xxxx_suffix，wxid 部分是 wxid_ 加字母数字。
    """
    import re

    env_wxid = os.environ.get("WECHAT_MY_WXID")
    if env_wxid:
        return env_wxid.strip()
    base = get_wechat_files_dir()
    if base is None or not base.exists():
        return ""
    for d in base.iterdir():
        if not d.is_dir():
            continue
        m = re.match(r"(wxid_[a-z0-9]+)_[0-9a-f]+$", d.name)
        if m:
            return m.group(1)
        if d.name.startswith("wxid_"):
            return d.name
    return ""


def get_wechat_files_dir() -> Optional[Path]:
    """定位微信数据根目录。

    优先级：WECHAT_FILES_DIR 环境变量 > 常见路径探测。
    找不到返回 None，调用方据此跳过自动刷新而不是报错。
    """
    env_val = os.environ.get("WECHAT_FILES_DIR", "").strip()
    if env_val:
        p = Path(env_val).expanduser()
        return p if p.exists() else None
    for cand in ("F:/xwechat_files", "D:/xwechat_files", "E:/xwechat_files"):
        p = Path(cand)
        if p.is_dir():
            return p
    p = Path.home() / "Documents" / "xwechat_files"
    return p if p.is_dir() else None


def get_db_storage(files_dir: Path) -> Optional[Path]:
    """返回最近活跃账号的 db_storage。"""
    try:
        dirs = [
            d for d in files_dir.iterdir()
            if d.is_dir() and (d / "db_storage").exists()
        ]
    except OSError:
        return None
    if not dirs:
        return None
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return dirs[0] / "db_storage"

