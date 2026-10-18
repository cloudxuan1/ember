"""单库搬家（app/migrate_single_store.py）：在按老结构造的库上跑，核对轩定的每一条规矩。"""

import json
import sqlite3
from pathlib import Path

import pytest

from app import memories, migrate_single_store
from app.db import db_path, get_conn, init_db

OLD_SCHEMA = (Path(__file__).parent / "fixtures" / "pre_single_store_schema.sql").read_text(encoding="utf-8")


@pytest.fixture
def old_db(tmp_path, monkeypatch):
    """老结构的库：已入库记忆 1–3（3 号被撤回删过，所以最大号是 2 但自增序列到过 3）、
    各种状态的草稿（含 5 条重复）、草稿之间和草稿到记忆的连线、一条没确认的修改提议。"""
    monkeypatch.setenv("EMBER_DB", str(tmp_path / "old.db"))
    conn = sqlite3.connect(db_path())
    conn.executescript(OLD_SCHEMA)
    conn.executescript(
        """
        INSERT INTO memories (id, date, content, tags, created_at) VALUES
          (1, '2026-04-18', '四月的旧事', '起点', '2026-07-01 10:00:00'),
          (2, '2026-09-15', '九月接上 ember', '', '2026-09-15 10:00:00'),
          (3, '2026-09-16', '后来被撤回的', '', '2026-09-16 10:00:00');
        DELETE FROM memories WHERE id = 3;
        INSERT INTO memory_sources (memory_id, source_ref, quote) VALUES (1, 'logs/a.txt#1', '原话一');
        """
    )
    drafts = [
        # (id, date, content, tags, status, memory_id, created_at, source_ref, quote, links, batch)
        (100, '2026-04-18', '已通过的旧快照', '', 'approved', 1, '2026-07-01 09:00:00', None, None, '', '4.18/g1'),
        (101, '2026-05-02', '被拒的', '', 'rejected', None, '2026-07-02 09:00:00', None, None, '', '4.18/g1'),
        (130, '2026-10-10', '重复一', '', 'pending', None, '2026-10-10 09:00:00', None, None, '', 'mcp-2026-10-10'),
        (156, '2026-10-11', '重复二', '', 'pending', None, '2026-10-11 09:00:00', None, None, '', 'mcp-2026-10-11'),
        (158, '2026-08-19', '重复三', '', 'pending', None, '2026-10-11 09:30:00', None, None, '', 'mcp-2026-10-11'),
        (167, '2026-10-10', '重复四', '', 'pending', None, '2026-10-11 10:00:00', None, None, '', 'mcp-2026-10-11'),
        (173, '2026-10-13', '重复五', '', 'pending', None, '2026-10-13 09:00:00', None, None, '', 'mcp-2026-10-13'),
        # 待审：注意 created_at 顺序和 id 顺序不一样，搬家按存的先后排
        (160, '2026-10-07', '十月草稿（后存）', '哈利波特', 'pending', None, '2026-10-08 09:00:00',
         None, None, json.dumps([{"draft_id": 159, "relation": "led_to", "dir": "in"}]), 'mcp-2026-10-08'),
        (159, '2026-07-20', '七月草稿（先存）', '关西', 'pending', None, '2026-10-01 09:00:00',
         'logs/b.txt#9', '原话二', json.dumps([{"memory_id": 1, "relation": "related", "dir": "out"},
                                              {"draft_id": 101, "relation": "related", "dir": "out"},
                                              {"draft_id": 100, "relation": "same_as", "dir": "out"},
                                              {"memory_id": 2, "relation": "none", "dir": "out"}]), '7.20/g3'),
        (161, '2026-06', '只到月的草稿', '旧档,心情', 'pending', None, '2026-10-09 09:00:00', None, None,
         json.dumps([{"memory_id": 2, "relation": "supersedes", "dir": "out"}]), 'mcp-2026-10-09'),
    ]
    conn.executemany(
        """INSERT INTO memory_drafts (id, date, content, tags, status, memory_id, created_at, source_ref, quote, links, batch)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        drafts,
    )
    conn.execute(
        """INSERT INTO memory_edits (memory_id, content, tags, topic, reason, base, created_by)
           VALUES (2, '九月接上 ember，补一句', NULL, '接入', '补原话', '{}', 'mcp')"""
    )
    conn.commit()
    conn.close()
    return tmp_path


def _rows(sql, params=()):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def test_migration_moves_pending_drafts_after_max_id_in_save_order(old_db):
    report = init_db()
    assert report["memories_before"] == 2 and report["memories_after"] == 5
    assert report["drafts"] == {
        "pending": 8, "skipped_duplicates": [130, 156, 158, 167, 173],
        "dropped_rejected": 1, "dropped_approved_snapshots": 1,
    }
    # 最大号是 2 → 新编号从 3 接，按 created_at 排：159(10-01) → 160(10-08) → 161(10-09)
    assert report["moved"] == [
        {"draft_id": 159, "memory_id": 3}, {"draft_id": 160, "memory_id": 4}, {"draft_id": 161, "memory_id": 5},
    ]
    rows = {r["id"]: r for r in _rows("SELECT * FROM memories")}
    assert rows[1]["content"] == "四月的旧事"  # 已入库的编号和内容不动
    assert rows[3]["content"] == "七月草稿（先存）" and rows[3]["created_at"] == "2026-10-01 09:00:00"
    assert rows[5]["date"] == "2026-06"
    assert all(r["deleted_at"] is None and r["updated_at"] is None for r in rows.values())
    assert not any("重复" in r["content"] or r["content"] in ("被拒的", "已通过的旧快照") for r in rows.values())
    # 搬完马上能搜到
    assert [m["id"] for m in memories.search_memories("十月草稿", space="all")] == [4]
    # 以后新存的接在后面
    assert memories.save_memory(date="2026-10-18", content="新的一条")["id"] == 6


def test_migration_moves_sources_and_rewires_links(old_db):
    report = init_db()
    assert _rows("SELECT source_ref, quote FROM memory_sources WHERE memory_id = 3") == [
        {"source_ref": "logs/b.txt#9", "quote": "原话二"}
    ]
    edges = {(e["from_id"], e["to_id"], e["relation"]) for e in _rows("SELECT * FROM memory_edges")}
    assert edges == {
        (3, 1, "related"),     # 草稿 → 已入库记忆
        (3, 1, "same_as"),     # 指向已通过草稿 100 = 记忆 1
        (3, 4, "led_to"),      # 草稿 160 说 159 led_to 它（dir in）→ 两条都搬了，换成新编号
        (5, 2, "supersedes"),
    }
    assert memories.get_memory(2)["superseded_by"] == 5
    # 指向被拒草稿 101 的那条对不上号，记进报告；"none" 不关联的不写也不算跳过
    assert report["links_skipped"] == [{"draft_id": 159, "link": {"draft_id": 101, "relation": "related", "dir": "out"}}]
    assert report["edges_written"] == 4


def test_migration_tags_old_by_event_date_before_august(old_db):
    report = init_db()
    tags = {r["id"]: r["tags"] for r in _rows("SELECT id, tags FROM memories")}
    assert tags[1] == "起点,旧档"     # 4 月，已入库
    assert tags[3] == "关西,旧档"     # 7 月，草稿
    assert tags[5] == "旧档,心情"     # 6 月，已经有旧档的不重复打
    assert tags[2] == "" and tags[4] == "哈利波特"  # 9 月 / 10 月不打
    assert report["tagged_old"] == [1, 3] and report["tagged_old_count"] == 2


def test_migration_reports_pending_edits_without_applying_and_drops_old_tables(old_db):
    report = init_db()
    assert report["pending_edits_not_applied"] == [{
        "memory_id": 2, "target": {"date": "2026-09-15", "preview": "九月接上 ember"},
        "proposed": {"content": "九月接上 ember，补一句", "topic": "接入"},
        "reason": "补原话", "created_by": "mcp", "updated_at": report["pending_edits_not_applied"][0]["updated_at"],
    }]
    assert memories.get_memory(2)["content"] == "九月接上 ember"
    names = {r["name"] for r in _rows("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "memory_drafts" not in names and "memory_edits" not in names
    assert {"memory_history", "activity_log", "settings"} <= names
    saved = json.loads(Path(report["report_file"]).read_text(encoding="utf-8"))
    assert saved["moved_count"] == 3
    assert _rows("SELECT * FROM activity_log") == []  # 搬家不刷屏最近动静


def test_migration_runs_once_and_is_atomic(old_db, monkeypatch):
    def boom(conn, report):
        raise RuntimeError("搬到一半出错")

    monkeypatch.setattr(migrate_single_store, "_tag_old", boom)
    with pytest.raises(RuntimeError):
        init_db()
    # 整个回滚：草稿表还在、一条都没搬
    names = {r["name"] for r in _rows("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "memory_drafts" in names and "memory_edits" in names
    assert _rows("SELECT COUNT(*) AS n FROM memories")[0]["n"] == 2
    monkeypatch.undo()
    monkeypatch.setenv("EMBER_DB", str(old_db / "old.db"))
    assert init_db()["moved_count"] == 3
    assert init_db() is None  # 第二次启动：没东西要搬
    assert _rows("SELECT COUNT(*) AS n FROM memories")[0]["n"] == 5


def test_fresh_database_needs_no_migration(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBER_DB", str(tmp_path / "new.db"))
    assert init_db() is None
    assert memories.save_memory(date="2026-10-18", content="第一条")["id"] == 1


def test_trial_run_cli_on_copy(old_db, capsys):
    assert migrate_single_store.main(["x", str(old_db / "old.db")]) == 0
    out = capsys.readouterr().out
    assert '"moved_count": 3' in out
    assert migrate_single_store.main(["x", str(old_db / "old.db")]) == 0
    assert "没有要搬的" in capsys.readouterr().out
