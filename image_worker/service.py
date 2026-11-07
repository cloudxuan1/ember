import asyncio
from contextlib import asynccontextmanager
import fcntl
import hmac
import io
import logging
import os
from pathlib import Path
import threading
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, Response
from PIL import Image
from pydantic import BaseModel, Field, StrictBool

from image_worker.models import OPTION_NAMES
from image_worker.openrouter import OpenRouterRunner
from image_worker.runner import CodexRunner, GenerationError
from image_worker.store import Conflict, QueueFull, Refused, Store


WAIT_LIMIT = 50  # image_status may hold one request this long (60 hit the MCP client's 60 s timeout); the backend's HTTP timeout sits above it.
MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}
HOUSEKEEPING_SECONDS = 60 * 60  # how often the recycle bin's 30 days are checked (also once at startup)
THUMB_SIDE = 480  # long side of a web page grid thumbnail, made from the preview


def thumbnail(path):
    """A small JPEG for the web page's grid. Made from the preview (≤1024 px), never from the original."""
    with Image.open(path) as source:
        source.draft("RGB", (THUMB_SIDE, THUMB_SIDE))  # a JPEG decodes straight at a reduced scale
        if "A" in source.getbands():  # old PNG previews may have alpha; JPEG has none
            rgba = source.convert("RGBA")
            image = Image.new("RGB", rgba.size, "white")
            image.paste(rgba, mask=rgba.getchannel("A"))
        else:
            image = source.convert("RGB")
    image.thumbnail((THUMB_SIDE, THUMB_SIDE))
    data = io.BytesIO()
    image.save(data, format="JPEG", quality=80, optimize=True)
    return data.getvalue()


class StarBody(BaseModel):
    starred: StrictBool


class TrashBody(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)


class EmptyBody(BaseModel):
    until: str = Field(min_length=1, max_length=64)


def refusal(error):
    """A Refused gallery action as HTTP: 404 for an unknown id, 409 otherwise, with a machine code and the reason."""
    return HTTPException(status_code=404 if error.code == "not_found" else 409,
                         detail={"code": error.code, "message": str(error)})


class Submission(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    request_id: str = Field(default="", max_length=128)
    # Checked by the store, so a bad value gets a readable 400 instead of a validation 422.
    model: str = ""
    reference_job_ids: list = Field(default_factory=list)
    quality: Any = None
    complexity: Any = None
    aspect_ratio: Any = None
    background: Any = None
    output_format: Any = None
    output_compression: Any = None
    n: Any = 1


def create_app(store=None, runner=None, token=None, run_queue=True, openrouter=None):
    store = store or Store(Path(os.environ.get("IMAGE_DATA_DIR", "/var/lib/ember-images")))
    timeout = int(os.environ.get("IMAGE_TIMEOUT_SECONDS", "420"))
    runner = runner or CodexRunner(store, os.environ.get("CODEX_HOME", "/home/node/.codex"), timeout)
    openrouter = openrouter or OpenRouterRunner(store, os.environ.get("OPENROUTER_IMAGE_KEY", ""), timeout)
    token = token if token is not None else os.environ.get("IMAGE_WORKER_TOKEN", "")
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            job = store.claim()
            if not job:
                stop.wait(0.25)
                continue
            try:
                # No model = the Codex subscription, exactly as before; never falls through to the other path.
                metadata = (openrouter if job["model"] else runner).generate(job, stop)
                store.finish(job["id"], metadata=metadata)
            except GenerationError as error:
                store.finish(job["id"], error=error.code, detail=error.detail)
            except Exception:
                # No traceback containing prompts, model output, or authentication data.
                store.finish(job["id"], error="generation_failed")

    def housekeeping():
        # The recycle bin: purge what has been in it for 30 days, retry file removals that failed. At startup, then hourly.
        while True:
            try:
                store.purge_expired()
                store.sweep_purged()
            except Exception:
                logging.getLogger(__name__).warning("Recycle bin cleanup deferred to the next round")
            if stop.wait(HOUSEKEEPING_SECONDS):
                return

    @asynccontextmanager
    async def lifespan(_):
        if len(token) < 32:
            raise RuntimeError("IMAGE_WORKER_TOKEN must contain at least 32 characters")
        lock = (store.root / "worker.lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store.recover()
        thread = threading.Thread(target=loop, name="image-queue", daemon=True)
        cleaner = threading.Thread(target=housekeeping, name="image-trash", daemon=True)
        if run_queue:
            thread.start()
            cleaner.start()
        try:
            yield
        finally:
            stop.set()
            if run_queue:
                thread.join(timeout=12)
                cleaner.join(timeout=12)
            lock.close()

    app = FastAPI(title="ember image worker", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def authorize(authorization: str = Header(default="")):
        if not token or not hmac.compare_digest(authorization.encode(), ("Bearer " + token).encode()):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/health")
    def health():
        return {"status": "ok", "login_cache_present": (runner.auth_home / "auth.json").is_file(),
                "openrouter_key_present": bool(openrouter.key)}

    @app.post("/jobs", dependencies=[Depends(authorize)])
    def submit(body: Submission):
        options = {name: getattr(body, name) for name in OPTION_NAMES}
        try:
            result = store.submit_request(body.prompt, body.request_id, body.model, body.reference_job_ids, options, body.n)
        except Conflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        except QueueFull as error:
            raise HTTPException(status_code=429, detail=str(error)) from None
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
        out = {"job": result["jobs"][0], "reused": result["reused"]}
        if len(result["jobs"]) > 1:  # n single-image jobs; "job" stays the first, as before
            out["jobs"] = result["jobs"]
        if result["ignored"]:
            out["ignored"] = result["ignored"]
        return out

    @app.get("/jobs", dependencies=[Depends(authorize)])
    def recent():
        return {"jobs": store.recent()}

    @app.get("/jobs/{job_id}", dependencies=[Depends(authorize)])
    async def status(job_id: str, wait: int = 0, full_prompt: bool = False):
        deadline = asyncio.get_running_loop().time() + max(0, min(wait, WAIT_LIMIT))
        while True:
            job = store.get(job_id, full_prompt)
            if not job:
                raise HTTPException(status_code=404, detail="找不到这个画图任务。")
            if job["status"] in ("succeeded", "failed") or asyncio.get_running_loop().time() >= deadline:
                return {"job": job}
            await asyncio.sleep(0.25)

    def served_file(job_id, preview):
        job = store.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="找不到这个画图任务。")
        if job.get("purged_at"):
            raise HTTPException(status_code=410, detail="这张图已经彻底删除了，原图和预览都没了。")
        if job["status"] != "succeeded":
            raise HTTPException(status_code=409, detail="图片还没准备好，请先查任务进度。")
        try:
            path = store.preview_file(job_id) if preview else store.original_file(job_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="找不到图片。") from None
        if not path.is_file():
            raise HTTPException(status_code=404, detail="图片文件不存在。")
        return path

    @app.get("/jobs/{job_id}/image", dependencies=[Depends(authorize)])
    def image(job_id: str, preview: bool = False):
        path = served_file(job_id, preview)
        return FileResponse(path, media_type=MEDIA_TYPES[path.suffix], headers={"Cache-Control": "private, no-store"})

    # ---------- Gallery: the web page's grid and 小克's list / ⭐ / recycle bin ----------
    thumbs = threading.BoundedSemaphore(2)  # decoding is the only heavy part; keep memory flat under a full grid

    @app.get("/jobs/{job_id}/thumb", dependencies=[Depends(authorize)])
    def thumb(job_id: str):
        path = served_file(job_id, preview=True)
        try:
            with thumbs:
                data = thumbnail(path)
        except (OSError, ValueError, Image.DecompressionBombError):
            raise HTTPException(status_code=404, detail="预览图读不出来。") from None
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})

    @app.get("/gallery", dependencies=[Depends(authorize)])
    def gallery(page: int = 1, page_size: int = 20, starred: bool | None = None, trash: bool = False,
                date_from: str = "", date_to: str = ""):
        try:
            return store.gallery(page, page_size, starred, trash, date_from, date_to)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None

    @app.get("/gallery/stats", dependencies=[Depends(authorize)])
    def gallery_stats():
        return store.gallery_stats()

    @app.post("/gallery/trash", dependencies=[Depends(authorize)])
    def trash(body: TrashBody):
        results = store.trash(body.ids)
        return {"results": results, "trashed": sum(result.get("status") == "trashed" for result in results)}

    @app.post("/gallery/empty", dependencies=[Depends(authorize)])
    def empty(body: EmptyBody):
        try:
            return store.empty_trash(body.until)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None

    @app.post("/jobs/{job_id}/star", dependencies=[Depends(authorize)])
    def star(job_id: str, body: StarBody):
        try:
            return {"job": store.set_star(job_id, body.starred)}
        except Refused as error:
            raise refusal(error) from None

    @app.post("/jobs/{job_id}/restore", dependencies=[Depends(authorize)])
    def restore(job_id: str):
        try:
            job, changed = store.restore(job_id)
        except Refused as error:
            raise refusal(error) from None
        return {"job": job, "restored": changed}

    @app.post("/jobs/{job_id}/purge", dependencies=[Depends(authorize)])
    def purge(job_id: str):
        try:
            job, changed = store.purge(job_id)
        except Refused as error:
            raise refusal(error) from None
        return {"job": job, "purged": changed}

    return app
