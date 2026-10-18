"""审核台待办的只读视图（给 MCP）：还没审的草稿 + 还没确认的修改提议，
以及 memory_search 顺带搜待审草稿。只读，不碰任何状态。"""

from app import memories, memory_edits
from app.db import get_conn

PAGE_SIZE = 20
PREVIEW_LEN = 80  # 队列里草稿内容的截断长度


def _clip(text: str, n: int) -> str:
    text = text or ""
    return text[:n] + ("…" if len(text) > n else "")


def list_queue(page: int = 1) -> dict:
    """两种待办合成一条队列，按进队时间新的在前（草稿看 created_at，提议看最后一次合并的 updated_at）。"""
    page = max(page, 1)
    with get_conn() as conn:
        n_drafts = conn.execute(
            "SELECT COUNT(*) FROM memory_drafts WHERE status = 'pending'"
        ).fetchone()[0]
        n_edits = conn.execute("SELECT COUNT(*) FROM memory_edits").fetchone()[0]
        refs = conn.execute(
            """SELECT 'draft' AS kind, id, created_at AS ts FROM memory_drafts WHERE status = 'pending'
               UNION ALL
               SELECT 'edit', id, updated_at FROM memory_edits
               ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?""",
            (PAGE_SIZE, (page - 1) * PAGE_SIZE),
        ).fetchall()
        items = []
        for ref in refs:
            if ref["kind"] == "draft":
                d = conn.execute("SELECT * FROM memory_drafts WHERE id = ?", (ref["id"],)).fetchone()
                items.append({
                    "kind": "draft",
                    "draft_id": d["id"],
                    "date": d["date"],
                    "topic": d["topic"],
                    "tags": d["tags"],
                    "content": _clip(d["content"], PREVIEW_LEN),
                })
            else:
                e = conn.execute(
                    """SELECT e.*, m.date AS m_date, m.content AS m_content
                       FROM memory_edits e JOIN memories m ON m.id = e.memory_id WHERE e.id = ?""",
                    (ref["id"],),
                ).fetchone()
                items.append({
                    "kind": "edit",
                    "memory_id": e["memory_id"],
                    "target": memory_edits.target_preview({"date": e["m_date"], "content": e["m_content"]}),
                    "changed": [f for f in memory_edits.FIELDS if e[f] is not None],
                })
    total = n_drafts + n_edits
    return {
        "stats": {"total": total, "drafts": n_drafts, "edits": n_edits},
        "page": page,
        "page_size": PAGE_SIZE,
        "total_pages": max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
        "items": items,
    }


def search_drafts(query: str, space: str | None = None, limit: int = 8) -> list[dict]:
    """待审草稿的关键词检索（草稿没有语义指纹，只走关键词腿）；space 语义同 memory_search。"""
    rows = memories._keyword_rows(query, memories.resolve_space(space), table="memory_drafts")
    return [
        {
            "draft_id": r["id"],
            "status": "待审",
            "date": r["date"],
            "content": _clip(r["content"], memories.SUMMARY_LEN),
            "tags": r["tags"],
            "tier": r["tier"],
            "topic": r["topic"],
            "space": r["space"],
        }
        for r in rows[:limit]
    ]
