"""V6b：主动浮现（memory_briefing 三级漏斗）。① 层年轮卡 PR3 起是「没吃到的饼」。

纪律：sensitive 记忆永不进 briefing；①③ 层 3 天冷却、② 话题层不冷却；
superseded 剔除；只看 personal；总上限 8 条。语义腿不可用时 ② 退关键词。
"""

from datetime import date, timedelta

import pytest

from app import briefing, memories, settings
from app.db import get_conn, init_db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBER_DB", str(tmp_path / "test.db"))
    init_db()


def _save(content="x", date="2026-07-01", **kw) -> int:
    return memories.save_memory(date=date, content=content, **kw)["id"]


def _ids(result) -> list[int]:
    return [item["id"] for item in result["items"]]


def _future(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


# ---------- 三级漏斗 ----------


def test_uneaten_pie_surfaces_eaten_and_dough_do_not():
    """方案第七节：一张连理饼、一张连理饼 + 吃到了、一张面团，饼那一层只出第一张。"""
    pie = _save("编的：说好明年一起去看极光", tags="连理饼,期待")
    eaten = _save("编的：一起包了饺子", tags="连理饼,吃到了")
    dough = _save("编的：也许可以养只猫", tags="面团")
    _save("编的：正文里写着南瓜饼", tags="小吃")
    result = briefing.build_briefing()
    pies = [i["id"] for i in result["items"] if i["reason"] == briefing.REASON_PIE]
    assert pies == [pie]
    assert eaten not in pies
    # 面团哪一层都不递：最近层、话题层也不行；主动搜索照样找得到
    assert dough not in _ids(result) and dough not in _ids(briefing.build_briefing(topic="养只猫"))
    assert dough in [r["id"] for r in memories.search_memories("养只猫")]


def test_pies_newest_ring_first_and_count_is_a_setting():
    old = _save("编的：想学游泳", date="2026-03-01", tags="养成饼")
    new = _save("编的：想去海边住几天", date="2026-06-01", tags="远方饼")
    memories.add_ring(old, "2026-07-02", "编的：报了游泳课")  # 补了一圈 → 最新一圈更近，排到前面
    third = _save("编的：想把阳台种满", date="2026-05-01", tags="筑巢饼")
    pies = [i["id"] for i in briefing.build_briefing()["items"] if i["reason"] == briefing.REASON_PIE]
    assert pies == [old, new, third]

    # 设置管整份开场：饼不能换成「最近的事」「和眼下话题相关」再溜进来
    for n, want in ((1, [old]), (0, [])):
        settings.update_settings({"briefing_pies": n})
        for topic in (None, "想"):
            with get_conn() as conn:  # 每次都当第一次开场，不让上一次的冷却挡住
                conn.execute("DELETE FROM briefing_log")
            assert [i for i in _ids(briefing.build_briefing(topic=topic)) if i in (old, new, third)] == want


def test_interval_fields_no_longer_surface_as_unfinished():
    mid = _save("编的：以前的区间记忆", date="2026-01-01", start_date="2026-06-01", end_date=_future(30))
    for i in range(3):
        _save(f"编的：近来的事 {i}", date=f"2026-07-0{i + 2}")
    assert mid not in _ids(briefing.build_briefing())


def test_topic_resonance_via_keyword_fallback():
    hit = _save("六月去了趟海边看日出")
    _save("完全无关的一条")
    result = briefing.build_briefing(topic="海边")
    items = {i["id"]: i for i in result["items"]}
    assert items[hit]["reason"] == briefing.REASON_TOPIC


def test_no_topic_skips_resonance_layer():
    _save("海边的记忆")
    result = briefing.build_briefing()
    assert all(i["reason"] != briefing.REASON_TOPIC for i in result["items"])


def test_recent_layer_surfaces_new_memories():
    mid = _save("昨天入库的新记忆")
    result = briefing.build_briefing()
    items = {i["id"]: i for i in result["items"]}
    assert items[mid]["reason"] == briefing.REASON_RECENT


def test_recent_layer_sorts_by_event_date_not_created_at():
    """刚审完入库的四月旧事不算"最近"——按事件时间排，不按入库时间排。"""
    april = _save("四月的旧事", date="2026-04-19")  # 后审先发生
    july = _save("七月的新事", date="2026-07-08")
    fillers = [_save(f"六月的事{i}", date=f"2026-06-0{i + 1}") for i in range(3)]
    result = briefing.build_briefing()
    ids = _ids(result)
    assert july in ids and ids.index(july) < min(ids.index(f) for f in fillers if f in ids)
    # 超出 RECENT_CAP 时，四月的不该挤掉事件时间更近的
    assert april not in ids


# ---------- 剔除规则 ----------


def test_sensitive_never_surfaces_in_any_layer():
    s1 = _save("敏感的事", tags="连理饼,sensitive")
    s2 = _save("敏感的海边", tags="海边,sensitive")
    result = briefing.build_briefing(topic="海边 敏感")
    assert s1 not in _ids(result) and s2 not in _ids(result)
    # 搜索照常可达（sensitive 只挡 briefing，不是新机制）
    assert s2 in [r["id"] for r in memories.search_memories("海边")]


def test_superseded_never_surfaces():
    old = _save("旧结论", tags="筑巢饼")
    _save("新结论", links=[{"id": old, "relation": "supersedes"}])
    assert old not in _ids(briefing.build_briefing(topic="结论"))


def test_project_space_stays_out():
    proj = _save("ember 施工细节", space="ember", tags="筑巢饼")
    assert proj not in _ids(briefing.build_briefing(topic="ember 施工"))


def test_total_cap_is_eight():
    settings.update_settings({"briefing_pies": 8})
    for i in range(12):
        _save(f"记忆 {i}", tags="远方饼")
    assert briefing.build_briefing()["count"] <= briefing.TOTAL_CAP


# ---------- 冷却 ----------


def test_cooldown_blocks_repeat_within_three_days():
    mid = _save("还没吃到的饼", tags="连理饼")
    assert mid in _ids(briefing.build_briefing())
    assert mid not in _ids(briefing.build_briefing())  # 第二次开场不重复递


def test_cooldown_expires_after_three_days():
    mid = _save("还没吃到的饼", tags="连理饼")
    briefing.build_briefing()
    with get_conn() as conn:  # 把日志拨回 4 天前，模拟冷却过期
        conn.execute(
            "UPDATE briefing_log SET surfaced_at = datetime('now', '+8 hours', '-4 days')"
        )
    assert mid in _ids(briefing.build_briefing())


def test_topic_layer_ignores_cooldown():
    mid = _save("海边的约定", tags="远方饼")
    briefing.build_briefing()  # 以 ① 层身份递过一次，进了冷却
    result = briefing.build_briefing(topic="海边")
    items = {i["id"]: i for i in result["items"]}
    assert items[mid]["reason"] == briefing.REASON_TOPIC  # 话题相关照样到场


def test_topic_layer_does_not_write_cooldown():
    mid = _save("海边的记忆")
    briefing.build_briefing()  # 先以 ③ 层身份递一次 → 日志 1 行
    result = briefing.build_briefing(topic="海边")  # 冷却中，只能走 ② 层
    assert {i["reason"] for i in result["items"] if i["id"] == mid} == {briefing.REASON_TOPIC}
    with get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM briefing_log WHERE memory_id = ?", (mid,)
        ).fetchone()[0]
    assert n == 1  # ② 层递出没有新增日志行


# ---------- 置顶：贴了「开场」的卡 ----------


def test_pinned_card_comes_whole_every_time_until_tag_removed():
    """「我们」这类卡贴「开场」：不分抽屉、每一圈原样、每次都带、不进 items；便利贴拿掉就不带。"""
    long_ring = "编的：" + "很长的一圈" * 200
    us = _save(long_ring, date="2026-01-01", tags="开场", space="longterm")
    memories.add_ring(us, "2026-10-01", "编的：第二圈")
    other = _save("编的：日常的一件事，很长的一圈", date="2026-10-02")
    for _ in range(3):  # 冷却挡不住
        result = briefing.build_briefing(topic="很长的一圈")
        assert [c["id"] for c in result["pinned"]] == [us]
        assert [r["content"] for r in result["pinned"][0]["rings"]] == [long_ring, "编的：第二圈"]
        assert us not in _ids(result)
    assert other in _ids(result)  # 后面的照旧（话题层不冷却，次次都在）

    memories.update_memory(us, {"tags": ""})
    assert briefing.build_briefing()["pinned"] == []
