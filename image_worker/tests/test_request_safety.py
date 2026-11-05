"""Synthetic regressions for bounded references and durable queued parameters."""

from concurrent.futures import ThreadPoolExecutor
import base64
import io
import os
from pathlib import Path
import threading

from PIL import Image
import pytest

from image_worker.openrouter import reference_url
from image_worker.store import Store

MODEL = "openai/gpt-image-2.6-flare"


def test_noisy_reference_is_bounded_png_and_keeps_alpha(tmp_path):
    path = tmp_path / "synthetic.png"
    Image.frombytes("RGBA", (2048, 2048), os.urandom(2048 * 2048 * 4)).save(path)
    original = path.read_bytes()
    url = reference_url(path)
    raw = base64.b64decode(url.partition(",")[2])
    assert url.startswith("data:image/png;base64,") and len(raw) <= 1024 * 1024
    with Image.open(io.BytesIO(raw)) as reduced:
        assert reduced.mode == "RGBA" and max(reduced.size) <= 2048
        assert reduced.getextrema()[3][0] < 255
    assert path.read_bytes() == original


def test_failed_batch_leaves_no_jobs_or_parameter_files(tmp_path, monkeypatch):
    store = Store(tmp_path)
    write = store.write_request
    calls = 0

    def fail_second(job_id, options):
        nonlocal calls
        calls += 1
        write(job_id, options)
        if calls == 2:
            raise OSError("synthetic full disk after writing parameters")

    monkeypatch.setattr(store, "write_request", fail_second)
    with pytest.raises(OSError):
        store.submit_request("synthetic batch", model=MODEL, options={"quality": "low"}, n=3)
    assert store.recent() == []
    assert list(store.requests.iterdir()) == []


def test_cleanup_failure_does_not_stop_the_queue(tmp_path, monkeypatch):
    store = Store(tmp_path)
    first, _ = store.submit("synthetic first", model=MODEL, options={"quality": "low"})
    second, _ = store.submit("synthetic second", model=MODEL)
    assert store.claim()["id"] == first["id"]
    unlink = Path.unlink

    def fail_cleanup(path, *args, **kwargs):
        if path == store.request_file(first["id"]):
            raise OSError("synthetic cleanup failure")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_cleanup)
        store.finish(first["id"], error="openrouter_credit")
    assert store.get(first["id"])["error"]["code"] == "openrouter_credit"
    assert store.claim()["id"] == second["id"]
    store.finish(second["id"], error="generation_failed")
    store.recover()
    assert list(store.requests.iterdir()) == []


def test_recovery_cannot_remove_a_concurrent_submission(tmp_path, monkeypatch):
    store = Store(tmp_path)
    scanning, release, submitting = threading.Event(), threading.Event(), threading.Event()
    glob = Path.glob

    def pause_scan(path, pattern, *args, **kwargs):
        if path == store.requests:
            scanning.set()
            assert release.wait(5)
        return glob(path, pattern, *args, **kwargs)

    def submit():
        submitting.set()
        return store.submit("synthetic concurrent", model=MODEL, options={"quality": "low"})[0]

    monkeypatch.setattr(Path, "glob", pause_scan)
    with ThreadPoolExecutor(max_workers=2) as pool:
        recovering = pool.submit(store.recover)
        try:
            assert scanning.wait(5)
            queued = pool.submit(submit)
            assert submitting.wait(5)
            # Old recovery releases its transaction before scanning and permits this commit.
            try:
                queued.result(timeout=0.2)
            except TimeoutError:
                pass
        finally:
            release.set()
        recovering.result(timeout=5)
        job = queued.result(timeout=5)
    assert store.request_file(job["id"]).is_file()
    assert store.claim()["options"] == {"quality": "low"}


def test_reopened_request_directory_is_private_and_parallel_retries_reuse_jobs(tmp_path):
    store = Store(tmp_path)
    store.requests.chmod(0o755)
    store = Store(tmp_path)
    assert store.requests.stat().st_mode & 0o777 == 0o700
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.submit_request(
            "synthetic retry", "same-request", MODEL, options={"complexity": "low"}, n=3), range(4)))
    ids = [job["id"] for job in results[0]["jobs"]]
    assert all([job["id"] for job in result["jobs"]] == ids for result in results)
    assert sum(not result["reused"] for result in results) == 1
    assert {p.stem for p in store.requests.iterdir()} == set(ids)
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in store.requests.iterdir())
