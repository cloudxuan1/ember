import asyncio
import base64
import json
import time
from urllib.parse import urlparse
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from app import images, mcp_server

JOB_ID = str(uuid.uuid4())
PNG = b"\x89PNG\r\n\x1a\nsynthetic-test-bytes"


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setenv("IMAGE_WORKER_URL", "http://worker")
    monkeypatch.setenv("IMAGE_WORKER_TOKEN", "synthetic-internal-token-" + "x" * 32)
    state = {"status": "succeeded", "calls": []}

    def handle(request):
        state["calls"].append(request)
        assert request.headers["authorization"].startswith("Bearer synthetic-internal-token-")
        if request.url.path.endswith("/image"):
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        if state["status"] == "failed":
            return httpx.Response(200, json={"job": {"id": JOB_ID, "status": "failed", "error": {"code": "login_required", "message": "Codex 订阅登录已失效，请重新完成设备码登录。"}}})
        return httpx.Response(200, json={"job": {"id": JOB_ID, "status": state["status"], "width": 24, "height": 24}, "reused": True})

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(images, "client", lambda: httpx.AsyncClient(base_url="http://worker", transport=transport,
                        headers={"Authorization": "Bearer " + images.config()[1]}))
    return state


def test_mcp_submission_does_not_retry_or_lose_request_id(worker):
    result = asyncio.run(mcp_server.image_generate("画一只猫", "same-id"))
    assert result["reused"] and "不会重复" in result["message"]
    assert len(worker["calls"]) == 1
    assert json.loads(worker["calls"][0].content) == {"prompt": "画一只猫", "request_id": "same-id"}


def test_mcp_login_failure_is_clear_and_not_claimed_as_success(worker):
    worker["status"] = "failed"
    status = asyncio.run(mcp_server.image_status(JOB_ID, 0))
    assert status["job"]["error"]["code"] == "login_required"
    assert "设备码" in status["job"]["error"]["message"]
    result = asyncio.run(mcp_server.image_get(JOB_ID))
    assert result.isError and all(c.type != "image" for c in result.content)
    assert "登录已失效" in result.content[0].text


def test_mcp_returns_real_image_content_and_scoped_link(worker):
    result = asyncio.run(mcp_server.image_get(JOB_ID))
    assert not result.isError
    image = next(c for c in result.content if c.type == "image")
    assert image.mimeType == "image/png" and base64.b64decode(image.data) == PNG
    metadata = json.loads(result.content[0].text)
    assert f"/images/{JOB_ID}?" in metadata["download_url"]
    assert images.config()[1] not in metadata["download_url"]


def test_original_download_requires_valid_unexpired_signature(worker):
    app = FastAPI()
    app.include_router(images.router)
    parsed = urlparse(images.download_url(JOB_ID))
    with TestClient(app) as client:
        good = client.get(parsed.path + "?" + parsed.query)
        assert good.status_code == 200 and good.content == PNG
        assert good.headers["cache-control"] == "private, no-store"
        before = len(worker["calls"])
        assert client.get(parsed.path + "?" + parsed.query.replace("signature=", "signature=bad")).status_code == 403
        expires = int(time.time()) - 2
        assert client.get(f"/images/{JOB_ID}?expires={expires}&signature={images.link_signature(JOB_ID,expires)}").status_code == 403
        another = str(uuid.uuid4())
        assert client.get(parsed.path.replace(JOB_ID, another) + "?" + parsed.query).status_code == 403
        assert len(worker["calls"]) == before


def test_invalid_ids_never_reach_worker_and_unconfigured_is_clear(worker, monkeypatch):
    result = asyncio.run(mcp_server.image_status("../../auth.json", 0))
    assert "error" in result and not worker["calls"]
    monkeypatch.delenv("IMAGE_WORKER_TOKEN")
    assert "尚未配置" in asyncio.run(mcp_server.image_generate("猫"))["error"]


def test_worker_network_failure_does_not_repeat_submission(worker, monkeypatch):
    calls = []
    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic timeout")
    monkeypatch.setattr(images, "client", lambda: httpx.AsyncClient(base_url="http://worker", transport=httpx.MockTransport(fail)))
    result = asyncio.run(mcp_server.image_generate("猫"))
    assert len(calls) == 1 and "先查最近任务" in result["error"]


def test_tool_descriptions_cover_queue_retry_privacy_and_subscription():
    assert "订阅" in mcp_server.image_generate.__doc__ and "不自动重试" in mcp_server.image_generate.__doc__
    assert "整段聊天" in mcp_server.image_generate.__doc__ and "不要重新生成" in mcp_server.image_get.__doc__
    assert "设备码" in mcp_server.image_status.__doc__
    doc = mcp_server.image_generate.__doc__
    assert "默认不传 model：走 Codex 订阅" in doc and "不自动换模型" in doc and "Sunburst 画的" in doc
    assert "reference_job_ids" in doc and "cost_usd" in mcp_server.image_status.__doc__


def test_mcp_registers_the_three_image_tools():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {tool.name for tool in tools}
    assert {"image_generate", "image_status", "image_get"} <= names


def test_model_and_references_pass_through_and_bad_references_stay_local(worker):
    ref = str(uuid.uuid4())
    asyncio.run(mcp_server.image_generate("同一个角色换冬装", "r1", "openai/gpt-image-2.5-sunburst", [ref]))
    assert json.loads(worker["calls"][0].content) == {"prompt": "同一个角色换冬装", "request_id": "r1",
                                                      "model": "openai/gpt-image-2.5-sunburst", "reference_job_ids": [ref]}
    result = asyncio.run(mcp_server.image_generate("改图", "", "", ["../../auth.json"]))
    assert "reference_job_ids" in result["error"] and len(worker["calls"]) == 1


def test_openrouter_image_names_its_model_and_carries_cost(worker, monkeypatch):
    job = {"id": JOB_ID, "status": "succeeded", "engine": "openrouter", "model": "openai/gpt-image-2.5-sunburst",
           "model_label": "Sunburst", "cost_usd": 0.19, "key_limit_remaining_usd": 4.81}

    def handle(request):
        if request.url.path.endswith("/image"):
            return httpx.Response(200, content=PNG)
        return httpx.Response(200, json={"job": job})

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(images, "client", lambda: httpx.AsyncClient(base_url="http://worker", transport=transport))
    metadata = json.loads(asyncio.run(mcp_server.image_get(JOB_ID)).content[0].text)
    assert "Sunburst 画的" in metadata["message"]
    assert metadata["job"]["cost_usd"] == 0.19 and metadata["job"]["key_limit_remaining_usd"] == 4.81
