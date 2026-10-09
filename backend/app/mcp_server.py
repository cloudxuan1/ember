"""ember 的 MCP 工具层：五个记忆操作工具 + memory_edit（改动等轩确认）+ briefing（V6b）
+ chat-lite 思考彩蛋词库两个工具，少而清楚。纪律写进工具本身。"""

import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app import briefing, drafts, memories, memory_edits, thinking_words

# FastMCP 默认只放行本机 Host 头（DNS-rebinding 防护），
# 经 Cloudflare Tunnel 进来的请求带公网域名，必须显式加进白名单，否则 421。
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "ember.cloudxuan1.com")

# 有状态 SSE 模式（不设 stateless_http/json_response）：
# claude.ai 网页 connector 实测只认这种——无状态 JSON 模式下 initialize 200
# 它也报"连不上"；旧系统 memory 用的正是有状态 SSE，能连。
mcp = FastMCP(
    "ember",
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[PUBLIC_HOST, "127.0.0.1:*", "localhost:*", "ember:*", "[::1]:*"],
        # 客户端服务器可能带自家 Origin 头（浏览器才不带），拦了就是 403
        allowed_origins=[
            f"https://{PUBLIC_HOST}",
            "https://claude.ai",
            "https://claude.com",
            "https://chatgpt.com",
        ],
    ),
)


@mcp.tool()
def memory_search(query: str, space: str | None = None, limit: int = 8) -> dict:
    """搜索记忆（语义 + 关键词 hybrid），返回目录（短结果 + ID），不含全文。

    用户提到过去的事、人物、约定、偏好时先调用这个。
    V5 起带语义检索：查询词和记忆字面不同也能召回（搜"难过"能找到
    "眼泪在眼眶里打转"），所以用自然的词直接搜就好，不用猜原文用词。
    需要某条的完整内容和来源时，再用 memory_recall(id) 取详情。
    space 语义（V6 空间隔离）：**不传 = 只搜 personal 核心层**（关系与个人）。
    聊项目/技术话题务必显式传对应空间（ember / vps / ...）；
    确实要跨全库找传 "all"。
    """
    results = memories.search_memories(query, space=space, limit=min(limit, 20))
    return {"count": len(results), "results": results}


@mcp.tool()
def memory_recall(id: int) -> dict:
    """按 ID 取一条记忆的完整内容、来源证据（source_ref + 原话片段）和关系边。

    边自带对方记忆的一行摘要（不用挨个再查）；区间型记忆带 interval_status
    （upcoming / ongoing / ended，按今天现算）。
    """
    memory = memories.get_memory(id)
    if memory is None:
        return {"error": f"记忆 {id} 不存在"}
    return memory


@mcp.tool()
def memory_save(
    date: str,
    content: str,
    tags: str = "",
    tier: str = "normal",
    topic: str = "",
    space: str = "personal",
    start_date: str | None = None,
    end_date: str | None = None,
    links: list[dict] | None = None,
) -> dict:
    """保存一条待审记忆草稿，不会直接写入正式记忆库。

    date 填事件发生的真实日期（YYYY-MM-DD），不是今天（除非事情就发生在今天）。
    content 写原话 + 一句上下文。tags 逗号分隔。
    tier：anchor（长期锚点，慎用）/ normal（默认）/ process（过程性，可清理）。
    topic 填主题或实体名，方便日后去重合并。
    区间型的事（一段状态/计划，非点事件）填 start_date / end_date（可只填一头：
    只有 start = 进行中的开放区间，只有 end = 截止型），状态读时按今天现算。
    links 连关系边：存之前你刚 memory_search 过，看到相关旧记忆就顺手连上——
    [{"id": 目标记忆id, "relation": "led_to/same_as/contradicts/supersedes/related",
      "dir": "out"(本条→目标，默认) 或 "in"(目标→本条)}]。
    led_to 方向 = 因 → 果；supersedes 会把被压过的旧记忆标记 superseded_by。
    调用后返回 draft_id；草稿状态为 pending，等轩在审核台通过后才会正式入库。
    """
    draft_links = None
    if links is not None:
        # MCP 对外一直用 id；草稿管线用 memory_id 表示已入库目标。
        # 这里只改字段名，默认值和合法性仍全部交给 drafts._normalize_links。
        draft_links = [
            {("memory_id" if key == "id" else key): value for key, value in link.items()}
            for link in links
        ]
    saved = drafts.save_draft(
        date=date, content=content, tags=tags, tier=tier, topic=topic, space=space,
        start_date=start_date, end_date=end_date, links=draft_links,
        batch=f"mcp-{memories._today()}",
    )
    return {
        "draft_id": saved["id"],
        "status": "pending",
        "message": "已存为待审草稿，等轩审核后入库。",
    }


@mcp.tool()
def memory_edit(
    memory_id: int | None = None,
    draft_id: int | None = None,
    content: str | None = None,
    tags: str | None = None,
    topic: str | None = None,
    reason: str | None = None,
) -> dict:
    """改一条正式记忆或待审草稿的 content / tags / topic。memory_id 和 draft_id 只给一个：
    memory_search / memory_recall / memory_list 给的是 memory_id；memory_save 返回的是 draft_id。

    先读后改：只给编号、不给内容 = 读出当前的 content / tags / topic
    （正式记忆还带 pending_edit：已经在排队等轩确认的改动，在它的基础上改，别把它冲掉）。
    - content 是整段替换：在末尾补一句，就传「原文 + 新增」的完整全文；不要顺手润色、压缩原话。
    - tags 也是整组替换（加标签要带上原有的）。tags / topic 不传或传空 = 不改，不能清空。
    - 改正式记忆（memory_id）：不会直接改正式记忆，而是生成一条修改提议，等轩在审核台确认后才覆盖；
      确认前 memory_recall / memory_search 查到的仍是旧内容。已有待确认的改动时合并进去，
      replaced = 被这次冲掉的上一版待确认值。
    - 改待审草稿（draft_id）：直接改草稿，草稿仍待审，轩在审核台通过后才入库。
    - 只用来改错字、补原话、补标签 / 主题。事情本身变了（计划成了结果、想法变了），
      用 memory_save 新存一条，links 连 supersedes 指向旧记忆，旧的留作历史。
    reason 写一句为什么改，审核台上给轩看。返回里的 target 是改的那一条的日期和开头，核对一下没改错。
    """
    if (memory_id is None) == (draft_id is None):
        return {"error": "memory_id 和 draft_id 要给且只给一个（memory_save 返回的是 draft_id）"}
    try:
        fields = memory_edits.normalize_fields(content, tags, topic)
    except ValueError as err:
        return {"error": str(err)}
    if draft_id is not None:
        return _edit_draft(draft_id, fields)
    if not fields:  # 只给编号 = 读
        memory = memories.get_memory(memory_id)
        if memory is None:
            return {"error": f"记忆 {memory_id} 不存在（草稿号请用 draft_id）"}
        return {
            "memory_id": memory_id,
            "date": memory["date"],
            "content": memory["content"],
            "tags": memory["tags"],
            "topic": memory["topic"],
            "pending_edit": memory_edits.pending_for(memory_id),
        }
    result = memory_edits.propose_edit(memory_id, reason=reason, by="mcp", **fields)
    if result is None:
        return {"error": f"记忆 {memory_id} 不存在（草稿号请用 draft_id）"}
    if result["status"] == "unchanged":
        withdrawn = "；之前排队的那版改动已撤回" if result["replaced"] else ""
        return {
            "memory_id": memory_id,
            "target": result["target"],
            "status": "unchanged",
            "message": f"跟现在的内容一样，没有要改的{withdrawn}。",
        }
    out = {
        "memory_id": memory_id,
        "edit_id": result["edit_id"],
        "target": result["target"],
        "status": "pending_review",
        "pending": result["pending"],
        "message": "已提交修改，等轩在审核台确认后才覆盖；确认前查到的仍是旧内容。"
        + ("已合并进这条记忆原有的待确认改动。" if result["merged"] else ""),
    }
    if result["replaced"]:
        out["replaced"] = result["replaced"]
    return out


def _edit_draft(draft_id: int, fields: dict) -> dict:
    """草稿本来就要等轩审，改了直接生效；只给 pending 的看和改（被拒的是审计记录，不外露）。"""
    draft = drafts.get_draft(draft_id)
    if draft is None or draft["status"] == "rejected":
        return {"error": f"草稿 {draft_id} 不存在或已被轩删掉"}
    if draft["status"] == "approved":
        return {
            "error": f"草稿 {draft_id} 已经入库成记忆 {draft['memory_id']}，请用 memory_id 改",
            "memory_id": draft["memory_id"],
        }
    if not fields:  # 只给编号 = 读
        return {
            "draft_id": draft_id,
            "status": "pending",
            "date": draft["date"],
            "content": draft["content"],
            "tags": draft["tags"],
            "topic": draft["topic"],
        }
    updated = drafts.update_draft(draft_id, fields)
    if updated is None or updated["status"] != "pending":  # 读完到改之间被轩审掉了
        return {"error": f"草稿 {draft_id} 刚被轩审核了，这次没改上；已入库的话请用 memory_id 改"}
    return {
        "draft_id": draft_id,
        "target": memory_edits.target_preview(updated),
        "status": "pending",
        "changed": [f for f in memory_edits.FIELDS if f in fields],
        "message": "草稿已改，仍待轩在审核台通过后入库。",
    }


@mcp.tool()
def memory_list(
    space: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
) -> dict:
    """分页浏览记忆目录，先返回统计概览（总数、按空间、按层级），再给当页短条目。

    永远分页（每页 20 条），不会一次吐出全库。日期格式 YYYY-MM-DD。
    space 语义与 memory_search 一致：不传 = 只看 personal 核心层；
    项目空间显式传（ember / vps / ...）；跨全库传 "all"。
    """
    return memories.list_memories(
        space=space, date_from=date_from, date_to=date_to, page=page
    )


@mcp.tool()
def memory_briefing(topic: str | None = None) -> dict:
    """开场小抄（V6b 主动浮现）：会话开始时调一次，接住"进行中的事"。

    返回至多 8 条分层标注的记忆，每条带 reason（进行中的事 / 和眼下话题
    相关 / 最近的事）+ 短内容 + id，细节用 memory_recall(id) 取。
    topic 传用户开场聊的内容（原话或主题词都行），不传则只出
    "进行中 + 近期"两层。

    分寸：这是背景记忆，不是任务清单。自然地主动提起其中一两件
    最相关或最有温度的事，其余记在心里、话题碰到再用；
    不念清单、不逐条汇报。同一条记忆 3 天内不会重复递给你，
    没提的不算错过。
    """
    return briefing.build_briefing(topic=topic)


@mcp.tool()
def memory_status() -> dict:
    """ember 服务状态：活着吗、库里多少条记忆/边/来源、最近一次写入时间。连接调试用。"""
    return memories.get_status()


@mcp.tool()
def thinking_words_view() -> dict:
    """查看 chat-lite 的思考彩蛋词库（和记忆无关）：思考时标题轮流显示的英文词 + 中文翻译。

    返回版本号、三个开关（egg 彩蛋总开关 / translate 翻出中文 / marauder 活点地图开场收尾）
    和全部系列：id、名字、是否内置、是否打开、词条 [{en, zh}]。
    改词前先看一眼，拿到准确的 series_id 和英文原词。
    """
    library = thinking_words.get_library()
    if library["data"] is None:
        return {"error": "云端还没有词库：请轩先在 chat-lite 网页打开一次（开着云同步），把现有词库搬上来"}
    return library


@mcp.tool()
def thinking_words_edit(
    action: str,
    series_id: str | None = None,
    name: str | None = None,
    words: list[dict] | None = None,
    en: str | None = None,
    new_en: str | None = None,
    zh: str | None = None,
    enabled: bool | None = None,
    setting: str | None = None,
    value: bool | None = None,
) -> dict:
    """修改 chat-lite 的思考彩蛋词库，改完网页刷新就能看到。一次做一件事：

    - add_series：新建系列，给 name，可带 words=[{"en": "Purring…", "zh": "呼噜呼噜中"}]
    - rename_series：series_id + name
    - delete_series：series_id（只能删自定义系列；内置的用 set_series_enabled 关掉）
    - set_series_enabled：series_id + enabled
    - add_words：series_id + words（英文相同的会跳过）
    - update_word：series_id + en（原来的英文），改英文给 new_en，改中文给 zh
    - delete_word：series_id + en
    - set_setting：setting（egg / translate / marauder）+ value

    英文习惯写成 -ing 动名词加省略号（Marinating…），中文写成「……中」；中文可以不写。
    """
    try:
        return thinking_words.edit_library(
            action, series_id=series_id, name=name, words=words, en=en, new_en=new_en,
            zh=zh, enabled=enabled, setting=setting, value=value, by="mcp",
        )
    except thinking_words.VersionConflict:
        return {"error": "刚好有人同时改了词库，请重新查看后再改一次"}
    except ValueError as err:
        return {"error": str(err)}
