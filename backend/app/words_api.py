"""chat-lite 思考彩蛋词库的网页读写接口（/internal/words/*）。

钥匙：沿用 chat-lite 的 EMBER_READ_TOKEN（轩 2026-09-27 拍板复用，免得再配一把）。
这把钥匙因此多了「读写词库」的权限，但词库和记忆是两张不相干的表，记忆仍然只读。
和记忆接口一样只收 POST（chat-lite Worker 那边统一按 POST 转发）。
"""

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import thinking_words
from app.chat_api import _require_token

router = APIRouter(prefix="/internal/words")


class SaveBody(BaseModel):
    base_version: int = Field(ge=0)
    data: dict


@router.post("/get")
def words_get(authorization: str | None = Header(default=None)) -> dict:
    _require_token(authorization)
    return thinking_words.get_library()


@router.post("/save")
def words_save(body: SaveBody, authorization: str | None = Header(default=None)):
    _require_token(authorization)
    try:
        return thinking_words.save_library(body.data, body.base_version, by="web")
    except thinking_words.VersionConflict as conflict:
        # 409 带上云端现在的样子，网页据此弹「用云端的 / 用我的覆盖」
        return JSONResponse(status_code=409, content={"detail": "词库已被另一端修改", "current": conflict.current})
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err))
