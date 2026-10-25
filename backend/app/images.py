"""MCP image bridge and scoped, expiring original-image links. No memory writes."""

import base64
import hashlib
import hmac
import json
import os
import time
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
import httpx
from mcp.types import CallToolResult, ImageContent, TextContent

router = APIRouter()
LINK_SECONDS = 24 * 60 * 60


def config():
    token = os.environ.get("IMAGE_WORKER_TOKEN", "")
    url = os.environ.get("IMAGE_WORKER_URL", "").rstrip("/")
    return url, token


def client():
    url, token = config()
    return httpx.AsyncClient(base_url=url, headers={"Authorization": "Bearer " + token},
                             timeout=httpx.Timeout(25, connect=3), follow_redirects=False)


def valid_id(value):
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


async def request(method, path, **kwargs):
    url, token = config()
    if not url or len(token) < 32:
        return {"error": "生图服务尚未配置，请让轩完成生图 worker 的安装和订阅登录。"}
    try:
        async with client() as http:
            response = await http.request(method, path, **kwargs)
        if response.status_code >= 500:
            return {"error": "生图服务暂时不可用，已有任务不会因查进度而重新提交。"}
        if response.status_code in (400, 404, 409, 429):
            return {"error": response.json().get("detail", "画图请求未完成。")}
        if response.status_code != 200:
            return {"error": "生图服务连接配置有误，请让轩检查内部连接。"}
        return response.json()
    except (httpx.HTTPError, ValueError):
        return {"error": "暂时连不上生图服务；提交结果可能尚未返回，请先查最近任务，不要换标记重复提交。"}


async def generate(prompt, request_id=""):
    result = await request("POST", "/jobs", json={"prompt": prompt, "request_id": request_id})
    if "job" in result:
        result["message"] = "任务已存在，继续查进度，不会重复生成。" if result.get("reused") else "已排队；用 image_status 查进度，完成后用 image_get 取图。不要反复提交。"
    return result


async def status(job_id="", wait_seconds=15):
    if not job_id:
        return await request("GET", "/jobs")
    if not valid_id(job_id):
        return {"error": "画图任务编号无效，请使用提交时返回的 id。"}
    return await request("GET", "/jobs/" + job_id, params={"wait": max(0, min(wait_seconds, 20))})


def link_signature(job_id, expires):
    _, token = config()
    if len(token) < 32:
        raise ValueError("image service not configured")
    message = f"ember-image-download-v1\n{job_id}\n{expires}".encode()
    return hmac.new(token.encode(), message, hashlib.sha256).hexdigest()


def download_url(job_id):
    host = os.environ.get("PUBLIC_HOST", "ember.cloudxuan1.com")
    expires = int(time.time()) + LINK_SECONDS
    return f"https://{host}/images/{job_id}?expires={expires}&signature={link_signature(job_id, expires)}"


async def image_bytes(job_id, preview=False):
    url, token = config()
    if not url or len(token) < 32:
        raise ValueError("生图服务尚未配置。")
    limit = 1024 * 1024 if preview else 21 * 1024 * 1024
    data = bytearray()
    try:
        async with client() as http:
            async with http.stream("GET", f"/jobs/{job_id}/image", params={"preview": str(preview).lower()}) as response:
                if response.status_code != 200:
                    raise ValueError("图片还没准备好或暂时取不到，请先查任务状态。")
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > limit:
                        raise ValueError("图片过大，无法直接交回。")
                    data.extend(chunk)
    except httpx.HTTPError:
        raise ValueError("暂时取不到图片，请稍后用同一任务编号取图；不要重新生成。") from None
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("服务没有交回有效 PNG 图片。")
    return bytes(data)


async def get_image(job_id):
    result = await status(job_id, wait_seconds=0)
    job = result.get("job")
    if not job or job["status"] != "succeeded":
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                              isError=bool("error" in result or job and job["status"] == "failed"))
    try:
        preview = await image_bytes(job_id, preview=True)
        url = download_url(job_id)
    except ValueError as error:
        return CallToolResult(content=[TextContent(type="text", text=str(error))], isError=True)
    return CallToolResult(content=[
        TextContent(type="text", text=json.dumps({"job": job, "download_url": url,
                    "message": "图片已生成，附带预览。请把原图链接发给轩；链接 24 小时有效，过期后用同一任务重新取链接，无需重画。"}, ensure_ascii=False)),
        ImageContent(type="image", mimeType="image/png", data=base64.b64encode(preview).decode(),
                     annotations={"audience": ["user", "assistant"]}),
    ])


@router.get("/images/{job_id}")
async def download(job_id: str, expires: int, signature: str):
    try:
        expected = link_signature(job_id, expires)
    except ValueError:
        raise HTTPException(status_code=403, detail="图片链接无效。") from None
    if not valid_id(job_id) or expires < time.time() or not hmac.compare_digest(signature.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="图片链接无效或已过期，请让小克用原任务重新取图。")
    try:
        data = await image_bytes(job_id)
    except ValueError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    return Response(content=data, media_type="image/png", headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'inline; filename="ember-image-{job_id}.png"',
    })
