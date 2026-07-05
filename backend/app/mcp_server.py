"""ember 的 MCP 工具层：五个工具，少而清楚。纪律写进工具本身。"""

import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app import memories

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
    space 可选：personal（默认核心层，关系与个人）/ ember / vps 等项目空间。
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
    """保存一条新记忆。

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
    """
    return memories.save_memory(
        date=date, content=content, tags=tags, tier=tier, topic=topic, space=space,
        start_date=start_date, end_date=end_date, links=links, links_by="mcp",
    )


@mcp.tool()
def memory_list(
    space: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
) -> dict:
    """分页浏览记忆目录，先返回统计概览（总数、按空间、按层级），再给当页短条目。

    永远分页（每页 20 条），不会一次吐出全库。日期格式 YYYY-MM-DD。
    """
    return memories.list_memories(
        space=space, date_from=date_from, date_to=date_to, page=page
    )


@mcp.tool()
def memory_status() -> dict:
    """ember 服务状态：活着吗、库里多少条记忆/边/来源、最近一次写入时间。连接调试用。"""
    return memories.get_status()
