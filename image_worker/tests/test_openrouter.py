import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
from pathlib import Path
import sqlite3
import sys
import threading
import time
import uuid

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from image_worker.models import OPENROUTER_MODELS
from image_worker.openrouter import OpenRouterRunner
from image_worker.runner import CodexRunner, GenerationError, save_image
from image_worker.service import create_app
from image_worker.store import Conflict, Store

KEY = "sk-or-v1-SYNTHETIC" + "k" * 40
TOKEN = "synthetic-worker-token-" + "x" * 32
FLARE = "openai/gpt-image-2.5-flare"
SUNBURST = "openai/gpt-image-2.5-sunburst"
FLARE_26 = "openai/gpt-image-2.6-flare"
SUNBURST_26 = "openai/gpt-image-2.6-sunburst"
GEMINI = "google/gemini-3.1-flash-image"
SEEDREAM = "bytedance-seed/seedream-5-0-flash"
ROOT = Path(__file__).resolve().parents[2]

# What GET /api/v1/images/models/{id}/endpoints returned on 2026-11-05 (2.6: read off the live page by 轩).
OPENAI_LIVE = {
    "aspect_ratio": {"type": "enum", "values": ["1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "21:9", "auto"]},
    "quality": {"type": "enum", "values": ["auto", "low", "medium", "high", "xhigh", "max"]},
    "background": {"type": "enum", "values": ["auto", "transparent", "opaque"]},
    "n": {"type": "range", "min": 1, "max": 10},
    "input_references": {"type": "range", "min": 0, "max": 16},
    "output_compression": {"type": "range", "min": 0, "max": 100},
}
OPENAI_25 = {"provider_name": "OpenAI", "provider_slug": "openai", "provider_tag": "openai", "supported_parameters": OPENAI_LIVE,
             "allowed_passthrough_parameters": ["moderation"], "supports_streaming": True}
OPENAI_26 = {**OPENAI_25, "supported_parameters": {**OPENAI_LIVE, "output_format": {"type": "enum", "values": ["png", "jpeg", "webp"]}},
             "allowed_passthrough_parameters": ["moderation", "complexity"]}
GEMINI_LIVE = {
    "resolution": {"type": "enum", "values": ["512", "1K", "2K", "4K"]},
    "aspect_ratio": {"type": "enum", "values": ["1:1", "1:4", "1:8", "2:3", "3:2", "3:4", "4:1", "4:3", "4:5", "5:4", "8:1",
                                                "9:16", "16:9", "21:9"]},
    "n": {"type": "range", "min": 1, "max": 1},
    "input_references": {"type": "range", "min": 0, "max": 14},
}
SEEDREAM_LIVE = {
    "resolution": {"type": "enum", "values": ["1K", "2K"]},
    "aspect_ratio": {"type": "enum", "values": ["1:1", "1:2", "2:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9",
                                                "9:19.5", "19.5:9", "9:20", "20:9", "9:21", "21:9", "auto"]},
    "n": {"type": "range", "min": 1, "max": 1},
    "input_references": {"type": "range", "min": 0, "max": 14},
    "seed": {"type": "boolean"},
}
LIVE_ENDPOINTS = {
    FLARE_26: [OPENAI_26], SUNBURST_26: [OPENAI_26], FLARE: [OPENAI_25], SUNBURST: [OPENAI_25],
    GEMINI: [{"provider_name": "Google Vertex", "provider_slug": slug, "supported_parameters": GEMINI_LIVE,
              "allowed_passthrough_parameters": ["cachedContent"]}
             for slug in ("google-vertex/us", "google-ai-studio", "google-vertex/global")],
    SEEDREAM: [{"provider_name": "Seed", "provider_slug": "seed", "supported_parameters": SEEDREAM_LIVE,
                "allowed_passthrough_parameters": []}],
}


def picture(fmt="PNG", size=(32, 20), color="teal"):
    data = io.BytesIO()
    Image.new("RGB", size, color).save(data, format=fmt)
    return data.getvalue()


@pytest.fixture
def openrouter():
    """A real local HTTP server standing in for openrouter.ai/api/v1."""
    state = {"requests": [], "image": {"status": 200, "body": None}, "key": {"status": 200, "body": {"data": {"limit_remaining": 9.5}}},
             "pages": {}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, spec):
            raw = json.dumps(spec["body"]).encode()
            self.send_response(spec["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            state["requests"].append({"method": "GET", "path": self.path, "auth": self.headers.get("Authorization")})
            if self.path in state["pages"]:
                self.reply({"status": 200, "body": state["pages"][self.path]})
                return
            self.reply(state["key"] if self.path.endswith("/key") else {"status": 200, "body": {"data": []}})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append({"method": "POST", "path": self.path, "auth": self.headers.get("Authorization"), "body": body})
            spec = dict(state["image"])
            if spec["body"] is None:
                spec["body"] = {"created": 1, "data": [{"b64_json": base64.b64encode(picture("JPEG")).decode(), "media_type": "image/jpeg"}],
                                "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10, "cost": 0.0421}}
            self.reply(spec)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_port}/api/v1"
    yield state
    server.shutdown()


def finished_image(store, prompt="定妆照", size=(48, 30)):
    job, _ = store.submit(prompt)
    store.claim()
    store.finish(job["id"], metadata=save_image(store, job["id"], io.BytesIO(picture(size=size))))
    return job["id"]


def run_openrouter(store, openrouter, key=KEY, **submit):
    job, _ = store.submit(submit.pop("prompt", "编的：雨天的橘猫"), **submit)
    claimed = store.claim()
    runner = OpenRouterRunner(store, key, timeout=10, base_url=openrouter["url"])
    try:
        store.finish(claimed["id"], metadata=runner.generate(claimed, threading.Event()))
    except GenerationError as error:
        store.finish(claimed["id"], error=error.code, detail=error.detail)
    return store.get(job["id"])


def test_unknown_model_is_rejected_before_anything_is_queued(tmp_path):
    store = Store(tmp_path)
    for bad in ("openai/gpt-image-9", "gpt-image-2", " codex "):
        with pytest.raises(ValueError, match="不认识的模型"):
            store.submit("猫", model=bad)
    app = create_app(store, CodexRunner(store, tmp_path / "auth"), TOKEN, run_queue=False)
    with TestClient(app) as client:
        response = client.post("/jobs", json={"prompt": "猫", "model": "openai/dall-e-9"}, headers={"Authorization": "Bearer " + TOKEN})
        assert response.status_code == 400 and FLARE in response.json()["detail"]
    assert store.recent() == []


def test_every_whitelisted_model_is_listed_in_the_mcp_tool_description():
    description = (ROOT / "backend/app/mcp_server.py").read_text()
    for model, spec in OPENROUTER_MODELS.items():
        assert model in description and spec["label"] in description


def test_openrouter_body_follows_model_capabilities_and_cost_is_recorded(tmp_path, openrouter):
    store = Store(tmp_path)
    job = run_openrouter(store, openrouter, model=FLARE)
    post, key = openrouter["requests"]
    assert post["path"] == "/api/v1/images" and post["auth"] == "Bearer " + KEY
    assert post["body"] == {"model": FLARE, "prompt": "编的：雨天的橘猫", "n": 1, "quality": "high"}
    assert key["path"] == "/api/v1/key"
    assert job["status"] == "succeeded" and job["engine"] == "openrouter" and job["model_label"] == "Flare 2.5"
    assert job["cost_usd"] == 0.0421 and job["key_limit_remaining_usd"] == 9.5
    assert (job["width"], job["height"]) == (32, 20)
    assert store.image_path(job["id"]).read_bytes().startswith(b"\x89PNG")  # JPEG reply stored as PNG like Codex images
    assert store.image_path(job["id"], preview=True).is_file()
    gemini = run_openrouter(store, openrouter, model=GEMINI)
    assert "quality" not in openrouter["requests"][2]["body"] and gemini["model_label"] == "Nano Banana 2"


def test_2_6_models_join_the_list_and_every_label_carries_its_version(tmp_path, openrouter):
    store = Store(tmp_path)
    for model, name in ((FLARE_26, "Flare 2.6"), (SUNBURST_26, "Sunburst 2.6"), (FLARE, "Flare 2.5"), (SUNBURST, "Sunburst 2.5")):
        job = run_openrouter(store, openrouter, prompt="编的：" + name, model=model)
        assert job["status"] == "succeeded" and job["model_label"] == name
        # Nothing chosen = the body #34 sent: quality high, one image, no other parameter.
        assert openrouter["requests"][-2]["body"] == {"model": model, "prompt": "编的：" + name, "n": 1, "quality": "high"}


def test_jobs_drawn_before_the_rename_read_with_the_versioned_label(tmp_path):
    store = Store(tmp_path)
    old_id = str(uuid.uuid4())
    with sqlite3.connect(store.database) as conn:  # a #34 job as it sits on the VPS volume; labels are computed on read
        conn.execute("INSERT INTO jobs (id,request_key,prompt,prompt_hash,status,created_at,model,cost_usd) VALUES (?,?,?,?,?,?,?,?)",
                     (old_id, "prompt:old", "旧图", "old", "succeeded", "2026-10-26T00:00:00.000+00:00", SUNBURST, 0.05))
    assert store.get(old_id)["model_label"] == "Sunburst 2.5" and store.recent()[0]["model_label"] == "Sunburst 2.5"


def test_reference_limit_follows_each_model(tmp_path):
    store = Store(tmp_path)
    done = [finished_image(store, f"编的：第 {i} 张", (8, 8)) for i in range(17)]
    for model, limit in ((FLARE_26, 16), (FLARE, 16), (GEMINI, 14), (SEEDREAM, 14), ("", 5)):
        job, _ = store.submit("照着画 " + model, model=model, reference_job_ids=done[:limit])
        assert job["reference_job_ids"] == done[:limit]
        with pytest.raises(ValueError, match=f"最多 {limit} 张"):
            store.submit("太多了 " + model, model=model, reference_job_ids=done[:limit + 1])


def test_probe_finds_models_py_matching_every_live_endpoint_and_flags_drift():
    import runpy
    compare = runpy.run_path(str(ROOT / "scripts/openrouter-image-probe.py"), run_name="probe")["compare"]
    for model, spec in OPENROUTER_MODELS.items():
        assert compare(spec, LIVE_ENDPOINTS[model])[0] == [], model
    assert any("resolution" in note for note in compare(OPENROUTER_MODELS[GEMINI], LIVE_ENDPOINTS[GEMINI])[1])  # offered less: a note
    drifted = {**OPENAI_26, "allowed_passthrough_parameters": ["moderation"],
               "supported_parameters": {**OPENAI_26["supported_parameters"], "quality": {"type": "enum", "values": ["low", "high"]}}}
    problems = compare(OPENROUTER_MODELS[FLARE_26], [drifted])[0]
    assert any("quality" in p and "max" in p for p in problems) and any("complexity" in p for p in problems)
    assert compare(OPENROUTER_MODELS[FLARE_26], [{**OPENAI_26, "provider_slug": "azure"}])[0]  # complexity would not arrive
    assert compare(OPENROUTER_MODELS[GEMINI], [{**LIVE_ENDPOINTS[GEMINI][0], "supported_parameters": {**GEMINI_LIVE, "input_references": {
        "type": "range", "min": 0, "max": 10}}}])[0]  # we would send more references than it takes


def test_probe_run_reports_every_model_ok_against_the_live_listing(tmp_path, openrouter, monkeypatch, capsys):
    import runpy
    from image_worker import models

    openrouter["pages"]["/api/v1/images/models"] = {"data": [{"id": model} for model in OPENROUTER_MODELS]}
    for model, endpoints in LIVE_ENDPOINTS.items():
        openrouter["pages"][f"/api/v1/images/models/{model}/endpoints"] = {"id": model, "endpoints": endpoints}
    monkeypatch.setattr(models, "API_BASE", openrouter["url"])
    env = tmp_path / "vps.env"
    env.write_text("OPENROUTER_IMAGE_KEY=" + KEY + "\n")
    monkeypatch.setattr(sys, "argv", ["probe", "--env-file", str(env), "--out", str(tmp_path / "probe")])
    runpy.run_path(str(ROOT / "scripts/openrouter-image-probe.py"), run_name="__main__")
    printed = capsys.readouterr().out
    assert printed.count("：OK") == len(OPENROUTER_MODELS) and "要改" not in printed and KEY not in printed


def test_reference_images_are_sent_as_input_references(tmp_path, openrouter):
    store = Store(tmp_path)
    first, second = finished_image(store, "定妆照", (48, 30)), finished_image(store, "背景", (20, 20))
    job = run_openrouter(store, openrouter, prompt="同一个角色换成冬装", model=FLARE, reference_job_ids=[first, second, first])
    references = openrouter["requests"][0]["body"]["input_references"]
    assert [r["type"] for r in references] == ["image_url", "image_url"]
    sizes = []
    for reference in references:
        url = reference["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        with Image.open(io.BytesIO(base64.b64decode(url.partition(",")[2]))) as image:
            sizes.append(image.size)
    assert sizes == [(48, 30), (20, 20)] and job["reference_job_ids"] == [first, second]


def test_bad_references_are_refused_at_submission(tmp_path):
    store = Store(tmp_path)
    pending, _ = store.submit("还没画完")
    good = finished_image(store)
    for refs, message in ((["../auth.json"], "任务的 id"), ([str(uuid.uuid4())], "不是已画好的图"),
                          ([pending["id"]], "不是已画好的图"), ("not-a-list", "列表")):
        with pytest.raises(ValueError, match=message):
            store.submit("改一下", model=FLARE, reference_job_ids=refs)
    with pytest.raises(ValueError, match="最多 5"):
        store.submit("改一下", reference_job_ids=[good] + [str(uuid.uuid4()) for _ in range(5)])


def test_model_and_references_are_part_of_idempotency(tmp_path):
    store = Store(tmp_path)
    codex, _ = store.submit("一只猫")
    flare, reused = store.submit("一只猫", model=FLARE)
    assert not reused and flare["id"] != codex["id"]
    again, reused = store.submit("一只猫", model=FLARE)
    assert reused and again["id"] == flare["id"]
    store.submit("一只猫", "fixed-id", model=FLARE)
    with pytest.raises(Conflict):
        store.submit("一只猫", "fixed-id", model=GEMINI)


@pytest.mark.parametrize(("status", "code"), [(401, "openrouter_auth"), (402, "openrouter_credit"), (403, "openrouter_refused"),
                                              (400, "openrouter_rejected"), (429, "openrouter_unavailable"), (503, "openrouter_unavailable")])
def test_openrouter_failures_are_named_and_never_retried_or_rerouted(tmp_path, openrouter, status, code):
    openrouter["image"] = {"status": status, "body": {"error": {"code": status, "message": "synthetic upstream says no"}}}
    job = run_openrouter(Store(tmp_path), openrouter, model=FLARE)
    assert job["status"] == "failed" and job["error"]["code"] == code
    assert job["error"]["detail"] == "synthetic upstream says no"
    assert len(openrouter["requests"]) == 1  # one POST, no retry, no fallback model, no key lookup


def test_reply_without_image_is_not_success(tmp_path, openrouter):
    openrouter["image"] = {"status": 200, "body": {"created": 1, "data": [], "usage": {"cost": 0.01}}}
    assert run_openrouter(Store(tmp_path), openrouter, model=FLARE)["error"]["code"] == "no_image"
    openrouter["image"] = {"status": 200, "body": {"created": 1, "data": [{"b64_json": base64.b64encode(b"not an image").decode()}]}}
    assert run_openrouter(Store(tmp_path / "2"), openrouter, model=FLARE)["error"]["code"] == "no_image"


def test_missing_key_fails_clearly_without_calling_openrouter(tmp_path, openrouter):
    job = run_openrouter(Store(tmp_path), openrouter, key="", model=FLARE)
    assert job["error"]["code"] == "openrouter_not_configured" and openrouter["requests"] == []


def test_remaining_credit_lookup_failure_does_not_fail_the_image(tmp_path, openrouter):
    openrouter["key"] = {"status": 500, "body": {"error": {"message": "down"}}}
    job = run_openrouter(Store(tmp_path), openrouter, model=FLARE)
    assert job["status"] == "succeeded" and job["cost_usd"] == 0.0421 and "key_limit_remaining_usd" not in job


def test_key_never_appears_in_jobs_logs_health_or_codex_child(tmp_path, openrouter, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("OPENROUTER_IMAGE_KEY", KEY)
    openrouter["image"] = {"status": 400, "body": {"error": {"message": f"bad key {KEY} or sk-or-v1-otherSecret99 rejected"}}}
    store = Store(tmp_path / "jobs")
    job = run_openrouter(store, openrouter, model=FLARE)
    assert "[已隐藏]" in job["error"]["detail"] and "otherSecret99" not in job["error"]["detail"]
    auth = tmp_path / "auth"
    auth.mkdir()
    (auth / "auth.json").write_text("synthetic-login-placeholder")
    cli = tmp_path / "fake-codex"
    marker = tmp_path / "child-checked"
    cli.write_text(f"#!{sys.executable}\nimport os,pathlib,sys\nsys.stdin.read()\nassert 'OPENROUTER_IMAGE_KEY' not in os.environ\n"
                   f"assert {KEY!r} not in repr(dict(os.environ)) + ' '.join(sys.argv)\npathlib.Path({str(marker)!r}).write_text('ok')\nsys.exit(3)\n")
    cli.chmod(0o700)
    codex = CodexRunner(store, auth, binary=str(cli))
    app = create_app(store, codex, TOKEN, run_queue=False)  # OpenRouter runner built from the environment
    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["openrouter_key_present"] is True
        listing = client.get("/jobs", headers={"Authorization": "Bearer " + TOKEN}).text
    plain, _ = store.submit("编的：订阅那条路")
    with pytest.raises(GenerationError, match="generation_failed"):
        codex.generate(store.claim(), threading.Event())
    assert marker.read_text() == "ok"  # the child's own checks passed
    captured = capsys.readouterr()
    database = (store.root / "jobs.sqlite3").read_bytes()
    for text in (json.dumps(job), json.dumps(health), listing, captured.out, captured.err, caplog.text):
        assert KEY not in text
    assert KEY.encode() not in database


def test_codex_path_with_references_attaches_copies_and_names_paths(tmp_path, monkeypatch):
    store = Store(tmp_path / "images")
    reference = finished_image(store)
    auth = tmp_path / "auth"
    auth.mkdir()
    (auth / "auth.json").write_text("synthetic-login-placeholder")
    thread = str(uuid.uuid4())
    body = f'''import base64,json,os,pathlib,sys
args=sys.argv[1:]
attach=[a for a in args if a.startswith('--image=')]
assert len(attach)==1 and args[-1]=='-'
path=pathlib.Path(attach[0].split('=',1)[1])
assert path.name=='reference-1.png' and path.read_bytes()==pathlib.Path({str(store.image_path(reference))!r}).read_bytes()
prompt=sys.stdin.read()
assert 'referenced_image_paths' in prompt and str(path) in prompt
out=pathlib.Path(os.environ['CODEX_HOME'])/'generated_images'/{thread!r}
out.mkdir(parents=True)
(out/'image.png').write_bytes(base64.b64decode({base64.b64encode(picture()).decode()!r}))
print(json.dumps({{'type':'thread.started','thread_id':{thread!r}}}))
'''
    cli = tmp_path / "fake-codex"
    cli.write_text(f"#!{sys.executable}\n" + body)
    cli.chmod(0o700)
    job, _ = store.submit("同一个角色，换个背景", reference_job_ids=[reference])
    claimed = store.claim()
    assert claimed["model"] == "" and claimed["reference_job_ids"] == [reference]
    metadata = CodexRunner(store, auth, binary=str(cli)).generate(claimed, threading.Event())
    store.finish(job["id"], metadata=metadata)
    finished = store.get(job["id"])
    assert finished["status"] == "succeeded" and finished["engine"] == "codex_subscription" and "cost_usd" not in finished


def test_codex_gets_a_png_copy_of_a_jpeg_reference(tmp_path):
    store = Store(tmp_path / "images")
    job, _ = store.submit("编的：jpeg 定妆照")
    store.claim()
    store.finish(job["id"], metadata=save_image(store, job["id"], io.BytesIO(picture("JPEG")), keep="jpeg"))
    assert store.original_file(job["id"]).suffix == ".jpg"
    auth = tmp_path / "auth"
    auth.mkdir()
    (auth / "auth.json").write_text("synthetic-login-placeholder")
    thread = str(uuid.uuid4())
    body = f'''import base64,json,os,pathlib,sys
attach=[a for a in sys.argv[1:] if a.startswith('--image=')]
path=pathlib.Path(attach[0].split('=',1)[1])
assert path.name=='reference-1.png' and path.read_bytes().startswith(bytes([0x89])+b'PNG')
sys.stdin.read()
out=pathlib.Path(os.environ['CODEX_HOME'])/'generated_images'/{thread!r}
out.mkdir(parents=True)
(out/'image.png').write_bytes(base64.b64decode({base64.b64encode(picture()).decode()!r}))
print(json.dumps({{'type':'thread.started','thread_id':{thread!r}}}))
'''
    cli = tmp_path / "fake-codex"
    cli.write_text(f"#!{sys.executable}\n" + body)
    cli.chmod(0o700)
    store.submit("同一个角色，换个背景", reference_job_ids=[job["id"]])
    metadata = CodexRunner(store, auth, binary=str(cli)).generate(store.claim(), threading.Event())
    assert metadata["width"] == 32


def test_codex_arguments_without_references_are_unchanged(tmp_path):
    runner = CodexRunner(Store(tmp_path), tmp_path / "auth", binary="codex")
    disabled = []
    for feature in ("shell_tool", "unified_exec", "multi_agent", "plugins", "apps", "hooks", "memories",
                    "computer_use", "browser_use", "in_app_browser"):
        disabled += ["--disable", feature]
    assert runner.arguments(tmp_path) == ["codex", *disabled,
        "--enable", "image_generation", "exec", "--model", "gpt-6.1-sol", "--ignore-user-config", "--ignore-rules",
        "--ephemeral", "--skip-git-repo-check", "--sandbox", "workspace-write", "-C", str(tmp_path),
        "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
        "-c", 'shell_environment_policy.inherit="none"', "-c", 'model_reasoning_effort="xhigh"',
        "-c", 'cli_auth_credentials_store="file"', "--json", "--color", "never", "-"]


def test_queue_sends_model_jobs_to_openrouter_and_plain_jobs_to_codex(tmp_path):
    store = Store(tmp_path)
    seen = []

    class Fake:
        def __init__(self, name, key=""):
            self.name, self.key, self.auth_home = name, key, tmp_path

        def generate(self, job, stop):
            seen.append((self.name, job["model"]))
            if self.name == "openrouter":
                raise GenerationError("openrouter_credit")
            return {"width": 1, "height": 1, "image_bytes": 1}

    app = create_app(store, Fake("codex"), TOKEN, openrouter=Fake("openrouter", "k"))
    headers = {"Authorization": "Bearer " + TOKEN}
    with TestClient(app) as client:
        plain = client.post("/jobs", json={"prompt": "订阅画"}, headers=headers).json()["job"]
        paid = client.post("/jobs", json={"prompt": "付费画", "model": FLARE}, headers=headers).json()["job"]
        for job in (plain, paid):
            deadline = time.monotonic() + 5
            while client.get(f"/jobs/{job['id']}", headers=headers).json()["job"]["status"] in ("queued", "running"):
                assert time.monotonic() < deadline
                time.sleep(0.05)
        failed = client.get(f"/jobs/{paid['id']}", headers=headers).json()["job"]
    assert seen == [("codex", ""), ("openrouter", FLARE)]  # the failed paid job did not fall back to Codex
    assert failed["error"]["code"] == "openrouter_credit"


def test_existing_job_database_is_upgraded_in_place(tmp_path):
    database = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(database) as conn:  # the #30 schema, as it exists on the VPS volume
        conn.execute("""CREATE TABLE jobs (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT UNIQUE NOT NULL, request_key TEXT UNIQUE NOT NULL,
            prompt TEXT NOT NULL, prompt_hash TEXT NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL,
            started_at TEXT, finished_at TEXT, error_code TEXT,
            width INTEGER, height INTEGER, image_bytes INTEGER,
            command_events INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("CREATE TABLE request_aliases (request_key TEXT PRIMARY KEY, job_id TEXT NOT NULL)")
        old_id = str(uuid.uuid4())
        digest = hashlib.sha256("旧图".encode()).hexdigest()
        conn.execute("INSERT INTO jobs (id,request_key,prompt,prompt_hash,status,created_at,width,height,image_bytes) VALUES (?,?,?,?,?,?,?,?,?)",
                     (old_id, "prompt:" + digest, "旧图", digest, "succeeded", "2026-10-25T00:00:00.000+00:00", 10, 10, 99))
    store = Store(tmp_path)
    old = store.get(old_id)
    assert old["status"] == "succeeded" and old["engine"] == "codex_subscription" and old["image_bytes"] == 99
    again, reused = store.submit("旧图")
    assert reused and again["id"] == old_id
    Store(tmp_path)  # reopening is a no-op


def test_only_the_image_worker_container_receives_the_openrouter_key():
    blocks, name = {}, None
    for line in (ROOT / "docker-compose.yml").read_text().splitlines():
        if line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":"):
            name = line.strip()[:-1]
        elif not line.startswith(" "):
            name = None
        if name:
            blocks.setdefault(name, []).append(line)
    holders = [service for service, lines in blocks.items() if any("OPENROUTER_IMAGE_KEY" in l for l in lines)]
    assert holders == ["image-worker"] and {"ember", "tunnel"} <= set(blocks)
    assert "OPENROUTER_IMAGE_KEY=\n" in (ROOT / ".env.example").read_text()


def test_vps_probe_script_reports_mismatches_without_leaking_the_key(tmp_path, openrouter, monkeypatch, capsys):
    import runpy
    from image_worker import models

    openrouter["image"] = {"status": 400, "body": {"error": {"message": "echoing " + KEY}}}
    openrouter["key"] = {"status": 200, "body": {"data": {"label": KEY[:14] + "...", "limit": 10, "limit_remaining": 9.5}}}
    monkeypatch.setattr(models, "API_BASE", openrouter["url"])
    env = tmp_path / "vps.env"
    env.write_text("OTHER=1\nOPENROUTER_IMAGE_KEY=" + KEY + "\n")
    out = tmp_path / "probe"
    monkeypatch.setattr(sys, "argv", ["probe", "--env-file", str(env), "--generate", "--out", str(out)])
    runpy.run_path(str(ROOT / "scripts/openrouter-image-probe.py"), run_name="__main__")
    printed = capsys.readouterr().out
    assert "不在清单里" in printed and "limit_remaining': 9.5" in printed  # fake listing is empty
    assert KEY not in printed and KEY[:14] not in printed
    assert out.stat().st_mode & 0o777 == 0o700
    for path in out.iterdir():
        assert KEY[:14].encode() not in path.read_bytes() and path.stat().st_mode & 0o777 == 0o600

