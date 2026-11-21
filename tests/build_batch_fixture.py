"""Build disposable fake WeChat data for the batch-workflow smoke test."""
import argparse
import hashlib
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path


def build(root: Path):
    root.mkdir(parents=True, exist_ok=False)
    (root / "contact").mkdir()
    (root / "message").mkdir()
    with closing(sqlite3.connect(root / "contact/contact.db")) as db, db:
        db.executescript("""
            CREATE TABLE contact(id INTEGER PRIMARY KEY, username TEXT, local_type INTEGER, nick_name TEXT);
            CREATE TABLE chatroom_member(room_id INTEGER, member_id INTEGER);
        """)
        contacts = [(1, "demo_host", 1, "小林"), (2, "demo_helper", 1, "小唐")]
        contacts += [(i + 2, f"{9900+i}@chatroom", 2, f"体验样例{i}") for i in range(1, 7)]
        db.executemany("INSERT INTO contact VALUES (?,?,?,?)", contacts)
        db.executemany("INSERT INTO chatroom_member VALUES (?,?)", [(i+2, p) for i in range(1, 7) for p in (1, 2)])
    for shard in range(2):
        with closing(sqlite3.connect(root / f"message/message_{shard}.db")) as db, db:
            db.execute("CREATE TABLE Name2Id(user_name TEXT)")
            db.executemany("INSERT INTO Name2Id VALUES (?)", [(u[1],) for u in contacts])
            for i in range(1, 6):
                cid = f"{9900+i}@chatroom"
                name = "Msg_" + hashlib.md5(cid.encode()).hexdigest()
                db.execute(f'CREATE TABLE "{name}"(local_id,create_time,local_type,real_sender_id,message_content,WCDB_CT_message_content)')
                entries = {
                    1: ("2026-11-18 11:56:00", None),
                    2: ("2026-11-18 16:15:00", None),
                    3: ("2026-11-17 12:00:00", "2026-11-17 17:00:00"),
                    4: ("2026-11-14 10:00:00", None),
                    5: ("2026-10-01 10:00:00", None),
                }
                started, paid = entries[i]
                stamp = started if shard == 0 else paid
                text = "邀请你加入了群聊" if shard == 0 else "已确认收到学费"
                if stamp:
                    ts = int(datetime.fromisoformat(stamp).timestamp())
                    db.execute(f'INSERT INTO "{name}" VALUES (?,?,?,?,?,?)', (shard+1, ts, 10000 if shard == 0 else 1, 1, text, 0))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    build(parser.parse_args().out)
