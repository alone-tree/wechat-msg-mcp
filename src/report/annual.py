"""年度报告：扫描消息分库，产出六份文件。

入口是 run(src, year, out_dir)，src 是 LocalSource。
只读打开 decrypted/ 下的库，按分库与分表分批读取，不把全量消息读进内存。
依据 docs/重构方案-2026-11-13.md 第 10 节。
"""
from __future__ import annotations

import csv
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import zstandard as zstd

from src.db.open_ import connect_ro, table_exists
from src.db.sender import build_local_sender_map

from .metrics import (
    AnnualAggregator,
    chat_kind,
    hash8,
    render_markdown,
)

SQL_TIME_FMT = "%Y-%m-%d %H:%M:%S"
MSG_TABLE_SQL = "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"


def _msg_tables(conn) -> list[str]:
    return [row[0] for row in conn.execute(MSG_TABLE_SQL)]


def _md5_map(conn) -> dict[str, str]:
    """本库会话分表名反查：md5(user_name) 到 user_name。

    注意 src/db/sender.build_name2id 里查的是 userName（camelCase），
    真实列名是 user_name，取不到会静默返回空表，这里自己查。
    """
    mapping: dict[str, str] = {}
    if not table_exists(conn, "Name2Id"):
        return mapping
    for _rid, name in conn.execute("SELECT rowid, user_name FROM Name2Id"):
        if name:
            mapping[hashlib.md5(name.encode("utf-8", "replace")).hexdigest()] = name
    return mapping


def _ts_text(ts: int) -> str:
    return datetime.fromtimestamp(int(ts)).strftime(SQL_TIME_FMT)


class _Decoder:
    """WCDB_CT_message_content=4 时用 zstd 解压，其余按 UTF-8。"""

    def __init__(self) -> None:
        self._zstd = zstd.ZstdDecompressor()

    def __call__(self, raw, flag: int) -> str:
        if raw is None:
            return ""
        if isinstance(raw, bytes):
            if flag == 4:
                try:
                    return self._zstd.decompress(raw).decode("utf-8", "replace")
                except Exception:
                    return ""
            return raw.decode("utf-8", "replace")
        if flag == 4:
            try:
                return self._zstd.decompress(raw.encode("latin-1", "ignore")).decode(
                    "utf-8", "replace"
                )
            except Exception:
                return str(raw)
        return str(raw)


def _contact_names(src) -> dict[str, str]:
    """username 到显示名，备注优先，其次昵称；取不到就不进字典。"""
    names: dict[str, str] = {}
    path = Path(src.path("contact"))
    if not path.exists():
        return names
    conn = connect_ro(path)
    try:
        if table_exists(conn, "contact"):
            for username, remark, nick in conn.execute(
                "SELECT username, remark, nick_name FROM contact"
            ):
                if not username:
                    continue
                display = (remark or "").strip() or (nick or "").strip()
                if display:
                    names[username] = display
    finally:
        conn.close()
    return names


def _profile(src, start: int, end: int) -> dict:
    """第一遍：每个会话的全量条数、首条时间、本年度条数，以及发送者出现的会话集合。"""
    chats: dict[str, dict] = {}
    senders: dict[str, set] = {}
    tables = 0
    for db_path in src.message_dbs():
        conn = connect_ro(db_path)
        try:
            if not table_exists(conn, "Name2Id"):
                continue
            md5map = _md5_map(conn)
            sender_map = build_local_sender_map(conn)
            for table in _msg_tables(conn):
                tables += 1
                chat = md5map.get(table[4:])
                if not chat:
                    continue
                row = conn.execute(
                    "SELECT count(*), min(create_time), max(create_time),"
                    " sum(create_time>=? AND create_time<?)"
                    f' FROM "{table}"',
                    (start, end),
                ).fetchone()
                entry = chats.setdefault(
                    chat, {"total_all": 0, "first_ts": None, "year_total": 0}
                )
                entry["total_all"] += row[0] or 0
                if row[1] is not None:
                    entry["first_ts"] = (
                        row[1] if entry["first_ts"] is None else min(entry["first_ts"], row[1])
                    )
                entry["year_total"] += row[3] or 0
                for (sender_id,) in conn.execute(
                    'SELECT DISTINCT real_sender_id FROM "%s"'
                    " WHERE create_time>=? AND create_time<?" % table,
                    (start, end),
                ):
                    if sender_id is None:
                        continue
                    name = sender_map.get(int(sender_id))
                    if name is None:
                        continue
                    senders.setdefault(name, set()).add(chat)
        finally:
            conn.close()
    return {"chats": chats, "senders": senders, "tables": tables}


def _pick_self(senders: dict[str, set], chat_count: int) -> Optional[str]:
    """自己的 wxid：出现在最多会话里的那个发送者。

    本机 Name2Id 不写空串，公众号会话也不带自己，所以用跨会话覆盖率推断。
    找不到明显领先者时返回 None，报告方向字段会退化为 unknown。
    """
    if not senders:
        return None
    ranked = sorted(senders.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    top, top_chats = ranked[0]
    second = len(ranked[1][1]) if len(ranked) > 1 else 0
    if len(top_chats) >= 3 and len(top_chats) >= 2 * second and len(top_chats) >= 0.4 * max(chat_count, 1):
        return top
    return None


def _scan(src, start: int, end: int, self_wxid: Optional[str], agg: AnnualAggregator, writer) -> int:
    """第二遍：按会话顺序读本年度消息，喂给聚合器并写明细。返回读取行数。"""
    decoder = _Decoder()
    rows = 0
    for db_path in src.message_dbs():
        conn = connect_ro(db_path)
        try:
            if not table_exists(conn, "Name2Id"):
                continue
            md5map = _md5_map(conn)
            sender_map = build_local_sender_map(conn)
            for table in _msg_tables(conn):
                chat = md5map.get(table[4:]) or ""
                kind = chat_kind(chat)
                cursor = conn.execute(
                    "SELECT create_time, local_type, real_sender_id, message_content,"
                    " WCDB_CT_message_content"
                    f' FROM "{table}" WHERE create_time>=? AND create_time<? ORDER BY create_time',
                    (start, end),
                )
                for ts, local_type, sender_id, raw, flag in cursor:
                    text = decoder(raw, flag or 0)
                    sender: Optional[str] = None
                    if sender_id is not None:
                        sender = sender_map.get(int(sender_id))
                    is_self = sender == "" or (self_wxid is not None and sender == self_wxid)
                    speaker = "" if is_self else (sender or "unknown")
                    main_name, sub_name = agg.add(
                        chat, kind, int(ts), local_type, speaker, text, is_self
                    )
                    if is_self:
                        direction = "sent"
                        sender_hash = "self"
                    elif sender:
                        direction = "received"
                        sender_hash = hash8(sender)
                    else:
                        direction = "unknown"
                        sender_hash = "unknown"
                    writer.writerow(
                        [
                            _ts_text(ts),
                            direction,
                            kind,
                            hash8(chat),
                            sender_hash,
                            main_name,
                            sub_name,
                            len(text),
                        ]
                    )
                    rows += 1
        finally:
            conn.close()
    return rows


def _new_friends(src, start: int, end: int) -> dict:
    """新增好友的近似口径：FMessageTable 里 type=37 的当年好友申请。"""
    result = {"count": 0, "note": "fmessage_type37_requests"}
    path = Path(src.path("general"))
    if not path.exists():
        result["note"] = "general_db_missing"
        return result
    conn = connect_ro(path)
    try:
        if not table_exists(conn, "FMessageTable"):
            result["note"] = "fmessage_table_missing"
            return result
        row = conn.execute(
            "SELECT count(*) FROM FMessageTable"
            " WHERE type_=37 AND is_sender_=0 AND timestamp_>=? AND timestamp_<?",
            (start, end),
        ).fetchone()
        result["count"] = int(row[0] or 0)
    finally:
        conn.close()
    return result


def _new_chats(profile: dict, start: int, end: int) -> dict:
    """新出现会话的近似口径：全量数据里首条消息落在本年度。"""
    total = 0
    groups = 0
    for chat, entry in profile["chats"].items():
        first = entry.get("first_ts")
        if not first or not (start <= first < end):
            continue
        if not entry.get("year_total"):
            continue
        total += 1
        if chat.endswith("@chatroom"):
            groups += 1
    return {"count": total, "groups": groups, "note": "first_message_in_year"}


def _display_names(names: dict[str, str]):
    def name_of(username: str) -> str:
        if not username:
            return "未知会话"
        display = names.get(username)
        if display:
            return display
        if username.endswith("@chatroom"):
            return "群-" + hash8(username)
        return "联系人-" + hash8(username)

    return name_of


def _write_csv(path: Path, header: list[str], rows) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)


def run(src, year: int, out_dir: str) -> dict:
    """生成某年的年度报告，返回 {ok, files, stats}。"""
    started = time.perf_counter()
    year = int(year)
    start = int(datetime(year, 1, 1).timestamp())
    end = int(datetime(year + 1, 1, 1).timestamp())
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    names = _contact_names(src)
    name_of = _display_names(names)
    profile = _profile(src, start, end)
    active_chats = sum(1 for entry in profile["chats"].values() if entry["year_total"])
    self_wxid = _pick_self(profile["senders"], active_chats)
    agg = AnnualAggregator(year)

    messages_path = out / "messages.csv"
    with open(messages_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "time",
                "direction",
                "chat_kind",
                "chat_hash",
                "sender_hash",
                "type",
                "subtype",
                "text_length",
            ]
        )
        scanned = _scan(src, start, end, self_wxid, agg, writer)

    expected = sum(entry["year_total"] for entry in profile["chats"].values())
    relations = {
        "new_friends": _new_friends(src, start, end),
        "new_chats": _new_chats(profile, start, end),
    }
    summary = agg.finish(name_of, relations)

    generated_at = datetime.now().strftime(SQL_TIME_FMT)
    meta = {
        "year": year,
        "generated_at": generated_at,
        "data_until": src.data_until() if hasattr(src, "data_until") else "",
        "message_dbs": len(src.message_dbs()),
        "tables": profile["tables"],
        "db_messages_total": sum(e["total_all"] for e in profile["chats"].values()),
        "self_detected": self_wxid is not None,
        "scanned_rows": scanned,
        "expected_rows": expected,
    }
    summary["meta"] = meta

    with open(out / "report.json", "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    _write_csv(
        out / "people.csv",
        [
            "chat_hash",
            "name",
            "total",
            "received",
            "sent",
            "share",
            "my_ratio",
            "first_time",
            "last_time",
            "active_days",
        ],
        (
            [
                row["chat_hash"],
                row["name"],
                row["total"],
                row["received"],
                row["sent"],
                row["share"],
                row["my_ratio"],
                row["first_time"],
                row["last_time"],
                row["active_days"],
            ]
            for row in summary["people"]
        ),
    )

    groups_header = [
        "chat_hash",
        "label",
        "total",
        "mine",
        "my_ratio",
        "active_days",
    ]
    for i in range(5):
        groups_header.extend([f"top{i + 1}_name", f"top{i + 1}_count"])

    def group_rows():
        for row in summary["groups"]:
            cells = [
                row["chat_hash"],
                row["label"],
                row["total"],
                row["mine"],
                row["my_ratio"],
                row["active_days"],
            ]
            speakers = row["top_speakers"]
            for i in range(5):
                if i < len(speakers):
                    cells.extend([speakers[i]["name"], speakers[i]["count"]])
                else:
                    cells.extend(["", ""])
            yield cells

    _write_csv(out / "groups.csv", groups_header, group_rows())
    _write_csv(
        out / "monthly.csv",
        ["month", "type", "count"],
        ([row["month"], row["type"], row["count"]] for row in summary["monthly"]),
    )

    (out / "report.md").write_text(
        render_markdown(summary, meta), encoding="utf-8"
    )

    files = [
        str((out / name).resolve())
        for name in (
            "report.json",
            "people.csv",
            "groups.csv",
            "monthly.csv",
            "messages.csv",
            "report.md",
        )
    ]
    elapsed = round(time.perf_counter() - started, 2)
    stats = {
        "messages": summary["overview"]["messages"],
        "chat_messages": summary["overview"]["chat_messages"],
        "conversations": summary["overview"]["conversations"],
        "people": len(summary["people"]),
        "groups": len(summary["groups"]),
        "message_dbs": meta["message_dbs"],
        "tables": profile["tables"],
        "db_messages_total": meta["db_messages_total"],
        "scanned_rows": scanned,
        "expected_rows": expected,
        "verified": scanned == expected,
        "self_detected": meta["self_detected"],
        "elapsed_seconds": elapsed,
    }
    return {"ok": scanned == expected, "year": year, "out_dir": str(out.resolve()), "files": files, "stats": stats}
