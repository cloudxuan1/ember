import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI

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


app = FastAPI(title="ember", version="0.2.0", lifespan=lifespan)
app.mount(f"/{MCP_PATH}", mcp.streamable_http_app())


@app.get("/health")
def health():
    return {
        "name": "ember",
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
    }
