"""年轮卡搬家（app/migrate_rings.py）：在按老结构（单库改造后、年轮卡之前）造的库上跑，
核对方案里的每一条规矩：只加不删、可以重复跑、跑第二遍不重复搬。测试数据全是编的。"""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from app import memories, migrate_rings, rings
from app.db import db_path, get_conn, init_db

OLD_SCHEMA = (Path(__file__).parent / "fixtures" / "pre_rings_schema.sql").read_text(encoding="utf-8")
SINGLE_STORE_SCHEMA = (Path(__file__).parent / "fixtures" / "pre_single_store_schema.sql").read_text(encoding="utf-8")

# (id, date, content, tags, tier, topic, space, created_at, updated_at, deleted_at)
CARDS = [
    (1, "2026-04-18", "编的：四月去看樱花", "春天", "anchor", "赏樱", "personal", "2026-07-01 10:00:00", None, None),
    (2, "2026-09", "编的：九月的周末小日记\n第二行带空格 ", "心情", "normal", "家人、朋友", "personal",
     "2026-09-30 21:00:00", None, None),
    (3, "2026-09-20", "编的：周末一起包饺子", "家人", "normal", "家人、朋友", "personal", "2026-09-20 20:00:00", None, None),
    (4, "2026-10-01", "编的：没有主题的一条", "", "process", "", "personal", "2026-10-01 09:00:00", None, None),
    # 存重复了又删掉的：删掉的不算「在用」，所以 1 号的赏樱还是只有它一张 → 标题；它自己按恢复回来算 → 便利贴
    (5, "2026-10-02", "编的：存重复了的赏樱", "", "normal", "赏樱", "personal", "2026-10-02 09:00:00", None,
     "2026-10-03 09:00:00"),
    (6, "2026-10-03", "编的：项目笔记", "ember", "anchor", "  部署  ", "ember", "2026-10-03 09:00:00",
     "2026-10-04 10:00:00.000", None),
]


def _make_old_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute("PRAGMA user_version = 1")  # 单库搬家已经做过
    conn.executemany(
        """INSERT INTO memories (id, date, content, tags, tier, topic, space, created_at, updated_at, deleted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        CARDS,
    )
    conn.executescript(
        """
        INSERT INTO memory_sources (memory_id, source_ref, quote) VALUES (1, 'logs/fake.txt#1', '编的原话');
        INSERT INTO memory_edges (from_id, to_id, relation, created_by) VALUES (1, 3, 'related', 'mcp');
        INSERT INTO memory_history (memory_id, snapshot, replaced_by) VALUES (3, '{"content": "编的：旧版"}', 'ke');
        INSERT INTO activity_log (actor, action, memory_id, title) VALUES ('ke', 'save', 4, '编的');
        INSERT INTO memory_embeddings (memory_id, model, dim, embedding) VALUES (1, 'fake-model', 1, x'0000803f');
        INSERT INTO briefing_log (memory_id) VALUES (2);
        INSERT INTO settings (key, value) VALUES ('history_keep', '2');
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture
def old_db(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBER_DB", str(tmp_path / "old.db"))
    _make_old_db(tmp_path / "old.db")
    return tmp_path


def _rows(sql, params=()):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _dump(table: str) -> list[tuple]:
    with get_conn() as conn:
        return [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def _report_files(folder: Path) -> list[Path]:
    return sorted(folder.glob("migration_rings*.json"))


def test_every_memory_becomes_a_card_with_its_content_as_first_ring(old_db):
    kept = {t: _dump_raw(old_db / "old.db", t) for t in migrate_rings.KEPT_TABLES + ("settings",)}
    init_db()
    cards = {r["id"]: r for r in _rows("SELECT * FROM memories")}
    ring_rows = _rows("SELECT * FROM memory_rings ORDER BY id")
    assert len(cards) == 6 and len(ring_rows) == 6  # 卡数 = 记忆数 = 第一圈数
    for (mid, date, content, *_rest), ring in zip(CARDS, ring_rows):
        assert ring["memory_id"] == mid
        assert (ring["date"], ring["content"]) == (date, content)  # 逐字照抄，空白和换行都不动
        assert cards[mid]["content"] == content and cards[mid]["date"] == date  # content 列保留
        assert ring["created_at"] == cards[mid]["created_at"] and ring["deleted_at"] is None
    assert ring_rows[5]["updated_at"] == "2026-10-04 10:00:00.000"
    # 原有的来源、连线、上一版、最近动静、指纹、浮现冷却、设置一条没动；搬家不刷屏最近动静
    assert {t: _dump(t) for t in kept} == kept


def test_topic_and_tier_follow_the_rules(old_db):
    init_db()
    cards = {r["id"]: r for r in _rows("SELECT * FROM memories")}
    assert cards[1]["title"] == "赏樱" and cards[1]["tags"] == "春天"  # 删掉的 5 号不算，只有它在用
    assert cards[5]["title"] is None and cards[5]["tags"] == "赏樱"      # 它自己恢复回来就是两张
    assert cards[2]["title"] is None and cards[2]["tags"] == "心情,家人,朋友"  # 共用 → 便利贴，顿号照样拆
    assert cards[3]["tags"] == "家人,朋友"                                   # 已有的不重复加
    assert cards[4]["title"] is None and cards[4]["tags"] == ""             # 空主题 → 标题留空
    assert cards[6]["title"] == "部署"                                       # 前后空白去掉
    assert {i for i, c in cards.items() if c["starred"]} == {1, 6}          # anchor → ⭐
    assert [cards[i]["topic"] for i in range(1, 7)] == ["赏樱", "家人、朋友", "家人、朋友", "", "赏樱", "  部署  "]
    assert [cards[i]["tier"] for i in range(1, 7)] == ["anchor", "normal", "normal", "process", "normal", "anchor"]


def test_report_has_the_numbers_to_check(old_db):
    init_db()
    [path] = _report_files(old_db)
    report = json.loads(path.read_text(encoding="utf-8"))
    assert {k: report[k] for k in (
        "cards", "cards_deleted", "rings_before", "moved", "rings_after", "title_from_topic", "topic_to_tags",
        "shared_topics", "starred_from_anchor", "tag_pairs_before", "tag_pairs_after", "rings_resynced", "self_check",
    )} == {
        "cards": 6, "cards_deleted": 1, "rings_before": 0, "moved": 6, "rings_after": 6, "title_from_topic": 2,
        "topic_to_tags": 3, "shared_topics": 2, "starred_from_anchor": 2, "tag_pairs_before": 4,
        "tag_pairs_after": 8, "rings_resynced": [], "self_check": "ok",
    }
    assert report["title_from_topic_ids"] == [1, 6] and report["topic_to_tags_ids"] == [2, 3, 5]
    assert "编的" not in path.read_text(encoding="utf-8")  # 报告只有编号和数字，不带记忆内容


def test_second_run_moves_nothing_and_changes_nothing(old_db):
    init_db()
    with get_conn() as conn:
        first = list(conn.iterdump())
        assert migrate_rings.migrate(conn) is None
    init_db()
    with get_conn() as conn:
        assert list(conn.iterdump()) == first
    assert len(_report_files(old_db)) == 1


def test_rerun_after_old_program_ran_moves_only_the_new_ones(old_db):
    init_db()
    [first_report] = _report_files(old_db)
    first_text = first_report.read_text(encoding="utf-8")
    # 退回旧程序期间：它不认识年轮，存的新记忆没有圈，改内容也不会动圈；改 topic 只改老栏位
    conn = sqlite3.connect(db_path())
    conn.execute(
        """INSERT INTO memories (id, date, content, tags, tier, topic) VALUES
           (7, '2026-10-19', '编的：回退期间存的赏樱', '', 'anchor', '赏樱')"""
    )
    conn.execute("UPDATE memories SET content = '编的：周末一起包饺子，还擀了皮' WHERE id = 3")
    conn.execute("UPDATE memories SET topic = '回退期间补的主题' WHERE id = 4")
    conn.commit()
    conn.close()
    with get_conn() as conn:  # 重新上线：启动时 init_db 里跑的就是这一步
        second = migrate_rings.migrate(conn)
    assert second["moved_ids"] == [7] and second["rings_resynced"] == [3]
    cards = {r["id"]: r for r in _rows("SELECT * FROM memories")}
    assert cards[7]["tags"] == "赏樱" and cards[7]["title"] is None and cards[7]["starred"] == 1  # 1 号也在用 → 便利贴
    assert cards[1]["title"] == "赏樱"  # 搬过的不再动
    assert cards[4]["title"] is None    # 搬过的卡改了 topic 也不再搬
    assert second["rings_before"] == 6 and second["rings_after"] == 7
    assert _rows("SELECT content FROM memory_rings WHERE memory_id = 3") == [{"content": "编的：周末一起包饺子，还擀了皮"}]
    assert first_report.read_text(encoding="utf-8") == first_text  # 第一份报告留着，另起一份
    assert len(_report_files(old_db)) == 2


def test_failure_halfway_rolls_back_everything(old_db, monkeypatch):
    calls = []
    real = rings.from_legacy

    def boom(conn, row, counts, can_star=True):
        calls.append(row["id"])
        if len(calls) == 3:
            raise RuntimeError("搬到一半出错")
        return real(conn, row, counts, can_star)

    monkeypatch.setattr(rings, "from_legacy", boom)
    with pytest.raises(RuntimeError, match="搬到一半"):
        init_db()
    assert _rows("SELECT COUNT(*) AS n FROM memory_rings") == [{"n": 0}]
    assert _rows("SELECT COUNT(*) AS n FROM memories WHERE title IS NOT NULL OR starred = 1") == [{"n": 0}]
    assert _report_files(old_db) == []
    monkeypatch.setattr(rings, "from_legacy", real)
    init_db()
    assert _rows("SELECT COUNT(*) AS n FROM memory_rings") == [{"n": 6}]


@pytest.mark.parametrize("bug", ["wrong_ring", "lost_tag", "changed_field", "extra_ring"])
def test_self_check_refuses_a_wrong_move(old_db, monkeypatch, bug):
    real_add, real_legacy = rings.add_first, rings.from_legacy

    def add_first(conn, memory_id):
        rid = real_add(conn, memory_id)
        if bug == "wrong_ring" and memory_id == 2:
            conn.execute("UPDATE memory_rings SET content = content || '多了一个字' WHERE id = ?", (rid,))
        if bug == "extra_ring" and memory_id == 2:
            real_add(conn, memory_id)
        return rid

    def from_legacy(conn, row, counts, can_star=True):
        done = real_legacy(conn, row, counts, can_star)
        if bug == "lost_tag" and row["id"] == 3:
            conn.execute("UPDATE memories SET tags = '' WHERE id = 3")
        if bug == "changed_field" and row["id"] == 4:
            conn.execute("UPDATE memories SET updated_at = '2026-10-20 00:00:00' WHERE id = 4")
        return done

    monkeypatch.setattr(rings, "add_first", add_first)
    monkeypatch.setattr(rings, "from_legacy", from_legacy)
    with pytest.raises(RuntimeError, match="自检没过"):
        init_db()
    assert _rows("SELECT COUNT(*) AS n FROM memory_rings") == [{"n": 0}]
    assert _rows("SELECT tags FROM memories WHERE id = 3") == [{"tags": "家人"}]


def test_fresh_database_has_nothing_to_move(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBER_DB", str(tmp_path / "new.db"))
    init_db()
    with get_conn() as conn:
        assert migrate_rings.migrate(conn) is None
    assert _report_files(tmp_path) == []


def test_pre_single_store_database_gets_both_moves_on_one_start(tmp_path, monkeypatch):
    """很老的库（还有草稿表）：先单库搬家，搬进来的草稿也一样变成卡。"""
    monkeypatch.setenv("EMBER_DB", str(tmp_path / "older.db"))
    conn = sqlite3.connect(db_path())
    conn.executescript(SINGLE_STORE_SCHEMA)
    conn.execute("INSERT INTO memories (id, date, content, topic) VALUES (1, '2026-09-01', '编的：老记忆', '散步')")
    conn.execute(
        "INSERT INTO memory_drafts (id, date, content, topic, tier) VALUES (200, '2026-09-02', '编的：待审草稿', '散步', 'anchor')"
    )
    conn.commit()
    conn.close()
    assert init_db()["moved_count"] == 1
    cards = {r["id"]: r for r in _rows("SELECT * FROM memories")}
    assert {r["memory_id"] for r in _rows("SELECT memory_id FROM memory_rings")} == {1, 2}
    assert cards[2]["tags"] == "散步" and cards[2]["starred"] == 1


def test_cli_trial_run_on_copy_and_compare_with_original(old_db, capsys):
    original = old_db / "original.db"
    shutil.copy(old_db / "old.db", original)
    copy = str(old_db / "old.db")
    assert migrate_rings.main(["x", copy, "--against", str(original)]) == 0
    out = capsys.readouterr().out
    assert '"moved": 6' in out and '"ok": true' in out
    result = migrate_rings.compare(str(original), copy)
    assert {k: result[k] for k in (
        "memories_before", "memories_after", "same_ids", "rings", "cards_with_one_ring",
        "first_ring_same_as_original", "content_unchanged", "other_fields_unchanged", "tag_pairs_before",
        "tag_pairs_after", "tags_lost", "anchors_before", "starred_after", "titles_after", "titles_not_from_topic",
    )} == {
        "memories_before": 6, "memories_after": 6, "same_ids": True, "rings": 6, "cards_with_one_ring": 6,
        "first_ring_same_as_original": 6, "content_unchanged": 6, "other_fields_unchanged": 6, "tag_pairs_before": 4,
        "tag_pairs_after": 8, "tags_lost": 0, "anchors_before": 2, "starred_after": 2, "titles_after": 2,
        "titles_not_from_topic": 0,
    }
    assert all(result["tables_unchanged"].values()) and len(result["sample"]) == 5
    assert result["integrity_check"] == "ok" and result["foreign_key_problems"] == 0 and result["ok"] is True
    # 再跑一遍：没有要搬的，核对照样通过
    assert migrate_rings.main(["x", copy, "--against", str(original)]) == 0
    assert "没有要搬的" in capsys.readouterr().out
    # 拷贝被动过手脚 → 核对不通过
    with get_conn() as conn:
        conn.execute("UPDATE memory_rings SET content = '编的：被改坏的' WHERE memory_id = 2")
    assert migrate_rings.compare(str(original), copy)["ok"] is False


def test_cli_refuses_bad_arguments(old_db, capsys):
    copy = str(old_db / "old.db")
    assert migrate_rings.main(["x"]) == 2
    assert migrate_rings.main(["x", copy, "--against"]) == 2
    missing = old_db / "typo.db"
    assert migrate_rings.main(["x", str(missing)]) == 2
    assert not missing.exists() and "找不到" in capsys.readouterr().err
    assert migrate_rings.main(["x", copy, "--against", copy]) == 2
    assert "同一个文件" in capsys.readouterr().err
    tables = {r["name"] for r in _rows("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "memory_rings" not in tables  # 参数不对时一点没动，连表都没建


def test_after_move_search_and_restore_still_work(old_db):
    init_db()
    assert [m["id"] for m in memories.search_memories("包饺子")] == [3]
    memories.restore_memory(5, by="xuan")
    assert memories.get_memory(5)["content"] == "编的：存重复了的赏樱"
    memories.update_memory(5, {"content": "编的：存重复了的赏樱（留着）"}, by="xuan")
    assert _rows("SELECT content FROM memory_rings WHERE memory_id = 5") == [{"content": "编的：存重复了的赏樱（留着）"}]


def _dump_raw(path: Path, table: str) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
    finally:
        conn.close()
