"""手机审核台：导入草稿在网页上过目——通过 / 改 / 删 / 撤回（V3 质检瓶颈的解药）。

鉴权与 MCP 的 token **彻底分开**（PR #6 评审 P1）：持有 EMBER_OAUTH_ACCESS_TOKEN
的一方（含正常 OAuth 后的 claude.ai 后端）只该有 MCP 的权限，不该开得了审核台
——审核台是"轩本人质检"的门，尤其撤回动作能删正式记忆。
  - 浏览器：GET /review 登录页输 EMBER_OAUTH_PASSWORD → 下发签名 cookie（30 天，
    HMAC 密钥 = EMBER_REVIEW_TOKEN（缺省退回口令），重启不失效；SameSite=Lax 挡跨站 POST）
  - 脚本 / 提取会话：API 带 Bearer EMBER_REVIEW_TOKEN（openssl rand -hex 32，
    与 MCP 的 token 不是同一个；未设置该变量则 API 只认 cookie）
  - 门禁关闭（本地开发）= 免登录，与 MCP 行为一致
登录口令连错 5 次锁 60 秒（单用户，进程内计数即可）。
"""

import hashlib
import hmac
import os
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


def _review_token() -> str:
    return os.environ.get("EMBER_REVIEW_TOKEN", "")


def _secret() -> str:
    # cookie 签名密钥不用 MCP 的 access token——否则持有它的一方能伪造登录态，
    # P1 就从旁门绕回来了。口令兜底：它只有轩知道。
    return _review_token() or oauth._password()


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


def _valid_review_bearer(authorization: str | None) -> bool:
    token = _review_token()
    if not token or not authorization or not authorization.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(authorization[7:].strip().encode(), token.encode())


def _authed(request: Request) -> bool:
    if not oauth.oauth_enabled():
        return True  # 本地开发
    if _valid_review_bearer(request.headers.get("authorization")):
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
  #statsRow { display: flex; justify-content: space-between; align-items: center; gap: .5rem; margin-top: .25rem; }
  #stats { color: var(--dim); font-size: .85rem; }
  #batches { display: flex; gap: .4rem; overflow-x: auto; padding: .5rem 0 .2rem; }
  .chip { flex: none; border: 1px solid var(--line); border-radius: 999px; padding: .25rem .7rem; font-size: .8rem; color: var(--dim); background: none; }
  .chip.on { border-color: var(--accent); color: var(--accent); }
  main { padding: .8rem; display: grid; gap: .8rem; max-width: 640px; margin: 0 auto; }
  .card { background: var(--card); border-radius: 12px; padding: .9rem; }
  .meta { display: flex; flex-wrap: wrap; gap: .4rem; font-size: .75rem; color: var(--dim); margin-bottom: .5rem; align-items: center; }
  .badge { border: 1px solid var(--line); border-radius: 6px; padding: .05rem .4rem; }
  .badge.anchor { border-color: var(--accent); color: var(--accent); }
  .badge.interval { border-color: #6a8caf; color: #9dbbd8; }
  .badge.approved { border-color: var(--ok); color: var(--ok); }
  .badge.rejected { border-color: #8a4a42; color: #ff8a80; }
  .content { white-space: pre-wrap; line-height: 1.55; font-size: .95rem; }
  .quote { margin-top: .6rem; padding: .5rem .7rem; border-left: 3px solid var(--line); color: var(--dim); font-size: .82rem; white-space: pre-wrap; }
  .quote .ref { display: block; margin-top: .3rem; opacity: .75; word-break: break-all; }
  .other { border: 1px solid var(--line); border-radius: 8px; padding: .5rem .7rem; font-size: .85rem; color: var(--dim); line-height: 1.5; }
  .other.off { opacity: .45; }
  .other .odate { color: var(--accent); font-size: .75rem; margin-right: .4rem; }
  .other .warn { display: block; color: #ff8a80; font-size: .75rem; }
  .connector { display: flex; justify-content: center; align-items: center; gap: .3rem; margin: .3rem 0; }
  .connector .word { border: 1px solid var(--line); background: var(--bg); color: var(--accent); border-radius: 999px; padding: .2rem .8rem; font-size: .8rem; }
  .connector .warn { color: #ff8a80; }
  .connmenu { display: grid; margin: .2rem auto .4rem; background: #333; border-radius: 8px; padding: .3rem; width: max-content; }
  .connmenu button { background: none; border: 0; color: #eee; padding: .5rem 1rem; text-align: left; font-size: .88rem; border-radius: 6px; }
  .connmenu button:active { background: #4a4a4a; }
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
  <div id="statsRow">
    <div id="stats">加载中…</div>
    <button id="modeBtn" class="chip">↩ 已审核</button>
  </div>
  <div id="batches"></div>
</header>
<main id="list"></main>
<div id="empty" hidden>🎉 没有待审核的草稿</div>
<div id="toast"></div>
<script>
const $ = (s, el = document) => el.querySelector(s);
let currentBatch = "";
let mode = "pending";  // pending = 待审核 / reviewed = 反悔区

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
  if (mode === "reviewed") return loadReviewed();
  const q = currentBatch ? "&batch=" + encodeURIComponent(currentBatch) : "";
  const data = await api("/review/api/drafts?status=pending" + q);
  renderStats(data.stats);
  renderBatches(data.stats.by_batch);
  const list = $("#list");
  list.replaceChildren(...data.items.map(card));
  $("#empty").hidden = data.items.length > 0;
}

async function loadReviewed() {
  const [ok, no] = await Promise.all([
    api("/review/api/drafts?status=approved"),
    api("/review/api/drafts?status=rejected"),
  ]);
  $("#stats").textContent = "反悔区：已通过 " + ok.stats.total + " · 已删 " + no.stats.total;
  $("#batches").replaceChildren();
  const items = [...ok.items, ...no.items].sort((a, b) => b.id - a.id);
  $("#list").replaceChildren(...items.map(reviewedCard));
  $("#empty").hidden = items.length > 0;
}

$("#modeBtn").onclick = () => {
  mode = mode === "pending" ? "reviewed" : "pending";
  $("#modeBtn").textContent = mode === "pending" ? "↩ 已审核" : "← 回待审核";
  load();
};

function reviewedCard(d) {
  const el = document.createElement("div");
  el.className = "card";
  const meta = metaEl(d);
  const st = span(d.status === "approved" ? "✓ 已入库 → 记忆 #" + d.memory_id : "✕ 已删");
  st.className = "badge " + d.status;
  meta.prepend(st);
  el.append(meta, graphEl(d, null));
  const box = document.createElement("div");
  box.className = "actions";
  box.append(btn("↩ 撤回到待审核", "edit", async () => {
    await api("/review/api/drafts/" + d.id + "/unreview", { method: "POST" });
    el.remove();
    toast(d.status === "approved" ? "已撤回，记忆已删" : "已捞回待审核");
    load();
  }));
  el.append(box);
  return el;
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
  el.append(metaEl(d), graphEl(d, el), actionsEl(d, el));
  return el;
}

// 三明治排版（轩的定稿）：位置即因果——早的在上、箭头恒向下，
// 语言里永远只有"导致/覆盖"一个写法，标反了不改词、换座位（⇅ 交换 = 翻 dir）。
// 相关/矛盾/同一件事不分方向；"不关联"是下拉的一员（躺平可随时换回）。
const REL_WORDS = {
  led_to: "↓ 导致", supersedes: "↓ 覆盖，上面的作废",
  related: "～ 相关", contradicts: "≠ 矛盾（都留）", same_as: "＝ 同一件事",
  none: "✂ 不关联",
};
const REL_MENU = [
  ["led_to", "↓ 导致"], ["supersedes", "↓ 覆盖（旧的作废）"], ["related", "～ 相关"],
  ["contradicts", "≠ 矛盾（两条都留）"], ["same_as", "＝ 同一件事"], ["none", "✂ 不关联（单独入库）"],
];
const isUpper = (l) => (l.relation === "led_to" && l.dir === "in") || (l.relation === "supersedes" && l.dir === "out");
const isDirectional = (l) => l.relation === "led_to" || l.relation === "supersedes";

function graphEl(d, el) {
  // 上方：因/旧版；中间：本条；下方：果/新版/对称关系/不关联。el 给 null = 只读（反悔区）
  const wrap = document.createElement("div");
  const links = d.links || [];
  links.forEach((l, i) => {
    if (l.relation !== "none" && isUpper(l)) wrap.append(otherEl(d, l), connectorEl(d, el, l, i));
  });
  wrap.append(contentEl(d));
  if (d.quote || d.source_ref) wrap.append(quoteEl(d));
  links.forEach((l, i) => {
    if (l.relation === "none" || !isUpper(l)) wrap.append(connectorEl(d, el, l, i), otherEl(d, l));
  });
  return wrap;
}

function otherEl(d, link) {
  const t = link.target || {};
  const box = document.createElement("div");
  box.className = "other" + (link.relation === "none" ? " off" : "");
  const label = (t.kind === "draft" ? "草稿#" : "记忆#") + t.id;
  if (t.missing) { box.textContent = label + "（已不存在）"; return box; }
  const date = span(t.date || "");
  date.className = "odate";
  box.append(date, span(t.preview + "（" + label + "）"));
  if (t.kind === "draft" && t.status === "rejected") {
    const w = span("⚠ 对方已被拒，入库时这条线自动放弃");
    w.className = "warn";
    box.append(w);
  }
  return box;
}

function dateWarn(d, link) {
  // 位置即因果，所以"打架"只有一种样子：上面的日期比下面晚
  if (!isDirectional(link) || !link.target || !link.target.date || !d.date) return false;
  const upper = isUpper(link) ? link.target.date : d.date;
  const lower = isUpper(link) ? d.date : link.target.date;
  return upper > lower;
}

function connectorEl(d, el, link, idx) {
  const row = document.createElement("div");
  row.className = "connector";
  const word = btn(REL_WORDS[link.relation] + (el ? " ▾" : ""), "word", () => el && toggleMenu(d, el, link, idx, row));
  row.append(word);
  if (dateWarn(d, link)) {
    const w = span("⚠");
    w.className = "warn";
    w.title = "上面的日期比下面晚，检查因果方向或日期";
    row.append(w);
  }
  return row;
}

function toggleMenu(d, el, link, idx, anchor) {
  const open = el.querySelector(".connmenu");
  if (open) { open.remove(); return; }
  const menu = document.createElement("div");
  menu.className = "connmenu";
  for (const [value, label] of REL_MENU) {
    if (value === link.relation) continue;
    menu.append(btn(label, "", () => patchLink(d, el, idx, { relation: value },
      value === "none" ? "已设为不关联（会单独入库，随时可换回）" : "已改为「" + label + "」")));
  }
  if (isDirectional(link)) {
    menu.append(btn("⇅ 上下交换", "", () => patchLink(d, el, idx, { dir: link.dir === "out" ? "in" : "out" }, "换好座位了")));
  }
  menu.append(btn("收起", "", () => menu.remove()));
  anchor.after(menu);
}

async function patchLink(d, el, idx, change, msg) {
  const links = d.links.map((l, i) => (i === idx ? { ...l, ...change } : l));
  const updated = await api("/review/api/drafts/" + d.id, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ links }),
  });
  el.replaceWith(card(updated));
  toast(msg);
}

const STATUS_LABELS = { upcoming: "还没开始", ongoing: "进行中", ended: "已结束" };

function metaEl(d) {
  const meta = document.createElement("div");
  meta.className = "meta";
  const parts = ["#" + d.id, d.date, d.space, d.topic, d.batch].filter(Boolean);
  for (const p of parts) meta.append(span(p));
  const tier = span(d.tier);
  tier.className = "badge" + (d.tier === "anchor" ? " anchor" : "");
  meta.append(tier);
  if (d.start_date || d.end_date) {  // 区间型：起止 + 服务端现算的状态（V4 主打，不能盲审）
    const iv = span("⏳ " + (d.start_date || "…") + " → " + (d.end_date || "…")
      + "・" + (STATUS_LABELS[d.interval_status] || d.interval_status));
    iv.className = "badge interval";
    meta.append(iv);
  }
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
  const row3 = document.createElement("div"); row3.className = "row2";
  row3.append(
    add("start_date（区间起点，可空）", input("start_date", d.start_date)),
    add("end_date（区间终点，可空）", input("end_date", d.end_date)),
  );
  form.append(add("content", content), row1, row2, row3, add("tags（逗号分隔，中文逗号也行）", input("tags", d.tags)));
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
    try:
        ids = drafts.save_drafts(items)  # 整批原子：坏一条整批不写，脚本可放心重试
    except ValueError as e:
        return JSONResponse({"error": "invalid_draft", "error_description": str(e)}, status_code=400)
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


@router.post("/review/api/drafts/{draft_id}/unreview")
def api_unreview_draft(draft_id: int, request: Request):
    """反悔：已通过/已拒绝的草稿撤回 pending；通过的连生成的记忆一起删。"""
    if not _authed(request):
        return _unauthorized()
    try:
        result = drafts.unreview_draft(draft_id)
    except ValueError as e:
        return JSONResponse({"error": "conflict", "error_description": str(e)}, status_code=409)
    if result is None:
        return JSONResponse({"error": "not_found", "error_description": "草稿不存在或还在待审核"}, status_code=404)
    return result
