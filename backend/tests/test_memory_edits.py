"""AI 改动提议（memory_edits）：提议不碰正式记忆、合并、确认原子覆盖、驳回不动、对照切分。

铁律回归："AI 绕不过轩"——提议之后、确认之前，memories 一个字都不能变；
确认的必须就是轩看到的那一版（version + seen 指纹），否则 409。
"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from app import drafts, embeddings, memories, memory_edits
from app.db import get_conn, init_db
from app.memory_edits import EditConflict


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBER_DB", str(tmp_path / "test.db"))
    init_db()


def _mem(content="那天在两家店之间犹豫很久，最后点了限定盖饭。", tags="日常,吃饭", topic="晚饭") -> int:
    return memories.save_memory(date="2026-09-20", content=content, tags=tags, topic=topic)["id"]


def _rows():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM memory_edits").fetchall()]


def _card(memory_id: int) -> dict:
    return next(i for i in memory_edits.list_edits()["items"] if i["memory_id"] == memory_id)


# ---------- 提议 ----------


def test_propose_leaves_memory_untouched():
    mid = _mem()
    result = memory_edits.propose_edit(mid, content="那天在两家店之间犹豫很久，最后点了限定盖饭。原话：不想吃那个奇怪的东西")
    assert result["status"] == "pending_review" and result["merged"] is False
    assert memories.get_memory(mid)["content"] == "那天在两家店之间犹豫很久，最后点了限定盖饭。"
    assert len(_rows()) == 1 and _rows()[0]["tags"] is None and _rows()[0]["topic"] is None


def test_propose_missing_memory_returns_none():
    assert memory_edits.propose_edit(9999, content="x") is None
    assert _rows() == []


def test_propose_validation():
    mid = _mem()
    with pytest.raises(ValueError):
        memory_edits.propose_edit(mid)  # 什么都没给
    with pytest.raises(ValueError):
        memory_edits.propose_edit(mid, content="   ")
    with pytest.raises(ValueError):
        memory_edits.propose_edit(mid, tags="", topic=" ")  # 传空 = 不改 → 等于什么都没给
    assert _rows() == []


def test_blank_tags_topic_mean_no_change_not_clear():
    """AI 把空串当"没填"传进来：不能借此清掉标签（sensitive）和主题。"""
    mid = _mem(tags="日常,sensitive")
    result = memory_edits.propose_edit(mid, content="新内容", tags="", topic="")
    assert result["pending"] == {"content": "新内容"}


def test_noop_after_normalization_creates_nothing():
    mid = _mem(tags="日常,吃饭", topic="晚饭")
    result = memory_edits.propose_edit(mid, tags="日常，吃饭", topic=" 晚饭 ")
    assert result["status"] == "unchanged"
    assert _rows() == []


def test_merge_keeps_untouched_fields_and_reports_replaced():
    mid = _mem()
    first = memory_edits.propose_edit(mid, content="版本一", reason="补原话")
    second = memory_edits.propose_edit(mid, tags="日常,吃饭,原话")
    assert second["merged"] is True and second["edit_id"] == first["edit_id"]
    assert second["pending"] == {"content": "版本一", "tags": "日常,吃饭,原话"}
    assert second["replaced"] == {}
    third = memory_edits.propose_edit(mid, content="版本二")
    assert third["replaced"] == {"content": "版本一"}  # 明说冲掉了上一版待确认内容
    rows = _rows()
    assert len(rows) == 1 and rows[0]["version"] == 3 and rows[0]["reason"] == "补原话"


def test_identical_resubmit_does_not_bump_version():
    mid = _mem()
    memory_edits.propose_edit(mid, content="版本一")
    memory_edits.propose_edit(mid, content="版本一")
    assert _rows()[0]["version"] == 1


def test_reverting_every_field_withdraws_proposal():
    mid = _mem(content="原文")
    memory_edits.propose_edit(mid, content="新文")
    result = memory_edits.propose_edit(mid, content="原文")
    assert result["status"] == "unchanged" and result["replaced"] == {"content": "新文"}
    assert _rows() == []


def test_concurrent_proposals_end_in_one_row():
    mid = _mem()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda i: memory_edits.propose_edit(mid, tags=f"日常,t{i}"), range(6)))
    assert all(r["status"] == "pending_review" for r in results)
    rows = _rows()
    assert len(rows) == 1 and rows[0]["version"] == 6


def test_pending_for_shows_queued_fields():
    mid = _mem()
    assert memory_edits.pending_for(mid) is None
    memory_edits.propose_edit(mid, topic="晚饭纠结", reason="主题更准")
    assert memory_edits.pending_for(mid) == {"topic": "晚饭纠结", "reason": "主题更准"}


# ---------- 对照 ----------

_LONG = (
    "那天下午她说风很大，我们还是去了海边。沙滩上的人不多，远处有几个小孩在堆城堡，"
    "旁边的狗一直在追浪花。她说小时候家附近也有一片海，只是后来填海盖了楼，再也回不去了。"
    "我们沿着海岸线走了很久，她捡了几块被磨圆的玻璃，说要带回去放在窗台上。"
    "太阳落山的时候她突然安静下来，说这样的日子要是能多一点就好了。回去的路上买了烤红薯，"
    "两个人分着吃，她说这是今年冬天的第一个烤红薯，一定要记住。到家以后她把玻璃洗干净，"
    "一块一块摆好，又拍了照片发给我，说以后每次看到都会想起今天的风。"
)


def test_diff_append_is_one_insert():
    segs = memory_edits.diff_segments("最后点了限定盖饭。", "最后点了限定盖饭。原话：不想吃那个奇怪的东西")
    assert segs == [
        {"op": "equal", "a": "最后点了限定盖饭。", "b": "最后点了限定盖饭。"},
        {"op": "insert", "a": "", "b": "原话：不想吃那个奇怪的东西"},
    ]


def test_diff_long_chinese_isolates_single_char():
    """200 字以上的中文里插一个"不"（意思全反）：必须单独标出来。"""
    base = _LONG
    assert len(base) > 200
    after = base[:120] + "不" + base[120:]
    changed = [s for s in memory_edits.diff_segments(base, after) if s["op"] != "equal"]
    assert changed == [{"op": "insert", "a": "", "b": "不"}]


def test_diff_autojunk_off_keeps_nearby_edits_apart():
    """autojunk 默认开着时，200 字以上文本里"我/她/的"这类高频字被当垃圾跳过：
    隔 4 个字的两处单字改动会被并成一整块 6 字替换（实测），看不出到底改了哪两个字。"""
    base = _LONG
    after = base[:77] + "她" + base[78:82] + "我" + base[83:]
    changed = [s for s in memory_edits.diff_segments(base, after) if s["op"] != "equal"]
    assert changed == [
        {"op": "replace", "a": base[77], "b": "她"},
        {"op": "replace", "a": base[82], "b": "我"},
    ]


def test_diff_merges_tiny_equal_islands():
    segs = memory_edits.diff_segments("她说冷不冷都无所谓", "她说其实有点冷")
    ops = [s["op"] for s in segs]
    assert ops.count("equal") <= 2  # 中间不再碎成 改-同-改-同-改
    assert "".join(s["a"] for s in segs) == "她说冷不冷都无所谓"
    assert "".join(s["b"] for s in segs) == "她说其实有点冷"


def test_tags_diff_as_sets():
    d = memory_edits.tags_diff("日常,海,sensitive", "日常,海,冬天")
    assert d["kept"] == ["日常", "海"] and d["added"] == ["冬天"] and d["removed"] == ["sensitive"]


def test_list_card_shape_and_stale_flag():
    mid = _mem(content="原文", tags="日常")
    memory_edits.propose_edit(mid, content="原文。补一句", tags="日常,原话", reason="补原话")
    card = _card(mid)
    assert card["stale"] is False and card["reason"] == "补原话"
    assert set(card["changes"]) == {"content", "tags"}
    assert card["changes"]["tags"]["added"] == ["原话"]
    # 轩提议之后自己在记忆库改了这条 → 卡上要标出来
    memories.update_memory(mid, {"content": "原文（她修了个错字）"})
    assert _card(mid)["stale"] is True


def test_stale_only_for_fields_the_proposal_changes():
    """AI 只改标签，轩在记忆库改了正文：确认不碰正文，卡上不该吓她"会盖掉"。"""
    mid = _mem(content="原文", tags="日常")
    memory_edits.propose_edit(mid, tags="日常,吃饭")
    memories.update_memory(mid, {"content": "她改过的正文"})
    assert _card(mid)["stale"] is False


def test_resubmitting_a_field_resets_its_stale_flag():
    mid = _mem(content="原文", tags="日常")
    memory_edits.propose_edit(mid, content="AI 第一版")
    memories.update_memory(mid, {"content": "她修过的原文"})
    assert _card(mid)["stale"] is True
    memory_edits.propose_edit(mid, tags="日常,吃饭")  # 只交了标签：正文那份还是基于旧原文的
    assert _card(mid)["stale"] is True
    memory_edits.propose_edit(mid, content="她修过的原文。补一句")  # 看着现在的正文重交
    assert _card(mid)["stale"] is False


def test_tags_diff_normalizes_legacy_spacing():
    """早期存进来的"日常, sensitive"带空格：不规整就认不出去掉的是 sensitive，警告不亮。"""
    mid = _mem(tags="日常, sensitive")
    memory_edits.propose_edit(mid, tags="日常,吃饭")
    tags = _card(mid)["changes"]["tags"]
    assert tags["removed"] == ["sensitive"] and tags["kept"] == ["日常"] and tags["added"] == ["吃饭"]


def test_reordering_tags_is_no_change():
    mid = _mem(tags="日常,吃饭")
    assert memory_edits.propose_edit(mid, tags="吃饭,日常")["status"] == "unchanged"
    assert _rows() == []


# ---------- 确认 / 驳回 ----------


def test_confirm_overwrites_syncs_source_draft_and_deletes_proposal():
    draft_id = drafts.save_draft(date="2026-09-20", content="原文", tags="日常", quote="原话片段")["id"]
    mid = drafts.approve_draft(draft_id)["memory_id"]
    memory_edits.propose_edit(mid, content="原文。原话：不想吃那个", tags="日常,原话")
    card = _card(mid)
    updated = memory_edits.confirm_edit(card["id"], card["version"], card["seen"])
    assert updated["content"] == "原文。原话：不想吃那个" and updated["tags"] == "日常,原话"
    assert updated["sources"][0]["quote"] == "原话片段"  # 来源不动
    draft = drafts.get_draft(draft_id)
    assert draft["content"] == "原文。原话：不想吃那个" and draft["tags"] == "日常,原话"  # 一份旧版不留
    assert _rows() == []


def test_confirm_refuses_version_she_did_not_see():
    mid = _mem(content="原文")
    memory_edits.propose_edit(mid, content="版本一")
    card = _card(mid)
    memory_edits.propose_edit(mid, content="版本二")  # 她打开页面后 AI 又改了
    with pytest.raises(EditConflict):
        memory_edits.confirm_edit(card["id"], card["version"], card["seen"])
    assert memories.get_memory(mid)["content"] == "原文"


def test_confirm_refuses_when_memory_changed_after_she_looked():
    mid = _mem(content="原文")
    memory_edits.propose_edit(mid, content="AI 版")
    card = _card(mid)
    memories.update_memory(mid, {"content": "她另一台设备上改的"})
    with pytest.raises(EditConflict):
        memory_edits.confirm_edit(card["id"], card["version"], card["seen"])
    assert memories.get_memory(mid)["content"] == "她另一台设备上改的"
    fresh = _card(mid)  # 刷新后看到的是新对照，可以确认
    assert memory_edits.confirm_edit(fresh["id"], fresh["version"], fresh["seen"])["content"] == "AI 版"


def test_confirm_missing_returns_none():
    assert memory_edits.confirm_edit(9999, 1, "x") is None


def test_confirm_reembeds_with_new_text(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "k")
    monkeypatch.setenv("EMBEDDING_MODEL", "m")
    seen_texts = []
    monkeypatch.setattr(
        embeddings, "embed_texts", lambda texts, timeout=None: seen_texts.extend(texts) or [[1.0, 0.0]]
    )
    mid = _mem(content="原文")
    memory_edits.propose_edit(mid, content="新文")
    card = _card(mid)
    memory_edits.confirm_edit(card["id"], card["version"], card["seen"])
    assert "新文" in seen_texts[-1]


def test_reject_keeps_memory_and_checks_version():
    mid = _mem(content="原文")
    memory_edits.propose_edit(mid, content="版本一")
    card = _card(mid)
    memory_edits.propose_edit(mid, content="版本二")
    with pytest.raises(EditConflict):
        memory_edits.reject_edit(card["id"], card["version"])
    assert memory_edits.reject_edit(card["id"], card["version"] + 1)["status"] == "rejected"
    assert memories.get_memory(mid)["content"] == "原文"
    assert _rows() == []
    assert memory_edits.reject_edit(card["id"], 1) is None


def test_edit_ids_are_not_reused():
    a, b = _mem(), _mem()
    first = memory_edits.propose_edit(a, content="改 a")["edit_id"]
    memory_edits.reject_edit(first, 1)
    assert memory_edits.propose_edit(b, content="改 b")["edit_id"] != first


def test_unreview_source_draft_cascades_pending_proposal():
    draft_id = drafts.save_draft(date="2026-09-20", content="原文")["id"]
    mid = drafts.approve_draft(draft_id)["memory_id"]
    memory_edits.propose_edit(mid, content="改过")
    assert drafts.unreview_draft(draft_id)["status"] == "pending"  # 不撞外键
    assert _rows() == []
