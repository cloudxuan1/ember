"""手机审核台：导入草稿在网页上过目——通过 / 改 / 删（V3 质检瓶颈的解药）。

鉴权复用 OAuth 门禁的两个值，不新增密钥（生成方式见 app/oauth.py）：
  - 浏览器：GET /review 登录页输 EMBER_OAUTH_PASSWORD → 下发签名 cookie（30 天，
    HMAC 密钥 = EMBER_OAUTH_ACCESS_TOKEN，重启不失效；SameSite=Lax 挡跨站 POST）
  - 脚本 / 提取会话：API 直接带 Bearer EMBER_OAUTH_ACCESS_TOKEN（与 MCP 相同）
  - 门禁关闭（本地开发）= 免登录，与 MCP 行为一致
登录口令连错 5 次锁 60 秒（单用户，进程内计数即可）。
"""

import hashlib
import hmac
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import drafts, oauth

router = APIRouter()

COOKIE_NAME = "ember_review"
COOKIE_TTL_SECONDS = 30 * 24 * 60 * 60
LOCK_AFTER_FAILS = 5
LOCK_SECONDS = 60

_login_guard = {"fails": 0, "locked_until": 0.0}


# ---------- cookie 签发与校验 ----------


def _sign(payload: str) -> str:
    key = oauth._access_token().encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def _make_cookie() -> str:
    expires = str(int(time.time()) + COOKIE_TTL_SECONDS)
    return f"{expires}.{_sign(expires)}"


def _valid_cookie(value: str) -> bool:
    expires, _, sig = value.partition(".")
    if not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(sig.encode(), _sign(expires).encode())


def _authed(request: Request) -> bool:
    if not oauth.oauth_enabled():
        return True  # 本地开发
    if oauth.valid_bearer(request.headers.get("authorization")):
        return True
    return _valid_cookie(request.cookies.get(COOKIE_NAME, ""))


def _unauthorized() -> JSONResponse:
    return JSONResponse({"error": "unauthorized"}, status_code=401)


# ---------- 页面 ----------


LOGIN_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ember 审核台</title>
<style>
  body { font-family: system-ui, sans-serif; display: grid; place-items: center; min-height: 100vh; margin: 0; background: #1a1614; color: #eee; }
  form { background: #262019; padding: 2rem; border-radius: 12px; width: min(320px, 85vw); }
  h1 { font-size: 1.2rem; margin: 0 0 1rem; } h1::before { content: "🔥 "; }
  input[type=password] { width: 100%; box-sizing: border-box; padding: .6rem; border-radius: 8px; border: 1px solid #555; background: #1a1614; color: #eee; }
  button { margin-top: 1rem; width: 100%; padding: .6rem; border: 0; border-radius: 8px; background: #d97742; color: #fff; font-size: 1rem; }
  .err { color: #ff8a80; }
</style></head><body>
<form method="post" action="/review/login">
  <h1>ember 审核台</h1>
  <p>确认是轩本人在审核：</p>
  {error_html}
  <input type="password" name="password" placeholder="口令" autofocus>
  <button type="submit">进入审核台</button>
</form></body></html>"""


CONSOLE_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ember 审核台</title>
<style>
  :root { --bg: #1a1614; --card: #262019; --line: #3a3128; --accent: #d97742; --ok: #7cb87c; --dim: #a89a8c; }
  * { box-sizing: border-box; }
  body { font-family: system-ui, sans-serif; margin: 0; background: var(--bg); color: #eee; padding-bottom: 4rem; }
  header { position: sticky; top: 0; background: var(--bg); padding: .8rem 1rem .5rem; border-bottom: 1px solid var(--line); z-index: 2; }
  h1 { font-size: 1.1rem; margin: 0; } h1::before { content: "🔥 "; }
  #stats { color: var(--dim); font-size: .85rem; margin-top: .25rem; }
  #batches { display: flex; gap: .4rem; overflow-x: auto; padding: .5rem 0 .2rem; }
  .chip { flex: none; border: 1px solid var(--line); border-radius: 999px; padding: .25rem .7rem; font-size: .8rem; color: var(--dim); background: none; }
  .chip.on { border-color: var(--accent); color: var(--accent); }
  main { padding: .8rem; display: grid; gap: .8rem; max-width: 640px; margin: 0 auto; }
  .card { background: var(--card); border-radius: 12px; padding: .9rem; }
  .meta { display: flex; flex-wrap: wrap; gap: .4rem; font-size: .75rem; color: var(--dim); margin-bottom: .5rem; align-items: center; }
  .badge { border: 1px solid var(--line); border-radius: 6px; padding: .05rem .4rem; }
  .badge.anchor { border-color: var(--accent); color: var(--accent); }
  .content { white-space: pre-wrap; line-height: 1.55; font-size: .95rem; }
  .quote { margin-top: .6rem; padding: .5rem .7rem; border-left: 3px solid var(--line); color: var(--dim); font-size: .82rem; white-space: pre-wrap; }
  .quote .ref { display: block; margin-top: .3rem; opacity: .75; word-break: break-all; }
  .actions { display: flex; gap: .5rem; margin-top: .8rem; }
  .actions button { flex: 1; padding: .55rem 0; border: 0; border-radius: 8px; font-size: .95rem; color: #fff; }
  .approve { background: #4a7a4a; } .edit { background: #55606e; } .reject { background: #8a4a42; }
  .editor { display: grid; gap: .5rem; margin-top: .6rem; }
  .editor label { font-size: .75rem; color: var(--dim); display: grid; gap: .2rem; }
  .editor input, .editor textarea, .editor select { width: 100%; padding: .45rem; border-radius: 8px; border: 1px solid #555; background: var(--bg); color: #eee; font: inherit; font-size: .9rem; }
  .editor textarea { min-height: 7rem; }
  .row2 { display: grid; grid-template-columns: 1fr 1fr; gap: .5rem; }
  #empty { text-align: center; color: var(--dim); padding: 3rem 1rem; }
  #toast { position: fixed; bottom: 1rem; left: 50%; transform: translateX(-50%); background: #333; color: #fff; padding: .5rem 1rem; border-radius: 8px; font-size: .85rem; opacity: 0; transition: opacity .3s; pointer-events: none; }
  #toast.show { opacity: 1; }
</style></head><body>
<header>
  <h1>ember 审核台</h1>
  <div id="stats">加载中…</div>
  <div id="batches"></div>
</header>
<main id="list"></main>
<div id="empty" hidden>🎉 没有待审核的草稿</div>
<div id="toast"></div>
<script>
const $ = (s, el = document) => el.querySelector(s);
let currentBatch = "";

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 1600);
}

async function api(path, options) {
  const resp = await fetch(path, options);
  if (resp.status === 401) { location.reload(); throw new Error("未登录"); }
  const data = await resp.json();
  if (!resp.ok) { toast(data.error_description || data.error || "出错了"); throw new Error(data.error); }
  return data;
}

async function load() {
  const q = currentBatch ? "&batch=" + encodeURIComponent(currentBatch) : "";
  const data = await api("/review/api/drafts?status=pending" + q);
  renderStats(data.stats);
  renderBatches(data.stats.by_batch);
  const list = $("#list");
  list.replaceChildren(...data.items.map(card));
  $("#empty").hidden = data.items.length > 0;
}

function renderStats(stats) {
  const n = Object.values(stats.by_batch).reduce((a, b) => a + b, 0);
  $("#stats").textContent = "待审核 " + n + " 条" + (currentBatch ? "（当前批次 " + stats.total + " 条）" : "");
}

function renderBatches(byBatch) {
  const names = Object.keys(byBatch).sort();
  const box = $("#batches");
  box.replaceChildren();
  if (names.length < 2 && !currentBatch) return;
  const all = chipEl("全部", "" === currentBatch);
  all.onclick = () => { currentBatch = ""; load(); };
  box.append(all);
  for (const name of names) {
    const chip = chipEl((name || "（无批次）") + " · " + byBatch[name], name === currentBatch);
    chip.onclick = () => { currentBatch = name; load(); };
    box.append(chip);
  }
}

function chipEl(text, on) {
  const b = document.createElement("button");
  b.className = "chip" + (on ? " on" : "");
  b.textContent = text;
  return b;
}

function card(d) {
  const el = document.createElement("div");
  el.className = "card";
  el.append(metaEl(d), contentEl(d));
  if (d.quote || d.source_ref) el.append(quoteEl(d));
  el.append(actionsEl(d, el));
  return el;
}

function metaEl(d) {
  const meta = document.createElement("div");
  meta.className = "meta";
  const parts = ["#" + d.id, d.date, d.space, d.topic, d.batch].filter(Boolean);
  for (const p of parts) meta.append(span(p));
  const tier = span(d.tier);
  tier.className = "badge" + (d.tier === "anchor" ? " anchor" : "");
  meta.append(tier);
  if (d.tags) meta.append(span("🏷 " + d.tags));
  return meta;
}

function span(text) { const s = document.createElement("span"); s.textContent = text; return s; }

function contentEl(d) {
  const c = document.createElement("div");
  c.className = "content";
  c.textContent = d.content;
  return c;
}

function quoteEl(d) {
  const q = document.createElement("div");
  q.className = "quote";
  q.textContent = d.quote || "";
  if (d.source_ref) {
    const ref = document.createElement("span");
    ref.className = "ref";
    ref.textContent = "📎 " + d.source_ref;
    q.append(ref);
  }
  return q;
}

function actionsEl(d, el) {
  const box = document.createElement("div");
  box.className = "actions";
  box.append(
    btn("✓ 通过", "approve", () => act(d.id, "approve", el)),
    btn("✎ 改", "edit", () => openEditor(d, el)),
    btn("✕ 删", "reject", () => confirm("确定不要这条草稿？") && act(d.id, "reject", el)),
  );
  return box;
}

function btn(text, cls, onclick) {
  const b = document.createElement("button");
  b.className = cls;
  b.textContent = text;
  b.onclick = onclick;
  return b;
}

async function act(id, action, el, edits) {
  await api("/review/api/drafts/" + id + "/" + action, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(edits || {}),
  });
  el.remove();
  toast(action === "approve" ? "已入库 ✓" : "已拒绝");
  load();
}

function openEditor(d, el) {
  const form = document.createElement("div");
  form.className = "editor";
  const fields = {};
  const add = (label, node) => {
    const wrap = document.createElement("label");
    wrap.append(label, node);
    return wrap;
  };
  const input = (name, value) => {
    const i = document.createElement("input");
    i.value = value || "";
    fields[name] = i;
    return i;
  };
  const content = document.createElement("textarea");
  content.value = d.content;
  fields.content = content;
  const tier = document.createElement("select");
  for (const t of ["normal", "anchor", "process"]) {
    const o = document.createElement("option");
    o.value = o.textContent = t;
    o.selected = d.tier === t;
    tier.append(o);
  }
  fields.tier = tier;
  const row1 = document.createElement("div"); row1.className = "row2";
  row1.append(add("date", input("date", d.date)), add("tier", tier));
  const row2 = document.createElement("div"); row2.className = "row2";
  row2.append(add("topic", input("topic", d.topic)), add("space", input("space", d.space)));
  form.append(add("content", content), row1, row2, add("tags", input("tags", d.tags)));
  const actions = document.createElement("div");
  actions.className = "actions";
  const values = () => Object.fromEntries(Object.entries(fields).map(([k, i]) => [k, i.value]));
  actions.append(
    btn("✓ 保存并通过", "approve", () => act(d.id, "approve", el, values())),
    btn("仅保存", "edit", async () => {
      const updated = await api("/review/api/drafts/" + d.id, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values()),
      });
      el.replaceWith(card(updated));
      toast("已保存");
    }),
    btn("取消", "reject", () => el.replaceWith(card(d))),
  );
  form.append(actions);
  el.replaceChildren(metaEl(d), form);
}

load();
</script></body></html>"""


def _login_page(error: str = "") -> HTMLResponse:
    error_html = f'<p class="err">{error}</p>' if error else ""
    return HTMLResponse(
        LOGIN_PAGE.replace("{error_html}", error_html),
        status_code=403 if error else 200,
    )


@router.get("/review")
def review_console(request: Request):
    if not _authed(request):
        return _login_page()
    return HTMLResponse(CONSOLE_PAGE)


@router.post("/review/login")
async def review_login(request: Request):
    if not oauth.oauth_enabled():
        return RedirectResponse("/review", status_code=302)
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
    resp = RedirectResponse("/review", status_code=302)
    resp.set_cookie(
        COOKIE_NAME, _make_cookie(),
        max_age=COOKIE_TTL_SECONDS, httponly=True, secure=True, samesite="lax", path="/review",
    )
    return resp


# ---------- API（cookie 或 Bearer 均可） ----------


@router.get("/review/api/drafts")
def api_list_drafts(request: Request, status: str = "pending", batch: str | None = None):
    if not _authed(request):
        return _unauthorized()
    return drafts.list_drafts(status=status, batch=batch)


@router.post("/review/api/drafts", status_code=201)
async def api_save_drafts(request: Request):
    """批量收草稿：{"drafts": [{...}]} 或单条 {...}。提取会话带 Bearer 直接推。"""
    if not _authed(request):
        return _unauthorized()
    body = await request.json()
    items = body.get("drafts") if isinstance(body, dict) and "drafts" in body else [body]
    if not isinstance(items, list) or not items:
        return JSONResponse({"error": "invalid_request", "error_description": "drafts 要是非空列表"}, status_code=400)
    ids = []
    try:
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("每条草稿要是 JSON 对象")
            allowed = {k: v for k, v in item.items() if k in drafts.EDITABLE_FIELDS}
            ids.append(drafts.save_draft(**allowed)["id"])
    except (ValueError, TypeError) as e:
        # 整批原子性不重要（每条独立），但报错要指出坏在第几条
        return JSONResponse(
            {"error": "invalid_draft", "error_description": f"第 {len(ids) + 1} 条有问题：{e}", "saved_ids": ids},
            status_code=400,
        )
    return {"saved": len(ids), "ids": ids}


@router.patch("/review/api/drafts/{draft_id}")
async def api_update_draft(draft_id: int, request: Request):
    if not _authed(request):
        return _unauthorized()
    try:
        updated = drafts.update_draft(draft_id, await request.json())
    except ValueError as e:
        return JSONResponse({"error": "invalid_draft", "error_description": str(e)}, status_code=400)
    if updated is None:
        return JSONResponse({"error": "not_found", "error_description": "草稿不存在或已审核过"}, status_code=404)
    return updated


@router.post("/review/api/drafts/{draft_id}/approve")
async def api_approve_draft(draft_id: int, request: Request):
    if not _authed(request):
        return _unauthorized()
    edits = await request.json() if int(request.headers.get("content-length") or 0) else None
    try:
        result = drafts.approve_draft(draft_id, edits=edits)
    except ValueError as e:
        return JSONResponse({"error": "invalid_draft", "error_description": str(e)}, status_code=400)
    if result is None:
        return JSONResponse({"error": "not_found", "error_description": "草稿不存在或已审核过"}, status_code=404)
    return result


@router.post("/review/api/drafts/{draft_id}/reject")
def api_reject_draft(draft_id: int, request: Request):
    if not _authed(request):
        return _unauthorized()
    result = drafts.reject_draft(draft_id)
    if result is None:
        return JSONResponse({"error": "not_found", "error_description": "草稿不存在或已审核过"}, status_code=404)
    return result
