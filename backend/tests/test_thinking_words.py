"""chat-lite 思考彩蛋词库：版本冲突、校验、历史、钥匙、MCP 逐项修改，以及不碰记忆表。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import mcp_server, memories, thinking_words, words_api
from app.db import get_conn, init_db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBER_DB", str(tmp_path / "test.db"))
    init_db()


@pytest.fixture(scope="module")
def client():
    test_app = FastAPI()
    test_app.include_router(words_api.router)
    with TestClient(test_app) as c:
        yield c


@pytest.fixture
def gated(client, monkeypatch):
    monkeypatch.setenv("EMBER_READ_TOKEN", "read-tok")
    monkeypatch.setenv("EMBER_OAUTH_ACCESS_TOKEN", "mcp-tok")
    monkeypatch.setenv("EMBER_REVIEW_TOKEN", "review-tok")
    return client


READ = {"Authorization": "Bearer read-tok"}


def library(**over):
    data = {
        "settings": {"egg": True, "translate": True, "marauder": True},
        "series": [
            {"id": "cooking", "name": "做饭系", "builtin": True, "enabled": True,
             "words": [{"en": "Marinating…", "zh": "腌入味中"}, {"en": "Simmering…", "zh": "小火慢炖中"}]},
            {"id": "custom-cat", "name": "猫猫系", "builtin": False, "enabled": True,
             "words": [{"en": "Purring…", "zh": "呼噜呼噜中"}]},
        ],
    }
    data.update(over)
    return data


# ---------- 存取与版本 ----------


def test_empty_library_reads_as_version_zero():
    assert thinking_words.get_library() == {"version": 0, "data": None, "updated_at": None, "updated_by": ""}


def test_first_save_then_versioned_updates():
    saved = thinking_words.save_library(library(), 0, by="web")
    assert saved["version"] == 1 and saved["updated_by"] == "web"
    assert saved["data"]["series"][1]["name"] == "猫猫系"
    again = thinking_words.save_library(library(settings={"egg": False}), 1, by="web")
    assert again["version"] == 2
    assert again["data"]["settings"] == {"egg": False, "translate": True, "marauder": True}


def test_save_response_belongs_to_its_own_commit(monkeypatch):
    """另一位写者在提交后立刻插队，响应仍须是本次版本，不能认领别人的版本。"""
    real_get_conn = thinking_words.get_conn

    class InterleavedCommit:
        def __init__(self):
            self.conn = real_get_conn()

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def commit(self):
            self.conn.commit()
            with monkeypatch.context() as patch:
                patch.setattr(thinking_words, "get_conn", real_get_conn)
                thinking_words.save_library(library(settings={"egg": False}), 1, by="mcp")

    monkeypatch.setattr(thinking_words, "get_conn", InterleavedCommit)
    saved = thinking_words.save_library(library(), 0, by="web")
    assert saved["version"] == 1
    assert saved["data"]["settings"]["egg"] is True
    monkeypatch.setattr(thinking_words, "get_conn", real_get_conn)
    assert thinking_words.get_library()["version"] == 2


def test_stale_base_version_conflicts_without_writing():
    thinking_words.save_library(library(), 0, by="web")
    thinking_words.save_library(library(settings={"egg": False}), 1, by="web")
    with pytest.raises(thinking_words.VersionConflict) as info:
        thinking_words.save_library(library(), 1, by="web")  # 另一台设备还停在版本 1
    assert info.value.current["version"] == 2
    assert thinking_words.get_library()["data"]["settings"]["egg"] is False
    with pytest.raises(thinking_words.VersionConflict):
        thinking_words.save_library(library(), 0, by="web")  # 已有词库时不能当成首次同步覆盖


def test_history_keeps_recent_versions_only():
    thinking_words.save_library(library(), 0, by="web")
    for v in range(1, 26):
        thinking_words.save_library(library(), v, by="web")
    with get_conn() as conn:
        versions = [r[0] for r in conn.execute("SELECT version FROM thinking_library_history ORDER BY version")]
    assert len(versions) == thinking_words.HISTORY_KEEP
    assert versions[-1] == 25 and versions[0] == 6


@pytest.mark.parametrize("bad, msg", [
    ({"series": "x"}, "series"),
    ({"series": [{"id": "Bad Id", "name": "x", "words": []}]}, "id"),
    ({"series": [{"id": "a", "name": "", "words": []}]}, "name"),
    ({"series": [{"id": "a", "name": "x", "words": [{"en": "", "zh": "空"}]}]}, "en"),
    ({"series": [{"id": "a", "name": "x", "words": []}, {"id": "a", "name": "y", "words": []}]}, "重复"),
    ({"settings": {"egg": "yes"}, "series": []}, "布尔"),
    ({"series": [{"id": "a", "name": "x" * 21, "words": []}]}, "20"),
])
def test_validation_rejects_bad_documents(bad, msg):
    with pytest.raises(ValueError, match=msg):
        thinking_words.save_library(bad, 0, by="web")
    assert thinking_words.get_library()["version"] == 0


def test_words_live_apart_from_memories():
    memories.save_memory(date="2026-09-01", content="一条记忆")
    with get_conn() as conn:
        before = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    thinking_words.save_library(library(), 0, by="web")
    thinking_words.edit_library("add_series", name="新系列")
    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == before


# ---------- HTTP 接口 ----------


def test_endpoints_closed_without_token(client, monkeypatch):
    monkeypatch.delenv("EMBER_READ_TOKEN", raising=False)
    assert client.post("/internal/words/get", headers=READ).status_code == 503


@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer wrong"},
    {"Authorization": "Bearer mcp-tok"}, {"Authorization": "Bearer review-tok"},
])
def test_endpoints_reject_other_keys(gated, headers):
    assert gated.post("/internal/words/get", headers=headers).status_code == 401
    r = gated.post("/internal/words/save", json={"base_version": 0, "data": library()}, headers=headers)
    assert r.status_code == 401
    assert thinking_words.get_library()["version"] == 0


def test_endpoints_save_conflict_and_validation(gated):
    assert gated.post("/internal/words/get", headers=READ).json()["version"] == 0
    r = gated.post("/internal/words/save", json={"base_version": 0, "data": library()}, headers=READ)
    assert r.status_code == 200 and r.json()["version"] == 1
    stale = gated.post("/internal/words/save", json={"base_version": 0, "data": library()}, headers=READ)
    assert stale.status_code == 409
    assert stale.json()["current"]["version"] == 1
    assert stale.json()["current"]["data"]["series"][0]["id"] == "cooking"
    bad = gated.post("/internal/words/save", json={"base_version": 1, "data": {"series": 1}}, headers=READ)
    assert bad.status_code == 422
    got = gated.post("/internal/words/get", headers=READ).json()
    assert got["version"] == 1 and got["data"]["series"][1]["words"][0]["zh"] == "呼噜呼噜中"


# ---------- MCP 工具 ----------


def test_mcp_view_before_first_sync_explains():
    assert "网页" in mcp_server.thinking_words_view()["error"]
    assert "网页" in mcp_server.thinking_words_edit("add_series", name="x")["error"]


def test_mcp_edit_actions_round_trip():
    thinking_words.save_library(library(), 0, by="web")
    added = mcp_server.thinking_words_edit("add_series", name="魔法系", words=[{"en": "Lumos-ing…", "zh": "灵光一闪中"}])
    assert added["ok"] and added["series"]["name"] == "魔法系"
    magic = added["series"]["id"]
    assert magic.startswith("custom-")

    r = mcp_server.thinking_words_edit("add_words", series_id=magic, words=[{"en": "lumos-ing…"}, {"en": "Accio-ing…", "zh": "答案飞来中"}, {"en": "accio-ing…"}])
    assert [w["en"] for w in r["series"]["words"]] == ["Lumos-ing…", "Accio-ing…"]
    assert "跳过" in r["summary"]

    r = mcp_server.thinking_words_edit("update_word", series_id=magic, en="accio-ing…", zh="飞来飞去中")
    assert r["series"]["words"][1] == {"en": "Accio-ing…", "zh": "飞来飞去中"}
    r = mcp_server.thinking_words_edit("delete_word", series_id=magic, en="Lumos-ing…")
    assert [w["en"] for w in r["series"]["words"]] == ["Accio-ing…"]
    r = mcp_server.thinking_words_edit("rename_series", series_id=magic, name="巫师系")
    assert r["series"]["name"] == "巫师系"
    r = mcp_server.thinking_words_edit("set_series_enabled", series_id="cooking", enabled=False)
    assert r["series"]["enabled"] is False
    r = mcp_server.thinking_words_edit("set_setting", setting="translate", value=False)
    assert r["ok"]
    assert mcp_server.thinking_words_edit("delete_series", series_id="cooking")["error"].startswith("内置")
    assert mcp_server.thinking_words_edit("delete_series", series_id=magic)["ok"]

    view = mcp_server.thinking_words_view()
    assert view["updated_by"] == "mcp"
    assert view["data"]["settings"]["translate"] is False
    assert [s["id"] for s in view["data"]["series"]] == ["cooking", "custom-cat"]
    assert view["version"] == 1 + 8


def test_mcp_edit_reports_mistakes_plainly():
    thinking_words.save_library(library(), 0, by="web")
    assert "没有这个系列" in mcp_server.thinking_words_edit("add_words", series_id="nope", words=[{"en": "x"}])["error"]
    assert "没有这个词" in mcp_server.thinking_words_edit("delete_word", series_id="cooking", en="Nope…")["error"]
    assert "action" in mcp_server.thinking_words_edit("explode")["error"]
    assert "setting" in mcp_server.thinking_words_edit("set_setting", setting="colour", value=True)["error"]
    assert thinking_words.get_library()["version"] == 1
