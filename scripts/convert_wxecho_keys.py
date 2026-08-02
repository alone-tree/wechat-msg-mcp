#!/usr/bin/env python3
"""
将 wxecho_keys.json 转换为本项目 keys.json 格式

wxecho_keys.json 格式：
  {"contact/contact.db": {"enc_key": "<64hex>"}, ...}

keys.json 目标格式（与 Mac 版完全一致）：
  {"xwechat_files/.../contact/contact.db": "x'<64hex_key><32hex_salt>'", ...}

enc_key 是 32 字节直接 AES key，salt 从数据库文件前 16 字节读取。
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT   = Path(__file__).parent.parent
WXECHO_KEYS    = PROJECT_ROOT / "wxecho_keys.json"
OUTPUT_KEYS    = PROJECT_ROOT / "keys.json"

# xwechat_files 根目录
def get_xwechat_base() -> Path:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat") as key:
            val, _ = winreg.QueryValueEx(key, "FileSavePath")
            if val and val != "MyDocument:":
                c = Path(val) / "xwechat_files"
                if c.exists():
                    return c
    except Exception:
        pass
    return Path.home() / "Documents" / "xwechat_files"


def main():
    if not WXECHO_KEYS.exists():
        print(f"❌ 找不到 {WXECHO_KEYS}")
        sys.exit(1)

    with open(WXECHO_KEYS, encoding="utf-8") as f:
        wxecho: dict = json.load(f)

    xwechat_base = get_xwechat_base()
    print(f"xwechat_files: {xwechat_base}")

    # 找到账号目录（db_storage 的父目录）
    account_dir: Path | None = None
    if xwechat_base.exists():
        candidates = sorted(
            [d for d in xwechat_base.iterdir() if d.is_dir() and (d / "db_storage").exists()],
            key=lambda p: p.stat().st_mtime, reverse=True
        )
        if candidates:
            account_dir = candidates[0] / "db_storage"
            print(f"账号目录:      {account_dir}")

    result: dict[str, str] = {}

    for rel_path, info in wxecho.items():
        enc_key_hex = info.get("enc_key", "")
        if len(enc_key_hex) != 64:
            print(f"  ⚠️  跳过 {rel_path}（enc_key 长度不对：{len(enc_key_hex)}）")
            continue

        # 尝试读取对应数据库文件的 salt（前 16 字节）
        salt_hex = None
        if account_dir:
            db_path = account_dir / rel_path.replace("/", "\\")
            if db_path.exists():
                try:
                    salt_hex = db_path.read_bytes()[:16].hex()
                except Exception:
                    pass

        if salt_hex is None:
            # 无法读取 salt，用全零占位（decrypt_db.py 会优先用 salt 匹配，找不到时回退直接 key 模式）
            salt_hex = "00" * 16
            print(f"  ⚠️  {rel_path} 无法读取 salt，使用占位符")

        # 格式：x'<64hex_key><32hex_salt>'
        key_value = f"x'{enc_key_hex}{salt_hex}'"

        # 用相对路径作为 key（与 Mac 版格式一致）
        if account_dir:
            rel_key = str(account_dir.relative_to(xwechat_base.parent) / rel_path).replace("\\", "/")
        else:
            rel_key = rel_path

        result[rel_key] = key_value
        print(f"  ✅  {Path(rel_path).name}")

    if not result:
        print("❌ 转换结果为空")
        sys.exit(1)

    with open(OUTPUT_KEYS, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print()
    print(f"✅ 已生成 {OUTPUT_KEYS}（{len(result)} 条）")
    print()
    print("下一步：运行解密")
    print("  uv run python scripts\\decrypt_db.py")


if __name__ == "__main__":
    main()
