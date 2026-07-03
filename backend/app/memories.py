"""记忆的读写与检索。纪律：目录先行（短结果），真正 recall 才取全文。"""

from app.db import get_conn

SUMMARY_LEN = 120  # search/list 返回的目录条目里 content 的截断长度


def _short(row) -> dict:
    content = row["content"]
    return {
        "id": row["id"],
        "date": row["date"],
        "content": content[:SUMMARY_LEN] + ("…" if len(content) > SUMMARY_LEN else ""),
        "tags": row["tags"],
        "tier": row["tier"],
        "topic": row["topic"],
        "space": row["space"],
    }


def save_memory(
    date: str,
    content: str,
    tags: str = "",
    tier: str = "normal",
    topic: str = "",
    space: str = "personal",
    start_date: str | None = None,
    end_date: str | None = None,
    source_ref: str | None = None,
    quote: str | None = None,
) -> dict:
    if tier not in ("anchor", "normal", "process"):
        raise ValueError(f"tier 必须是 anchor/normal/process，收到: {tier}")
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO memories (date, content, tags, tier, topic, space, start_date, end_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (date, content, tags, tier, topic, space, start_date, end_date),
        )
        memory_id = cur.lastrowid
        if source_ref or quote:
            conn.execute(
                "INSERT INTO memory_sources (memory_id, source_ref, quote) VALUES (?, ?, ?)",
                (memory_id, source_ref, quote),
            )
    return {"id": memory_id, "saved": True}


def get_memory(memory_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            return None
        memory = dict(row)
        memory["sources"] = [
            dict(s)
            for s in conn.execute(
                "SELECT source_ref, quote FROM memory_sources WHERE memory_id = ?", (memory_id,)
            ).fetchall()
        ]
        memory["edges"] = [
            dict(e)
            for e in conn.execute(
                """SELECT from_id, to_id, relation FROM memory_edges
                   WHERE from_id = ? OR to_id = ?""",
                (memory_id, memory_id),
            ).fetchall()
        ]
    return memory


def search_memories(query: str, space: str | None = None, limit: int = 8) -> list[dict]:
    tokens = query.split() or [query]
    where = " OR ".join(["(content LIKE ? OR tags LIKE ? OR topic LIKE ?)"] * len(tokens))
    params: list = []
    for t in tokens:
        like = f"%{t}%"
        params += [like, like, like]
    sql = f"SELECT * FROM memories WHERE ({where})"
    if space:
        sql += " AND space = ?"
        params.append(space)
    sql += " ORDER BY date DESC LIMIT 200"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    def score(row) -> int:
        s = 0
        for t in tokens:
            if t in row["content"]:
                s += 2
            if t in row["tags"] or t in row["topic"]:
                s += 3
        if row["tier"] == "anchor":
            s += 1
        return s

    rows = sorted(rows, key=lambda r: (score(r), r["date"]), reverse=True)
    return [_short(r) for r in rows[:limit]]


def list_memories(
    space: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    conds, params = [], []
    if space:
        conds.append("space = ?")
        params.append(space)
    if date_from:
        conds.append("date >= ?")
        params.append(date_from)
    if date_to:
        conds.append("date <= ?")
        params.append(date_to)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    page = max(page, 1)
    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM memories {where}", params).fetchone()[0]
        by_space = {
            r["space"]: r["n"]
            for r in conn.execute(
                f"SELECT space, COUNT(*) n FROM memories {where} GROUP BY space", params
            ).fetchall()
        }
        by_tier = {
            r["tier"]: r["n"]
            for r in conn.execute(
                f"SELECT tier, COUNT(*) n FROM memories {where} GROUP BY tier", params
            ).fetchall()
        }
        rows = conn.execute(
            f"SELECT * FROM memories {where} ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
            params + [page_size, (page - 1) * page_size],
        ).fetchall()
    return {
        "stats": {"total": total, "by_space": by_space, "by_tier": by_tier},
        "page": page,
        "page_size": page_size,
        "total_pages": max((total + page_size - 1) // page_size, 1),
        "items": [_short(r) for r in rows],
    }


def get_status() -> dict:
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        last = conn.execute("SELECT MAX(created_at) FROM memories").fetchone()[0]
        edges = conn.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0]
        sources = conn.execute("SELECT COUNT(*) FROM memory_sources").fetchone()[0]
    return {
        "service": "ember",
        "status": "ok",
        "memories": total,
        "edges": edges,
        "sources": sources,
        "last_write": last,
    }
