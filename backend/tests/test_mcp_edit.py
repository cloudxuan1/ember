"""MCP memory_edit：改正式记忆只生成提议（AI 绕不过轩），改待审草稿直接生效；
编号分两套（memory_id / draft_id），只给编号 = 先读。"""

import pytest

from app import drafts, mcp_server, memories, memory_edits
from app.db import init_db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBER_DB", str(tmp_path / "test.db"))
    init_db()


def _mem(content="原文", tags="日常", topic="晚饭") -> int:
    return memories.save_memory(date="2026-09-20", content=content, tags=tags, topic=topic)["id"]


def _pending_draft(content="草稿原文", tags="日常,sensitive", topic="晚饭") -> int:
    return drafts.save_draft(date="2026-09-20", content=content, tags=tags, topic=topic, quote="原话片段")["id"]


def test_docstring_tells_model_the_rules():
    doc = mcp_server.memory_edit.__doc__
    assert "不会直接改正式记忆" in doc
    assert "整段替换" in doc and "supersedes" in doc and "draft_id" in doc


def test_needs_exactly_one_id():
    assert "error" in mcp_server.memory_edit(content="x")
    assert "error" in mcp_server.memory_edit(memory_id=1, draft_id=1, content="x")


def test_memory_edit_only_proposes():
    mid = _mem()
    result = mcp_server.memory_edit(memory_id=mid, content="原文。原话：不想吃那个", reason="补原话")
    assert result["status"] == "pending_review"
    assert result["target"]["preview"] == "原文" and "等轩在审核台确认" in result["message"]
    assert memories.get_memory(mid)["content"] == "原文"  # 正式记忆一个字没变
    card = memory_edits.list_edits()["items"][0]
    assert card["memory_id"] == mid and card["reason"] == "补原话"


def test_read_memory_shows_pending_edit():
    mid = _mem()
    mcp_server.memory_edit(memory_id=mid, topic="晚饭纠结")
    read = mcp_server.memory_edit(memory_id=mid)
    assert read["content"] == "原文" and read["pending_edit"]["topic"] == "晚饭纠结"


def test_merge_reports_replaced_value():
    mid = _mem()
    mcp_server.memory_edit(memory_id=mid, content="版本一")
    second = mcp_server.memory_edit(memory_id=mid, content="版本二")
    assert second["replaced"] == {"content": "版本一"} and "合并" in second["message"]


def test_unchanged_and_blank_inputs():
    mid = _mem()
    assert mcp_server.memory_edit(memory_id=mid, content=" 原文 ")["status"] == "unchanged"
    assert "error" in mcp_server.memory_edit(memory_id=mid, content="   ")
    # tags/topic 传空 = 不改：只给空值等于只给编号，走"读"
    assert mcp_server.memory_edit(memory_id=mid, tags="", topic="")["content"] == "原文"


def test_missing_memory_hints_draft_id():
    assert "draft_id" in mcp_server.memory_edit(memory_id=9999, content="x")["error"]


def test_draft_read_then_append():
    did = _pending_draft()
    read = mcp_server.memory_edit(draft_id=did)
    assert read == {
        "draft_id": did, "status": "pending", "date": "2026-09-20",
        "content": "草稿原文", "tags": "日常,sensitive", "topic": "晚饭",
    }  # 不外露 quote / source_ref / links
    result = mcp_server.memory_edit(draft_id=did, content=read["content"] + "原话：不想吃那个", tags="")
    assert result["status"] == "pending" and result["changed"] == ["content"]
    draft = drafts.get_draft(did)
    assert draft["content"] == "草稿原文原话：不想吃那个"
    assert draft["tags"] == "日常,sensitive"  # 空 tags 没把 sensitive 清掉
    assert draft["status"] == "pending" and memories.get_status()["memories"] == 0


def test_draft_states():
    rejected = _pending_draft()
    drafts.reject_draft(rejected)
    assert "error" in mcp_server.memory_edit(draft_id=rejected)  # 被拒的不外露
    assert "error" in mcp_server.memory_edit(draft_id=rejected, content="x")
    approved = _pending_draft()
    mid = drafts.approve_draft(approved)["memory_id"]
    result = mcp_server.memory_edit(draft_id=approved, content="x")
    assert result["memory_id"] == mid and "memory_id" in result["error"]


def test_draft_approved_between_read_and_write(monkeypatch):
    did = _pending_draft()
    real_update = drafts.update_draft

    def approve_first(draft_id, edits):
        drafts.approve_draft(draft_id)  # 轩刚好在这一刻点了通过
        return real_update(draft_id, edits)

    monkeypatch.setattr(drafts, "update_draft", approve_first)
    result = mcp_server.memory_edit(draft_id=did, content="晚了一步")
    assert "刚被轩审核" in result["error"]
    assert memories.get_memory(drafts.get_draft(did)["memory_id"])["content"] == "草稿原文"


def test_same_number_routes_by_kind():
    """草稿号和记忆号各自从 1 数：同一个数字两边都有时，按参数名改对那一条。"""
    mid = _mem(content="记忆一号")
    did = _pending_draft(content="草稿一号")
    assert mid == did == 1
    mcp_server.memory_edit(draft_id=1, content="草稿一号改")
    assert drafts.get_draft(1)["content"] == "草稿一号改"
    assert memories.get_memory(1)["content"] == "记忆一号"
    assert memory_edits.list_edits()["stats"]["total"] == 0
