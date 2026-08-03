import sqlite3
from pathlib import Path
from datetime import datetime

def mtime(p):
    try:
        return datetime.fromtimestamp(p.stat().st_mtime).strftime('%m-%d %H:%M:%S')
    except:
        return 'N/A'

xbase = Path.home() / 'Documents/xwechat_files'
dec   = Path('decrypted/message')

print('=== 源文件（xwechat_files）===')
for wxid in xbase.iterdir():
    src = wxid / 'db_storage/message'
    if not src.exists():
        continue
    for db in sorted(src.glob('*.db')):
        if 'fts' in db.name:
            continue
        wal = Path(str(db) + '-wal')
        wal_info = f'WAL={wal.stat().st_size//1024}KB' if wal.exists() else 'no WAL'
        print(f'  {db.name}: mtime={mtime(db)}, {wal_info}')

print()
print('=== 解密文件（decrypted/）===')
if dec.exists():
    for db in sorted(dec.glob('message_*.db')):
        try:
            conn = sqlite3.connect(f'file:{db}?mode=ro&immutable=1', uri=True)
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            ).fetchall()]
            max_ts = 0
            for t in tables:
                row = conn.execute(f'SELECT MAX(CreateTime) FROM [{t}]').fetchone()
                if row and row[0] and row[0] > max_ts:
                    max_ts = row[0]
            conn.close()
            latest = datetime.fromtimestamp(max_ts).strftime('%m-%d %H:%M:%S') if max_ts else 'empty'
        except Exception as e:
            latest = str(e)[:60]
        print(f'  {db.name}: mtime={mtime(db)}, 最新消息={latest}')
