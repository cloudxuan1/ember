"""The gallery: ⭐, the recycle bin (trash → restore / purge / empty / 30 days), listing and stats.

Synthetic images only. Every destructive case checks both ends: what should go is gone, everything else is untouched.
"""

from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import time
import uuid

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from image_worker.runner import CodexRunner, save_image
from image_worker.service import THUMB_SIDE, create_app
from image_worker.store import TRASH_DAYS, Refused, Store, stamp

TOKEN = "synthetic-worker-token-" + "x" * 32
HEADERS = {"Authorization": "Bearer " + TOKEN}


def png(size=(24, 24), color="orange"):
    data = io.BytesIO()
    Image.new("RGB", size, color).save(data, format="PNG")
    return data.getvalue()


def finished(store, prompt, size=(24, 24), **kwargs):
    """A succeeded job with a real PNG original and JPEG preview, the way a generation leaves it."""
    job, _ = store.submit(prompt, **kwargs)
    assert store.claim()["id"] == job["id"]
    store.finish(job["id"], metadata=save_image(store, job["id"], io.BytesIO(png(size))))
    return job["id"]


def files(store, job_id):
    return sorted(path.name for path in store.images.iterdir() if path.name.startswith(job_id))


def contents(store, job_id):
    return {path.name: path.read_bytes() for path in store.images.iterdir() if path.name.startswith(job_id)}


def set_column(store, job_id, **values):
    with store.connection() as conn:
        for name, value in values.items():
            conn.execute(f"UPDATE jobs SET {name}=? WHERE id=?", (value, job_id))


def ids(listing):
    return [item["id"] for item in listing["items"]]


# The jobs table exactly as #30 created it, before any later column.
FIRST_SCHEMA = """CREATE TABLE jobs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL, request_key TEXT UNIQUE NOT NULL,
    prompt TEXT NOT NULL, prompt_hash TEXT NOT NULL,
    status TEXT NOT NULL, created_at TEXT NOT NULL,
    started_at TEXT, finished_at TEXT, error_code TEXT,
    width INTEGER, height INTEGER, image_bytes INTEGER,
    command_events INTEGER NOT NULL DEFAULT 0
)"""


def test_an_old_jobs_table_gains_star_and_bin_columns_and_keeps_every_row(tmp_path):
    old = [(str(uuid.uuid4()), "prompt:a", "编的：第一张", "a", "succeeded", "2026-10-25T08:57:54.123+00:00", 1254, 1254, 2525224),
           (str(uuid.uuid4()), "prompt:b", "编的：失败的一张", "b", "failed", "2026-10-25T09:00:00.000+00:00", None, None, None)]
    conn = sqlite3.connect(tmp_path / "jobs.sqlite3")
    conn.execute(FIRST_SCHEMA)
    conn.execute("CREATE TABLE request_aliases (request_key TEXT PRIMARY KEY, job_id TEXT NOT NULL)")
    conn.executemany("INSERT INTO jobs (id,request_key,prompt,prompt_hash,status,created_at,width,height,image_bytes) VALUES (?,?,?,?,?,?,?,?,?)", old)
    conn.commit()
    before = conn.execute("SELECT * FROM jobs ORDER BY seq").fetchall()
    conn.close()
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / (old[0][0] + ".png")).write_bytes(png())

    store = Store(tmp_path)
    store = Store(tmp_path)  # a second start adds nothing twice
    with store.connection() as conn:
        after = conn.execute("SELECT * FROM jobs ORDER BY seq").fetchall()
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(jobs)")]
    assert [tuple(row)[:len(before[0])] for row in after] == before  # every old value as it was
    assert [(row["starred"], row["trashed_at"], row["purged_at"]) for row in after] == [(0, None, None)] * 2
    assert len(columns) == len(set(columns))
    kept = store.get(old[0][0])
    assert kept["status"] == "succeeded" and kept["starred"] is False
    assert not {"trashed_at", "purge_at", "purged_at"} & kept.keys()
    assert ids(store.gallery()) == [old[0][0]]  # the failed job has no image to list


def test_trash_keeps_the_files_and_restore_brings_the_image_back(tmp_path):
    store = Store(tmp_path)
    first, second = finished(store, "编的：一"), finished(store, "编的：二")
    kept_files = contents(store, first)
    result = store.trash([first])
    assert result[0]["ok"] and result[0]["status"] == "trashed"
    job = result[0]["job"]
    trashed = datetime.fromisoformat(job["trashed_at"])
    assert datetime.fromisoformat(job["purge_at"]) - trashed == timedelta(days=TRASH_DAYS)
    assert contents(store, first) == kept_files  # nothing deleted yet
    assert ids(store.gallery()) == [second] and ids(store.gallery(trash=True)) == [first]
    again = store.trash([first])[0]
    assert again["ok"] and again["status"] == "already" and again["job"]["trashed_at"] == job["trashed_at"]

    job, changed = store.restore(first)
    assert changed and "trashed_at" not in job and "purge_at" not in job
    assert ids(store.gallery()) == [second, first] and ids(store.gallery(trash=True)) == []
    assert store.restore(first) == (job, False)
    assert contents(store, first) == kept_files


def test_starred_images_cannot_be_trashed_and_binned_ones_cannot_be_starred(tmp_path):
    store = Store(tmp_path)
    good, draft = finished(store, "编的：好图"), finished(store, "编的：过程图")
    assert store.set_star(good, True)["starred"] is True
    refused = store.trash([good])[0]
    assert not refused["ok"] and refused["code"] == "starred" and "先取消⭐" in refused["message"]
    assert store.get(good).keys().isdisjoint({"trashed_at", "purged_at"})
    assert store.set_star(good, False)["starred"] is False
    assert store.trash([good])[0]["status"] == "trashed"

    store.trash([draft])
    with pytest.raises(Refused) as error:
        store.set_star(draft, True)
    assert error.value.code == "trashed"
    assert store.set_star(draft, False)["starred"] is False  # taking away a ⭐ it does not have is harmless
    assert store.get(draft)["starred"] is False


def test_references_of_waiting_jobs_are_protected_and_binned_images_are_not_references(tmp_path):
    store = Store(tmp_path)
    reference = finished(store, "编的：定妆照")
    edit, _ = store.submit("编的：照着定妆照换冬装", reference_job_ids=[reference])
    refused = store.trash([reference])[0]
    assert refused["code"] == "in_use" and "参考图" in refused["message"]  # queued
    assert store.claim()["id"] == edit["id"]
    assert store.trash([reference])[0]["code"] == "in_use"  # running
    store.finish(edit["id"], error="generation_failed")
    assert store.trash([reference])[0]["status"] == "trashed"  # its job is over

    with pytest.raises(ValueError, match="回收站"):
        store.submit("编的：再照着它改", reference_job_ids=[reference])
    store.purge(reference)
    with pytest.raises(ValueError, match="彻底删除"):
        store.submit("编的：还照着它改", reference_job_ids=[reference])
    assert [job["status"] for job in store.recent()] == ["failed", "succeeded"]  # nothing queued by the refusals


def test_purge_removes_only_that_images_files_and_keeps_its_record(tmp_path):
    store = Store(tmp_path)
    gone, kept, binned = finished(store, "编的：不要了"), finished(store, "编的：留着"), finished(store, "编的：也在回收站")
    store.trash([gone, binned])
    untouched = {**contents(store, kept), **contents(store, binned)}
    assert files(store, gone) == [gone + ".png", gone + ".preview.jpg"]

    job, purged = store.purge(gone)
    assert purged and job["status"] == "succeeded" and job["purged_at"]
    assert not {"trashed_at", "purge_at"} & job.keys()
    assert files(store, gone) == []
    assert {**contents(store, kept), **contents(store, binned)} == untouched
    assert store.get(gone, full_prompt=True)["prompt"] == "编的：不要了"  # the id still answers
    assert gone not in ids(store.gallery()) + ids(store.gallery(trash=True))
    assert store.purge(gone) == (store.get(gone), False)
    for action in (lambda: store.restore(gone), lambda: store.set_star(gone, True)):
        with pytest.raises(Refused) as error:
            action()
        assert error.value.code == "purged"
    assert store.trash([gone])[0]["code"] == "purged"


def test_purge_only_takes_images_from_the_bin(tmp_path):
    store = Store(tmp_path)
    image = finished(store, "编的：还在用")
    before = contents(store, image)
    with pytest.raises(Refused) as error:
        store.purge(image)
    assert error.value.code == "not_in_trash"
    queued, _ = store.submit("编的：还在排队")
    for job_id, code in ((queued["id"], "no_image"), (str(uuid.uuid4()), "not_found"), ("../jobs.sqlite3", "not_found")):
        with pytest.raises(Refused) as error:
            store.purge(job_id)
        assert error.value.code == code
    assert contents(store, image) == before and "purged_at" not in store.get(image)


def test_emptying_the_bin_spares_what_was_trashed_after_the_confirmation(tmp_path):
    store = Store(tmp_path)
    first, second, later, kept = (finished(store, f"编的：{n}") for n in ("一", "二", "晚来的", "留着"))
    store.trash([first])
    store.trash([second])
    seen = store.gallery_stats()["trash"]  # what the confirmation dialog showed
    assert seen["count"] == 2
    # While the dialog is open, 小克 trashes one more.
    store.trash([later])
    set_column(store, later, trashed_at=stamp(datetime.fromisoformat(seen["latest"]) + timedelta(milliseconds=1)))
    untouched = {**contents(store, later), **contents(store, kept)}

    assert store.empty_trash(seen["latest"]) == {"purged": 2, "left": 1}
    assert files(store, first) == files(store, second) == []
    assert ids(store.gallery(trash=True)) == [later] and ids(store.gallery()) == [kept]
    assert {**contents(store, later), **contents(store, kept)} == untouched
    for bad in ("", "最近", "2026-11-07T08:00:00"):  # no timezone: refuse rather than guess
        with pytest.raises(ValueError):
            store.empty_trash(bad)
    assert ids(store.gallery(trash=True)) == [later]


def test_images_purge_themselves_after_thirty_days_in_the_bin(tmp_path):
    store = Store(tmp_path)
    old, young, needed, kept = (finished(store, f"编的：{n}") for n in ("满 30 天", "差一点", "被排队任务用着", "没删"))
    store.trash([old, young, needed])
    moment = datetime.now(timezone.utc)
    set_column(store, old, trashed_at=stamp(moment - timedelta(days=30, minutes=1)))
    set_column(store, young, trashed_at=stamp(moment - timedelta(days=29, hours=23)))
    set_column(store, needed, trashed_at=stamp(moment - timedelta(days=45)))
    # Cannot happen through the API (a binned image is refused as a reference), but the purge must still spare it.
    waiting, _ = store.submit("编的：排队中")
    set_column(store, waiting["id"], reference_job_ids=json.dumps([needed]))
    kept_files = contents(store, kept)
    untouched = {**contents(store, young), **contents(store, needed), **kept_files}

    assert store.purge_expired(moment) == 1
    assert files(store, old) == [] and store.get(old)["purged_at"]
    assert {**contents(store, young), **contents(store, needed), **contents(store, kept)} == untouched
    assert sorted(ids(store.gallery(trash=True))) == sorted([young, needed])
    assert store.purge_expired(moment) == 0  # nothing twice

    assert store.claim()["id"] == waiting["id"]
    store.finish(waiting["id"], error="generation_failed")
    assert store.purge_expired(moment) == 1 and files(store, needed) == []
    assert store.purge_expired(moment + timedelta(hours=1)) == 1 and files(store, young) == []
    assert ids(store.gallery()) == [kept] and contents(store, kept) == kept_files


def test_gallery_pages_newest_first_with_total_and_has_more(tmp_path):
    store = Store(tmp_path)
    made = [finished(store, f"编的：第 {n} 张") for n in range(25)]
    first = store.gallery(page=1, page_size=10)
    assert ids(first) == made[::-1][:10] and first["total"] == 25 and first["has_more"] is True
    assert ids(store.gallery(page=3, page_size=10)) == made[::-1][20:] and not store.gallery(page=3, page_size=10)["has_more"]
    assert store.gallery(page=4, page_size=10) == {"items": [], "total": 25, "page": 4, "page_size": 10, "has_more": False}
    assert store.gallery(page_size=1000)["page_size"] == 100
    assert store.gallery(page_size=0)["page_size"] == 1
    item = first["items"][0]
    assert item["prompt_preview"] == "编的：第 24 张" and "prompt" not in item and item["starred"] is False


def test_gallery_filters_by_star_bin_and_beijing_day(tmp_path):
    store = Store(tmp_path)
    late_6th, early_7th, late_7th, early_8th = (finished(store, f"编的：{n}") for n in ("6 号深夜", "7 号零点", "7 号深夜", "8 号零点"))
    # Beijing days: 2026-11-07 runs from 2026-11-06 16:00 UTC to 2026-11-07 16:00 UTC.
    for job_id, created in ((late_6th, "2026-11-06T15:59:59.999+00:00"), (early_7th, "2026-11-06T16:00:00.000+00:00"),
                            (late_7th, "2026-11-07T15:59:00.000+00:00"), (early_8th, "2026-11-07T16:00:00.000+00:00")):
        set_column(store, job_id, created_at=created)
    store.set_star(late_7th, True)
    assert ids(store.gallery(date_from="2026-11-07", date_to="2026-11-07")) == [late_7th, early_7th]
    assert ids(store.gallery(date_from="2026-11-07", date_to="2026-11-07", starred=False)) == [early_7th]  # 「今天没点⭐的」
    assert ids(store.gallery(starred=True)) == [late_7th]
    assert ids(store.gallery(date_from="2026-11-08")) == [early_8th]
    assert ids(store.gallery(date_to="2026-11-06")) == [late_6th]
    assert len(store.gallery(date_from="2026-11", date_to="2026-11")["items"]) == 4
    assert store.gallery(date_from="2026", date_to="2026")["total"] == 4
    assert store.gallery(date_from="2026-12")["total"] == 0
    store.trash([early_7th])
    assert ids(store.gallery(trash=True, date_from="2026-11-07", date_to="2026-11-07")) == [early_7th]
    assert ids(store.gallery(date_from="2026-11-07", date_to="2026-11-07")) == [late_7th]
    for bad in ({"date_from": "最近"}, {"date_from": "2026-02-30"}, {"date_to": "2026-13"},
                {"date_from": "2026-11-08", "date_to": "2026-11-07"}):
        with pytest.raises(ValueError):
            store.gallery(**bad)


def test_stats_cover_the_whole_library_and_measure_the_disk(tmp_path):
    store = Store(tmp_path)
    starred, plain, binned = finished(store, "编的：⭐", (300, 200)), finished(store, "编的：普通", (40, 40)), finished(store, "编的：删了", (90, 60))
    store.set_star(starred, True)
    store.trash([binned])

    def disk(job_id):
        return sum(path.stat().st_size for path in store.images.iterdir() if path.name.startswith(job_id))

    stats = store.gallery_stats()
    assert stats["kept"] == {"count": 2, "bytes": disk(starred) + disk(plain), "starred": 1}
    assert stats["trash"] == {"count": 1, "bytes": disk(binned), "latest": store.get(binned)["trashed_at"]}
    assert stats["trash_days"] == TRASH_DAYS == 30
    assert store.gallery(page_size=1)["total"] == stats["kept"]["count"]  # not one page's worth
    store.purge(binned)
    assert store.gallery_stats()["trash"] == {"count": 0, "bytes": 0, "latest": None}


def test_a_batch_rechecks_each_image_when_it_arrives(tmp_path):
    store = Store(tmp_path)
    a, b, c = (finished(store, f"编的：{n}") for n in "abc")
    # The page showed a, b, c without ⭐; before 轩's delete arrives, 小克 stars b.
    store.set_star(b, True)
    results = store.trash([a, b, c, "not-an-id", a])
    assert [result["ok"] for result in results] == [True, False, True, False, True]
    assert [result.get("status") or result["code"] for result in results] == ["trashed", "starred", "trashed", "not_found", "already"]
    assert store.get(b)["starred"] and "trashed_at" not in store.get(b)
    # 轩 restores a while an old page still offers to purge it: the purge is refused, nothing is deleted.
    before = contents(store, a)
    store.restore(a)
    with pytest.raises(Refused) as error:
        store.purge(a)
    assert error.value.code == "not_in_trash" and contents(store, a) == before


def test_a_failed_file_removal_stays_hidden_and_is_retried(tmp_path, monkeypatch):
    store = Store(tmp_path)
    image = finished(store, "编的：删不掉的文件")
    store.trash([image])
    unlink = Path.unlink

    def stuck(path, *args, **kwargs):
        if path.name.startswith(image):
            raise OSError("synthetic busy disk")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", stuck)
    job, purged = store.purge(image)
    assert purged and job["purged_at"] and files(store, image)  # recorded as purged, files left behind
    assert ids(store.gallery()) == ids(store.gallery(trash=True)) == []
    assert store.gallery_stats()["trash"]["count"] == 0
    monkeypatch.setattr(Path, "unlink", unlink)
    store.sweep_purged()
    assert files(store, image) == []


def test_a_reused_request_reports_that_its_image_is_in_the_bin(tmp_path):
    store = Store(tmp_path)
    image = finished(store, "编的：同一段提示词")
    store.trash([image])
    job, reused = store.submit("编的：同一段提示词")
    assert reused and job["id"] == image and job["trashed_at"]  # not redrawn; the caller can see why


def test_gallery_over_http(tmp_path):
    store = Store(tmp_path / "jobs")
    app = create_app(store, CodexRunner(store, tmp_path / "auth"), TOKEN, run_queue=False)
    a, b = finished(store, "编的：横图", (1500, 1000)), finished(store, "编的：方图")
    with TestClient(app) as client:
        for method, path, body in (("GET", "/gallery", None), ("GET", "/gallery/stats", None), ("POST", "/gallery/trash", {"ids": [a]}),
                                   ("POST", f"/jobs/{a}/star", {"starred": True}), ("POST", f"/jobs/{a}/purge", None),
                                   ("POST", "/gallery/empty", {"until": "x"}), ("GET", f"/jobs/{a}/thumb", None)):
            assert client.request(method, path, **({"json": body} if body else {})).status_code == 401
        assert files(store, a) and "trashed_at" not in store.get(a)
        listing = client.get("/gallery?page_size=1", headers=HEADERS).json()
        assert ids(listing) == [b] and listing["total"] == 2 and listing["has_more"]
        assert client.get("/gallery?date_from=最近", headers=HEADERS).status_code == 400

        assert client.post(f"/jobs/{b}/star", json={"starred": True}, headers=HEADERS).json()["job"]["starred"] is True
        assert client.post(f"/jobs/{b}/star", json={"starred": "yes"}, headers=HEADERS).status_code == 422
        result = client.post("/gallery/trash", json={"ids": [a, b]}, headers=HEADERS).json()
        assert result["trashed"] == 1 and result["results"][1]["code"] == "starred"
        refused = client.post(f"/jobs/{b}/purge", headers=HEADERS)
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "not_in_trash"
        unknown = client.post(f"/jobs/{uuid.uuid4()}/restore", headers=HEADERS)
        assert unknown.status_code == 404 and unknown.json()["detail"]["code"] == "not_found"

        thumb = client.get(f"/jobs/{a}/thumb", headers=HEADERS)  # a binned image still shows in the bin
        assert thumb.headers["content-type"] == "image/jpeg"
        with Image.open(io.BytesIO(thumb.content)) as image:
            assert image.format == "JPEG" and image.width == THUMB_SIDE and abs(image.height - 320) <= 1
        stats = client.get("/gallery/stats", headers=HEADERS).json()
        assert stats["kept"]["count"] == 1 and stats["trash"]["count"] == 1

        assert client.post(f"/jobs/{a}/purge", headers=HEADERS).json()["purged"] is True
        for path in (f"/jobs/{a}/image", f"/jobs/{a}/image?preview=true", f"/jobs/{a}/thumb"):
            gone = client.get(path, headers=HEADERS)
            assert gone.status_code == 410 and "彻底删除" in gone.json()["detail"]
        assert client.get(f"/jobs/{a}", headers=HEADERS).json()["job"]["purged_at"]
        assert client.post("/gallery/empty", json={"until": "最近"}, headers=HEADERS).status_code == 400
        assert client.get(f"/jobs/{b}/image", headers=HEADERS).status_code == 200


def test_the_bin_is_checked_when_the_worker_starts(tmp_path):
    store = Store(tmp_path / "jobs")
    old, fresh = finished(store, "编的：放了一个多月"), finished(store, "编的：昨天删的")
    store.trash([old, fresh])
    set_column(store, old, trashed_at=stamp(datetime.now(timezone.utc) - timedelta(days=31)))
    app = create_app(store, CodexRunner(store, tmp_path / "auth"), TOKEN)
    with TestClient(app):
        deadline = time.monotonic() + 5
        while "purged_at" not in store.get(old) and time.monotonic() < deadline:
            time.sleep(0.05)
    assert store.get(old)["purged_at"] and files(store, old) == []
    assert store.get(fresh)["trashed_at"] and files(store, fresh)
