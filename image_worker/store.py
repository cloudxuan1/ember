"""Durable FIFO and idempotency; unrelated to Ember's memory database."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from image_worker.models import MAX_REFERENCES, OPENROUTER_MODELS, label


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
    "openrouter_not_configured": "OpenRouter 这条路还没配密钥（VPS .env 的 OPENROUTER_IMAGE_KEY），任务没有开始。不传 model 可以走 Codex 订阅。",
    "openrouter_auth": "OpenRouter 不认这把密钥，请让轩检查 VPS .env 的 OPENROUTER_IMAGE_KEY。没有自动改走订阅。",
    "openrouter_credit": "OpenRouter 额度不够或已到这把密钥的上限，没有自动换模型或重试。",
    "openrouter_refused": "OpenRouter / 模型方拒绝了这次请求（常见是内容审核），没有自动换模型或重试。",
    "openrouter_rejected": "OpenRouter 不接受这次请求的参数或参考图，没有自动换模型或重试；原因见 error.detail。",
    "openrouter_unavailable": "OpenRouter 或模型方暂时不可用，没有自动重试、没有换模型。要重画请明确再提交一次。",
}
# Columns added after the first release; existing rows read as Codex subscription jobs.
ADDED_COLUMNS = {
    "model": "TEXT", "reference_job_ids": "TEXT", "cost_usd": "REAL",
    "key_limit_remaining_usd": "REAL", "error_detail": "TEXT",
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class QueueFull(ValueError):
    pass


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, root: Path, limit=8):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.images = self.root / "images"
        self.images.mkdir(exist_ok=True, mode=0o700)
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
    def connection(self):
        conn = sqlite3.connect(self.database, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def submit(self, prompt, request_id="", model="", reference_job_ids=()):
        prompt = prompt.strip()
        if not prompt or len(prompt) > 8000:
            raise ValueError("画图要求不能为空，且最多 8000 字。")
        if len(request_id) > 128:
            raise ValueError("请求标记最多 128 字。")
        model = (model or "").strip()
        if model and model not in OPENROUTER_MODELS:
            raise ValueError("不认识的模型 " + model[:128] + "。可选：" + "、".join(OPENROUTER_MODELS)
                             + "；不传 model 走 Codex 订阅。")
        references = self.reference_ids(reference_job_ids)
        # Codex jobs without references keep the original prompt-only hash, so older jobs still deduplicate.
        source = json.dumps([prompt, model, references], ensure_ascii=False) if model or references else prompt
        digest = hashlib.sha256(source.encode()).hexdigest()
        key = "request:" + request_id if request_id else "prompt:" + digest
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT * FROM jobs WHERE request_key=? OR id=(SELECT job_id FROM request_aliases WHERE request_key=?)", (key, key)).fetchone()
            if old:
                if old["prompt_hash"] != digest:
                    raise Conflict("同一个请求标记已用于另一段画图要求，请给新的请求换一个标记。")
                return self.public(old, conn), True
            for reference in references:
                row = conn.execute("SELECT status FROM jobs WHERE id=?", (reference,)).fetchone()
                if not row or row["status"] != "succeeded" or not self.image_path(reference).is_file():
                    raise ValueError("参考图 " + reference + " 不是已画好的图，请用 image_status 里成功任务的 id。")
            # A lost acknowledgement must not turn an agent's retry into a second image.
            active = conn.execute(
                "SELECT * FROM jobs WHERE prompt_hash=? AND status IN ('queued','running') ORDER BY seq LIMIT 1",
                (digest,),
            ).fetchone()
            if active:
                conn.execute("INSERT INTO request_aliases (request_key,job_id) VALUES (?,?)", (key, active["id"]))
                return self.public(active, conn), True
            if conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= self.limit:
                raise QueueFull("画图队列已满，先查已有任务，稍后再提交。")
            job_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO jobs (id,request_key,prompt,prompt_hash,status,created_at,model,reference_job_ids) VALUES (?,?,?,?,?,?,?,?)",
                (job_id, key, prompt, digest, "queued", now(), model or None, json.dumps(references) if references else None),
            )
            return self.public(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(), conn), False

    @staticmethod
    def reference_ids(values):
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
        if len(references) > MAX_REFERENCES:
            raise ValueError(f"参考图最多 {MAX_REFERENCES} 张。")
        return references

    def public(self, row, conn):
        out = {k: row[k] for k in ("id", "status", "created_at", "started_at", "finished_at", "width", "height", "image_bytes", "command_events")}
        out["prompt_preview"] = row["prompt"][:80]
        out["engine"] = "openrouter" if row["model"] else "codex_subscription"
        out["model"] = row["model"]
        out["model_label"] = label(row["model"])
        if row["reference_job_ids"]:
            out["reference_job_ids"] = json.loads(row["reference_job_ids"])
        for field in ("cost_usd", "key_limit_remaining_usd"):
            if row[field] is not None:
                out[field] = row[field]
        if row["status"] == "queued":
            out["queue_position"] = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='queued' AND seq<=?", (row["seq"],)).fetchone()[0]
        if row["error_code"]:
            out["error"] = {"code": row["error_code"], "message": ERRORS.get(row["error_code"], ERRORS["generation_failed"])}
            if row["error_detail"]:
                out["error"]["detail"] = row["error_detail"]
        return out

    def get(self, job_id):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return self.public(row, conn) if row else None

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
                    "reference_job_ids": json.loads(row["reference_job_ids"] or "[]")}

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

    def recover(self):
        # Never automatically reissue an interrupted request to the subscription service.
        with self.connection() as conn:
            conn.execute("UPDATE jobs SET status='failed',error_code='worker_restarted',finished_at=? WHERE status='running'", (now(),))

    def image_path(self, job_id, preview=False):
        # All paths derive from our UUID, never from model output or request paths.
        canonical = str(uuid.UUID(job_id))
        if canonical != job_id:
            raise ValueError("invalid image id")
        return self.images / (canonical + (".preview.png" if preview else ".png"))
