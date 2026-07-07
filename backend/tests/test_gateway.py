"""聊天网关 + 回流：每轮记忆注入、透明化、软失败、锚点推进。

纪律：哨兵挂了聊天照常（正则兜底、单向纠正）；memory_meta 记录本轮注入了
什么（prompt 透明不黑箱）；回流产物进草稿区不直接入库；锚点提取成功才推进，
整批原子可重试；CHAT_MODEL 未配置 = /chat 关闭报人话错误。
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import drafts, gateway, memories, reflow, review
from app.db import get_conn, init_db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBER_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("CHAT_MODEL", "test/model")
    monkeypatch.setenv("EMBEDDING_API_KEY", "test-key")
    init_db()


@pytest.fixture(scope="module")
def client():
    test_app = FastAPI()
    test_app.include_router(review.router)
    test_app.include_router(gateway.router)
    with TestClient(test_app) as c:
        yield c


@pytest.fixture
def fake_llm(monkeypatch):
    """替身主模型：记录收到的 messages，回固定台词。"""
    calls = []

    def fake(model, messages, timeout, max_tokens=2048):
        calls.append({"model": model, "messages": messages})
        return "我记得呢。"

    monkeypatch.setattr(gateway, "_completion", fake)
    return calls


def _send(client, text, conversation_id=None):
    return client.post("/chat/api/send", json={"text": text, "conversation_id": conversation_id})


# ---------- 发消息主流程 ----------


def test_send_creates_conversation_and_stores_both_sides(client, fake_llm):
    resp = _send(client, "晚上好呀")
    assert resp.status_code == 200
    data = resp.json()
    assert data["reply"] == "我记得呢。"
    msgs = client.get(f"/chat/api/conversations/{data['conversation_id']}/messages").json()["items"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["memory_meta"] is not None  # 透明化：注入了什么有据可查


def test_memory_injected_into_prompt_with_discipline(client, fake_llm):
    memories.save_memory(date="2026-07-01", content="轩喜欢螃蟹和云")
    _send(client, "上次我们聊到哪了")  # "上次"触发单向纠正必搜
    sent = fake_llm[-1]["messages"]
    joined = "\n".join(m["content"] for m in sent)
    assert "小抄" in joined and "不是任务清单" in joined
    assert "螃蟹和云" in joined  # 记忆真的进了 prompt


def test_sentinel_fallback_keeps_chat_alive(client, fake_llm, monkeypatch):
    monkeypatch.setenv("SENTINEL_MODEL", "broken/model")
    monkeypatch.setattr(gateway, "instant_digest", gateway.instant_digest)  # 真跑（会因 fake_llm 替身而不炸）
    resp = _send(client, "记得那次的约定吗")
    assert resp.status_code == 200
    meta = resp.json()["memory"]
    assert meta["digest"]["search_needed"] is True  # 正则单向纠正


def test_history_carried_in_same_conversation(client, fake_llm):
    cid = _send(client, "第一句").json()["conversation_id"]
    _send(client, "第二句", conversation_id=cid)
    sent = fake_llm[-1]["messages"]
    joined = "\n".join(m["content"] for m in sent)
    assert "第一句" in joined and "我记得呢" in joined


def test_unconfigured_chat_returns_503(client, fake_llm, monkeypatch):
    monkeypatch.delenv("CHAT_MODEL")
    resp = _send(client, "在吗")
    assert resp.status_code == 503
    assert "CHAT_MODEL" in resp.json()["error_description"]


def test_missing_conversation_404_and_empty_text_400(client, fake_llm):
    assert _send(client, "x", conversation_id=999).status_code == 404
    assert _send(client, "   ").status_code == 400


def test_conversations_list(client, fake_llm):
    _send(client, "开个新会话")
    items = client.get("/chat/api/conversations").json()["items"]
    assert items and items[0]["messages"] >= 2


# ---------- 回流 ----------


def _chat_rounds(client, cid, n):
    for i in range(n):
        cid = _send(client, f"第{i}轮的话", conversation_id=cid).json()["conversation_id"]
    return cid


def test_reflow_extracts_drafts_and_advances_anchor(client, fake_llm, monkeypatch):
    cid = _chat_rounds(client, None, 3)  # 6 条消息
    extracted = json.dumps([{
        "date": "2026-07-08", "content": "轩说了件值得记的事", "tags": "生活，sensitive",
        "tier": "重要", "topic": "测试", "quote": "值得记的事",
    }], ensure_ascii=False)
    monkeypatch.setattr(gateway, "_completion", lambda *a, **k: extracted)
    result = reflow.run_reflow(cid)
    assert result["drafts"] == 1
    with get_conn() as conn:
        d = conn.execute("SELECT * FROM memory_drafts ORDER BY id DESC").fetchone()
        anchor = conn.execute("SELECT reflow_anchor_id FROM conversations WHERE id = ?", (cid,)).fetchone()[0]
        last_msg = conn.execute("SELECT MAX(id) FROM chat_messages WHERE conversation_id = ?", (cid,)).fetchone()[0]
    assert d["batch"].startswith("chat回流/")
    assert d["source_ref"].startswith(f"chat:{cid}:")
    assert d["tags"] == "生活,sensitive"      # 中文逗号规整
    assert d["tier"] == "normal"              # 模型瞎填的 tier 兜底
    assert d["status"] == "pending"           # 进草稿区，不直接入库
    assert anchor == last_msg                 # 锚点推进到最后一条


def test_reflow_empty_extraction_still_advances_anchor(client, fake_llm, monkeypatch):
    cid = _chat_rounds(client, None, 2)
    monkeypatch.setattr(gateway, "_completion", lambda *a, **k: "[]")
    result = reflow.run_reflow(cid)
    assert result["drafts"] == 0
    result2 = reflow.run_reflow(cid)
    assert result2["skipped"] is True and result2["pending"] == 0  # 不会反复嚼同一段


def test_reflow_failure_keeps_anchor_for_retry(client, fake_llm, monkeypatch):
    cid = _chat_rounds(client, None, 2)

    def boom(*a, **k):
        raise RuntimeError("LLM 炸了")

    monkeypatch.setattr(gateway, "_completion", boom)
    with pytest.raises(RuntimeError):
        reflow.run_reflow(cid)
    with get_conn() as conn:
        anchor = conn.execute("SELECT reflow_anchor_id FROM conversations WHERE id = ?", (cid,)).fetchone()[0]
    assert anchor == 0  # 锚点没动，下次重试不漏


def test_reflow_respects_min_messages(client, fake_llm):
    cid = _chat_rounds(client, None, 1)  # 只有 2 条
    result = reflow.run_reflow(cid, min_messages=12)
    assert result["skipped"] is True and result["pending"] == 2


def test_reflow_feeds_known_memories_and_saves_link_suggestions(client, fake_llm, monkeypatch):
    """kiwi-mem 学的两招：提取前给模型看已知记忆防重；矛盾/进展建议 supersedes 边。"""
    old = memories.save_memory(date="2026-04-19", content="朋友要生孩子了")["id"]
    cid = _send(client, "要生孩子").json()["conversation_id"]  # 话题跟已知记忆搭上（测试环境纯关键词）
    _send(client, "要生孩子", conversation_id=cid)
    prompts = []

    def fake_extract(model, messages, timeout, max_tokens=2048):
        prompts.append(messages[0]["content"])
        return json.dumps([{
            "date": "2026-07-08", "content": "朋友的宝宝三个月了", "topic": "朋友",
            "links": [
                {"memory_id": old, "relation": "supersedes"},
                {"memory_id": -1, "relation": "supersedes"},     # 瞎编的 id，丢弃
                {"memory_id": old, "relation": "contradicts"},   # 纪律外关系，丢弃
            ],
        }], ensure_ascii=False)

    monkeypatch.setattr(gateway, "_completion", fake_extract)
    result = reflow.run_reflow(cid)
    assert result["drafts"] == 1
    assert "要生孩子" in prompts[0] and "已知记忆" in prompts[0]  # 已知信息进了提取 prompt
    with get_conn() as conn:
        d = conn.execute("SELECT links FROM memory_drafts ORDER BY id DESC").fetchone()
    links = json.loads(d["links"])
    assert len(links) == 1
    assert links[0]["memory_id"] == old and links[0]["relation"] == "supersedes"


# ---------- 门禁 ----------


def test_gate_rejects_anonymous_and_mcp_token(client, fake_llm, monkeypatch):
    monkeypatch.setenv("EMBER_OAUTH_PASSWORD", "开门")
    monkeypatch.setenv("EMBER_OAUTH_ACCESS_TOKEN", "token-abc")
    monkeypatch.setenv("EMBER_REVIEW_TOKEN", "review-tok")
    assert _send(client, "在吗").status_code == 401
    assert client.get("/chat/api/conversations").status_code == 401
    assert client.get(
        "/chat/api/conversations", headers={"Authorization": "Bearer token-abc"}
    ).status_code == 401  # MCP 的 token 进不来
    assert client.get(
        "/chat/api/conversations", headers={"Authorization": "Bearer review-tok"}
    ).status_code == 200
