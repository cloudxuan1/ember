"""MCP memory_queue（审核台待办只读）+ memory_search 的 include_drafts。"""

import pytest

from app import drafts, mcp_server, memories, memory_edits
from app.db import get_conn, init_db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBER_DB", str(tmp_path / "test.db"))
    init_db()


def _draft(content="草稿", **kw) -> int:
    return drafts.save_draft(date="2026-10-01", content=content, **kw)["id"]


def _set_ts(table: str, col: str, row_id: int, ts: str) -> None:
    with get_conn() as conn:
        conn.execute(f"UPDATE {table} SET {col} = ? WHERE id = ?", (ts, row_id))


def test_empty_queue():
    q = mcp_server.memory_queue()
    assert q["stats"] == {"total": 0, "drafts": 0, "edits": 0}
    assert q["items"] == [] and q["total_pages"] == 1


def test_queue_mixes_drafts_and_edits_newest_first():
    mid = memories.save_memory(date="2026-09-20", content="正式记忆开头" + "字" * 50, topic="晚饭")["id"]
    old = _draft("旧草稿", topic="t1", tags="a,b")
    new = _draft("新草稿" + "长" * 100)
    edit = memory_edits.propose_edit(mid, content="改过的", topic="晚饭纠结")["edit_id"]
    _set_ts("memory_drafts", "created_at", old, "2026-10-01 10:00:00")
    _set_ts("memory_edits", "updated_at", edit, "2026-10-02 10:00:00")
    _set_ts("memory_drafts", "created_at", new, "2026-10-03 10:00:00")
    rejected = _draft("被拒的")
    drafts.reject_draft(rejected)

    q = mcp_server.memory_queue()
    assert q["stats"] == {"total": 3, "drafts": 2, "edits": 1}
    kinds = [(i["kind"], i.get("draft_id") or i.get("memory_id")) for i in q["items"]]
    assert kinds == [("draft", new), ("edit", mid), ("draft", old)]
    first, middle, last = q["items"]
    assert first["content"] == "新草稿" + "长" * 77 + "…"
    assert last == {"kind": "draft", "draft_id": old, "date": "2026-10-01", "topic": "t1",
                    "tags": "a,b", "content": "旧草稿"}
    assert middle["changed"] == ["content", "topic"]
    assert middle["target"]["date"] == "2026-09-20" and middle["target"]["preview"].startswith("正式记忆开头")


def test_queue_pages_of_20():
    ids = [_draft(f"草稿{i}") for i in range(25)]
    p1, p2 = mcp_server.memory_queue(), mcp_server.memory_queue(page=2)
    assert p1["stats"]["total"] == 25 and p1["total_pages"] == 2
    assert len(p1["items"]) == 20 and len(p2["items"]) == 5
    # 同一批 created_at 相同时按编号新的在前
    assert [i["draft_id"] for i in p1["items"] + p2["items"]] == ids[::-1]


def test_queue_is_read_only():
    did = _draft()
    mcp_server.memory_queue()
    assert drafts.get_draft(did)["status"] == "pending"


def test_search_drafts_off_by_default():
    memories.save_memory(date="2026-09-01", content="猫咪打呼噜")
    _draft("猫咪又打呼噜了")
    r = mcp_server.memory_search("打呼噜")
    assert r["count"] == 1 and "draft_id" not in r["results"][0]


def test_search_include_drafts_after_formal():
    mid = memories.save_memory(date="2026-09-01", content="猫咪打呼噜")["id"]
    did = _draft("猫咪又打呼噜了")
    _draft("项目里的打呼噜", space="ember")
    drafts.reject_draft(_draft("被拒的打呼噜"))
    r = mcp_server.memory_search("打呼噜", include_drafts=True)
    assert r["count"] == 2
    assert r["results"][0]["id"] == mid
    assert r["results"][1]["draft_id"] == did and r["results"][1]["status"] == "待审"
    assert "id" not in r["results"][1]
    assert len(mcp_server.memory_search("打呼噜", space="all", include_drafts=True)["results"]) == 3
