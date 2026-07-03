import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import oauth
from app.db import init_db
from app.mcp_server import mcp

# MCP 挂载路径。公网部署时在 .env 里设 MCP_PATH=mcp-<随机串> 当能力密钥用，
# 因为 claude.ai 自定义 connector 不支持自定义请求头。
MCP_PATH = os.environ.get("MCP_PATH", "mcp").strip("/")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="ember", version="0.3.0", lifespan=lifespan)
app.include_router(oauth.router)
app.mount(f"/{MCP_PATH}", mcp.streamable_http_app())
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://claude.ai", "https://claude.com", "https://chatgpt.com"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["WWW-Authenticate"],
)


@app.get("/health")
def health():
    return {
        "name": "ember",
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
    }


class _MCPPathFix:
    """Rewrite /<MCP_PATH> → /<MCP_PATH>/ in-process so Claude doesn't get 307."""

    def __init__(self, inner, prefix: str) -> None:
        self.inner = inner
        self.prefix = f"/{prefix}"

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("path") == self.prefix:
            scope = {**scope, "path": self.prefix + "/"}
        await self.inner(scope, receive, send)


class _BearerGuard:
    """MCP 端点的 Bearer 校验（OAuth 门禁第三层，见 app/oauth.py）。

    门禁开着（oauth_enabled）时，/<MCP_PATH>/* 必须带有效 token，
    否则按 MCP 规范回 401 + WWW-Authenticate 指向资源元数据，
    claude.ai 收到后会自动走 OAuth 授权流程。
    健康检查和 OAuth 自身的端点不在此列。
    """

    def __init__(self, inner, prefix: str) -> None:
        self.inner = inner
        self.prefix = f"/{prefix}"

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope["type"] == "http"
            and (scope["path"] == self.prefix or scope["path"].startswith(self.prefix + "/"))
            and oauth.oauth_enabled()
        ):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            if not oauth.valid_bearer(headers.get("authorization")):
                body = json.dumps({"error": "unauthorized"}).encode()
                extra = oauth.unauthorized_response_headers()
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        *((k.encode(), v.encode()) for k, v in extra.items()),
                    ],
                })
                await send({"type": "http.response.body", "body": body})
                return
        await self.inner(scope, receive, send)


app = _MCPPathFix(_BearerGuard(app, MCP_PATH), MCP_PATH)
