"""图片管理（#41）的后端这一头：小克的 image_list / image_delete / image_star、image_get 碰上回收站 / 彻底删除，
网页的 /api/images/*。worker 用假的（httpx.MockTransport）；真 worker 连着测的在 image_worker/tests/test_gallery_end_to_end.py。"""

import asyncio
import base64
import json
from urllib.parse import urlparse
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from app import console, images, mcp_server

JOB = str(uuid.uuid4())
OTHER = str(uuid.uuid4())
JPEG = b"\xff\xd8\xff\xe0synthetic-jpeg"
PNG = b"\x89PNG\r\n\x1a\nsynthetic-png"


def job(**extra):
    base = {"id": JOB, "status": "succeeded", "created_at": "2026-11-07T06:09:12.345+00:00", "engine": "codex_subscription",
            "model": None, "model_label": "Codex 订阅（gpt-image-2）", "prompt_preview": "编的：雨里撑伞的橘猫",
            "starred": False, "width": 1024, "height": 1024, "image_bytes": 2_000_000}
    return {**base, **extra}


BINNED = {"trashed_at": "2026-11-07T06:30:00.000+00:00", "purge_at": "2026-12-07T06:30:00.000+00:00"}


@pytest.fixture
def worker(monkeypatch):
    """A fake worker: routes[(method, path)] = function(request) → httpx.Response. Every request is kept in calls."""
    monkeypatch.setenv("IMAGE_WORKER_URL", "http://worker")
    monkeypatch.setenv("IMAGE_WORKER_TOKEN", "synthetic-internal-token-" + "x" * 32)
    state = {"calls": [], "routes": {}}

    def handle(request):
        state["calls"].append(request)
        route = state["routes"].get((request.method, request.url.path))
        if route is None:
            return httpx.Response(404, json={"detail": {"code": "not_found", "message": "找不到这张图，编号不对。"}})
        return route(request)

    monkeypatch.setattr(images, "client", lambda: httpx.AsyncClient(base_url="http://worker", transport=httpx.MockTransport(handle)))
    return state


def reply(payload, status=200):
    return lambda request: httpx.Response(status, json=payload)


def body(request):
    return json.loads(request.content)


# ---------- 小克的工具 ----------


def test_the_three_gallery_tools_are_registered_with_their_parameters():
    tools = {tool.name: tool for tool in asyncio.run(mcp_server.mcp.list_tools())}
    assert {"image_list", "image_delete", "image_star", "image_generate", "image_status", "image_get"} <= tools.keys()
    assert set(tools["image_list"].inputSchema["properties"]) == {"page", "page_size", "date_from", "date_to", "starred", "in_trash"}
    assert tools["image_star"].inputSchema["required"] == ["job_id", "starred"]
    assert tools["image_delete"].inputSchema["required"] == ["job_id"]
    doc = mcp_server.image_list.__doc__
    assert "默认 10 条，最多 20 条" in doc and "has_more" in doc and "今天没点⭐的" in doc and "image_get" in doc
    assert "先问轩" in mcp_server.image_delete.__doc__ and "30 天" in mcp_server.image_delete.__doc__
    assert "回收站" in mcp_server.image_get.__doc__ and "彻底删除" in mcp_server.image_status.__doc__


def test_image_list_forwards_filters_and_caps_a_page_at_twenty(worker):
    worker["routes"][("GET", "/gallery")] = reply({"items": [], "total": 0, "page": 1, "page_size": 10, "has_more": False})
    asyncio.run(mcp_server.image_list())
    assert dict(worker["calls"][-1].url.params) == {"page": "1", "page_size": "10", "trash": "false"}
    asyncio.run(mcp_server.image_list(page=2, page_size=50, date_from="2026-11-07", date_to="2026-11-07", starred=False))
    assert dict(worker["calls"][-1].url.params) == {"page": "2", "page_size": "20", "trash": "false", "starred": "false",
                                                   "date_from": "2026-11-07", "date_to": "2026-11-07"}
    asyncio.run(mcp_server.image_list(page=0, page_size=0, starred=True, in_trash=True))
    assert dict(worker["calls"][-1].url.params) == {"page": "1", "page_size": "1", "trash": "true", "starred": "true"}


def test_image_list_items_are_short_text_in_beijing_time(worker):
    long_prompt = "编的：一只橘猫撑着透明雨伞走在下雨的石板路上，背景是黄昏的老街，水洼里有倒影，" * 2
    items = [job(prompt_preview=long_prompt[:80]),
             job(id=OTHER, created_at="2026-11-06T16:00:00.000+00:00", engine="openrouter", model="openai/gpt-image-2.6-flare",
                 model_label="Flare 2.6", prompt_preview="编的：短的\n第二行", starred=True, **BINNED)]
    worker["routes"][("GET", "/gallery")] = reply({"items": items, "total": 37, "page": 1, "page_size": 10, "has_more": True})
    result = asyncio.run(mcp_server.image_list())
    assert result["total"] == 37 and result["has_more"] is True and len(result["today"]) == 10
    first, second = result["items"]
    assert first == {"id": JOB, "date": "2026-11-07 14:09", "model": "Codex 订阅（gpt-image-2）",
                     "prompt": long_prompt[:30] + "…", "starred": False, "in_trash": False}
    assert second == {"id": OTHER, "date": "2026-11-07 00:00", "model": "Flare 2.6", "prompt": "编的：短的 第二行",
                      "starred": True, "in_trash": True, "purge_on": "2026-12-07"}


def test_image_list_reports_a_bad_date_instead_of_failing(worker):
    worker["routes"][("GET", "/gallery")] = reply({"detail": "日期「最近」看不懂：写成 2026-11-07"}, 400)
    assert asyncio.run(mcp_server.image_list(date_from="最近")) == {"error": "日期「最近」看不懂：写成 2026-11-07"}


def test_image_delete_puts_it_in_the_bin_and_says_until_when(worker):
    worker["routes"][("POST", "/gallery/trash")] = lambda request: httpx.Response(200, json={
        "results": [{"id": JOB, "ok": True, "status": "trashed", "job": job(**BINNED)}], "trashed": 1})
    result = asyncio.run(mcp_server.image_delete(JOB))
    assert body(worker["calls"][-1]) == {"ids": [JOB]}
    assert result["status"] == "trashed" and result["purge_on"] == "2026-12-07"
    assert "回收站" in result["message"] and "恢复" in result["message"] and "2026-12-07" in result["message"]


def test_image_delete_refusals_tell_ke_what_to_do(worker):
    outcomes = {"starred": "先问轩", "in_use": "等那张画完", "no_image": "没有画好的图", "purged": "不用再删"}
    for code, words in outcomes.items():
        worker["routes"][("POST", "/gallery/trash")] = reply({"results": [{"id": JOB, "ok": False, "code": code, "message": "x"}], "trashed": 0})
        result = asyncio.run(mcp_server.image_delete(JOB))
        assert result["code"] == code and words in result["error"]
    calls = len(worker["calls"])
    assert "编号不对" in asyncio.run(mcp_server.image_delete("../jobs.sqlite3"))["error"]
    assert len(worker["calls"]) == calls  # never reaches the worker


def test_image_star_sends_a_bool_and_a_binned_image_points_to_restore(worker):
    worker["routes"][("POST", f"/jobs/{JOB}/star")] = lambda request: httpx.Response(200, json={"job": job(starred=body(request)["starred"])})
    on = asyncio.run(mcp_server.image_star(JOB, True))
    assert body(worker["calls"][-1]) == {"starred": True} and on["starred"] is True and "删不了" in on["message"]
    assert asyncio.run(mcp_server.image_star(JOB, False))["starred"] is False
    worker["routes"][("POST", f"/jobs/{JOB}/star")] = reply({"detail": {"code": "trashed", "message": "x"}}, 409)
    refused = asyncio.run(mcp_server.image_star(JOB, True))
    assert refused["code"] == "trashed" and "回收站" in refused["error"] and "恢复" in refused["error"]


def test_image_get_still_shows_a_binned_image_and_says_so(worker):
    worker["routes"][("GET", f"/jobs/{JOB}")] = reply({"job": job(prompt="编的：完整提示词", **BINNED)})
    worker["routes"][("GET", f"/jobs/{JOB}/image")] = lambda request: httpx.Response(200, content=JPEG)
    result = asyncio.run(mcp_server.image_get(JOB))
    assert not result.isError
    image = next(c for c in result.content if c.type == "image")
    assert base64.b64decode(image.data) == JPEG
    meta = json.loads(result.content[0].text)
    assert "回收站" in meta["message"] and "2026-12-07" in meta["message"] and "恢复" in meta["message"]
    assert meta["download_url"]


def test_image_get_on_a_purged_image_explains_and_keeps_the_prompt(worker):
    worker["routes"][("GET", f"/jobs/{JOB}")] = reply({"job": job(prompt="编的：完整提示词", purged_at="2026-12-07T07:00:00.000+00:00")})
    result = asyncio.run(mcp_server.image_get(JOB))
    assert not result.isError and all(c.type == "text" for c in result.content)
    meta = json.loads(result.content[0].text)
    assert "彻底删除" in meta["message"] and "2026-12-07" in meta["message"] and "request_id" in meta["message"]
    assert meta["job"]["prompt"] == "编的：完整提示词"
    assert not any(call.url.path.endswith("/image") for call in worker["calls"])  # did not even try


def test_image_get_when_the_image_is_purged_between_the_two_calls(worker):
    worker["routes"][("GET", f"/jobs/{JOB}")] = reply({"job": job(prompt="编的：完整提示词", **BINNED)})
    worker["routes"][("GET", f"/jobs/{JOB}/image")] = reply({"detail": "这张图已经彻底删除了"}, 410)
    result = asyncio.run(mcp_server.image_get(JOB))
    assert not result.isError and "彻底删除" in json.loads(result.content[0].text)["message"]


def test_resubmitting_a_binned_image_says_it_was_not_redrawn(worker):
    worker["routes"][("POST", "/jobs")] = reply({"job": job(**BINNED), "reused": True})
    result = asyncio.run(mcp_server.image_generate("编的：雨里撑伞的橘猫"))
    assert "不会重复生成" in result["message"] and "回收站或已彻底删除" in result["message"] and "新的 request_id" in result["message"]
    worker["routes"][("POST", "/jobs")] = reply({"job": job(), "reused": True})
    assert "回收站" not in asyncio.run(mcp_server.image_generate("编的：雨里撑伞的橘猫"))["message"]


def test_an_old_download_link_of_a_purged_image_says_gone(worker):
    worker["routes"][("GET", f"/jobs/{JOB}/image")] = reply({"detail": "这张图已经彻底删除了"}, 410)
    app = FastAPI()
    app.include_router(images.router)
    parsed = urlparse(images.download_url(JOB))
    with TestClient(app) as client:
        response = client.get(parsed.path + "?" + parsed.query)
    assert response.status_code == 410 and "彻底删除" in response.json()["detail"]


# ---------- 网页 /api/images/* ----------


@pytest.fixture
def page(worker):
    app = FastAPI()
    app.include_router(console.router)
    with TestClient(app) as client:
        yield client


def test_every_image_route_needs_xuans_login(worker, page, monkeypatch):
    monkeypatch.setenv("EMBER_OAUTH_PASSWORD", "开门")
    monkeypatch.setenv("EMBER_OAUTH_ACCESS_TOKEN", "token-abc")
    page.cookies.clear()
    for method, path, payload in (("GET", "/api/images", None), ("GET", "/api/images/stats", None),
                                  ("POST", "/api/images/trash", {"ids": [JOB]}), ("POST", "/api/images/trash/empty", {"until": "x"}),
                                  ("GET", f"/api/images/{JOB}", None), ("POST", f"/api/images/{JOB}/star", {"starred": True}),
                                  ("POST", f"/api/images/{JOB}/restore", None), ("POST", f"/api/images/{JOB}/purge", None),
                                  ("GET", f"/api/images/{JOB}/thumb", None), ("GET", f"/api/images/{JOB}/preview", None),
                                  ("GET", f"/api/images/{JOB}/original", None)):
        kwargs = {"json": payload} if payload else {}
        assert page.request(method, path, **kwargs).status_code == 401
        # The MCP token is not a login either.
        assert page.request(method, path, headers={"Authorization": "Bearer token-abc"}, **kwargs).status_code == 401
    assert worker["calls"] == []


def test_grid_views_map_to_the_worker_filters(worker, page):
    worker["routes"][("GET", "/gallery")] = reply({"items": [job()], "total": 1, "page": 1, "page_size": 60, "has_more": False})
    assert page.get("/api/images").json()["items"][0]["id"] == JOB
    assert dict(worker["calls"][-1].url.params) == {"page": "1", "page_size": "60", "trash": "false"}
    page.get("/api/images?view=starred&page=2&page_size=30")
    assert dict(worker["calls"][-1].url.params) == {"page": "2", "page_size": "30", "trash": "false", "starred": "true"}
    page.get("/api/images?view=trash")
    assert worker["calls"][-1].url.params["trash"] == "true" and "starred" not in worker["calls"][-1].url.params
    calls = len(worker["calls"])
    assert page.get("/api/images?view=everything").status_code == 400
    assert page.get("/api/images?page_size=500").status_code == 422
    assert len(worker["calls"]) == calls


def test_actions_are_forwarded_and_bad_input_stays_on_this_side(worker, page):
    worker["routes"][("POST", "/gallery/trash")] = lambda request: httpx.Response(200, json={
        "results": [{"id": i, "ok": True, "status": "trashed", "job": job(id=i, **BINNED)} for i in body(request)["ids"]],
        "trashed": len(body(request)["ids"])})
    worker["routes"][("POST", "/gallery/empty")] = reply({"purged": 2, "left": 1})
    worker["routes"][("POST", f"/jobs/{JOB}/star")] = reply({"job": job(starred=True)})
    worker["routes"][("POST", f"/jobs/{JOB}/restore")] = reply({"job": job(), "restored": True})
    worker["routes"][("POST", f"/jobs/{JOB}/purge")] = reply({"job": job(purged_at="2026-11-07T07:00:00.000+00:00"), "purged": True})

    assert page.post("/api/images/trash", json={"ids": [JOB, OTHER]}).json()["trashed"] == 2
    assert body(worker["calls"][-1]) == {"ids": [JOB, OTHER]}
    until = "2026-11-07T06:30:00.000+00:00"
    assert page.post("/api/images/trash/empty", json={"until": until}).json() == {"purged": 2, "left": 1}
    assert body(worker["calls"][-1]) == {"until": until}
    assert page.post(f"/api/images/{JOB}/star", json={"starred": True}).json()["job"]["starred"] is True
    assert body(worker["calls"][-1]) == {"starred": True}
    assert page.post(f"/api/images/{JOB}/restore").json()["restored"] is True
    assert page.post(f"/api/images/{JOB}/purge").json()["purged"] is True

    calls = len(worker["calls"])
    for path, payload in (("/api/images/trash", {"ids": []}), ("/api/images/trash", {"ids": [1]}),
                          ("/api/images/trash", {"ids": [JOB] * 101}), ("/api/images/trash", None),
                          ("/api/images/trash/empty", {}), (f"/api/images/{JOB}/star", {"starred": "yes"})):
        assert page.post(path, **({"json": payload} if payload is not None else {})).status_code == 400
    for path in ("/api/images/not-an-id/star", "/api/images/not-an-id/purge", "/api/images/not-an-id/restore"):
        assert page.post(path, json={"starred": True}).status_code == 404
    assert page.get("/api/images/not-an-id/thumb").status_code == 404
    assert len(worker["calls"]) == calls


def test_a_refusal_reaches_the_page_with_its_reason(worker, page):
    worker["routes"][("POST", f"/jobs/{JOB}/purge")] = reply({"detail": {"code": "not_in_trash", "message": "这张不在回收站里了（可能刚被恢复），没有彻底删。"}}, 409)
    refused = page.post(f"/api/images/{JOB}/purge")
    assert refused.status_code == 409
    assert refused.json() == {"error": "not_in_trash", "error_description": "这张不在回收站里了（可能刚被恢复），没有彻底删。"}
    unknown = page.get(f"/api/images/{OTHER}")
    assert unknown.status_code == 404 and "编号不对" in unknown.json()["error_description"]


def test_worker_trouble_is_never_a_401_to_the_browser(worker, page, monkeypatch):
    worker["routes"][("GET", "/gallery/stats")] = reply({"detail": "unauthorized"}, 401)
    response = page.get("/api/images/stats")
    assert response.status_code == 503 and "内部连接" in response.json()["error_description"]  # 401 would reload the page forever
    worker["routes"][("GET", "/gallery/stats")] = lambda request: httpx.Response(500, text="boom")
    assert page.get("/api/images/stats").status_code == 503

    def down(request):
        raise httpx.ConnectError("synthetic: worker down")

    monkeypatch.setattr(images, "client", lambda: httpx.AsyncClient(base_url="http://worker", transport=httpx.MockTransport(down)))
    response = page.get("/api/images")
    assert response.status_code == 503 and "连不上" in response.json()["error_description"]
    assert page.get(f"/api/images/{JOB}/thumb").status_code == 503
    monkeypatch.delenv("IMAGE_WORKER_TOKEN")
    assert "尚未配置" in page.get("/api/images/stats").json()["error_description"]


def test_thumbs_and_previews_are_cached_but_originals_are_not(worker, page):
    worker["routes"][("GET", f"/jobs/{JOB}/thumb")] = lambda request: httpx.Response(200, content=JPEG)
    worker["routes"][("GET", f"/jobs/{JOB}/image")] = lambda request: httpx.Response(
        200, content=JPEG if request.url.params["preview"] == "true" else PNG)
    thumb = page.get(f"/api/images/{JOB}/thumb")
    assert thumb.content == JPEG and thumb.headers["content-type"] == "image/jpeg"
    assert thumb.headers["cache-control"] == "private, max-age=604800, immutable"
    preview = page.get(f"/api/images/{JOB}/preview")
    assert preview.content == JPEG and "max-age" in preview.headers["cache-control"]
    original = page.get(f"/api/images/{JOB}/original")
    assert original.content == PNG and original.headers["content-type"] == "image/png"
    assert original.headers["cache-control"] == "private, no-store"
    assert original.headers["content-disposition"] == f'inline; filename="ember-image-{JOB}.png"'
    worker["routes"][("GET", f"/jobs/{JOB}/thumb")] = reply({"detail": "这张图已经彻底删除了"}, 410)
    assert page.get(f"/api/images/{JOB}/thumb").status_code == 410
