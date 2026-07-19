import pytest

from app import drafts, mcp_server, memories
from app.db import init_db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBER_DB", str(tmp_path / "test.db"))
    init_db()


def test_memory_save_creates_pending_draft_without_writing_memory():
    result = mcp_server.memory_save(
        date="2026-07-20",
        content="MCP 新记忆先等轩审核。",
    )

    assert set(result) == {"draft_id", "status", "message"}
    assert isinstance(result["draft_id"], int)
    assert result["status"] == "pending"
    assert result["message"] == "已存为待审草稿，等轩审核后入库。"
    assert "不会直接写入正式记忆库" in mcp_server.memory_save.__doc__
    assert drafts.get_draft(result["draft_id"])["status"] == "pending"
    assert memories.get_status()["memories"] == 0


def test_memory_save_maps_all_fields_to_draft():
    target = memories.save_memory(date="2026-07-01", content="已有记忆")["id"]
    result = mcp_server.memory_save(
        date="2026-07-20",
        content="完整字段草稿",
        tags="项目，约定",
        tier="anchor",
        topic="MCP 草稿",
        space="ember",
        start_date="2026-07-15",
        end_date="2026-07-31",
        links=[{"id": target, "relation": "led_to", "dir": "in"}],
    )

    draft = drafts.get_draft(result["draft_id"])
    assert {
        key: draft[key]
        for key in (
            "date", "content", "tags", "tier", "topic", "space",
            "start_date", "end_date", "batch", "status",
        )
    } == {
        "date": "2026-07-20",
        "content": "完整字段草稿",
        "tags": "项目,约定",
        "tier": "anchor",
        "topic": "MCP 草稿",
        "space": "ember",
        "start_date": "2026-07-15",
        "end_date": "2026-07-31",
        "batch": f"mcp-{memories._today()}",
        "status": "pending",
    }
    assert {
        key: draft["links"][0][key] for key in ("memory_id", "relation", "dir")
    } == {"memory_id": target, "relation": "led_to", "dir": "in"}
    assert memories.get_status()["memories"] == 1  # 只有预先存在的目标记忆


def test_memory_save_draft_enters_memory_after_review_approval():
    pending = mcp_server.memory_save(
        date="2026-07-20",
        content="审核通过后才入库",
        topic="MCP 审核验收",
    )

    approved = drafts.approve_draft(pending["draft_id"])
    assert approved["status"] == "approved"
    assert drafts.get_draft(pending["draft_id"])["status"] == "approved"
    memory = memories.get_memory(approved["memory_id"])
    assert memory["content"] == "审核通过后才入库"
    assert memory["topic"] == "MCP 审核验收"
