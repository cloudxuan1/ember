import hashlib
from base64 import urlsafe_b64encode

import pytest
from fastapi.testclient import TestClient

from app.main import app

VERIFIER = "wiz" * 15  # PKCE code_verifier（43+ 字符）
CHALLENGE = urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


# mcp 的 StreamableHTTPSessionManager 每个实例只能 run 一次，
# 所以整个模块共用一个 TestClient（lifespan 只进出一次）。
@pytest.fixture(scope="module")
def client(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("EMBER_DB", str(tmp_path_factory.mktemp("db") / "test.db"))
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:  # Host 需过 mcp 的白名单
        yield c
    mp.undo()


@pytest.fixture
def gated(client, monkeypatch):
    """开启门禁的客户端。"""
    monkeypatch.setenv("EMBER_OAUTH_PASSWORD", "开门")
    monkeypatch.setenv("EMBER_OAUTH_ACCESS_TOKEN", "token-abc")
    monkeypatch.setenv("EMBER_OAUTH_REFRESH_TOKEN", "refresh-xyz")
    return client


def _full_flow_code(c: TestClient) -> tuple[str, str]:
    """注册客户端 → 授权页输对口令 → 返回 (client_id, 授权码)。"""
    reg = c.post("/oauth/register", json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]})
    assert reg.status_code == 201
    client_id = reg.json()["client_id"]

    resp = c.post(
        "/oauth/authorize",
        data={
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "response_type": "code",
            "state": "s1",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "password": "开门",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://claude.ai/api/mcp/auth_callback?")
    assert "state=s1" in location
    code = location.split("code=")[1].split("&")[0]
    return client_id, code


def test_discovery_endpoints(client):
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["registration_endpoint"].endswith("/oauth/register")
    assert "S256" in meta["code_challenge_methods_supported"]
    # claude.ai 会带路径后缀探测
    assert client.get("/.well-known/oauth-protected-resource/mcp-xyz").status_code == 200
    assert client.get("/.well-known/openid-configuration").status_code == 200


def test_authorize_rejects_wrong_password_and_bad_client(gated):
    reg = gated.post("/oauth/register", json={"redirect_uris": ["https://claude.ai/cb"]})
    client_id = reg.json()["client_id"]
    base = {
        "client_id": client_id,
        "redirect_uri": "https://claude.ai/cb",
        "response_type": "code",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
    }
    wrong = gated.post("/oauth/authorize", data={**base, "password": "猜的"})
    assert wrong.status_code == 403
    evil = gated.post("/oauth/authorize", data={**base, "redirect_uri": "https://evil.com/cb", "password": "开门"})
    assert evil.status_code == 400


def test_full_flow_and_token_exchange(gated):
    _, code = _full_flow_code(gated)

    bad = gated.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": "错的" * 15})
    assert bad.json()["error"] == "invalid_grant"  # 授权码一次性，验证失败即作废

    _, code2 = _full_flow_code(gated)
    ok = gated.post("/oauth/token", data={"grant_type": "authorization_code", "code": code2, "code_verifier": VERIFIER})
    assert ok.status_code == 200
    assert ok.json()["access_token"] == "token-abc"

    refreshed = gated.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": "refresh-xyz"})
    assert refreshed.json()["access_token"] == "token-abc"


INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def test_mcp_open_when_gate_disabled(client):
    resp = client.post("/mcp/", json=INIT, headers=MCP_HEADERS)
    assert resp.status_code == 200
    # 有状态 SSE 模式：claude.ai 认这个组合（memory 同款），无状态 JSON 它不认
    assert "mcp-session-id" in resp.headers
    assert resp.headers["content-type"].startswith("text/event-stream")


def test_mcp_requires_bearer_when_gated(gated):
    resp = gated.post("/mcp/", json=INIT, headers=MCP_HEADERS)
    assert resp.status_code == 401
    assert "oauth-protected-resource" in resp.headers["WWW-Authenticate"]

    ok = gated.post("/mcp/", json=INIT, headers={**MCP_HEADERS, "Authorization": "Bearer token-abc"})
    assert ok.status_code == 200

    bad = gated.post("/mcp/", json=INIT, headers={**MCP_HEADERS, "Authorization": "Bearer wrong-token"})
    assert bad.status_code == 401
