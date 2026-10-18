"""单库改造的一次性搬家（2026-10）：草稿并进正式记忆，审核台的两张表退场。

init_db() 每次启动都会调 migrate()，但只有库里还有 memory_drafts / memory_edits 时才真搬，
搬完两张表就删了，所以同一个库只搬一次。整个搬家一个事务：中途出错整库回到搬之前。

搬家规矩（轩 2026-10-18 定）：
- 已入库的记忆编号不动；待审草稿全部进库，编号接在当前最大号后面，按存的先后（created_at, id）排；
  存的时间沿用草稿的 created_at。
- SKIP_DRAFTS 是重复的草稿，不搬；被拒的、已通过的草稿行（旧快照）都不搬——已通过的早就是正式记忆。
- 草稿的原话 / 来源进 memory_sources；草稿的连线换成新编号写成正式边（目标草稿没搬的跳过）。
- 发生日期在 2026-08 之前的（已入库的和草稿都算）统一打「旧档」标签。
- 还没确认的修改提议不套用：原样写进报告给轩看，备份里也有。
- 新进库的记忆没有语义指纹，搬完跑一次 python -m app.embeddings 补上；补之前只能按关键词搜到。

试跑：在库的拷贝上跑 python -m app.migrate_single_store /path/to/copy.db，打印报告，不碰正式库。
"""

import json
import sys

from app import memories
from app.db import db_path

SKIP_DRAFTS = (130, 156, 158, 167, 173)
OLD_TAG = "旧档"
OLD_BEFORE = "2026-08"  # 字符串比较：'2026-07-31' / '2026-07' 都 < '2026-08'，'2026-08' 和 8 月的日子都不算
REPORT_FILE = "migration_single_store.json"


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _now(conn) -> str:
    return conn.execute("SELECT datetime('now','+8 hours')").fetchone()[0]


def needed(conn) -> bool:
    return bool(_tables(conn) & {"memory_drafts", "memory_edits"})


def _resolve_links(links_text: str, draft_to_memory: dict[int, int]) -> tuple[list[dict], list[dict]]:
    """草稿的连线 → add_edges 认的 [{id, relation, dir}]；对不上号的放进 skipped。"""
    if not links_text:
        return [], []
    resolved, skipped = [], []
    for link in json.loads(links_text):
        relation = link.get("relation", "related")
        if relation == "none":
            continue  # 轩在审核台设了「不关联」
        if link.get("memory_id") is not None:
            target = link["memory_id"]
        else:
            target = draft_to_memory.get(link.get("draft_id"))
        if target is None:
            skipped.append(link)
            continue
        resolved.append({"id": target, "relation": relation, "dir": link.get("dir", "out")})
    return resolved, skipped


def _move_drafts(conn, report: dict) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(memory_drafts)")}
    order = "created_at, id" if "created_at" in cols else "id"
    # 转成 dict 用 .get：很老的草稿表缺列（测试里的 V3 库）也能走完
    rows = [dict(r) for r in conn.execute(f"SELECT * FROM memory_drafts ORDER BY {order}").fetchall()]
    # 已通过的草稿行就是旧快照：只拿它们的 draft_id → memory_id 对照给连线用
    draft_to_memory = {r["id"]: r.get("memory_id") for r in rows if r["status"] == "approved" and r.get("memory_id")}
    pending = [r for r in rows if r["status"] == "pending" and r["id"] not in SKIP_DRAFTS]
    report["drafts"] = {
        "pending": sum(1 for r in rows if r["status"] == "pending"),
        "skipped_duplicates": sorted(r["id"] for r in rows if r["status"] == "pending" and r["id"] in SKIP_DRAFTS),
        "dropped_rejected": sum(1 for r in rows if r["status"] == "rejected"),
        "dropped_approved_snapshots": sum(1 for r in rows if r["status"] == "approved"),
    }
    next_id = (conn.execute("SELECT MAX(id) FROM memories").fetchone()[0] or 0) + 1
    moved = []
    for r in pending:
        new_id = next_id
        next_id += 1
        conn.execute(
            """INSERT INTO memories
               (id, date, content, tags, tier, topic, space, start_date, end_date, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (new_id, r["date"], r["content"], memories.normalize_tags(r.get("tags") or ""),
             r.get("tier") or "normal", r.get("topic") or "", r.get("space") or "personal",
             r.get("start_date"), r.get("end_date"), r.get("created_at") or _now(conn)),
        )
        if r.get("source_ref") or r.get("quote"):
            conn.execute(
                "INSERT INTO memory_sources (memory_id, source_ref, quote) VALUES (?, ?, ?)",
                (new_id, r.get("source_ref"), r.get("quote")),
            )
        draft_to_memory[r["id"]] = new_id
        moved.append((r, new_id))
    # 连线放第二遍：草稿之间互连时，目标草稿要先有新编号
    edges, skipped_links = 0, []
    for r, new_id in moved:
        links, skipped = _resolve_links(r.get("links") or "", draft_to_memory)
        edges += memories.add_edges(conn, new_id, links, created_by="extraction")
        skipped_links += [{"draft_id": r["id"], "link": link} for link in skipped]
    report["moved"] = [{"draft_id": r["id"], "memory_id": new_id} for r, new_id in moved]
    report["moved_count"] = len(moved)
    report["new_id_range"] = [moved[0][1], moved[-1][1]] if moved else None
    report["edges_written"] = edges
    report["links_skipped"] = skipped_links


def _pending_edits(conn, report: dict) -> None:
    rows = conn.execute(
        """SELECT e.*, m.date AS m_date, m.content AS m_content
           FROM memory_edits e JOIN memories m ON m.id = e.memory_id ORDER BY e.id"""
    ).fetchall()
    report["pending_edits_not_applied"] = [
        {
            "memory_id": r["memory_id"],
            "target": {"date": r["m_date"], "preview": (r["m_content"] or "")[:40]},
            "proposed": {f: r[f] for f in ("content", "tags", "topic") if r[f] is not None},
            "reason": r["reason"] or "",
            "created_by": r["created_by"] or "",
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def _tag_old(conn, report: dict) -> None:
    tagged = []
    for r in conn.execute("SELECT id, tags FROM memories WHERE date < ? ORDER BY id", (OLD_BEFORE,)).fetchall():
        tags = [t for t in memories.normalize_tags(r["tags"] or "").split(",") if t]
        if OLD_TAG in tags:
            continue
        conn.execute("UPDATE memories SET tags = ? WHERE id = ?", (",".join(tags + [OLD_TAG]), r["id"]))
        tagged.append(r["id"])
    report["tagged_old"] = tagged
    report["tagged_old_count"] = len(tagged)


def migrate(conn) -> dict | None:
    """在调用方给的连接上搬家（自己管事务）。不需要搬时返回 None。"""
    if not needed(conn):
        return None
    tables = _tables(conn)
    report: dict = {"memories_before": conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]}
    conn.commit()  # 交出之前可能挂着的隐式事务，下面整段自己包
    conn.execute("BEGIN IMMEDIATE")
    try:
        if "memory_drafts" in tables:
            _move_drafts(conn, report)
        if "memory_edits" in tables:
            _pending_edits(conn, report)
        _tag_old(conn, report)
        conn.execute("DROP TABLE IF EXISTS memory_edits")
        conn.execute("DROP TABLE IF EXISTS memory_drafts")
        report["memories_after"] = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    path = db_path().parent / REPORT_FILE
    try:
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["report_file"] = str(path)
    except OSError as err:  # 报告写不进去不该拦启动；内容照样打进日志
        report["report_file_error"] = str(err)
    print(f"[migrate] 单库改造搬家完成：{json.dumps(_summary(report), ensure_ascii=False)}", flush=True)
    return report


def _summary(report: dict) -> dict:
    return {
        k: report.get(k)
        for k in ("memories_before", "memories_after", "moved_count", "new_id_range", "edges_written",
                  "tagged_old_count", "drafts", "report_file")
    } | {
        "links_skipped": len(report.get("links_skipped") or []),
        "pending_edits_not_applied": len(report.get("pending_edits_not_applied") or []),
    }


def main(argv: list[str]) -> int:
    """试跑：python -m app.migrate_single_store /path/to/copy.db —— 只在拷贝上跑。"""
    import os

    if len(argv) != 2:
        print("用法：python -m app.migrate_single_store /path/to/copy.db（只在备份拷贝上跑）", file=sys.stderr)
        return 2
    os.environ["EMBER_DB"] = argv[1]
    from app.db import init_db

    report = init_db()
    if report is None:
        print("这个库已经是单库结构了，没有要搬的。")
        return 0
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
