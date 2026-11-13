"""FTS5 索引：构建、增量、查询。

依据 docs/重构方案-2026-11-13.md 第 8 节：
逐字分词解决中文子串匹配，msg_meta 与 msg_fts 同 rowid，水位按 (库, 表) 记录。
索引库只写 decrypted/_index/ 下，不改动任何源库。
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

import zstandard as zstd

from ..db.locator import LocalSource
from ..db.open_ import connect_ro
from ..db.sender import build_local_sender_map, username_to_md5

_ZSTD = zstd.ZstdDecompressor()
_CJK = re.compile(r"([\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])")
_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS msg_fts USING fts5(text, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS msg_meta(
  rowid INTEGER PRIMARY KEY, db TEXT, tbl TEXT, local_id INTEGER,
  chat_id TEXT, sender_id TEXT, ts INTEGER, type INTEGER);
CREATE TABLE IF NOT EXISTS watermark(
  db TEXT, tbl TEXT, max_local_id INTEGER, PRIMARY KEY(db, tbl));
CREATE INDEX IF NOT EXISTS idx_meta_chat_ts ON msg_meta(chat_id, ts);
CREATE INDEX IF NOT EXISTS idx_meta_ts ON msg_meta(ts);
"""


def index_path(src: LocalSource) -> Path:
    return src.root / "_index" / "search.db"


def _tokenize(text: str) -> str:
    """中文逐字加空格，拉丁与数字保持成词，实现中文子串匹配。"""
    spaced = _CJK.sub(r" \1 ", text)
    return re.sub(r"\s+", " ", spaced).strip()


def _content(raw, flag) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    try:
        if flag == 4:
            return _ZSTD.decompress(raw).decode("utf-8", "replace")
        return raw.decode("utf-8", "replace")
    except Exception:
        return ""


_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
_DES_RE = re.compile(r"<des>(.*?)</des>", re.S)
_FILE_RE = re.compile(r"<title>(.*?)</title>", re.S)


def _distill(raw, flag) -> str:
    """把消息正文压成可检索文本：应用消息只取标题与描述，纯文本原样，长度截 500。"""
    text = _content(raw, flag).replace("\n", " ")
    if not text:
        return ""
    if "<msg>" in text or "<appmsg" in text:
        parts = _TITLE_RE.findall(text) + _DES_RE.findall(text)
        text = " ".join(parts) if parts else re.sub(r"<[^>]+>", " ", text)
    return text.strip()[:500]


def _open_index(src: LocalSource) -> sqlite3.Connection:
    path = index_path(src)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    return conn


def ensure(src: LocalSource) -> Path:
    conn = _open_index(src)
    conn.close()
    return index_path(src)


def _msg_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
        )
    ]


def _index_db(iconn: sqlite3.Connection, db_path: Path, batch: int = 2000) -> dict:
    src_conn = connect_ro(db_path)
    try:
        md5_to_user = {}
        for col in ("user_name", "userName"):
            try:
                for _rid, name in src_conn.execute(f"SELECT rowid, {col} FROM Name2Id"):
                    if name:
                        md5_to_user[username_to_md5(name)] = name
                break
            except sqlite3.OperationalError:
                continue
        sender_map = build_local_sender_map(src_conn)
        total = 0
        for table in _msg_tables(src_conn):
            md5 = table[4:] if table.startswith("Msg_") else ""
            chat_id = md5_to_user.get(md5, "")
            row = iconn.execute(
                "SELECT max_local_id FROM watermark WHERE db=? AND tbl=?",
                (db_path.name, table),
            ).fetchone()
            wm = int(row[0]) if row else -1
            try:
                cur = src_conn.execute(
                    'SELECT local_id, create_time, local_type, real_sender_id,'
                    ' message_content, WCDB_CT_message_content FROM "%s"'
                    " WHERE local_id > ? ORDER BY local_id" % table,
                    (wm,),
                )
            except sqlite3.OperationalError:
                continue
            batch_rows: list[tuple] = []
            max_lid = wm

            def flush() -> None:
                nonlocal total
                if not batch_rows:
                    return
                seq = int(iconn.execute("SELECT COALESCE(MAX(rowid),0) FROM msg_meta").fetchone()[0])
                meta_rows = []
                fts_rows = []
                for lid, ts, ltype, rsid, content, flag in batch_rows:
                    text = _distill(content, flag)
                    if not text:
                        continue
                    seq += 1
                    sender = ""
                    if rsid not in (None, ""):
                        try:
                            sender = sender_map.get(int(rsid), "")
                        except (TypeError, ValueError):
                            sender = ""
                    meta_rows.append(
                        (seq, db_path.name, table, lid, chat_id, sender, ts, ltype)
                    )
                    fts_rows.append((seq, _tokenize(text)))
                iconn.executemany(
                    "INSERT INTO msg_meta(rowid,db,tbl,local_id,chat_id,sender_id,ts,type)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    meta_rows,
                )
                iconn.executemany("INSERT INTO msg_fts(rowid,text) VALUES (?,?)", fts_rows)
                total += len(meta_rows)
                batch_rows.clear()

            for r in cur:
                batch_rows.append((r[0], r[1], r[2], r[3], r[4], r[5]))
                if r[0] is not None:
                    max_lid = max(max_lid, int(r[0]))
                if len(batch_rows) >= batch:
                    flush()
                    iconn.commit()
            flush()
            iconn.execute(
                "INSERT INTO watermark(db,tbl,max_local_id) VALUES (?,?,?)"
                " ON CONFLICT(db,tbl) DO UPDATE SET max_local_id=excluded.max_local_id",
                (db_path.name, table, max_lid),
            )
            iconn.commit()
        return {"db": db_path.name, "rows": total}
    finally:
        src_conn.close()


def build(src: LocalSource, rebuild: bool = False) -> dict:
    """全量或增量构建。rebuild=True 时删除索引文件重来。"""
    path = index_path(src)
    if rebuild and path.exists():
        path.unlink()
    t0 = time.time()
    conn = _open_index(src)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA cache_size=-131072")
        parts = []
        for db_path in src.message_dbs():
            parts.append(_index_db(conn, db_path))
        rows = conn.execute("SELECT COUNT(*) FROM msg_meta").fetchone()[0]
        return {
            "ok": True,
            "index": str(path),
            "rows": rows,
            "added": sum(p["rows"] for p in parts),
            "seconds": round(time.time() - t0, 2),
            "size_mb": round(path.stat().st_size / 1048576, 1) if path.exists() else 0,
        }
    finally:
        conn.close()


def update(src: LocalSource) -> dict:
    return build(src, rebuild=False)


def _match_expr(text: str) -> str:
    tokens = [t for t in re.split(r"\s+", text.strip()) if t]
    phrases = []
    for tok in tokens:
        spaced = _tokenize(tok)
        phrases.append('"%s"' % spaced.replace('"', ""))
    return " AND ".join(phrases)


def query(
    src: LocalSource,
    text: str,
    limit: int = 30,
    offset: int = 0,
    chat_ids: list[str] | None = None,
    since: int | None = None,
    until: int | None = None,
) -> list[dict]:
    path = index_path(src)
    if not path.exists():
        return []
    match = _match_expr(text)
    if not match:
        return []
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        where = ["msg_fts MATCH ?"]
        args: list = [match]
        if chat_ids:
            where.append("m.chat_id IN (%s)" % ",".join("?" * len(chat_ids)))
            args.extend(chat_ids)
        if since:
            where.append("m.ts >= ?")
            args.append(since)
        if until:
            where.append("m.ts <= ?")
            args.append(until)
        sql = (
            "SELECT m.db, m.tbl, m.local_id, m.chat_id, m.sender_id, m.ts, m.type,"
            " snippet(msg_fts, 0, '[', ']', '…', 12) AS snip"
            " FROM msg_fts JOIN msg_meta m ON m.rowid = msg_fts.rowid"
            f" WHERE {' AND '.join(where)} ORDER BY m.ts DESC LIMIT ? OFFSET ?"
        )
        args.extend([limit, offset])
        return [dict(r) for r in conn.execute(sql, args)]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def count(src: LocalSource) -> dict:
    path = index_path(src)
    if not path.exists():
        return {"rows": 0, "exists": False}
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM msg_meta").fetchone()[0]
        return {"rows": rows, "exists": True, "size_mb": round(path.stat().st_size / 1048576, 1)}
    finally:
        conn.close()
