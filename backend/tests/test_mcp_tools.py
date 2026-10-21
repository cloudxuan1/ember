"""MCP 记忆工具（单库）：存了就进库、改了立刻生效（旧的留作上一版）、删了先藏起来；
日期必须是具体日子或只到月；小克删除受设置开关管。年轮卡 PR3：补一圈、按圈改 / 删、老参数兼容。"""

import pytest

from app import activity, mcp_server, memories, settings
from app.db import get_conn, init_db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBER_DB", str(tmp_path / "test.db"))
    init_db()


def _actors() -> list[tuple[str, str]]:
    with get_conn() as conn:
        return [(r["actor"], r["action"]) for r in conn.execute("SELECT actor, action FROM activity_log ORDER BY id")]


def test_docstrings_tell_model_the_rules():
    save_doc = mcp_server.memory_save.__doc__
    assert "直接进库" in save_doc and "不许写" in save_doc and "最近" in save_doc and "下周" in save_doc
    assert "YYYY-MM" in save_doc
    assert "立刻生效" in mcp_server.memory_edit.__doc__ and "整段替换" in mcp_server.memory_edit.__doc__
    assert "恢复" in mcp_server.memory_delete.__doc__
    assert "date_from" in mcp_server.memory_search.__doc__
    assert not hasattr(mcp_server, "memory_queue")


def test_save_goes_straight_in_and_is_searchable():
    result = mcp_server.memory_save(date="2026-10-17", content="一起看 Mind Your Language", tags="英剧，笑场")
    assert result["status"] == "saved" and isinstance(result["id"], int)
    hit = mcp_server.memory_search("Mind Your Language")["results"][0]
    assert hit["id"] == result["id"] and hit["date"] == "2026-10-17"
    assert memories.get_memory(result["id"])["tags"] == "英剧,笑场"
    assert _actors() == [("ke", "save")]


def test_save_requires_content_and_concrete_date():
    for bad in ("最近", "下周", "", "2026-13", "2026-02-30", "10-17"):
        assert "error" in mcp_server.memory_save(date=bad, content="x"), bad
    assert "error" in mcp_server.memory_save(date="2026-10-17", content="  ")
    month = mcp_server.memory_save(date="2026-7", content="只记得是七月")
    assert month["date"] == "2026-07"
    assert memories.get_status()["memories"] == 1


def test_save_takes_card_fields_and_ignores_old_params():
    target = memories.save_memory(date="2026-07-01", content="已有记忆")["id"]
    result = mcp_server.memory_save(
        date="2026-07-20", content="完整字段", title="一起做的项目", tags="项目，约定", starred=True, space="longterm",
        tier="anchor", topic="MCP", start_date="2026-07-15", end_date="2026-07-31",
        links=[{"id": target, "relation": "led_to", "dir": "in"}],
    )
    assert result["status"] == "saved" and result["date"] == "2026-07-20"
    m = memories.get_memory(result["id"])
    assert {k: m[k] for k in ("date", "content", "title", "tags", "starred", "space")} == {
        "date": "2026-07-20", "content": "完整字段", "title": "一起做的项目", "tags": "项目,约定",
        "starred": 1, "space": "longterm",
    }
    # 老参数传了不报错、直接忽略：不变成标题 / 便利贴，不连边，不写区间
    assert (m["topic"], m["tier"], m["start_date"], m["end_date"], m["edges"]) == ("", "normal", None, None, [])
    assert [(r["date"], r["content"]) for r in m["rings"]] == [("2026-07-20", "完整字段")]


def test_save_rejects_future_day_and_star_switch_only_skips_the_star():
    assert "以后" in mcp_server.memory_save(date="2099-01-01", content="计划")["error"]
    assert "以后" in mcp_server.memory_save(date="2099-01", content="计划")["error"]
    settings.update_settings({"ke_can_star": False})
    result = mcp_server.memory_save(date="2026-10-01", content="想点星", starred=True)
    assert result["status"] == "saved" and "⭐没点上" in result["message"]
    assert memories.get_memory(result["id"])["starred"] == 0


def test_add_ring_goes_into_previous_version_and_activity():
    mid = mcp_server.memory_save(date="2026-09-01", content="编的：打算学游泳", tags="养成饼")["id"]
    result = mcp_server.memory_add_ring(memory_id=mid, date="2026-10-02", content="编的：报了游泳课")
    assert result["status"] == "added" and result["date"] == "2026-10-02" and "2 圈" in result["message"]
    m = memories.get_memory(mid)
    assert [r["content"] for r in m["rings"]] == ["编的：打算学游泳", "编的：报了游泳课"]
    assert m["rings"][1]["id"] == result["ring_id"]
    with get_conn() as conn:
        snap = conn.execute("SELECT snapshot, replaced_by FROM memory_history WHERE memory_id = ?", (mid,)).fetchone()
    assert "报了游泳课" not in snap["snapshot"] and snap["replaced_by"] == "ke"
    assert _actors() == [("ke", "save"), ("ke", "edit")]
    assert "以后" in mcp_server.memory_add_ring(memory_id=mid, date="2099-01-01", content="x")["error"]
    assert "error" in mcp_server.memory_add_ring(memory_id=mid, date="最近", content="x")
    assert "error" in mcp_server.memory_add_ring(memory_id=mid, date="2026-10-03", content="  ")
    assert "error" in mcp_server.memory_add_ring(memory_id=9999, date="2026-10-03", content="x")


def test_edit_reads_whole_card_then_edits_the_only_ring_in_place():
    mid = memories.save_memory(date="2026-09-20", content="原文", tags="日常", title="晚饭")["id"]
    read = mcp_server.memory_edit(memory_id=mid)["memory"]
    assert (read["title"], read["tags"], read["date"]) == ("晚饭", "日常", "2026-09-20")
    assert [(r["date"], r["content"]) for r in read["rings"]] == [("2026-09-20", "原文")]
    assert "tier" not in read and "topic" not in read and "edges" not in read
    result = mcp_server.memory_edit(memory_id=mid, content="原文。原话：不想吃那个", date="2026-09")
    assert result["status"] == "updated" and result["changed"] == ["ring"]
    assert result["target"] == {"date": "2026-09", "preview": "晚饭"}
    m = memories.get_memory(mid)
    assert m["content"] == "原文。原话：不想吃那个" and m["date"] == "2026-09"  # 立刻生效
    assert [(r["date"], r["content"]) for r in m["rings"]] == [("2026-09", "原文。原话：不想吃那个")]
    with get_conn() as conn:
        snap = conn.execute("SELECT snapshot, replaced_by FROM memory_history WHERE memory_id = ?", (mid,)).fetchone()
    assert '"原文"' in snap["snapshot"] and snap["replaced_by"] == "ke"
    assert _actors() == [("ke", "save"), ("ke", "edit")]


def test_edit_blank_header_fields_mean_no_change_and_unchanged_is_reported():
    mid = memories.save_memory(date="2026-09-20", content="原文", tags="日常,sensitive", title="晚饭")["id"]
    same = mcp_server.memory_edit(memory_id=mid, content=" 原文 ", tags="", title="  ", space=" ", topic="改主题")
    assert same["status"] == "unchanged"
    m = memories.get_memory(mid)
    assert (m["tags"], m["title"], m["space"], m["topic"]) == ("日常,sensitive", "晚饭", "personal", "")
    assert "error" in mcp_server.memory_edit(memory_id=mid, content="  ")
    assert "error" in mcp_server.memory_edit(memory_id=mid, date="下周")
    assert "以后" in mcp_server.memory_edit(memory_id=mid, date="2099-01-01")["error"]
    assert "error" in mcp_server.memory_edit(memory_id=9999, content="x")
    assert _actors() == [("ke", "save")]  # 没变化不记动静、不留上一版


def test_edit_on_many_rings_needs_ring_id_and_header_plus_ring_is_one_version():
    settings.update_settings({"history_keep": 2})  # 留 2 版才数得出这次改动进了几版
    mid = memories.save_memory(date="2026-09-01", content="第一圈", tags="散心")["id"]
    second = memories.add_ring(mid, "2026-10-01", "第二圈")["ring_id"]
    err = mcp_server.memory_edit(memory_id=mid, content="改哪圈？")["error"]
    assert "2 圈" in err and f"#{second}" in err and "ring_id" in err
    assert "error" in mcp_server.memory_edit(memory_id=mid, ring_id=99999, content="不存在的圈")

    with get_conn() as conn:
        before = conn.execute("SELECT COUNT(*) FROM memory_history WHERE memory_id = ?", (mid,)).fetchone()[0]
    result = mcp_server.memory_edit(memory_id=mid, ring_id=second, content="第二圈。补一句原话", title="出门走走", tags="散心,开心")
    assert result["status"] == "updated" and set(result["changed"]) == {"title", "tags", "ring"}
    m = memories.get_memory(mid)
    assert (m["title"], m["tags"]) == ("出门走走", "散心,开心")
    assert [r["content"] for r in m["rings"]] == ["第一圈", "第二圈。补一句原话"]
    with get_conn() as conn:
        rows = conn.execute("SELECT id, snapshot FROM memory_history WHERE memory_id = ? ORDER BY id", (mid,)).fetchall()
    assert len(rows) == before + 1  # 卡头和那一圈一起改只进一版：只留 1 版时也能一次退回去
    assert '"第二圈"' in rows[-1]["snapshot"] and '"出门走走"' not in rows[-1]["snapshot"]
    memories.revert_memory(mid, rows[-1]["id"])  # 一次回退：卡头和那一圈都回到改之前
    m = memories.get_memory(mid)
    assert (m["title"], m["tags"]) == (None, "散心")
    assert [r["content"] for r in m["rings"]] == ["第一圈", "第二圈"]

    only_date = mcp_server.memory_edit(memory_id=mid, ring_id=second, date="2026-10")
    assert only_date["changed"] == ["ring"]
    assert [(r["date"], r["content"]) for r in memories.get_memory(mid)["rings"]][-1] == ("2026-10", "第二圈")


def test_edit_moves_drawer_and_stars_with_switch():
    mid = memories.save_memory(date="2026-09-20", content="轩喜欢下雨天")["id"]
    moved = mcp_server.memory_edit(memory_id=mid, space="longterm", starred=True)
    assert set(moved["changed"]) == {"space", "starred"}
    m = memories.get_memory(mid)
    assert (m["space"], m["starred"]) == ("longterm", 1)
    settings.update_settings({"ke_can_star": False})
    assert "关掉" in mcp_server.memory_edit(memory_id=mid, starred=False)["error"]
    partly = mcp_server.memory_edit(memory_id=mid, title="天气", starred=False)
    assert partly["changed"] == ["title"] and "⭐没改上" in partly["message"]
    assert memories.get_memory(mid)["starred"] == 1
    assert mcp_server.memory_edit(memory_id=mid, starred=True)["status"] == "unchanged"  # 已经是⭐，不用开关


def test_delete_one_ring_respects_switch_and_can_be_reverted():
    mid = memories.save_memory(date="2026-09-01", content="第一圈")["id"]
    second = memories.add_ring(mid, "2026-10-01", "第二圈")["ring_id"]
    settings.update_settings({"ke_can_delete": False})
    assert "关掉" in mcp_server.memory_delete(memory_id=mid, ring_id=second)["error"]
    settings.update_settings({"ke_can_delete": True})
    assert "error" in mcp_server.memory_delete(memory_id=mid, ring_id=99999)
    result = mcp_server.memory_delete(memory_id=mid, ring_id=second)
    assert result["status"] == "ring_deleted" and "剩 1 圈" in result["message"]
    assert [r["content"] for r in memories.get_memory(mid)["rings"]] == ["第一圈"]
    first = memories.get_memory(mid)["rings"][0]["id"]
    assert "最后一圈" in mcp_server.memory_delete(memory_id=mid, ring_id=first)["error"]
    with get_conn() as conn:
        hid = conn.execute("SELECT MAX(id) FROM memory_history WHERE memory_id = ?", (mid,)).fetchone()[0]
    memories.revert_memory(mid, hid)
    assert [r["content"] for r in memories.get_memory(mid)["rings"]] == ["第一圈", "第二圈"]


def test_recall_lists_rings_in_order_with_sources():
    mid = memories.save_memory(
        date="2026-08", content="第一圈", title="学游泳", tags="养成饼", tier="anchor", topic="旧主题",
        source_ref="编的日志.md#八月", quote="编的原话",
    )["id"]
    memories.add_ring(mid, "2026-09-15", "第二圈")
    memories.add_ring(mid, "2026-08-20", "补在中间的一圈")
    card = mcp_server.memory_recall(id=mid)
    assert [(r["date"], r["content"]) for r in card["rings"]] == [
        ("2026-08", "第一圈"), ("2026-08-20", "补在中间的一圈"), ("2026-09-15", "第二圈"),
    ]
    assert card["sources"] == [{"source_ref": "编的日志.md#八月", "quote": "编的原话"}]
    assert (card["title"], card["drawer"], card["date"]) == ("学游泳", "日常", "2026-09-15")
    assert not {"tier", "topic", "edges", "start_date", "content"} & set(card)


def test_search_list_briefing_drop_old_fields():
    memories.save_memory(date="2026-10-01", content="编的：一起去海边", tags="远方饼", start_date="2026-10-01")
    for out in (mcp_server.memory_search("海边")["results"], mcp_server.memory_list()["items"],
                mcp_server.memory_briefing()["items"]):
        assert out and not {"tier", "topic", "start_date", "end_date", "interval_status"} & set(out[0])
    assert "by_tier" not in mcp_server.memory_list()["stats"]


def test_status_reports_ring_fingerprints():
    assert mcp_server.memory_status()["ring_fingerprints"] == {"enabled": False, "note": "按意思搜没开（没配 embedding），只按关键词搜"}


def test_delete_hides_and_respects_switch():
    mid = memories.save_memory(date="2026-09-20", content="存重复了")["id"]
    settings.update_settings({"ke_can_delete": False})
    assert "关掉" in mcp_server.memory_delete(memory_id=mid)["error"]
    assert memories.get_memory(mid) is not None
    settings.update_settings({"ke_can_delete": True})
    result = mcp_server.memory_delete(memory_id=mid)
    assert result["status"] == "deleted" and "30 天" in result["message"]
    assert "error" in mcp_server.memory_recall(id=mid)
    assert mcp_server.memory_search("存重复了")["count"] == 0
    assert mcp_server.memory_list()["stats"]["total"] == 0
    assert mcp_server.memory_status()["memories"] == 0
    assert "error" in mcp_server.memory_delete(memory_id=mid)  # 删过了
    assert "error" in mcp_server.memory_edit(memory_id=mid, content="改删掉的")
    assert _actors()[-1] == ("ke", "delete")


def test_search_and_list_date_ranges():
    ids = {d: memories.save_memory(date=d, content=f"旅行 {d}")["id"] for d in ("2026-07-31", "2026-08", "2026-08-15", "2026-09-01")}
    aug = mcp_server.memory_search("旅行", date_from="2026-08", date_to="2026-08")
    assert {r["id"] for r in aug["results"]} == {ids["2026-08"], ids["2026-08-15"]}
    since = mcp_server.memory_search("旅行", date_from="2026-08-10")  # 只到 8 月的那条也可能在 10 号之后
    assert {r["id"] for r in since["results"]} == {ids["2026-08"], ids["2026-08-15"], ids["2026-09-01"]}
    first_day = mcp_server.memory_search("旅行", date_from="2026-08-01", date_to="2026-08-31")
    assert {r["id"] for r in first_day["results"]} == {ids["2026-08"], ids["2026-08-15"]}
    year = mcp_server.memory_list(date_from="2026", date_to="2026")
    assert year["stats"]["total"] == 4
    assert "error" in mcp_server.memory_search("旅行", date_from="上个月")
    assert "error" in mcp_server.memory_list(date_to="最近")


def test_deleted_memory_edges_hidden_from_recall_and_briefing():
    a = memories.save_memory(date="2026-10-01", content="留着的")["id"]
    b = memories.save_memory(date="2026-10-02", content="要删的", links=[{"id": a, "relation": "related"}])["id"]
    memories.delete_memory(b, by="ke")
    assert memories.get_memory(a)["edges"] == []
    assert all(i["id"] != b for i in mcp_server.memory_briefing()["items"])
    assert activity.list_activity()["items"][0]["can"] == "restore"


def test_dates_accept_chinese_and_fullwidth_input():
    for raw, stored in (("２０２６－０７", "2026-07"), ("2026年7月18日", "2026-07-18"), ("2026/7/5", "2026-07-05")):
        assert mcp_server.memory_save(date=raw, content=f"日期 {raw}")["date"] == stored


def test_month_only_interval_ends_at_month_end():
    assert memories.interval_status(None, "2026-10", today="2026-10-18") == "ongoing"
    assert memories.interval_status(None, "2026-09", today="2026-10-18") == "ended"
    assert memories.interval_status("2026-10", None, today="2026-10-01") == "ongoing"
    assert memories.interval_status("2026-11", None, today="2026-10-18") == "upcoming"


def test_dates_with_spaces_around_chinese_units():
    assert memories.normalize_date("2026 年 7 月") == "2026-07"
    assert memories.normalize_date("2026 年 7 月 18 日") == "2026-07-18"
