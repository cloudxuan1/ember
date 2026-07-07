"""对话实时回流：聊天原文 → 记忆草稿。开采从考古变日常代谢（终局方向的核心闭环）。

纪律：
- 提取结果进 memory_drafts（batch=chat回流/日期），**审核台闸门不变**——轩仍是唯一守门人
- 锚点逐组推进（aion 的 anchor_ts 模式）：conversations.reflow_anchor_id 记录该会话
  已提取到哪条消息，提取成功才推进，失败下次重试不漏；一个 chunk 提不出东西也推进
  （空转不是错，别永远重试同一段）
- 后台任务里跑，任何异常只打日志，绝不影响聊天主流程
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone

from app import drafts, gateway, memories
from app.db import get_conn

REFLOW_MIN_MESSAGES = int(os.environ.get("REFLOW_MIN_MESSAGES", "12"))  # 攒够 ~6 轮才提取
REFLOW_TIMEOUT = 120.0
_TZ = timezone(timedelta(hours=8))


def reflow_model() -> str:
    return os.environ.get("REFLOW_MODEL") or gateway.chat_model()


EXTRACT_PROMPT = """你是记忆提取器。从下面这段对话里提取值得**长期**保留的记忆，输出 JSON 数组（没有值得留的就输出 []，宁缺勿滥）。

每条记忆的字段：
- date: "YYYY-MM-DD"（事件真实日期；对话里聊"今天"的事就用对话当天）
- content: 原话 + 一句上下文，写清楚谁说的什么
- tags: 逗号分隔；**疑似隐私/敏感的加 sensitive**（宁多勿漏，审核时会把关）
- tier: "normal"（默认）/ "anchor"（重大长期锚点，慎用）/ "process"（过程性，可清理）
- topic: 主题或实体名（一短语）
- space: "personal"（默认）；纯聊 ember/技术项目的用对应空间名
- start_date / end_date: 只有"一段时间的事"（进行中的计划/状态/期限）才填，可只填一头；一天的事别填
- quote: 对话里的关键原话片段（一两句）
- links: 可选。跟下方"已知记忆"有关系才填：
  [{"memory_id": 已知记忆的id, "relation": "supersedes"}] = 本条是新进展，**覆盖**那条旧事实（如"生了"覆盖"要生了"）
  [{"memory_id": 已知记忆的id, "relation": "led_to"}] = 那条旧事**导致**了本条。只用这两种关系。

已知记忆（**已经在库里，别重复提取**；对话里有它们的新进展/矛盾时，提取新条并用 links 建议覆盖）：
{known}

只输出 JSON 数组，不要解释。对话如下：

"""


def _known_memories(rows) -> tuple[str, str]:
    """拿最近几条用户消息当查询，搜相关已有记忆——给提取模型当"已知信息"（kiwi-mem 的防重招）。"""
    user_text = " ".join(r["content"] for r in rows if r["role"] == "user")[-300:]
    if not user_text.strip():
        return "（无）", ""
    try:
        related = memories.search_memories(user_text, space="all", limit=8)
    except Exception:
        return "（无）", ""
    if not related:
        return "（无）", ""
    lines = [
        f"#{m['id']} [{m['date']}] {m['content'][:80]}" + ("（已被覆盖）" if m.get("superseded_by") else "")
        for m in related
    ]
    return "\n".join(lines), user_text


def _parse_drafts(raw: str) -> list[dict]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("提取结果要是 JSON 数组")
    allowed = ("date", "content", "tags", "tier", "topic", "space", "start_date", "end_date", "quote")
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        row = {k: item[k] for k in allowed if k in item}
        if row.get("tier") not in ("anchor", "normal", "process"):
            row["tier"] = "normal"  # 模型瞎填的 tier 兜底成 normal，别让整批卡死重试
        links = [
            {"memory_id": l["memory_id"], "relation": l["relation"]}
            for l in (item.get("links") or [])
            if isinstance(l, dict)
            and isinstance(l.get("memory_id"), int) and l["memory_id"] > 0
            and l.get("relation") in ("supersedes", "led_to")  # 导入纪律：只这两种边
        ]
        if links:
            row["links"] = links
        out.append(row)
    return out


def run_reflow(conversation_id: int, min_messages: int = 0) -> dict:
    """提取一个会话的未回流消息。返回结果摘要；供后台钩子和手动触发共用。"""
    with get_conn() as conn:
        conv = conn.execute(
            "SELECT reflow_anchor_id FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if conv is None:
            raise ValueError(f"会话 {conversation_id} 不存在")
        rows = conn.execute(
            "SELECT id, role, content, created_at FROM chat_messages WHERE conversation_id = ? AND id > ? ORDER BY id",
            (conversation_id, conv["reflow_anchor_id"]),
        ).fetchall()
    if not rows or len(rows) < min_messages:
        return {"conversation_id": conversation_id, "skipped": True, "pending": len(rows)}

    today = datetime.now(_TZ).date().isoformat()
    transcript = "\n".join(
        f"[{r['created_at']}] {'轩' if r['role'] == 'user' else 'AI'}: {r['content']}" for r in rows
    )
    known, _ = _known_memories(rows)
    prompt = EXTRACT_PROMPT.replace("{known}", known)  # 不用 .format——提示词里的 JSON 花括号会炸
    raw = gateway._completion(
        reflow_model(),
        [{"role": "user", "content": f"{prompt}（对话发生在 {today}）\n{transcript}"}],
        REFLOW_TIMEOUT, max_tokens=4000,
    )
    items = _parse_drafts(raw)
    saved = 0
    if items:
        source_ref = f"chat:{conversation_id}:{rows[0]['id']}-{rows[-1]['id']}"
        for item in items:
            item.setdefault("date", today)
            item["batch"] = f"chat回流/{today}"
            item["source_ref"] = source_ref
        saved = len(drafts.save_drafts(items))  # 整批原子：坏一条整批不写，锚点不动可重试
    with get_conn() as conn:  # 提取成功（含空数组）才推进锚点
        conn.execute(
            "UPDATE conversations SET reflow_anchor_id = ? WHERE id = ?",
            (rows[-1]["id"], conversation_id),
        )
    return {"conversation_id": conversation_id, "skipped": False, "messages": len(rows), "drafts": saved}


def maybe_reflow(conversation_id: int) -> None:
    """每轮聊完的后台钩子：攒够了就回流。任何异常只打日志，聊天主流程无感。"""
    try:
        result = run_reflow(conversation_id, min_messages=REFLOW_MIN_MESSAGES)
        if not result.get("skipped"):
            print(f"[reflow] 会话{conversation_id}: {result['messages']}条消息 → {result['drafts']}条草稿", flush=True)
    except Exception as e:
        print(f"[reflow] 会话{conversation_id} 回流失败（锚点未动，下次重试）: {e}", flush=True)
