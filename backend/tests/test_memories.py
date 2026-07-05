import pytest

from app import memories
from app.db import init_db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBER_DB", str(tmp_path / "test.db"))
    init_db()


def test_save_and_recall_with_source():
    saved = memories.save_memory(
        date="2026-05-29",
        content="那晚的恐惧：模型说下架就下架，她开始翻找别的出路。",
        tags="关系,转折",
        tier="anchor",
        topic="5.29",
        source_ref="claude-export/2026-07-03/conversations.json#abc12345",
        quote="说下架就下架",
    )
    memory = memories.get_memory(saved["id"])
    assert memory["tier"] == "anchor"
    assert memory["sources"][0]["quote"] == "说下架就下架"


def test_recall_missing_returns_none():
    assert memories.get_memory(9999) is None


def test_save_rejects_bad_tier():
    with pytest.raises(ValueError):
        memories.save_memory(date="2026-07-03", content="x", tier="important")


def test_search_returns_short_catalog_not_fulltext():
    long_content = "关于 ember 部署的决定：" + "长内容" * 100
    memories.save_memory(date="2026-07-03", content=long_content, tags="部署", space="ember")
    results = memories.search_memories("部署", space="ember")
    assert len(results) == 1
    assert len(results[0]["content"]) <= memories.SUMMARY_LEN + 1  # 截断 + 省略号
    assert "id" in results[0]


def test_search_space_filter_and_tag_weight():
    memories.save_memory(date="2026-07-01", content="喜欢热情一点的语气", tags="偏好", space="personal")
    memories.save_memory(date="2026-07-02", content="VPS 上跑着旧系统", tags="部署", space="vps")
    assert len(memories.search_memories("热情", space="personal")) == 1
    assert memories.search_memories("热情", space="vps") == []


def test_search_default_isolates_personal_core():
    """V6 空间隔离：不传 space = 只搜 personal，项目记忆不稀释核心层；all 才跨全库。"""
    p = memories.save_memory(date="2026-07-01", content="喜欢热情一点的语气")
    memories.save_memory(date="2026-07-02", content="容器日志里语气冷冰冰的报错", space="vps")
    assert [r["id"] for r in memories.search_memories("语气")] == [p["id"]]
    assert len(memories.search_memories("语气", space="all")) == 2


def test_list_default_isolates_personal_core():
    memories.save_memory(date="2026-07-01", content="核心记忆")
    memories.save_memory(date="2026-07-02", content="项目记忆", space="ember")
    default = memories.list_memories()
    assert default["stats"]["total"] == 1
    assert default["stats"]["by_space"] == {"personal": 1}
    assert memories.list_memories(space="all")["stats"]["total"] == 2


def test_list_pagination_and_stats():
    for i in range(25):
        memories.save_memory(date=f"2026-06-{i % 28 + 1:02d}", content=f"记忆 {i}", space="ember")
    page1 = memories.list_memories(space="ember", page=1)
    assert page1["stats"]["total"] == 25
    assert page1["stats"]["by_space"] == {"ember": 25}
    assert len(page1["items"]) == 20
    page2 = memories.list_memories(space="ember", page=2)
    assert len(page2["items"]) == 5
    assert page2["total_pages"] == 2


def test_status_counts():
    memories.save_memory(date="2026-07-03", content="x", source_ref="a", quote="b")
    status = memories.get_status()
    assert status["memories"] == 1
    assert status["sources"] == 1
    assert status["last_write"] is not None
