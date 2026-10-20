"""年轮卡（2026-10 记忆库第二轮，方案：docs/产出/261020-记忆库第二轮年轮卡方案.md）。

一件事一张卡：卡 = memories 一行（标题 title、⭐ starred、便利贴 tags、抽屉 space），
下面一圈一圈是年轮 = memory_rings（哪天 date、发生了什么 content）。卡片日期 = 最新一圈的日期。

PR1 只搭结构、搬家：存的时候建第一圈（start_card），只剩一圈的卡改 / 回退时这一圈跟着卡的
content / date 走（follow_card）。
PR4（网页）起能补圈 / 改圈 / 删圈：圈是正本，卡的 content 跟着圈重算（joined），给还没改成按圈读的
老地方用（MCP 搜索 / 回忆 / 开场、语义指纹、上一版）；好几圈的卡不能再按整条改 content / date。
上一版连圈一起存（snapshot_rings），回退时圈回到那一版的样子（restore）。
都在调用方的写事务里做，不自己开事务。
"""

from collections import Counter

from app import settings

NOW = "datetime('now','+8 hours')"


def add_first(c, memory_id: int) -> int:
    """给还没有年轮的卡建第一圈：content 原样抄、日期照旧，存 / 改的时间沿用卡的。"""
    cur = c.execute(
        f"""INSERT INTO memory_rings (memory_id, date, content, created_at, updated_at)
            SELECT id, date, content, COALESCE(created_at, {NOW}), updated_at FROM memories WHERE id = ?""",
        (memory_id,),
    )
    return cur.lastrowid


def follow_card(c, memory_id: int) -> bool:
    """卡的 content / date 改了（改、回退）：只剩一圈的卡，让这一圈跟上。返回这一圈改没改。
    好几圈的卡不动（圈才是正本，update_memory 也不让按整条改）；删掉的圈不算；
    还没有圈的也不动，留给启动时的搬家连标题一起搬。"""
    rings = live(c, memory_id)
    if len(rings) != 1:
        return False
    ring = rings[0]
    card = c.execute("SELECT date, content FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if (ring["date"], ring["content"]) == (card["date"], card["content"]):
        return False
    c.execute(
        f"UPDATE memory_rings SET date = ?, content = ?, updated_at = {NOW} WHERE id = ?",
        (card["date"], card["content"], ring["id"]),
    )
    sync_card_date(c, memory_id)
    return True


def _date_key(value: str) -> str:
    from app.memories import normalize_date  # memories 引用本模块，延迟导入免循环

    try:
        return normalize_date(value)
    except ValueError:
        return value  # 老数据里认不出的格式照原样比


def _order(row) -> tuple:
    return (_date_key(row["date"]), row["id"])


def live(c, memory_id: int) -> list[dict]:
    """这张卡还在的圈，从早到晚：只到月的 2026-10 排在 10 月所有具体日子前面，同一天先补的在前。"""
    rows = c.execute(
        """SELECT id, date, content, created_at, updated_at FROM memory_rings
           WHERE memory_id = ? AND deleted_at IS NULL""",
        (memory_id,),
    ).fetchall()
    return sorted((dict(r) for r in rows), key=_order)


def live_many(c, memory_ids: list[int]) -> dict[int, list[dict]]:
    """好几张卡一起拿（网页列表一页一次查完），每张都是从早到晚。"""
    out: dict[int, list[dict]] = {i: [] for i in memory_ids}
    if not memory_ids:
        return out
    marks = ",".join("?" * len(memory_ids))
    for r in c.execute(
        f"""SELECT memory_id, id, date, content, created_at, updated_at FROM memory_rings
            WHERE deleted_at IS NULL AND memory_id IN ({marks})""",
        memory_ids,
    ).fetchall():
        row = dict(r)
        out[row.pop("memory_id")].append(row)
    for rows in out.values():
        rows.sort(key=_order)
    return out


def joined(rows: list[dict]) -> str:
    """卡的 content：只有一圈就是这一圈原文；好几圈按从早到晚，每圈前面写上日子，空一行隔开。"""
    if len(rows) == 1:
        return rows[0]["content"]
    return "\n\n".join(f"{r['date']}：{r['content']}" for r in rows)


def card_fields(c, memory_id: int) -> dict:
    """按圈重算卡的 content / date（date = 最新一圈）。一圈都不剩时返回空，卡不动。"""
    rows = live(c, memory_id)
    if not rows:
        return {}
    return {"content": joined(rows), "date": rows[-1]["date"]}


def add(c, memory_id: int, date: str, content: str) -> int:
    cur = c.execute(
        "INSERT INTO memory_rings (memory_id, date, content) VALUES (?, ?, ?)", (memory_id, date, content)
    )
    return cur.lastrowid


def find(c, memory_id: int, ring_id: int):
    """这张卡上还在的这一圈；不是这张卡的、删掉了的都当不存在。"""
    return c.execute(
        "SELECT * FROM memory_rings WHERE id = ? AND memory_id = ? AND deleted_at IS NULL", (ring_id, memory_id)
    ).fetchone()


def change(c, ring_id: int, date: str, content: str) -> None:
    c.execute(
        f"UPDATE memory_rings SET date = ?, content = ?, updated_at = {NOW} WHERE id = ?", (date, content, ring_id)
    )


def drop(c, ring_id: int) -> None:
    """删一圈：先藏起来（deleted_at），回退能找回来；卡真删时跟着级联删。"""
    c.execute(f"UPDATE memory_rings SET deleted_at = {NOW}, updated_at = {NOW} WHERE id = ?", (ring_id,))


def snapshot_rings(c, memory_id: int) -> list[dict]:
    """存进上一版的圈：还在的那些，编号 + 日子 + 内容。"""
    return [{"id": r["id"], "date": r["date"], "content": r["content"]} for r in live(c, memory_id)]


def from_snapshot(c, memory_id: int, snap: dict) -> list[dict] | None:
    """上一版里的圈。PR4 之前存的上一版没有圈：那时候每张卡只有一圈（最早建的那圈），
    内容 / 日子就是那一版的 content / date。卡上一圈都没有时返回 None（不动圈）。"""
    if "rings" in snap:
        return snap["rings"]
    first = c.execute("SELECT MIN(id) FROM memory_rings WHERE memory_id = ?", (memory_id,)).fetchone()[0]
    if first is None:
        return None
    return [{"id": first, "date": snap["date"], "content": snap["content"]}]


def restore(c, memory_id: int, snap_rings: list[dict]) -> None:
    """回退：圈回到那一版的样子——那一版里有的圈照它的日子、内容，删了的找回来；
    那一版之后补的圈藏起来（再回退一次又能找回来）。"""
    want = {r["id"]: r for r in snap_rings}
    for r in c.execute(
        "SELECT id, date, content, deleted_at FROM memory_rings WHERE memory_id = ?", (memory_id,)
    ).fetchall():
        s = want.pop(r["id"], None)
        if s is None:
            if not r["deleted_at"]:
                drop(c, r["id"])
        elif (r["date"], r["content"], r["deleted_at"]) != (s["date"], s["content"], None):
            c.execute(
                f"UPDATE memory_rings SET date = ?, content = ?, deleted_at = NULL, updated_at = {NOW} WHERE id = ?",
                (s["date"], s["content"], r["id"]),
            )
    for s in want.values():  # 圈不会单独真删，这里只是兜底：找不到原来那行就照内容补一圈，不丢字
        add(c, memory_id, s["date"], s["content"])


def sync_card_date(c, memory_id: int) -> str | None:
    """卡片日期 = 最新一圈的日期（删掉的圈不算）。补圈 / 改圈 / 删圈之后都调这里，返回卡片现在的日期。
    跟网页列表同一个比法：只到月的 2026-10 排在 10 月所有具体日子前面，同一天的看后补的那圈；
    老数据里没补零的（2026-8-5）补零再比。一圈都不剩时不动。"""
    rows = c.execute(
        "SELECT id, date FROM memory_rings WHERE memory_id = ? AND deleted_at IS NULL", (memory_id,)
    ).fetchall()
    if not rows:
        return None
    latest = max(rows, key=_order)["date"]
    c.execute("UPDATE memories SET date = ? WHERE id = ? AND date IS NOT ?", (latest, memory_id, latest))
    return latest


def topic_counts(c) -> Counter:
    """每个 topic 有几张还在的卡在用（删掉的藏起来了，轩看不见，不算）。"""
    counts: Counter = Counter()
    for r in c.execute("SELECT topic FROM memories WHERE deleted_at IS NULL"):
        topic = (r["topic"] or "").strip()
        if topic:
            counts[topic] += 1
    return counts


def from_legacy(c, row, counts: Counter, can_star: bool = True) -> dict:
    """老栏位换成新栏位（轩 2026-10-20 定；搬家和 PR3 之前照老参数存的新卡都按这一套）：
    - topic 只有这一张卡在用 → 变成标题（已有标题不动）；好几张共用 → 并进便利贴（已有就不加）；空的不管。
      施工时补的细则（#24）：只数还在的卡；删掉的卡按「恢复回来」算，它自己也算一张。
    - tier = anchor → ⭐（can_star=False 时不点：小克存的，轩又关了「小克能不能点⭐」）。
    topic、tier 两栏原样留着（PR5 才删）。返回这张卡改了什么。"""
    from app.memories import normalize_tags  # 同上，延迟导入

    done: dict = {}
    topic = (row["topic"] or "").strip()
    if topic:
        users = counts[topic] + (1 if row["deleted_at"] else 0)
        if users == 1:
            if not row["title"]:
                c.execute("UPDATE memories SET title = ? WHERE id = ?", (topic, row["id"]))
                done["title"] = topic
        else:
            tags = [t for t in normalize_tags(row["tags"] or "").split(",") if t]
            extra = [t for t in dict.fromkeys(normalize_tags(topic).split(",")) if t and t not in tags]
            if extra:
                c.execute("UPDATE memories SET tags = ? WHERE id = ?", (",".join(tags + extra), row["id"]))
                done["tags"] = extra
    if row["tier"] == "anchor" and not row["starred"] and can_star:
        c.execute("UPDATE memories SET starred = 1 WHERE id = ?", (row["id"],))
        done["starred"] = True
    return done


def start_card(c, memory_id: int, by: str) -> dict:
    """刚存进来的卡：建第一圈；PR3 之前工具还收 topic / tier，按搬家同一套规矩换成标题 / 便利贴 / ⭐。"""
    add_first(c, memory_id)
    row = c.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    can_star = by == "xuan" or settings.get_settings(c)["ke_can_star"]
    return from_legacy(c, row, topic_counts(c), can_star)
