"""最近动静：一份流水，几点、谁（xuan 轩 / ke 小克）、存了 / 改了 / 回退了 / 删了 / 恢复了哪条，给网页看。

写流水在调用方（memories）的写事务里做，跟记忆本身一起提交或一起回滚。
只显示最近 activity_days 天（设置项），更早的行由 memories.purge_expired() 清掉。
"""

from app import settings
from app.db import get_conn

ACTORS = ("xuan", "ke")
ACTIONS = ("save", "edit", "revert", "delete", "restore")
TITLE_LEN = 30


def title_of(content: str) -> str:
    text = " ".join((content or "").split())
    return text[:TITLE_LEN] + ("…" if len(text) > TITLE_LEN else "")


def log(conn, actor: str, action: str, memory_id: int, content: str, history_id: int | None = None) -> int:
    if actor not in ACTORS:
        raise ValueError(f"actor 必须是 {'/'.join(ACTORS)}")
    cur = conn.execute(
        "INSERT INTO activity_log (actor, action, memory_id, title, history_id) VALUES (?, ?, ?, ?, ?)",
        (actor, action, memory_id, title_of(content), history_id),
    )
    return cur.lastrowid


def mark_undone(conn, memory_id: int, *, history_id: int | None = None, action: str | None = None) -> None:
    """撤销对应的那一行：回退消耗了某一版 → 换下它的那次改动算已回退；恢复 → 最近那次删除算已恢复。"""
    if history_id is not None:
        conn.execute(
            "UPDATE activity_log SET undone = 1 WHERE memory_id = ? AND history_id = ?",
            (memory_id, history_id),
        )
    if action == "delete":
        conn.execute(
            """UPDATE activity_log SET undone = 1 WHERE id = (
                 SELECT id FROM activity_log WHERE memory_id = ? AND action = 'delete' AND undone = 0
                 ORDER BY id DESC LIMIT 1)""",
            (memory_id,),
        )


def _status(row, mem, history_ids: set[int], later_change: bool) -> tuple[str, str]:
    """→ (能做的事 revert / restore / '', 给人看的状态)。"""
    if row["action"] in ("edit", "revert"):
        if row["undone"]:
            return "", "已回退"
        if mem is None:
            return "", "已彻底删除"
        if mem["deleted_at"]:
            return "", "已删除"
        if row["history_id"] not in history_ids:
            return "", "上一版已清掉"
        if later_change:
            return "", "已被覆盖"
        return "revert", ""
    if row["action"] == "delete":
        if row["undone"]:
            return "", "已恢复"
        if mem is None:
            return "", "已彻底删除"
        if mem["deleted_at"]:
            return "restore", ""
        return "", "已恢复"
    if mem is None:
        return "", "已彻底删除"
    return "", "已删除" if mem["deleted_at"] else ""


def list_activity() -> dict:
    """最近 activity_days 天的流水，新的在前；每行带现在还能不能回退 / 恢复。"""
    with get_conn() as conn:
        days = settings.get_settings(conn)["activity_days"]
        rows = conn.execute(
            """SELECT * FROM activity_log WHERE at >= datetime('now', '+8 hours', ?)
               ORDER BY id DESC""",
            (f"-{days} days",),
        ).fetchall()
        ids = sorted({r["memory_id"] for r in rows})
        mems, history_ids = {}, set()
        if ids:
            marks = ",".join("?" * len(ids))
            mems = {
                r["id"]: r
                for r in conn.execute(
                    f"SELECT id, content, deleted_at FROM memories WHERE id IN ({marks})", ids
                ).fetchall()
            }
            history_ids = {
                r["id"] for r in conn.execute(
                    f"SELECT id FROM memory_history WHERE memory_id IN ({marks})", ids
                ).fetchall()
            }
    newest_change: dict[int, int] = {}  # 每条记忆最近一次改内容（改 / 回退）的流水号
    for r in rows:
        if r["action"] in ("edit", "revert"):
            newest_change.setdefault(r["memory_id"], r["id"])
    items = []
    for r in rows:
        mem = mems.get(r["memory_id"])
        can, status = _status(r, mem, history_ids, newest_change.get(r["memory_id"], r["id"]) != r["id"])
        items.append({
            "id": r["id"],
            "at": r["at"],
            "actor": r["actor"],
            "action": r["action"],
            "memory_id": r["memory_id"],
            "title": title_of(mem["content"]) if mem is not None else r["title"],
            "history_id": r["history_id"],
            "can": can,
            "status": status,
        })
    return {"days": days, "items": items}
