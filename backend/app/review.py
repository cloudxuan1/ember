"""手机审核台：导入草稿在网页上过目——通过 / 改 / 删 / 撤回（V3 质检瓶颈的解药）。

鉴权与 MCP 的 token **彻底分开**（PR #6 评审 P1）：持有 EMBER_OAUTH_ACCESS_TOKEN
的一方（含正常 OAuth 后的 claude.ai 后端）只该有 MCP 的权限，不该开得了审核台
——审核台是"轩本人质检"的门，尤其撤回动作能删正式记忆。
  - 浏览器：GET /review 登录页输 EMBER_OAUTH_PASSWORD → 下发签名 cookie（30 天，
    HMAC 密钥 = 数据目录里自动生成的随机密钥文件，重启不失效；SameSite=Lax 挡跨站 POST）
  - 脚本 / 提取会话：API 带 Bearer EMBER_REVIEW_TOKEN（openssl rand -hex 32，
    与 MCP 的 token 不是同一个；未设置该变量则 API 只认 cookie）
  - 门禁关闭（本地开发）= 免登录，与 MCP 行为一致
登录口令连错 5 次锁 60 秒（单用户，进程内计数即可）。
"""

import hashlib
import hmac
import os
import secrets
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import drafts, memories, memory_edits, oauth
from app.db import db_path

router = APIRouter()

COOKIE_NAME = "ember_review"
COOKIE_KEY_FILE = "review_cookie.key"  # 跟数据库同目录（Docker 卷），永不入库
COOKIE_TTL_SECONDS = 30 * 24 * 60 * 60
LOCK_AFTER_FAILS = 5
LOCK_SECONDS = 60

_login_guard = {"fails": 0, "locked_until": 0.0}


# ---------- cookie 签发与校验 ----------


def _review_token() -> str:
    return os.environ.get("EMBER_REVIEW_TOKEN", "")


_cookie_keys: dict = {}  # 密钥文件路径 → 密钥（测试里每个用例的库在不同目录）


def _secret() -> str:
    """cookie 签名密钥：数据目录里的随机密钥文件，首次用时生成。

    不用 MCP 的 access token（PR #6 P1），也不用 EMBER_REVIEW_TOKEN——那把钥匙在提取用的
    AI 会话手里，拿它签名等于 AI 能自己算出轩的登录态，"确认 / 驳回只认浏览器登录"就形同虚设。
    也不直接拿口令当密钥：口令熵低，偷到一枚 cookie 就能离线猜口令。
    """
    path = db_path().parent / COOKIE_KEY_FILE
    key = _cookie_keys.get(path)
    if key:
        return key
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(secrets.token_hex(32))
        os.chmod(tmp, 0o600)
        try:
            os.link(tmp, path)  # 原子落地：并发首建只有一份生效，没人读到写了一半的文件
        except FileExistsError:
            pass
        finally:
            tmp.unlink()
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


def _authed_human(request: Request) -> bool:
    """只认轩本人的浏览器登录（cookie），不认脚本钥匙 EMBER_REVIEW_TOKEN——
    那把钥匙在提取用的 AI 会话手里，AI 不能自己确认自己提的改动。"""
    if not oauth.oauth_enabled():
        return True
    return _valid_cookie(request.cookies.get(COOKIE_NAME, ""))


def _unauthorized() -> JSONResponse:
    return JSONResponse({"error": "unauthorized"}, status_code=401)


# ---------- 页面 ----------


LOGIN_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ember 审核台</title>
<link rel="stylesheet" href="/static/fonts/fonts.css">
<style>
  body { font-family: "Noto Sans SC", system-ui, sans-serif; display: grid; place-items: center; min-height: 100vh; margin: 0; background: #FFF6DE; color: #4A3D2E; }
  form { background: #FFFEF8; padding: 2rem; border-radius: 12px; width: min(320px, 85vw); }
  h1 { font-family: "Noto Serif SC", serif; font-weight: 900; font-size: 1.2rem; margin: 0 0 1rem; } h1::before { content: "🔥 "; }
  input[type=password] { width: 100%; box-sizing: border-box; padding: .6rem; border-radius: 8px; border: 1px solid #DFCFA8; background: #FFF6DE; color: #4A3D2E; }
  button { margin-top: 1rem; width: 100%; padding: .6rem; border: 0; border-radius: 8px; background: #F48F68; color: #5C2410; font-size: 1rem; }
  .err { color: #C24A28; }
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
<link rel="stylesheet" href="/static/fonts/fonts.css">
<style>
  :root { --bg: #FFF6DE; --card: #FFFEF8; --line: #EBDDBE; --accent: #E0602F; --ok: #2E7B78; --dim: #9C8A6E; }
  * { box-sizing: border-box; }
  body { font-family: "Noto Sans SC", system-ui, sans-serif; margin: 0; background: var(--bg); color: #4A3D2E; padding-bottom: 4rem; }
  header { position: sticky; top: 0; background: var(--bg); padding: .8rem 1rem .5rem; border-bottom: 1px solid var(--line); z-index: 2; }
  h1 { font-family: "Noto Serif SC", serif; font-weight: 900; font-size: 1.1rem; margin: 0; } h1::before { content: "🔥 "; }
  #statsRow { display: flex; justify-content: flex-start; align-items: center; gap: .5rem; margin-top: .25rem; flex-wrap: wrap; }
  #stats { font-family: "Fira Code", ui-monospace, monospace; color: var(--dim); font-size: .85rem; margin-right: auto; }
  #batches { display: flex; gap: .4rem; overflow-x: auto; padding: .5rem 0 .2rem; align-items: center; }
  .memsearch { flex: 1; min-width: 9rem; padding: .3rem .7rem; border-radius: 999px; border: 1px solid var(--line); background: var(--card); color: #4A3D2E; font-size: .85rem; }
  .chip { flex: none; font-family: "Fira Code", ui-monospace, monospace; border: 1px solid var(--line); border-radius: 999px; padding: .25rem .7rem; font-size: .8rem; color: var(--dim); background: none; }
  .chip.on { border-color: var(--accent); color: var(--accent); }
  main { padding: .8rem; display: grid; gap: .8rem; max-width: 640px; margin: 0 auto; }
  .card { background: var(--card); border-radius: 12px; padding: .9rem; }
  .meta { display: flex; flex-wrap: wrap; gap: .4rem; font-family: "Fira Code", ui-monospace, monospace; font-size: .75rem; color: var(--dim); margin-bottom: .5rem; align-items: center; }
  .badge { font-family: "Fira Code", ui-monospace, monospace; border: 1px solid var(--line); border-radius: 6px; padding: .05rem .4rem; }
  .badge.anchor { border-color: var(--accent); color: var(--accent); }
  .badge.interval { border-color: #8BDFDD; color: #2E7B78; }
  .badge.approved { border-color: var(--ok); color: var(--ok); }
  .badge.rejected { border-color: #F48F68; color: #C24A28; }
  .content { white-space: pre-wrap; line-height: 1.55; font-size: .95rem; }
  .quote { margin-top: .6rem; padding: .5rem .7rem; border-left: 3px solid var(--line); color: var(--dim); font-size: .82rem; white-space: pre-wrap; }
  .quote .ref { display: block; font-family: "Fira Code", ui-monospace, monospace; margin-top: .3rem; opacity: .75; word-break: break-all; }
  .membox { background: var(--bg); border: 1px solid var(--line); border-radius: 10px; padding: .55rem .75rem; margin-top: .5rem; }
  .membox .boxid, .addtitle, .edithead { display: flex; align-items: baseline; gap: .5rem; flex-wrap: wrap; font-family: "Noto Serif SC", serif; font-size: 1.05rem; font-weight: 900; color: var(--accent); }
  .odate { font-family: "Fira Code", ui-monospace, monospace; }
  .membox .boxid .odate { font-size: .72rem; font-weight: 400; color: var(--dim); }
  .membox .boxid .meta, .edithead .meta { margin: 0; font-weight: 400; }
  .membox .boxtext { white-space: pre-wrap; line-height: 1.55; font-size: .95rem; margin-top: .3rem; }
  .membox.target .boxtext { font-size: .85rem; color: var(--dim); }
  .membox .warn { display: block; color: #C24A28; font-size: .75rem; margin-top: .3rem; }
  .linkframe { border: 1px solid var(--accent); border-radius: 12px; padding: .15rem .6rem .6rem; margin-top: .7rem; }
  .linkgroup.off { opacity: .5; }
  .verb { position: relative; display: flex; align-items: center; gap: .5rem; margin-top: .5rem; padding-left: .2rem; }
  .verb .word { background: none; border: 0; padding: .1rem .2rem; font-size: .95rem; font-weight: 600; color: var(--accent); text-decoration: underline dotted; }
  .verb .swap { background: none; border: 1px solid var(--line); border-radius: 6px; color: var(--dim); font-size: .75rem; padding: .15rem .5rem; }
  .verb .warn { color: #C24A28; }
  .connmenu { position: absolute; left: .2rem; top: 100%; z-index: 10; display: grid; background: #FFFEF8; border-radius: 8px; padding: .3rem; box-shadow: 0 4px 16px rgba(90,70,40,.25); }
  .connmenu button { background: none; border: 0; color: #4A3D2E; padding: .55rem 1.1rem; text-align: left; font-size: .9rem; border-radius: 6px; }
  .connmenu button:active { background: #FFE394; }
  .actions { display: flex; gap: .5rem; margin-top: .8rem; }
  .actions button { flex: 1; padding: .55rem 0; border: 0; border-radius: 8px; font-size: .95rem; }
  .approve { background: #8BDFDD; color: #1C4E4B; } .edit { background: #FFE394; color: #6B5310; } .reject { background: #F48F68; color: #5C2410; }
  .editor { display: grid; gap: .5rem; margin-top: .6rem; }
  .editor label { font-size: .75rem; color: var(--dim); display: grid; gap: .2rem; }
  .editor input, .editor textarea, .editor select { width: 100%; padding: .45rem; border-radius: 8px; border: 1px solid #DFCFA8; background: var(--bg); color: #4A3D2E; font: inherit; font-size: .9rem; }
  .editor textarea { min-height: 7rem; }
  .row2 { display: grid; grid-template-columns: 1fr 1fr; gap: .5rem; }
  #empty { text-align: center; color: var(--dim); padding: 3rem 1rem; }
  #empty.go { color: var(--accent); text-decoration: underline dotted; cursor: pointer; }
  /* ✎ 改动视图：AI 提的改动，删掉的字珊瑚色划掉、新加的字青色下划线 */
  .reason { margin-top: .45rem; font-size: .85rem; background: var(--bg); border-left: 3px solid #FFE394; padding: .35rem .6rem; border-radius: 0 6px 6px 0; }
  .reason::before { content: "AI 的理由　"; color: var(--dim); font-size: .75rem; }
  .ewarn { color: #C24A28; font-size: .78rem; margin-top: .4rem; line-height: 1.5; }
  .label { font-size: .72rem; color: var(--dim); margin: .7rem 0 .25rem; letter-spacing: .05em; }
  .pane { background: var(--bg); border: 1px solid var(--line); border-radius: 10px; padding: .55rem .7rem; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.7; font-size: .95rem; }
  .cols { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: .5rem; }
  del { background: rgba(244,143,104,.38); text-decoration: line-through; text-decoration-color: #C24A28; color: #6B2A12; border-radius: 3px; }
  ins { background: rgba(139,223,221,.6); text-decoration: underline; text-decoration-color: var(--ok); text-underline-offset: 3px; color: #1C4E4B; border-radius: 3px; }
  .fold { background: none; border: 1px dashed var(--line); border-radius: 6px; color: var(--dim); font-size: .75rem; padding: 0 .35rem; margin: 0 .15rem; font: inherit; font-size: .75rem; }
  .tagrow { display: flex; flex-wrap: wrap; gap: .35rem; align-items: center; }
  .tag { font-family: "Fira Code", ui-monospace, monospace; font-size: .78rem; border-radius: 999px; padding: .1rem .55rem; border: 1px solid var(--line); color: var(--dim); }
  .tag.add { background: rgba(139,223,221,.6); border-color: #8BDFDD; color: #1C4E4B; }
  .tag.rm { background: rgba(244,143,104,.38); border-color: #F48F68; color: #6B2A12; text-decoration: line-through; }
  #toast { position: fixed; bottom: 1rem; left: 50%; transform: translateX(-50%); background: #4A3D2E; color: #fff; padding: .5rem 1rem; border-radius: 8px; font-size: .85rem; opacity: 0; transition: opacity .3s; pointer-events: none; }
  #toast.show { opacity: 1; }
</style></head><body>
<header>
  <h1>ember 审核台</h1>
  <div id="statsRow">
    <div id="stats">加载中…</div>
    <button id="addBtn" class="chip">＋ 添加</button>
    <button id="editBtn" class="chip on" hidden>✎ 改动</button>
    <button id="memBtn" class="chip">🗂 记忆库</button>
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
let mode = "pending";  // pending = 待审核 / reviewed = 反悔区 / memories = 记忆库 / edits = AI 改动
let editCount = 0;     // 等确认的 AI 改动条数：有才在顶栏亮出「✎ 改动 N」

function setEmpty(show, text = "🎉 没有待审核的草稿", onclick = null) {
  const e = $("#empty");
  e.hidden = !show;
  e.textContent = text;
  e.className = onclick ? "go" : "";
  e.onclick = onclick;
}

function renderEditBtn() {
  const b = $("#editBtn");
  b.hidden = mode !== "edits" && editCount === 0;
  b.textContent = mode === "edits" ? "← 回待审核" : "✎ 改动 " + editCount;
}

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
  if (!resp.ok) {
    toast(data.error_description || data.error || "出错了");
    // 内容在她看过之后变了（409），或改动已被撤回 / 换成新的一版（改动的 404）：拉最新的给她重看，不留旧卡片
    if (resp.status === 409 || (resp.status === 404 && path.startsWith("/review/api/edits/"))) load();
    throw new Error(data.error);
  }
  return data;
}

async function load() {
  if (mode === "memories") return loadMemories();
  if (mode === "reviewed") return loadReviewed();
  if (mode === "edits") return loadEdits();
  const q = currentBatch ? "&batch=" + encodeURIComponent(currentBatch) : "";
  const [data, edits] = await Promise.all([
    api("/review/api/drafts?status=pending" + q),
    api("/review/api/edits"),
  ]);
  editCount = edits.stats.total;
  renderEditBtn();
  renderStats(data.stats);
  renderBatches(data.stats.by_batch);
  const list = $("#list");
  list.replaceChildren(...data.items.map(card));
  if (data.items.length || !editCount) setEmpty(!data.items.length);
  else setEmpty(true, "草稿审完了，还有 " + editCount + " 条改动等你确认 →", () => setMode("edits"));
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
  setEmpty(!items.length);
}

function setMode(next) {
  mode = mode === next ? "pending" : next;
  $("#modeBtn").textContent = mode === "reviewed" ? "← 回待审核" : "↩ 已审核";
  $("#memBtn").textContent = mode === "memories" ? "← 回待审核" : "🗂 记忆库";
  renderEditBtn();
  load();
}
$("#modeBtn").onclick = () => setMode("reviewed");
$("#memBtn").onclick = () => setMode("memories");
$("#editBtn").onclick = () => setMode("edits");

// ---------- 手动添加：提取切粗了轩顺手补一条，走同一条草稿→入库管线（反悔区照样能撤回） ----------

const MANUAL_BATCH = "手动添加";

$("#addBtn").onclick = () => {
  const open = $("#addCard");
  if (open) { open.remove(); load(); return; }  // 再点一下收起
  const blank = {
    date: new Date().toLocaleDateString("sv"),  // 今天，YYYY-MM-DD
    content: "", tags: "", tier: "normal", topic: "", space: "personal",
    start_date: "", end_date: "",
  };
  const el = document.createElement("div");
  el.className = "card";
  el.id = "addCard";
  const { form, values } = editorForm(blank);
  let draftId = null;  // 入库分两步（存草稿→通过），记住第一步结果，失败重试不写重
  const post = async () => {
    if (draftId === null) {
      const saved = await api("/review/api/drafts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...values(), batch: MANUAL_BATCH }),
      });
      draftId = saved.ids[0];
    }
    return draftId;
  };
  const actions = document.createElement("div");
  actions.className = "actions";
  actions.append(
    btn("✓ 直接入库", "approve", async () => {
      const id = await post();
      await api("/review/api/drafts/" + id + "/approve", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values()),  // 带上表单现值：第一步成功第二步失败后她再改再点，以最新内容为准
      });
      el.remove();
      toast("已入库 ✓（反悔区可撤回）");
      load();
    }),
    btn("存草稿", "edit", async () => {
      await post();
      el.remove();
      toast("已存草稿，在「手动添加」批次里");
      load();
    }),
    btn("取消", "reject", () => { el.remove(); load(); }),
  );
  form.append(actions);
  const head = document.createElement("div");
  head.className = "addtitle";
  head.append(span("＋ 手动添加"));
  el.append(head, form);
  setEmpty(false);
  $("#list").prepend(el);
  form.querySelector("textarea").focus();
};

// ---------- 记忆库视图：已入库记忆的浏览与修改（打 sensitive 标签的家） ----------

let memPage = 1, memQuery = "";

async function loadMemories() {
  const params = "?page=" + memPage + (memQuery ? "&q=" + encodeURIComponent(memQuery) : "");
  const data = await api("/review/api/memories" + params);
  $("#stats").textContent = "记忆库 " + data.stats.total + " 条 · 第 " + data.page + "/" + data.total_pages + " 页";
  const box = $("#batches");
  box.replaceChildren();
  const search = document.createElement("input");
  search.className = "memsearch";
  search.placeholder = "🔍 搜内容 / 标签 / 主题";
  search.value = memQuery;
  search.onchange = () => { memQuery = search.value.trim(); memPage = 1; loadMemories(); };
  box.append(search);
  if (data.page > 1) {
    const p = chipEl("‹ 上一页", false);
    p.onclick = () => { memPage--; loadMemories(); };
    box.append(p);
  }
  if (data.page < data.total_pages) {
    const n = chipEl("下一页 ›", false);
    n.onclick = () => { memPage++; loadMemories(); };
    box.append(n);
  }
  $("#list").replaceChildren(...data.items.map(memCard));
  setEmpty(!data.items.length);
}

function memCard(m) {
  const el = document.createElement("div");
  el.className = "card";
  const box = document.createElement("div");
  box.className = "membox";
  const head = document.createElement("div");
  head.className = "boxid";
  head.append(span("记忆#" + m.id), metaEl(m));
  const body = document.createElement("div");
  body.className = "boxtext";
  body.textContent = m.content;
  box.append(head, body);
  el.append(box);
  const actions = document.createElement("div");
  actions.className = "actions";
  actions.append(btn("✎ 改", "edit", () => openMemEditor(m, el)));
  el.append(actions);
  return el;
}

function openMemEditor(m, el) {
  const { form, values } = editorForm(m);
  const actions = document.createElement("div");
  actions.className = "actions";
  actions.append(
    btn("✓ 保存", "approve", async () => {
      const updated = await api("/review/api/memories/" + m.id, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values()),
      });
      el.replaceWith(memCard(updated));
      toast("已保存，指纹已跟着更新");
    }),
    btn("取消", "reject", () => el.replaceWith(memCard(m))),
  );
  form.append(actions);
  el.replaceChildren(span("记忆#" + m.id), form);
}

// ---------- ✎ 改动视图：AI 经 MCP 提的修改，轩确认才覆盖（覆盖后不留旧版本） ----------

const EDIT_LAYOUT = "B";  // A 左右两栏 / B 上下叠放 / C 合成一栏（轩看预览后定）
const FOLD_OVER = 30, FOLD_KEEP = 10;  // 没变的段超过 30 字就折起来，只留头尾各 10 字

async function loadEdits() {
  const data = await api("/review/api/edits");
  editCount = data.stats.total;
  renderEditBtn();
  $("#stats").textContent = "改动 " + editCount + " 条（AI 提的，等你确认）";
  $("#batches").replaceChildren();
  $("#list").replaceChildren(...data.items.map(editCard));
  setEmpty(!data.items.length, "没有等你确认的改动");
}

function unchanged(text, first, last) {
  // 没变的长段落折成可点开的小框；开头 / 结尾的段只留贴着改动那一侧
  // 按字（码点）切，跟服务端对比一致——按 JS 默认的 UTF-16 切会把 emoji 劈成两半显示成 �
  const frag = document.createDocumentFragment();
  const chars = Array.from(text);
  const head = first ? [] : chars.slice(0, FOLD_KEEP), tail = last ? [] : chars.slice(-FOLD_KEEP);
  const hidden = chars.slice(head.length, chars.length - tail.length).join("");
  const hiddenLen = chars.length - head.length - tail.length;
  if (chars.length <= FOLD_OVER || hiddenLen < 8) { frag.append(text); return frag; }
  const b = btn("…" + hiddenLen + " 字没变…", "fold", () => b.replaceWith(hidden));
  frag.append(head.join(""), b, tail.join(""));
  return frag;
}

function visible(t) {
  // 只改了换行 / 空格时，划线和下划线画在空白上看不见——换成看得见的记号
  return /^\\s+$/.test(t) ? t.replace(/ /g, "·").replace(/\\n/g, "↵\\n") : t;
}

function diffPane(segs, side) {  // side: a = 改前 / b = 改后 / both = 合成一栏
  const box = document.createElement("div");
  box.className = "pane";
  segs.forEach((s, i) => {
    if (s.op === "equal") { box.append(unchanged(s.a, i === 0, i === segs.length - 1)); return; }
    if (side !== "b" && s.a) { const x = document.createElement("del"); x.textContent = visible(s.a); box.append(x); }
    if (side !== "a" && s.b) { const x = document.createElement("ins"); x.textContent = visible(s.b); box.append(x); }
  });
  return box;
}

function labeled(text, node) {
  const w = document.createElement("div");
  const l = document.createElement("div");
  l.className = "label";
  l.textContent = text;
  w.append(l, node);
  return w;
}

function contentDiff(segs) {
  if (EDIT_LAYOUT === "C") return [labeled("正文改动", diffPane(segs, "both"))];
  const before = labeled("改前", diffPane(segs, "a")), after = labeled("改后", diffPane(segs, "b"));
  if (EDIT_LAYOUT === "B") return [before, after];
  const cols = document.createElement("div");
  cols.className = "cols";
  cols.append(before, after);
  return [cols];
}

function editCard(e) {
  const el = document.createElement("div");
  el.className = "card";
  const head = document.createElement("div");
  head.className = "edithead";  // 跟记忆库 / 草稿卡同一副门牌
  const meta = document.createElement("div");
  meta.className = "meta";
  for (const p of [e.date, e.space]) meta.append(span(p));
  const tier = span(e.tier);
  tier.className = "badge" + (e.tier === "anchor" ? " anchor" : "");
  meta.append(tier);
  head.append(span("记忆#" + e.memory_id), meta);
  el.append(head);
  if (e.reason) { const r = document.createElement("div"); r.className = "reason"; r.textContent = e.reason; el.append(r); }
  if (e.stale) {
    const w = document.createElement("div");
    w.className = "ewarn";
    w.textContent = "⚠ AI 提了这条改动之后，这条记忆又被改过（可能是你在记忆库改的）。确认会用「改后」盖掉现在的内容，下面标出的就是会变的地方。";
    el.append(w);
  }
  const c = e.changes;
  if (c.content) el.append(...contentDiff(c.content.segments));
  if (c.topic) {
    const row = document.createElement("div");
    row.className = "pane";
    const a = document.createElement("del"), b = document.createElement("ins");
    a.textContent = c.topic.before || "（空）";
    b.textContent = c.topic.after;
    row.append(a, "  →  ", b);
    el.append(labeled("主题", row));
  }
  if (c.tags) {
    const row = document.createElement("div");
    row.className = "tagrow";
    const chip = (t, cls, prefix) => { const x = span((prefix || "") + t); x.className = "tag" + cls; row.append(x); };
    c.tags.kept.forEach(t => chip(t, ""));
    c.tags.removed.forEach(t => chip(t, " rm"));
    c.tags.added.forEach(t => chip(t, " add", "+"));
    el.append(labeled("标签", row));
    if (c.tags.removed.includes("sensitive")) {
      const w = document.createElement("div");
      w.className = "ewarn";
      w.textContent = "⚠ 去掉了 sensitive：确认后这条会出现在开场小抄里";
      el.append(w);
    }
  }
  const actions = document.createElement("div");
  actions.className = "actions";
  actions.append(
    btn("✓ 确认覆盖", "approve", async () => {
      if (!confirm("确定覆盖？覆盖后旧内容不保留。")) return;
      await api("/review/api/edits/" + e.id + "/confirm", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ version: e.version, seen: e.seen }),  // 她看到的那一版，变了服务端回 409
      });
      toast("已覆盖 ✓");
      load();
    }),
    btn("✕ 驳回", "reject", async () => {
      await api("/review/api/edits/" + e.id + "/reject", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ version: e.version }),
      });
      toast("已驳回，记忆保持原样");
      load();
    }),
  );
  el.append(actions);
  return el;
}

function reviewedCard(d) {
  const el = document.createElement("div");
  el.className = "card";
  const st = span(d.status === "approved" ? "✓ 已入库 → 记忆 #" + d.memory_id : "✕ 已删");
  st.className = "badge " + d.status;
  el.append(bodyEl(d, null, st));
  const box = document.createElement("div");
  box.className = "actions";
  box.append(btn("↩ 撤回到待审核", "edit", async () => {
    const r = await api("/review/api/drafts/" + d.id + "/unreview", { method: "POST" });
    el.remove();
    toast(d.status !== "approved" ? "已捞回待审核"
      : r.dropped_edit ? "已撤回，记忆已删（挂着的 AI 改动也一起丢了）" : "已撤回，记忆已删");
    load();
  }));
  el.append(box);
  return el;
}

function renderStats(stats) {
  const n = Object.values(stats.by_batch).reduce((a, b) => a + b, 0);
  $("#stats").textContent = "待审核 " + n + " 条" + (currentBatch ? "（当前批次 " + stats.total + " 条）" : "")
    + (editCount ? " · 改动 " + editCount + " 条" : "");
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
  el.append(bodyEl(d, el), actionsEl(d, el));
  return el;
}

// 句子框排版（轩的定稿）：连线单独框起来，框里主语-动词-宾语从上往下读，
// 上面的是主语——"草稿#1 导致 草稿#2"、"草稿#3 覆盖 记忆#6"，纯文字无符号。
// 序号是大门牌，跟本条的框对上号。导入菜单只留 导致/覆盖/不关联（其余关系年轮阶段再回来）。
const REL_WORDS = {
  led_to: "导致", supersedes: "覆盖", none: "不关联",
  related: "相关", contradicts: "矛盾", same_as: "同一件事",  // 旧数据展示兜底
};
const REL_MENU = [["led_to", "导致"], ["supersedes", "覆盖"], ["none", "不关联（单独入库）"]];
const isDirectional = (l) => l.relation === "led_to" || l.relation === "supersedes";

function bodyEl(d, el, badge, mainNode) {
  // 整张卡就是一句话：本条完整内容坐在句子里自己的位置上，不重复出现（轩的定稿）。
  // 主语组（它导致/覆盖本条）在本条上方，其余（本条是主语 / 不关联）在下方。
  // mainNode：编辑时用编辑框顶替本条的位置，连线和原话照常摆着，对着改。
  const links = d.links || [];
  const main = mainNode || mainBox(d, badge);
  const rest = d.quote || d.source_ref ? [quoteEl(d)] : [];
  if (!links.length) {
    const wrap = document.createElement("div");
    wrap.append(main, ...rest);
    return wrap;
  }
  const frame = document.createElement("div");
  frame.className = "linkframe";
  const subjSide = (l) => isDirectional(l) && l.dir === "in";
  links.forEach((l, i) => { if (subjSide(l)) frame.append(linkGroup(d, el, l, i, true)); });
  frame.append(main, ...rest);
  links.forEach((l, i) => { if (!subjSide(l)) frame.append(linkGroup(d, el, l, i, false)); });
  return frame;
}

function mainBox(d, badge) {
  const box = document.createElement("div");
  box.className = "membox";
  const head = document.createElement("div");
  head.className = "boxid";
  head.append(span("草稿#" + d.id));
  const meta = metaEl(d);  // meta 直接跟在门牌后面，不再单独占一行
  if (badge) meta.prepend(badge);
  head.append(meta);
  const body = document.createElement("div");
  body.className = "boxtext";
  body.textContent = d.content;
  box.append(head, body);
  return box;
}

function targetBox(link) {
  const t = link.target || {};
  const box = document.createElement("div");
  box.className = "membox target";
  const head = document.createElement("div");
  head.className = "boxid";
  head.append(span((t.kind === "draft" ? "草稿#" : "记忆#") + t.id));
  if (t.date) { const dt = span(t.date); dt.className = "odate"; head.append(dt); }
  const body = document.createElement("div");
  body.className = "boxtext";
  body.textContent = t.missing ? "（已不存在）" : t.preview;
  box.append(head, body);
  if (t.kind === "draft" && t.status === "rejected") {
    const w = span("⚠ 对方已被拒，入库时这条线自动放弃");
    w.className = "warn";
    box.append(w);
  }
  return box;
}

function dateWarn(d, link) {
  // 主语-动词-宾语定死后，"打架"= 日期不支持这句话：导致的主语该更早，覆盖的主语该更新
  const t = link.target || {};
  if (!t.date || !d.date) return false;
  const subjDate = link.dir === "in" ? t.date : d.date;
  const objDate = link.dir === "in" ? d.date : t.date;
  if (link.relation === "led_to") return subjDate > objDate;
  if (link.relation === "supersedes") return subjDate < objDate;
  return false;
}

function linkGroup(d, el, link, idx, subjectSide) {
  const g = document.createElement("div");
  g.className = "linkgroup" + (link.relation === "none" ? " off" : "");
  if (subjectSide) g.append(targetBox(link), verbRow(d, el, link, idx));
  else g.append(verbRow(d, el, link, idx), targetBox(link));
  return g;
}

function verbRow(d, el, link, idx) {
  const row = document.createElement("div");
  row.className = "verb";
  row.append(btn(REL_WORDS[link.relation] + (el ? " ▾" : ""), "word", () => el && toggleMenu(d, el, link, idx, row)));
  if (el && isDirectional(link)) {  // 交换在外面直接点，不藏菜单里
    row.append(btn("⇅ 交换", "swap", () => patchLink(d, el, idx, { dir: link.dir === "out" ? "in" : "out" }, "换好位置了")));
  }
  if (dateWarn(d, link)) {
    const w = span("⚠");
    w.className = "warn";
    w.title = "日期跟这句话对不上（导致的主语该更早，覆盖的主语该更新），检查关系或日期";
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
  menu.append(btn("收起", "", () => menu.remove()));
  anchor.append(menu);  // 浮层：挂在动词行上，absolute 浮出不挤内容
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
  // 序号不在这——它是大门牌，挂在内容框上
  const meta = document.createElement("div");
  meta.className = "meta";
  const parts = [d.date, d.space, d.topic, d.batch].filter(Boolean);
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
    // 带上卡片上看到的内容：她打开页面后 AI 又改过这条，服务端对不上回 409，不让通过没看过的内容
    btn("✓ 通过", "approve", () => act(d.id, "approve", el, { expect: { content: d.content, tags: d.tags, topic: d.topic } })),
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

function editorForm(d) {
  // 草稿编辑器和记忆库编辑器共用的表单体：同一批字段、同一副长相
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
  const values = () => Object.fromEntries(Object.entries(fields).map(([k, i]) => [k, i.value]));
  return { form, values };
}

function openEditor(d, el) {
  const { form, values } = editorForm(d);
  const actions = document.createElement("div");
  actions.className = "actions";
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
  // 编辑框坐进本条的框里：门牌、连线句子框、原话都还在（连线这时只读，改完再调）；
  // 按钮留在框外，跟平时卡片同一个位置
  const box = document.createElement("div");
  box.className = "membox";
  const head = document.createElement("div");
  head.className = "boxid";
  head.append(span("草稿#" + d.id), metaEl(d));
  box.append(head, form);
  el.replaceChildren(bodyEl(d, null, null, box), actions);
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
    expect = edits.pop("expect", None) if isinstance(edits, dict) else None
    try:
        result = drafts.approve_draft(
            draft_id, edits=edits, expect=expect if isinstance(expect, dict) else None
        )
    except drafts.DraftConflict:
        return _conflict("这条草稿在你打开页面后被 AI 改过，已刷新，请再看一眼")
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


@router.get("/review/api/memories")
def api_browse_memories(request: Request, page: int = 1, q: str | None = None, space: str | None = None):
    """记忆库视图：已入库记忆分页浏览（默认跨全库），供打标/修正。"""
    if not _authed(request):
        return _unauthorized()
    return memories.browse_memories(q=q, space=space, page=page)


@router.patch("/review/api/memories/{memory_id}")
async def api_update_memory(memory_id: int, request: Request):
    """改一条已入库记忆（tags 打 sensitive、修正内容等）。指纹跟着内容自动重算。"""
    if not _authed(request):
        return _unauthorized()
    try:
        updated = memories.update_memory(memory_id, await request.json())
    except ValueError as e:
        return JSONResponse({"error": "invalid_memory", "error_description": str(e)}, status_code=400)
    if updated is None:
        return JSONResponse({"error": "not_found", "error_description": "记忆不存在"}, status_code=404)
    return updated


def _conflict(message: str) -> JSONResponse:
    return JSONResponse({"error": "conflict", "error_description": message}, status_code=409)


EDIT_GONE = "这条改动已经处理过，或 AI 撤回 / 换了一版，已刷新"


@router.get("/review/api/edits")
def api_list_edits(request: Request):
    """AI 提的改动（等轩确认）：每条带改前 / 改后对照、version 和 seen 指纹。"""
    if not _authed(request):
        return _unauthorized()
    return memory_edits.list_edits()


async def _edit_body(request: Request) -> dict:
    body = await request.json() if int(request.headers.get("content-length") or 0) else {}
    return body if isinstance(body, dict) else {}


def _int_or_none(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@router.post("/review/api/edits/{edit_id}/confirm")
async def api_confirm_edit(edit_id: int, request: Request):
    """确认覆盖：只认轩的浏览器登录；带她看到的 version + seen，对不上 409。"""
    if not _authed_human(request):
        return _unauthorized()
    body = await _edit_body(request)
    version, seen = _int_or_none(body.get("version")), body.get("seen")
    if version is None or not isinstance(seen, str):
        return JSONResponse(
            {"error": "invalid_request", "error_description": "要带上 version 和 seen"}, status_code=400
        )
    try:
        updated = memory_edits.confirm_edit(edit_id, version, seen)
    except memory_edits.EditConflict:
        return _conflict("这条改动在你打开页面后又变了（AI 又改过，或记忆被改过），已刷新，请再看一眼")
    except ValueError as e:
        return JSONResponse({"error": "invalid_memory", "error_description": str(e)}, status_code=400)
    if updated is None:
        return JSONResponse({"error": "not_found", "error_description": EDIT_GONE}, status_code=404)
    return updated


@router.post("/review/api/edits/{edit_id}/reject")
async def api_reject_edit(edit_id: int, request: Request):
    """驳回：记忆保持原样。只认轩的浏览器登录；version 对不上 409。"""
    if not _authed_human(request):
        return _unauthorized()
    version = _int_or_none((await _edit_body(request)).get("version"))
    if version is None:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "要带上 version"}, status_code=400
        )
    try:
        result = memory_edits.reject_edit(edit_id, version)
    except memory_edits.EditConflict:
        return _conflict("这条改动在你打开页面后又变了，已刷新，请再看一眼")
    if result is None:
        return JSONResponse({"error": "not_found", "error_description": EDIT_GONE}, status_code=404)
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
