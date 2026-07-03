"""导入草稿的暂存与审核。

管线纪律（docs/施工计划.md 第 4 节第 3 条）：提取结果先进 memory_drafts，
人工接受/编辑/拒绝后才进 memories；拒绝的不落库（但草稿行保留作审计）。
只有 pending 状态可改可审；approved/rejected 可撤回（unreview）到 pending 重审。
"""

import sqlite3

from app import memories
from app.db import get_conn

# 审核时允许编辑的字段（status/memory_id/时间戳只能由审核动作改）
EDITABLE_FIELDS = (
    "date", "content", "tags", "tier", "topic", "space",
    "start_date", "end_date", "source_ref", "quote", "batch",
)

_DEFAULTS = {
    "date": "", "content": "", "tags": "", "tier": "normal", "topic": "",
    "space": "personal", "start_date": None, "end_date": None,
    "source_ref": None, "quote": None, "batch": "",
}

_INSERT_SQL = """INSERT INTO memory_drafts
    (date, content, tags, tier, topic, space, start_date, end_date, source_ref, quote, batch)
    VALUES (:date, :content, :tags, :tier, :topic, :space, :start_date, :end_date, :source_ref, :quote, :batch)"""


def _normalize_tags(tags: str) -> str:
    """中文逗号/顿号一律当分隔符——轩打中文标点不该被卡住。"""
    for sep in ("，", "、"):
        tags = tags.replace(sep, ",")
    return ",".join(t.strip() for t in tags.split(",") if t.strip())


def _validate(date: str, content: str, tier: str) -> None:
    if not date or not content:
        raise ValueError("date 和 content 必填")
    if tier not in ("anchor", "normal", "process"):
        raise ValueError(f"tier 必须是 anchor/normal/process，收到: {tier}")


def _prepare(item: dict) -> dict:
    row = {**_DEFAULTS, **{k: v for k, v in item.items() if k in EDITABLE_FIELDS}}
    row["tags"] = _normalize_tags(str(row["tags"] or ""))
    _validate(str(row["date"] or ""), str(row["content"] or ""), row["tier"])
    return row


def save_draft(
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
    batch: str = "",
) -> dict:
    row = _prepare({
        "date": date, "content": content, "tags": tags, "tier": tier, "topic": topic,
        "space": space, "start_date": start_date, "end_date": end_date,
        "source_ref": source_ref, "quote": quote, "batch": batch,
    })
    with get_conn() as conn:
        cur = conn.execute(_INSERT_SQL, row)
    return {"id": cur.lastrowid, "saved": True}


def save_drafts(items: list[dict]) -> list[int]:
    """批量存草稿，整批原子：先全量校验，任何一条不合法整批不写——
    否则提取脚本收到 400 后整批重试，会把前半批重复写一遍。"""
    rows = []
    for i, item in enumerate(items, start=1):
        try:
            if not isinstance(item, dict):
                raise ValueError("每条草稿要是 JSON 对象")
            rows.append(_prepare(item))
        except ValueError as e:
            raise ValueError(f"第 {i} 条有问题：{e}")
    with get_conn() as conn:  # 单事务：全成或全不写
        return [conn.execute(_INSERT_SQL, row).lastrowid for row in rows]


def list_drafts(status: str = "pending", batch: str | None = None, limit: int = 500) -> dict:
    """返回统计 + 草稿全文（审核要过目原文，不截断——与 memories 的目录纪律相反）。"""
    conds, params = ["status = ?"], [status]
    if batch:
        conds.append("batch = ?")
        params.append(batch)
    where = "WHERE " + " AND ".join(conds)
    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM memory_drafts {where}", params).fetchone()[0]
        by_batch = {
            r["batch"]: r["n"]
            for r in conn.execute(
                "SELECT batch, COUNT(*) n FROM memory_drafts WHERE status = ? GROUP BY batch",
                (status,),
            ).fetchall()
        }
        rows = conn.execute(
            f"SELECT * FROM memory_drafts {where} ORDER BY id LIMIT ?", params + [limit]
        ).fetchall()
    return {
        "stats": {"total": total, "by_batch": by_batch},
        "items": [dict(r) for r in rows],
    }


def get_draft(draft_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM memory_drafts WHERE id = ?", (draft_id,)).fetchone()
    return dict(row) if row else None


def update_draft(draft_id: int, edits: dict) -> dict | None:
    """改一条 pending 草稿的可编辑字段，返回改后的行；非 pending 或不存在返回 None。"""
    fields = {k: v for k, v in edits.items() if k in EDITABLE_FIELDS}
    if not fields:
        return get_draft(draft_id)
    if "tags" in fields:
        fields["tags"] = _normalize_tags(str(fields["tags"] or ""))
    draft = get_draft(draft_id)
    if draft is None or draft["status"] != "pending":
        return None
    merged = {**draft, **fields}
    _validate(merged["date"], merged["content"], merged["tier"])
    sets = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE memory_drafts SET {sets} WHERE id = ? AND status = 'pending'",
            [*fields.values(), draft_id],
        )
    return get_draft(draft_id)


def approve_draft(draft_id: int, edits: dict | None = None) -> dict | None:
    """通过：（可带最后修改）写入 memories + memory_sources，草稿标记 approved。

    服务端幂等（不靠前端禁按钮）："拿写锁 → 重读 pending → 写记忆 → 标记 approved"
    全在同一个写事务里。并发/重复 approve 时后来者重读拿不到 pending 行，返回 None。
    """
    fields = {}
    if edits:
        fields = {k: v for k, v in edits.items() if k in EDITABLE_FIELDS}
        if "tags" in fields:
            fields["tags"] = _normalize_tags(str(fields["tags"] or ""))
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")  # 先拿写锁，锁内重读才作数
        row = conn.execute(
            "SELECT * FROM memory_drafts WHERE id = ? AND status = 'pending'", (draft_id,)
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        draft = {**dict(row), **fields}
        _validate(draft["date"], draft["content"], draft["tier"])
        saved = memories.save_memory(
            date=draft["date"],
            content=draft["content"],
            tags=draft["tags"],
            tier=draft["tier"],
            topic=draft["topic"],
            space=draft["space"],
            start_date=draft["start_date"],
            end_date=draft["end_date"],
            source_ref=draft["source_ref"],
            quote=draft["quote"],
            conn=conn,  # 借本事务写，不另开连接
        )
        sets = "".join(f"{k} = ?, " for k in fields)
        conn.execute(
            f"""UPDATE memory_drafts
               SET {sets}status = 'approved', memory_id = ?, reviewed_at = datetime('now','+8 hours')
               WHERE id = ?""",
            [*fields.values(), saved["id"], draft_id],
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"draft_id": draft_id, "memory_id": saved["id"], "status": "approved"}


def reject_draft(draft_id: int) -> dict | None:
    """删（拒绝）：不进 memories，草稿行保留 rejected 状态作审计。

    单条带状态守卫的 UPDATE，天然原子：跟 approve 赛跑输了就改不到行，返回 None。
    """
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE memory_drafts
               SET status = 'rejected', reviewed_at = datetime('now','+8 hours')
               WHERE id = ? AND status = 'pending'""",
            (draft_id,),
        )
    return {"draft_id": draft_id, "status": "rejected"} if cur.rowcount else None


def unreview_draft(draft_id: int) -> dict | None:
    """反悔（手抖保险）：把已审核的草稿撤回 pending 重审。

    approved 的同时删掉它生成的正式记忆（含来源和边）；
    若该记忆已被别的记忆 superseded_by 引用，拒绝撤回，先解引用。
    """
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")  # 与 approve 同款：锁内重读，防赛跑
        row = conn.execute(
            "SELECT * FROM memory_drafts WHERE id = ? AND status != 'pending'", (draft_id,)
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        # 先解绑草稿自己的 memory_id 外键，再删记忆（同一事务，失败一起回滚）
        conn.execute(
            """UPDATE memory_drafts
               SET status = 'pending', memory_id = NULL, reviewed_at = NULL
               WHERE id = ?""",
            (draft_id,),
        )
        if row["status"] == "approved" and row["memory_id"]:
            mid = row["memory_id"]
            conn.execute("DELETE FROM memory_sources WHERE memory_id = ?", (mid,))
            conn.execute("DELETE FROM memory_edges WHERE from_id = ? OR to_id = ?", (mid, mid))
            conn.execute("DELETE FROM memories WHERE id = ?", (mid,))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError("这条记忆已被其他记忆引用（superseded_by），先解开引用再撤回")
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"draft_id": draft_id, "status": "pending"}
