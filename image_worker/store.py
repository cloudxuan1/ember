"""Durable FIFO and idempotency; unrelated to Ember's memory database."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
import uuid


ERRORS = {
    "login_required": "Codex 订阅尚未登录或登录已失效，请让轩在生图容器重新完成设备码登录；不用提供密码或 API key。",
    "quota_exhausted": "Codex 订阅额度暂时不足，请等额度恢复后再明确要求重画；不会自动切换付费 API。",
    "generation_timeout": "这次画图超时，任务已停止，没有自动重试。要重画请明确提交一次新请求。",
    "generation_failed": "Codex 这次没有成功完成图片，原请求不会自动重试。",
    "no_image": "Codex 没有交回有效图片，不能把文字回复当成画图成功。原请求不会自动重试。",
    "worker_restarted": "生图服务在任务期间重启，任务已中断；为避免重复生成，没有自动重试。",
    "unsafe_tool": "本次任务尝试调用画图之外的命令工具，已停止，不会执行服务器命令。",
    "storage_full": "图片存储空间不足，任务没有开始；需要先由轩处理空间，不会自动删除旧图片。",
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

    def submit(self, prompt, request_id=""):
        prompt = prompt.strip()
        if not prompt or len(prompt) > 8000:
            raise ValueError("画图要求不能为空，且最多 8000 字。")
        if len(request_id) > 128:
            raise ValueError("请求标记最多 128 字。")
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        key = "request:" + request_id if request_id else "prompt:" + digest
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT * FROM jobs WHERE request_key=? OR id=(SELECT job_id FROM request_aliases WHERE request_key=?)", (key, key)).fetchone()
            if old:
                if old["prompt_hash"] != digest:
                    raise Conflict("同一个请求标记已用于另一段画图要求，请给新的请求换一个标记。")
                return self.public(old, conn), True
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
                "INSERT INTO jobs (id,request_key,prompt,prompt_hash,status,created_at) VALUES (?,?,?,?,?,?)",
                (job_id, key, prompt, digest, "queued", now()),
            )
            return self.public(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(), conn), False

    def public(self, row, conn):
        out = {k: row[k] for k in ("id", "status", "created_at", "started_at", "finished_at", "width", "height", "image_bytes", "command_events")}
        out["prompt_preview"] = row["prompt"][:80]
        if row["status"] == "queued":
            out["queue_position"] = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='queued' AND seq<=?", (row["seq"],)).fetchone()[0]
        if row["error_code"]:
            out["error"] = {"code": row["error_code"], "message": ERRORS.get(row["error_code"], ERRORS["generation_failed"])}
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
            return {"id": row["id"], "prompt": row["prompt"]}

    def finish(self, job_id, *, error=None, metadata=None):
        metadata = metadata or {}
        with self.connection() as conn:
            conn.execute(
                """UPDATE jobs SET status=?,finished_at=?,error_code=?,width=?,height=?,image_bytes=?,command_events=?
                   WHERE id=? AND status='running'""",
                ("failed" if error else "succeeded", now(), error, metadata.get("width"), metadata.get("height"),
                 metadata.get("image_bytes"), metadata.get("command_events", 0), job_id),
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
