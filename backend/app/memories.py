"""记忆的读写与检索。纪律：目录先行（短结果），真正 recall 才取全文。

单库（2026-10 改造）：一条记忆只存一份，存了就进库，谁改都直接改这一份。
- 改：改前的整条旧值挤进 memory_history（app/history.py），只留最近 N 版；能回退（回退也算一次改）。
- 删：先藏起来（deleted_at），所有读和搜都看不见；最近动静里能恢复，过了显示期限才真删（purge_expired）。
- 每次存 / 改 / 回退 / 删 / 恢复都在同一个写事务里记一行最近动静（app/activity.py）。
by = 谁干的：xuan（轩，网页登录）/ ke（小克，MCP 和提取脚本）。
"""

import json
import re
from contextlib import contextmanager
from datetime import date as _date
from datetime import datetime, timedelta, timezone

from app import activity, embeddings, history, rings, settings
from app.db import get_conn

SUMMARY_LEN = 120  # search/list 返回的目录条目里 content 的截断长度

RRF_K = 60      # RRF 融合常数（业界惯用值）：越大头部排名的权重差越平缓
VEC_TOP_K = 40  # 语义腿取多少候选进融合

RELATIONS = ("led_to", "same_as", "contradicts", "supersedes", "related")

# 网页 / 小克能改的字段白名单——is_resolved/superseded_by 等状态列不开放手改
MEMORY_EDITABLE = history.SNAPSHOT_FIELDS
TIERS = ("anchor", "normal", "process")
LIVE = "deleted_at IS NULL"  # 删了先藏起来：所有读和搜都带上这一句
# 修改时间带毫秒；_touch 保证每次递增，同一毫秒连改或系统时钟回拨也不会撞版本。
NOW_MS = "strftime('%Y-%m-%d %H:%M:%f', 'now', '+8 hours')"
BROWSE_PAGE_MAX = 200
MAX_ID = 2**63 - 1  # SQLite 整数上限：更大的编号直接当不存在

_TZ_BEIJING = timezone(timedelta(hours=8))  # 与库里 created_at 的 +8 保持一致

DATE_HINT = "发生日期要写具体的日子（2026-10-18）或只到月（2026-10），不能写「最近」「下周」这种相对说法"


class Conflict(Exception):
    """网页保存时，这条记忆在轩打开之后又被改过（多半是小克）：这次不覆盖，刷新再看。"""


def _today() -> str:
    return datetime.now(_TZ_BEIJING).date().isoformat()


def normalize_tags(tags: str) -> str:
    """中文逗号/顿号一律当分隔符——轩打中文标点不该被卡住。"""
    for sep in ("，", "、"):
        tags = tags.replace(sep, ",")
    return ",".join(t.strip() for t in tags.split(",") if t.strip())


_FULLWIDTH = str.maketrans("０１２３４５６７８９－．／", "0123456789-./")


def normalize_date(value, *, allow_year: bool = False) -> str:
    """发生日期：YYYY-MM-DD 或只到月 YYYY-MM（. / 全角数字 2026年7月18日 也认，月日补零），
    得是日历上真有的日子。allow_year=True 时还认只有年份（按日期范围筛选用）。"""
    text = str(value or "").strip().translate(_FULLWIDTH)
    text = re.sub(r"\s*[年月日]$", "", re.sub(r"(\d)\s*[年月]\s*(?=\d)", r"\1-", text))
    text = text.replace("/", "-").replace(".", "-")
    m = re.fullmatch(r"([0-9]{4})(?:-([0-9]{1,2})(?:-([0-9]{1,2}))?)?", text)
    if not m or (m.group(2) is None and not allow_year):
        raise ValueError(DATE_HINT)
    year, month, day = m.group(1), m.group(2), m.group(3)
    if month is None:
        return year
    if not 1 <= int(month) <= 12:
        raise ValueError(DATE_HINT)
    if day is None:
        return f"{year}-{int(month):02d}"
    try:
        return _date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        raise ValueError(DATE_HINT) from None


def _period(value: str) -> tuple[str, str]:
    """一个日期覆盖的头尾两天（字符串比较用）：'2026' → 01-01..12-31；'2026-08' → 08-01..08-31。
    月底一律写 31：只拿来比大小，'2026-09-31' 照样排在所有 9 月的日子后面。"""
    if len(value) == 4:
        return f"{value}-01-01", f"{value}-12-31"
    if len(value) == 7:
        return f"{value}-01", f"{value}-31"
    return value, value


def _next_period(prefix: str) -> str:
    """'2026' → '2027'；'2026-12' → '2027-01'：只写到年 / 月的终点用开区间，老数据里没补零的
    '2026-08-5' 也算在 8 月里（它按字符串比比 '2026-08-31' 大）。"""
    if len(prefix) == 4:
        return f"{int(prefix) + 1:04d}"
    year, month = int(prefix[:4]), int(prefix[5:7])
    return f"{year + month // 12:04d}-{month % 12 + 1:02d}"


def _date_end(col: str) -> str:
    """记忆自己覆盖到哪天为止：只到月的 '2026-08' 算到 8 月底。"""
    return f"(CASE WHEN length({col}) = 7 THEN {col} || '-31' WHEN length({col}) = 4 THEN {col} || '-12-31' ELSE {col} END)"


def date_conditions(date_from: str | None, date_to: str | None, col: str = "date") -> tuple[list[str], list]:
    """按发生日期筛，记忆和查询范围有重叠就算：两头都能只写到年或月——
    date_from="2026-08", date_to="2026-08" = 8 月发生的；只到月的记忆（'2026-08'）算在那个月里，
    从 8 月 10 日起查也会带上它（那个月里哪天都有可能）。"""
    conds, params = [], []
    lo = _period(normalize_date(date_from, allow_year=True))[0] if date_from else None
    to = normalize_date(date_to, allow_year=True) if date_to else None
    if lo and to and lo > _period(to)[1]:
        raise ValueError("开始日期晚于结束日期，两头是不是填反了")
    if lo:
        conds.append(f"{_date_end(col)} >= ?")
        params.append(lo)
    if to and len(to) == 10:
        conds.append(f"{col} <= ?")
        params.append(to)
    elif to:
        conds.append(f"{col} < ?")
        params.append(_next_period(to))
    return conds, params


def interval_status(start_date: str | None, end_date: str | None, today: str | None = None) -> str | None:
    """区间状态读时现算，不存死（V4）。点事件（无 start/end）返回 None。

    只有 start = 进行中的开放区间；只有 end = 截止型（到期前算 ongoing）。
    ISO 日期字符串可直接比大小。
    """
    if not start_date and not end_date:
        return None
    today = today or _today()
    if start_date and today < _bound(start_date, 0):
        return "upcoming"
    if end_date and today > _bound(end_date, 1):
        return "ended"
    return "ongoing"


def _bound(value: str, end: int) -> str:
    """区间端点只到月 / 年时，起点按月初、终点按月底算（'2026-10' 截止 = 整个 10 月都还没过期）。"""
    try:
        return _period(normalize_date(value, allow_year=True))[end]
    except ValueError:
        return value  # 老数据里认不出的格式照原样比


def _short(row) -> dict:
    content = row["content"]
    item = {
        "id": row["id"],
        "date": row["date"],
        "content": content[:SUMMARY_LEN] + ("…" if len(content) > SUMMARY_LEN else ""),
        "tags": row["tags"],
        "tier": row["tier"],
        "topic": row["topic"],
        "space": row["space"],
    }
    status = interval_status(row["start_date"], row["end_date"])
    if status:  # 目录保持精瘦：点事件不带区间字段
        item["start_date"] = row["start_date"]
        item["end_date"] = row["end_date"]
        item["interval_status"] = status
    if row["superseded_by"]:  # 事实已翻页：目录里就要让模型看见，别把旧事实当现状说
        item["superseded_by"] = row["superseded_by"]
    return item


# 三个抽屉（年轮卡，2026-10）：键名 → 网页上叫什么。personal 键名不变（老数据和工具都认它）；
# 其余空间（ember / vps / …）都归「项目」抽屉。默认搜索带上长期是 PR2，网页切抽屉是 PR4。
LONGTERM = "longterm"
DRAWER_LABELS = {LONGTERM: "长期", "personal": "日常"}
PROJECT_LABEL = "项目"
# 四张饼便利贴（方案第二节）：网页的「饼」按钮 = 贴了其中任意一张；整张比，正文里写着「南瓜饼」不算
PIE_TAGS = ("筑巢饼", "连理饼", "养成饼", "远方饼")


def drawer_of(space: str | None) -> str:
    return DRAWER_LABELS.get(space or "personal", PROJECT_LABEL)


def resolve_space(space: str | None) -> str | None:
    """检索空间语义（V6 空间隔离）：personal 是核心底色，不该被项目记忆稀释。

    不传 / 空串 = 只搜 personal 核心层；"all" = 明确要求跨全库；
    其他值 = 指定空间。返回 None 表示不加空间过滤。
    """
    if not space:
        return "personal"
    if space == "all":
        return None
    return space


@contextmanager
def _write_tx(conn=None, wait: bool = True):
    """写事务：给了 conn 就借调用方的事务（不提交）；没给就自开连接、先拿写锁，锁内重读才作数。
    wait=False：写锁被占着就立刻放弃（抛 OperationalError），顺手做的杂活别让读页面干等。"""
    if conn is not None:
        yield conn
        return
    c = get_conn()
    try:
        if not wait:
            c.execute("PRAGMA busy_timeout = 0")
        c.execute("BEGIN IMMEDIATE")
        if not wait:
            c.execute("PRAGMA busy_timeout = 5000")
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()


def _validate_links(links) -> list[dict]:
    """links = [{"id": 目标记忆, "relation": led_to/…, "dir": "out"(本条→目标, 默认)/"in"}]。"""
    if not links:
        return []
    if not isinstance(links, list):
        raise ValueError("links 要是列表")
    out = []
    for i, link in enumerate(links, start=1):
        if not isinstance(link, dict):
            raise ValueError(f"links 第 {i} 条要是对象")
        relation = link.get("relation", "related")
        if relation not in RELATIONS:
            raise ValueError(f"links 第 {i} 条 relation 必须是 {'/'.join(RELATIONS)}，收到: {relation}")
        direction = link.get("dir", "out")
        if direction not in ("out", "in"):
            raise ValueError(f"links 第 {i} 条 dir 必须是 out/in，收到: {direction}")
        target = link.get("id")
        if not isinstance(target, int) or isinstance(target, bool) or not 0 < target <= MAX_ID:
            raise ValueError(f"links 第 {i} 条要有目标记忆 id（正整数）")
        out.append({"id": target, "relation": relation, "dir": direction})
    return out


def add_edges(conn, memory_id: int, links: list[dict], created_by: str = "") -> int:
    """在调用方事务里写边（不提交），返回实际写入条数。

    软失败：目标不存在 / 已删 / 连自己 / 重复边 → 跳过不报错，记忆本体照常保存。
    supersedes 边同步 superseded_by 列——列是权威，边是导航，单一事实来源。
    """
    written = 0
    for link in _validate_links(links):
        other = link["id"]
        if other == memory_id:
            continue
        if conn.execute(f"SELECT 1 FROM memories WHERE id = ? AND {LIVE}", (other,)).fetchone() is None:
            continue
        from_id, to_id = (memory_id, other) if link["dir"] == "out" else (other, memory_id)
        cur = conn.execute(
            """INSERT OR IGNORE INTO memory_edges (from_id, to_id, relation, created_by)
               VALUES (?, ?, ?, ?)""",
            (from_id, to_id, link["relation"], created_by),
        )
        if cur.rowcount:
            written += 1
            if link["relation"] == "supersedes":
                _recompute_superseded(conn, to_id)  # 跟删 / 恢复 / 真删同一条规矩：最新的活记忆压它
    return written


def _clean_fields(edits: dict) -> dict:
    """白名单 + 规整 + 校验：日期得是具体日子或月份，内容不能空，tags 规整，区间空串 = 清掉。"""
    fields = {k: edits[k] for k in MEMORY_EDITABLE if k in edits}
    for k, v in fields.items():
        if v is not None and not isinstance(v, str):
            raise ValueError(f"{k} 要是文字")
    if "date" in fields:
        fields["date"] = normalize_date(fields["date"])
    if "content" in fields:
        fields["content"] = str(fields["content"] or "").strip()
        if not fields["content"]:
            raise ValueError("内容不能是空的")
    if "tier" in fields and fields["tier"] not in TIERS:
        raise ValueError(f"tier 必须是 {'/'.join(TIERS)}，收到: {fields['tier']}")
    if "tags" in fields:
        fields["tags"] = normalize_tags(str(fields["tags"] or ""))
    if "topic" in fields:
        fields["topic"] = str(fields["topic"] or "").strip()
    if "title" in fields:  # 空 = 没写标题（网页显示正文开头）
        fields["title"] = str(fields["title"] or "").strip() or None
    if "space" in fields:
        fields["space"] = str(fields["space"] or "").strip() or "personal"
    for k in ("start_date", "end_date"):  # 表单送来的空串 = 清掉区间端点
        if k in fields:
            fields[k] = normalize_date(fields[k]) if fields[k] else None
    return fields


def save_memory(
    date: str,
    content: str,
    tags: str = "",
    tier: str = "normal",
    topic: str = "",
    space: str = "personal",
    start_date: str | None = None,
    end_date: str | None = None,
    source_ref: str | None = None,
    quote: str | None = None,
    links: list[dict] | None = None,
    links_by: str = "mcp",
    by: str = "ke",
    conn=None,
    title: str | None = None,
) -> dict:
    """存一张卡，直接进库、马上能搜到；content / date 是第一圈。内容和发生日期缺一样存不进去（ValueError）。
    conn 给定时借用调用方的连接和事务（不提交，commit 归调用方管）——批量存用它保证整批原子。"""
    fields = _clean_fields({
        "date": date, "content": content, "tags": tags or "", "tier": tier or "normal",
        "topic": topic or "", "space": space or "personal",
        "start_date": start_date, "end_date": end_date, "title": title,
    })
    links = _validate_links(links)  # 先校验后落库，坏 links 不留半条记忆
    for name, v in (("source_ref", source_ref), ("quote", quote)):
        if v is not None and not isinstance(v, str):
            raise ValueError(f"{name} 要是文字")

    with _write_tx(conn) as c:
        cur = c.execute(
            """INSERT INTO memories (date, content, tags, tier, topic, space, start_date, end_date, title)
               VALUES (:date, :content, :tags, :tier, :topic, :space, :start_date, :end_date, :title)""",
            fields,
        )
        memory_id = cur.lastrowid
        rings.start_card(c, memory_id, by)  # 第一圈 + 老参数 topic / tier 换成标题 / 便利贴 / ⭐
        if source_ref or quote:
            c.execute(
                "INSERT INTO memory_sources (memory_id, source_ref, quote) VALUES (?, ?, ?)",
                (memory_id, source_ref, quote),
            )
        # 语义指纹软失败：算不出来照常入库，缺席的等 rebuild_embeddings() 补
        embedded = embeddings.embed_memory(
            c, memory_id, fields["content"], topic=fields["topic"], tags=fields["tags"]
        )
        edges_written = add_edges(c, memory_id, links, created_by=links_by)
        activity.log(c, by, "save", memory_id, fields["content"])
    result = {"id": memory_id, "saved": True}
    if embeddings.enabled():
        result["embedded"] = embedded
    if links:
        result["edges_written"] = edges_written
    return result


def save_many(items: list[dict], by: str = "ke") -> list[int]:
    """批量存（提取脚本推送用）：整批一个事务，坏一条整批不写，脚本可放心重试。"""
    if not isinstance(items, list) or not items:
        raise ValueError("要存的记忆不能为空")
    allowed = set(MEMORY_EDITABLE) | {"source_ref", "quote", "links"}
    with _write_tx() as c:
        ids = []
        for i, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"第 {i} 条要是对象")
            try:
                ids.append(save_memory(**{k: v for k, v in item.items() if k in allowed},
                                       links_by="extraction", by=by, conn=c)["id"])
            except (ValueError, TypeError) as e:
                raise ValueError(f"第 {i} 条有问题：{e}") from None
        return ids


def get_memory(memory_id: int, include_deleted: bool = False) -> dict | None:
    """一条记忆的全文 + 年轮（还在的圈，从早到晚）+ 来源 + 关系边。删了的（藏起来的）默认当不存在。"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None or (row["deleted_at"] and not include_deleted):
            return None
        memory = dict(row)
        memory["drawer"] = drawer_of(row["space"])
        memory["rings"] = rings.live(conn, memory_id)
        memory["sources"] = [
            dict(s)
            for s in conn.execute(
                "SELECT source_ref, quote FROM memory_sources WHERE memory_id = ?", (memory_id,)
            ).fetchall()
        ]
        # 边带对方记忆的一行摘要——一次 recall 看见因果脉络，不用再挨个查 id
        memory["edges"] = [
            {
                "from_id": e["from_id"],
                "to_id": e["to_id"],
                "relation": e["relation"],
                "direction": "out" if e["from_id"] == memory_id else "in",
                "other": {
                    "id": e["other_id"],
                    "date": e["other_date"],
                    "content": e["other_content"][:SUMMARY_LEN]
                    + ("…" if len(e["other_content"]) > SUMMARY_LEN else ""),
                    "topic": e["other_topic"],
                },
                "created_by": e["created_by"],
                "created_at": e["created_at"],
            }
            for e in conn.execute(
                """SELECT e.from_id, e.to_id, e.relation, e.created_by, e.created_at,
                          m.id AS other_id, m.date AS other_date,
                          m.content AS other_content, m.topic AS other_topic
                   FROM memory_edges e
                   JOIN memories m
                     ON m.id = CASE WHEN e.from_id = ? THEN e.to_id ELSE e.from_id END
                   WHERE (e.from_id = ? OR e.to_id = ?) AND m.deleted_at IS NULL""",
                (memory_id, memory_id, memory_id),
            ).fetchall()
        ]
    memory["interval_status"] = interval_status(memory["start_date"], memory["end_date"])
    return memory


def _keyword_rows(query: str, space: str | None, extra: tuple[list[str], list] = ([], [])) -> list:
    """关键词腿：LIKE 子串匹配 + 简单加权，中文子串直接好使（不上 FTS5，分词坑不值得踩）。"""
    tokens = query.split() or [query]
    where = " OR ".join(["(content LIKE ? OR tags LIKE ? OR topic LIKE ?)"] * len(tokens))
    params: list = []
    for t in tokens:
        like = f"%{t}%"
        params += [like, like, like]
    sql = f"SELECT * FROM memories WHERE ({where}) AND {LIVE}"
    if space:
        sql += " AND space = ?"
        params.append(space)
    conds, extra_params = extra
    for cond in conds:
        sql += f" AND {cond}"
    params += extra_params
    sql += " ORDER BY date DESC LIMIT 200"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    def score(row) -> int:
        s = 0
        for t in tokens:
            if t in row["content"]:
                s += 2
            if t in row["tags"] or t in row["topic"]:
                s += 3
        if row["tier"] == "anchor":
            s += 1
        return s

    return sorted(rows, key=lambda r: (score(r), r["date"]), reverse=True)


def search_memories(
    query: str,
    space: str | None = None,
    limit: int = 8,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    """hybrid 检索（V5）：关键词腿 + 语义腿，RRF 融合。

    语义腿不可用（没配 key / API 挂了 / 库里还没有当前模型的指纹）时
    自动退回纯关键词，结果形状不变。
    space 不传 = 只搜 personal（V6 隔离）；"all" = 跨全库。
    date_from / date_to 按发生日期筛（见 date_conditions），两条腿都筛。
    """
    space = resolve_space(space)
    dates = date_conditions(date_from, date_to)
    kw_rows = _keyword_rows(query, space, dates)
    vec_ids = embeddings.vector_search(
        query, space=space, k=VEC_TOP_K, where=date_conditions(date_from, date_to, col="m.date")
    )
    if not vec_ids:
        return [_short(r) for r in kw_rows[:limit]]

    by_id = {row["id"]: row for row in kw_rows}
    missing = [mid for mid in vec_ids if mid not in by_id]
    if missing:
        marks = ",".join("?" * len(missing))
        conds, params = dates
        sql = f"SELECT * FROM memories WHERE id IN ({marks}) AND {LIVE}" + "".join(f" AND {c}" for c in conds)
        with get_conn() as conn:
            for row in conn.execute(sql, missing + params):
                by_id[row["id"]] = row

    # RRF：score = Σ 1/(K + 该腿排名)，两边都排前面的浮到最上面
    scores: dict[int, float] = {}
    for rank, row in enumerate(kw_rows):
        scores[row["id"]] = scores.get(row["id"], 0.0) + 1.0 / (RRF_K + rank)
    for rank, mid in enumerate(vec_ids):
        if mid in by_id:  # 已删 / 不在日期范围里的语义候选不算
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (RRF_K + rank)
    top = sorted(scores, key=lambda mid: (scores[mid], by_id[mid]["date"]), reverse=True)
    return [_short(by_id[mid]) for mid in top[:limit]]


def list_memories(
    space: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    space = resolve_space(space)
    conds, params = [LIVE], []
    if space:
        conds.append("space = ?")
        params.append(space)
    date_conds, date_params = date_conditions(date_from, date_to)
    conds += date_conds
    params += date_params
    where = "WHERE " + " AND ".join(conds)
    page = max(page, 1)
    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM memories {where}", params).fetchone()[0]
        by_space = {
            r["space"]: r["n"]
            for r in conn.execute(
                f"SELECT space, COUNT(*) n FROM memories {where} GROUP BY space", params
            ).fetchall()
        }
        by_tier = {
            r["tier"]: r["n"]
            for r in conn.execute(
                f"SELECT tier, COUNT(*) n FROM memories {where} GROUP BY tier", params
            ).fetchall()
        }
        rows = conn.execute(
            f"SELECT * FROM memories {where} ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
            params + [page_size, (page - 1) * page_size],
        ).fetchall()
    return {
        "stats": {"total": total, "by_space": by_space, "by_tier": by_tier},
        "page": page,
        "page_size": page_size,
        "total_pages": max((total + page_size - 1) // page_size, 1),
        "items": [_short(r) for r in rows],
    }


def browse_memories(
    q: str | None = None,
    space: str | None = None,
    page: int = 1,
    page_size: int = 50,
    date_from: str | None = None,
    date_to: str | None = None,
    tag: str | None = None,
    starred: bool = False,
    pie: bool = False,
) -> dict:
    """网页"记忆库"：默认跨全库（管理视角，与检索的 personal 默认不同），content 不截断，
    每张卡带上还在的圈（从早到晚）。
    q 是关键词筛选（标题 / content（含每一圈）/ tags / topic 子串）；q 写成 "#123" 或纯数字时也按编号找。
    tag 只看带这个标签的；starred 只看⭐；pie 只看贴了四张饼便利贴之一的（整张比）。
    按日期筛看圈（「混着来」）：任意一圈落在范围里这张卡就在，按范围里最新那一圈的日子排；
    不筛日期按卡片日期（= 最新一圈）排。都是新的在前，同一天编号大的在前。"""
    conds, params = [LIVE], []
    if space and space != "all":
        conds.append("space = ?")
        params.append(space)
    if q and q.strip():
        q = q.strip()
        number = q.lstrip("#")
        like_cond = "(content LIKE ? OR tags LIKE ? OR topic LIKE ? OR title LIKE ?)"
        if re.fullmatch(r"[0-9]{1,19}", number) and int(number) <= MAX_ID:
            conds.append(f"(id = ? OR {like_cond})")
            params += [int(number)] + [f"%{q}%"] * 4
        else:
            conds.append(like_cond)
            params += [f"%{q}%"] * 4
    if tag:
        conds.append("(',' || tags || ',') LIKE ?")
        params.append(f"%,{tag.strip()},%")
    if starred:
        conds.append("starred = 1")
    if pie:
        conds.append("(" + " OR ".join(["(',' || tags || ',') LIKE ?"] * len(PIE_TAGS)) + ")")
        params += [f"%,{t},%" for t in PIE_TAGS]
    ring_conds, ring_params = date_conditions(date_from, date_to, col="r.date")
    order, order_params = "date DESC, id DESC", []
    if ring_conds:
        in_range = "r.memory_id = memories.id AND r.deleted_at IS NULL AND " + " AND ".join(ring_conds)
        conds.append(f"EXISTS (SELECT 1 FROM memory_rings r WHERE {in_range})")
        params += ring_params
        order = f"(SELECT MAX(r.date) FROM memory_rings r WHERE {in_range}) DESC, id DESC"
        order_params = ring_params
    where = "WHERE " + " AND ".join(conds)
    page = max(page, 1)
    page_size = min(max(page_size, 1), BROWSE_PAGE_MAX)
    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM memories {where}", params).fetchone()[0]
        live_total = conn.execute(f"SELECT COUNT(*) FROM memories WHERE {LIVE}").fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM memories {where} ORDER BY {order} LIMIT ? OFFSET ?",
            params + order_params + [page_size, (page - 1) * page_size],
        ).fetchall()
        ring_map = rings.live_many(conn, [r["id"] for r in rows])
        with_history = {
            r["memory_id"]: r["n"]
            for r in conn.execute(
                "SELECT memory_id, COUNT(*) n FROM memory_history GROUP BY memory_id"
            ).fetchall()
        }
        # 月份按钮看全库的圈（有过一圈的月都算）；第一页可能全是同一个月，不能从分页条目里推断。
        months = set()
        for r in conn.execute(
            """SELECT DISTINCT r.date FROM memory_rings r JOIN memories m ON m.id = r.memory_id
               WHERE r.deleted_at IS NULL AND m.deleted_at IS NULL"""
        ):
            try:
                months.add(normalize_date(r["date"])[:7])
            except ValueError:
                pass  # 老数据中无法辨认的日期仍可在「全部」查看。
    items = []
    for r in rows:
        item = dict(r)
        item["drawer"] = drawer_of(r["space"])
        item["rings"] = ring_map[r["id"]]
        item["interval_status"] = interval_status(r["start_date"], r["end_date"])
        item["history_count"] = with_history.get(r["id"], 0)
        items.append(item)
    return {
        "stats": {"total": total, "all": live_total},
        "months": sorted(months, reverse=True)[:4],  # 最近三个有圈的月 + 是否还有更早。
        "page": page,
        "page_size": page_size,
        "total_pages": max((total + page_size - 1) // page_size, 1),
        "items": items,
    }


def list_tags() -> list[dict]:
    """库里用过的全部标签和各自条数（网页编辑时挑标签用），多的在前。"""
    counts: dict[str, int] = {}
    with get_conn() as conn:
        for r in conn.execute(f"SELECT tags FROM memories WHERE {LIVE}"):
            for t in normalize_tags(r["tags"] or "").split(","):
                if t:
                    counts[t] = counts.get(t, 0) + 1
    return [{"tag": t, "count": n} for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _touch(c, memory_id: int) -> None:
    c.execute(
        f"""UPDATE memories SET updated_at = CASE
              WHEN updated_at >= {NOW_MS}
              THEN strftime('%Y-%m-%d %H:%M:%f', updated_at, '+0.001 seconds')
              ELSE {NOW_MS} END
            WHERE id = ?""",
        (memory_id,),
    )


def _apply(c, row, fields: dict) -> None:
    """写入新值、记修改时间；content/topic/tags 变了就重算语义指纹。指纹算不出来时
    删掉旧指纹——留着它语义检索会按改前的意思召回这条，而 rebuild 只补缺席/换模型的行，
    补不回"同模型但过时"的；删了就回到缺指纹状态，关键词照常、rebuild 能补。"""
    sets = ", ".join(f"{k} = ?" for k in fields)
    c.execute(
        f"UPDATE memories SET {sets} WHERE id = ?",
        [*fields.values(), row["id"]],
    )
    _touch(c, row["id"])
    if "content" in fields or "date" in fields:
        rings.follow_card(c, row["id"])
    if any(k in fields and fields[k] != row[k] for k in ("content", "topic", "tags")):
        merged = {**dict(row), **fields}
        if not embeddings.embed_memory(c, row["id"], merged["content"], topic=merged["topic"], tags=merged["tags"]):
            c.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (row["id"],))


_UNSET = object()


def _stored_norm(key: str, value):
    """库里的旧值按同样规矩规整后再比：老代码存的原样（首尾空白、日期没补零、标签带空格、NULL）不算「改了」。"""
    try:
        return _clean_fields({key: value}).get(key, value)
    except ValueError:
        return value


def update_memory(
    memory_id: int, edits: dict, by: str = "xuan", conn=None, expect_updated_at=_UNSET
) -> dict | None:
    """直接改这一份，立刻生效；改前的整条挤进上一版（只在真有变化时）。

    expect_updated_at：网页打开这条时看到的 updated_at；锁内重读对不上 = 打开后又被改过，
    抛 Conflict 不覆盖（网页的保存不会悄悄冲掉小克刚改的，反之小克的改动本来就进了上一版）。
    返回 {"id", "changed": [...]} + 改后的完整记忆；没变化 changed 为空。不存在 / 已删返回 None。
    """
    fields = _clean_fields(edits)
    if not fields:
        raise ValueError(f"没有可改的字段（可改：{'/'.join(MEMORY_EDITABLE)}）")
    with _write_tx(conn) as c:
        row = c.execute(f"SELECT * FROM memories WHERE id = ? AND {LIVE}", (memory_id,)).fetchone()
        if row is None:
            return None
        if expect_updated_at is not _UNSET and (expect_updated_at or None) != row["updated_at"]:
            raise Conflict
        rewrite = {k: v for k, v in fields.items() if v != row[k]}
        changed = {k: v for k, v in rewrite.items() if v != _stored_norm(k, row[k])}
        if "content" in changed or "date" in changed:
            n = len(rings.live(c, memory_id))
            if n > 1:  # 好几圈的卡 content 是按圈拼出来的，整条改了圈就对不上了
                raise ValueError(f"这张卡有 {n} 圈了，内容和日子要一圈一圈改（补一圈 / 改某一圈）")
        if changed:
            hid = history.push(c, row, by, "edit")
            _apply(c, row, rewrite)
            activity.log(c, by, "edit", memory_id, changed.get("content", row["content"]), history_id=hid)
        elif rewrite:  # 只是把老数据存成规整的样子（日期补零、标签去空格）：顺手写回，不算一次改
            sets = ", ".join(f"{k} = ?" for k in rewrite)
            c.execute(f"UPDATE memories SET {sets} WHERE id = ?", [*rewrite.values(), memory_id])
            if "content" in rewrite or "date" in rewrite:
                rings.follow_card(c, memory_id)
    if conn is not None:
        return {"id": memory_id, "changed": list(changed)}
    return {"id": memory_id, "changed": list(changed), "memory": get_memory(memory_id)}


def revert_memory(memory_id: int, history_id: int, by: str = "xuan", expect_updated_at=_UNSET) -> dict | None:
    """退回到留着的某一版（留 2 版时能选退到哪一版）。回退也算一次改：
    现在的内容挤进上一版，退回去的那一版从上一版里拿走。圈也回到那一版的样子（rings.restore），
    卡的 content / date 再按圈重算。版本不存在 / 记忆已删返回 None。"""
    with _write_tx() as c:
        row = c.execute(f"SELECT * FROM memories WHERE id = ? AND {LIVE}", (memory_id,)).fetchone()
        target = history.get(c, memory_id, history_id) if row is not None else None
        if target is None:
            return None
        if expect_updated_at is not _UNSET and (expect_updated_at or None) != row["updated_at"]:
            raise Conflict  # 页面打开后又被改过：退回去会连那次改动一起抹掉，先刷新看一眼
        snap_all = json.loads(target["snapshot"])
        snap = {k: v for k, v in snap_all.items() if k in MEMORY_EDITABLE}
        snap_rings = rings.from_snapshot(c, memory_id, snap_all)
        c.execute("DELETE FROM memory_history WHERE id = ?", (history_id,))
        activity.mark_undone(c, memory_id, history_id=history_id)
        hid = history.push(c, row, by, "revert")  # 先存现在的（连圈），再动圈
        if snap_rings:
            rings.restore(c, memory_id, snap_rings)
            snap |= rings.card_fields(c, memory_id)
        changed = {k: v for k, v in snap.items() if v != row[k]}
        if changed:
            _apply(c, row, changed)
        else:  # 内容一样也记修改时间：网页的「打开后又被改过」要能看出来
            _touch(c, memory_id)
        activity.log(c, by, "revert", memory_id, snap.get("content", row["content"]), history_id=hid)
    return {"id": memory_id, "reverted_to": history_id, "memory": get_memory(memory_id)}


# ---------- 年轮：补一圈 / 改某一圈 / 删某一圈（网页 PR4；MCP 工具 PR3 也走这里） ----------


def _ring_input(date, content, empty_msg: str) -> tuple[str, str]:
    """这一圈的日子和内容：日子写具体的日子或只到月，不能写以后的（打算哪天做写进内容里）；内容不能空。"""
    if not isinstance(date, str) or not isinstance(content, str):
        raise ValueError("这一圈的日子和内容要是文字")
    content = content.strip()
    if not content:
        raise ValueError(empty_msg)
    date = normalize_date(date)
    today = _today()
    if date > (today if len(date) == 10 else today[:7]):
        raise ValueError("不能写以后的日子；打算哪天做，写进内容里")
    return date, content


def _live_row(c, memory_id: int, expect_updated_at):
    row = c.execute(f"SELECT * FROM memories WHERE id = ? AND {LIVE}", (memory_id,)).fetchone()
    if row is not None and expect_updated_at is not _UNSET and (expect_updated_at or None) != row["updated_at"]:
        raise Conflict
    return row


def _follow_rings(c, row) -> None:
    """圈变了：卡的 content / date 按圈重算（content 变了顺手重算语义指纹），修改时间往前走。"""
    fields = {k: v for k, v in rings.card_fields(c, row["id"]).items() if v != row[k]}
    if fields:
        _apply(c, row, fields)
    else:
        _touch(c, row["id"])


def add_ring(memory_id: int, date, content, by: str = "xuan", expect_updated_at=_UNSET) -> dict | None:
    """补一圈：同一件事有了新进展。改前的整张卡（连圈）进上一版，最近动静记一行「改了」，能回退。
    返回 {"id", "ring_id", "memory"}；卡不存在 / 已删返回 None。"""
    date, content = _ring_input(date, content, "这一圈写点什么再存")
    with _write_tx() as c:
        row = _live_row(c, memory_id, expect_updated_at)
        if row is None:
            return None
        hid = history.push(c, row, by, "edit")
        ring_id = rings.add(c, memory_id, date, content)
        _follow_rings(c, row)
        activity.log(c, by, "edit", memory_id, row["title"] or content, history_id=hid)
    return {"id": memory_id, "ring_id": ring_id, "memory": get_memory(memory_id)}


def edit_ring(memory_id: int, ring_id: int, date, content, by: str = "xuan", expect_updated_at=_UNSET) -> dict | None:
    """改某一圈的日子 / 内容，跟补一圈一样进上一版、记动静。没变化就什么都不记（changed=False）。
    卡或这一圈不在了返回 None。"""
    date, content = _ring_input(date, content, "这一圈不能改成空的；不要了就删掉它")
    with _write_tx() as c:
        row = _live_row(c, memory_id, expect_updated_at)
        ring = rings.find(c, memory_id, ring_id) if row is not None else None
        if ring is None:
            return None
        changed = (ring["date"], ring["content"]) != (date, content)
        if changed:
            hid = history.push(c, row, by, "edit")
            rings.change(c, ring_id, date, content)
            _follow_rings(c, row)
            activity.log(c, by, "edit", memory_id, row["title"] or content, history_id=hid)
    return {"id": memory_id, "ring_id": ring_id, "changed": changed, "memory": get_memory(memory_id)}


def delete_ring(memory_id: int, ring_id: int, by: str = "xuan", expect_updated_at=_UNSET) -> dict | None:
    """删某一圈：先藏起来，进上一版、记动静，回退能找回来（返回的 history_id 就是网页提示条上「撤回」退回去的那一版）。
    最后一圈不让单独删（那等于删整张卡）。卡或这一圈不在了返回 None。"""
    with _write_tx() as c:
        row = _live_row(c, memory_id, expect_updated_at)
        ring = rings.find(c, memory_id, ring_id) if row is not None else None
        if ring is None:
            return None
        if len(rings.live(c, memory_id)) <= 1:
            raise ValueError("这是最后一圈了，要删就删整张卡")
        hid = history.push(c, row, by, "edit")
        rings.drop(c, ring_id)
        _follow_rings(c, row)
        activity.log(c, by, "edit", memory_id, row["title"] or ring["content"], history_id=hid)
    return {"id": memory_id, "ring_id": ring_id, "deleted": True, "history_id": hid, "memory": get_memory(memory_id)}


def set_star(memory_id: int, starred, by: str = "xuan") -> dict | None:
    """点⭐ / 取消⭐：像书签，不进上一版（不挤掉真正的上一版）、不记动静、不动修改时间
    （正开着的编辑不会因为点了颗星就撞上「刚被改过」）。小克点要看设置「小克能不能点⭐」。
    不存在 / 已删返回 None。"""
    if not isinstance(starred, bool):
        raise ValueError("starred 要是 true / false")
    with _write_tx() as c:
        if by != "xuan" and not settings.get_settings(c)["ke_can_star"]:
            raise ValueError("轩关掉了「小克能不能点⭐」，这次没点上")
        cur = c.execute(f"UPDATE memories SET starred = ? WHERE id = ? AND {LIVE}", (int(starred), memory_id))
        if not cur.rowcount:
            return None
    return {"id": memory_id, "starred": starred}


def _resync_superseded(c, memory_id: int) -> None:
    """这条被删 / 恢复 / 真删后，重算它压过（supersedes）的旧记忆的 superseded_by：
    取还活着的、最新的那个压它的记忆；没有了就松开。藏起来的记忆不该继续压着别人。"""
    targets = {
        r[0] for r in c.execute(
            "SELECT to_id FROM memory_edges WHERE from_id = ? AND relation = 'supersedes'", (memory_id,)
        )
    } | {r[0] for r in c.execute("SELECT id FROM memories WHERE superseded_by = ?", (memory_id,))}
    for target in targets:
        _recompute_superseded(c, target)


def _recompute_superseded(c, target: int) -> None:
    c.execute(
        """UPDATE memories SET superseded_by = (
             SELECT MAX(e.from_id) FROM memory_edges e JOIN memories m ON m.id = e.from_id
             WHERE e.to_id = ? AND e.relation = 'supersedes' AND m.deleted_at IS NULL)
           WHERE id = ?""",
        (target, target),
    )


def delete_memory(memory_id: int, by: str = "xuan") -> dict | None:
    """删：先藏起来，所有读和搜都看不见；最近动静里能恢复，过了显示期限才真删。不存在 / 已删返回 None。"""
    with _write_tx() as c:
        row = c.execute(f"SELECT * FROM memories WHERE id = ? AND {LIVE}", (memory_id,)).fetchone()
        if row is None:
            return None
        c.execute(
            "UPDATE memories SET deleted_at = datetime('now','+8 hours'), deleted_by = ? WHERE id = ?",
            (by, memory_id),
        )
        _resync_superseded(c, memory_id)
        activity.log(c, by, "delete", memory_id, row["content"])
        days = settings.get_settings(c)["activity_days"]
    return {"id": memory_id, "deleted": True, "restorable_days": days}


def restore_memory(memory_id: int, by: str = "xuan") -> dict | None:
    """把删掉的找回来（还没过恢复期限的才行）。不是已删状态返回 None。"""
    purge_expired()
    with _write_tx() as c:
        row = c.execute(
            "SELECT * FROM memories WHERE id = ? AND deleted_at IS NOT NULL", (memory_id,)
        ).fetchone()
        if row is None:
            return None
        c.execute("UPDATE memories SET deleted_at = NULL, deleted_by = NULL WHERE id = ?", (memory_id,))
        _resync_superseded(c, memory_id)
        activity.mark_undone(c, memory_id, action="delete")
        activity.log(c, by, "restore", memory_id, row["content"])
    return {"id": memory_id, "restored": True, "memory": get_memory(memory_id)}


def purge_expired(wait: bool = True) -> dict:
    """过了最近动静显示期限的：删掉的记忆真删（连同来源、连线、指纹、上一版），流水也清掉。
    被它压过（superseded_by）的旧记忆按剩下的活记忆重算压制。没有定时任务：网页列表 / 启动时顺手调。"""
    probe = get_conn()  # 先只读看一眼有没有要清的：大多数时候没有，不去抢写锁
    try:
        cutoff = f"-{settings.get_settings(probe)['activity_days']} days"
        due = probe.execute(
            "SELECT EXISTS(SELECT 1 FROM memories WHERE deleted_at IS NOT NULL "
            "AND deleted_at < datetime('now', '+8 hours', ?)) "
            "OR EXISTS(SELECT 1 FROM activity_log WHERE at < datetime('now', '+8 hours', ?))",
            (cutoff, cutoff),
        ).fetchone()[0]
    finally:
        probe.close()
    if not due:
        return {"purged": [], "activity_pruned": 0}
    with _write_tx(wait=wait) as c:
        cutoff = f"-{settings.get_settings(c)['activity_days']} days"
        ids = [
            r["id"] for r in c.execute(
                "SELECT id FROM memories WHERE deleted_at IS NOT NULL "
                "AND deleted_at < datetime('now', '+8 hours', ?)",
                (cutoff,),
            ).fetchall()
        ]
        for mid in ids:
            c.execute("DELETE FROM memory_sources WHERE memory_id = ?", (mid,))
            superseded = [r[0] for r in c.execute("SELECT id FROM memories WHERE superseded_by = ?", (mid,))]
            c.execute("UPDATE memories SET superseded_by = NULL WHERE superseded_by = ?", (mid,))
            c.execute("DELETE FROM memory_edges WHERE from_id = ? OR to_id = ?", (mid, mid))
            c.execute("DELETE FROM memories WHERE id = ?", (mid,))
            for target in superseded:  # 还有别的活记忆压着它的，压制要留着
                _recompute_superseded(c, target)  # 指纹 / 上一版 / 浮现冷却级联删
        logs = c.execute(
            "DELETE FROM activity_log WHERE at < datetime('now', '+8 hours', ?)", (cutoff,)
        ).rowcount
    return {"purged": ids, "activity_pruned": logs}


def get_status() -> dict:
    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM memories WHERE {LIVE}").fetchone()[0]
        last = conn.execute(f"SELECT MAX(created_at) FROM memories WHERE {LIVE}").fetchone()[0]
        edges = conn.execute(
            """SELECT COUNT(*) FROM memory_edges e
               JOIN memories a ON a.id = e.from_id JOIN memories b ON b.id = e.to_id
               WHERE a.deleted_at IS NULL AND b.deleted_at IS NULL"""
        ).fetchone()[0]
        sources = conn.execute(
            "SELECT COUNT(*) FROM memory_sources s JOIN memories m ON m.id = s.memory_id WHERE m.deleted_at IS NULL"
        ).fetchone()[0]
    return {
        "service": "ember",
        "status": "ok",
        "memories": total,
        "edges": edges,
        "sources": sources,
        "last_write": last,
    }
