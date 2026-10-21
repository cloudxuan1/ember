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
    with pytest.raises(ValueError, match="一圈一圈改"):  # 好几圈的卡：圈才是正本，不让按整条改
        memories.update_memory(mid, {"content": "编的：整张卡的老写法改了一下"}, by="ke")
    with pytest.raises(ValueError, match="一圈一圈改"):
        memories.update_memory(mid, {"date": "2026-11-05"}, by="ke")
    assert _rings(mid) == before
    memories.update_memory(mid, {"tags": "看海"}, by="ke")  # 标签、标题照常能改
    assert _card(mid)["tags"] == "看海"


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


def test_title_is_editable_and_star_goes_through_its_own_switch():
    mid = memories.save_memory(date="2026-10-01", content="编的：有主题的一条", topic="主题", tier="anchor")["id"]
    memory = memories.get_memory(mid)
    assert memory["title"] == "主题" and memory["starred"] == 1
    with pytest.raises(ValueError):  # ⭐ 不走「改」（不进上一版），只能用 set_star
        memories.update_memory(mid, {"starred": 0}, by="xuan")
    result = memories.update_memory(mid, {"title": "  新标题 "}, by="xuan")
    assert result["changed"] == ["title"] and result["memory"]["title"] == "新标题"
    [version] = history.list_for(mid)
    assert version["title"] == "主题"
    memories.update_memory(mid, {"title": ""}, by="xuan")  # 清空 = 没写标题
    assert _card(mid)["title"] is None


def test_drawers_keep_personal_key():
    assert memories.DRAWER_LABELS == {"longterm": "长期", "personal": "日常"}
    mid = memories.save_memory(date="2026-10-01", content="编的：长期抽屉里的常识", space=memories.LONGTERM)["id"]
    assert _card(mid)["space"] == "longterm"
    assert memories.search_memories("常识", space="longterm")[0]["id"] == mid


# ---------- 补一圈 / 改某一圈 / 删某一圈 / ⭐（网页 PR4） ----------


@pytest.fixture
def today(monkeypatch):
    monkeypatch.setattr(memories, "_today", lambda: "2026-10-20")


def _live(memory_id: int) -> list[tuple[str, str]]:
    return [(r["date"], r["content"]) for r in memories.get_memory(memory_id)["rings"]]


def _acts() -> list[dict]:
    from app import activity
    return activity.list_activity()["items"]


def test_add_ring_updates_card_date_content_history_and_activity(today):
    mid = memories.save_memory(date="2026-10-02", content="编的：说好去海边住一晚", title="去海边", by="xuan")["id"]
    result = memories.add_ring(mid, "2026-10-16", "编的：存了两家民宿", by="xuan")
    assert _live(mid) == [("2026-10-02", "编的：说好去海边住一晚"), ("2026-10-16", "编的：存了两家民宿")]
    card = _card(mid)
    assert card["date"] == "2026-10-16"  # 卡片日期 = 最新一圈
    assert card["content"] == "2026-10-02：编的：说好去海边住一晚\n\n2026-10-16：编的：存了两家民宿"
    assert card["updated_at"] is not None and result["ring_id"] == _rings(mid)[-1]["id"]
    [version] = history.list_for(mid)
    assert version["rings"] == [{"id": _rings(mid)[0]["id"], "date": "2026-10-02", "content": "编的：说好去海边住一晚"}]
    act = _acts()[0]
    assert (act["action"], act["actor"], act["can"], act["title"]) == ("edit", "xuan", "revert", "去海边")


def test_ring_dates_cannot_be_in_the_future_and_content_cannot_be_empty(today):
    mid = memories.save_memory(date="2026-10-02", content="编的：一件事")["id"]
    for date in ("2026-10-21", "2026-11"):
        with pytest.raises(ValueError, match="以后的日子"):
            memories.add_ring(mid, date, "编的：还没发生")
    with pytest.raises(ValueError, match="写点什么"):
        memories.add_ring(mid, "2026-10-20", "   ")
    with pytest.raises(ValueError):
        memories.add_ring(mid, "最近", "编的：相对说法不行")
    assert memories.add_ring(mid, "2026-10", "编的：只记得是这个月")["memory"]["date"] == "2026-10-02"
    assert len(_live(mid)) == 2 and history.list_for(mid)  # 当月只到月可以写
    assert memories.add_ring(10**6, "2026-10-01", "编的：卡不在") is None


def test_edit_ring_and_unchanged_edit_records_nothing(today):
    mid = memories.save_memory(date="2026-09-06", content="编的：第一次做蛋糕，塌了")["id"]
    second = memories.add_ring(mid, "2026-09-27", "编的：第二次立住了")["ring_id"]
    versions = len(history.list_for(mid))
    same = memories.edit_ring(mid, second, "2026-09-27", " 编的：第二次立住了 ")
    assert same["changed"] is False and len(history.list_for(mid)) == versions
    memories.edit_ring(mid, second, "2026-10-18", "编的：第三次成功了，切开是漂亮的绿色")
    assert _live(mid)[-1] == ("2026-10-18", "编的：第三次成功了，切开是漂亮的绿色")
    assert _card(mid)["date"] == "2026-10-18"
    with pytest.raises(ValueError, match="改成空的"):
        memories.edit_ring(mid, second, "2026-10-18", "")
    other = memories.save_memory(date="2026-09-01", content="编的：别的卡")["id"]
    assert memories.edit_ring(other, second, "2026-09-02", "编的：圈不是这张卡的") is None


def test_delete_ring_keeps_the_last_one_and_revert_brings_it_back(today):
    mid = memories.save_memory(date="2026-08-03", content="编的：葱根泡在水里")["id"]
    second = memories.add_ring(mid, "2026-08-19", "编的：真的长出来了")["ring_id"]
    memories.delete_ring(mid, second, by="xuan")
    assert _live(mid) == [("2026-08-03", "编的：葱根泡在水里")]
    card = _card(mid)
    assert (card["date"], card["content"]) == ("2026-08-03", "编的：葱根泡在水里")
    [first] = memories.get_memory(mid)["rings"]
    with pytest.raises(ValueError, match="最后一圈"):
        memories.delete_ring(mid, first["id"])
    assert memories.delete_ring(mid, second) is None  # 删过的圈当不存在
    act = _acts()[0]
    memories.revert_memory(mid, act["history_id"], by="xuan")  # 最近动静里的「回退」
    assert _live(mid) == [("2026-08-03", "编的：葱根泡在水里"), ("2026-08-19", "编的：真的长出来了")]
    assert _card(mid)["date"] == "2026-08-19"


def test_revert_hides_rings_added_after_that_version_and_old_versions_without_rings_still_work(today):
    settings.update_settings({"history_keep": 2})
    mid = memories.save_memory(date="2026-08-12", content="编的：约好一起读一本书")["id"]
    memories.update_memory(mid, {"content": "编的：约好一起读《小王子》"}, by="xuan")
    with get_conn() as conn:  # 假装是 PR4 之前存的上一版：里面没有圈
        conn.execute("UPDATE memory_history SET snapshot = json_remove(snapshot, '$.rings') WHERE memory_id = ?", (mid,))
    memories.add_ring(mid, "2026-09-14", "编的：读完了")
    old = history.list_for(mid)[-1]
    assert "rings" not in old and old["content"] == "编的：约好一起读一本书"
    memories.revert_memory(mid, old["id"], by="xuan")
    assert _live(mid) == [("2026-08-12", "编的：约好一起读一本书")]
    assert _card(mid)["content"] == "编的：约好一起读一本书"
    newest = history.list_for(mid)[0]  # 回退前的样子（两圈）进了上一版，再退回去两圈都回来
    memories.revert_memory(mid, newest["id"], by="xuan")
    assert _live(mid) == [("2026-08-12", "编的：约好一起读《小王子》"), ("2026-09-14", "编的：读完了")]


def test_ring_change_waits_for_nobody_and_refuses_when_card_changed_meanwhile(today):
    """网页打开卡之后小克又改了：补圈 / 改圈 / 删圈都不覆盖，让轩刷新再看。"""
    mid = memories.save_memory(date="2026-10-01", content="编的：一件事")["id"]
    seen = memories.get_memory(mid)["updated_at"]
    memories.update_memory(mid, {"tags": "小克刚贴的"}, by="ke")
    with pytest.raises(memories.Conflict):
        memories.add_ring(mid, "2026-10-02", "编的：轩写的新进展", expect_updated_at=seen)
    assert len(_live(mid)) == 1
    fresh = memories.get_memory(mid)["updated_at"]
    ring_id = memories.add_ring(mid, "2026-10-02", "编的：轩写的新进展", expect_updated_at=fresh)["ring_id"]
    with pytest.raises(memories.Conflict):
        memories.delete_ring(mid, ring_id, expect_updated_at=fresh)
    with pytest.raises(memories.Conflict):
        memories.edit_ring(mid, ring_id, "2026-10-03", "编的：改", expect_updated_at=fresh)


def test_ring_change_survives_embedding_failure(today, monkeypatch):
    from app import embeddings
    monkeypatch.setenv("EMBEDDING_API_KEY", "test-key")
    monkeypatch.setattr(embeddings, "embed_texts", lambda texts, timeout=None: [[1.0, 0.0] for _ in texts])
    mid = memories.save_memory(date="2026-10-01", content="编的：会算指纹的一条")["id"]

    def boom(texts, timeout=None):
        raise RuntimeError("embedding API down")

    monkeypatch.setattr(embeddings, "embed_texts", boom)
    memories.add_ring(mid, "2026-10-05", "编的：指纹服务坏了也照样补上")
    assert _live(mid)[-1] == ("2026-10-05", "编的：指纹服务坏了也照样补上")
    with get_conn() as conn:  # 旧指纹按改前的意思，删掉等补算
        assert conn.execute("SELECT COUNT(*) FROM memory_embeddings WHERE memory_id = ?", (mid,)).fetchone()[0] == 0


def test_star_is_a_bookmark_not_an_edit():
    mid = memories.save_memory(date="2026-10-01", content="编的：喜欢的天气")["id"]
    assert memories.set_star(mid, True, by="xuan") == {"id": mid, "starred": True}
    card = _card(mid)
    assert card["starred"] == 1 and card["updated_at"] is None  # 不算改过
    assert history.list_for(mid) == [] and [a["action"] for a in _acts()] == ["save"]
    settings.update_settings({"ke_can_star": False})
    with pytest.raises(ValueError, match="小克能不能点"):
        memories.set_star(mid, False, by="ke")
    assert memories.set_star(mid, False, by="xuan")["starred"] is False
    with pytest.raises(ValueError):
        memories.set_star(mid, 1, by="xuan")
    memories.delete_memory(mid)
    assert memories.set_star(mid, True) is None
