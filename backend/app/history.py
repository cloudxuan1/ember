"""记忆的「上一版」：每改一次，改前的整条可改字段挤进 memory_history，只留最近 N 版（设置 history_keep）。

改、回退都在调用方（memories）的写事务里调这里的函数，不自己开事务；
看、清掉是网页直接调的，自开连接。
"""

import json

from app import settings
from app.db import get_conn

# 一版里存哪些字段 = 网页 / 小克能改的字段（编号、存的时间、删除状态不算内容，不进历史）
SNAPSHOT_FIELDS = ("date", "content", "tags", "tier", "topic", "space", "start_date", "end_date")


def snapshot(row) -> dict:
    return {f: row[f] for f in SNAPSHOT_FIELDS}


def push(conn, row, by: str, action: str = "edit") -> int:
    """把 row（改前的记忆整行）存成一版，按设置只留最近 N 版，返回这一版的编号。"""
    cur = conn.execute(
        "INSERT INTO memory_history (memory_id, snapshot, replaced_by, action) VALUES (?, ?, ?, ?)",
        (row["id"], json.dumps(snapshot(row), ensure_ascii=False), by, action),
    )
    trim(conn, row["id"], settings.get_settings(conn)["history_keep"])
    return cur.lastrowid


def trim(conn, memory_id: int, keep: int) -> None:
    conn.execute(
        """DELETE FROM memory_history WHERE memory_id = ? AND id NOT IN
           (SELECT id FROM memory_history WHERE memory_id = ? ORDER BY id DESC LIMIT ?)""",
        (memory_id, memory_id, keep),
    )


def trim_all(conn, keep: int) -> None:
    conn.execute(
        """DELETE FROM memory_history WHERE id IN (
             SELECT id FROM (
               SELECT id, ROW_NUMBER() OVER (PARTITION BY memory_id ORDER BY id DESC) AS rn
               FROM memory_history)
             WHERE rn > ?)""",
        (keep,),
    )


def _present(row) -> dict:
    return {
        "id": row["id"],
        "replaced_by": row["replaced_by"],
        "action": row["action"],
        "replaced_at": row["replaced_at"],
        **json.loads(row["snapshot"]),
    }


def list_for(memory_id: int, conn=None) -> list[dict]:
    """这条记忆留着的旧版，新的在前。"""
    sql = "SELECT * FROM memory_history WHERE memory_id = ? ORDER BY id DESC"
    if conn is not None:
        return [_present(r) for r in conn.execute(sql, (memory_id,)).fetchall()]
    with get_conn() as c:
        return [_present(r) for r in c.execute(sql, (memory_id,)).fetchall()]


def get(conn, memory_id: int, history_id: int):
    return conn.execute(
        "SELECT * FROM memory_history WHERE id = ? AND memory_id = ?", (history_id, memory_id)
    ).fetchone()


def clear(memory_id: int | None = None, history_id: int | None = None) -> int:
    """清掉旧版：给 history_id 清那一版；只给 memory_id 清这条的全部；都不给 = 设置里的「全部清空」。
    返回清掉了几版。清掉后就没法回退了，最近动静里对应的「回退」按钮也会消失。"""
    with get_conn() as conn:
        if history_id is not None:
            cur = conn.execute(
                "DELETE FROM memory_history WHERE id = ? AND memory_id = ?", (history_id, memory_id)
            )
        elif memory_id is not None:
            cur = conn.execute("DELETE FROM memory_history WHERE memory_id = ?", (memory_id,))
        else:
            cur = conn.execute("DELETE FROM memory_history")
        return cur.rowcount
