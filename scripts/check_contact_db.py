import sqlite3
from pathlib import Path

db = Path("decrypted/contact/contact.db")
conn = sqlite3.connect(str(db))

# 直接测试 reader.py 里的查询
try:
    rows = conn.execute("SELECT id, username, remark, nick_name FROM contact LIMIT 5").fetchall()
    print(f"查询成功，{len(rows)} 行:")
    for r in rows:
        print(f"  id={r[0]}, username={r[1]}, remark={r[2]}, nick_name={r[3]}")
except Exception as e:
    print(f"查询失败: {e}")

conn.close()
