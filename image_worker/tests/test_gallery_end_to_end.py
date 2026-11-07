"""Both ends at once: 小克's tools and the web page's routes (backend app) against the real worker app and store.

The backend's own tests fake the worker; this file checks the two sides still agree on paths, fields and refusals.
Needs the backend's dependencies too (CI installs them before running the worker tests); skipped without them.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
from urllib.parse import urlparse

import pytest

pytest.importorskip("mcp")
pytest.importorskip("sqlite_vec")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import httpx  # noqa: E402
from PIL import Image  # noqa: E402

from app import console, images, mcp_server  # noqa: E402
from image_worker.runner import CodexRunner, save_image  # noqa: E402
from image_worker.service import create_app  # noqa: E402
from image_worker.store import Store  # noqa: E402

TOKEN = "synthetic-worker-token-" + "x" * 32


def finished(store, prompt):
    job, _ = store.submit(prompt)
    assert store.claim()["id"] == job["id"]
    data = io.BytesIO()
    Image.new("RGB", (64, 48), "orange").save(data, format="PNG")
    store.finish(job["id"], metadata=save_image(store, job["id"], io.BytesIO(data.getvalue())))
    return job["id"]


@pytest.fixture
def ends(tmp_path, monkeypatch):
    store = Store(tmp_path / "jobs")
    worker = create_app(store, CodexRunner(store, tmp_path / "auth"), TOKEN, run_queue=False)
    monkeypatch.setenv("IMAGE_WORKER_URL", "http://worker")
    monkeypatch.setenv("IMAGE_WORKER_TOKEN", TOKEN)
    monkeypatch.delenv("EMBER_OAUTH_PASSWORD", raising=False)  # login gate off, as in local development
    monkeypatch.setattr(images, "client", lambda: httpx.AsyncClient(
        base_url="http://worker", transport=httpx.ASGITransport(app=worker), headers={"Authorization": "Bearer " + TOKEN}))
    page = FastAPI()
    page.include_router(console.router)
    page.include_router(images.router)
    with TestClient(page) as client:
        yield store, client


def tool(result):
    return json.loads(result.content[0].text) if hasattr(result, "content") else result


def test_ke_and_xuan_manage_the_same_images(ends):
    store, page = ends
    good, draft, other = finished(store, "编的：定稿"), finished(store, "编的：过程图一"), finished(store, "编的：过程图二")

    # 小克: list, ⭐ the good one, try to delete it, delete a draft.
    listing = asyncio.run(mcp_server.image_list())
    assert [item["id"] for item in listing["items"]] == [other, draft, good] and listing["total"] == 3
    assert asyncio.run(mcp_server.image_star(good, True))["starred"] is True
    assert "先问轩" in asyncio.run(mcp_server.image_delete(good))["error"]
    deleted = asyncio.run(mcp_server.image_delete(draft))
    assert deleted["status"] == "trashed" and deleted["purge_on"]
    assert [item["id"] for item in asyncio.run(mcp_server.image_list(starred=False))["items"]] == [other]
    binned = asyncio.run(mcp_server.image_list(in_trash=True))["items"]
    assert [item["id"] for item in binned] == [draft] and binned[0]["in_trash"] is True
    got = asyncio.run(mcp_server.image_get(draft))
    assert not got.isError and any(c.type == "image" for c in got.content) and "回收站" in tool(got)["message"]

    # 轩 on the web page sees the same thing.
    stats = page.get("/api/images/stats").json()
    assert stats["kept"]["count"] == 2 and stats["kept"]["starred"] == 1 and stats["trash"]["count"] == 1
    assert [item["id"] for item in page.get("/api/images?view=trash").json()["items"]] == [draft]
    assert [item["id"] for item in page.get("/api/images?view=starred").json()["items"]] == [good]
    thumb = page.get(f"/api/images/{draft}/thumb")
    assert thumb.status_code == 200 and thumb.content.startswith(b"\xff\xd8\xff")
    assert page.get(f"/api/images/{good}").json()["job"]["prompt"] == "编的：定稿"

    # She restores the draft, then selects both drafts and the ⭐ one: the ⭐ one is refused, the others go.
    assert page.post(f"/api/images/{draft}/restore").json()["restored"] is True
    batch = page.post("/api/images/trash", json={"ids": [draft, other, good]}).json()
    assert batch["trashed"] == 2 and batch["results"][2]["code"] == "starred"
    latest = page.get("/api/images/stats").json()["trash"]["latest"]
    assert page.post("/api/images/trash/empty", json={"until": latest}).json() == {"purged": 2, "left": 0}
    assert not any(path.name.startswith((draft, other)) for path in store.images.iterdir())
    assert any(path.name.startswith(good) for path in store.images.iterdir())

    # Purged: the id still answers, clearly, everywhere.
    gone = tool(asyncio.run(mcp_server.image_get(draft)))
    assert "彻底删除" in gone["message"] and gone["job"]["prompt"] == "编的：过程图一"
    assert asyncio.run(mcp_server.image_status(draft, 0))["job"]["purged_at"]
    assert page.get(f"/api/images/{draft}/thumb").status_code == 410
    parsed = urlparse(images.download_url(draft))
    assert page.get(parsed.path + "?" + parsed.query).status_code == 410
    assert "彻底删除" in asyncio.run(mcp_server.image_delete(draft))["error"]
    assert asyncio.run(mcp_server.image_list())["total"] == 1


def test_a_refused_purge_and_the_thirty_days_seen_from_both_ends(ends):
    store, page = ends
    image = finished(store, "编的：放久了")
    refused = page.post(f"/api/images/{image}/purge")
    assert refused.status_code == 409 and refused.json()["error"] == "not_in_trash"
    asyncio.run(mcp_server.image_delete(image))
    with store.connection() as conn:
        conn.execute("UPDATE jobs SET trashed_at=? WHERE id=?",
                     ((datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec="milliseconds"), image))
    assert store.purge_expired() == 1
    assert page.get("/api/images/stats").json()["trash"]["count"] == 0
    preview = page.get(f"/api/images/{image}/preview")
    assert preview.status_code == 410  # gone, not "not ready"
