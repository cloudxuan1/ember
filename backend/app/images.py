"""MCP image bridge and scoped, expiring original-image links. No memory writes.

Also the gallery (#41): list / ⭐ / recycle bin for 小克's tools and the web page. The images, their records and
the recycle bin all live in the image worker's volume; this module only forwards and words the answers."""

import base64
from datetime import datetime, timedelta, timezone
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
WAIT_LIMIT = 50  # image_status 一次最多等这么久；worker 那边同一个上限。60 实测会被 MCP 客户端 60 秒超时掐断
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
BEIJING = timezone(timedelta(hours=8))  # 列表上的日子按北京时间（跟记忆库一样）；worker 存的是 UTC
LIST_PAGE_MAX = 20  # image_list 一页最多几条
TRASH_PLACE = "网页「图片 → 回收站」"


class ImageGone(ValueError):
    """这张图已经从回收站彻底删除：文件没了，任务记录还在。"""


def bj(stamp, with_time=True):
    """worker 存的 UTC 时间 → 北京时间 '2026-11-07 14:09'（with_time=False 只要日子）。"""
    try:
        moment = datetime.fromisoformat(stamp).astimezone(BEIJING)
    except (TypeError, ValueError):
        return ""
    return moment.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def image_type(data):
    """(media type, extension) from the bytes. 原图默认 PNG，要了 jpeg / webp 的就是那种；预览是 JPEG（改版前的是 PNG）。"""
    if data.startswith(PNG_MAGIC):
        return "image/png", "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None, None


def config():
    token = os.environ.get("IMAGE_WORKER_TOKEN", "")
    url = os.environ.get("IMAGE_WORKER_URL", "").rstrip("/")
    return url, token


def client():
    url, token = config()
    return httpx.AsyncClient(base_url=url, headers={"Authorization": "Bearer " + token},
                             timeout=httpx.Timeout(WAIT_LIMIT + 15, connect=3), follow_redirects=False)


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
        if response.status_code == 422:
            return {"error": "画图请求不合规：提示词要 1–8000 字，request_id 最多 128 字。改好后再提交，任务没有排队。"}
        if response.status_code != 200:
            return {"error": "生图服务连接配置有误，请让轩检查内部连接。"}
        return response.json()
    except (httpx.HTTPError, ValueError):
        return {"error": "暂时连不上生图服务；提交结果可能尚未返回，请先查最近任务，不要换标记重复提交。"}


async def generate(prompt, request_id="", model="", reference_job_ids=None, options=None, n=1):
    body = {"prompt": prompt, "request_id": request_id}
    # Without model / references / parameters the request is byte-for-byte the original Codex submission.
    if model:
        body["model"] = model
    if reference_job_ids:
        if not isinstance(reference_job_ids, list) or not all(valid_id(r) for r in reference_job_ids):
            return {"error": "reference_job_ids 要用之前画图任务返回的 id 列表。"}
        body["reference_job_ids"] = reference_job_ids
    # 取值对不对、模型能不能用，由 worker 按它的型号名单判断。
    body.update({name: value for name, value in (options or {}).items() if value is not None and value != ""})
    if n != 1:
        body["n"] = n
    result = await request("POST", "/jobs", json=body)
    if "job" in result and len(result.get("jobs") or ()) > 1:
        result["job_ids"] = [job["id"] for job in result["jobs"]]
        count = len(result["job_ids"])
        result["message"] = (f"这 {count} 张的任务都已存在，继续查进度，不会重复生成。" if result.get("reused") else
                             f"已排队 {count} 张，一张一个任务（job_ids）；用 image_status 分别查进度，画好的用 image_get 分别取图。不要反复提交。")
    elif "job" in result:
        result["message"] = "任务已存在，继续查进度，不会重复生成。" if result.get("reused") else "已排队；用 image_status 查进度，完成后用 image_get 取图。不要反复提交。"
    if "job" in result and result.get("ignored"):
        result["message"] += "这些参数这次用不了，已忽略、没发给模型：" + "；".join(result["ignored"]) + "。"
    binned = [job for job in result.get("jobs") or [result.get("job") or {}] if job.get("trashed_at") or job.get("purged_at")]
    if binned:  # 复用到的老任务，图已经进了回收站或彻底删了：别让人以为马上会有新图
        result["message"] += (f"注意：复用到的任务里有 {len(binned)} 张图在回收站或已彻底删除，这次没有重画；"
                              "轩要重画就换一个新的 request_id 再提交。")
    return result


async def status(job_id="", wait_seconds=15, full_prompt=False):
    if not job_id:
        return await request("GET", "/jobs")
    if not valid_id(job_id):
        return {"error": "画图任务编号无效，请使用提交时返回的 id。"}
    params = {"wait": max(0, min(wait_seconds, WAIT_LIMIT))}
    if full_prompt:
        params["full_prompt"] = "true"
    return await request("GET", "/jobs/" + job_id, params=params)


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


async def _fetch(path, params, limit):
    url, token = config()
    if not url or len(token) < 32:
        raise ValueError("生图服务尚未配置。")
    data = bytearray()
    try:
        async with client() as http:
            async with http.stream("GET", path, params=params) as response:
                if response.status_code == 410:
                    raise ImageGone("这张图已经彻底删除了，原图和预览都没了。")
                if response.status_code != 200:
                    raise ValueError("图片还没准备好或暂时取不到，请先查任务状态。")
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > limit:
                        raise ValueError("图片过大，无法直接交回。")
                    data.extend(chunk)
    except httpx.HTTPError:
        raise ValueError("暂时取不到图片，请稍后用同一任务编号取图；不要重新生成。") from None
    return bytes(data)


async def image_bytes(job_id, preview=False):
    data = await _fetch(f"/jobs/{job_id}/image", {"preview": str(preview).lower()},
                        1024 * 1024 if preview else 21 * 1024 * 1024)
    kind, _ = image_type(data)
    if kind is None or (preview and kind == "image/webp"):
        raise ValueError("服务没有交回有效图片。")
    return data


async def thumb_bytes(job_id):
    """网页网格用的小图（worker 从预览缩出来的 JPEG）。"""
    data = await _fetch(f"/jobs/{job_id}/thumb", None, 512 * 1024)
    if image_type(data)[0] != "image/jpeg":
        raise ValueError("服务没有交回有效图片。")
    return data


def purged_reply(job):
    """image_get 碰上已经彻底删除的图：说清楚，带上任务记录和完整提示词，不当成出错。"""
    day = bj(job.get("purged_at"), with_time=False)
    message = ((f"这张图 {day} 已经彻底删除了" if day else "这张图刚被彻底删除了") +
               "，原图和预览都没了，取不到；任务记录还在，job.prompt 是当时的完整提示词。"
               "轩要重画就照着它再提交一次，换一个新的 request_id。")
    return CallToolResult(content=[TextContent(type="text", text=json.dumps({"job": job, "message": message}, ensure_ascii=False))])


async def get_image(job_id):
    result = await status(job_id, wait_seconds=0, full_prompt=True)
    job = result.get("job")
    if job and job.get("purged_at"):
        return purged_reply(job)
    if not job or job["status"] != "succeeded":
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                              isError=bool("error" in result or job and job["status"] == "failed"))
    try:
        preview = await image_bytes(job_id, preview=True)
        url = download_url(job_id)
    except ImageGone:  # 查完状态、取图之前，正好被彻底删了
        return purged_reply(job)
    except ValueError as error:
        return CallToolResult(content=[TextContent(type="text", text=str(error))], isError=True)
    message = "图片已生成，附带预览。请把原图链接发给轩；链接 24 小时有效，过期后用同一任务重新取链接，无需重画。"
    if job.get("trashed_at"):
        message = (f"这张在回收站里（{bj(job['trashed_at'], False)} 放进去的，{bj(job.get('purge_at'), False)} 自动彻底删除），"
                   f"还能看、能下原图，附带预览，原图链接 24 小时有效；轩要留着就在{TRASH_PLACE}里恢复。")
    if job.get("engine") == "openrouter":
        message += f"这张是 {job.get('model_label')} 画的（OpenRouter），交图时顺口告诉轩。"
    return CallToolResult(content=[
        TextContent(type="text", text=json.dumps({"job": job, "download_url": url, "message": message}, ensure_ascii=False)),
        ImageContent(type="image", mimeType="image/png" if preview.startswith(PNG_MAGIC) else "image/jpeg",
                     data=base64.b64encode(preview).decode(),
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
    except ImageGone as error:
        raise HTTPException(status_code=410, detail=str(error)) from None
    except ValueError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    return original_response(job_id, data)


def original_response(job_id, data):
    media_type, extension = image_type(data)
    return Response(content=data, media_type=media_type, headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'inline; filename="ember-image-{job_id}.{extension}"',
    })


# ---------- 图片管理：列、⭐、回收站（#41） ----------
# 网页和小克的工具都走这里；真正的核对（⭐ 的不能删、排队任务的参考图不能删、只从回收站彻底删）在 worker 里，
# 每次都在同一个写事务里重读一遍，所以页面开着的时候小克改了什么，也不会被旧页面冲掉。


async def manage(method, path, **kwargs):
    """调 worker 的图片管理接口 → (HTTP 状态, JSON)。被拒的带上 worker 的原因和 code；
    worker 那把内部口令不对时不回 401（网页会以为登录过期一直刷新），回 503。"""
    url, token = config()
    if not url or len(token) < 32:
        return 503, {"error": "生图服务尚未配置，图片管理暂时用不了。"}
    try:
        async with client() as http:
            response = await http.request(method, path, **kwargs)
    except httpx.HTTPError:
        return 503, {"error": "暂时连不上生图服务，稍后再试。"}
    status_code = response.status_code
    if status_code >= 500:
        return 503, {"error": "生图服务暂时不可用，稍后再试。"}
    if status_code == 401:
        return 503, {"error": "生图服务连接配置有误，请让轩检查内部连接。"}
    try:
        data = response.json()
    except ValueError:
        return 502, {"error": "生图服务回的内容看不懂，稍后再试。"}
    if status_code == 200:
        return 200, data
    detail = data.get("detail") if isinstance(data, dict) else None
    if isinstance(detail, dict) and detail.get("message"):
        return status_code, {"error": detail["message"], "code": detail.get("code")}
    if isinstance(detail, str):
        return status_code, {"error": detail}
    return 400, {"error": "参数不对，没办成。"}


def bad_id():
    return 404, {"error": "图片编号不对，请用列表里的 id。", "code": "not_found"}


async def gallery(page=1, page_size=20, starred=None, trash=False, date_from="", date_to=""):
    """画好的图，新的在前：没进回收站的，或 trash=True 时回收站里的。starred None = 有没有⭐都要。"""
    params = {"page": page, "page_size": page_size, "trash": "true" if trash else "false"}
    if starred is not None:
        params["starred"] = "true" if starred else "false"
    for name, value in (("date_from", date_from), ("date_to", date_to)):
        if value:
            params[name] = value
    return await manage("GET", "/gallery", params=params)


async def gallery_stats():
    return await manage("GET", "/gallery/stats")


async def job_detail(job_id):
    if not valid_id(job_id):
        return bad_id()
    return await manage("GET", f"/jobs/{job_id}", params={"full_prompt": "true"})


async def set_star(job_id, starred):
    if not valid_id(job_id):
        return bad_id()
    return await manage("POST", f"/jobs/{job_id}/star", json={"starred": bool(starred)})


async def trash(ids):
    return await manage("POST", "/gallery/trash", json={"ids": list(ids)})


async def restore(job_id):
    if not valid_id(job_id):
        return bad_id()
    return await manage("POST", f"/jobs/{job_id}/restore")


async def purge(job_id):
    if not valid_id(job_id):
        return bad_id()
    return await manage("POST", f"/jobs/{job_id}/purge")


async def empty_trash(until):
    return await manage("POST", "/gallery/empty", json={"until": until})


# 小克那边的说法（网页直接用 worker 的原因）。
KE_REFUSALS = {
    "not_found": "找不到这张图，编号不对；用 image_list 里的 id。",
    "no_image": "这个任务没有画好的图（还在排队、正在画，或者没画成），没有可删的。",
    "starred": "这张点了⭐，删不了。⭐ = 要留着：先问轩，她说不要了，再用 image_star 取消⭐后删。",
    "in_use": "这张正被排队 / 正在画的任务当参考图，等那张画完再删。",
    "purged": "这张已经彻底删除了，不用再删。",
}


def short(text, n):
    text = " ".join(str(text or "").split())
    return text[:n] + ("…" if len(text) > n else "")


def listed(job):
    """image_list 的一条：只有文字，没有预览。"""
    item = {"id": job["id"], "date": bj(job.get("created_at")), "model": job.get("model_label"),
            "prompt": short(job.get("prompt_preview"), 30), "starred": bool(job.get("starred")),
            "in_trash": "trashed_at" in job}
    if job.get("purge_at"):
        item["purge_on"] = bj(job["purge_at"], with_time=False)
    return item


async def list_images(page=1, page_size=10, date_from="", date_to="", starred=None, in_trash=False):
    page_size = max(1, min(int(page_size), LIST_PAGE_MAX))
    status_code, data = await gallery(max(1, int(page)), page_size, starred, in_trash, date_from, date_to)
    if status_code != 200:
        return {"error": data["error"]}
    return {"total": data["total"], "page": data["page"], "page_size": data["page_size"], "has_more": data["has_more"],
            "today": datetime.now(BEIJING).strftime("%Y-%m-%d"), "items": [listed(job) for job in data["items"]]}


async def delete_image(job_id):
    """小克的 image_delete：只进回收站，不碰文件。"""
    if not valid_id(job_id):
        return {"error": KE_REFUSALS["not_found"]}
    status_code, data = await trash([job_id])
    if status_code != 200:
        return {"error": data["error"]}
    result = data["results"][0]
    if not result["ok"]:
        return {"error": KE_REFUSALS.get(result["code"], result["message"]), "code": result["code"]}
    day = bj(result["job"].get("purge_at"), with_time=False)
    if result["status"] == "already":
        return {"job_id": job_id, "status": "already_in_trash", "purge_on": day,
                "message": f"这张本来就在回收站里，{day} 自动彻底删除。"}
    return {"job_id": job_id, "status": "trashed", "purge_on": day,
            "message": f"放进回收站了，文件还在：{day} 之前轩能在{TRASH_PLACE}里恢复，到那天自动彻底删除。"}


async def star_image(job_id, starred):
    """小克的 image_star。"""
    if not valid_id(job_id):
        return {"error": KE_REFUSALS["not_found"]}
    status_code, data = await set_star(job_id, starred)
    if status_code != 200:
        if data.get("code") == "trashed":
            return {"error": f"这张在回收站里，不能点⭐；要留着先请轩在{TRASH_PLACE}里恢复。", "code": "trashed"}
        return {"error": data["error"], **({"code": data["code"]} if data.get("code") else {})}
    on = data["job"]["starred"]
    return {"job_id": job_id, "starred": on,
            "message": "点上⭐了：这张会留着，删不了（要删得先取消⭐）。" if on else "取消⭐了。"}
