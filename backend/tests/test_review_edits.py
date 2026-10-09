"""审核台 × AI 改动：通过时防偷换、「✎ 改动」的列表 / 确认 / 驳回接口与门禁。

门禁纪律：确认 / 驳回只认轩的浏览器 cookie——脚本钥匙 EMBER_REVIEW_TOKEN 在提取用的
AI 会话手里，MCP 的 token 更不行；AI 不能自己确认自己提的改动。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import drafts, mcp_server, memories, memory_edits, review
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
MCP_BEARER = {"Authorization": "Bearer token-abc"}


def _cookie(gated) -> dict:
    login = gated.post("/review/login", data={"password": "开门"}, follow_redirects=False)
    return {"Cookie": login.headers["set-cookie"].split(";")[0]}


def _proposal(content="原文", new="原文。原话：不想吃那个") -> tuple[int, dict]:
    mid = memories.save_memory(date="2026-09-20", content=content, tags="日常")["id"]
    memory_edits.propose_edit(mid, content=new, reason="补原话")
    return mid, memory_edits.list_edits()["items"][0]


# ---------- 通过时防偷换 ----------


def test_approve_with_matching_expect_passes(client):
    did = drafts.save_draft(date="2026-09-20", content="草稿", tags="日常，吃饭")["id"]
    card = client.get("/review/api/drafts").json()["items"][0]
    seen = {"content": card["content"], "tags": "日常,吃饭", "topic": card["topic"]}
    resp = client.post(f"/review/api/drafts/{did}/approve", json={"expect": seen})
    assert resp.status_code == 200 and resp.json()["status"] == "approved"


def test_approve_refuses_content_she_never_saw(client):
    """她早上打开审核台，之后 AI 改了这张草稿，她在旧卡片上点通过 → 409，不入库。"""
    did = drafts.save_draft(date="2026-09-20", content="她看到的版本")["id"]
    card = client.get("/review/api/drafts").json()["items"][0]
    mcp_server.memory_edit(draft_id=did, content="AI 后来改的版本")
    resp = client.post(
        f"/review/api/drafts/{did}/approve",
        json={"expect": {"content": card["content"], "tags": card["tags"], "topic": card["topic"]}},
    )
    assert resp.status_code == 409
    assert drafts.get_draft(did)["status"] == "pending"
    assert memories.get_status()["memories"] == 0


def test_approve_without_expect_unchanged(client):
    """不带 expect 的老调用（脚本、保存并通过、手动添加）行为不变。"""
    did = drafts.save_draft(date="2026-09-20", content="草稿")["id"]
    resp = client.post(f"/review/api/drafts/{did}/approve", json={"content": "她改过的"})
    assert resp.status_code == 200
    assert memories.get_memory(resp.json()["memory_id"])["content"] == "她改过的"


def test_console_sends_expect_on_plain_approve(client):
    page = client.get("/review").text
    assert "expect: { content: d.content, tags: d.tags, topic: d.topic }" in page
    assert "resp.status === 409" in page


# ---------- 改动列表 / 确认 / 驳回 ----------


def test_list_edits_card(client):
    mid, _ = _proposal()
    data = client.get("/review/api/edits").json()
    assert data["stats"]["total"] == 1
    card = data["items"][0]
    assert card["memory_id"] == mid and card["reason"] == "补原话"
    assert [s["op"] for s in card["changes"]["content"]["segments"]] == ["equal", "insert"]


def test_confirm_and_reject_flow_with_cookie(gated):
    cookie = _cookie(gated)
    mid, card = _proposal()
    resp = gated.post(
        f"/review/api/edits/{card['id']}/confirm",
        json={"version": card["version"], "seen": card["seen"]}, headers=cookie,
    )
    assert resp.status_code == 200 and resp.json()["content"] == "原文。原话：不想吃那个"
    assert gated.post(  # 处理过了
        f"/review/api/edits/{card['id']}/confirm",
        json={"version": card["version"], "seen": card["seen"]}, headers=cookie,
    ).status_code == 404

    mid2, card2 = _proposal(content="另一条")
    resp = gated.post(f"/review/api/edits/{card2['id']}/reject", json={"version": card2["version"]}, headers=cookie)
    assert resp.status_code == 200
    assert memories.get_memory(mid2)["content"] == "另一条"


def test_confirm_stale_is_409_and_changes_nothing(client):
    mid, card = _proposal()
    memory_edits.propose_edit(mid, content="AI 又改了一版")
    resp = client.post(
        f"/review/api/edits/{card['id']}/confirm", json={"version": card["version"], "seen": card["seen"]}
    )
    assert resp.status_code == 409
    assert memories.get_memory(mid)["content"] == "原文"
    resp = client.post(f"/review/api/edits/{card['id']}/reject", json={"version": card["version"]})
    assert resp.status_code == 409 and memory_edits.list_edits()["stats"]["total"] == 1


def test_confirm_requires_version_and_seen(client):
    _, card = _proposal()
    assert client.post(f"/review/api/edits/{card['id']}/confirm", json={}).status_code == 400
    assert client.post(f"/review/api/edits/{card['id']}/confirm", json={"version": "1", "seen": "x"}).status_code == 400
    assert client.post(f"/review/api/edits/{card['id']}/reject").status_code == 400


def test_scripts_and_mcp_cannot_confirm_or_reject(gated):
    """脚本钥匙能看列表（同其他审核台 API），但确认 / 驳回只认轩的浏览器。"""
    mid, card = _proposal()
    body = {"version": card["version"], "seen": card["seen"]}
    assert gated.get("/review/api/edits").status_code == 401
    assert gated.get("/review/api/edits", headers=MCP_BEARER).status_code == 401
    assert gated.get("/review/api/edits", headers=BEARER).status_code == 200
    for headers in ({}, BEARER, MCP_BEARER):
        assert gated.post(f"/review/api/edits/{card['id']}/confirm", json=body, headers=headers).status_code == 401
        assert gated.post(f"/review/api/edits/{card['id']}/reject", json=body, headers=headers).status_code == 401
    assert memories.get_memory(mid)["content"] == "原文"
    assert memory_edits.list_edits()["stats"]["total"] == 1


def test_unreview_reports_dropped_edit(client):
    did = drafts.save_draft(date="2026-09-20", content="原文")["id"]
    mid = drafts.approve_draft(did)["memory_id"]
    memory_edits.propose_edit(mid, content="改过")
    resp = client.post(f"/review/api/drafts/{did}/unreview").json()
    assert resp == {"draft_id": did, "status": "pending", "dropped_edit": True}
    assert memory_edits.list_edits()["stats"]["total"] == 0


def test_console_has_edits_view_and_valid_escapes(client):
    """CONSOLE_PAGE 是普通 Python 字符串：JS 正则里的 \\n 必须写成双反斜杠，
    否则变成真换行，整段脚本语法错误、审核台白屏。"""
    page = client.get("/review").text
    assert 'id="editBtn"' in page and "/review/api/edits/" in page
    assert "replace(/\\n/g" in page and "/^\\s+$/" in page
    assert "\n/g" not in page  # 正则里没有被吃成真换行的 \n
