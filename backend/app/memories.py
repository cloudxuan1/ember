"""记忆的读写与检索。纪律：目录先行（短结果），真正 recall 才取全文。"""

from datetime import datetime, timedelta, timezone

from app import embeddings
from app.db import get_conn

SUMMARY_LEN = 120  # search/list 返回的目录条目里 content 的截断长度

RRF_K = 60      # RRF 融合常数（业界惯用值）：越大头部排名的权重差越平缓
VEC_TOP_K = 40  # 语义腿取多少候选进融合

RELATIONS = ("led_to", "same_as", "contradicts", "supersedes", "related")

_TZ_BEIJING = timezone(timedelta(hours=8))  # 与库里 created_at 的 +8 保持一致


def _today() -> str:
    return datetime.now(_TZ_BEIJING).date().isoformat()


def interval_status(start_date: str | None, end_date: str | None, today: str | None = None) -> str | None:
    """区间状态读时现算，不存死（V4）。点事件（无 start/end）返回 None。

    只有 start = 进行中的开放区间；只有 end = 截止型（到期前算 ongoing）。
    ISO 日期字符串可直接比大小。
    """
    if not start_date and not end_date:
        return None
    today = today or _today()
    if start_date and today < start_date:
        return "upcoming"
    if end_date and today > end_date:
        return "ended"
    return "ongoing"


def _short(row) -> dict:
    content = row["content"]
    item = {
        "id": row["id"],
        "date": row["date"],
        "content": content[:SUMMARY_LEN] + ("…" if len(content) > SUMMARY_LEN else ""),
        "tags": row["tags"],
        "tier": row["tier"],
        "topic": row["topic"],
        "space": row["space"],
    }
    status = interval_status(row["start_date"], row["end_date"])
    if status:  # 目录保持精瘦：点事件不带区间字段
        item["start_date"] = row["start_date"]
        item["end_date"] = row["end_date"]
        item["interval_status"] = status
    return item


def resolve_space(space: str | None) -> str | None:
    """检索空间语义（V6 空间隔离）：personal 是核心底色，不该被项目记忆稀释。

    不传 / 空串 = 只搜 personal 核心层；"all" = 明确要求跨全库；
    其他值 = 指定空间。返回 None 表示不加空间过滤。
    """
    if not space:
        return "personal"
    if space == "all":
        return None
    return space


def _validate_links(links) -> list[dict]:
    """links = [{"id": 目标记忆, "relation": led_to/…, "dir": "out"(本条→目标, 默认)/"in"}]。"""
    if not links:
        return []
    if not isinstance(links, list):
        raise ValueError("links 要是列表")
    out = []
    for i, link in enumerate(links, start=1):
        if not isinstance(link, dict):
            raise ValueError(f"links 第 {i} 条要是对象")
        relation = link.get("relation", "related")
        if relation not in RELATIONS:
            raise ValueError(f"links 第 {i} 条 relation 必须是 {'/'.join(RELATIONS)}，收到: {relation}")
        direction = link.get("dir", "out")
        if direction not in ("out", "in"):
            raise ValueError(f"links 第 {i} 条 dir 必须是 out/in，收到: {direction}")
        target = link.get("id")
        if not isinstance(target, int) or isinstance(target, bool) or target <= 0:
            raise ValueError(f"links 第 {i} 条要有目标记忆 id（正整数）")
        out.append({"id": target, "relation": relation, "dir": direction})
    return out


def add_edges(conn, memory_id: int, links: list[dict], created_by: str = "") -> int:
    """在调用方事务里写边（不提交），返回实际写入条数。

    软失败：目标不存在 / 连自己 / 重复边 → 跳过不报错，记忆本体照常保存。
    supersedes 边同步 superseded_by 列——列是权威，边是导航，单一事实来源。
    """
    written = 0
    for link in _validate_links(links):
        other = link["id"]
        if other == memory_id:
            continue
        if conn.execute("SELECT 1 FROM memories WHERE id = ?", (other,)).fetchone() is None:
            continue
        from_id, to_id = (memory_id, other) if link["dir"] == "out" else (other, memory_id)
        cur = conn.execute(
            """INSERT OR IGNORE INTO memory_edges (from_id, to_id, relation, created_by)
               VALUES (?, ?, ?, ?)""",
            (from_id, to_id, link["relation"], created_by),
        )
        if cur.rowcount:
            written += 1
            if link["relation"] == "supersedes":
                conn.execute("UPDATE memories SET superseded_by = ? WHERE id = ?", (from_id, to_id))
    return written


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
    links: list[dict] | None = None,
    links_by: str = "mcp",
    conn=None,
) -> dict:
    """conn 给定时借用调用方的连接和事务（不提交，commit 归调用方管）——
    审核台 approve 用它把写记忆并进自己的写事务。"""
    if tier not in ("anchor", "normal", "process"):
        raise ValueError(f"tier 必须是 anchor/normal/process，收到: {tier}")
    links = _validate_links(links)  # 先校验后落库，坏 links 不留半条记忆

    def _write(c) -> tuple[int, int, bool]:
        cur = c.execute(
            """INSERT INTO memories (date, content, tags, tier, topic, space, start_date, end_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (date, content, tags, tier, topic, space, start_date, end_date),
        )
        memory_id = cur.lastrowid
        if source_ref or quote:
            c.execute(
                "INSERT INTO memory_sources (memory_id, source_ref, quote) VALUES (?, ?, ?)",
                (memory_id, source_ref, quote),
            )
        # 语义指纹软失败：算不出来照常入库，缺席的等 rebuild_embeddings() 补
        embedded = embeddings.embed_memory(c, memory_id, content, topic=topic, tags=tags)
        return memory_id, add_edges(c, memory_id, links, created_by=links_by), embedded

    if conn is not None:
        memory_id, edges_written, embedded = _write(conn)
    else:
        with get_conn() as c:
            memory_id, edges_written, embedded = _write(c)
    result = {"id": memory_id, "saved": True}
    if embeddings.enabled():
        result["embedded"] = embedded
    if links:
        result["edges_written"] = edges_written
    return result


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
        # 边带对方记忆的一行摘要——一次 recall 看见因果脉络，不用再挨个查 id
        memory["edges"] = [
            {
                "from_id": e["from_id"],
                "to_id": e["to_id"],
                "relation": e["relation"],
                "direction": "out" if e["from_id"] == memory_id else "in",
                "other": {
                    "id": e["other_id"],
                    "date": e["other_date"],
                    "content": e["other_content"][:SUMMARY_LEN]
                    + ("…" if len(e["other_content"]) > SUMMARY_LEN else ""),
                    "topic": e["other_topic"],
                },
                "created_by": e["created_by"],
                "created_at": e["created_at"],
            }
            for e in conn.execute(
                """SELECT e.from_id, e.to_id, e.relation, e.created_by, e.created_at,
                          m.id AS other_id, m.date AS other_date,
                          m.content AS other_content, m.topic AS other_topic
                   FROM memory_edges e
                   JOIN memories m
                     ON m.id = CASE WHEN e.from_id = ? THEN e.to_id ELSE e.from_id END
                   WHERE e.from_id = ? OR e.to_id = ?""",
                (memory_id, memory_id, memory_id),
            ).fetchall()
        ]
    memory["interval_status"] = interval_status(memory["start_date"], memory["end_date"])
    return memory


def _keyword_rows(query: str, space: str | None) -> list:
    """关键词腿：LIKE 子串匹配 + 简单加权，中文子串直接好使（不上 FTS5，分词坑不值得踩）。"""
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

    return sorted(rows, key=lambda r: (score(r), r["date"]), reverse=True)


def search_memories(query: str, space: str | None = None, limit: int = 8) -> list[dict]:
    """hybrid 检索（V5）：关键词腿 + 语义腿，RRF 融合。

    语义腿不可用（没配 key / API 挂了 / 库里还没有当前模型的指纹）时
    自动退回纯关键词，结果形状不变。
    space 不传 = 只搜 personal（V6 隔离）；"all" = 跨全库。
    """
    space = resolve_space(space)
    kw_rows = _keyword_rows(query, space)
    vec_ids = embeddings.vector_search(query, space=space, k=VEC_TOP_K)
    if not vec_ids:
        return [_short(r) for r in kw_rows[:limit]]

    # RRF：score = Σ 1/(K + 该腿排名)，两边都排前面的浮到最上面
    scores: dict[int, float] = {}
    for rank, row in enumerate(kw_rows):
        scores[row["id"]] = scores.get(row["id"], 0.0) + 1.0 / (RRF_K + rank)
    for rank, mid in enumerate(vec_ids):
        scores[mid] = scores.get(mid, 0.0) + 1.0 / (RRF_K + rank)

    by_id = {row["id"]: row for row in kw_rows}
    missing = [mid for mid in vec_ids if mid not in by_id]
    if missing:
        marks = ",".join("?" * len(missing))
        with get_conn() as conn:
            for row in conn.execute(f"SELECT * FROM memories WHERE id IN ({marks})", missing):
                by_id[row["id"]] = row
    top = sorted(scores, key=lambda mid: (scores[mid], by_id[mid]["date"]), reverse=True)
    return [_short(by_id[mid]) for mid in top[:limit]]


def list_memories(
    space: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    space = resolve_space(space)
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
