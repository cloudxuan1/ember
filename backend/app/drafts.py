"""导入草稿的暂存与审核。

管线纪律（docs/施工计划.md 第 4 节第 3 条）：提取结果先进 memory_drafts，
人工接受/编辑/拒绝后才进 memories；拒绝的不落库（但草稿行保留作审计）。
只有 pending 状态可改可审；approved/rejected 可撤回（unreview）到 pending 重审。
"""

import json
import sqlite3

from app import memories
from app.db import get_conn

# 审核时允许编辑的字段（status/memory_id/时间戳只能由审核动作改）
EDITABLE_FIELDS = (
    "date", "content", "tags", "tier", "topic", "space",
    "start_date", "end_date", "source_ref", "quote", "batch", "links",
)

_DEFAULTS = {
    "date": "", "content": "", "tags": "", "tier": "normal", "topic": "",
    "space": "personal", "start_date": None, "end_date": None,
    "source_ref": None, "quote": None, "batch": "", "links": "",
}

_INSERT_SQL = """INSERT INTO memory_drafts
    (date, content, tags, tier, topic, space, start_date, end_date, source_ref, quote, batch, links)
    VALUES (:date, :content, :tags, :tier, :topic, :space, :start_date, :end_date, :source_ref, :quote, :batch, :links)"""


def _normalize_links(links) -> str:
    """边建议（V4）：列表或 JSON 字符串，规整后存 TEXT。

    每条 = {"memory_id": 已入库记忆} 或 {"draft_id": 同批草稿}（二选一），
    加 "relation"（led_to/… 默认 related）和 "dir"（out=本条→目标 默认 / in）。
    relation 另收 "none"（不关联）——审核台下拉的一员：连线躺平但不删，
    随时可换回（轩的后悔药），approve 时跳过不写边。草稿专属，正式边没有这个值。
    这里只校验结构；目标是否存在等 approve 时再查——那时才知道对方审没审过。
    """
    if links in (None, "", []):
        return ""
    if isinstance(links, str):
        try:
            links = json.loads(links)
        except json.JSONDecodeError:
            raise ValueError("links 不是合法 JSON")
    if not isinstance(links, list):
        raise ValueError("links 要是列表")
    normalized = []
    for i, link in enumerate(links, start=1):
        if not isinstance(link, dict):
            raise ValueError(f"links 第 {i} 条要是对象")
        relation = link.get("relation", "related")
        if relation not in (*memories.RELATIONS, "none"):
            raise ValueError(
                f"links 第 {i} 条 relation 必须是 {'/'.join(memories.RELATIONS)}/none，收到: {relation}"
            )
        direction = link.get("dir", "out")
        if direction not in ("out", "in"):
            raise ValueError(f"links 第 {i} 条 dir 必须是 out/in，收到: {direction}")
        memory_id, draft_id = link.get("memory_id"), link.get("draft_id")
        if (memory_id is None) == (draft_id is None):
            raise ValueError(f"links 第 {i} 条要有且只有 memory_id / draft_id 之一")
        target = memory_id if memory_id is not None else draft_id
        if not isinstance(target, int) or isinstance(target, bool) or target <= 0:
            raise ValueError(f"links 第 {i} 条的目标 id 要是正整数")
        key = "memory_id" if memory_id is not None else "draft_id"
        normalized.append({key: target, "relation": relation, "dir": direction})
    return json.dumps(normalized, ensure_ascii=False)


PREVIEW_LEN = 40  # 连线目标的内容摘要长度——轩审的是内容不是编号


def _present_draft(row_dict: dict, conn) -> dict:
    """给 API 返回用：links TEXT → 列表，并把目标编号翻译成人能审的内容摘要
    （target 是展示字段，PATCH 回来时 _normalize_links 会剥掉）；
    区间状态服务端现算下发——审核台不在 JS 里重复实现"今天"的时区逻辑。"""
    links = json.loads(row_dict["links"]) if row_dict.get("links") else []
    for link in links:
        if link.get("memory_id") is not None:
            row = conn.execute(
                "SELECT date, content FROM memories WHERE id = ?", (link["memory_id"],)
            ).fetchone()
            link["target"] = (
                {
                    "kind": "memory",
                    "id": link["memory_id"],
                    "date": row["date"],
                    "preview": row["content"][:PREVIEW_LEN]
                    + ("…" if len(row["content"]) > PREVIEW_LEN else ""),
                }
                if row
                else {"kind": "memory", "id": link["memory_id"], "missing": True}
            )
        else:
            row = conn.execute(
                "SELECT date, content, status FROM memory_drafts WHERE id = ?",
                (link["draft_id"],),
            ).fetchone()
            link["target"] = (
                {
                    "kind": "draft",
                    "id": link["draft_id"],
                    "date": row["date"],
                    "preview": row["content"][:PREVIEW_LEN]
                    + ("…" if len(row["content"]) > PREVIEW_LEN else ""),
                    "status": row["status"],
                }
                if row
                else {"kind": "draft", "id": link["draft_id"], "missing": True}
            )
    row_dict["links"] = links
    row_dict["interval_status"] = memories.interval_status(
        row_dict.get("start_date"), row_dict.get("end_date")
    )
    return row_dict


def _blank_interval_to_null(fields: dict) -> dict:
    """编辑器清空日期发来空字符串 → 规整成 NULL，不让 '' 脏值进库。"""
    for key in ("start_date", "end_date"):
        if key in fields and fields[key] == "":
            fields[key] = None
    return fields


def _validate(date: str, content: str, tier: str) -> None:
    if not date or not content:
        raise ValueError("date 和 content 必填")
    if tier not in ("anchor", "normal", "process"):
        raise ValueError(f"tier 必须是 anchor/normal/process，收到: {tier}")


def _prepare(item: dict) -> dict:
    row = {**_DEFAULTS, **{k: v for k, v in item.items() if k in EDITABLE_FIELDS}}
    row["tags"] = memories.normalize_tags(str(row["tags"] or ""))
    row["links"] = _normalize_links(row["links"])
    _blank_interval_to_null(row)
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
    links: list[dict] | str = "",
) -> dict:
    row = _prepare({
        "date": date, "content": content, "tags": tags, "tier": tier, "topic": topic,
        "space": space, "start_date": start_date, "end_date": end_date,
        "source_ref": source_ref, "quote": quote, "batch": batch, "links": links,
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
        items = [_present_draft(dict(r), conn) for r in rows]
    return {
        "stats": {"total": total, "by_batch": by_batch},
        "items": items,
    }


def get_draft(draft_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM memory_drafts WHERE id = ?", (draft_id,)).fetchone()
        return _present_draft(dict(row), conn) if row else None


def update_draft(draft_id: int, edits: dict) -> dict | None:
    """改一条 pending 草稿的可编辑字段，返回改后的行；非 pending 或不存在返回 None。"""
    fields = {k: v for k, v in edits.items() if k in EDITABLE_FIELDS}
    if not fields:
        return get_draft(draft_id)
    if "tags" in fields:
        fields["tags"] = memories.normalize_tags(str(fields["tags"] or ""))
    if "links" in fields:
        fields["links"] = _normalize_links(fields["links"])
    _blank_interval_to_null(fields)
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


def _write_draft_edges(conn, memory_id: int, links_text: str) -> None:
    """把草稿的边建议解析成真实边（approve 事务内）。

    memory_id 目标直接写；draft_id 目标只有对方已 approved 才写——
    还 pending 的先跳过，等对方 approve 时由 _backfill_edges 补上；被拒的自然丢弃。
    """
    if not links_text:
        return
    resolved = []
    for link in json.loads(links_text):
        if link["relation"] == "none":
            continue  # 轩设了"不关联"：草稿单独入库，这条线不写
        if link.get("memory_id") is not None:
            target = link["memory_id"]
        else:
            row = conn.execute(
                "SELECT memory_id FROM memory_drafts WHERE id = ? AND status = 'approved'",
                (link["draft_id"],),
            ).fetchone()
            if row is None or row["memory_id"] is None:
                continue
            target = row["memory_id"]
        resolved.append({"id": target, "relation": link["relation"], "dir": link["dir"]})
    memories.add_edges(conn, memory_id, resolved, created_by="extraction")


def _backfill_edges(conn, draft_id: int, memory_id: int) -> None:
    """反向补边：别的已 approved 草稿若建议过连到本草稿，现在本草稿有记忆 id 了，把它们的边补上。
    配合 UNIQUE + INSERT OR IGNORE，任意审批顺序（含撤回重审）最终边都齐且不重复。"""
    rows = conn.execute(
        """SELECT memory_id, links FROM memory_drafts
           WHERE status = 'approved' AND links != '' AND id != ?""",
        (draft_id,),
    ).fetchall()
    for row in rows:
        if row["memory_id"] is None:
            continue
        wanted = [
            {"id": memory_id, "relation": link["relation"], "dir": link["dir"]}
            for link in json.loads(row["links"])
            if link.get("draft_id") == draft_id and link["relation"] != "none"
        ]
        if wanted:
            memories.add_edges(conn, row["memory_id"], wanted, created_by="extraction")


def approve_draft(draft_id: int, edits: dict | None = None) -> dict | None:
    """通过：（可带最后修改）写入 memories + memory_sources + 边建议，草稿标记 approved。

    服务端幂等（不靠前端禁按钮）："拿写锁 → 重读 pending → 写记忆 → 标记 approved"
    全在同一个写事务里。并发/重复 approve 时后来者重读拿不到 pending 行，返回 None。
    """
    fields = {}
    if edits:
        fields = {k: v for k, v in edits.items() if k in EDITABLE_FIELDS}
        if "tags" in fields:
            fields["tags"] = memories.normalize_tags(str(fields["tags"] or ""))
        if "links" in fields:
            fields["links"] = _normalize_links(fields["links"])
        _blank_interval_to_null(fields)
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
        _write_draft_edges(conn, saved["id"], draft["links"])
        sets = "".join(f"{k} = ?, " for k in fields)
        conn.execute(
            f"""UPDATE memory_drafts
               SET {sets}status = 'approved', memory_id = ?, reviewed_at = datetime('now','+8 hours')
               WHERE id = ?""",
            [*fields.values(), saved["id"], draft_id],
        )
        _backfill_edges(conn, draft_id, saved["id"])  # 状态已标 approved，先标后补
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
    该记忆经 supersedes 边压过的旧记忆，superseded_by 一并松开（边没了，压制也该解除）。
    重新 approve 时边建议还在草稿里，会随记忆重新生成。
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
            conn.execute("UPDATE memories SET superseded_by = NULL WHERE superseded_by = ?", (mid,))
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
