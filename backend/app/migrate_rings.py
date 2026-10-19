"""年轮卡搬家（2026-10 记忆库第二轮 PR1）：每条记忆变成一张卡，原来的 content 原样抄成第一圈。

init_db() 每次启动都会调 migrate()。只加不删、可以重复跑：
- 还没有年轮的记忆才搬（有圈 = 搬过了），跑第二遍不会重复搬；没有要搬的什么都不做、不写报告。
- 搬的时候（规矩见 app/rings.py from_legacy，轩 2026-10-20 定）：content 原样抄成第一圈、日期照旧，
  content 列保留；topic 只被一张卡用 → 标题，好几张共用 → 并进便利贴，空的 → 标题留空；tier = anchor → ⭐。
  topic、tier 两栏原样留着（PR5 才删）。删掉的（藏起来的）也照样搬，恢复回来照常是卡。
- 只有一圈的卡，圈跟卡对不上（只会发生在退回旧程序期间改过内容 / 日期）：让这一圈跟上卡，编号写进报告。
- 来源、上一版、最近动静、连线、语义指纹都不碰；不改 updated_at、不刷屏最近动静、不重算指纹（按圈算是 PR2）。
- 整个一个事务。提交前先自检：每张卡都有圈、只有一圈的和卡逐字一样、原有各栏没变、标签只多不少、
  每张卡的标题 / ⭐ / 便利贴跟按规矩另算的一份对得上、搬过的卡没被动、圈数 = 原有 + 新搬、
  来源 / 上一版 / 动静 / 连线 / 指纹 / 浮现冷却的条数没变；不对就整个回滚并报错（程序起不来，自动部署会退回上一版）。

试跑（只在备份拷贝上跑）：python -m app.migrate_rings /path/to/copy.db --against /path/to/original.db
先搬拷贝、打印报告，再拿只读打开的原备份逐条核对，打印要核对的数字。
"""

import json
import os
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

from app import memories, rings
from app.db import db_path

REPORT_FILE = "migration_rings.json"
# 搬家不该碰的：卡上原有的各栏（tags 只能多、title / starred 是新栏）和这几张表的条数
KEPT_FIELDS = (
    "date", "content", "topic", "tier", "space", "start_date", "end_date", "is_resolved",
    "superseded_by", "created_at", "updated_at", "deleted_at", "deleted_by",
)
KEPT_TABLES = (
    "memory_sources", "memory_history", "activity_log", "memory_edges", "memory_embeddings", "briefing_log",
)

_UNMOVED = "SELECT id FROM memories m WHERE NOT EXISTS (SELECT 1 FROM memory_rings r WHERE r.memory_id = m.id)"
# 只有一圈、没删过圈，但圈跟卡对不上
_DRIFTED = """SELECT m.id FROM memories m JOIN memory_rings r ON r.memory_id = m.id
              WHERE r.deleted_at IS NULL
                AND (SELECT COUNT(*) FROM memory_rings r2 WHERE r2.memory_id = m.id) = 1
                AND (r.content IS NOT m.content OR r.date IS NOT m.date)"""


def _now(conn) -> str:
    return conn.execute("SELECT datetime('now','+8 hours')").fetchone()[0]


def _ids(conn, sql: str) -> list[int]:
    return [r[0] for r in conn.execute(sql + " ORDER BY m.id")]


def needed(conn) -> bool:
    return bool(conn.execute(f"SELECT EXISTS({_UNMOVED}) OR EXISTS({_DRIFTED})").fetchone()[0])


def _tag_set(tags: str | None) -> set[str]:
    return {t for t in memories.normalize_tags(tags or "").split(",") if t}


def _col(row, key):
    return row[key] if key in row.keys() else None  # 很老的备份里没有这一栏


def _card_now(row) -> tuple:
    """一张卡的（标题, ⭐, 便利贴）：空标题一律算 None，便利贴按集合比。"""
    return (_col(row, "title") or None, _col(row, "starred") or 0, _tag_set(row["tags"]))


def _expected(rows: dict, moved_before: set[int]) -> dict:
    """照方案规矩，从搬之前的样子独立算出每张卡搬完该有的（标题, ⭐, 便利贴）。
    不调 rings.from_legacy：搬家自检和试跑核对都拿这一份去对它的结果。搬之前就有圈的卡算搬过，三样都该原样不动。"""
    live: Counter = Counter()
    for r in rows.values():
        topic = (r["topic"] or "").strip()
        if topic and not _col(r, "deleted_at"):
            live[topic] += 1
    out = {}
    for i, r in rows.items():
        title, starred, tags = _card_now(r)
        topic = (r["topic"] or "").strip()
        if i not in moved_before:
            if topic and live[topic] + (1 if _col(r, "deleted_at") else 0) == 1:
                title = title or topic
            elif topic:
                tags = tags | _tag_set(topic)
            if r["tier"] == "anchor":
                starred = 1
        out[i] = (title, starred, tags)
    return out


def _state(conn) -> dict:
    rows = conn.execute("SELECT * FROM memories").fetchall()
    return {
        "rows": {r["id"]: r for r in rows},
        "fields": {r["id"]: tuple(r[k] for k in KEPT_FIELDS) for r in rows},
        "tags": {r["id"]: _tag_set(r["tags"]) for r in rows},
        "card": {r["id"]: (r["title"], r["starred"], r["tags"]) for r in rows},  # 只有这次新搬的卡能变
        "now": {r["id"]: _card_now(r) for r in rows},
        "rings": conn.execute("SELECT COUNT(*) FROM memory_rings").fetchone()[0],
        "moved_before": {r[0] for r in conn.execute("SELECT DISTINCT memory_id FROM memory_rings")},
        "tables": {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in KEPT_TABLES},
    }


def _check(conn, before: dict, moved: list[int]) -> None:
    """自检，不对就抛 RuntimeError（调用方回滚）。只报编号，不带记忆内容。"""
    after = _state(conn)
    problems = []
    if set(after["fields"]) != set(before["fields"]):
        problems.append("记忆编号变了")
    changed = sorted(i for i, v in before["fields"].items() if after["fields"].get(i) != v)
    if changed:
        problems.append(f"原有各栏被改了：{changed}")
    lost = sorted(i for i, tags in before["tags"].items() if not tags <= after["tags"].get(i, set()))
    if lost:
        problems.append(f"标签少了：{lost}")
    again = sorted(i for i, v in before["card"].items() if i not in set(moved) and after["card"].get(i) != v)
    if again:
        problems.append(f"搬过的卡又被动了：{again}")
    if after["rings"] != before["rings"] + len(moved):
        problems.append(f"圈数不对：原有 {before['rings']} + 新搬 {len(moved)} ≠ {after['rings']}")
    expected = _expected(before["rows"], before["moved_before"])
    wrong = sorted(i for i, want in expected.items() if after["now"].get(i) != want)
    if wrong:
        problems.append(f"标题 / ⭐ / 便利贴没按规矩搬：{wrong}")
    tables = [t for t in KEPT_TABLES if after["tables"][t] != before["tables"][t]]
    if tables:
        problems.append(f"这些表的条数变了：{tables}")
    unmoved = _ids(conn, _UNMOVED)
    if unmoved:
        problems.append(f"还有卡没有圈：{unmoved}")
    drifted = _ids(conn, _DRIFTED)
    if drifted:
        problems.append(f"只有一圈的卡和圈对不上：{drifted}")
    if problems:
        raise RuntimeError("年轮卡搬家自检没过，已整个回滚：" + "；".join(problems))


def migrate(conn) -> dict | None:
    """在调用方给的连接上搬家（自己管事务）。不需要搬时返回 None。"""
    if not needed(conn):
        return None
    conn.commit()  # 交出之前可能挂着的隐式事务，下面整段自己包
    conn.execute("BEGIN IMMEDIATE")
    try:
        before = _state(conn)
        report: dict = {
            "cards": len(before["fields"]),
            "cards_deleted": conn.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NOT NULL").fetchone()[0],
            "rings_before": before["rings"],
            "tag_pairs_before": sum(len(t) for t in before["tags"].values()),
        }
        drifted = _ids(conn, _DRIFTED)  # 搬之前就有圈的卡里对不上的；这次新搬的不算，搬错了要让自检拦下来
        counts = rings.topic_counts(conn)
        moved, titled, tagged, starred, shared = [], [], [], [], set()
        for row in conn.execute(f"SELECT * FROM memories WHERE id IN ({_UNMOVED}) ORDER BY id").fetchall():
            rings.add_first(conn, row["id"])
            done = rings.from_legacy(conn, row, counts)
            moved.append(row["id"])
            if "title" in done:
                titled.append(row["id"])
            if "tags" in done:
                tagged.append(row["id"])
            if (row["topic"] or "").strip() and "title" not in done:
                shared.add(row["topic"].strip())
            if "starred" in done:
                starred.append(row["id"])
        for memory_id in drifted:
            rings.follow_card(conn, memory_id)
        _check(conn, before, moved)
        report |= {
            "moved": len(moved),
            "moved_ids": moved,
            "rings_after": conn.execute("SELECT COUNT(*) FROM memory_rings").fetchone()[0],
            "title_from_topic": len(titled),
            "title_from_topic_ids": titled,
            "topic_to_tags": len(tagged),
            "topic_to_tags_ids": tagged,
            "shared_topics": len(shared),
            "starred_from_anchor": len(starred),
            "starred_from_anchor_ids": starred,
            "rings_resynced": drifted,
            "tag_pairs_after": sum(len(_tag_set(r[0])) for r in conn.execute("SELECT tags FROM memories")),
            "self_check": "ok",
        }
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    path = db_path().parent / REPORT_FILE
    if path.exists():  # 第一份报告留着；以后再搬（退回旧程序又回来）另起一份
        path = path.with_name(f"migration_rings.{_now(conn).replace(' ', 'T').replace(':', '')}.json")
    try:
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["report_file"] = str(path)
    except OSError as err:  # 报告写不进去不该拦启动；数字照样打进日志
        report["report_file_error"] = str(err)
    print(f"[migrate] 年轮卡搬家完成：{json.dumps(_summary(report), ensure_ascii=False)}", flush=True)
    return report


def _summary(report: dict) -> dict:
    keys = ("cards", "cards_deleted", "rings_before", "moved", "rings_after", "title_from_topic", "topic_to_tags",
            "shared_topics", "starred_from_anchor", "tag_pairs_before", "tag_pairs_after", "report_file")
    return {k: report.get(k) for k in keys} | {"rings_resynced": len(report.get("rings_resynced") or [])}


def _open_ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def compare(original: str, migrated: str, sample: int = 5) -> dict:
    """搬完的拷贝跟搬之前的原备份逐条对（两个库都只读打开），给出要核对的数字；ok 是总判断。
    这段跟搬家本身的代码分开写，不复用它的自检，图的就是两边各算一遍。"""
    a, b = _open_ro(original), _open_ro(migrated)
    try:
        old = {r["id"]: r for r in a.execute("SELECT * FROM memories")}
        new = {r["id"]: r for r in b.execute("SELECT * FROM memories")}
        by_card = defaultdict(list)
        for r in b.execute("SELECT * FROM memory_rings ORDER BY id"):
            by_card[r["memory_id"]].append(r)
        both = sorted(set(old) & set(new))
        had_rings = a.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memory_rings'").fetchone()
        moved_before = {r[0] for r in a.execute("SELECT DISTINCT memory_id FROM memory_rings")} if had_rings else set()
        expected = _expected(old, moved_before)
        mismatch = {"title": [], "starred": [], "tags": []}
        for i in both:
            for name, got, want in zip(mismatch, _card_now(new[i]), expected[i]):
                if got != want:
                    mismatch[name].append(i)

        def first_same(i: int) -> bool:
            rs = by_card.get(i)
            return bool(rs) and rs[0]["content"] == old[i]["content"] and rs[0]["date"] == old[i]["date"]

        out = {
            "memories_before": len(old),
            "memories_after": len(new),
            "same_ids": set(old) == set(new),
            "rings": sum(len(v) for v in by_card.values()),
            "cards_with_one_ring": sum(1 for i in new if len(by_card.get(i, [])) == 1),
            "first_ring_same_as_original": sum(1 for i in both if first_same(i)),
            "content_unchanged": sum(1 for i in both if new[i]["content"] == old[i]["content"]),
            "other_fields_unchanged": sum(
                1 for i in both if all(new[i][k] == old[i][k] for k in KEPT_FIELDS if k in old[i].keys())
            ),
            "tag_pairs_before": sum(len(_tag_set(old[i]["tags"])) for i in old),
            "tag_pairs_after": sum(len(_tag_set(new[i]["tags"])) for i in new),
            "tags_lost": sum(len(_tag_set(old[i]["tags"]) - _tag_set(new[i]["tags"])) for i in both),
            "anchors_before": sum(1 for i in old if old[i]["tier"] == "anchor"),
            "titles_expected": sum(1 for v in expected.values() if v[0]),
            "titles_after": sum(1 for i in new if new[i]["title"]),
            "title_mismatch": mismatch["title"],
            "starred_expected": sum(v[1] for v in expected.values()),
            "starred_after": sum(1 for i in new if new[i]["starred"]),
            "starred_mismatch": mismatch["starred"],
            "tags_mismatch": mismatch["tags"],
            "tables_unchanged": {
                t: [tuple(r) for r in a.execute(f"SELECT * FROM {t} ORDER BY rowid")]
                == [tuple(r) for r in b.execute(f"SELECT * FROM {t} ORDER BY rowid")]
                for t in KEPT_TABLES + ("settings",)
            },
            "sample": [{"id": i, "first_ring_same": first_same(i)} for i in random.sample(both, min(sample, len(both)))],
            "integrity_check": b.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_problems": len(b.execute("PRAGMA foreign_key_check").fetchall()),
        }
    finally:
        a.close()
        b.close()
    n = out["memories_before"]
    out["ok"] = (
        out["same_ids"] and out["memories_after"] == n and out["cards_with_one_ring"] == n
        and out["first_ring_same_as_original"] == n and out["content_unchanged"] == n
        and out["other_fields_unchanged"] == n and out["tags_lost"] == 0
        and out["tag_pairs_after"] >= out["tag_pairs_before"] and not any(mismatch.values())
        and all(out["tables_unchanged"].values()) and all(s["first_ring_same"] for s in out["sample"])
        and out["integrity_check"] == "ok" and out["foreign_key_problems"] == 0
    )
    return out


USAGE = "用法：python -m app.migrate_rings /path/to/copy.db [--against /path/to/original.db]（只在备份拷贝上跑）"


def main(argv: list[str]) -> int:
    args = argv[1:]
    against = None
    if len(args) == 3 and args[1] == "--against":
        against, args = args[2], args[:1]
    if len(args) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    for path in (args[0], against):
        if path is not None and not os.path.isfile(path):  # 打错路径时 sqlite 会悄悄建一个空库
            print(f"找不到库文件：{path}", file=sys.stderr)
            return 2
    if against and os.path.samefile(args[0], against):
        print("拷贝和原备份是同一个文件：先复制一份再搬", file=sys.stderr)
        return 2
    os.environ["EMBER_DB"] = args[0]
    from app.db import get_conn, migrate_all

    conn = get_conn()
    try:
        report = migrate_all(conn)[1]  # 跟启动时一模一样：先补表补列、单库搬家（早就搬过的会跳过），再年轮卡
    except RuntimeError as err:  # 自检没过：搬家整个回滚了（建表补列已经做了，数据没动）
        print(str(err), file=sys.stderr)
        return 1
    finally:
        conn.close()
    if report is None:
        print("这个库已经都是年轮卡了，没有要搬的。")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    if against is None:
        return 0
    result = compare(against, args[0])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
