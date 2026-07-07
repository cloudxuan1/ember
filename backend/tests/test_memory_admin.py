"""审核台"记忆库"视图：已入库记忆的浏览与修改（sensitive 打标的家）。

纪律：只收白名单字段（状态列不开放手改）；tags 规整中文标点；
date/content 不能改空；改内容后指纹跟着重算（软失败）；门禁与草稿 API 同一套
——MCP 的 token 必须进不来。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import memories, review
from app.db import init_db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBER_DB", str(tmp_path / "test.db"))
    init_db()


@pytest.fixture(scope="module")
def client():
    test_app = FastAPI()
    test_app.include_router(review.router)
    with TestClient(test_app) as c:
        yield c


@pytest.fixture
def gated(client, monkeypatch):
    monkeypatch.setenv("EMBER_OAUTH_PASSWORD", "开门")
    monkeypatch.setenv("EMBER_OAUTH_ACCESS_TOKEN", "token-abc")
    monkeypatch.setenv("EMBER_REVIEW_TOKEN", "review-tok")
    return client


BEARER = {"Authorization": "Bearer review-tok"}
MCP_BEARER = {"Authorization": "Bearer token-abc"}  # MCP 的 token，审核台必须拒收


def _save(content="一条记忆", date="2026-07-01", **kw) -> int:
    return memories.save_memory(date=date, content=content, **kw)["id"]


# ---------- 浏览 ----------


def test_browse_returns_full_content_and_pages(client):
    long = "很长的内容 " * 40
    _save(long, date="2026-07-02")  # 日期最新 → 排第一页，好断言
    for i in range(21):
        _save(f"记忆 {i}", space="ember")
    data = client.get("/review/api/memories").json()
    assert data["stats"]["total"] == 22  # 默认跨全库（管理视角）
    assert data["total_pages"] == 2
    assert any(i["content"] == long for i in data["items"])  # 不截断


def test_browse_q_and_space_filters(client):
    _save("海边看日出")
    _save("工地日志", space="ember")
    q = client.get("/review/api/memories?q=海边").json()
    assert q["stats"]["total"] == 1 and "海边" in q["items"][0]["content"]
    sp = client.get("/review/api/memories?space=ember").json()
    assert sp["stats"]["total"] == 1 and sp["items"][0]["space"] == "ember"


def test_browse_carries_interval_status(client):
    _save("进行中", start_date="2026-06-01")
    data = client.get("/review/api/memories").json()
    assert data["items"][0]["interval_status"] == "ongoing"


# ---------- 修改 ----------


def test_patch_tags_normalizes_chinese_separators(client):
    mid = _save()
    resp = client.patch(f"/review/api/memories/{mid}", json={"tags": "海边，sensitive、旅行"})
    assert resp.status_code == 200
    assert resp.json()["tags"] == "海边,sensitive,旅行"
    assert memories.get_memory(mid)["tags"] == "海边,sensitive,旅行"


def test_patch_ignores_non_whitelist_fields(client):
    mid = _save()
    resp = client.patch(
        f"/review/api/memories/{mid}",
        json={"tags": "x", "superseded_by": 999, "is_resolved": 1, "id": 12345},
    )
    assert resp.status_code == 200
    m = memories.get_memory(mid)
    assert m["superseded_by"] is None and m["is_resolved"] == 0


def test_patch_blank_interval_clears_endpoint(client):
    mid = _save(start_date="2026-06-01", end_date="2026-12-31")
    resp = client.patch(f"/review/api/memories/{mid}", json={"end_date": ""})
    assert resp.status_code == 200
    m = memories.get_memory(mid)
    assert m["end_date"] is None and m["interval_status"] == "ongoing"


def test_patch_rejects_bad_tier_and_empty_content(client):
    mid = _save()
    assert client.patch(f"/review/api/memories/{mid}", json={"tier": "vip"}).status_code == 400
    assert client.patch(f"/review/api/memories/{mid}", json={"content": "  "}).status_code == 400
    assert client.patch(f"/review/api/memories/{mid}", json={}).status_code == 400


def test_patch_missing_memory_404(client):
    assert client.patch("/review/api/memories/9999", json={"tags": "x"}).status_code == 404


# ---------- 门禁 ----------


def test_gate_rejects_anonymous_and_mcp_token(gated):
    mid = _save()
    assert gated.get("/review/api/memories").status_code == 401
    assert gated.patch(f"/review/api/memories/{mid}", json={"tags": "x"}).status_code == 401
    assert (
        gated.patch(f"/review/api/memories/{mid}", json={"tags": "x"}, headers=MCP_BEARER).status_code
        == 401
    )
    assert gated.get("/review/api/memories", headers=BEARER).status_code == 200
