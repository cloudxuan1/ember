"""chat-lite 专用的窄只读接口（2026-09 与 Codex 讨论稿定的第一阶段）。

为什么不让 chat-lite 的 Worker 直接当 MCP 客户端：MCP 端点是有状态 SSE 模式（claude.ai
只认这种），握手和流解析对一个 Cloudflare Worker 来说太重；这里给它三条普通 HTTP，
复用 briefing / search / recall 的现有业务函数，不碰数据库结构，不动 MCP 和 OAuth。

鉴权：Bearer EMBER_READ_TOKEN，与 MCP 的 OAuth token、审核台的 EMBER_REVIEW_TOKEN 刻意分开
（三把钥匙互不通用）。没配 token 时整组接口关闭（503），错 token 401。

只读：这组接口不写正库；memory_briefing 会写自己的冷却记录（briefing_log），这是它本来的行为。
"""

import hmac
import os

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app import briefing, memories

router = APIRouter(prefix="/internal/memory")

QUERY_MAX_CHARS = 500
TOPIC_MAX_CHARS = 500
SPACE_MAX_CHARS = 40
LIMIT_MAX = 8


def _read_token() -> str:
    return os.environ.get("EMBER_READ_TOKEN", "")


def _require_token(authorization: str | None) -> None:
    token = _read_token()
    if not token:
        raise HTTPException(status_code=503, detail="chat 只读接口未启用（EMBER_READ_TOKEN 未配置）")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="unauthorized")
    if not hmac.compare_digest(authorization[7:].strip().encode(), token.encode()):
        raise HTTPException(status_code=401, detail="unauthorized")


class BriefingBody(BaseModel):
    topic: str | None = Field(default=None, max_length=TOPIC_MAX_CHARS)


class SearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=QUERY_MAX_CHARS)
    space: str | None = Field(default=None, max_length=SPACE_MAX_CHARS)
    limit: int = Field(default=LIMIT_MAX, ge=1, le=LIMIT_MAX)


class RecallBody(BaseModel):
    id: int = Field(ge=1)


@router.post("/briefing")
def chat_briefing(body: BriefingBody, authorization: str | None = Header(default=None)) -> dict:
    _require_token(authorization)
    topic = (body.topic or "").strip() or None
    return briefing.build_briefing(topic=topic)


@router.post("/search")
def chat_search(body: SearchBody, authorization: str | None = Header(default=None)) -> dict:
    _require_token(authorization)
    query = body.query.strip()
    if not query:
        raise HTTPException(status_code=422, detail="query 不能为空")
    space = (body.space or "").strip() or None
    results = memories.search_memories(query, space=space, limit=body.limit)
    return {"count": len(results), "results": results}


@router.post("/recall")
def chat_recall(body: RecallBody, authorization: str | None = Header(default=None)) -> dict:
    _require_token(authorization)
    memory = memories.get_memory(body.id)
    if memory is None:
        raise HTTPException(status_code=404, detail=f"记忆 {body.id} 不存在")
    return memory
