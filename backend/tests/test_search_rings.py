"""年轮卡 PR2 搜索：按圈（长的圈按段）算指纹、关键词搜标题 / 便利贴 / 每一圈、任意一圈落在日期范围里就算、
按便利贴筛（整张比、口令「饼」、排除「吃到了」）、默认搜日常 + 长期、补算。

不打真 API：假指纹按词数算（同一类词越多越偏那个方向），所以一句心情混在一大段别的话里会被冲淡——
正好用来验证分段指纹。测试数据全是编的。"""

import random

import pytest

from app import embeddings, mcp_server, memories, migrate_rings, rings
from app.db import get_conn, init_db

AXES = {
    "难过": 0, "伤心": 0, "眼泪": 0, "哭": 0,
    "开心": 1, "高兴": 1, "笑": 1,
    "部署": 2, "服务器": 2,
}
CALLS: list[list[str]] = []
REAL_EMBED_TEXTS = embeddings.embed_texts  # fixture 会换成假的；测接口回包时要用真的


def fake_embed(texts, timeout=None):
    CALLS.append(list(texts))
    out = []
    for t in texts:
        v = [0.0, 0.0, 0.0, 1e-6]  # 尾轴垫底值防零向量（余弦距离对零向量无定义）
        for word, axis in AXES.items():
            v[axis] += t.count(word)
        out.append(v)
    return out


def boom(texts, timeout=None):
    raise RuntimeError("embedding API down")


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBER_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("EMBEDDING_MODEL", "test-model-a")
    monkeypatch.setattr(embeddings, "embed_texts", fake_embed)
    CALLS.clear()
    init_db()


def _save(content, date="2026-10-01", **kw) -> int:
    return memories.save_memory(date=date, content=content, **kw)["id"]


def _add_ring(memory_id: int, date: str, content: str) -> int:
    """补一圈（正式入口在 PR3）：写一圈、卡片日期跟上、算指纹。"""
    with get_conn() as c:
        ring_id = c.execute(
            "INSERT INTO memory_rings (memory_id, date, content) VALUES (?, ?, ?)", (memory_id, date, content)
        ).lastrowid
        rings.sync_card_date(c, memory_id)
        embeddings.embed_card(c, memory_id, whole=False)
    return ring_id


def _first_ring(memory_id: int) -> int:
    with get_conn() as c:
        return c.execute("SELECT MIN(id) FROM memory_rings WHERE memory_id = ?", (memory_id,)).fetchone()[0]


def _ring_rows(memory_id: int | None = None) -> list[dict]:
    sql = "SELECT ring_id, memory_id, chunk, char_from, char_to, text_hash, model FROM ring_embeddings"
    with get_conn() as c:
        rows = c.execute(sql + (" WHERE memory_id = ?" if memory_id else "") + " ORDER BY ring_id, chunk",
                         (memory_id,) if memory_id else ()).fetchall()
    return [dict(r) for r in rows]


def _ids(results) -> list[int]:
    return [r["id"] for r in results]


# ---------- 分段 ----------


def _covered(text: str, spans) -> bool:
    hit = [False] * len(text)
    for s, e in spans:
        for i in range(s, e):
            hit[i] = True
    return all(hit[i] or text[i].isspace() for i in range(len(text)))


def _check_spans(text: str, spans) -> None:
    assert spans, text[:20]
    assert _covered(text, spans)  # 每个字都在某一段里，一句不丢
    starts = [s for s, _ in spans]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)
    for s, e in spans:
        assert 0 <= s < e <= len(text)
        assert not text[s].isspace() and not text[e - 1].isspace()
        assert e - s <= embeddings.CHUNK_MAX + embeddings.CHUNK_MIN
        if len(spans) > 1:
            assert e - s >= embeddings.CHUNK_MIN


def test_short_ring_is_one_chunk_and_blank_is_none():
    assert embeddings.chunk_spans("  编的：今天学会了骑车。 \n") == [(2, 2 + len("编的：今天学会了骑车。"))]
    assert embeddings.chunk_spans(" \n\t ") == []
    text = "编" * embeddings.CHUNK_MAX
    assert embeddings.chunk_spans(text) == [(0, embeddings.CHUNK_MAX)]


def test_long_ring_splits_by_blank_lines_and_merges_short_paragraphs():
    paras = ["第一段" + "很长的一段话。" * 25, "嗯。", "第三段" + "又是一段话。" * 10, "第四段" + "收尾的话。" * 30]
    text = "\n\n".join(paras)
    spans = embeddings.chunk_spans(text)
    _check_spans(text, spans)
    pieces = [text[s:e] for s, e in spans]
    assert any("嗯。" in p and "第三段" in p for p in pieces)  # 碎段并进邻段，不单独成一段
    assert all(len(p) <= embeddings.CHUNK_MAX for p in pieces)


def test_one_long_paragraph_cuts_at_sentences_with_one_sentence_overlap():
    sentences = [f"第{i}句编的话，说的是一件小事。" for i in range(60)]
    text = "".join(sentences)
    spans = embeddings.chunk_spans(text)
    _check_spans(text, spans)
    for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
        assert text[e1 - 1] == "。"  # 切在句末
        assert s2 < e1 and text[s2:e1] in sentences  # 下一块开头带上上一块的最后一句
    for s, e in spans:
        assert e - s <= embeddings.CHUNK_TARGET + embeddings.CHUNK_OVERLAP


def test_no_punctuation_falls_back_to_hard_cut_with_overlap():
    text = "无标点的长串" * 200
    spans = embeddings.chunk_spans(text)
    _check_spans(text, spans)
    assert len(spans) > 1
    for (s1, e1), (s2, _) in zip(spans, spans[1:]):
        assert e1 - s2 == embeddings.HARD_OVERLAP


def test_crlf_and_fullwidth_space_blank_lines_split_paragraphs():
    for blank in ("\r\n　\r\n", "\n\xa0\n", "\n \t\n"):
        text = ("甲" * 300) + blank + ("乙" * 300)
        spans = embeddings.chunk_spans(text)
        assert [text[s:e] for s, e in spans] == ["甲" * 300, "乙" * 300], repr(blank)
    p1, p2 = "编" * 200 + "。" + "短" * 40 + "。", "长" * 300 + "。"
    text = p1 + "\n\xa0\n" + p2  # 重叠的那一句不越过空行
    assert [text[s:e] for s, e in embeddings.chunk_spans(text)] == [p1, p2]


def test_overlap_never_leaves_a_chunk_inside_the_next():
    text = "编" * 239 + "。" + "短" * 39 + "。" + "长" * 219 + "。"
    spans = embeddings.chunk_spans(text)
    assert spans == [(0, 240), (240, 500)]
    _check_spans(text, spans)


def test_ascii_closing_quote_stays_with_its_sentence():
    text = '他说"' + "编" * 240 + '。"然后' + "长" * 220 + "。"
    pieces = [text[s:e] for s, e in embeddings.chunk_spans(text)]
    assert pieces[0].endswith('。"') and pieces[1].startswith("然后")
    text = '她说："' + "编" * 240 + '。""那' + "长" * 220 + "。"  # 后面紧跟下一句的开引号：只带走一个
    pieces = [text[s:e] for s, e in embeddings.chunk_spans(text)]
    assert pieces[0].endswith('。"') and pieces[1].startswith('"那')


def test_realistic_sentence_runs_keep_chunk_invariants():
    rnd = random.Random(1020)
    for _ in range(2000):
        sentences = ["编" * rnd.randint(10, 300) + rnd.choice("。！？；") for _ in range(rnd.randint(2, 8))]
        text = rnd.choice(["", "\n\n"]).join(sentences)
        _check_spans(text, embeddings.chunk_spans(text))


def test_random_texts_keep_chunk_invariants():
    rnd = random.Random(20261020)
    alphabet = "编的字词语句" * 5 + "。！？；，、\n " + "”）"
    for _ in range(300):
        n = rnd.randint(1, 2500)
        text = "".join(rnd.choice(alphabet) for _ in range(n))
        if rnd.random() < 0.3:
            text = text.replace(" ", "\n\n")
        spans = embeddings.chunk_spans(text)
        if not text.strip():
            assert spans == []
            continue
        _check_spans(text, spans)


# ---------- 写入时算指纹 ----------


def test_save_writes_whole_and_ring_fingerprints_in_one_call():
    mid = _save("编的：今天很开心", tags="心情", topic="小确幸")
    assert len(CALLS) == 1 and len(CALLS[0]) == 2  # 整条 + 一圈一段，一次 API 调用
    [row] = _ring_rows(mid)
    assert (row["ring_id"], row["chunk"], row["model"]) == (_first_ring(mid), 0, "test-model-a")
    assert CALLS[0][1] == "小确幸\n心情\n编的：今天很开心"  # 标题 + 便利贴 + 正文
    with get_conn() as c:
        assert c.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0] == 1


def test_long_ring_gets_one_fingerprint_per_chunk():
    text = "\n\n".join(["编的一段话。" * 40] * 4)
    mid = _save(text)
    spans = embeddings.chunk_spans(text)
    assert len(spans) > 1
    assert [(r["chunk"], r["char_from"], r["char_to"]) for r in _ring_rows(mid)] == [
        (i, s, e) for i, (s, e) in enumerate(spans)
    ]


def test_edit_content_recomputes_and_date_only_edit_keeps_rows(monkeypatch):
    mid = _save("编的：那晚眼泪在眼眶里打转")
    before = _ring_rows(mid)
    monkeypatch.setattr(embeddings, "embed_texts", boom)
    memories.update_memory(mid, {"date": "2026-10-02"})
    assert _ring_rows(mid) == before  # 只改日期，指纹不用动，API 挂了也不误删
    memories.update_memory(mid, {"content": "编的：那天很开心，一直在笑"})
    assert _ring_rows(mid) == []  # 内容变了算不出来：旧的删掉，不按改前的意思命中
    assert memories.search_memories("伤心") == []
    monkeypatch.setattr(embeddings, "embed_texts", fake_embed)
    memories.update_memory(mid, {"content": "编的：那天很开心，一直在笑，笑出了眼泪"})
    [row] = _ring_rows(mid)
    assert row["text_hash"] != before[0]["text_hash"]


def test_tag_change_refreshes_ring_fingerprints():
    mid = _save("编的：去海边走了走", tags="周末")
    old = _ring_rows(mid)
    memories.update_memory(mid, {"tags": "周末,开心"})
    new = _ring_rows(mid)
    assert len(new) == 1 and new[0]["text_hash"] != old[0]["text_hash"]
    assert CALLS[-1][-1] == "周末,开心\n编的：去海边走了走"


def test_ring_content_change_from_any_path_drops_its_fingerprints():
    mid = _save("编的：第一圈")
    assert _ring_rows(mid)
    with get_conn() as c:  # 比如退回旧程序期间、或者以后的改圈：只要正文变了，旧指纹立刻作废
        c.execute("UPDATE memory_rings SET content = '编的：改过的第一圈' WHERE memory_id = ?", (mid,))
    assert _ring_rows(mid) == []
    with get_conn() as c:
        c.execute("UPDATE memory_rings SET date = '2026-10-05' WHERE memory_id = ?", (mid,))
    assert _ring_rows(mid) == []  # 改日期不碰指纹（本来也没有了）


def test_purge_cascades_ring_fingerprints():
    mid = _save("编的：要删的一条")
    memories.delete_memory(mid)
    with get_conn() as c:
        c.execute("UPDATE memories SET deleted_at = datetime('now', '-400 days') WHERE id = ?", (mid,))
    assert memories.purge_expired()["purged"] == [mid]
    assert _ring_rows() == []


# ---------- 搜索 ----------


def _long_card_with_buried_mood() -> tuple[int, int]:
    """一张很长的卡（一圈），一句心情埋在中间一段，其余几段是工作流水。"""
    filler = "今天把东西搬上了服务器，顺手整理了目录、改了几个配置、写了点说明，又对着日志看了半天。" * 3
    paras = [filler, filler, "那天晚上眼泪止不住，一个人在阳台哭到很晚。", filler, filler, filler]
    mid = _save("\n\n".join(paras), date="2026-09-12", tags="工作")
    return mid, _first_ring(mid)


def test_buried_mood_found_by_paraphrase_via_ring_chunks():
    mid, ring_id = _long_card_with_buried_mood()
    for i in range(5):  # 字面不同、意思沾边的干扰项
        _save(f"编的：第{i}次笑着哭了一下，又开心又高兴", date=f"2026-09-0{i + 1}")
    hits = memories.search_memories("伤心")  # 字面上哪条都没有「伤心」，只能靠意思
    assert mid in _ids(hits)[:3]
    hit = next(h for h in hits if h["id"] == mid)
    assert hit["ring"]["id"] == ring_id and hit["ring"]["date"] == "2026-09-12"
    with get_conn() as c:
        content = c.execute("SELECT content FROM memory_rings WHERE id = ?", (ring_id,)).fetchone()[0]
    [(s, e)] = [(s, e) for s, e in embeddings.chunk_spans(content) if "眼泪" in content[s:e]]
    [vec] = [v for v in embeddings.vector_search("伤心") if v["id"] == mid]
    assert (vec["ring_id"], vec["char_from"], vec["char_to"]) == (ring_id, s, e)  # 对上的就是埋着心情的那一段
    assert hit["ring"]["excerpt"] == memories._excerpt(content, s, e)  # 摘录是那一段的开头

    with get_conn() as c:  # 对照：只有整条指纹时，这句心情被整段工作流水冲淡了
        c.execute("DELETE FROM ring_embeddings")
    assert mid not in _ids(memories.search_memories("伤心"))[:3]


def test_card_without_ring_fingerprints_falls_back_to_whole():
    mid = _save("编的：那晚眼泪在眼眶里打转")
    with get_conn() as c:  # 上线后、补算之前：只有整条指纹
        c.execute("DELETE FROM ring_embeddings")
    [hit] = memories.search_memories("伤心")
    assert hit["id"] == mid and "ring" not in hit


def test_keyword_leg_searches_title_tags_and_every_ring(monkeypatch):
    monkeypatch.delenv("EMBEDDING_API_KEY")  # 只看关键词腿（假指纹里没这些词，语义腿会把所有卡都排进来）
    titled = _save("编的：一段没有关键词的正文", topic="南瓜田")  # 主题只有它用 → 变成标题
    tagged = _save("编的：另一段正文", tags="稻草人")
    card = _save("编的：十月种下了种子", date="2026-10-03")
    later = _add_ring(card, "2026-11-02", "编的：十一月发芽了，冒出两片小叶子")
    assert _ids(memories.search_memories("南瓜田")) == [titled]
    assert _ids(memories.search_memories("稻草人")) == [tagged]
    [hit] = memories.search_memories("小叶子")
    assert hit["id"] == card and hit["ring"]["id"] == later and hit["ring"]["date"] == "2026-11-02"
    assert hit["date"] == "2026-11-02"  # 卡片日期 = 最新一圈
    assert "小叶子" in hit["ring"]["excerpt"]


def test_keyword_hit_in_first_ring_has_no_duplicate_excerpt():
    mid = _save("编的：周末去看了一场电影")
    [hit] = memories.search_memories("电影")
    assert hit["ring"] == {"id": _first_ring(mid), "date": "2026-10-01"}  # 跟卡的开头一样，不重复给摘录


def test_long_ring_keyword_excerpt_centers_on_the_word():
    mid = _save("编的开头。" * 60 + "后来在桥边捡到一只纸船。" + "编的结尾。" * 20)
    [hit] = memories.search_memories("纸船")
    excerpt = hit["ring"]["excerpt"]
    assert hit["id"] == mid and "纸船" in excerpt and excerpt.startswith("…") and excerpt.endswith("…")
    assert len(excerpt) <= memories.SUMMARY_LEN + 2


def test_deleted_ring_is_not_searched(monkeypatch):
    monkeypatch.delenv("EMBEDDING_API_KEY")  # 只看关键词腿（假指纹里没这些词，语义腿会把所有卡都排进来）
    card = _save("编的：第一圈在这里")
    later = _add_ring(card, "2026-10-09", "编的：第二圈说到了萤火虫")
    with get_conn() as c:
        c.execute("UPDATE memory_rings SET deleted_at = datetime('now') WHERE id = ?", (later,))
    assert memories.search_memories("萤火虫") == []


def test_date_filter_counts_any_ring_in_range():
    """方案第七节：一张两圈的卡（一圈 10 月、一圈 11 月），按 10 月筛、按 11 月筛都能出来。"""
    card = _save("编的：十月计划去看海", date="2026-10-05")
    _add_ring(card, "2026-11-20", "编的：十一月真的去看了海，很开心")
    other = _save("编的：九月也说过想看海", date="2026-09-01")
    for lo, hi in (("2026-10", "2026-10"), ("2026-11", "2026-11"), ("2026-10-01", "2026-10-31")):
        assert _ids(memories.search_memories("看海", date_from=lo, date_to=hi)) == [card], (lo, hi)
        assert _ids(memories.list_memories(date_from=lo, date_to=hi)["items"]) == [card], (lo, hi)
    assert set(_ids(memories.search_memories("看海", date_from="2026-09", date_to="2026-11"))) == {card, other}
    assert memories.search_memories("看海", date_from="2026-12") == []
    with get_conn() as c:
        assert c.execute("SELECT date FROM memories WHERE id = ?", (card,)).fetchone()[0] == "2026-11-20"


def test_date_filter_month_only_ring_and_semantic_leg():
    card = _save("编的：那个月常常想哭", date="2026-08")  # 只记到月：算整个 8 月
    _add_ring(card, "2026-10-02", "编的：十月已经好多了")
    assert _ids(memories.search_memories("伤心", date_from="2026-08-20", date_to="2026-08-25")) == [card]
    assert memories.search_memories("伤心", date_from="2026-09", date_to="2026-09") == []


def test_tag_filter_is_exact_and_pie_alias(monkeypatch):
    """方案第七节：贴了「连理饼」的卡 vs 正文带「南瓜饼」的；按便利贴搜「饼」只出前者，自由搜「南瓜饼」能出后者。"""
    pie = _save("编的：说好明年一起去看极光", tags="连理饼")
    pumpkin = _save("编的：晚饭做了南瓜饼，有点甜")
    eaten = _save("编的：一起学会了做饭", tags="连理饼,吃到了")
    dough = _save("编的：也许可以养一只猫", tags="面团")
    lookalike = _save("编的：买了一盒饼干", tags="连理饼干")
    assert set(_ids(memories.list_memories(tags="饼")["items"])) == {pie, eaten}
    assert set(_ids(memories.search_memories("饼", tags="饼"))) == {pie, eaten}
    with monkeypatch.context() as m:  # 只看关键词腿（假指纹里没有这些词，语义腿会把所有卡都排进来）
        m.delenv("EMBEDDING_API_KEY")
        assert _ids(memories.search_memories("南瓜饼")) == [pumpkin]
        assert memories.search_memories("南瓜饼", tags="饼") == []
    unfulfilled = memories.list_memories(tags="饼", exclude_tags="吃到了")["items"]
    assert _ids(unfulfilled) == [pie]  # 还没兑现的饼；面团不算饼
    assert set(_ids(memories.list_memories(tags="面团,饼")["items"])) == set()
    assert _ids(memories.list_memories(tags="面团")["items"]) == [dough]
    assert _ids(memories.list_memories(tags="连理饼干")["items"]) == [lookalike]


def test_tag_filter_groups_and_old_untidy_tags():
    a = _save("编的：一起去爬山", tags="远方饼,开心")
    b = _save("编的：一起搭了书架", tags="筑巢饼")
    with get_conn() as c:  # 老数据里标签带空格、中文逗号
        c.execute("UPDATE memories SET tags = ' 筑巢饼 ， 开心 ' WHERE id = ?", (b,))
    assert set(_ids(memories.list_memories(tags="饼，开心")["items"])) == {a, b}  # 每组都要满足
    assert _ids(memories.list_memories(tags="远方饼, 开心")["items"]) == [a]
    assert _ids(memories.list_memories(exclude_tags="饼")["items"]) == []
    assert memories.list_memories(tags=" , ")["stats"]["total"] == 2  # 空的等于不筛
    with pytest.raises(ValueError):
        memories.list_memories(tags=["饼"])


def test_default_scope_is_daily_plus_longterm():
    daily = _save("编的：日常里的散步")
    longterm = _save("编的：长期里的散步习惯", space="longterm")
    project = _save("编的：项目里的散步脚本", space="ember")
    assert set(_ids(memories.search_memories("散步"))) == {daily, longterm}
    assert _ids(memories.search_memories("散步", space="ember")) == [project]
    assert _ids(memories.search_memories("散步", space="personal")) == [daily]
    assert set(_ids(memories.search_memories("散步", space="all"))) == {daily, longterm, project}
    listed = memories.list_memories()
    assert listed["stats"]["total"] == 2 and listed["stats"]["by_space"] == {"personal": 1, "longterm": 1}
    assert memories.list_memories(space="all")["stats"]["total"] == 3


def test_semantic_api_down_falls_back_to_keyword(monkeypatch):
    mid = _save("编的：今天很难过", tags="心情")
    monkeypatch.setattr(embeddings, "embed_texts", boom)
    assert _ids(memories.search_memories("难过")) == [mid]
    assert memories.search_memories("伤心") == []


def test_semantic_candidates_respect_filters_before_top_k(monkeypatch):
    """语义腿先按日期 / 便利贴 / 空间筛再取前 k 张：范围外一样像（编号还更小）的不能把范围内的挤出候选。"""
    monkeypatch.setattr(memories, "VEC_TOP_K", 2)
    for d in ("2026-05-01", "2026-05-02", "2026-05-03"):
        _save("编的：哭了一场", date=d)
    for _ in range(3):
        _save("编的：哭了一场", space="ember")
    inside = _save("编的：眼泪", date="2026-08-02")
    pie = _save("编的：眼泪", date="2026-07-02", tags="远方饼", space="longterm")
    assert _ids(memories.search_memories("伤心", date_from="2026-08", date_to="2026-08")) == [inside]
    assert _ids(memories.search_memories("伤心", space="all", tags="饼")) == [pie]
    assert _ids(memories.search_memories("伤心", space="ember")) == [4, 5]


# ---------- MCP ----------


def test_mcp_search_and_list_take_tag_filters():
    pie = mcp_server.memory_save(date="2026-10-10", content="编的：想一起去海边住几天", tags="远方饼")["id"]
    mcp_server.memory_save(date="2026-10-11", content="编的：海边的南瓜饼摊", tags="小吃")
    found = mcp_server.memory_search("海边", tags="饼")
    assert found["count"] == 1 and found["results"][0]["id"] == pie
    listed = mcp_server.memory_list(tags="饼", exclude_tags="吃到了")
    assert [i["id"] for i in listed["items"]] == [pie]
    assert "error" in mcp_server.memory_search("海边", date_from="下周")
    doc = mcp_server.memory_search.__doc__
    assert "吃到了" in doc and "longterm" in doc and "ring" in doc


# ---------- 补算 ----------


def test_rebuild_backfills_rings_dry_run_and_is_idempotent(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts", boom)
    a = _save("编的：那晚眼泪在眼眶里打转")
    b = _save("\n\n".join(["编的长段。" * 60] * 3))
    assert _ring_rows() == []
    dry = embeddings.rebuild_embeddings(dry_run=True)  # 不调 API
    n_chunks = 1 + len(embeddings.chunk_spans(memories.get_memory(b)["content"]))
    assert dry["dry_run"] and dry["memories_to_embed"] == 2 and dry["rings_to_embed"] == 2
    assert dry["ring_chunks_to_embed"] == n_chunks and dry["chars_to_embed"] > 0
    assert _ring_rows() == []
    monkeypatch.setattr(embeddings, "embed_texts", fake_embed)
    stats = embeddings.rebuild_embeddings()
    assert stats["embedded_now"] == 2 and stats["rings_embedded_now"] == 2
    assert stats["ring_chunks_now"] == n_chunks and stats["rings_up_to_date"] == stats["rings_total"] == 2
    assert {r["memory_id"] for r in _ring_rows()} == {a, b}
    CALLS.clear()
    again = embeddings.rebuild_embeddings()
    assert again["embedded_now"] == 0 and again["rings_embedded_now"] == 0 and CALLS == []
    assert _ids(memories.search_memories("伤心"))[0] == a


def test_rebuild_resumes_after_failure_mid_way(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts", boom)
    ids = [_save(f"编的：第{i}条，有点难过") for i in range(5)]
    calls = {"n": 0}

    def flaky(texts, timeout=None):
        calls["n"] += 1
        if calls["n"] == 5:  # 整条 3 批都过了，圈的第 2 批断了
            raise RuntimeError("network")
        return fake_embed(texts)

    monkeypatch.setattr(embeddings, "embed_texts", flaky)
    with pytest.raises(RuntimeError):
        embeddings.rebuild_embeddings(batch_size=2)
    assert len({r["ring_id"] for r in _ring_rows()}) == 2  # 断之前那一批已经落盘
    monkeypatch.setattr(embeddings, "embed_texts", fake_embed)
    stats = embeddings.rebuild_embeddings(batch_size=2)
    assert stats["embedded_now"] == 0 and stats["rings_embedded_now"] == 3
    assert stats["rings_up_to_date"] == 5 and {r["memory_id"] for r in _ring_rows()} == set(ids)


def test_rebuild_fills_rows_dropped_by_header_change_and_model_switch(monkeypatch):
    mid = _save("编的：去看了一场雨", tags="散心")
    with get_conn() as c:  # 便利贴被别的路改了（没走写入时重算）：触发器删掉了分段指纹
        c.execute("UPDATE memories SET tags = '散心,开心' WHERE id = ?", (mid,))
    assert embeddings.rebuild_embeddings()["rings_embedded_now"] == 1
    monkeypatch.setenv("EMBEDDING_MODEL", "test-model-b")
    assert memories.search_memories("高兴") == []  # 换模型：旧指纹全部失效，退回关键词
    stats = embeddings.rebuild_embeddings()
    assert stats["embedded_now"] == 1 and stats["rings_embedded_now"] == 1
    assert {r["model"] for r in _ring_rows()} == {"test-model-b"}
    assert _ids(memories.search_memories("高兴")) == [mid]


def test_cli_dry_run(capsys):
    _save("编的：一条")
    assert embeddings.main(["embeddings", "--dry-run"]) == 0
    assert '"dry_run": true' in capsys.readouterr().out
    assert embeddings.main(["embeddings", "--bogus"]) == 2


# ---------- 搬家对齐圈时 ----------


def test_ring_resync_on_startup_drops_stale_fingerprints():
    mid = _save("编的：原来的内容")
    with get_conn() as c:  # 退回旧程序期间只改了卡没改圈
        c.execute("UPDATE memories SET content = '编的：旧程序改过的内容' WHERE id = ?", (mid,))
    with get_conn() as c:
        report = migrate_rings.migrate(c)
    assert report["rings_resynced"] == [mid]
    assert _ring_rows(mid) == []  # 圈跟上了，按旧内容算的指纹作废，补算会补
    assert embeddings.rebuild_embeddings()["rings_embedded_now"] == 1


# ---------- 审查补的几条 ----------


def test_topic_edited_before_pr3_is_still_searchable(monkeypatch):
    monkeypatch.delenv("EMBEDDING_API_KEY")
    mid = _save("编的：一段普通的正文")
    memories.update_memory(mid, {"topic": "萤火虫计划"}, by="ke")  # PR3 之前 memory_edit 还能改主题
    assert _ids(memories.search_memories("萤火虫计划")) == [mid]


def test_hit_ring_prefers_rings_inside_date_range(monkeypatch):
    card = _save("编的：十月说想去看海", date="2026-10-05")
    _add_ring(card, "2026-11-20", "编的：十一月终于去看海了")
    with monkeypatch.context() as m:
        m.delenv("EMBEDDING_API_KEY")
        [hit] = memories.search_memories("看海", date_from="2026-10", date_to="2026-10")
        assert hit["ring"]["date"] == "2026-10-05"
        [hit] = memories.search_memories("看海")
        assert hit["ring"]["date"] == "2026-11-20"  # 不带日期：一样多看新的
    sad = _save("编的：那天很开心", date="2026-10-06")
    _add_ring(sad, "2026-11-21", "编的：哭了很久，眼泪止不住")
    hits = {h["id"]: h for h in memories.search_memories("伤心", date_from="2026-10", date_to="2026-10")}
    assert hits[sad]["ring"]["date"] == "2026-10-06"  # 语义也只拿范围里的圈去比
    hits = {h["id"]: h for h in memories.search_memories("伤心")}
    assert hits[sad]["ring"]["date"] == "2026-11-21"


def test_card_header_change_from_any_path_drops_ring_fingerprints():
    mid = _save("编的：去看了一场雨", tags="散心")
    assert _ring_rows(mid)
    with get_conn() as c:  # 比如退回旧程序期间改了便利贴（旧程序只管整条指纹）
        c.execute("UPDATE memories SET tags = '散心,难过' WHERE id = ?", (mid,))
    assert _ring_rows(mid) == []
    [hit] = memories.search_memories("伤心")  # 退回整条指纹，不按旧的便利贴命中
    assert hit["id"] == mid and "ring" not in hit


class _ShortResponse:
    """少给一条的接口回包（中间那条丢了）。"""

    def __init__(self, n):
        self.n = n

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": [{"index": i, "embedding": [1.0, 0.0, 0.0, 1e-6]} for i in range(self.n) if i != 1]}


def test_short_api_response_counts_as_failure(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts", REAL_EMBED_TEXTS)
    monkeypatch.setattr(embeddings.httpx, "post", lambda url, json, **kw: _ShortResponse(len(json["input"])))
    with pytest.raises(RuntimeError):
        embeddings.embed_texts(["a", "b", "c"])
    saved = memories.save_memory(date="2026-10-01", content="\n\n".join(["编的一段话。" * 40] * 4))
    assert saved["embedded"] is False and _ring_rows() == []  # 不会只存一半、也不会把指纹配错段


def test_rebuild_does_not_overwrite_an_edit_made_meanwhile(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts", boom)
    mid = _save("编的：那晚眼泪在眼眶里打转，一个人哭")
    other = _save("编的：另一条，也有点难过")
    calls = {"n": 0}

    def edit_during_ring_call(texts, timeout=None):
        calls["n"] += 1
        if calls["n"] == 2:  # 第 1 次是整条指纹，第 2 次是按圈：这会儿小克改了第一张卡
            memories.update_memory(mid, {"content": "编的：今天很开心"}, by="ke")
        return fake_embed(texts)

    monkeypatch.setattr(embeddings, "embed_texts", edit_during_ring_call)
    stats = embeddings.rebuild_embeddings()
    assert stats["skipped_changed_meanwhile"] == 1 and stats["rings_embedded_now"] == 1
    assert stats["rings_up_to_date"] == stats["rings_total"] == 2  # 改的那一下自己算过了
    [row] = _ring_rows(mid)
    with get_conn() as c:
        plan = embeddings._ring_plans(c, "r.memory_id = ?", (mid,))[0]
    assert row["text_hash"] == plan["chunks"][0][4]  # 是改之后的文本，没被旧的盖掉
    assert _ids(memories.search_memories("伤心"))[0] == other


def test_rebuild_survives_a_card_purged_meanwhile(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts", boom)
    gone = _save("编的：马上要被真删的一条")
    keep = _save("编的：留着的一条")
    with get_conn() as c:  # 整条指纹先补齐，只剩圈要算
        for mid in (gone, keep):
            c.execute("INSERT INTO memory_embeddings (memory_id, model, dim, embedding) VALUES (?, 'test-model-a', 4, ?)",
                      (mid, embeddings._pack([0.0, 0.0, 0.0, 1.0])))
    state = {"done": False}

    def purge_during_call(texts, timeout=None):
        if not state["done"]:
            state["done"] = True
            with get_conn() as c:
                c.execute("DELETE FROM memories WHERE id = ?", (gone,))
        return fake_embed(texts)

    monkeypatch.setattr(embeddings, "embed_texts", purge_during_call)
    stats = embeddings.rebuild_embeddings()
    assert stats["skipped_changed_meanwhile"] == 1 and stats["rings_embedded_now"] == 1
    assert {r["memory_id"] for r in _ring_rows()} == {keep}


def test_outdated_text_hash_is_found_by_rebuild_and_dropped_on_failure(monkeypatch):
    """行还在、但文本摘要对不上（比如以后改了拼法或分段参数）：补算要认出来重算；算不了时删掉。"""
    mid = _save("编的：那晚眼泪在眼眶里打转")
    before = _ring_rows(mid)
    with get_conn() as c:
        c.execute("UPDATE ring_embeddings SET text_hash = 'outdated' WHERE memory_id = ?", (mid,))
    assert embeddings.rebuild_embeddings(dry_run=True)["rings_to_embed"] == 1
    assert embeddings.rebuild_embeddings()["rings_embedded_now"] == 1
    assert _ring_rows(mid) == before
    with get_conn() as c:
        c.execute("UPDATE ring_embeddings SET text_hash = 'outdated' WHERE memory_id = ?", (mid,))
    monkeypatch.setattr(embeddings, "embed_texts", boom)
    with get_conn() as c:
        assert embeddings.embed_card(c, mid, whole=False) is False
    assert _ring_rows(mid) == []


def test_tidying_old_tags_on_save_keeps_ring_fingerprints():
    mid = _save("编的：去海边走了走", tags="周末")
    with get_conn() as c:  # 老数据里的标签带空格、中文逗号
        c.execute("UPDATE memories SET tags = ' 周末 ， 开心 ' WHERE id = ?", (mid,))
    embeddings.rebuild_embeddings()
    CALLS.clear()
    result = memories.update_memory(mid, {"tags": "周末,开心"}, by="xuan")  # 网页存的时候原样送回来
    assert result["changed"] == []
    assert len(_ring_rows(mid)) == 1  # 规整写回删掉了分段指纹，当场补上
    assert len(CALLS) == 1 and CALLS[0] == ["周末,开心\n编的：去海边走了走"]


def test_exclude_only_tag_filter_keeps_the_rest():
    plain = _save("编的：没有便利贴")
    happy = _save("编的：开心的一天", tags="开心")
    pie = _save("编的：想一起去看海", tags="远方饼")
    eaten = _save("编的：做成了蛋糕", tags="筑巢饼,吃到了")
    assert set(_ids(memories.list_memories(exclude_tags="饼")["items"])) == {plain, happy}
    assert set(_ids(memories.list_memories(exclude_tags="吃到了")["items"])) == {plain, happy, pie}
    assert eaten not in _ids(memories.list_memories(exclude_tags="吃到了")["items"])


def test_cards_without_live_rings_filter_by_card_date():
    ringless = _save("编的：退回旧程序期间存的", date="2026-08-03")
    hidden = _save("编的：圈都删掉了", date="2026-07-05")
    with get_conn() as c:
        c.execute("DELETE FROM memory_rings WHERE memory_id = ?", (ringless,))
        c.execute("UPDATE memory_rings SET deleted_at = datetime('now') WHERE memory_id = ?", (hidden,))
    assert _ids(memories.list_memories(date_from="2026-08", date_to="2026-08")["items"]) == [ringless]
    assert _ids(memories.list_memories(date_from="2026-07", date_to="2026-07")["items"]) == [hidden]
    assert memories.list_memories(date_from="2026-09")["items"] == []


def test_semantic_leg_ignores_deleted_rings():
    card = _save("编的：那天很开心", date="2026-10-01")
    sad = _add_ring(card, "2026-10-08", "编的：哭了很久，眼泪止不住")
    other = _save("编的：有一点难过", date="2026-09-01")
    [top] = memories.search_memories("伤心", limit=1)
    assert top["id"] == card and top["ring"]["id"] == sad
    with get_conn() as c:
        c.execute("UPDATE memory_rings SET deleted_at = datetime('now') WHERE id = ?", (sad,))
    hits = memories.search_memories("伤心")
    assert hits[0]["id"] == other  # 删掉的那一圈不再拿来比
    assert all(h.get("ring", {}).get("id") != sad for h in hits)
    with get_conn() as c:  # 还在的那一圈也没有指纹了（删掉那圈的还留着）：退回整条指纹，不报哪一圈
        c.execute("DELETE FROM ring_embeddings WHERE memory_id = ? AND ring_id != ?", (card, sad))
    hit = next(h for h in memories.search_memories("伤心") if h["id"] == card)
    assert "ring" not in hit


def test_rebuild_whole_pass_skips_a_card_edited_meanwhile(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts", boom)
    mid = _save("编的：那晚眼泪在眼眶里打转")
    calls = {"n": 0}

    def edit_during_whole_call(texts, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:  # 第 1 次是整条指纹：这会儿轩在网页上改了它
            memories.update_memory(mid, {"content": "编的：今天很开心"}, by="xuan")
        return fake_embed(texts)

    monkeypatch.setattr(embeddings, "embed_texts", edit_during_whole_call)
    stats = embeddings.rebuild_embeddings()
    assert stats["embedded_now"] == 0 and stats["skipped_changed_meanwhile"] >= 1
    with get_conn() as c:
        blob = c.execute("SELECT embedding FROM memory_embeddings WHERE memory_id = ?", (mid,)).fetchone()[0]
    assert blob == embeddings._pack(fake_embed(["编的：今天很开心"])[0])  # 是改之后的，没被旧的盖掉


def test_rebuild_does_not_overwrite_chunk_offsets_after_whitespace_edit(monkeypatch):
    content = "编" * 299 + "。\n\n" + "写" * 299 + "。"
    edited = content.replace("\n\n", "\n\n\n")
    mid = _save(content)
    with get_conn() as c:
        c.execute("DELETE FROM ring_embeddings WHERE memory_id = ?", (mid,))
    state = {"edited": False}

    def edit_during_call(texts, timeout=None):
        if not state["edited"]:
            state["edited"] = True
            memories.update_memory(mid, {"content": edited}, by="xuan")
        return fake_embed(texts)

    monkeypatch.setattr(embeddings, "embed_texts", edit_during_call)
    stats = embeddings.rebuild_embeddings()
    assert [(r["char_from"], r["char_to"]) for r in _ring_rows(mid)] == embeddings.chunk_spans(edited)
    assert stats["skipped_changed_meanwhile"] == 1 and stats["rings_embedded_now"] == 0
    assert embeddings.rebuild_embeddings(dry_run=True)["rings_to_embed"] == 0


def test_rebuild_repairs_stale_chunk_offsets():
    content = "编" * 299 + "。\n\n\n" + "写" * 299 + "。"
    mid = _save(content)
    with get_conn() as c:  # 模拟先前补算曾留下的错位坐标；段文本摘要仍然正确
        c.execute(
            "UPDATE ring_embeddings SET char_from = char_from - 1, char_to = char_to - 1 "
            "WHERE memory_id = ? AND chunk = 1", (mid,),
        )
    assert embeddings.rebuild_embeddings(dry_run=True)["rings_to_embed"] == 1
    assert embeddings.rebuild_embeddings()["rings_embedded_now"] == 1
    assert [(r["char_from"], r["char_to"]) for r in _ring_rows(mid)] == embeddings.chunk_spans(content)
    assert embeddings.rebuild_embeddings(dry_run=True)["rings_to_embed"] == 0


class _InvalidVectorResponse:
    def __init__(self, n, bad_vector):
        self.n, self.bad_vector = n, bad_vector

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": [
            {"index": i, "embedding": self.bad_vector if i == self.n - 1 else [1.0, 0.0, 0.0, 1e-6]}
            for i in range(self.n)
        ]}


@pytest.mark.parametrize("bad_vector", [["bad", 0.0, 0.0, 1e-6], None, [1e100, 0.0, 0.0, 1e-6],
                                      [float("nan"), 0.0, 0.0, 1e-6]],
                         ids=["non_numeric", "null", "float32_overflow", "nan"])
def test_invalid_vector_response_does_not_block_save(monkeypatch, bad_vector):
    monkeypatch.setattr(embeddings, "embed_texts", REAL_EMBED_TEXTS)
    monkeypatch.setattr(embeddings.httpx, "post",
                        lambda url, json, **kw: _InvalidVectorResponse(len(json["input"]), bad_vector))
    saved = memories.save_memory(date="2026-10-01", content="编的：指纹暂时算不出来也要保存")
    assert saved["saved"] is True and saved["embedded"] is False
    assert memories.get_memory(saved["id"])["content"] == "编的：指纹暂时算不出来也要保存"
    assert _ring_rows() == []
    with get_conn() as c:
        assert c.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0] == 0


def test_invalid_vector_response_does_not_roll_back_edit(monkeypatch):
    mid = _save("编的：原来想去海边")
    monkeypatch.setattr(embeddings, "embed_texts", REAL_EMBED_TEXTS)
    monkeypatch.setattr(embeddings.httpx, "post",
                        lambda url, json, **kw: _InvalidVectorResponse(len(json["input"]), ["bad", 0.0, 0.0, 1e-6]))
    memories.update_memory(mid, {"content": "编的：后来决定去山上"}, by="xuan")
    assert memories.get_memory(mid)["content"] == "编的：后来决定去山上"
    assert _ring_rows(mid) == []
    with get_conn() as c:
        assert c.execute("SELECT COUNT(*) FROM memory_embeddings WHERE memory_id = ?", (mid,)).fetchone()[0] == 0


def test_float32_overflow_packed_as_infinity_is_still_a_soft_failure(monkeypatch):
    real_pack = embeddings._pack

    def pack_as_native_cast(vector):  # 模拟 Python 3.12 的 native float 转换：溢出为 inf，不抛异常
        return real_pack([float("inf") if v == 1e100 else v for v in vector])

    monkeypatch.setattr(embeddings, "_pack", pack_as_native_cast)
    monkeypatch.setattr(embeddings, "embed_texts", REAL_EMBED_TEXTS)
    monkeypatch.setattr(embeddings.httpx, "post",
                        lambda url, json, **kw: _InvalidVectorResponse(len(json["input"]), [1e100, 0.0, 0.0, 1e-6]))
    saved = memories.save_memory(date="2026-10-01", content="编的：不同运行环境下也要保存成功")
    assert saved["saved"] is True and saved["embedded"] is False
    assert memories.get_memory(saved["id"]) is not None and _ring_rows() == []
    with get_conn() as c:
        assert c.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0] == 0
