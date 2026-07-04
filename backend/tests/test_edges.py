"""V4：关系边 + 区间时间。

边的纪律：软失败（目标不存在/连自己/重复 → 跳过不炸记忆本体）；
supersedes 边同步 superseded_by 列；草稿边任意审批顺序最终都齐（回填 + 幂等）。
区间状态读时现算，随"今天"自动变。
"""

import sqlite3

import pytest

from app import drafts, memories
from app.db import db_path, get_conn, init_db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBER_DB", str(tmp_path / "test.db"))
    init_db()


def _save(content="x", date="2026-07-01", **kw) -> int:
    return memories.save_memory(date=date, content=content, **kw)["id"]


# ---------- memory_save 直连写边 ----------


def test_save_with_links_writes_edge_with_provenance():
    a = _save("原因")
    saved = memories.save_memory(
        date="2026-07-02", content="结果",
        links=[{"id": a, "relation": "led_to", "dir": "in"}],  # a 导致本条
        links_by="mcp",
    )
    assert saved["edges_written"] == 1
    edges = memories.get_memory(saved["id"])["edges"]
    assert len(edges) == 1
    e = edges[0]
    assert (e["from_id"], e["to_id"], e["relation"]) == (a, saved["id"], "led_to")
    assert e["direction"] == "in"
    assert e["created_by"] == "mcp"
    assert e["created_at"] is not None
    assert e["other"]["id"] == a and "原因" in e["other"]["content"]


def test_link_dir_out_is_default():
    a = _save("旧")
    saved = memories.save_memory(date="2026-07-02", content="新", links=[{"id": a}])
    e = memories.get_memory(saved["id"])["edges"][0]
    assert (e["from_id"], e["to_id"], e["relation"]) == (saved["id"], a, "related")


def test_duplicate_and_self_and_missing_target_are_soft_skipped():
    a = _save()
    saved = memories.save_memory(
        date="2026-07-02", content="y",
        links=[
            {"id": a, "relation": "related"},
            {"id": a, "relation": "related"},   # 重复边
            {"id": 9999, "relation": "related"},  # 目标不存在
        ],
    )
    assert saved["edges_written"] == 1  # 记忆本体照常保存，坏边悄悄跳过
    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0] == 1


def test_self_link_skipped_via_add_edges():
    a = _save()
    with get_conn() as conn:
        assert memories.add_edges(conn, a, [{"id": a, "relation": "related"}]) == 0


def test_bad_links_reject_whole_save():
    a = _save()
    with pytest.raises(ValueError):
        memories.save_memory(date="2026-07-02", content="y", links=[{"id": a, "relation": "caused"}])
    with pytest.raises(ValueError):
        memories.save_memory(date="2026-07-02", content="y", links=[{"relation": "related"}])
    with get_conn() as conn:  # 坏 links 不留半条记忆
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


def test_supersedes_edge_syncs_superseded_by_column():
    old = _save("旧说法")
    new = memories.save_memory(
        date="2026-07-02", content="新说法", links=[{"id": old, "relation": "supersedes"}]
    )
    assert memories.get_memory(old)["superseded_by"] == new["id"]


# ---------- 区间时间读时现算 ----------


def test_interval_status_computed_against_today():
    assert memories.interval_status(None, None) is None  # 点事件
    assert memories.interval_status("2026-07-10", "2026-07-20", today="2026-07-01") == "upcoming"
    assert memories.interval_status("2026-07-10", "2026-07-20", today="2026-07-15") == "ongoing"
    assert memories.interval_status("2026-07-10", "2026-07-20", today="2026-07-21") == "ended"
    # 边界日算区间内
    assert memories.interval_status("2026-07-10", "2026-07-20", today="2026-07-10") == "ongoing"
    assert memories.interval_status("2026-07-10", "2026-07-20", today="2026-07-20") == "ongoing"
    # 只有 start = 开放区间，一直 ongoing；只有 end = 截止型
    assert memories.interval_status("2026-04-01", None, today="2027-01-01") == "ongoing"
    assert memories.interval_status(None, "2026-07-20", today="2026-07-01") == "ongoing"
    assert memories.interval_status(None, "2026-07-20", today="2026-08-01") == "ended"


def test_recall_and_catalog_carry_interval_status():
    mid = _save("在做 ember", start_date="2026-04-01")
    memory = memories.get_memory(mid)
    assert memory["interval_status"] == "ongoing"
    point = memories.get_memory(_save("ember 点事件"))
    assert point["interval_status"] is None
    items = memories.search_memories("ember")
    interval_item = next(i for i in items if i["id"] == mid)
    assert interval_item["interval_status"] == "ongoing"
    point_item = next(i for i in items if i["id"] != mid)
    assert "interval_status" not in point_item  # 目录保持精瘦


# ---------- 草稿边建议 → 审核入库 ----------


def _draft(content="草稿", links="", **kw) -> int:
    return drafts.save_draft(date="2026-07-01", content=content, links=links, **kw)["id"]


def _edges() -> list[tuple]:
    with get_conn() as conn:
        return [
            (r["from_id"], r["to_id"], r["relation"], r["created_by"])
            for r in conn.execute("SELECT * FROM memory_edges ORDER BY id").fetchall()
        ]


def test_draft_link_to_existing_memory():
    old = _save("已入库的旧记忆")
    d = _draft(links=[{"memory_id": old, "relation": "led_to", "dir": "in"}])
    result = drafts.approve_draft(d)
    assert _edges() == [(old, result["memory_id"], "led_to", "extraction")]


def test_draft_links_validation_rejects_bad_shape():
    with pytest.raises(ValueError):
        _draft(links=[{"relation": "related"}])  # 没有目标
    with pytest.raises(ValueError):
        _draft(links=[{"memory_id": 1, "draft_id": 2}])  # 目标二选一
    with pytest.raises(ValueError):
        _draft(links=[{"memory_id": 1, "relation": "caused"}])
    with pytest.raises(ValueError):
        _draft(links="不是json")


def test_draft_to_draft_edges_any_approve_order():
    # B 建议：A 导致 B。先审 B（目标还 pending，边暂缺）→ 再审 A（回填补上）
    a = _draft("事件A")
    b = _draft("事件B", links=[{"draft_id": a, "relation": "led_to", "dir": "in"}])
    rb = drafts.approve_draft(b)
    assert _edges() == []  # A 还没入库，先跳过
    ra = drafts.approve_draft(a)
    assert _edges() == [(ra["memory_id"], rb["memory_id"], "led_to", "extraction")]


def test_draft_to_draft_edges_in_order():
    a = _draft("事件A")
    b = _draft("事件B", links=[{"draft_id": a, "relation": "led_to", "dir": "in"}])
    ra = drafts.approve_draft(a)
    rb = drafts.approve_draft(b)  # 审到 B 时 A 已有记忆 id，直接写
    assert _edges() == [(ra["memory_id"], rb["memory_id"], "led_to", "extraction")]


def test_rejected_target_drops_edge_silently():
    a = _draft("要被拒的")
    b = _draft("事件B", links=[{"draft_id": a, "relation": "related"}])
    drafts.reject_draft(a)
    result = drafts.approve_draft(b)
    assert result["status"] == "approved" and _edges() == []


def test_unreview_removes_edges_and_reapprove_regenerates():
    a = _draft("事件A")
    b = _draft("事件B", links=[{"draft_id": a, "relation": "led_to", "dir": "in"}])
    drafts.approve_draft(a)
    drafts.approve_draft(b)
    assert len(_edges()) == 1
    drafts.unreview_draft(a)  # 撤回 A：记忆连边一起删
    assert _edges() == []
    ra2 = drafts.approve_draft(a)  # 重新通过：B 的边建议还在，回填复活
    rb = drafts.get_draft(b)
    assert _edges() == [(ra2["memory_id"], rb["memory_id"], "led_to", "extraction")]


def test_unreview_unwinds_superseded_by():
    old = _save("旧说法")
    d = _draft("新说法", links=[{"memory_id": old, "relation": "supersedes"}])
    drafts.approve_draft(d)
    assert memories.get_memory(old)["superseded_by"] is not None
    drafts.unreview_draft(d)  # 压它的记忆没了，压制解除
    assert memories.get_memory(old)["superseded_by"] is None


def test_draft_api_roundtrip_returns_parsed_links():
    old = _save()
    d = _draft(links=[{"memory_id": old, "relation": "related", "dir": "out"}])
    assert drafts.get_draft(d)["links"] == [{"memory_id": old, "relation": "related", "dir": "out"}]
    assert drafts.list_drafts()["items"][0]["links"][0]["memory_id"] == old
    updated = drafts.update_draft(d, {"links": []})  # 审核台 ✕ 移除连线走这条路
    assert updated["links"] == []
    drafts.approve_draft(d)
    assert _edges() == []


# ---------- 老库就地迁移 ----------


def test_migration_upgrades_v3_edges_table(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBER_DB", str(tmp_path / "old.db"))
    conn = sqlite3.connect(db_path())
    conn.executescript(
        """CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL,
               content TEXT NOT NULL, tags TEXT DEFAULT '', tier TEXT DEFAULT 'normal',
               topic TEXT DEFAULT '', space TEXT DEFAULT 'personal', start_date TEXT, end_date TEXT,
               is_resolved INTEGER DEFAULT 0, superseded_by INTEGER REFERENCES memories(id),
               created_at TEXT DEFAULT (datetime('now','+8 hours')));
           CREATE TABLE memory_edges (id INTEGER PRIMARY KEY AUTOINCREMENT,
               from_id INTEGER NOT NULL REFERENCES memories(id),
               to_id INTEGER NOT NULL REFERENCES memories(id), relation TEXT DEFAULT 'related');
           CREATE TABLE memory_drafts (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL,
               content TEXT NOT NULL, batch TEXT DEFAULT '', status TEXT DEFAULT 'pending');
           INSERT INTO memories (date, content) VALUES ('2026-01-01','a'), ('2026-01-02','b');
           INSERT INTO memory_edges (from_id, to_id, relation) VALUES (1, 2, 'led_to');"""
    )
    conn.commit()
    conn.close()

    init_db()  # 迁移 + 建表，幂等
    init_db()

    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM memory_edges").fetchall()
        assert [(r["from_id"], r["to_id"], r["relation"]) for r in rows] == [(1, 2, "led_to")]
        assert rows[0]["created_by"] == "" and "created_at" in rows[0].keys()
        # 新约束生效：重复边被 UNIQUE 挡住，坏 relation 被 CHECK 挡住
        conn.execute("INSERT OR IGNORE INTO memory_edges (from_id,to_id,relation) VALUES (1,2,'led_to')")
        assert conn.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO memory_edges (from_id,to_id,relation) VALUES (2,1,'caused')")
        assert "links" in {r["name"] for r in conn.execute("PRAGMA table_info(memory_drafts)")}
