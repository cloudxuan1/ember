import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import oauth, review
from app.db import init_db
from app.mcp_server import mcp

# 自托管静态资源（审核台字体）。路径相对本文件定位，不依赖 cwd。
# backend/app/main.py → backend/static
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

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
app.include_router(review.router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
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
    """Rewrite /<MCP_PATH> → /<MCP_PATH>/ in-process so Claude doesn't get 307.

    注意：有状态 SSE 模式下 GET 是合法的服务器推送流入口，不要拦 GET。
    """

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


class _WireLog:
    """临时排查用：把 MCP / OAuth 相关请求的关键头和响应码打进日志。

    只记录调试必需的头（Origin / UA / Accept / 会话 / 协议版本），不记请求体。
    connector 联调稳定后可移除。
    """

    WATCH_HEADERS = (b"origin", b"user-agent", b"accept", b"content-type",
                     b"mcp-session-id", b"mcp-protocol-version", b"authorization")

    def __init__(self, inner, prefix: str) -> None:
        self.inner = inner
        self.prefix = f"/{prefix}"

    BODY_PATHS = ("/oauth/register", "/oauth/token")
    SECRET_FIELDS = ("client_secret", "code", "code_verifier", "refresh_token", "access_token")

    @classmethod
    def _redact(cls, body: str) -> str:
        import re

        for f in cls.SECRET_FIELDS:
            body = re.sub(rf'("{f}"\s*:\s*")[^"]{{6,}}(")', r"\1<redacted>\2", body)
            body = re.sub(rf"({f}=)[^&]{{6,}}", r"\1<redacted>", body)
        return body[:600]

    async def __call__(self, scope, receive, send) -> None:
        interesting = (
            scope["type"] == "http"
            and (scope["path"].startswith(self.prefix)
                 or scope["path"].startswith("/.well-known")
                 or scope["path"].startswith("/oauth"))
        )
        if not interesting:
            await self.inner(scope, receive, send)
            return
        headers = []
        for k, v in scope.get("headers", []):
            if k in self.WATCH_HEADERS:
                value = "<redacted>" if k == b"authorization" else v.decode("latin-1")
                headers.append(f"{k.decode()}={value}")

        # register/token 的请求体先吸进来拍照（脱敏），再回放给应用
        if scope["path"] in self.BODY_PATHS and scope["method"] == "POST":
            chunks = []
            while True:
                message = await receive()
                chunks.append(message)
                if not message.get("more_body"):
                    break
            body = b"".join(c.get("body", b"") for c in chunks).decode("utf-8", "replace")
            print(f"[wire-body] {scope['path']} ← {self._redact(body)}", flush=True)
            replay = iter(chunks)

            async def receive():  # noqa: F811 —— 回放缓冲
                return next(replay)

        status_box = {}

        async def send_logged(message):
            if message["type"] == "http.response.start":
                status_box["status"] = message["status"]
            await send(message)

        await self.inner(scope, receive, send_logged)
        print(f"[wire] {scope['method']} {scope['path']} → {status_box.get('status', '?')} | {' | '.join(headers)}", flush=True)


app = _WireLog(_MCPPathFix(_BearerGuard(app, MCP_PATH), MCP_PATH), MCP_PATH)
