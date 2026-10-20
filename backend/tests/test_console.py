"""记忆库网页的接口（替掉旧审核台）：浏览、手写、改、删、恢复、回退、清上一版、最近动静、设置。

纪律：只收白名单字段；tags 规整中文标点；内容 / 日期不能改空；网页上的一切记成轩；
提取脚本的钥匙只能存（记成小克），改删回退设置一律只认浏览器登录；MCP 的 token 进不来。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import console, memories
from app.db import get_conn, init_db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBER_DB", str(tmp_path / "test.db"))
    init_db()


@pytest.fixture(scope="module")
def client():
    test_app = FastAPI()
    test_app.include_router(console.router)
    with TestClient(test_app) as c:
        yield c


@pytest.fixture
def gated(client, monkeypatch):
    monkeypatch.setenv("EMBER_OAUTH_PASSWORD", "开门")
    monkeypatch.setenv("EMBER_OAUTH_ACCESS_TOKEN", "token-abc")
    monkeypatch.setenv("EMBER_REVIEW_TOKEN", "review-tok")
    client.cookies.clear()
    yield client
    client.cookies.clear()


BEARER = {"Authorization": "Bearer review-tok"}
MCP_BEARER = {"Authorization": "Bearer token-abc"}  # MCP 的 token，网页必须拒收


def _save(content="一条记忆", date="2026-07-01", **kw) -> int:
    return memories.save_memory(date=date, content=content, **kw)["id"]


def _acts(client) -> list[dict]:
    return client.get("/api/activity").json()["items"]


# ---------- 浏览 ----------


def test_browse_returns_full_content_and_pages(client):
    long = "很长的内容 " * 40
    _save(long, date="2026-07-02")
    for i in range(21):
        _save(f"记忆 {i}", space="ember")
    data = client.get("/api/memories?page_size=20").json()
    assert data["stats"]["total"] == 22  # 默认跨全库（管理视角）
    assert data["total_pages"] == 2
    assert data["items"][0]["content"] == long.strip()  # 日期最新排第一，不截断（首尾空白存时去掉）


def test_browse_q_number_space_tag_and_month_filters(client):
    a = _save("海边看日出", date="2026-08-03", tags="旅行")
    _save("工地日志", space="ember", date="2026-09-01")
    month_only = _save("八月只记得月份", date="2026-08")
    q = client.get("/api/memories?q=海边").json()
    assert [i["id"] for i in q["items"]] == [a]
    assert [i["id"] for i in client.get(f"/api/memories?q=%23{a}").json()["items"]] == [a]
    sp = client.get("/api/memories?space=ember").json()
    assert sp["stats"]["total"] == 1 and sp["items"][0]["space"] == "ember"
    assert [i["id"] for i in client.get("/api/memories?tag=旅行").json()["items"]] == [a]
    aug = client.get("/api/memories?date_from=2026-08&date_to=2026-08").json()
    assert {i["id"] for i in aug["items"]} == {a, month_only}
    assert aug["stats"]["all"] == 3
    assert client.get("/api/memories?date_from=最近").status_code == 400


def test_browse_carries_interval_status_and_history_count(client):
    mid = _save("进行中", start_date="2026-06-01")
    client.patch(f"/api/memories/{mid}", json={"content": "进行中，补一句"})
    item = client.get("/api/memories").json()["items"][0]
    assert item["interval_status"] == "ongoing" and item["history_count"] == 1


def test_month_chips_cover_the_whole_library_instead_of_the_first_page(client):
    with get_conn() as conn:
        conn.executemany("INSERT INTO memories (date, content) VALUES (?, ?)",
                         [("2026-10-18", f"十月 {i}") for i in range(201)] +
                         [(f"2026-{month:02d}", "更早的") for month in (9, 8, 7)])
        conn.execute("INSERT INTO memory_rings (memory_id, date, content) SELECT id, date, content FROM memories")
    first = client.get("/api/memories?page_size=200").json()
    assert len(first["items"]) == 200 and all(m["date"] == "2026-10-18" for m in first["items"])
    assert first["months"] == ["2026-10", "2026-09", "2026-08", "2026-07"]
    filtered = client.get("/api/memories?q=更早的&date_from=2026-07&date_to=2026-07").json()
    assert filtered["months"] == first["months"]  # 搜索和切月份不改变按钮的含义。
    assert [m["date"] for m in filtered["items"]] == ["2026-07"]


# ---------- 手写一条 ----------


def test_web_save_is_xuan_and_needs_content_and_date(client):
    resp = client.post("/api/memories", json={"date": "2026-10", "content": " 我自己写的 ", "tags": "心情小日记，新标签"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["memory"]["date"] == "2026-10" and body["memory"]["content"] == "我自己写的"
    assert body["memory"]["tags"] == "心情小日记,新标签"  # 编辑时直接新建标签
    assert _acts(client)[0]["actor"] == "xuan"
    assert client.post("/api/memories", json={"content": "没日期"}).status_code == 400
    assert client.post("/api/memories", json={"date": "下周", "content": "相对日期"}).status_code == 400
    assert client.post("/api/memories", json={"date": "2026-10-18", "content": "  "}).status_code == 400


def test_tags_endpoint_counts(client):
    _save("a", tags="旅行,旧档")
    _save("b", tags="旅行")
    assert client.get("/api/tags").json()["tags"][0] == {"tag": "旅行", "count": 2}


# ---------- 改 ----------


def test_patch_edits_in_place_and_keeps_previous_version(client):
    mid = _save("原文", tags="日常")
    resp = client.patch(f"/api/memories/{mid}", json={"content": "新文", "tags": "海边，sensitive、旅行"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["changed"]) == {"content", "tags"}
    assert body["memory"]["content"] == "新文" and body["memory"]["tags"] == "海边,sensitive,旅行"
    assert body["memory"]["updated_at"]
    assert [h["content"] for h in body["memory"]["history"]] == ["原文"]
    assert body["memory"]["history"][0]["replaced_by"] == "xuan"
    assert memories.get_memory(mid)["content"] == "新文"  # 立刻生效


def test_patch_ignores_non_whitelist_fields(client):
    mid = _save()
    resp = client.patch(
        f"/api/memories/{mid}",
        json={"tags": "x", "superseded_by": 999, "is_resolved": 1, "id": 12345, "deleted_at": "2026-01-01"},
    )
    assert resp.status_code == 200
    m = memories.get_memory(mid)
    assert m["superseded_by"] is None and m["is_resolved"] == 0 and m["deleted_at"] is None


def test_patch_blank_interval_clears_endpoint(client):
    mid = _save(start_date="2026-06-01", end_date="2026-12-31")
    assert client.patch(f"/api/memories/{mid}", json={"end_date": ""}).status_code == 200
    m = memories.get_memory(mid)
    assert m["end_date"] is None and m["interval_status"] == "ongoing"


def test_patch_rejects_bad_values(client):
    mid = _save()
    assert client.patch(f"/api/memories/{mid}", json={"tier": "vip"}).status_code == 400
    assert client.patch(f"/api/memories/{mid}", json={"content": "  "}).status_code == 400
    assert client.patch(f"/api/memories/{mid}", json={"date": "最近"}).status_code == 400
    assert client.patch(f"/api/memories/{mid}", json={"date": "2026-02-30"}).status_code == 400
    assert client.patch(f"/api/memories/{mid}", json={}).status_code == 400
    assert client.patch("/api/memories/9999", json={"tags": "x"}).status_code == 404


def test_patch_with_stale_expect_updated_at_conflicts(client):
    mid = _save("原文")
    seen = client.get(f"/api/memories/{mid}").json()["updated_at"]  # 还没改过：None
    memories.update_memory(mid, {"content": "小克刚改的"}, by="ke")
    resp = client.patch(f"/api/memories/{mid}", json={"content": "轩的版本", "expect_updated_at": seen})
    assert resp.status_code == 409
    assert memories.get_memory(mid)["content"] == "小克刚改的"
    fresh = client.get(f"/api/memories/{mid}").json()["updated_at"]
    ok = client.patch(f"/api/memories/{mid}", json={"content": "轩的版本", "expect_updated_at": fresh})
    assert ok.status_code == 200


# ---------- 删 / 恢复 ----------


def test_delete_hides_and_restore_brings_back(client):
    mid = _save("要删的")
    assert client.delete(f"/api/memories/{mid}").json()["deleted"] is True
    assert client.get(f"/api/memories/{mid}").status_code == 404
    assert client.get("/api/memories").json()["stats"]["total"] == 0
    assert client.delete(f"/api/memories/{mid}").status_code == 404  # 删过了
    row = _acts(client)[0]
    assert (row["action"], row["actor"], row["can"]) == ("delete", "xuan", "restore")
    assert client.post(f"/api/memories/{mid}/restore").status_code == 200
    assert memories.get_memory(mid)["content"] == "要删的"
    acts = _acts(client)
    assert acts[0]["action"] == "restore" and acts[1]["status"] == "已恢复"
    assert client.post(f"/api/memories/{mid}/restore").status_code == 404  # 没在删除状态


# ---------- 上一版：回退 / 清掉 ----------


def test_revert_from_detail_and_activity_state(client):
    mid = _save("第一版")
    client.patch(f"/api/memories/{mid}", json={"content": "第二版"})
    hist = client.get(f"/api/memories/{mid}").json()["history"]
    assert [h["content"] for h in hist] == ["第一版"]
    edit_row = _acts(client)[0]
    assert (edit_row["action"], edit_row["can"]) == ("edit", "revert")
    resp = client.post(f"/api/memories/{mid}/revert", json={"history_id": edit_row["history_id"]})
    assert resp.status_code == 200
    assert resp.json()["memory"]["content"] == "第一版"
    assert [h["content"] for h in resp.json()["memory"]["history"]] == ["第二版"]  # 回退也算一次改
    acts = _acts(client)
    assert acts[0]["action"] == "revert" and acts[0]["can"] == "revert"
    assert acts[1]["status"] == "已回退"
    gone = client.post(f"/api/memories/{mid}/revert", json={"history_id": edit_row["history_id"]})
    assert gone.status_code == 404
    assert client.post(f"/api/memories/{mid}/revert", json={}).status_code == 400


def test_keep_two_versions_and_pick_which_to_revert_to(client):
    assert client.patch("/api/settings", json={"history_keep": 2}).json()["history_keep"] == 2
    mid = _save("v1")
    for text in ("v2", "v3", "v4"):
        client.patch(f"/api/memories/{mid}", json={"content": text})
    hist = client.get(f"/api/memories/{mid}").json()["history"]
    assert [h["content"] for h in hist] == ["v3", "v2"]  # 只留 2 版，更早的扔掉
    resp = client.post(f"/api/memories/{mid}/revert", json={"history_id": hist[1]["id"]})
    assert resp.json()["memory"]["content"] == "v2"
    assert [h["content"] for h in resp.json()["memory"]["history"]] == ["v4", "v3"]
    # 调回 1 版：多的立刻扔掉
    client.patch("/api/settings", json={"history_keep": 1})
    assert [h["content"] for h in client.get(f"/api/memories/{mid}").json()["history"]] == ["v4"]


def test_clear_one_memory_history_and_clear_all(client):
    a, b = _save("a1"), _save("b1")
    client.patch(f"/api/memories/{a}", json={"content": "a2"})
    client.patch(f"/api/memories/{b}", json={"content": "b2"})
    assert client.delete(f"/api/memories/{a}/history").json() == {"cleared": 1}
    assert client.get(f"/api/memories/{a}").json()["history"] == []
    assert _acts(client)[1]["status"] == "上一版已清掉"  # a 的那次改动不能再一键回退
    assert client.delete("/api/history").json() == {"cleared": 1}
    assert client.get(f"/api/memories/{b}").json()["history"] == []


def test_older_edit_shows_superseded(client):
    mid = _save("v1")
    client.patch(f"/api/memories/{mid}", json={"content": "v2"})
    client.patch(f"/api/memories/{mid}", json={"content": "v3"})
    acts = _acts(client)
    assert acts[0]["can"] == "revert"
    assert acts[1]["status"] in ("上一版已清掉", "已被覆盖")  # 只留 1 版时 v1 已经挤掉了


# ---------- 设置 ----------


def test_settings_defaults_and_validation(client):
    assert client.get("/api/settings").json() == {
        "history_keep": 1, "activity_days": 30, "ke_can_delete": True, "ke_can_star": True,
    }
    assert client.patch("/api/settings", json={"history_keep": 3}).status_code == 400
    assert client.patch("/api/settings", json={"activity_days": 0}).status_code == 400
    assert client.patch("/api/settings", json={"ke_can_delete": "no"}).status_code == 400
    assert client.patch("/api/settings", json={"ke_can_star": 1}).status_code == 400
    assert client.patch("/api/settings", json={"nope": 1}).status_code == 400
    assert client.patch("/api/settings", json={}).status_code == 400
    ok = client.patch("/api/settings", json={"activity_days": 7, "ke_can_delete": False}).json()
    assert ok == {"history_keep": 1, "activity_days": 7, "ke_can_delete": False, "ke_can_star": True}
    assert client.patch("/api/settings", json={"ke_can_star": False}).json()["ke_can_star"] is False


def test_expired_delete_is_purged_for_real(client):
    keep = _save("被压的旧记忆")
    mid = memories.save_memory(
        date="2026-07-02", content="要真删的", quote="原话",
        links=[{"id": keep, "relation": "supersedes"}],
    )["id"]
    client.patch(f"/api/memories/{mid}", json={"content": "要真删的，改过"})
    client.delete(f"/api/memories/{mid}")
    with get_conn() as conn:  # 假装 31 天前删的
        conn.execute("UPDATE memories SET deleted_at = datetime('now','+8 hours','-31 days') WHERE id = ?", (mid,))
        conn.execute("UPDATE activity_log SET at = datetime('now','+8 hours','-31 days') WHERE memory_id = ?", (mid,))
    client.get("/api/activity")  # 打开最近动静时顺手清
    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memories WHERE id = ?", (mid,)).fetchone()[0] == 0
        for table, col in (("memory_sources", "memory_id"), ("memory_history", "memory_id"),
                           ("memory_edges", "from_id"), ("activity_log", "memory_id")):
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (mid,)).fetchone()[0] == 0
    assert memories.get_memory(keep)["superseded_by"] is None  # 压制松开


def test_shorter_window_purges_older_deletes_immediately(client):
    mid = _save("十天前删的")
    client.delete(f"/api/memories/{mid}")
    with get_conn() as conn:
        conn.execute("UPDATE memories SET deleted_at = datetime('now','+8 hours','-10 days') WHERE id = ?", (mid,))
    client.patch("/api/settings", json={"activity_days": 7})
    assert memories.get_memory(mid, include_deleted=True) is None


# ---------- 门禁 ----------


def test_gate_rejects_anonymous_and_mcp_token(gated):
    mid = _save()
    for method, url, body in (
        ("get", "/api/memories", None), ("get", f"/api/memories/{mid}", None),
        ("patch", f"/api/memories/{mid}", {"tags": "x"}), ("delete", f"/api/memories/{mid}", None),
        ("post", f"/api/memories/{mid}/restore", None), ("post", f"/api/memories/{mid}/revert", {"history_id": 1}),
        ("delete", f"/api/memories/{mid}/history", None), ("delete", "/api/history", None),
        ("get", "/api/activity", None), ("get", "/api/settings", None), ("patch", "/api/settings", {"history_keep": 2}),
        ("get", "/api/tags", None), ("post", "/api/memories", {"date": "2026-10-01", "content": "x"}),
    ):
        for headers in ({}, MCP_BEARER):
            kwargs = {"headers": headers} | ({"json": body} if body is not None else {})
            assert getattr(gated, method)(url, **kwargs).status_code == 401, (method, url, headers)
    assert gated.get("/").status_code == 200 and "口令" in gated.get("/").text  # 登录页


def test_script_token_can_only_save_and_is_logged_as_ke(gated):
    mid = _save()
    resp = gated.post("/api/memories", json={"memories": [
        {"date": "2026-05-01", "content": "提取一", "source_ref": "logs/x.txt#3", "quote": "原话"},
        {"date": "2026-05", "content": "提取二"},
    ]}, headers=BEARER)
    assert resp.status_code == 201 and resp.json()["saved"] == 2
    assert memories.get_memory(resp.json()["ids"][0])["sources"][0]["quote"] == "原话"
    bad = gated.post("/api/memories", json={"memories": [{"date": "2026-05-01", "content": "好"}, {"content": "缺日期"}]},
                     headers=BEARER)
    assert bad.status_code == 400 and "第 2 条" in bad.json()["error_description"]
    assert memories.get_status()["memories"] == 3  # 坏一条整批不写
    assert gated.patch(f"/api/memories/{mid}", json={"tags": "x"}, headers=BEARER).status_code == 401
    assert gated.delete(f"/api/memories/{mid}", headers=BEARER).status_code == 401
    assert gated.get("/api/activity", headers=BEARER).status_code == 401
    with get_conn() as conn:
        actors = [r[0] for r in conn.execute("SELECT actor FROM activity_log ORDER BY id")]
    assert actors == ["ke", "ke", "ke"]  # _save 默认也是小克


def test_login_sets_root_cookie_and_old_address_redirects(gated):
    assert gated.post("/login", data={"password": "错的"}).status_code == 403
    resp = gated.post("/login", data={"password": "开门"}, follow_redirects=False)
    assert resp.status_code == 302 and resp.headers["location"] == "/"
    cookie = resp.headers["set-cookie"]
    assert "Path=/;" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    gated.cookies.set(console.COOKIE_NAME, resp.cookies[console.COOKIE_NAME])
    assert gated.get("/api/settings").status_code == 200
    assert gated.get("/review", follow_redirects=False).headers["location"] == "/"


def test_deleting_a_superseder_releases_the_old_memory_and_restore_reapplies(client):
    old = _save("旧计划")
    new = memories.save_memory(date="2026-07-02", content="新计划", links=[{"id": old, "relation": "supersedes"}])["id"]
    assert memories.get_memory(old)["superseded_by"] == new
    client.delete(f"/api/memories/{new}")
    assert memories.get_memory(old)["superseded_by"] is None  # 藏起来的不再压着它
    assert "superseded_by" not in memories.search_memories("旧计划")[0]
    client.post(f"/api/memories/{new}/restore")
    assert memories.get_memory(old)["superseded_by"] == new


def test_purging_one_superseder_keeps_the_other(client):
    old = _save("最早的计划")
    a = memories.save_memory(date="2026-07-02", content="改过一次", links=[{"id": old, "relation": "supersedes"}])["id"]
    b = memories.save_memory(date="2026-07-03", content="又改一次", links=[{"id": old, "relation": "supersedes"}])["id"]
    assert memories.get_memory(old)["superseded_by"] == b
    client.delete(f"/api/memories/{b}")
    assert memories.get_memory(old)["superseded_by"] == a
    with get_conn() as conn:
        conn.execute("UPDATE memories SET deleted_at = datetime('now','+8 hours','-31 days') WHERE id = ?", (b,))
    memories.purge_expired()
    assert memories.get_memory(old)["superseded_by"] == a


def test_noop_save_of_legacy_row_is_not_an_edit(client):
    mid = _save("原话")
    with get_conn() as conn:  # 老代码存的原样：首尾空白、日期没补零、标签带空格
        conn.execute("UPDATE memories SET content = '原话\n', topic = '海边 ', date = '2026-4-5', tags = '日常, 旅行' WHERE id = ?", (mid,))
    shown = client.get(f"/api/memories/{mid}").json()
    body = {k: shown[k] for k in ("date", "content", "tags", "topic", "tier", "space", "start_date", "end_date")}
    assert client.patch(f"/api/memories/{mid}", json=body).json()["changed"] == []
    assert client.get(f"/api/memories/{mid}").json()["history"] == []


def test_conflict_detected_within_the_same_millisecond_and_on_revert(client, monkeypatch):
    # 固定数据库时钟，保证两次写确实落在同一毫秒；不靠机器速度或 sleep。
    monkeypatch.setattr(memories, "NOW_MS", "'2026-10-18 20:00:00.123'")
    client.patch("/api/settings", json={"history_keep": 2})
    mid = _save("v1")
    first = client.patch(f"/api/memories/{mid}", json={"content": "v2"}).json()["memory"]
    seen = _acts(client)[0]
    assert seen["updated_at"] == first["updated_at"]
    second = memories.update_memory(mid, {"content": "小克同一毫秒又改了"}, by="ke")["memory"]
    assert second["updated_at"] > first["updated_at"]
    assert client.patch(f"/api/memories/{mid}", json={"content": "v3", "expect_updated_at": first["updated_at"]}).status_code == 409
    stale = client.post(f"/api/memories/{mid}/revert", json={"history_id": seen["history_id"], "expect_updated_at": seen["updated_at"]})
    assert stale.status_code == 409
    assert memories.get_memory(mid)["content"] == "小克同一毫秒又改了"
    fresh = client.post(f"/api/memories/{mid}/revert", json={"history_id": seen["history_id"], "expect_updated_at": second["updated_at"]})
    assert fresh.status_code == 200 and fresh.json()["memory"]["content"] == "v1"
    assert fresh.json()["memory"]["updated_at"] > second["updated_at"]


def test_bad_inputs_are_4xx_not_500(client):
    mid = _save()
    huge = 10**20
    assert client.post("/api/memories", json={"date": "2026-10-01", "content": "x", "source_ref": {"f": 1}}).status_code == 400
    assert client.post("/api/memories", json={"date": "2026-10-01", "content": "x", "links": [{"id": huge}]}).status_code == 400
    assert client.post("/api/memories", json={"date": 20261001, "content": "x"}).status_code == 400
    assert client.patch(f"/api/memories/{mid}", json={"tags": ["a", "b"]}).status_code == 400
    for method, url in (("get", f"/api/memories/{huge}"), ("delete", f"/api/memories/{huge}"),
                        ("post", f"/api/memories/{huge}/restore"), ("get", f"/api/memories?page={huge}"),
                        ("delete", f"/api/memories/{mid}/history?history_id={huge}")):
        assert getattr(client, method)(url).status_code == 422, url
    assert client.post(f"/api/memories/{mid}/revert", json={"history_id": huge}).status_code == 400
    deep = client.patch(f"/api/memories/{mid}", content="[" * 100000, headers={"content-type": "application/json"})
    assert deep.status_code == 400


def test_reads_do_not_wait_for_a_writer(client):
    _save("在列表里")
    writer = get_conn()
    writer.execute("BEGIN IMMEDIATE")  # 模拟小克存记忆时卡在算语义指纹
    try:
        assert client.get("/api/memories").status_code == 200
        assert client.get("/api/activity").status_code == 200
    finally:
        writer.rollback()
        writer.close()


def test_status_ignores_hidden_memories_edges_and_sources(client):
    a = _save("留着")
    b = memories.save_memory(date="2026-07-02", content="要删", quote="原话", links=[{"id": a, "relation": "related"}])["id"]
    client.delete(f"/api/memories/{b}")
    status = memories.get_status()
    assert (status["memories"], status["edges"], status["sources"]) == (1, 0, 0)


def test_saving_normalized_form_repairs_legacy_values_without_an_edit(client):
    mid = _save("原话", tags="日常")
    with get_conn() as conn:
        conn.execute("UPDATE memories SET date = '2026-8-5', tags = '日常, 旅行', topic = NULL WHERE id = ?", (mid,))
    resp = client.patch(f"/api/memories/{mid}", json={"date": "2026-08-05", "tags": "日常,旅行", "topic": ""})
    assert resp.json()["changed"] == []
    m = memories.get_memory(mid)
    assert (m["date"], m["tags"], m["topic"]) == ("2026-08-05", "日常,旅行", "")
    assert client.get(f"/api/memories/{mid}").json()["history"] == []
    assert [i["id"] for i in client.get("/api/memories?tag=旅行").json()["items"]] == [mid]


def test_month_filter_still_finds_legacy_unpadded_days_and_rejects_inverted_ranges(client):
    mid = _save("老格式日期")
    with get_conn() as conn:  # 老数据搬家时日期原样抄进第一圈，没补零的也照抄
        conn.execute("UPDATE memories SET date = '2026-08-5' WHERE id = ?", (mid,))
        conn.execute("UPDATE memory_rings SET date = '2026-08-5' WHERE memory_id = ?", (mid,))
    assert [i["id"] for i in client.get("/api/memories?date_from=2026-08&date_to=2026-08").json()["items"]] == [mid]
    assert client.get("/api/memories?date_from=2026-08-20&date_to=2026-08-10").status_code == 400


def test_numeric_queries_never_500(client):
    mid = _save("卡号 12345678901234567890 记在这")
    assert [i["id"] for i in client.get("/api/memories?q=12345678901234567890").json()["items"]] == [mid]
    assert client.get("/api/memories?q=²").status_code == 200


def test_reads_skip_a_due_purge_instead_of_waiting(client):
    import time
    mid = _save("过期删掉的")
    client.delete(f"/api/memories/{mid}")
    with get_conn() as conn:
        conn.execute("UPDATE memories SET deleted_at = datetime('now','+8 hours','-31 days') WHERE id = ?", (mid,))
    writer = get_conn()
    writer.execute("BEGIN IMMEDIATE")
    try:
        start = time.monotonic()
        assert client.get("/api/memories").status_code == 200
        assert time.monotonic() - start < 2
    finally:
        writer.rollback()
        writer.close()


def test_root_serves_the_page_without_caching(client):
    resp = client.get("/")
    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    assert "/static/console/console.js" in resp.text and 'id="brand"' in resp.text


# ---------- 年轮卡（PR4）：混着来的月份、⭐ / 饼、补圈 / 改圈 / 删圈 ----------


@pytest.fixture
def today(monkeypatch):
    monkeypatch.setattr(memories, "_today", lambda: "2026-10-20")


def test_month_filter_mixes_cards_by_any_ring(client, today):
    cake = _save("编的：在甜品店说好学做抹茶蛋糕", date="2026-08-09", title="学做抹茶蛋糕")
    memories.add_ring(cake, "2026-09-06", "编的：第一次做，塌了一半")
    memories.add_ring(cake, "2026-10-18", "编的：第三次成功了")
    walk = _save("编的：九月底散步", date="2026-09-28")
    whole = client.get("/api/memories").json()
    assert [i["id"] for i in whole["items"]] == [cake, walk]  # 全部：只出现一次，按最新一圈
    assert whole["months"] == ["2026-10", "2026-09", "2026-08"]
    assert [r["date"] for r in whole["items"][0]["rings"]] == ["2026-08-09", "2026-09-06", "2026-10-18"]
    assert whole["items"][0]["drawer"] == "日常"
    sep = client.get("/api/memories?date_from=2026-09&date_to=2026-09").json()
    assert [i["id"] for i in sep["items"]] == [walk, cake]  # 按九月那一圈排：09-28 在 09-06 前面
    aug = client.get("/api/memories?date_to=2026-08").json()  # 「更早」
    assert [i["id"] for i in aug["items"]] == [cake]
    assert [i["id"] for i in client.get("/api/memories?q=抹茶蛋糕").json()["items"]] == [cake]  # 标题也搜
    assert [i["id"] for i in client.get("/api/memories?q=塌了").json()["items"]] == [cake]  # 每一圈也搜


def test_star_and_pie_filters(client):
    pie = _save("编的：约好一起读完一本书", tags="连理饼,读书")
    eaten = _save("编的：阳台挂上了小灯", tags="筑巢饼,吃到了")
    dough = _save("编的：有点想学游泳", tags="面团")
    pumpkin = _save("编的：下午吃了南瓜饼", tags="吃的")
    assert [i["id"] for i in client.get("/api/memories?pie=1").json()["items"]] == [eaten, pie]
    assert client.post(f"/api/memories/{dough}/star", json={"starred": True}).json() == {"id": dough, "starred": True}
    client.post(f"/api/memories/{pie}/star", json={"starred": True})
    assert [i["id"] for i in client.get("/api/memories?starred=1").json()["items"]] == [dough, pie]
    assert [i["id"] for i in client.get("/api/memories?starred=1&pie=1").json()["items"]] == [pie]
    assert [i["id"] for i in client.get("/api/memories?q=南瓜饼").json()["items"]] == [pumpkin]
    assert client.post(f"/api/memories/{pie}/star", json={"starred": "yes"}).status_code == 400
    assert client.post("/api/memories/99999/star", json={"starred": True}).status_code == 404
    assert _acts(client)[0]["action"] == "save"  # 点⭐不进最近动静


def test_ring_endpoints(client, today):
    mid = _save("编的：约好等天凉了去海边", date="2026-08-30", title="去海边住一晚")
    seen = client.get(f"/api/memories/{mid}").json()
    assert [r["content"] for r in seen["rings"]] == ["编的：约好等天凉了去海边"]
    resp = client.post(f"/api/memories/{mid}/rings", json={
        "date": "2026-10-16", "content": " 编的：存了两家民宿 ", "expect_updated_at": seen["updated_at"]})
    assert resp.status_code == 201
    body = resp.json()
    ring_id = body["ring_id"]
    assert body["memory"]["date"] == "2026-10-16" and len(body["memory"]["history"]) == 1
    assert body["memory"]["rings"][-1] == {**body["memory"]["rings"][-1], "id": ring_id, "content": "编的：存了两家民宿"}
    stale = client.patch(f"/api/memories/{mid}/rings/{ring_id}", json={
        "date": "2026-10-17", "content": "编的：改", "expect_updated_at": seen["updated_at"]})
    assert stale.status_code == 409
    fresh = body["memory"]["updated_at"]
    edited = client.patch(f"/api/memories/{mid}/rings/{ring_id}", json={
        "date": "2026-10", "content": "编的：十月存了两家", "expect_updated_at": fresh}).json()
    assert edited["changed"] is True and edited["memory"]["rings"][-1]["date"] == "2026-10"
    assert client.post(f"/api/memories/{mid}/rings", json={"date": "2026-12-01", "content": "编的：以后"}).status_code == 400
    assert client.post(f"/api/memories/{mid}/rings", json=["不是对象"]).status_code == 400
    assert client.patch(f"/api/memories/{mid}/rings/99999", json={"date": "2026-10-01", "content": "编的"}).status_code == 404
    gone = client.request("DELETE", f"/api/memories/{mid}/rings/{ring_id}",
                          json={"expect_updated_at": edited["memory"]["updated_at"]})
    assert gone.status_code == 200 and len(gone.json()["memory"]["rings"]) == 1
    first = gone.json()["memory"]["rings"][0]["id"]
    last = client.delete(f"/api/memories/{mid}/rings/{first}")
    assert last.status_code == 400 and "最后一圈" in last.json()["error_description"]
    act = _acts(client)[0]
    assert (act["action"], act["can"], act["title"]) == ("edit", "revert", "去海边住一晚")


def test_ring_and_star_endpoints_need_the_browser_login(gated):
    mid = _save("编的：一张卡")
    for method, path, body in (
        ("POST", f"/api/memories/{mid}/rings", {"date": "2026-07-02", "content": "编的"}),
        ("PATCH", f"/api/memories/{mid}/rings/1", {"date": "2026-07-02", "content": "编的"}),
        ("DELETE", f"/api/memories/{mid}/rings/1", None),
        ("POST", f"/api/memories/{mid}/star", {"starred": True}),
    ):
        for headers in ({}, BEARER, MCP_BEARER):
            assert gated.request(method, path, json=body, headers=headers).status_code == 401
