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

from app import activity, embeddings, history, settings
from app.db import get_conn

SUMMARY_LEN = 120  # search/list 返回的目录条目里 content 的截断长度

RRF_K = 60      # RRF 融合常数（业界惯用值）：越大头部排名的权重差越平缓
VEC_TOP_K = 40  # 语义腿取多少候选进融合

RELATIONS = ("led_to", "same_as", "contradicts", "supersedes", "related")

# 网页 / 小克能改的字段白名单——is_resolved/superseded_by 等状态列不开放手改
MEMORY_EDITABLE = history.SNAPSHOT_FIELDS
TIERS = ("anchor", "normal", "process")
LIVE = "deleted_at IS NULL"  # 删了先藏起来：所有读和搜都带上这一句
BROWSE_PAGE_MAX = 200

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


def normalize_date(value, *, allow_year: bool = False) -> str:
    """发生日期：YYYY-MM-DD 或只到月 YYYY-MM（. / 也认，月日补零），得是日历上真有的日子。
    allow_year=True 时还认只有年份（按日期范围筛选用）。"""
    text = str(value or "").strip().replace("/", "-").replace(".", "-")
    m = re.fullmatch(r"(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?", text)
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


def _next_period(prefix: str) -> str:
    """'2026' → '2027'；'2026-12' → '2027-01'：给"到某年 / 某月为止"做开区间上界。"""
    if len(prefix) == 4:
        return f"{int(prefix) + 1:04d}"
    year, month = int(prefix[:4]), int(prefix[5:7])
    return f"{year + month // 12:04d}-{month % 12 + 1:02d}"


def date_conditions(date_from: str | None, date_to: str | None) -> tuple[list[str], list]:
    """按发生日期筛：两头都能只写到年或月——date_from="2026-08", date_to="2026-08" = 8 月发生的。
    只到月的记忆（'2026-08'）算在那个月里。"""
    conds, params = [], []
    if date_from:
        conds.append("date >= ?")
        params.append(normalize_date(date_from, allow_year=True))
    if date_to:
        to = normalize_date(date_to, allow_year=True)
        if len(to) == 10:
            conds.append("date <= ?")
            params.append(to)
        else:
            conds.append("date < ?")
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
    if start_date and today < start_date:
        return "upcoming"
    if end_date and today > end_date:
        return "ended"
    return "ongoing"


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
def _write_tx(conn=None):
    """写事务：给了 conn 就借调用方的事务（不提交）；没给就自开连接、先拿写锁，锁内重读才作数。"""
    if conn is not None:
        yield conn
        return
    c = get_conn()
    try:
        c.execute("BEGIN IMMEDIATE")
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
        if not isinstance(target, int) or isinstance(target, bool) or target <= 0:
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
                conn.execute("UPDATE memories SET superseded_by = ? WHERE id = ?", (from_id, to_id))
    return written


def _clean_fields(edits: dict) -> dict:
    """白名单 + 规整 + 校验：日期得是具体日子或月份，内容不能空，tags 规整，区间空串 = 清掉。"""
    fields = {k: edits[k] for k in MEMORY_EDITABLE if k in edits}
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
) -> dict:
    """存一条，直接进库、马上能搜到。内容和发生日期缺一样存不进去（ValueError）。
    conn 给定时借用调用方的连接和事务（不提交，commit 归调用方管）——批量存用它保证整批原子。"""
    fields = _clean_fields({
        "date": date, "content": content, "tags": tags or "", "tier": tier or "normal",
        "topic": topic or "", "space": space or "personal",
        "start_date": start_date, "end_date": end_date,
    })
    links = _validate_links(links)  # 先校验后落库，坏 links 不留半条记忆

    with _write_tx(conn) as c:
        cur = c.execute(
            """INSERT INTO memories (date, content, tags, tier, topic, space, start_date, end_date)
               VALUES (:date, :content, :tags, :tier, :topic, :space, :start_date, :end_date)""",
            fields,
        )
        memory_id = cur.lastrowid
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
    """一条记忆的全文 + 来源 + 关系边。删了的（藏起来的）默认当不存在。"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None or (row["deleted_at"] and not include_deleted):
            return None
        memory = dict(row)
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
    vec_ids = embeddings.vector_search(query, space=space, k=VEC_TOP_K)
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
) -> dict:
    """网页"记忆库"：默认跨全库（管理视角，与检索的 personal 默认不同），content 不截断。
    q 是关键词筛选（content/tags/topic 子串）；q 写成 "#123" 或纯数字时也按编号找。
    tag 只看带这个标签的。按发生日期新的在前，同一天编号大的在前。"""
    conds, params = [LIVE], []
    if space and space != "all":
        conds.append("space = ?")
        params.append(space)
    if q and q.strip():
        q = q.strip()
        number = q.lstrip("#")
        like_cond = "(content LIKE ? OR tags LIKE ? OR topic LIKE ?)"
        if number.isdigit():
            conds.append(f"(id = ? OR {like_cond})")
            params += [int(number)] + [f"%{q}%"] * 3
        else:
            conds.append(like_cond)
            params += [f"%{q}%"] * 3
    if tag:
        conds.append("(',' || tags || ',') LIKE ?")
        params.append(f"%,{tag.strip()},%")
    date_conds, date_params = date_conditions(date_from, date_to)
    conds += date_conds
    params += date_params
    where = "WHERE " + " AND ".join(conds)
    page = max(page, 1)
    page_size = min(max(page_size, 1), BROWSE_PAGE_MAX)
    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM memories {where}", params).fetchone()[0]
        live_total = conn.execute(f"SELECT COUNT(*) FROM memories WHERE {LIVE}").fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM memories {where} ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
            params + [page_size, (page - 1) * page_size],
        ).fetchall()
        with_history = {
            r["memory_id"]: r["n"]
            for r in conn.execute(
                "SELECT memory_id, COUNT(*) n FROM memory_history GROUP BY memory_id"
            ).fetchall()
        }
    items = []
    for r in rows:
        item = dict(r)
        item["interval_status"] = interval_status(r["start_date"], r["end_date"])
        item["history_count"] = with_history.get(r["id"], 0)
        items.append(item)
    return {
        "stats": {"total": total, "all": live_total},
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


def _apply(c, row, fields: dict) -> None:
    """写入新值、记修改时间；content/topic/tags 变了就重算语义指纹。指纹算不出来时
    删掉旧指纹——留着它语义检索会按改前的意思召回这条，而 rebuild 只补缺席/换模型的行，
    补不回"同模型但过时"的；删了就回到缺指纹状态，关键词照常、rebuild 能补。"""
    sets = ", ".join(f"{k} = ?" for k in fields)
    c.execute(
        f"UPDATE memories SET {sets}, updated_at = datetime('now','+8 hours') WHERE id = ?",
        [*fields.values(), row["id"]],
    )
    if any(k in fields and fields[k] != row[k] for k in ("content", "topic", "tags")):
        merged = {**dict(row), **fields}
        if not embeddings.embed_memory(c, row["id"], merged["content"], topic=merged["topic"], tags=merged["tags"]):
            c.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (row["id"],))


_UNSET = object()


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
        changed = {k: v for k, v in fields.items() if v != row[k]}
        if changed:
            hid = history.push(c, row, by, "edit")
            _apply(c, row, changed)
            activity.log(c, by, "edit", memory_id, changed.get("content", row["content"]), history_id=hid)
    if conn is not None:
        return {"id": memory_id, "changed": list(changed)}
    return {"id": memory_id, "changed": list(changed), "memory": get_memory(memory_id)}


def revert_memory(memory_id: int, history_id: int, by: str = "xuan") -> dict | None:
    """退回到留着的某一版（留 2 版时能选退到哪一版）。回退也算一次改：
    现在的内容挤进上一版，退回去的那一版从上一版里拿走。版本不存在 / 记忆已删返回 None。"""
    with _write_tx() as c:
        row = c.execute(f"SELECT * FROM memories WHERE id = ? AND {LIVE}", (memory_id,)).fetchone()
        target = history.get(c, memory_id, history_id) if row is not None else None
        if target is None:
            return None
        snap = {k: v for k, v in json.loads(target["snapshot"]).items() if k in MEMORY_EDITABLE}
        c.execute("DELETE FROM memory_history WHERE id = ?", (history_id,))
        activity.mark_undone(c, memory_id, history_id=history_id)
        hid = history.push(c, row, by, "revert")
        changed = {k: v for k, v in snap.items() if v != row[k]}
        if changed:
            _apply(c, row, changed)
        else:  # 内容一样也记修改时间：网页的「打开后又被改过」要能看出来
            c.execute("UPDATE memories SET updated_at = datetime('now','+8 hours') WHERE id = ?", (memory_id,))
        activity.log(c, by, "revert", memory_id, snap.get("content", row["content"]), history_id=hid)
    return {"id": memory_id, "reverted_to": history_id, "memory": get_memory(memory_id)}


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


def purge_expired() -> dict:
    """过了最近动静显示期限的：删掉的记忆真删（连同来源、连线、指纹、上一版），流水也清掉。
    被它压过（superseded_by）的旧记忆按剩下的活记忆重算压制。没有定时任务：网页列表 / 启动时顺手调。"""
    with _write_tx() as c:
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
        edges = conn.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0]
        sources = conn.execute("SELECT COUNT(*) FROM memory_sources").fetchone()[0]
    return {
        "service": "ember",
        "status": "ok",
        "memories": total,
        "edges": edges,
        "sources": sources,
        "last_write": last,
    }
