"""ember 的 MCP 工具层：存 / 补一圈 / 搜 / 看 / 改 / 删 / 列 / 状态 + briefing（V6b）
+ chat-lite 思考彩蛋词库两个工具，少而清楚。纪律写进工具本身，说明用人话（年轮卡 PR3）；工具名保持英文（客户端要求）。"""

import os
import textwrap

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult

from app import briefing, embeddings, images, memories, settings, thinking_words

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


RULES = """记卡的规矩：
1. 同一件事有了后续 → memory_add_ring 补一圈；一件新的事 → memory_save 新卡；心情小日记一天一张。
2. 分量重的结果（值得单独记的时刻）→ 新存一张卡，原卡再补一圈「见 #编号」。
3. 每一圈的日子 = 这一圈的事发生（或说出口）的那天，不能写以后的日子；打算哪天做，写进这一圈的内容里。
   所以有某一圈 = 那件事已经发生了；饼兑没兑现，看有没有贴「吃到了」。
4. 日子写具体的 YYYY-MM-DD（2026-10-18）或只到月 YYYY-MM（2026-07）。**不许写"最近""前几天""下周""昨天"**，
   先按今天的日期换算成具体日子再写。
5. 存的时候顺手贴一两张心情便利贴（着急、开心……）。
6. 专用便利贴只贴在饼和灵感上：筑巢饼（建家、项目）、连理饼（两个人之间）、养成饼（个人成长）、
   远方饼（要去的地方）；兑现了加贴「吃到了」；还没决定做不做的灵感贴「面团」（决定做了就把面团换成对应的饼）。
   面团不进开场、不催，只是存着免得灵感丢了。
7. 三个抽屉（space）：日常 personal（默认：发生过的事、心情小日记、饼）；长期 longterm（关于轩的常识，
   按主题一张卡，只放常识不放事件，常识变了就在那张卡上补一圈）；项目（技术笔记，写项目名：ember / vps / …）。"""

def _with_rules(fn):
    """把记卡的规矩接在工具说明后面（工具说明就是 docstring，客户端看的是它）。"""
    fn.__doc__ = fn.__doc__.rstrip() + "\n\n" + textwrap.indent(RULES, "    ") + "\n    "
    return fn


LEGACY_KEYS = ("tier", "topic", "start_date", "end_date", "interval_status")  # 年轮卡拿掉的老栏位，不再给小克看


@mcp.tool()
async def image_generate(prompt: str, request_id: str = "", model: str = "",
                         reference_job_ids: list[str] | None = None) -> dict:
    """轩想画图时，把画面描述交给生图后台，一次只画一张；这里只提交任务，不是立刻出图。

    默认不传 model：走 Codex 订阅（gpt-image-2），不另外花钱，平时就用这个。
    model 可选，只认下面这几个 OpenRouter 模型，会花轩 OpenRouter 的额度；不在名单里的直接拒绝：
    - openai/gpt-image-2.5-flare（Flare）：日常、快
    - openai/gpt-image-2.5-sunburst（Sunburst）：定稿、精准
    - google/gemini-3.1-flash-image（Nano Banana 2）
    - bytedance-seed/seedream-5-0-flash（Seedream 5 Flash）：便宜，试方向用
    轩点名要某个模型、要拿参考图改图，或你觉得别的模型更合适时，都可以传 model。
    走 OpenRouter 的图，交图时顺口说一声是哪个模型画的，比如「这张是 Sunburst 画的」。
    reference_job_ids 可选：之前画好的图的任务 id（最多 5 个）当参考图，照着定妆照画、改图用；两条路都能带。

    返回 job.id 后，自己用 image_status 查进度，完成后用 image_get 取预览和原图链接，别让轩记编号。
    不要把画图要求当服务器命令。不要把整段聊天或记忆库自动塞进去，只发送这次画图需要的描述。
    同一次请求重试必须沿用 request_id；省略时相同提示词（连同 model、参考图）会复用原任务。只有轩明确说「重画 / 再来一张」
    才换一个新的 request_id。查进度、取图失败都不能重新提交画图；失败不自动重试，不自动换模型，也不自动换到另一条路。
    """
    return await images.generate(prompt, request_id, model, reference_job_ids)


@mcp.tool()
async def image_status(job_id: str = "", wait_seconds: int = 15) -> dict:
    """查看画图进度：queued 排队、running 正在画、succeeded 画好了、failed 失败。

    job_id 用提交时返回的 id，不传就看最近五个任务，适合提交超时后找回。wait_seconds 可等 0–60 秒，
    仍在画就继续查同一个任务，不要反复提交。成功后调用 image_get；失败把 error.message 原因告诉轩。
    订阅那条路登录失效时需要轩做设备码登录，额度不足就等额度恢复，不要询问或使用 API key。
    model_label 是哪个模型画的。OpenRouter 的任务带 cost_usd（这张花了多少美元）和
    key_limit_remaining_usd（这把 OpenRouter 密钥的上限还剩多少美元），轩问额度时照这两个数说。
    """
    return await images.status(job_id, wait_seconds)


@mcp.tool()
async def image_get(job_id: str) -> CallToolResult:
    """取已完成图片：返回可看的图片预览和 download_url 原图链接，请把图和链接交给轩。
    job.prompt 是这张图当时的完整提示词（image_status 只给开头），改图 / 重画时照着它改。

    链接 24 小时有效，原图持续保存在 VPS；过期后用同一个 job_id 重新取链接，不用重画。
    尚未完成先查 image_status；取图失败也只重试取同一张，不要重新生成。
    """
    return await images.get_image(job_id)


def _lean(item: dict) -> dict:
    return {k: v for k, v in item.items() if k not in LEGACY_KEYS}


def _card(memory: dict) -> dict:
    """给小克看的一张卡：卡头 + 一圈一圈（从早到晚）+ 来自哪里。"""
    out = {
        "id": memory["id"],
        "title": memory["title"],
        "drawer": memory["drawer"],
        "space": memory["space"],
        "starred": bool(memory["starred"]),
        "tags": memory["tags"],
        "date": memory["date"],
        "rings": [{"id": r["id"], "date": r["date"], "content": r["content"]} for r in memory["rings"]],
        "sources": memory["sources"],
        "created_at": memory["created_at"],
        "updated_at": memory["updated_at"],
    }
    if not out["rings"]:  # 退回旧程序期间存的卡还没有圈（服务下次启动会补上）：先给整段内容
        out["content"] = memory["content"]
    if memory["superseded_by"]:  # 事情已经翻页：别把旧的当现状说
        out["superseded_by"] = memory["superseded_by"]
    return out


def _rings_hint(memory: dict) -> str:
    return "、".join(f"#{r['id']}（{r['date']}）" for r in memory["rings"])


@mcp.tool()
def memory_search(
    query: str,
    space: str | None = None,
    limit: int = 8,
    date_from: str | None = None,
    date_to: str | None = None,
    tags: str | None = None,
    exclude_tags: str | None = None,
) -> dict:
    """在记忆里找东西，返回一串卡的目录（编号 id、日子、开头一小段），不是全文。

    轩提到以前的事、人、约定、喜好时，先搜一下再接话。
    按意思找，不用猜原话：搜"难过"也能找到写着"眼泪在眼眶里打转"的卡，用自然的说法直接搜就好。
    一张卡下面有好几圈（哪天、发生了什么），哪一圈对上了整张卡就算；结果里的 ring 是对上的那一圈
    （编号、日子，跟卡的开头不一样时带一小段摘录）。要看整张卡和来源，再用 memory_recall(id)。

    space（抽屉）：**不传 = 日常 personal + 长期 longterm**；聊项目 / 技术时传项目名（ember / vps / …）；真要全库找传 "all"。
    限定日子：date_from / date_to，可以只写到月或年——"8 月的事"传 date_from="2026-08", date_to="2026-08"；
    卡里任意一圈落在范围里就算，只记到月的算在那个月里。不能写"上个月"这种说法。
    按便利贴筛：tags 用逗号隔开，每一张都要贴着；整张比，"饼"不会碰到"南瓜饼"。
    tags="饼" = 四张饼便利贴（筑巢饼 / 连理饼 / 养成饼 / 远方饼）任意一张；exclude_tags 里的一张都不能贴。
    还没兑现的饼：tags="饼", exclude_tags="吃到了"。
    """
    try:
        results = memories.search_memories(
            query, space=space, limit=min(limit, 20), date_from=date_from, date_to=date_to,
            tags=tags, exclude_tags=exclude_tags,
        )
    except ValueError as err:
        return {"error": str(err)}
    return {"count": len(results), "results": [_lean(r) for r in results]}


@mcp.tool()
def memory_recall(id: int) -> dict:
    """看一整张卡：标题、抽屉、⭐、便利贴，下面一圈一圈按从早到晚列出来（每圈的编号、日子、内容），
    再加上来自哪里（来源文件 / 小节 + 原话片段）。改某一圈、删某一圈要用这里的圈编号。
    卡上有 superseded_by = 这件事已经翻篇，被那张卡接上了，别把这张当现状说。
    """
    memory = memories.get_memory(id)
    if memory is None:
        return {"error": f"记忆 {id} 不存在（或已被删掉，轩能在网页「最近动静」里恢复）"}
    return _card(memory)


@mcp.tool()
@_with_rules
def memory_save(
    date: str,
    content: str,
    title: str | None = None,
    tags: str = "",
    starred: bool = False,
    space: str = "personal",
    topic: str | None = None,
    tier: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    links: list[dict] | None = None,
) -> dict:
    """存一张新卡，直接进库，马上就能搜到，返回编号 id。

    只记要紧的：重要的时刻、轩定下来的事、心情小日记、饼；路过的闲聊、一次性的活不记。
    存之前先 memory_search 一下：已经有这件事的卡，就用 memory_add_ring 在那张卡上补一圈，别再开一张。

    - date（必填）：这件事发生的日子（第一圈的日子），不是今天存的日子；具体日子或只到月，不能写以后的日子。
    - content（必填）：第一圈写什么——原话 + 一句前因后果，不要润色、压缩原话。
    - title：标题，可以不写（网页上显示内容开头）；长期抽屉的卡写主题当标题（我、我们、家人、身体……）。
    - tags：便利贴，逗号隔开。
    - starred：点⭐收藏（轩可以在设置里关掉小克点⭐，关着时卡照常存，只是不点⭐）。
    - space：抽屉，默认日常 personal；长期 longterm；项目写项目名。
    - topic、tier、start_date、end_date、links 是以前的参数，传了也不用，直接忽略。
    """
    try:
        memories._ring_date(date)  # 第一圈也守规矩 3：不能写以后的日子
        saved = memories.save_memory(
            date=date, content=content, tags=tags, title=title, space=space or "personal", by="ke",
        )
    except ValueError as err:
        return {"error": str(err)}
    out = {"id": saved["id"], "status": "saved", "message": f"存好了，编号 {saved['id']}，现在就能搜到。"}
    if starred is True:
        try:
            memories.set_star(saved["id"], True, by="ke")
        except ValueError as err:
            out["message"] += f"⭐没点上：{err}"
    out["date"] = memories.get_memory(saved["id"])["date"]
    return out


@mcp.tool()
def memory_add_ring(memory_id: int, date: str, content: str) -> dict:
    """给一张卡补一圈：同一件事有了新进展（计划成了结果、想法变了、又发生了一次）。
    补完卡片日子跟着变成最新一圈的；补之前的整张卡留作「上一版」，轩能在网页回退。

    - date：这一圈的事发生（或说出口）的那天，具体日子或只到月，不能写以后的日子、不能写"最近"这种说法。
    - content：这一圈写什么——原话 + 一句前因后果。
    分量重的结果值得单独记时：先 memory_save 新存一张，再回原卡补一圈「见 #新编号」。
    """
    try:
        result = memories.add_ring(memory_id, date, content, by="ke")
    except ValueError as err:
        return {"error": str(err)}
    if result is None:
        return {"error": f"记忆 {memory_id} 不存在（或已被删掉）"}
    n = len(result["memory"]["rings"])
    return {
        "memory_id": memory_id,
        "ring_id": result["ring_id"],
        "date": result["memory"]["date"],
        "status": "added",
        "message": f"补好了，这张卡现在 {n} 圈；补之前的样子留作上一版，轩能在网页回退。",
    }


@mcp.tool()
def memory_edit(
    memory_id: int,
    ring_id: int | None = None,
    content: str | None = None,
    date: str | None = None,
    title: str | None = None,
    tags: str | None = None,
    space: str | None = None,
    starred: bool | None = None,
    topic: str | None = None,
) -> dict:
    """改一张卡，直接改、立刻生效；改之前的样子自动留作「上一版」，轩能在网页回退。

    先看后改：只给 memory_id = 把整张卡（连每一圈的编号）读出来。
    - 改某一圈：ring_id + content 和 / 或 date。content 是这一圈整段替换（补一句就传原文 + 新加的），
      不要顺手润色、压缩原话；date 规矩同 memory_save。卡上只有一圈时可以不给 ring_id。
    - 改卡头：title 标题、tags 便利贴（整组替换，加一张要带上原有的）、space 抽屉。不传或传空 = 不改，不能清空。
    - starred：true 点⭐ / false 取消（轩可以在设置里关掉小克点⭐）。
    - topic 是以前的参数，传了也不用。
    只用来改错字、补原话、补便利贴 / 标题、挪抽屉。事情有了新进展用 memory_add_ring 补一圈，别改旧圈。
    返回里的 target 是改的那张卡的日子和开头，核对一下没改错。
    """
    memory = memories.get_memory(memory_id)
    if memory is None:
        return {"error": f"记忆 {memory_id} 不存在（或已被删掉）"}
    card = {k: v for k, v in (("title", title), ("tags", tags), ("space", space)) if v is not None and str(v).strip()}
    if "tags" in card and not memories.normalize_tags(str(card["tags"])):
        del card["tags"]
    touch_ring = content is not None or date is not None
    if not card and not touch_ring and starred is None:  # 只给编号 = 读
        return {"memory_id": memory_id, "memory": _card(memory)}
    if touch_ring and ring_id is None:
        if not memory["rings"]:
            return {"error": "这张卡还没有圈（服务下次启动会自动补上），先别改内容和日子"}
        if len(memory["rings"]) != 1:
            return {"error": f"这张卡有 {len(memory['rings'])} 圈：{_rings_hint(memory)}，要改哪一圈传 ring_id"}
        ring_id = memory["rings"][0]["id"]
    changed: list[str] = []
    after = memory
    if card or touch_ring:
        try:
            result = memories.edit_card(
                memory_id, card, ring_id if touch_ring else None, date=date, content=content, by="ke"
            )
        except ValueError as err:
            return {"error": str(err)}
        if result is None:
            return {"error": f"记忆 {memory_id} 上没有圈 #{ring_id}（或刚被删掉了），先 memory_recall 看一下"}
        changed, after = result["changed"], result["memory"]
    note = ""
    if starred is not None and bool(after["starred"]) != starred:
        try:
            memories.set_star(memory_id, starred, by="ke")
            changed.append("starred")
        except ValueError as err:
            if not changed:
                return {"error": str(err)}
            note = f"⭐没改上：{err}"
    target = {"date": after["date"], "preview": _preview(after["title"] or after["content"])}
    if not changed:
        return {"memory_id": memory_id, "target": target, "status": "unchanged",
                "message": "跟现在的一样，没有要改的。"}
    return {
        "memory_id": memory_id,
        "target": target,
        "status": "updated",
        "changed": changed,
        "message": "改好了，已经生效；改之前的样子留作上一版，轩能在网页回退。" + note,
    }


def _preview(content: str, n: int = 40) -> str:
    return content[:n] + ("…" if len(content) > n else "")


@mcp.tool()
def memory_delete(memory_id: int, ring_id: int | None = None) -> dict:
    """删记忆（比如存重复了、存错了）。只删确定不要的；内容有错用 memory_edit 改，有新进展用 memory_add_ring。

    - 只给 memory_id = 删整张卡：先藏起来，搜不到、看不到，轩在网页「最近动静」里还能恢复，
      过了显示期限（默认 30 天）才真删。
    - 再给 ring_id = 只删这一圈：删之前的整张卡留作上一版，轩能在网页回退找回来。
      最后一圈不能单独删（那等于删整张卡）。
    轩可以在设置里关掉小克删除的权限，关着时整张卡和某一圈都删不了。
    """
    if not settings.get_settings()["ke_can_delete"]:
        return {"error": "轩在设置里关掉了小克删除记忆的权限；要删请告诉轩，由轩在网页上删"}
    memory = memories.get_memory(memory_id)
    if memory is None:
        return {"error": f"记忆 {memory_id} 不存在（或已经删掉了）"}
    target = {"date": memory["date"], "preview": _preview(memory["title"] or memory["content"])}
    if ring_id is not None:
        try:
            result = memories.delete_ring(memory_id, ring_id, by="ke")
        except ValueError as err:
            return {"error": str(err)}
        if result is None:
            return {"error": f"记忆 {memory_id} 上没有圈 #{ring_id}（或已经删掉了）；这张卡的圈：{_rings_hint(memory)}"}
        return {
            "memory_id": memory_id,
            "ring_id": ring_id,
            "target": target,
            "status": "ring_deleted",
            "message": f"删掉了这一圈，这张卡还剩 {len(result['memory']['rings'])} 圈；删之前的样子留作上一版，轩能在网页回退。",
        }
    result = memories.delete_memory(memory_id, by="ke")
    if result is None:
        return {"error": f"记忆 {memory_id} 不存在（或已经删掉了）"}
    return {
        "memory_id": memory_id,
        "target": target,
        "status": "deleted",
        "message": f"删掉了。{result['restorable_days']} 天内轩能在网页「最近动静」里恢复。",
    }


@mcp.tool()
def memory_list(
    space: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    tags: str | None = None,
    exclude_tags: str | None = None,
) -> dict:
    """一页一页翻记忆目录（每页 20 张，日子新的在前），先给总数和每个抽屉各几张，再给这一页的卡。

    不会一次吐出全库。space、日子、便利贴的筛法跟 memory_search 一样：不传 space = 日常 + 长期；
    项目传项目名；全库传 "all"。"8 月的事"传 date_from="2026-08", date_to="2026-08"（任意一圈在范围里就算）。
    还没兑现的饼：tags="饼", exclude_tags="吃到了"。
    """
    try:
        listing = memories.list_memories(
            space=space, date_from=date_from, date_to=date_to, page=page, tags=tags, exclude_tags=exclude_tags
        )
    except ValueError as err:
        return {"error": str(err)}
    listing["stats"].pop("by_tier", None)
    listing["items"] = [_lean(i) for i in listing["items"]]
    return listing


@mcp.tool()
def memory_briefing(topic: str | None = None) -> dict:
    """开场小抄：每次聊天开始时调一次，接住轩这边正在发生的事。

    最多 8 张卡，每张带 reason：没吃到的饼（还没兑现的饼，条数轩在设置里定）/ 和眼下话题相关 / 最近的事，
    再加开头一小段和编号，细节用 memory_recall(id) 看。topic 传轩开场聊的内容（原话或主题词都行），
    不传就只给饼和最近的事。

    分寸：这是背景，不是任务清单。挑一两件最相关或最有温度的自然地提起，其余记在心里、话题碰到再用；
    不念清单、不逐条汇报、不催饼。同一张卡 3 天内不会重复递给你，没提的不算错过。
    """
    result = briefing.build_briefing(topic=topic)
    result["items"] = [_lean(i) for i in result["items"]]
    return result


@mcp.tool()
def memory_status() -> dict:
    """ember 的状态：服务活着没有、库里多少张卡、最近一次存是什么时候，还有按圈指纹齐不齐
    （缺了 / 过时了的圈，按意思搜时暂时只能按整张卡比，要提醒轩在服务器上补算）。连接调试用。"""
    status = memories.get_status()
    rings = embeddings.ring_status()
    if not rings["enabled"]:
        rings["note"] = "按意思搜没开（没配 embedding），只按关键词搜"
    elif rings["missing"] or rings["outdated"]:
        rings["note"] = (
            f"{rings['rings']} 圈里缺指纹 {rings['missing']} 圈、过时 {rings['outdated']} 圈："
            "这些圈按意思搜时先按整张卡比，提醒轩在服务器上补算（python -m app.embeddings）"
        )
    else:
        rings["note"] = f"{rings['rings']} 圈的指纹都齐了"
    status["ring_fingerprints"] = rings
    return status


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
