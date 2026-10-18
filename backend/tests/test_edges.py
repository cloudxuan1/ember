"""V4：关系边 + 区间时间。

边的纪律：软失败（目标不存在/连自己/重复 → 跳过不炸记忆本体）；
supersedes 边同步 superseded_by 列；草稿边任意审批顺序最终都齐（回填 + 幂等）。
区间状态读时现算，随"今天"自动变。
"""

import sqlite3

import pytest

from app import memories
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


def test_superseded_shows_in_search_catalog():
    """搜索目录要让模型看见"事实已翻页"——否则旧事实会被当成现状说出去。"""
    old = _save("朋友要生孩子了")
    new = memories.save_memory(
        date="2026-07-02", content="朋友生孩子了，是女宝",
        links=[{"id": old, "relation": "supersedes"}],
    )
    results = {r["id"]: r for r in memories.search_memories("生孩子")}
    assert results[old]["superseded_by"] == new["id"]
    assert "superseded_by" not in results[new["id"]]  # 现行事实不带多余字段


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
        # 老草稿表随单库搬家退场（这张老表一条待审草稿都没有）
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'memory_drafts'").fetchone() is None
