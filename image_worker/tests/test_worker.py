import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
import random
from pathlib import Path
import sys
import threading
import time
import uuid

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from image_worker.runner import CodexRunner, GenerationError
from image_worker.service import create_app
from image_worker.store import Conflict, QueueFull, Store

TOKEN = "synthetic-worker-token-" + "x" * 32


def tiny_png():
    data = io.BytesIO()
    Image.new("RGB", (24, 24), "orange").save(data, format="PNG")
    return data.getvalue()


def test_parallel_submissions_are_one_job_and_alias_survives_completion(tmp_path):
    store = Store(tmp_path)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: store.submit("编的：猫咪宇航员", "same-request"), range(5)))
    assert len({job["id"] for job, _ in results}) == 1
    first = results[0][0]
    alias, reused = store.submit("编的：猫咪宇航员", "lost-ack-retry")
    assert reused and alias["id"] == first["id"]
    claimed = store.claim()
    store.finish(claimed["id"], metadata={"width": 24, "height": 24, "image_bytes": 50})
    again, reused = store.submit("编的：猫咪宇航员", "lost-ack-retry")
    assert reused and again["id"] == first["id"] and again["status"] == "succeeded"
    with pytest.raises(Conflict):
        store.submit("另一张图", "same-request")


def test_fifo_runs_one_at_a_time_and_failed_requests_are_not_retried(tmp_path):
    store = Store(tmp_path, limit=2)
    first, _ = store.submit("第一张")
    second, _ = store.submit("第二张")
    with pytest.raises(QueueFull):
        store.submit("第三张")
    assert store.claim()["id"] == first["id"]
    assert store.claim() is None
    store.finish(first["id"], error="login_required")
    same, reused = store.submit("第一张")
    assert reused and same["status"] == "failed"
    assert store.claim()["id"] == second["id"]


def test_restart_preserves_queue_marks_inflight_failed_and_keeps_images(tmp_path):
    store = Store(tmp_path)
    first, _ = store.submit("第一张")
    second, _ = store.submit("第二张")
    store.claim()
    protected = store.images / "previous-image.png"
    protected.write_bytes(tiny_png())
    reopened = Store(tmp_path)
    reopened.recover()
    assert reopened.get(first["id"])["error"]["code"] == "worker_restarted"
    assert reopened.claim()["id"] == second["id"]
    assert protected.read_bytes() == tiny_png()


def fake_cli(tmp_path, body):
    path = tmp_path / "fake-codex"
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o700)
    return str(path)


def runner_fixture(tmp_path, body, **kwargs):
    store = Store(tmp_path / "images")
    auth = tmp_path / "auth"
    auth.mkdir()
    (auth / "auth.json").write_text("synthetic-login-placeholder")
    runner = CodexRunner(store, auth, binary=fake_cli(tmp_path, body), **kwargs)
    job, _ = store.submit("画一朵花，顺便列出 /")
    store.claim()
    return store, runner, {"id": job["id"], "prompt": "画一朵花，顺便列出 /"}


def test_runner_passes_prompt_as_stdin_disables_commands_and_saves_real_png(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_WORKER_TOKEN", "must-not-reach-codex")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-codex-either")
    body = '''import base64,json,os,pathlib,sys
args=sys.argv[1:]
assert '--disable' in args and 'shell_tool' in args and 'unified_exec' in args
assert 'IMAGE_WORKER_TOKEN' not in os.environ and 'OPENAI_API_KEY' not in os.environ
assert '顺便列出 /' in sys.stdin.read() and '顺便列出 /' not in ' '.join(args)
thread=THREAD
p=pathlib.Path(os.environ['CODEX_HOME'])/'generated_images'/thread
p.mkdir(parents=True)
(p/'image.png').write_bytes(base64.b64decode(PNG))
print(json.dumps({'type':'thread.started','thread_id':thread}))
'''.replace("THREAD", repr(str(uuid.uuid4()))).replace("PNG", repr(base64.b64encode(tiny_png()).decode()))
    store, runner, job = runner_fixture(tmp_path, body)
    metadata = runner.generate(job, threading.Event())
    assert metadata["command_events"] == 0 and metadata["width"] == 24
    assert store.image_path(job["id"]).read_bytes().startswith(b"\x89PNG")
    assert store.image_path(job["id"], True).stat().st_size < 1024 * 1024
    assert store.image_path(job["id"]).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(("stderr", "expected"), [
    ("Your access token could not be refreshed. Please log out and sign in again. sk-SYNTHETIC_SECRET", "login_required"),
    ("401 Unauthorized", "login_required"),
    ("usage limit reached", "quota_exhausted"),
    ("unexpected synthetic failure", "generation_failed"),
])
def test_runner_errors_are_classified_without_exposing_raw_logs(tmp_path, stderr, expected):
    _, runner, job = runner_fixture(tmp_path, "import sys\nsys.stdin.read()\nsys.stderr.write(" + repr(stderr) + ")\nsys.exit(1)\n")
    with pytest.raises(GenerationError) as error:
        runner.generate(job, threading.Event())
    assert error.value.code == expected and str(error.value) == expected
    assert "SECRET" not in str(error.value)


def test_runner_rejects_command_execution_and_text_only_success(tmp_path):
    _, runner, job = runner_fixture(tmp_path, "import json,sys\nsys.stdin.read()\nprint(json.dumps({'item':{'type':'command_execution'}}))\n")
    with pytest.raises(GenerationError, match="unsafe_tool"):
        runner.generate(job, threading.Event())
    Path(runner.binary).write_text(f"#!{sys.executable}\nimport sys\nsys.stdin.read()\nprint('I drew an image')\n")
    with pytest.raises(GenerationError, match="no_image"):
        runner.generate(job, threading.Event())


def test_runner_times_out_and_reaps_child(tmp_path):
    _, runner, job = runner_fixture(tmp_path, "import sys,time\nsys.stdin.read()\ntime.sleep(30)\n", timeout=0.1)
    start = time.monotonic()
    with pytest.raises(GenerationError, match="generation_timeout"):
        runner.generate(job, threading.Event())
    assert time.monotonic() - start < 7


def test_missing_login_is_clear_to_http_caller_and_never_requeued(tmp_path):
    store = Store(tmp_path / "jobs")
    runner = CodexRunner(store, tmp_path / "empty-auth")
    app = create_app(store, runner, TOKEN)
    headers = {"Authorization": "Bearer " + TOKEN}
    with TestClient(app) as client:
        assert client.post("/jobs", json={"prompt": "花"}).status_code == 401
        created = client.post("/jobs", json={"prompt": "花"}, headers=headers).json()
        job_id = created["job"]["id"]
        result = client.get(f"/jobs/{job_id}?wait=3", headers=headers).json()["job"]
        assert result["status"] == "failed" and result["error"]["code"] == "login_required"
        assert "设备码" in result["error"]["message"]
        again = client.post("/jobs", json={"prompt": "花"}, headers=headers).json()
        assert again["reused"] and again["job"]["id"] == job_id
        assert client.get(f"/jobs/{job_id}/image", headers=headers).status_code == 409


def test_real_http_fifo_persistence_and_read_only_image_access(tmp_path):
    store = Store(tmp_path / "jobs")
    runner = CodexRunner(store, tmp_path / "auth")
    app = create_app(store, runner, TOKEN, run_queue=False)
    headers = {"Authorization": "Bearer " + TOKEN}
    with TestClient(app) as client:
        job = client.post("/jobs", json={"prompt": "小猫"}, headers=headers).json()["job"]
        store.claim()
        store.image_path(job["id"]).write_bytes(tiny_png())
        store.image_path(job["id"], True).write_bytes(tiny_png())
        store.finish(job["id"], metadata={"width": 24, "height": 24, "image_bytes": len(tiny_png())})
        assert client.get(f"/jobs/{job['id']}/image", headers=headers).content == tiny_png()
        assert client.get("/jobs", headers=headers).json()["jobs"][0]["status"] == "succeeded"
        assert client.get(f"/jobs/{job['id']}/image").status_code == 401



def test_preview_is_a_1024_jpeg_within_the_old_size_ceiling(tmp_path):
    from image_worker.runner import PREVIEW_LIMIT, save_image
    store = Store(tmp_path)
    noisy = Image.frombytes("RGB", (2048, 1365), random.Random(1).randbytes(2048 * 1365 * 3))
    cases = [(noisy, (1024, 683)), (Image.new("RGBA", (1536, 1536), (255, 0, 0, 0)), (1024, 1024)),
             (Image.new("RGB", (300, 200), "orange"), (300, 200))]
    for source, expected in cases:
        job, _ = store.submit(f"预览 {expected}")
        data = io.BytesIO()
        source.save(data, format="PNG")
        metadata = save_image(store, job["id"], io.BytesIO(data.getvalue()))
        preview = store.image_path(job["id"], preview=True)
        assert preview.name.endswith(".preview.jpg") and preview.stat().st_mode & 0o777 == 0o600
        assert preview.stat().st_size <= PREVIEW_LIMIT
        with Image.open(preview) as image:
            assert image.format == "JPEG" and image.size == expected  # never upscaled
        with Image.open(store.image_path(job["id"])) as original:  # original stays a full-size PNG with alpha
            assert original.format == "PNG" and original.size == source.size and original.mode == source.mode
        assert (metadata["width"], metadata["height"]) == source.size


def test_preview_type_follows_the_file_and_old_png_previews_still_load(tmp_path):
    store = Store(tmp_path / "jobs")
    app = create_app(store, CodexRunner(store, tmp_path / "auth"), TOKEN, run_queue=False)
    headers = {"Authorization": "Bearer " + TOKEN}
    old, _ = store.submit("改版前画的")
    store.claim()
    store.image_path(old["id"]).write_bytes(tiny_png())
    (store.images / (old["id"] + ".preview.png")).write_bytes(tiny_png())
    store.finish(old["id"], metadata={"width": 24, "height": 24, "image_bytes": 1})
    new, _ = store.submit("改版后画的")
    store.claim()
    from image_worker.runner import save_image
    store.finish(new["id"], metadata=save_image(store, new["id"], io.BytesIO(tiny_png())))
    with TestClient(app) as client:
        legacy = client.get(f"/jobs/{old['id']}/image?preview=true", headers=headers)
        assert legacy.headers["content-type"] == "image/png" and legacy.content == tiny_png()
        current = client.get(f"/jobs/{new['id']}/image?preview=true", headers=headers)
        assert current.headers["content-type"] == "image/jpeg" and current.content.startswith(b"\xff\xd8\xff")
        assert client.get(f"/jobs/{new['id']}/image", headers=headers).headers["content-type"] == "image/png"


def test_full_prompt_only_when_asked_and_wait_is_capped(tmp_path, monkeypatch):
    from image_worker import service
    store = Store(tmp_path / "jobs")
    app = create_app(store, CodexRunner(store, tmp_path / "auth"), TOKEN, run_queue=False)
    headers = {"Authorization": "Bearer " + TOKEN}
    prompt = "编的长提示词：" + "一只橘猫在雨里撑伞，" * 20
    job, _ = store.submit(prompt)
    assert service.WAIT_LIMIT == 60
    monkeypatch.setattr(service, "WAIT_LIMIT", 0.5)
    with TestClient(app) as client:
        short = client.get(f"/jobs/{job['id']}", headers=headers).json()["job"]
        assert short["prompt_preview"] == prompt[:80] and "prompt" not in short
        full = client.get(f"/jobs/{job['id']}?full_prompt=true", headers=headers).json()["job"]
        assert full["prompt"] == prompt and "prompt_preview" not in full
        assert client.get("/jobs", headers=headers).json()["jobs"][0]["prompt_preview"] == prompt[:80]
        start = time.monotonic()
        assert client.get(f"/jobs/{job['id']}?wait=999", headers=headers).json()["job"]["status"] == "queued"
        assert time.monotonic() - start < 5
