"""V6b：主动浮现（memory_briefing 三级漏斗）。

纪律：sensitive 记忆永不进 briefing；①③ 层 3 天冷却、② 话题层不冷却；
superseded 剔除；只看 personal；总上限 8 条。语义腿不可用时 ② 退关键词。
"""

from datetime import date, timedelta

import pytest

from app import briefing, memories
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


def test_unfinished_interval_surfaces_with_reason():
    ongoing = _save("朋友预产期临近", start_date="2026-06-01", end_date=_future(30))
    _save("早就结束的事", start_date="2026-01-01", end_date="2026-02-01")
    result = briefing.build_briefing()
    items = {i["id"]: i for i in result["items"]}
    assert items[ongoing]["reason"] == briefing.REASON_UNFINISHED
    # ended 的区间不算"没完的事"（它只可能以"近期"身份出现）
    ended_reasons = [i["reason"] for i in result["items"] if "结束" in i["content"]]
    assert briefing.REASON_UNFINISHED not in ended_reasons


def test_upcoming_counts_as_unfinished():
    upcoming = _save("下月的约定", start_date=_future(10))
    result = briefing.build_briefing()
    assert any(
        i["id"] == upcoming and i["reason"] == briefing.REASON_UNFINISHED
        for i in result["items"]
    )


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
    s1 = _save("敏感的事", tags="sensitive", start_date="2026-06-01")
    s2 = _save("敏感的海边", tags="海边,sensitive")
    result = briefing.build_briefing(topic="海边 敏感")
    assert s1 not in _ids(result) and s2 not in _ids(result)
    # 搜索照常可达（sensitive 只挡 briefing，不是新机制）
    assert s2 in [r["id"] for r in memories.search_memories("海边")]


def test_superseded_never_surfaces():
    old = _save("旧结论", start_date="2026-06-01")
    _save("新结论", links=[{"id": old, "relation": "supersedes"}])
    assert old not in _ids(briefing.build_briefing(topic="结论"))


def test_project_space_stays_out():
    proj = _save("ember 施工细节", space="ember", start_date="2026-06-01")
    assert proj not in _ids(briefing.build_briefing(topic="ember 施工"))


def test_total_cap_is_eight():
    for i in range(12):
        _save(f"记忆 {i}", start_date="2026-06-01")
    assert briefing.build_briefing()["count"] <= briefing.TOTAL_CAP


# ---------- 冷却 ----------


def test_cooldown_blocks_repeat_within_three_days():
    mid = _save("进行中的事", start_date="2026-06-01")
    assert mid in _ids(briefing.build_briefing())
    assert mid not in _ids(briefing.build_briefing())  # 第二次开场不重复递


def test_cooldown_expires_after_three_days():
    mid = _save("进行中的事", start_date="2026-06-01")
    briefing.build_briefing()
    with get_conn() as conn:  # 把日志拨回 4 天前，模拟冷却过期
        conn.execute(
            "UPDATE briefing_log SET surfaced_at = datetime('now', '+8 hours', '-4 days')"
        )
    assert mid in _ids(briefing.build_briefing())


def test_topic_layer_ignores_cooldown():
    mid = _save("海边的约定", start_date="2026-06-01")
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
