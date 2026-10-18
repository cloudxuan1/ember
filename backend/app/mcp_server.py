"""ember 的 MCP 工具层：存 / 搜 / 看 / 改 / 删 / 列 / 状态 + briefing（V6b）
+ chat-lite 思考彩蛋词库两个工具，少而清楚。纪律写进工具本身。"""

import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app import briefing, memories, settings, thinking_words

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
def memory_search(
    query: str,
    space: str | None = None,
    limit: int = 8,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """搜索记忆（语义 + 关键词 hybrid），返回目录（编号 id + 发生日期 date + 短内容），不含全文。

    用户提到过去的事、人物、约定、偏好时先调用这个。
    V5 起带语义检索：查询词和记忆字面不同也能召回（搜"难过"能找到
    "眼泪在眼眶里打转"），所以用自然的词直接搜就好，不用猜原文用词。
    需要某条的完整内容和来源时，再用 memory_recall(id) 取详情。
    space 语义（V6 空间隔离）：**不传 = 只搜 personal 核心层**（关系与个人）。
    聊项目/技术话题务必显式传对应空间（ember / vps / ...）；
    确实要跨全库找传 "all"。
    限定发生日期：date_from / date_to，可只写到月或年——"8 月发生的"传
    date_from="2026-08", date_to="2026-08"；只记到月的记忆算在那个月里。
    """
    try:
        results = memories.search_memories(
            query, space=space, limit=min(limit, 20), date_from=date_from, date_to=date_to
        )
    except ValueError as err:
        return {"error": str(err)}
    return {"count": len(results), "results": results}


@mcp.tool()
def memory_recall(id: int) -> dict:
    """按 ID 取一条记忆的完整内容、来源证据（source_ref + 原话片段）和关系边。

    边自带对方记忆的一行摘要（不用挨个再查）；区间型记忆带 interval_status
    （upcoming / ongoing / ended，按今天现算）。
    """
    memory = memories.get_memory(id)
    if memory is None:
        return {"error": f"记忆 {id} 不存在（或已被删掉，轩能在网页「最近动静」里恢复）"}
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
    """存一条记忆，直接进库，马上就能搜到。返回它的编号 id。

    date（必填）= 事情真正发生的日子，不是今天（除非事情就发生在今天）：
    写具体日期 YYYY-MM-DD（2026-10-18），只记得月份就写 YYYY-MM（2026-07）。
    **不许写"最近""前几天""下周""昨天"这种相对说法**——先按今天的日期换算成具体日期再存。
    content（必填）写原话 + 一句上下文。tags 逗号分隔。内容或日期缺一样都存不进去。
    tier：anchor（长期锚点，慎用）/ normal（默认）/ process（过程性，可清理）。
    topic 填主题或实体名，方便日后去重合并。存之前先 memory_search 一下，已有的就用 memory_edit 补，别存重复的。
    区间型的事（一段状态/计划，非点事件）填 start_date / end_date（格式同 date，可只填一头：
    只有 start = 进行中的开放区间，只有 end = 截止型），状态读时按今天现算。
    links 连关系边：存之前你刚 memory_search 过，看到相关旧记忆就顺手连上——
    [{"id": 目标记忆id, "relation": "led_to/same_as/contradicts/supersedes/related",
      "dir": "out"(本条→目标，默认) 或 "in"(目标→本条)}]。
    led_to 方向 = 因 → 果；supersedes 会把被压过的旧记忆标记 superseded_by。
    """
    try:
        saved = memories.save_memory(
            date=date, content=content, tags=tags, tier=tier, topic=topic, space=space,
            start_date=start_date, end_date=end_date, links=links, by="ke",
        )
    except ValueError as err:
        return {"error": str(err)}
    memory = memories.get_memory(saved["id"])
    out = {
        "id": saved["id"],
        "date": memory["date"],
        "status": "saved",
        "message": f"存好了，编号 {saved['id']}，现在就能搜到。",
    }
    if "edges_written" in saved:
        out["edges_written"] = saved["edges_written"]
    return out


@mcp.tool()
def memory_edit(
    memory_id: int,
    content: str | None = None,
    tags: str | None = None,
    topic: str | None = None,
    date: str | None = None,
) -> dict:
    """改一条记忆的 content / tags / topic / date，直接改、立刻生效；改前的旧内容自动留作「上一版」，轩能回退。

    先读后改：只给 memory_id、不给内容 = 读出当前的 content / tags / topic / date。
    - content 是整段替换：在末尾补一句，就传「原文 + 新增」的完整全文；不要顺手润色、压缩原话。
    - tags 也是整组替换（加标签要带上原有的）。tags / topic 不传或传空 = 不改，不能清空。
    - date 规矩同 memory_save：具体日期或只到月，不写相对说法。
    - 只用来改错字、补原话、补标签 / 主题 / 日期。事情本身变了（计划成了结果、想法变了），
      用 memory_save 新存一条，links 连 supersedes 指向旧记忆，旧的留作历史。
    返回里的 target 是改的那一条的日期和开头，核对一下没改错。
    """
    fields = {}
    if content is not None:
        if not str(content).strip():
            return {"error": "content 不能改成空"}
        fields["content"] = content
    if tags is not None and memories.normalize_tags(str(tags)):
        fields["tags"] = tags
    if topic is not None and str(topic).strip():
        fields["topic"] = topic
    if date is not None and str(date).strip():
        fields["date"] = date
    memory = memories.get_memory(memory_id)
    if memory is None:
        return {"error": f"记忆 {memory_id} 不存在（或已被删掉）"}
    if not fields:  # 只给编号 = 读
        return {
            "memory_id": memory_id,
            "date": memory["date"],
            "content": memory["content"],
            "tags": memory["tags"],
            "topic": memory["topic"],
        }
    try:
        result = memories.update_memory(memory_id, fields, by="ke")
    except ValueError as err:
        return {"error": str(err)}
    if result is None:
        return {"error": f"记忆 {memory_id} 刚被删掉了，这次没改上"}
    after = result["memory"]
    target = {"date": after["date"], "preview": _preview(after["content"])}
    if not result["changed"]:
        return {"memory_id": memory_id, "target": target, "status": "unchanged",
                "message": "跟现在的内容一样，没有要改的。"}
    return {
        "memory_id": memory_id,
        "target": target,
        "status": "updated",
        "changed": result["changed"],
        "message": "改好了，已经生效；改前的内容留作上一版，轩能在网页回退。",
    }


def _preview(content: str, n: int = 40) -> str:
    return content[:n] + ("…" if len(content) > n else "")


@mcp.tool()
def memory_delete(memory_id: int) -> dict:
    """删一条记忆（比如存重复了、存错了）。删了先藏起来：搜不到、recall 不到，
    但轩在网页「最近动静」里还能恢复，过了显示期限（默认 30 天）才真删。
    只删确定不要的；内容有错用 memory_edit 改，事情变了用 memory_save 新存一条。
    轩可以在设置里关掉小克删除的权限，关着时这里会报错。
    """
    if not settings.get_settings()["ke_can_delete"]:
        return {"error": "轩在设置里关掉了小克删除记忆的权限；要删请告诉轩，让她在网页上删"}
    memory = memories.get_memory(memory_id)
    if memory is None:
        return {"error": f"记忆 {memory_id} 不存在（或已经删掉了）"}
    result = memories.delete_memory(memory_id, by="ke")
    if result is None:
        return {"error": f"记忆 {memory_id} 不存在（或已经删掉了）"}
    return {
        "memory_id": memory_id,
        "target": {"date": memory["date"], "preview": _preview(memory["content"])},
        "status": "deleted",
        "message": f"删掉了。{result['restorable_days']} 天内轩能在网页「最近动静」里恢复。",
    }


@mcp.tool()
def memory_list(
    space: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
) -> dict:
    """分页浏览记忆目录，先返回统计概览（总数、按空间、按层级），再给当页短条目。

    永远分页（每页 20 条），不会一次吐出全库，按发生日期新的在前。
    date_from / date_to 可只写到月或年：date_from="2026-08", date_to="2026-08" = 8 月发生的。
    space 语义与 memory_search 一致：不传 = 只看 personal 核心层；
    项目空间显式传（ember / vps / ...）；跨全库传 "all"。
    """
    try:
        return memories.list_memories(space=space, date_from=date_from, date_to=date_to, page=page)
    except ValueError as err:
        return {"error": str(err)}


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
