"""年轮卡（app/rings.py）：存的时候建第一圈，改 / 回退时这一圈跟着卡走，卡片日期 = 最新一圈的日期。
测试数据全是编的。"""

import pytest

from app import history, memories, rings, settings
from app.db import get_conn, init_db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBER_DB", str(tmp_path / "test.db"))
    init_db()


def _rings(memory_id: int) -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM memory_rings WHERE memory_id = ? ORDER BY id", (memory_id,)
        ).fetchall()]


def _card(memory_id: int) -> dict:
    with get_conn() as conn:
        return dict(conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone())


def test_fresh_database_has_rings_table_and_new_columns():
    with get_conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(memories)")}
        ring_cols = {r["name"] for r in conn.execute("PRAGMA table_info(memory_rings)")}
    assert {"title", "starred"} <= cols
    assert ring_cols == {"id", "memory_id", "date", "content", "created_at", "updated_at", "deleted_at"}


def test_save_creates_first_ring_with_same_content_and_date():
    mid = memories.save_memory(date="2026-10", content="编的：十月学会了做蛋挞", tags="手艺")["id"]
    card = _card(mid)
    [ring] = _rings(mid)
    assert (ring["date"], ring["content"]) == ("2026-10", "编的：十月学会了做蛋挞")
    assert ring["created_at"] == card["created_at"] and ring["updated_at"] is None and ring["deleted_at"] is None
    assert card["title"] is None and card["starred"] == 0


def test_save_turns_old_topic_into_title_or_shared_tag():
    first = memories.save_memory(date="2026-10-01", content="编的：第一次去爬山", topic="爬山")["id"]
    second = memories.save_memory(date="2026-10-08", content="编的：又去爬山", tags="周末", topic="爬山")["id"]
    assert _card(first)["title"] == "爬山"  # 存的时候只有它一张在用
    assert _card(second)["title"] is None and _card(second)["tags"] == "周末,爬山"  # 第二张起并进便利贴
    third = memories.save_memory(date="2026-10-09", content="编的：爬山回来", tags="爬山", topic="爬山")["id"]
    assert _card(third)["tags"] == "爬山"  # 已有就不加
    assert _card(first)["topic"] == "爬山"  # 老栏位原样留着


def test_anchor_becomes_star_unless_ke_is_not_allowed():
    by_ke = memories.save_memory(date="2026-10-01", content="编的：要紧的一件事", tier="anchor")["id"]
    assert _card(by_ke)["starred"] == 1
    settings.update_settings({"ke_can_star": False})
    blocked = memories.save_memory(date="2026-10-02", content="编的：又一件要紧的事", tier="anchor")["id"]
    assert _card(blocked)["starred"] == 0 and _card(blocked)["tier"] == "anchor"
    by_xuan = memories.save_memory(date="2026-10-03", content="编的：轩自己存的", tier="anchor", by="xuan")["id"]
    assert _card(by_xuan)["starred"] == 1
    normal = memories.save_memory(date="2026-10-04", content="编的：平常的一件事")["id"]
    assert _card(normal)["starred"] == 0


def test_edit_and_revert_keep_the_only_ring_in_step():
    mid = memories.save_memory(date="2026-10-01", content="编的：计划周末去海边")["id"]
    memories.update_memory(mid, {"content": "编的：计划周末去海边，带上风筝", "date": "2026-10-02"}, by="ke")
    [ring] = _rings(mid)
    assert (ring["date"], ring["content"]) == ("2026-10-02", "编的：计划周末去海边，带上风筝")
    assert ring["updated_at"] is not None
    [version] = history.list_for(mid)
    memories.revert_memory(mid, version["id"], by="xuan")
    [ring] = _rings(mid)
    assert (ring["date"], ring["content"]) == ("2026-10-01", "编的：计划周末去海边")
    memories.update_memory(mid, {"tags": "海边"}, by="xuan")
    assert _rings(mid) == [ring]  # 只改标签，圈不用动


def test_tidying_old_data_also_tidies_the_ring():
    """老数据（日期没补零、内容前后有空白）被顺手存成规整的样子时，圈也跟着规整。"""
    mid = memories.save_memory(date="2026-08-05", content="编的：老数据")["id"]
    with get_conn() as conn:
        conn.execute("UPDATE memories SET date = '2026-8-5', content = ' 编的：老数据 ' WHERE id = ?", (mid,))
        conn.execute("UPDATE memory_rings SET date = '2026-8-5', content = ' 编的：老数据 ' WHERE memory_id = ?", (mid,))
    result = memories.update_memory(mid, {"date": "2026-08-05", "content": "编的：老数据"}, by="xuan")
    assert result["changed"] == []
    [ring] = _rings(mid)
    assert (ring["date"], ring["content"]) == ("2026-08-05", "编的：老数据")


def test_card_with_several_rings_is_left_alone_and_card_date_is_latest_ring():
    mid = memories.save_memory(date="2026-10-20", content="编的：说好要一起去看海")["id"]
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO memory_rings (memory_id, date, content, deleted_at) VALUES (?, ?, ?, ?)",
            [(mid, "2026-10", "编的：十月某天去了", None),       # 只到月：排在 10 月具体日子前面
             (mid, "2026-11-02", "编的：被删掉的一圈", "2026-11-03 10:00:00"),
             (mid, "2026-10-20", "编的：同一天后补的一圈", None)],
        )
        assert rings.sync_card_date(conn, mid) == "2026-10-20"
        conn.execute("UPDATE memory_rings SET deleted_at = NULL WHERE date = '2026-11-02'")
        assert rings.sync_card_date(conn, mid) == "2026-11-02"
    assert _card(mid)["date"] == "2026-11-02"
    before = _rings(mid)
    memories.update_memory(mid, {"content": "编的：整张卡的老写法改了一下"}, by="ke")
    assert _rings(mid) == before  # 好几圈的卡：圈才是正本，老写法不去动圈


def test_sync_card_date_pads_old_dates_and_ignores_cards_without_live_rings():
    mid = memories.save_memory(date="2026-08-12", content="编的：八月")["id"]
    with get_conn() as conn:
        conn.execute("INSERT INTO memory_rings (memory_id, date, content) VALUES (?, '2026-8-5', '编的：老格式')", (mid,))
        assert rings.sync_card_date(conn, mid) == "2026-08-12"  # 补零后 08-05 比 08-12 早
        conn.execute("UPDATE memory_rings SET deleted_at = '2026-10-01 00:00:00' WHERE memory_id = ?", (mid,))
        assert rings.sync_card_date(conn, mid) is None
    assert _card(mid)["date"] == "2026-08-12"


def test_delete_restore_keep_rings_and_purge_removes_them():
    mid = memories.save_memory(date="2026-10-01", content="编的：要删的一条")["id"]
    memories.delete_memory(mid, by="xuan")
    assert len(_rings(mid)) == 1 and _rings(mid)[0]["deleted_at"] is None  # 删整张卡不是删圈
    memories.restore_memory(mid, by="xuan")
    assert len(_rings(mid)) == 1
    memories.delete_memory(mid, by="xuan")
    with get_conn() as conn:  # 假装 31 天前删的
        conn.execute("UPDATE memories SET deleted_at = datetime('now','+8 hours','-31 days') WHERE id = ?", (mid,))
    assert memories.purge_expired()["purged"] == [mid]
    assert _rings(mid) == []


def test_title_and_star_are_shown_but_not_editable_yet():
    mid = memories.save_memory(date="2026-10-01", content="编的：有主题的一条", topic="主题", tier="anchor")["id"]
    memory = memories.get_memory(mid)
    assert memory["title"] == "主题" and memory["starred"] == 1
    with pytest.raises(ValueError):  # 改标题 / 点⭐ 的入口在 PR3 / PR4
        memories.update_memory(mid, {"title": "新标题", "starred": 0}, by="xuan")


def test_drawers_keep_personal_key():
    assert memories.DRAWER_LABELS == {"longterm": "长期", "personal": "日常"}
    mid = memories.save_memory(date="2026-10-01", content="编的：长期抽屉里的常识", space=memories.LONGTERM)["id"]
    assert _card(mid)["space"] == "longterm"
    assert memories.search_memories("常识", space="longterm")[0]["id"] == mid
