import sys, os
sys.path.insert(0, r'D:\my\AI商业\工具\wechat-msg-mcp')
from src.db.reader import WeChatDBReader

r = WeChatDBReader(r'D:\my\AI商业\工具\wechat-msg-mcp\decrypted')
print("=== my username ===", r._my_username or "(未识别)")
cs = r.get_contacts()
print("=== 联系人总数 ===", len(cs))
for c in cs[:10]:
    print("  ", c.username, "|", c.remark or c.nickname or c.username, "" if not c.is_group else "[群]")

print("=== 最近会话(前10) ===")
for s in r.get_sessions(limit=10):
    print(vars(s))

print("=== 搜索\"陪跑\"(前5) ===")
try:
    for m in r.search_messages("陪跑", limit=5):
        print(vars(m))
except Exception as e:
    print("搜索失败:", e)

print("=== 搜索\"独树\"(前3) ===")
try:
    for m in r.search_messages("独树", limit=3):
        print(vars(m))
except Exception as e:
    print("搜索失败:", e)
