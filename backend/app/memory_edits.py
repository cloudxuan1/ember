"""已入库记忆的修改提议：AI 经 MCP memory_edit 提交，轩在审核台「✎ 改动」确认才覆盖。

纪律（"AI 绕不过轩"）：
- 提议只存新值（NULL = 该字段不改），不存旧值——"改前"永远现读 memories；
  确认覆盖时源草稿一并同步成新值，库里不留旧版本。
- 一条记忆最多一张待确认提议，再改合并进同一张，version +1。
- 确认要带上轩看到的 version + seen 指纹，锁内重算对不上 = 冲突（409）：
  她确认的必须就是她看到的；驳回带 version，不会顺手丢掉她没看过的新改动。
- AI 只能改 content / tags / topic；tags / topic 传空当"不改"（清不掉 sensitive）。
"""

import difflib
import hashlib
import json

from app import drafts, memories
from app.db import get_conn

FIELDS = ("content", "tags", "topic")
REASON_MAX = 200
FRAGMENT_MAX = 2  # 夹在两处改动之间、不超过这么多字的"没变"并进改动，免得红绿碎成一地


class EditConflict(Exception):
    """提议或记忆在轩看过之后又变了：这次确认 / 驳回作废，让她刷新重看。"""


def _hash(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:16]


def _values(row, prefix: str = "") -> dict:
    return {f: row[prefix + f] or "" for f in FIELDS}


def _tag_list(tags: str | None) -> list[str]:
    """规整后的标签列表：老数据里有"日常, sensitive"这种带空格的，不规整就认不出 sensitive。"""
    return [t for t in memories.normalize_tags(tags or "").split(",") if t]


def _same(field: str, new: str, current: str) -> bool:
    """新值和现值算不算一样：按入口同一套规整比，免得只差首尾空白 / 中文逗号 / 标签顺序
    也生成一张卡上什么都看不出来的假改动。"""
    if field == "tags":
        return set(_tag_list(new)) == set(_tag_list(current))
    return new == (current or "").strip()


def target_preview(mem) -> dict:
    """改的是哪一条：日期 + 开头几十字，让 AI 自己核对没把草稿号当成记忆号。"""
    content = mem["content"] or ""
    preview = content[:drafts.PREVIEW_LEN] + ("…" if len(content) > drafts.PREVIEW_LEN else "")
    return {"date": mem["date"], "preview": preview}


def normalize_fields(content=None, tags=None, topic=None) -> dict:
    """AI 传来的新值 → {字段: 规整后的值}；没传或传空的 tags / topic 不出现（= 不改）。"""
    out = {}
    if content is not None:
        content = str(content).strip()
        if not content:
            raise ValueError("content 不能改成空")
        out["content"] = content
    if tags is not None and memories.normalize_tags(str(tags)):
        out["tags"] = memories.normalize_tags(str(tags))
    if topic is not None and str(topic).strip():
        out["topic"] = str(topic).strip()
    return out


def propose_edit(
    memory_id: int,
    content: str | None = None,
    tags: str | None = None,
    topic: str | None = None,
    reason: str | None = None,
    by: str = "mcp",
) -> dict | None:
    """提交（或合并进已有的）修改提议，不碰 memories。记忆不存在返回 None。

    读现值 → 合并 → 写提议全在一把写锁里：并发提交不会撞 UNIQUE，也不会写丢。
    合并后跟现值一样的字段丢掉（含"改回原样"）；一个都不剩就撤掉提议，回 unchanged。
    """
    new = normalize_fields(content, tags, topic)
    if not new:
        raise ValueError("content / tags / topic 至少给一个要改的（tags、topic 传空算不改）")
    reason = str(reason).strip()[:REASON_MAX] if reason else None
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        mem = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if mem is None:
            conn.rollback()
            return None
        old = conn.execute("SELECT * FROM memory_edits WHERE memory_id = ?", (memory_id,)).fetchone()
        before = {f: old[f] for f in FIELDS} if old else dict.fromkeys(FIELDS)
        pending = {**before, **new}
        for f in FIELDS:
            if pending[f] is not None and _same(f, pending[f], mem[f]):
                pending[f] = None
        replaced = {
            f: before[f] for f in FIELDS if before[f] is not None and before[f] != pending[f]
        }
        values = tuple(pending[f] for f in FIELDS)
        # 每个待改字段记一份"提议时现值"的哈希：这次 AI 交了的字段按现在算（它是看着现在改的），
        # 没交的沿用上次——轩在中间改过哪个字段，卡上就只对那个字段亮"已改过"
        current = _values(mem)
        old_base = json.loads(old["base"]) if old else {}
        base = json.dumps({
            f: _hash(current[f]) if (f in new or f not in old_base) else old_base[f]
            for f in FIELDS if pending[f] is not None
        })
        if all(v is None for v in values):
            if old:
                conn.execute("DELETE FROM memory_edits WHERE id = ?", (old["id"],))
            conn.commit()
            return {
                "memory_id": memory_id, "target": target_preview(mem), "status": "unchanged", "replaced": replaced,
            }
        if old is None:
            edit_id = conn.execute(
                """INSERT INTO memory_edits
                   (memory_id, content, tags, topic, reason, base, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (memory_id, *values, reason or "", base, by),
            ).lastrowid
        else:
            edit_id = old["id"]
            # 原样重交不加 version：不然轩开着的卡会白白 409
            if values != tuple(before.values()) or (reason is not None and reason != old["reason"]):
                conn.execute(
                    """UPDATE memory_edits
                       SET content = ?, tags = ?, topic = ?, reason = ?, base = ?, created_by = ?,
                           version = version + 1, updated_at = datetime('now','+8 hours')
                       WHERE id = ?""",
                    (*values, reason if reason is not None else old["reason"], base, by, edit_id),
                )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "edit_id": edit_id,
        "memory_id": memory_id,
        "target": target_preview(mem),
        "status": "pending_review",
        "merged": old is not None,
        "pending": {f: pending[f] for f in FIELDS if pending[f] is not None},
        "replaced": replaced,
    }


def pending_for(memory_id: int) -> dict | None:
    """一条记忆挂着的待确认提议（只含要改的字段），给 MCP 读：AI 改之前先看，别把上一版冲掉。"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM memory_edits WHERE memory_id = ?", (memory_id,)).fetchone()
    if row is None:
        return None
    out = {f: row[f] for f in FIELDS if row[f] is not None}
    out["reason"] = row["reason"] or ""
    return out


# ---------- 审核台：对照、确认、驳回 ----------


def diff_segments(before: str, after: str) -> list[dict]:
    """按字对比（中文没有空格，按字最准），返回 [{op: equal/delete/insert/replace, a, b}]。

    autojunk 关掉：默认开着时 200 字以上的文本里"的/了/，"这类高频字被当垃圾跳过，
    改两个字可能整句标红。夹在两处改动之间的 1–2 字"没变"并进改动，免得碎块。
    """
    ops = difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes()
    segs = [{"op": tag, "a": before[i1:i2], "b": after[j1:j2]} for tag, i1, i2, j1, j2 in ops]
    merged: list[dict] = []
    for i, seg in enumerate(segs):
        island = seg["op"] == "equal" and len(seg["a"]) <= FRAGMENT_MAX and 0 < i < len(segs) - 1
        if merged and merged[-1]["op"] != "equal" and (seg["op"] != "equal" or island):
            merged[-1]["a"] += seg["a"]
            merged[-1]["b"] += seg["b"]
            merged[-1]["op"] = "replace"
        else:
            merged.append(dict(seg))
    for seg in merged:
        if seg["op"] != "equal":
            seg["op"] = "insert" if not seg["a"] else "delete" if not seg["b"] else "replace"
    return merged


def tags_diff(before: str, after: str) -> dict:
    """标签按集合比：哪个留着、哪个新加、哪个去掉——逐字对比一串逗号看不出少了谁。"""
    a, b = _tag_list(before), _tag_list(after)
    return {
        "before": before or "",
        "after": after or "",
        "kept": [t for t in b if t in a],
        "added": [t for t in b if t not in a],
        "removed": [t for t in a if t not in b],
    }


_LIST_SQL = """SELECT e.*, m.date, m.space, m.tier,
                      m.content AS cur_content, m.tags AS cur_tags, m.topic AS cur_topic
               FROM memory_edits e JOIN memories m ON m.id = e.memory_id"""


def _seen(row) -> str:
    """轩看到的那一版的指纹：提议号 + 版本 + 改前 + 改后，任何一样变了都对不上。"""
    return _hash(row["id"], row["version"], _values(row, "cur_"), {f: row[f] for f in FIELDS})


def _stale(row, current: dict) -> bool:
    """这条提议要改的字段里，有没有哪个在 AI 提完之后又被改过（多半是轩在记忆库亲手改的）。"""
    base = json.loads(row["base"] or "{}")
    return any(base.get(f) != _hash(current[f]) for f in FIELDS if row[f] is not None)


def _present(row) -> dict:
    current = _values(row, "cur_")
    changes = {}
    if row["content"] is not None:
        changes["content"] = {
            "before": current["content"],
            "after": row["content"],
            "segments": diff_segments(current["content"], row["content"]),
        }
    if row["tags"] is not None:
        changes["tags"] = tags_diff(current["tags"], row["tags"])
    if row["topic"] is not None:
        changes["topic"] = {"before": current["topic"], "after": row["topic"]}
    return {
        "id": row["id"],
        "memory_id": row["memory_id"],
        "version": row["version"],
        "seen": _seen(row),
        "date": row["date"],
        "space": row["space"],
        "tier": row["tier"],
        "tags": current["tags"],
        "topic": current["topic"],
        "reason": row["reason"] or "",
        "created_by": row["created_by"] or "",
        "updated_at": row["updated_at"],
        "stale": _stale(row, current),
        "changes": changes,
    }


def list_edits() -> dict:
    with get_conn() as conn:
        rows = conn.execute(_LIST_SQL + " ORDER BY e.updated_at, e.id").fetchall()
    return {"stats": {"total": len(rows)}, "items": [_present(r) for r in rows]}


def confirm_edit(edit_id: int, version: int, seen: str) -> dict | None:
    """确认覆盖：锁内重读 → 核对轩看到的就是现在的 → 写 memories（重算指纹）
    → 源草稿同步成新值 → 删提议，一个事务。提议不存在返回 None，对不上抛 EditConflict。"""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(_LIST_SQL + " WHERE e.id = ?", (edit_id,)).fetchone()
        if row is None:
            conn.rollback()
            return None
        if row["version"] != version or _seen(row) != seen:
            conn.rollback()
            raise EditConflict
        fields = {f: row[f] for f in FIELDS if row[f] is not None}
        memories.update_memory(row["memory_id"], fields, conn=conn)
        # 轩定：一份旧版不留——反悔区那张源草稿也改成新值，撤回重审回来的同样是新版
        sets = ", ".join(f"{f} = ?" for f in fields)
        conn.execute(
            f"UPDATE memory_drafts SET {sets} WHERE memory_id = ? AND status = 'approved'",
            [*fields.values(), row["memory_id"]],
        )
        conn.execute("DELETE FROM memory_edits WHERE id = ?", (edit_id,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return memories.get_memory(row["memory_id"])


def reject_edit(edit_id: int, version: int) -> dict | None:
    """驳回：删提议，记忆不动。version 对不上 = 她没看过最新那版，抛 EditConflict。"""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM memory_edits WHERE id = ? AND version = ?", (edit_id, version)
        )
        exists = cur.rowcount == 0 and conn.execute(
            "SELECT 1 FROM memory_edits WHERE id = ?", (edit_id,)
        ).fetchone()
    if exists:
        raise EditConflict
    return {"edit_id": edit_id, "status": "rejected"} if cur.rowcount else None
