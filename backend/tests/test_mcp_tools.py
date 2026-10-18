"""MCP 记忆工具（单库）：存了就进库、改了立刻生效（旧的留作上一版）、删了先藏起来；
日期必须是具体日子或只到月；小克删除受设置开关管。"""

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


def test_save_maps_all_fields_and_links():
    target = memories.save_memory(date="2026-07-01", content="已有记忆")["id"]
    result = mcp_server.memory_save(
        date="2026-07-20", content="完整字段", tags="项目，约定", tier="anchor", topic="MCP",
        space="ember", start_date="2026-07-15", end_date="2026-07-31",
        links=[{"id": target, "relation": "led_to", "dir": "in"}],
    )
    m = memories.get_memory(result["id"])
    assert {k: m[k] for k in ("date", "content", "tags", "tier", "topic", "space", "start_date", "end_date")} == {
        "date": "2026-07-20", "content": "完整字段", "tags": "项目,约定", "tier": "anchor", "topic": "MCP",
        "space": "ember", "start_date": "2026-07-15", "end_date": "2026-07-31",
    }
    assert result["edges_written"] == 1
    assert (m["edges"][0]["from_id"], m["edges"][0]["to_id"]) == (target, result["id"])


def test_edit_reads_then_writes_in_place_with_previous_version():
    mid = memories.save_memory(date="2026-09-20", content="原文", tags="日常", topic="晚饭")["id"]
    read = mcp_server.memory_edit(memory_id=mid)
    assert read == {"memory_id": mid, "date": "2026-09-20", "content": "原文", "tags": "日常", "topic": "晚饭"}
    result = mcp_server.memory_edit(memory_id=mid, content="原文。原话：不想吃那个", date="2026-09")
    assert result["status"] == "updated" and set(result["changed"]) == {"content", "date"}
    assert result["target"] == {"date": "2026-09", "preview": "原文。原话：不想吃那个"}
    assert memories.get_memory(mid)["content"] == "原文。原话：不想吃那个"  # 立刻生效
    with get_conn() as conn:
        snap = conn.execute("SELECT snapshot, replaced_by FROM memory_history WHERE memory_id = ?", (mid,)).fetchone()
    assert '"原文"' in snap["snapshot"] and snap["replaced_by"] == "ke"
    assert _actors() == [("ke", "save"), ("ke", "edit")]


def test_edit_blank_tags_topic_mean_no_change_and_unchanged_is_reported():
    mid = memories.save_memory(date="2026-09-20", content="原文", tags="日常,sensitive", topic="晚饭")["id"]
    same = mcp_server.memory_edit(memory_id=mid, content=" 原文 ", tags="", topic="  ")
    assert same["status"] == "unchanged"
    assert memories.get_memory(mid)["tags"] == "日常,sensitive"
    assert "error" in mcp_server.memory_edit(memory_id=mid, content="  ")
    assert "error" in mcp_server.memory_edit(memory_id=mid, date="下周")
    assert "error" in mcp_server.memory_edit(memory_id=9999, content="x")
    assert _actors() == [("ke", "save")]  # 没变化不记动静、不留上一版


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
