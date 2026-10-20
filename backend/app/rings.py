"""年轮卡（2026-10 记忆库第二轮，方案：docs/产出/261020-记忆库第二轮年轮卡方案.md）。

一件事一张卡：卡 = memories 一行（标题 title、⭐ starred、便利贴 tags、抽屉 space），
下面一圈一圈是年轮 = memory_rings（哪天 date、发生了什么 content）。卡片日期 = 最新一圈的日期。

PR1 只搭结构、搬家：工具和网页还是按整条 content 存、改，所以每张卡先只有一圈——
存的时候建第一圈（start_card），改 / 回退时这一圈跟着卡的 content / date 走（follow_card）。
补圈 / 改圈 / 删圈在 PR3；那以后圈才是正本，follow_card 只管还只有一圈的卡，要跟 PR3 一起重看。
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
    """卡的 content / date 改了（改、回退）：只有一圈的卡，让这一圈跟上。返回这一圈改没改。
    有好几圈或删过圈的卡不动（那时候圈才是正本）；还没有圈的也不动，留给启动时的搬家连标题一起搬。"""
    rings = c.execute(
        "SELECT id, date, content, deleted_at FROM memory_rings WHERE memory_id = ?", (memory_id,)
    ).fetchall()
    if len(rings) != 1 or rings[0]["deleted_at"]:
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


def sync_card_date(c, memory_id: int) -> str | None:
    """卡片日期 = 最新一圈的日期（删掉的圈不算）。补圈 / 改圈 / 删圈之后都调这里，返回卡片现在的日期。
    跟网页列表同一个比法：只到月的 2026-10 排在 10 月所有具体日子前面，同一天的看后补的那圈；
    老数据里没补零的（2026-8-5）补零再比。一圈都不剩时不动。"""
    rows = c.execute(
        "SELECT id, date FROM memory_rings WHERE memory_id = ? AND deleted_at IS NULL", (memory_id,)
    ).fetchall()
    if not rows:
        return None
    latest = max(rows, key=lambda r: (_date_key(r["date"]), r["id"]))["date"]
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
