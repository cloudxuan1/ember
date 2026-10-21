"""主动浮现（V6b）：开场小抄的三级漏斗。

① 没吃到的饼（年轮卡 PR3 起，换掉按起止日期挑的「没完的事」）——贴了任意一张饼便利贴、又没贴「吃到了」
的卡，最新一圈的日子新的在前，条数看设置 briefing_pies；② 话题共振——topic 走
V5 hybrid 检索（语义腿挂了自动退关键词）；③ 近期——事件时间最近
（按 date 排不按 created_at 排：刚审完入库的四月旧事不算"最近"）。
合并去重、sensitive 剔除、superseded 剔除、已删（藏起来的）剔除，总上限 8 条。
冷却：①③ 层同一条记忆 3 天内不重复递（briefing_log）；② 层不冷却——
主动聊到相关的事，相关记忆永远该到场。
只看 personal 核心层——briefing 是关系连续性的开场，不是项目看板。
"""

import json

from app import memories, settings
from app.db import get_conn

TOTAL_CAP = 8
TOPIC_CAP = 4        # ② 话题共振（① 饼的条数在设置里）
RECENT_CAP = 3       # ③ 近期
COOLDOWN_DAYS = 3

REASON_PIE = "没吃到的饼"
EATEN = "吃到了"
REASON_TOPIC = "和眼下话题相关"
REASON_RECENT = "最近的事"


def _is_sensitive(tags: str) -> bool:
    return "sensitive" in {t.strip() for t in tags.split(",")}


def _recently_surfaced(conn) -> set[int]:
    rows = conn.execute(
        """SELECT DISTINCT memory_id FROM briefing_log
           WHERE surfaced_at >= datetime('now', '+8 hours', ?)""",
        (f"-{COOLDOWN_DAYS} days",),
    ).fetchall()
    return {r["memory_id"] for r in rows}


def _pies(conn) -> list:
    """① 没吃到的饼：便利贴整张比（memories.tag_filter_ids，口令「饼」= 四张饼任一；只贴了「面团」的不算饼），
    卡片日期（= 最新一圈的日子）新的在前。"""
    ids = memories.tag_filter_ids(conn, "饼", EATEN)
    return conn.execute(
        """SELECT * FROM memories
           WHERE space = 'personal' AND superseded_by IS NULL AND deleted_at IS NULL
             AND id IN (SELECT value FROM json_each(?))
           ORDER BY date DESC, id DESC""",
        (json.dumps(ids),),
    ).fetchall()


def _topic_rows(conn, topic: str) -> list:
    """② 话题共振：hybrid 检索拿 id，再回表取整行（要 superseded_by 等目录里没有的列）。"""
    hits = memories.search_memories(topic, space="personal", limit=TOPIC_CAP * 2)
    if not hits:
        return []
    ids = [h["id"] for h in hits]
    marks = ",".join("?" * len(ids))
    by_id = {
        r["id"]: r
        for r in conn.execute(
            f"SELECT * FROM memories WHERE id IN ({marks}) AND superseded_by IS NULL AND deleted_at IS NULL",
            ids,
        ).fetchall()
    }
    return [by_id[i] for i in ids if i in by_id]  # 保住检索的相关性排序


def _recent(conn) -> list:
    return conn.execute(
        """SELECT * FROM memories
           WHERE space = 'personal' AND superseded_by IS NULL AND deleted_at IS NULL
           ORDER BY date DESC, id DESC LIMIT ?""",
        (RECENT_CAP * 3,),  # 多取一些，冷却/sensitive/去重筛完还够用
    ).fetchall()


def build_briefing(topic: str | None = None) -> dict:
    picked: list[dict] = []
    seen: set[int] = set()
    to_log: list[int] = []  # ①③ 层实际递出去的才记冷却

    def take(rows, reason: str, cap: int, skip: set[int], cooled: bool) -> None:
        added = 0
        for row in rows:
            if added >= cap or len(picked) >= TOTAL_CAP:
                return
            if row["id"] in seen or row["id"] in skip or _is_sensitive(row["tags"]):
                continue
            item = memories._short(row)
            item["reason"] = reason
            picked.append(item)
            seen.add(row["id"])
            if cooled:
                to_log.append(row["id"])
            added += 1

    with get_conn() as conn:
        cooldown = _recently_surfaced(conn)
        take(_pies(conn), REASON_PIE, settings.get_settings(conn)["briefing_pies"], skip=cooldown, cooled=True)
        if topic:
            take(_topic_rows(conn, topic), REASON_TOPIC, TOPIC_CAP, skip=set(), cooled=False)
        take(_recent(conn), REASON_RECENT, RECENT_CAP, skip=cooldown, cooled=True)
        for mid in to_log:
            conn.execute("INSERT INTO briefing_log (memory_id) VALUES (?)", (mid,))

    return {"count": len(picked), "items": picked}
