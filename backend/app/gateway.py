"""聊天网关（终局试验田第一块砖）：每轮开口前，后端默默把记忆准备好。

设计整体借鉴 aion-chat 的实战经验（轩深度研究过的运行系统）：
- 哨兵 instant_digest：一次便宜模型调用判断 要不要搜/keywords/topic，
  正则只做单向纠正（命中"记得/上次"必搜，不反向关搜索），异常回安全默认
- 两路记忆：briefing（主动浮现，无条件）+ search（被动召回，哨兵说搜才搜）
- 伪 user/assistant 对注入人设和记忆，不用 system role，兼容性更好
- 稳定块（人设）排最前吃 API 缓存，动态块（记忆/历史）在后
- 处处软失败：哨兵挂了照常聊（只是不带召回），主模型挂了报人话错误

模型与密钥全在 .env（CHAT_MODEL / SENTINEL_MODEL，key 默认复用 EMBEDDING_API_KEY），
人设在 data/persona.md（数据卷里，不进仓库，轩可随时改）。
"""

import json
import os
import re
from pathlib import Path

import httpx
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

from app import briefing, memories, review
from app.db import get_conn

router = APIRouter()

HISTORY_LIMIT = 30    # 每轮带进 prompt 的历史消息条数（aion 的 context_limit）
RECALL_LIMIT = 6      # 被动召回上限
SENTINEL_TIMEOUT = 15.0
CHAT_TIMEOUT = 180.0
TITLE_LEN = 24

# 哨兵的单向纠正：提到过去的字眼必搜——只往"要搜"方向掰，不反过来（aion 的教训）
FORCE_SEARCH = re.compile(r"记得|上次|之前|以前|那次|那天|那时候|说过|约好|约定|承诺")

DEFAULT_PERSONA = (
    "你是轩的 AI 伙伴，背后连着 ember 记忆库——你们共同经历的事都存在里面。"
    "带着记忆自然地聊天：相关的旧事可以主动提起一两件，但别念清单、别逐条汇报。"
    "拿不准的事宁可说记不清，也不要编造记忆。"
)


def api_base() -> str:
    return os.environ.get(
        "CHAT_API_BASE", os.environ.get("EMBEDDING_API_BASE", "https://openrouter.ai/api/v1")
    ).rstrip("/")


def api_key() -> str:
    return os.environ.get("CHAT_API_KEY") or os.environ.get("EMBEDDING_API_KEY", "")


def chat_model() -> str:
    return os.environ.get("CHAT_MODEL", "")


def sentinel_model() -> str:
    return os.environ.get("SENTINEL_MODEL", "")


def enabled() -> bool:
    return bool(api_key() and chat_model())


def persona() -> str:
    """人设住在数据卷（data/persona.md），不进公开仓库；没有就用中性默认。"""
    path = Path(os.environ.get("EMBER_PERSONA", "data/persona.md"))
    try:
        text = path.read_text(encoding="utf-8").strip()
        return text or DEFAULT_PERSONA
    except OSError:
        return DEFAULT_PERSONA


def _completion(model: str, messages: list[dict], timeout: float, max_tokens: int = 2048) -> str:
    resp = httpx.post(
        f"{api_base()}/chat/completions",
        headers={"Authorization": f"Bearer {api_key()}"},
        json={"model": model, "messages": messages, "max_tokens": max_tokens},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


# ---------- 哨兵 ----------

SENTINEL_PROMPT = """你是记忆检索哨兵。看用户这句话，判断要不要去长期记忆库搜索。
只回 JSON，不要别的：{"search_needed": true/false, "keywords": "搜索用的关键词（空格分隔，2-4个）", "topic": "这句话的主题（一短语）"}
提到过去的人/事/约定/偏好 → search_needed=true。纯寒暄或纯新话题 → false。topic 永远要给。"""


def _parse_sentinel(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):  # 模型爱把 JSON 包代码块里（aion 踩过）
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    data = json.loads(raw)
    return {
        "search_needed": bool(data.get("search_needed")),
        "keywords": str(data.get("keywords") or ""),
        "topic": str(data.get("topic") or ""),
    }


def instant_digest(text: str) -> dict:
    """哨兵：便宜模型一次调用。没配模型或炸了 → 正则兜底 + 原文当 topic，绝不崩主流程。"""
    fallback = {
        "search_needed": bool(FORCE_SEARCH.search(text)),
        "keywords": text[:40],
        "topic": text[:40],
        "sentinel": "fallback",
    }
    if not (api_key() and sentinel_model()):
        return fallback
    try:
        raw = _completion(
            sentinel_model(),
            [{"role": "user", "content": f"{SENTINEL_PROMPT}\n\n用户这句话：{text}"}],
            SENTINEL_TIMEOUT, max_tokens=200,
        )
        digest = _parse_sentinel(raw)
    except Exception as e:
        print(f"[gateway] 哨兵失败，正则兜底: {e}", flush=True)
        return fallback
    if FORCE_SEARCH.search(text):  # 单向纠正：只往"要搜"掰
        digest["search_needed"] = True
        digest["keywords"] = digest["keywords"] or text[:40]
    digest["sentinel"] = sentinel_model()
    return digest


# ---------- 记忆注入 ----------


def gather_memories(text: str, digest: dict) -> dict:
    """两路：briefing 主动浮现（无条件）+ search 被动召回（哨兵说搜才搜），按 id 去重。"""
    surfacing = briefing.build_briefing(topic=digest["topic"] or text)["items"]
    recall = []
    if digest["search_needed"]:
        seen = {m["id"] for m in surfacing}
        query = digest["keywords"] or text
        recall = [m for m in memories.search_memories(query, limit=RECALL_LIMIT) if m["id"] not in seen]
    return {"surfacing": surfacing, "recall": recall, "digest": digest}


def _memory_lines(items: list[dict], label: str) -> str:
    lines = []
    for m in items:
        mark = f"[{m['date']}·{m.get('reason', label)}]"
        if m.get("superseded_by"):
            mark += f"[已被记忆#{m['superseded_by']}取代，别当现状说]"
        lines.append(f"{mark} {m['content']}")
    return "\n".join(lines)


def build_messages(history: list[dict], user_text: str, mem: dict) -> list[dict]:
    """伪 user/assistant 对注入（不用 system role）；稳定块在前吃缓存，动态块在后。"""
    msgs = [
        {"role": "user", "content": f"（人设与规则）\n{persona()}"},
        {"role": "assistant", "content": "明白，我会带着记忆自然地聊。"},
    ]
    blocks = []
    if mem["surfacing"]:
        blocks.append("【背景记忆·开场小抄】\n" + _memory_lines(mem["surfacing"], "背景"))
    if mem["recall"]:
        blocks.append("【相关记忆·按这句话搜到的】\n" + _memory_lines(mem["recall"], "相关"))
    if blocks:
        msgs.append({
            "role": "user",
            "content": "（记忆库递来的小抄，是背景不是任务清单：自然用，别逐条汇报）\n\n" + "\n\n".join(blocks),
        })
        msgs.append({"role": "assistant", "content": "收到，记在心里了。"})
    msgs += [{"role": m["role"], "content": m["content"]} for m in history]
    msgs.append({"role": "user", "content": user_text})
    return msgs


# ---------- 对话存取 ----------


def _history(conn, conversation_id: int, limit: int = HISTORY_LIMIT) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM chat_messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
        (conversation_id, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def send(conversation_id: int | None, text: str) -> dict:
    if not enabled():
        raise RuntimeError("CHAT_MODEL / API key 未配置（.env 里设 CHAT_MODEL，key 默认复用 EMBEDDING_API_KEY）")
    digest = instant_digest(text)
    mem = gather_memories(text, digest)
    with get_conn() as conn:
        if conversation_id is None:
            cur = conn.execute("INSERT INTO conversations (title) VALUES (?)", (text[:TITLE_LEN],))
            conversation_id = cur.lastrowid
        elif conn.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone() is None:
            raise ValueError(f"会话 {conversation_id} 不存在")
        history = _history(conn, conversation_id)
    reply = _completion(chat_model(), build_messages(history, text, mem), CHAT_TIMEOUT)
    meta = json.dumps(mem, ensure_ascii=False)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO chat_messages (conversation_id, role, content) VALUES (?, 'user', ?)",
            (conversation_id, text),
        )
        cur = conn.execute(
            "INSERT INTO chat_messages (conversation_id, role, content, memory_meta) VALUES (?, 'assistant', ?, ?)",
            (conversation_id, reply, meta),
        )
        reply_id = cur.lastrowid
        conn.execute(
            "UPDATE conversations SET updated_at = datetime('now','+8 hours') WHERE id = ?",
            (conversation_id,),
        )
    return {"conversation_id": conversation_id, "reply": reply, "reply_id": reply_id, "memory": mem}


# ---------- HTTP 端点（鉴权复用审核台那套：cookie 或 EMBER_REVIEW_TOKEN） ----------


@router.post("/chat/api/send")
async def api_send(request: Request, background: BackgroundTasks):
    if not review._authed(request):
        return review._unauthorized()
    body = await request.json()
    text = str(body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "empty", "error_description": "说点什么吧"}, status_code=400)
    conversation_id = body.get("conversation_id")
    try:
        result = send(conversation_id, text)
    except ValueError as e:
        return JSONResponse({"error": "not_found", "error_description": str(e)}, status_code=404)
    except RuntimeError as e:
        return JSONResponse({"error": "not_configured", "error_description": str(e)}, status_code=503)
    except httpx.HTTPError as e:
        return JSONResponse({"error": "llm_error", "error_description": f"主模型没接住：{e}"}, status_code=502)
    from app import reflow  # 延迟导入避免环
    background.add_task(reflow.maybe_reflow, result["conversation_id"])
    return result


@router.post("/chat/api/reflow/{conversation_id}")
def api_reflow(conversation_id: int, request: Request):
    """手动触发回流（平时不用：每轮聊完攒够 REFLOW_MIN_MESSAGES 条会自动跑）。"""
    if not review._authed(request):
        return review._unauthorized()
    from app import reflow
    try:
        return reflow.run_reflow(conversation_id)
    except ValueError as e:
        return JSONResponse({"error": "not_found", "error_description": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": "reflow_error", "error_description": str(e)}, status_code=502)


@router.get("/chat/api/conversations")
def api_conversations(request: Request):
    if not review._authed(request):
        return review._unauthorized()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT c.id, c.title, c.updated_at,
                      (SELECT COUNT(*) FROM chat_messages m WHERE m.conversation_id = c.id) AS messages
               FROM conversations c ORDER BY c.updated_at DESC LIMIT 50"""
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.get("/chat/api/conversations/{conversation_id}/messages")
def api_messages(conversation_id: int, request: Request):
    if not review._authed(request):
        return review._unauthorized()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, role, content, memory_meta, created_at FROM chat_messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
    items = []
    for r in rows:
        item = dict(r)
        item["memory_meta"] = json.loads(r["memory_meta"]) if r["memory_meta"] else None
        items.append(item)
    return {"items": items}
