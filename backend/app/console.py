"""记忆库网页（2026-10 单库改造后替掉旧审核台）：轩在手机上看、搜、写、改、删、回退、恢复、调设置。

鉴权与 MCP 的 token **彻底分开**（PR #6 评审 P1）：持有 EMBER_OAUTH_ACCESS_TOKEN
的一方（含正常 OAuth 后的 claude.ai 后端）只该有 MCP 的权限，不该开得了网页。
  - 浏览器：GET / 登录页输 EMBER_OAUTH_PASSWORD → 下发签名 cookie（30 天，
    HMAC 密钥 = 数据目录里自动生成的随机密钥文件，重启不失效；SameSite=Lax 挡跨站写请求）。
    网页上的一切操作都记成「轩」。
  - 提取脚本：只能 POST /api/memories 存记忆，带 Bearer EMBER_REVIEW_TOKEN（与 MCP 的 token 不是同一个；
    未设置该变量则脚本存不了）。脚本存的记成「小克」；改、删、回退、设置一律只认浏览器登录。
  - 门禁关闭（本地开发）= 免登录，与 MCP 行为一致
登录口令连错 5 次锁 60 秒（单用户，进程内计数即可）。
"""

import hashlib
import hmac
import os
import secrets
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import activity, history, memories, oauth, settings
from app.db import db_path

router = APIRouter()

COOKIE_NAME = "ember_session"
COOKIE_KEY_FILE = "review_cookie.key"  # 跟数据库同目录（Docker 卷），永不入库；沿用旧审核台的密钥文件
COOKIE_TTL_SECONDS = 30 * 24 * 60 * 60
LOCK_AFTER_FAILS = 5
LOCK_SECONDS = 60

# 网页本体（第二步）：backend/static/console/index.html；还没有时给个占位页
PAGE_FILE = Path(__file__).resolve().parent.parent / "static" / "console" / "index.html"

_login_guard = {"fails": 0, "locked_until": 0.0}


# ---------- cookie 签发与校验 ----------


def _review_token() -> str:
    return os.environ.get("EMBER_REVIEW_TOKEN", "")


_cookie_keys: dict = {}  # 密钥文件路径 → 密钥（测试里每个用例的库在不同目录）


def _secret() -> str:
    """cookie 签名密钥：数据目录里的随机密钥文件，首次用时生成。

    不用 MCP 的 access token（PR #6 P1），也不用 EMBER_REVIEW_TOKEN——那把钥匙在提取用的
    AI 会话手里，拿它签名等于 AI 能自己算出轩的登录态。
    也不直接拿口令当密钥：口令熵低，偷到一枚 cookie 就能离线猜口令。
    """
    path = db_path().parent / COOKIE_KEY_FILE
    key = _cookie_keys.get(path)
    if key:
        return key
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        # PID 不能区分线程池里的并发请求。每次使用独立候选文件，创建时就限定为 600，
        # 写完再原子发布；输掉竞争的请求只清理自己的候选，绝不改动赢家的密钥。
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f"{path.name}.", suffix=".tmp"
        ) as tmp:
            tmp.write(secrets.token_hex(32))
            tmp.flush()
            try:
                os.link(tmp.name, path)
            except FileExistsError:
                pass
    key = path.read_text().strip()
    _cookie_keys[path] = key
    return key


def _sign(payload: str) -> str:
    return hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()


def _make_cookie() -> str:
    expires = str(int(time.time()) + COOKIE_TTL_SECONDS)
    return f"{expires}.{_sign(expires)}"


def _valid_cookie(value: str) -> bool:
    expires, _, sig = value.partition(".")
    if not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(sig.encode(), _sign(expires).encode())


def _valid_script_bearer(authorization: str | None) -> bool:
    token = _review_token()
    if not token or not authorization or not authorization.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(authorization[7:].strip().encode(), token.encode())


def _is_xuan(request: Request) -> bool:
    """轩本人的浏览器登录（cookie）。脚本钥匙不算——那把钥匙在提取用的 AI 会话手里。"""
    if not oauth.oauth_enabled():
        return True  # 本地开发
    return _valid_cookie(request.cookies.get(COOKIE_NAME, ""))


def _unauthorized() -> JSONResponse:
    return JSONResponse({"error": "unauthorized"}, status_code=401)


def _bad(message: str, error: str = "invalid_request") -> JSONResponse:
    return JSONResponse({"error": error, "error_description": message}, status_code=400)


def _not_found(message: str) -> JSONResponse:
    return JSONResponse({"error": "not_found", "error_description": message}, status_code=404)


async def _json_body(request: Request):
    if not int(request.headers.get("content-length") or 0):
        return {}
    try:
        return await request.json()
    except ValueError:
        return None


# ---------- 页面 ----------


LOGIN_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ember</title>
<link rel="stylesheet" href="/static/fonts/fonts.css">
<style>
  body { font-family: "Noto Sans SC", system-ui, sans-serif; display: grid; place-items: center; min-height: 100vh; margin: 0; background: #F8F5F0; color: #26211D; }
  form { background: rgba(255,255,255,.8); padding: 2rem; border-radius: 22px; width: min(320px, 85vw); box-shadow: 0 10px 28px rgba(70,45,30,.08); }
  h1 { font-family: "Noto Serif SC", Georgia, serif; font-weight: 600; font-size: 1.3rem; margin: 0 0 1rem; }
  input[type=password] { width: 100%; box-sizing: border-box; padding: .7rem .9rem; border-radius: 14px; border: 1px solid rgba(38,33,29,.12); background: #fff; color: #26211D; font-size: 16px; }
  button { margin-top: 1rem; width: 100%; padding: .75rem; border: 0; border-radius: 999px; background: #2B2622; color: #fff; font-size: 1rem; font-weight: 600; }
  .err { color: #B3261E; }
</style></head><body>
<form method="post" action="/login">
  <h1>ember</h1>
  <p>确认是轩本人：</p>
  {error_html}
  <input type="password" name="password" placeholder="口令" autofocus>
  <button type="submit">进入</button>
</form></body></html>"""

PLACEHOLDER_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ember</title></head><body style="font-family: system-ui, sans-serif; padding: 2rem;">
<p>记忆库网页还没装上（第二步）。接口已经就绪：/api/memories、/api/activity、/api/settings。</p>
</body></html>"""


def _login_page(error: str = "") -> HTMLResponse:
    error_html = f'<p class="err">{error}</p>' if error else ""
    return HTMLResponse(
        LOGIN_PAGE.replace("{error_html}", error_html),
        status_code=403 if error else 200,
    )


@router.get("/")
def console_page(request: Request):
    if not _is_xuan(request):
        return _login_page()
    page = PAGE_FILE.read_text(encoding="utf-8") if PAGE_FILE.exists() else PLACEHOLDER_PAGE
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@router.get("/review")
def old_review_address():
    """旧审核台的书签：送到新网页。"""
    return RedirectResponse("/", status_code=302)


@router.post("/login")
async def login(request: Request):
    if not oauth.oauth_enabled():
        return RedirectResponse("/", status_code=302)
    now = time.time()
    if now < _login_guard["locked_until"]:
        return _login_page(f"错太多次了，{int(_login_guard['locked_until'] - now) + 1} 秒后再试")
    form = await request.form()
    password = str(form.get("password") or "")
    if not hmac.compare_digest(password.encode(), oauth._password().encode()):
        _login_guard["fails"] += 1
        if _login_guard["fails"] >= LOCK_AFTER_FAILS:
            _login_guard["locked_until"] = now + LOCK_SECONDS
            _login_guard["fails"] = 0
        return _login_page("口令不对，再试试")
    _login_guard["fails"] = 0
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(
        COOKIE_NAME, _make_cookie(),
        max_age=COOKIE_TTL_SECONDS, httponly=True, secure=True, samesite="lax", path="/",
    )
    return resp


# ---------- 记忆 ----------


@router.get("/api/memories")
def api_browse(
    request: Request,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
    date_from: str | None = None,
    date_to: str | None = None,
    tag: str | None = None,
    space: str | None = None,
):
    """记忆库列表：默认跨全库，按发生日期新的在前，content 不截断，带 history_count。"""
    if not _is_xuan(request):
        return _unauthorized()
    memories.purge_expired()
    try:
        return memories.browse_memories(
            q=q, space=space, page=page, page_size=page_size,
            date_from=date_from, date_to=date_to, tag=tag,
        )
    except ValueError as e:
        return _bad(str(e))


@router.post("/api/memories", status_code=201)
async def api_save(request: Request):
    """存：网页手写一条（记成轩），或提取脚本带 Bearer 推一批 {"memories": [...]}（记成小克，整批原子）。"""
    if _is_xuan(request):
        by = "xuan"
    elif _valid_script_bearer(request.headers.get("authorization")):
        by = "ke"
    else:
        return _unauthorized()
    body = await _json_body(request)
    if not isinstance(body, dict) or not body:
        return _bad("要存的内容要是 JSON 对象")
    items = body.get("memories") if "memories" in body else [body]
    try:
        ids = memories.save_many(items, by=by)
    except ValueError as e:
        return _bad(str(e), "invalid_memory")
    if "memories" in body:
        return {"saved": len(ids), "ids": ids}
    return {"saved": 1, "id": ids[0], "memory": memories.get_memory(ids[0])}


@router.get("/api/memories/{memory_id}")
def api_get(memory_id: int, request: Request):
    """一条的全文 + 来源 + 连线 + 留着的上一版（新的在前）。"""
    if not _is_xuan(request):
        return _unauthorized()
    memory = memories.get_memory(memory_id)
    if memory is None:
        return _not_found("这条记忆不存在或已经删掉了")
    memory["history"] = history.list_for(memory_id)
    return memory


@router.patch("/api/memories/{memory_id}")
async def api_update(memory_id: int, request: Request):
    """直接改，立刻生效；改前的进上一版。带 expect_updated_at（打开时看到的修改时间）防冲掉别人刚改的。"""
    if not _is_xuan(request):
        return _unauthorized()
    body = await _json_body(request)
    if not isinstance(body, dict):
        return _bad("要改的内容要是 JSON 对象")
    kwargs = {}
    if "expect_updated_at" in body:
        kwargs["expect_updated_at"] = body.pop("expect_updated_at")
    try:
        result = memories.update_memory(memory_id, body, by="xuan", **kwargs)
    except memories.Conflict:
        return JSONResponse(
            {"error": "conflict", "error_description": "这条在你打开之后又被改过（多半是小克），已刷新，再看一眼"},
            status_code=409,
        )
    except ValueError as e:
        return _bad(str(e), "invalid_memory")
    if result is None:
        return _not_found("这条记忆不存在或已经删掉了")
    memory = result["memory"]
    memory["history"] = history.list_for(memory_id)
    return {"changed": result["changed"], "memory": memory}


@router.delete("/api/memories/{memory_id}")
def api_delete(memory_id: int, request: Request):
    """删：先藏起来，最近动静里能恢复。确认弹窗在网页上做。"""
    if not _is_xuan(request):
        return _unauthorized()
    result = memories.delete_memory(memory_id, by="xuan")
    if result is None:
        return _not_found("这条记忆不存在或已经删掉了")
    return result


@router.post("/api/memories/{memory_id}/restore")
def api_restore(memory_id: int, request: Request):
    if not _is_xuan(request):
        return _unauthorized()
    result = memories.restore_memory(memory_id, by="xuan")
    if result is None:
        return _not_found("这条没有在删除状态，或已经过了恢复期限")
    return result


@router.post("/api/memories/{memory_id}/revert")
async def api_revert(memory_id: int, request: Request):
    """退回到留着的某一版：{"history_id": 那一版的编号}。"""
    if not _is_xuan(request):
        return _unauthorized()
    body = await _json_body(request)
    history_id = body.get("history_id") if isinstance(body, dict) else None
    if not isinstance(history_id, int) or isinstance(history_id, bool):
        return _bad("要带上 history_id（退到哪一版）")
    result = memories.revert_memory(memory_id, history_id, by="xuan")
    if result is None:
        return _not_found("这一版已经不在了（可能被清掉或已经回退过），已刷新")
    memory = result["memory"]
    memory["history"] = history.list_for(memory_id)
    return {"reverted_to": history_id, "memory": memory}


@router.delete("/api/memories/{memory_id}/history")
def api_clear_history(memory_id: int, request: Request, history_id: int | None = None):
    """清掉这条的上一版：带 history_id 清那一版，不带清这条的全部。"""
    if not _is_xuan(request):
        return _unauthorized()
    return {"cleared": history.clear(memory_id=memory_id, history_id=history_id)}


@router.delete("/api/history")
def api_clear_all_history(request: Request):
    """设置里的「全部清空上一版」。"""
    if not _is_xuan(request):
        return _unauthorized()
    return {"cleared": history.clear()}


@router.get("/api/tags")
def api_tags(request: Request):
    if not _is_xuan(request):
        return _unauthorized()
    return {"tags": memories.list_tags()}


# ---------- 最近动静 / 设置 ----------


@router.get("/api/activity")
def api_activity(request: Request):
    if not _is_xuan(request):
        return _unauthorized()
    memories.purge_expired()
    return activity.list_activity()


@router.get("/api/settings")
def api_settings(request: Request):
    if not _is_xuan(request):
        return _unauthorized()
    return settings.get_settings()


@router.patch("/api/settings")
async def api_update_settings(request: Request):
    if not _is_xuan(request):
        return _unauthorized()
    body = await _json_body(request)
    try:
        updated = settings.update_settings(body if isinstance(body, dict) else {})
    except ValueError as e:
        return _bad(str(e))
    memories.purge_expired()  # 恢复期限调短了：过期的删除马上真删
    return updated
