import asyncio
from contextlib import asynccontextmanager
import fcntl
import hmac
import os
from pathlib import Path
import threading

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from image_worker.openrouter import OpenRouterRunner
from image_worker.runner import CodexRunner, GenerationError
from image_worker.store import Conflict, QueueFull, Store


WAIT_LIMIT = 50  # image_status may hold one request this long (60 hit the MCP client's 60 s timeout); the backend's HTTP timeout sits above it.


class Submission(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    request_id: str = Field(default="", max_length=128)
    # Checked by the store, so a bad value gets a readable 400 instead of a validation 422.
    model: str = ""
    reference_job_ids: list = Field(default_factory=list)


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

    @asynccontextmanager
    async def lifespan(_):
        if len(token) < 32:
            raise RuntimeError("IMAGE_WORKER_TOKEN must contain at least 32 characters")
        lock = (store.root / "worker.lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store.recover()
        thread = threading.Thread(target=loop, name="image-queue", daemon=True)
        if run_queue:
            thread.start()
        try:
            yield
        finally:
            stop.set()
            if run_queue:
                thread.join(timeout=12)
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
        try:
            job, reused = store.submit(body.prompt, body.request_id, body.model, body.reference_job_ids)
        except Conflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        except QueueFull as error:
            raise HTTPException(status_code=429, detail=str(error)) from None
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
        return {"job": job, "reused": reused}

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

    @app.get("/jobs/{job_id}/image", dependencies=[Depends(authorize)])
    def image(job_id: str, preview: bool = False):
        job = store.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="找不到这个画图任务。")
        if job["status"] != "succeeded":
            raise HTTPException(status_code=409, detail="图片还没准备好，请先查任务进度。")
        try:
            path = store.preview_file(job_id) if preview else store.image_path(job_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="找不到图片。") from None
        if not path.is_file():
            raise HTTPException(status_code=404, detail="图片文件不存在。")
        media_type = "image/jpeg" if path.suffix == ".jpg" else "image/png"
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, no-store"})

    return app
