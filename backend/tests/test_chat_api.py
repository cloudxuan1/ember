"""chat-lite 只读接口（/internal/memory/*）：三把钥匙分开、参数上限、只读、软边界。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import chat_api, memories
from app.db import get_conn, init_db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBER_DB", str(tmp_path / "test.db"))
    init_db()


# 与审核台测试同理：不走 app.main（MCP session manager 全局只能 run 一次），单独组装。
@pytest.fixture(scope="module")
def client():
    test_app = FastAPI()
    test_app.include_router(chat_api.router)
    with TestClient(test_app) as c:
        yield c


@pytest.fixture
def gated(client, monkeypatch):
    monkeypatch.setenv("EMBER_READ_TOKEN", "read-tok")
    monkeypatch.setenv("EMBER_OAUTH_ACCESS_TOKEN", "mcp-tok")
    monkeypatch.setenv("EMBER_REVIEW_TOKEN", "review-tok")
    return client


READ = {"Authorization": "Bearer read-tok"}


def _save(content, date="2026-09-01", **kw) -> int:
    return memories.save_memory(date=date, content=content, **kw)["id"]


# ---------- 钥匙 ----------


def test_closed_when_token_missing(client, monkeypatch):
    monkeypatch.delenv("EMBER_READ_TOKEN", raising=False)
    r = client.post("/internal/memory/search", json={"query": "x"}, headers=READ)
    assert r.status_code == 503


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer wrong"},
    {"Authorization": "Bearer mcp-tok"},     # MCP 的钥匙开不了这扇门
    {"Authorization": "Bearer review-tok"},  # 审核台的钥匙也不行
])
def test_rejects_other_keys(gated, headers):
    for path, body in [("/search", {"query": "x"}), ("/recall", {"id": 1}), ("/briefing", {})]:
        r = gated.post(f"/internal/memory{path}", json=body, headers=headers)
        assert r.status_code == 401, path


# ---------- search ----------


def test_search_returns_catalog_without_full_text(gated):
    long_content = "她说想把记忆留住。" * 30
    mid = _save(long_content, tags="关系")
    r = gated.post("/internal/memory/search", json={"query": "记忆"}, headers=READ)
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    item = data["results"][0]
    assert item["id"] == mid
    assert len(item["content"]) < len(long_content)  # 目录条目是截断的
    assert "sources" not in item


@pytest.mark.parametrize("body", [
    {"query": ""},
    {"query": "   "},
    {"query": "x" * 501},
    {"query": "x", "limit": 0},
    {"query": "x", "limit": 9},
    {"query": "x", "space": "s" * 41},
    {},
])
def test_search_rejects_bad_params(gated, body):
    r = gated.post("/internal/memory/search", json=body, headers=READ)
    assert r.status_code == 422


def test_search_space_defaults_to_personal(gated):
    _save("个人层的事", tags="关系")
    _save("项目层的事", space="ember")
    r = gated.post("/internal/memory/search", json={"query": "的事"}, headers=READ)
    ids = {i["space"] for i in r.json()["results"]}
    assert ids == {"personal"}
    r = gated.post("/internal/memory/search", json={"query": "的事", "space": "all"}, headers=READ)
    assert {i["space"] for i in r.json()["results"]} == {"personal", "ember"}


# ---------- recall ----------


def test_recall_full_memory_and_404(gated):
    mid = _save("完整内容在这里", tags="关系")
    r = gated.post("/internal/memory/recall", json={"id": mid}, headers=READ)
    assert r.status_code == 200
    assert r.json()["content"] == "完整内容在这里"
    assert "sources" in r.json() and "edges" in r.json()
    assert gated.post("/internal/memory/recall", json={"id": mid + 99}, headers=READ).status_code == 404
    assert gated.post("/internal/memory/recall", json={"id": 0}, headers=READ).status_code == 422


# ---------- briefing ----------


def test_briefing_passthrough_and_cooldown(gated):
    _save("最近发生的事", date="2026-09-10")
    r = gated.post("/internal/memory/briefing", json={"topic": "  "}, headers=READ)
    assert r.status_code == 200
    items = r.json()["items"]
    assert items and items[0]["reason"]
    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM briefing_log").fetchone()[0] == len(items)
    # 三天冷却是 briefing 本来的行为：再调一次同一条不再递出
    r2 = gated.post("/internal/memory/briefing", json={}, headers=READ)
    assert all(i["id"] != items[0]["id"] for i in r2.json()["items"])


def test_endpoints_never_write_memories(gated):
    before = memories.get_status()["memories"]
    gated.post("/internal/memory/search", json={"query": "x"}, headers=READ)
    gated.post("/internal/memory/briefing", json={"topic": "x"}, headers=READ)
    assert memories.get_status()["memories"] == before
