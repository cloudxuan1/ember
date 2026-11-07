"""Durable FIFO and idempotency; unrelated to Ember's memory database."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import uuid

from image_worker.models import OPENROUTER_MODELS, check_options, label, reference_limit


ERRORS = {
    "login_required": "Codex 订阅尚未登录或登录已失效，请让轩在生图容器重新完成设备码登录；不用提供密码或 API key。",
    "quota_exhausted": "Codex 订阅额度暂时不足，请等额度恢复后再明确要求重画；不会自动切换付费 API。",
    "generation_timeout": "这次画图超时，任务已停止，没有自动重试。要重画请明确提交一次新请求。",
    "generation_failed": "Codex 这次没有成功完成图片，原请求不会自动重试。",
    "no_image": "Codex 没有交回有效图片，不能把文字回复当成画图成功。原请求不会自动重试。",
    "worker_restarted": "生图服务在任务期间重启，任务已中断；为避免重复生成，没有自动重试。",
    "unsafe_tool": "本次任务尝试调用画图之外的命令工具，已停止，不会执行服务器命令。",
    "storage_full": "图片存储空间不足，任务没有开始；需要先由轩处理空间，不会自动删除旧图片。",
    "reference_missing": "参考图的原图找不到了，任务没有开始。请换一张已画好的图当参考。",
    "reference_too_large": "参考图请求超过安全大小，任务没有发送。请减少参考图后明确提交新请求。",
    "openrouter_not_configured": "OpenRouter 这条路还没配密钥（VPS .env 的 OPENROUTER_IMAGE_KEY），任务没有开始。不传 model 可以走 Codex 订阅。",
    "openrouter_auth": "OpenRouter 不认这把密钥，请让轩检查 VPS .env 的 OPENROUTER_IMAGE_KEY。没有自动改走订阅。",
    "openrouter_credit": "OpenRouter 额度不够或已到这把密钥的上限，没有自动换模型或重试。",
    "openrouter_refused": "OpenRouter / 模型方拒绝了这次请求（常见是内容审核），没有自动换模型或重试。",
    "openrouter_rejected": "OpenRouter 不接受这次请求的参数或参考图，没有自动换模型或重试；原因见 error.detail。",
    "openrouter_unavailable": "OpenRouter 或模型方暂时不可用，没有自动重试、没有换模型。要重画请明确再提交一次。",
    "request_lost": "这张图提交时选的参数找不到了，没有按默认参数乱画，任务没有开始。要画请明确再提交一次。",
}
# An original is PNG, unless the caller asked OpenRouter for jpeg / webp output (the extension says which).
ORIGINAL_SUFFIXES = (".png", ".jpg", ".webp")
# Columns added after the first release; existing rows read as Codex subscription jobs.
# starred / trashed_at / purged_at (the gallery): existing rows read as kept, not starred, not in the recycle bin.
ADDED_COLUMNS = {
    "model": "TEXT", "reference_job_ids": "TEXT", "cost_usd": "REAL",
    "key_limit_remaining_usd": "REAL", "error_detail": "TEXT",
    "starred": "INTEGER NOT NULL DEFAULT 0", "trashed_at": "TEXT", "purged_at": "TEXT",
}
TRASH_DAYS = 30  # A trashed image is purged after this many days. Fixed on purpose: there is no setting for it.
GALLERY_PAGE_MAX = 100
# Days in listings are Beijing days (the memory library's convention); timestamps are stored in UTC.
BEIJING = timezone(timedelta(hours=8))
# Why a gallery action was refused. Neutral wording: the web page shows these to 轩 as they are.
REFUSALS = {
    "not_found": "找不到这张图，编号不对。",
    "no_image": "这个任务没有画好的图（还在排队、正在画，或者没画成）。",
    "starred": "这张点了⭐，不能删；要删先取消⭐。",
    "in_use": "这张正被排队 / 正在画的任务当参考图，那张画完才能删。",
    "trashed": "这张在回收站里，不能点⭐；先恢复再点。",
    "not_in_trash": "这张不在回收站里了（可能刚被恢复），没有彻底删。",
    "purged": "这张已经彻底删除了，原图和预览都没了。",
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def stamp(moment):
    """A UTC timestamp in now()'s exact format, so stored times compare correctly as text."""
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def purge_time(trashed_at):
    """When an image trashed at this time is purged automatically."""
    return stamp(datetime.fromisoformat(trashed_at) + timedelta(days=TRASH_DAYS))


def day_bound(value, end=False):
    """'2026-11-07' / '2026-11' / '2026' (a Beijing day / month / year) → the UTC timestamp where it starts,
    or with end=True where the next one starts. Empty → None."""
    value = str(value or "").strip()
    if not value:
        return None
    match = re.fullmatch(r"(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?", value)
    if not match:
        raise ValueError(f"日期「{value[:20]}」看不懂：写成 2026-11-07，或只写到月 2026-11、到年 2026（北京时间）。")
    year, month, day = int(match[1]), int(match[2] or 1), int(match[3] or 1)
    try:
        start = datetime(year, month, day, tzinfo=BEIJING)
    except ValueError:
        raise ValueError(f"没有 {value} 这个日子。") from None
    if end:
        if match[3]:
            start += timedelta(days=1)
        elif match[2]:
            start = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=BEIJING)
        else:
            start = datetime(year + 1, 1, 1, tzinfo=BEIJING)
    return stamp(start)


class QueueFull(ValueError):
    pass


class Conflict(ValueError):
    pass


class Refused(ValueError):
    """A gallery action the image's state does not allow right now (⭐, in use as a reference, already purged…)."""

    def __init__(self, code, message=None):
        super().__init__(message or REFUSALS[code])
        self.code = code


class Store:
    def __init__(self, root: Path, limit=8):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.images = self.root / "images"
        self.images.mkdir(exist_ok=True, mode=0o700)
        # The parameters a queued job was submitted with wait here (<id>.json), not in the table, until it finishes.
        self.requests = self.root / "requests"
        self.requests.mkdir(exist_ok=True, mode=0o700)
        self.requests.chmod(0o700)
        self.database = self.root / "jobs.sqlite3"
        self.limit = limit
        # SQLite WAL/SHM inherit the database's private mode.
        self.database.touch(mode=0o600, exist_ok=True)
        self.database.chmod(0o600)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL, request_key TEXT UNIQUE NOT NULL,
                prompt TEXT NOT NULL, prompt_hash TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL,
                started_at TEXT, finished_at TEXT, error_code TEXT,
                width INTEGER, height INTEGER, image_bytes INTEGER,
                command_events INTEGER NOT NULL DEFAULT 0
            )""")
            conn.execute("CREATE TABLE IF NOT EXISTS request_aliases (request_key TEXT PRIMARY KEY, job_id TEXT NOT NULL)")
            present = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
            for name, kind in ADDED_COLUMNS.items():
                if name not in present:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")

    @contextmanager
    def connection(self, request_files=()):
        conn = sqlite3.connect(self.database, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            for job_id in request_files:
                self.remove_request(self.request_file(job_id))
            raise
        finally:
            conn.close()

    @staticmethod
    def digest(prompt, model, references, options=None, copy=1):
        """A request's identity. Codex jobs without references keep the original prompt-only hash and plain
        OpenRouter jobs keep #34's, so older jobs still deduplicate; chosen parameters, and each extra copy
        of an n-image request, make a different one."""
        if options or copy > 1:
            extra = [options or {}] + ([copy] if copy > 1 else [])
            source = json.dumps([prompt, model, references, *extra], ensure_ascii=False, sort_keys=True)
        elif model or references:
            source = json.dumps([prompt, model, references], ensure_ascii=False)
        else:
            source = prompt
        return hashlib.sha256(source.encode()).hexdigest()

    def submit(self, prompt, request_id="", model="", reference_job_ids=(), options=None):
        """One job: (job, reused). submit_request also says which parameters were ignored."""
        result = self.submit_request(prompt, request_id, model, reference_job_ids, options)
        return result["jobs"][0], result["reused"]

    def copies(self, model, n, ignored):
        """How many single-image jobs n asks for: what the model's endpoint allows, capped by the queue."""
        if n is None or (n == 1 and not isinstance(n, bool)):
            return 1
        if not model:
            ignored.append("n（不传 model 走 Codex 订阅，一个请求只画一张，只排了 1 张）")
            return 1
        spec = OPENROUTER_MODELS[model]
        low, high = spec["params"].get("n", (1, 1))
        high = min(high, self.limit)
        if isinstance(n, bool) or not isinstance(n, int) or not low <= n <= high:
            if high == 1:
                raise ValueError(f"{spec['label']} 一次只能画 1 张，n 只能是 1。")
            raise ValueError(f"n 要是 {low}–{high} 的整数（{spec['label']}；画图队列一共 {self.limit} 个位置）。")
        return n

    def submit_request(self, prompt, request_id="", model="", reference_job_ids=(), options=None, n=1):
        """One job, or n single-image jobs (each its own id and request) when n > 1. Each copy deduplicates on its own."""
        prompt = prompt.strip()
        if not prompt or len(prompt) > 8000:
            raise ValueError("画图要求不能为空，且最多 8000 字。")
        if len(request_id) > 128:
            raise ValueError("请求标记最多 128 字。")
        model = (model or "").strip()
        if model and model not in OPENROUTER_MODELS:
            raise ValueError("不认识的模型 " + model[:128] + "。可选：" + "、".join(OPENROUTER_MODELS)
                             + "；不传 model 走 Codex 订阅。")
        references = self.reference_ids(reference_job_ids, model)
        options, ignored = check_options(model, options or {})
        count = self.copies(model, n, ignored)
        plain = self.digest(prompt, model, references)
        wanted = []  # (digest, request key) per copy; the first copy is exactly the single-image request
        for copy in range(1, count + 1):
            digest = self.digest(prompt, model, references, options, copy)
            suffix = "" if copy == 1 else f"#{copy}"
            wanted.append((digest, "request:" + request_id + suffix if request_id else "prompt:" + digest))
        request_files = []
        with self.connection(request_files) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows, new = [None] * count, []
            for i, (digest, key) in enumerate(wanted):
                old = conn.execute("SELECT * FROM jobs WHERE request_key=? OR id=(SELECT job_id FROM request_aliases WHERE request_key=?)", (key, key)).fetchone()
                if old and old["prompt_hash"] != digest:
                    raise Conflict("同一个请求标记已用于另一段画图要求，请给新的请求换一个标记。")
                if old:
                    rows[i] = old
                else:
                    new.append(i)
            for reference in references if new else ():
                row = conn.execute("SELECT status, trashed_at, purged_at FROM jobs WHERE id=?", (reference,)).fetchone()
                # A trashed reference could be purged while its job waits in the queue; a purged one has no original.
                if row and row["purged_at"]:
                    raise ValueError("参考图 " + reference + " 已经彻底删除了，原图没了；换一张还在的图当参考。")
                if row and row["trashed_at"]:
                    raise ValueError("参考图 " + reference + " 在回收站里；要用它，先请轩在网页「图片 → 回收站」里恢复。")
                if not row or row["status"] != "succeeded" or not self.original_file(reference).is_file():
                    raise ValueError("参考图 " + reference + " 不是已画好的图，请用 image_status 里成功任务的 id。")
            for i in list(new):
                digest, key = wanted[i]
                # A lost acknowledgement must not turn an agent's retry into a second image.
                active = conn.execute(
                    "SELECT * FROM jobs WHERE prompt_hash=? AND status IN ('queued','running') ORDER BY seq LIMIT 1",
                    (digest,),
                ).fetchone()
                if active:
                    conn.execute("INSERT INTO request_aliases (request_key,job_id) VALUES (?,?)", (key, active["id"]))
                    rows[i] = active
                    new.remove(i)
            busy = conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            if new and busy + len(new) > self.limit:
                if count == 1:
                    raise QueueFull("画图队列已满，先查已有任务，稍后再提交。")
                raise QueueFull(f"画图队列一共 {self.limit} 个位置，现在还空 {max(self.limit - busy, 0)} 个，这次要新排 {len(new)} 张；"
                                "少要几张，或者等前面的画完再提交。一张都没有排上。")
            for i in new:
                digest, key = wanted[i]
                job_id = str(uuid.uuid4())
                if digest != plain:  # chose parameters, or an extra copy: the worker reads them from this file
                    request_files.append(job_id)
                    self.write_request(job_id, options)
                conn.execute(
                    "INSERT INTO jobs (id,request_key,prompt,prompt_hash,status,created_at,model,reference_job_ids) VALUES (?,?,?,?,?,?,?,?)",
                    (job_id, key, prompt, digest, "queued", now(), model or None, json.dumps(references) if references else None),
                )
                rows[i] = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return {"jobs": [self.public(row, conn) for row in rows], "reused": not new, "ignored": ignored}

    def request_file(self, job_id):
        return self.requests / (str(uuid.UUID(job_id)) + ".json")

    def write_request(self, job_id, options):
        fd = os.open(self.request_file(job_id), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump({"options": options}, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        # Persist the directory entry before SQLite may commit a queued job referring to it.
        directory = os.open(self.requests, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    @staticmethod
    def remove_request(path):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # The job's outcome is already committed. Cleanup must not stop the queue;
            # recovery retries it without logging paths, parameters, or prompts.
            logging.getLogger(__name__).warning("Image parameter cleanup deferred until recovery")

    def read_request(self, row):
        """The parameters a job was submitted with. None = it had some but their file is gone: never draw it with defaults."""
        try:
            options = json.loads(self.request_file(row["id"]).read_text())["options"]
            return options if isinstance(options, dict) else None
        except FileNotFoundError:
            plain = self.digest(row["prompt"], row["model"] or "", json.loads(row["reference_job_ids"] or "[]"))
            return {} if row["prompt_hash"] == plain else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def reference_ids(values, model=""):
        if isinstance(values, str) or not isinstance(values, (list, tuple)):
            raise ValueError("reference_job_ids 要是任务 id 的列表。")
        references = []
        for value in values:
            try:
                canonical = str(uuid.UUID(value))
            except (ValueError, TypeError, AttributeError):
                canonical = None
            if canonical != value:
                raise ValueError("参考图要用之前画图任务的 id。")
            if value not in references:
                references.append(value)
        limit = reference_limit(model)
        if len(references) > limit:
            raise ValueError(f"参考图最多 {limit} 张（{label(model)}）。")
        return references

    def public(self, row, conn, full_prompt=False):
        out = {k: row[k] for k in ("id", "status", "created_at", "started_at", "finished_at", "width", "height", "image_bytes", "command_events")}
        if full_prompt:
            out["prompt"] = row["prompt"]
        else:
            out["prompt_preview"] = row["prompt"][:80]
        out["engine"] = "openrouter" if row["model"] else "codex_subscription"
        out["model"] = row["model"]
        out["model_label"] = label(row["model"])
        if row["reference_job_ids"]:
            out["reference_job_ids"] = json.loads(row["reference_job_ids"])
        for field in ("cost_usd", "key_limit_remaining_usd"):
            if row[field] is not None:
                out[field] = row[field]
        out["starred"] = bool(row["starred"])
        if row["purged_at"]:  # files gone; the record stays
            out["purged_at"] = row["purged_at"]
        elif row["trashed_at"]:
            out["trashed_at"] = row["trashed_at"]
            out["purge_at"] = purge_time(row["trashed_at"])
        if row["status"] == "queued":
            out["queue_position"] = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='queued' AND seq<=?", (row["seq"],)).fetchone()[0]
        if row["error_code"]:
            out["error"] = {"code": row["error_code"], "message": ERRORS.get(row["error_code"], ERRORS["generation_failed"])}
            if row["error_detail"]:
                out["error"]["detail"] = row["error_detail"]
        return out

    def get(self, job_id, full_prompt=False):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return self.public(row, conn, full_prompt) if row else None

    def recent(self):
        with self.connection() as conn:
            return [self.public(r, conn) for r in conn.execute("SELECT * FROM jobs ORDER BY seq DESC LIMIT 5").fetchall()]

    def claim(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM jobs WHERE status='running' LIMIT 1").fetchone():
                return None
            row = conn.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY seq LIMIT 1").fetchone()
            if not row:
                return None
            conn.execute("UPDATE jobs SET status='running',started_at=? WHERE id=?", (now(), row["id"]))
            return {"id": row["id"], "prompt": row["prompt"], "model": row["model"] or "",
                    "reference_job_ids": json.loads(row["reference_job_ids"] or "[]"), "options": self.read_request(row)}

    def finish(self, job_id, *, error=None, metadata=None, detail=None):
        metadata = metadata or {}
        with self.connection() as conn:
            conn.execute(
                """UPDATE jobs SET status=?,finished_at=?,error_code=?,width=?,height=?,image_bytes=?,command_events=?,
                   cost_usd=?,key_limit_remaining_usd=?,error_detail=? WHERE id=? AND status='running'""",
                ("failed" if error else "succeeded", now(), error, metadata.get("width"), metadata.get("height"),
                 metadata.get("image_bytes"), metadata.get("command_events", 0), metadata.get("cost_usd"),
                 metadata.get("key_limit_remaining_usd"), detail, job_id),
            )
        self.remove_request(self.request_file(job_id))

    def recover(self):
        # Never automatically reissue an interrupted request to the subscription service.
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE jobs SET status='failed',error_code='worker_restarted',finished_at=? WHERE status='running'", (now(),))
            queued = {row["id"] for row in conn.execute("SELECT id FROM jobs WHERE status='queued'")}
            # Keep the write lock until cleanup ends; a concurrent submit must not lose its file.
            for path in self.requests.glob("*.json"):
                if path.stem not in queued:
                    self.remove_request(path)

    def image_path(self, job_id, preview=False):
        # All paths derive from our UUID, never from model output or request paths.
        canonical = str(uuid.UUID(job_id))
        if canonical != job_id:
            raise ValueError("invalid image id")
        return self.images / (canonical + (".preview.jpg" if preview else ".png"))

    def original_file(self, job_id):
        """<id>.png, or the <id>.jpg / <id>.webp a jpeg / webp request was stored as."""
        path = self.image_path(job_id)
        return next((p for p in (path.with_suffix(s) for s in ORIGINAL_SUFFIXES) if p.is_file()), path)

    def preview_file(self, job_id):
        """The JPEG preview, or the PNG preview that images made before the switch still have."""
        path = self.image_path(job_id, preview=True)
        legacy = path.with_name(path.name.replace(".preview.jpg", ".preview.png"))
        return path if path.is_file() or not legacy.is_file() else legacy

    # ---------- Gallery: ⭐, the recycle bin, listing ----------
    # Every change re-reads the row inside BEGIN IMMEDIATE, so a stale page or a second caller (the web page and
    # 小克 at the same time) gets a refusal instead of undoing what the other just did.

    def image_files(self, job_id):
        """Every file an image can have (original in any format, JPEG or old PNG preview); most never exist."""
        original = self.image_path(job_id)
        preview = self.image_path(job_id, preview=True)
        return [original.with_suffix(s) for s in ORIGINAL_SUFFIXES] + [preview, preview.with_suffix(".png")]

    def disk_bytes(self, job_id):
        total = 0
        for path in self.image_files(job_id):
            try:
                total += path.stat().st_size
            except FileNotFoundError:
                pass
        return total

    def remove_files(self, job_id):
        """Delete a purged image's original and preview. The row already says purged, so nothing lists or serves
        them; a failure leaves them for the next sweep (sweep_purged) instead of stopping anything."""
        for path in self.image_files(job_id):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).warning("Image file removal deferred until the next cleanup")
                return False
        return True

    @staticmethod
    def in_use(conn):
        """Images a queued or running job will still read as references: never trashed or purged."""
        used = set()
        for row in conn.execute("SELECT reference_job_ids FROM jobs WHERE status IN ('queued','running') AND reference_job_ids IS NOT NULL"):
            used.update(json.loads(row["reference_job_ids"]))
        return used

    @staticmethod
    def image_row(conn, job_id):
        """The row of a finished image that still has its files, or Refused."""
        try:
            known = str(uuid.UUID(job_id)) == job_id
        except (ValueError, TypeError, AttributeError):
            known = False
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone() if known else None
        if not row:
            raise Refused("not_found")
        if row["purged_at"]:
            raise Refused("purged")
        if row["status"] != "succeeded":
            raise Refused("no_image")
        return row

    def reread(self, conn, job_id):
        return self.public(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(), conn)

    def set_star(self, job_id, starred):
        """⭐ an image (starred images cannot be trashed) or take the ⭐ away."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self.image_row(conn, job_id)
            if starred and row["trashed_at"]:
                raise Refused("trashed")
            conn.execute("UPDATE jobs SET starred=? WHERE id=?", (1 if starred else 0, job_id))
            return self.reread(conn, job_id)

    def trash(self, ids):
        """Move images into the recycle bin; their files stay until purged. One result per id, in order:
        {"id", "ok": True, "status": "trashed" | "already", "job"} or {"id", "ok": False, "code", "message"}."""
        results = []
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            used = self.in_use(conn)
            moment = now()
            for job_id in ids:
                try:
                    row = self.image_row(conn, job_id)
                    if row["trashed_at"]:
                        results.append({"id": job_id, "ok": True, "status": "already", "job": self.public(row, conn)})
                        continue
                    if row["starred"]:
                        raise Refused("starred")
                    if job_id in used:
                        raise Refused("in_use")
                    conn.execute("UPDATE jobs SET trashed_at=? WHERE id=?", (moment, job_id))
                    results.append({"id": job_id, "ok": True, "status": "trashed", "job": self.reread(conn, job_id)})
                except Refused as refused:
                    results.append({"id": job_id, "ok": False, "code": refused.code, "message": str(refused)})
        return results

    def restore(self, job_id):
        """Take an image out of the recycle bin: (job, whether it was in there)."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self.image_row(conn, job_id)
            if not row["trashed_at"]:
                return self.public(row, conn), False
            conn.execute("UPDATE jobs SET trashed_at=NULL WHERE id=?", (job_id,))
            return self.reread(conn, job_id), True

    def mark_purged(self, conn, where, args=()):
        """Mark trashed images matching `where` purged (never one a queued / running job still needs) → their ids."""
        used = self.in_use(conn)
        rows = conn.execute(f"SELECT id FROM jobs WHERE trashed_at IS NOT NULL AND purged_at IS NULL AND starred=0 AND {where}", args)
        ids = [row["id"] for row in rows if row["id"] not in used]
        moment = now()
        conn.executemany("UPDATE jobs SET purged_at=? WHERE id=?", [(moment, job_id) for job_id in ids])
        return ids

    def purge(self, job_id):
        """Permanently delete one image from the recycle bin: original and preview files go, the job row stays
        (purged_at), so its id still answers. (job, whether it was purged just now)."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.image_row(conn, job_id)
            except Refused as refused:
                if refused.code == "purged":
                    return self.reread(conn, job_id), False
                raise
            if not row["trashed_at"]:
                raise Refused("not_in_trash")
            if not self.mark_purged(conn, "id=?", (job_id,)):
                raise Refused("in_use")
            job = self.reread(conn, job_id)
        self.remove_files(job_id)
        return job, True

    def empty_trash(self, until):
        """Purge what was in the recycle bin when 轩 confirmed: everything trashed at or before `until` (the newest
        trashed_at her page showed). Something trashed after that, e.g. by 小克 while the dialog was open, stays."""
        try:
            moment = datetime.fromisoformat(str(until))
            if moment.tzinfo is None:
                raise ValueError("no timezone")
        except (ValueError, TypeError):
            raise ValueError("清空回收站要带上页面看到的最后一张的时间（until）。") from None
        cutoff = stamp(moment)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            ids = self.mark_purged(conn, "trashed_at<=?", (cutoff,))
            left = conn.execute("SELECT COUNT(*) FROM jobs WHERE trashed_at IS NOT NULL AND purged_at IS NULL").fetchone()[0]
        for job_id in ids:
            self.remove_files(job_id)
        return {"purged": len(ids), "left": left}

    def purge_expired(self, moment=None):
        """Purge images that have been in the recycle bin for TRASH_DAYS days. Returns how many."""
        cutoff = stamp((moment or datetime.now(timezone.utc)) - timedelta(days=TRASH_DAYS))
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            ids = self.mark_purged(conn, "trashed_at<=?", (cutoff,))
        for job_id in ids:
            self.remove_files(job_id)
        return len(ids)

    def sweep_purged(self):
        """Retry removing files of purged images whose deletion failed before."""
        with self.connection() as conn:
            ids = [row["id"] for row in conn.execute("SELECT id FROM jobs WHERE purged_at IS NOT NULL")]
        for job_id in ids:
            self.remove_files(job_id)

    def gallery(self, page=1, page_size=20, starred=None, trash=False, date_from="", date_to=""):
        """Finished images that still have their files, newest first: kept ones, or (trash=True) the recycle bin,
        most recently trashed first. starred None = both; dates are Beijing days on created_at, both ends included."""
        page, page_size = max(1, int(page)), max(1, min(int(page_size), GALLERY_PAGE_MAX))
        where = ["status='succeeded'", "purged_at IS NULL", "trashed_at IS NOT NULL" if trash else "trashed_at IS NULL"]
        args = []
        if starred is not None:
            where.append("starred=?")
            args.append(1 if starred else 0)
        start, end = day_bound(date_from), day_bound(date_to, end=True)
        if start and end and start >= end:
            raise ValueError("date_from 不能晚于 date_to。")
        if start:
            where.append("created_at>=?")
            args.append(start)
        if end:
            where.append("created_at<?")
            args.append(end)
        clause = " AND ".join(where)
        order = "trashed_at DESC, seq DESC" if trash else "seq DESC"
        with self.connection() as conn:
            conn.execute("BEGIN")  # one snapshot: total and items agree
            total = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {clause}", args).fetchone()[0]
            rows = conn.execute(f"SELECT * FROM jobs WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
                                [*args, page_size, (page - 1) * page_size]).fetchall()
            items = [self.public(row, conn) for row in rows]
        return {"items": items, "total": total, "page": page, "page_size": page_size,
                "has_more": page * page_size < total}

    def gallery_stats(self):
        """How many images, and how much disk their files take (measured), kept vs. in the recycle bin.
        trash.latest is the newest trashed_at: emptying the bin purges up to it and no further."""
        with self.connection() as conn:
            rows = conn.execute("SELECT id, starred, trashed_at FROM jobs WHERE status='succeeded' AND purged_at IS NULL").fetchall()
        kept = {"count": 0, "bytes": 0, "starred": 0}
        trash = {"count": 0, "bytes": 0, "latest": None}
        for row in rows:
            part = trash if row["trashed_at"] else kept
            part["count"] += 1
            part["bytes"] += self.disk_bytes(row["id"])
            if row["trashed_at"]:
                trash["latest"] = max(trash["latest"] or "", row["trashed_at"])
            elif row["starred"]:
                kept["starred"] += 1
        return {"kept": kept, "trash": trash, "trash_days": TRASH_DAYS}

