#!/usr/bin/env python3
"""快速解密：用 keys.json 里的密钥刷新 decrypted/。

实现已移到 src/decrypt.py，本脚本只是命令行外壳。
默认只解源库比副本新的库（增量），加 --full 全量重解。
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import decrypt
from src.config import get_db_storage, get_decrypted_dir, get_wechat_files_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="用已保存密钥解密微信数据库")
    parser.add_argument("--full", action="store_true", help="全量重解，忽略 mtime 判断")
    args = parser.parse_args()

    keys_file = ROOT / "keys.json"
    if not keys_file.exists():
        print(f"找不到 {keys_file}，请先运行 scripts/decrypt_standalone.py 生成密钥")
        return 1

    files_dir = get_wechat_files_dir()
    if files_dir is None:
        print("找不到微信数据目录，请设置环境变量 WECHAT_FILES_DIR")
        return 1

    db_storage = get_db_storage(files_dir)
    if db_storage is None:
        print(f"{files_dir} 下没有账号目录包含 db_storage")
        return 1

    out = get_decrypted_dir()
    print(f"使用 {keys_file.name}")
    print(f"源目录: {db_storage}")
    print(f"输出目录: {out}")
    print()

    result = decrypt.refresh(db_storage, out, keys_file, only_changed=not args.full)
    for rel in result["changed"]:
        print(f"  解密 {rel}")
    detail = result.get("failed_detail") or {}
    for rel in result["failed"]:
        print(f"  失败 {rel}（{detail.get(rel, '未知')}）")
    print()
    print(
        f"检查 {result['checked']} 个库，重新解密 {len(result['changed'])} 个，"
        f"失败 {len(result['failed'])} 个，耗时 {result['seconds']} 秒"
    )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
