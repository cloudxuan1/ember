import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import drafts, memories, review
from app.db import init_db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBER_DB", str(tmp_path / "test.db"))
    init_db()


# 审核台不依赖 MCP，单独组装 app——app.main 的 mcp session manager
# 全局只能 run 一次，已被 test_oauth 的 TestClient 占用。
@pytest.fixture(scope="module")
def client():
    test_app = FastAPI()
    test_app.include_router(review.router)
    with TestClient(test_app) as c:
        yield c


@pytest.fixture
def gated(client, monkeypatch):
    """开启门禁的客户端。"""
    monkeypatch.setenv("EMBER_OAUTH_PASSWORD", "开门")
    monkeypatch.setenv("EMBER_OAUTH_ACCESS_TOKEN", "token-abc")
    return client


BEARER = {"Authorization": "Bearer token-abc"}


def _draft(**overrides) -> dict:
    base = {
        "date": "2026-04-20",
        "content": "草稿：那天她说想把记忆留住。",
        "tags": "关系",
        "topic": "记忆",
        "source_ref": "2026日志/4月/4.20.md#片段",
        "quote": "想把记忆留住",
        "batch": "4.18起点/group_001",
    }
    return {**base, **overrides}


# ---------- 存取层 ----------


def test_approve_moves_draft_into_memories_with_source():
    draft_id = drafts.save_draft(**_draft())["id"]
    result = drafts.approve_draft(draft_id)
    assert result["status"] == "approved"
    memory = memories.get_memory(result["memory_id"])
    assert memory["content"] == "草稿：那天她说想把记忆留住。"
    assert memory["sources"][0]["quote"] == "想把记忆留住"
    # 终态不能二审
    assert drafts.approve_draft(draft_id) is None
    assert drafts.reject_draft(draft_id) is None


def test_approve_with_edits_saves_edited_version():
    draft_id = drafts.save_draft(**_draft())["id"]
    result = drafts.approve_draft(draft_id, edits={"content": "改后的内容", "tier": "anchor"})
    memory = memories.get_memory(result["memory_id"])
    assert memory["content"] == "改后的内容"
    assert memory["tier"] == "anchor"


def test_reject_keeps_row_but_not_in_memories():
    draft_id = drafts.save_draft(**_draft())["id"]
    assert drafts.reject_draft(draft_id)["status"] == "rejected"
    assert drafts.get_draft(draft_id)["status"] == "rejected"  # 审计留痕
    assert memories.get_status()["memories"] == 0
    assert drafts.update_draft(draft_id, {"content": "x"}) is None  # 终态不能改


def test_update_ignores_non_editable_fields():
    draft_id = drafts.save_draft(**_draft())["id"]
    updated = drafts.update_draft(draft_id, {"content": "新内容", "status": "approved", "memory_id": 99})
    assert updated["content"] == "新内容"
    assert updated["status"] == "pending"
    assert updated["memory_id"] is None


def test_save_draft_rejects_bad_input():
    with pytest.raises(ValueError):
        drafts.save_draft(date="", content="x")
    with pytest.raises(ValueError):
        drafts.save_draft(date="2026-04-20", content="x", tier="important")


def test_list_drafts_stats_by_batch():
    drafts.save_draft(**_draft())
    drafts.save_draft(**_draft(batch="4.18起点/group_002"))
    drafts.save_draft(**_draft(batch="4.18起点/group_002"))
    data = drafts.list_drafts()
    assert data["stats"]["by_batch"] == {"4.18起点/group_001": 1, "4.18起点/group_002": 2}
    only = drafts.list_drafts(batch="4.18起点/group_002")
    assert data["stats"]["total"] == 3
    assert only["stats"]["total"] == 2
    # 审核要看全文，不截断
    assert only["items"][0]["content"] == _draft()["content"]


# ---------- 鉴权 ----------


def test_console_and_api_require_auth_when_gated(gated):
    page = gated.get("/review")
    assert page.status_code == 200
    assert "口令" in page.text  # 未登录 → 登录页
    assert gated.get("/review/api/drafts").status_code == 401
    assert gated.post("/review/api/drafts", json=_draft()).status_code == 401


def test_bearer_and_cookie_both_work(gated):
    resp = gated.post("/review/api/drafts", json={"drafts": [_draft(), _draft()]}, headers=BEARER)
    assert resp.status_code == 201
    assert resp.json()["saved"] == 2

    login = gated.post("/review/login", data={"password": "开门"}, follow_redirects=False)
    assert login.status_code == 302
    cookie = login.headers["set-cookie"].split(";")[0]
    listed = gated.get("/review/api/drafts", headers={"Cookie": cookie})
    assert listed.status_code == 200
    assert listed.json()["stats"]["total"] == 2
    console = gated.get("/review", headers={"Cookie": cookie})
    assert "审核台" in console.text and "口令" not in console.text


def test_wrong_password_no_cookie(gated):
    resp = gated.post("/review/login", data={"password": "猜的"}, follow_redirects=False)
    assert resp.status_code == 403
    assert "set-cookie" not in resp.headers


def test_open_when_gate_disabled(client):
    assert client.get("/review/api/drafts").status_code == 200


# ---------- API 全流程 ----------


def test_api_full_review_flow(gated):
    saved = gated.post("/review/api/drafts", json=_draft(), headers=BEARER).json()
    draft_id = saved["ids"][0]

    edited = gated.patch(
        f"/review/api/drafts/{draft_id}", json={"content": "手机上改过"}, headers=BEARER
    ).json()
    assert edited["content"] == "手机上改过"

    approved = gated.post(f"/review/api/drafts/{draft_id}/approve", headers=BEARER).json()
    assert approved["status"] == "approved"
    assert memories.get_memory(approved["memory_id"])["content"] == "手机上改过"

    # 已审核的草稿再操作 → 404
    assert gated.post(f"/review/api/drafts/{draft_id}/reject", headers=BEARER).status_code == 404


def test_api_bad_draft_reports_position(gated):
    resp = gated.post(
        "/review/api/drafts",
        json={"drafts": [_draft(), {"date": "", "content": ""}]},
        headers=BEARER,
    )
    assert resp.status_code == 400
    assert "第 2 条" in resp.json()["error_description"]
