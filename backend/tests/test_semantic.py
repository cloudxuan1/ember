"""V5 语义检索：hybrid 融合、软失败降级、换模型失效与重建。

不打真 API——用假指纹函数模拟一个微型语义空间：
同轴的词（难过/伤心/眼泪/哭）指纹相近，跨轴的相远。
"""

import pytest

from app import embeddings, memories
from app.db import get_conn, init_db

AXES = {
    "难过": 0, "伤心": 0, "眼泪": 0, "哭": 0,
    "开心": 1, "高兴": 1,
    "部署": 2, "服务器": 2,
}


def fake_embed(texts, timeout=None):
    out = []
    for t in texts:
        v = [0.0, 0.0, 0.0, 1e-6]  # 尾轴垫底值防零向量（余弦距离对零向量无定义）
        for word, axis in AXES.items():
            if word in t:
                v[axis] += 1.0
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
    init_db()


def _emb_rows():
    with get_conn() as conn:
        return conn.execute("SELECT memory_id, model, dim FROM memory_embeddings").fetchall()


def test_save_stores_fingerprint():
    saved = memories.save_memory(date="2026-07-05", content="今天很难过", tags="心情")
    assert saved["embedded"] is True
    rows = _emb_rows()
    assert len(rows) == 1
    assert rows[0]["model"] == "test-model-a"
    assert rows[0]["dim"] == 4


def test_save_survives_embedding_failure(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts", boom)
    saved = memories.save_memory(date="2026-07-05", content="难过的一天")
    assert saved["saved"] is True
    assert saved["embedded"] is False
    assert _emb_rows() == []
    # 记忆本体在，关键词照常能搜到
    assert [r["id"] for r in memories.search_memories("难过")] == [saved["id"]]


def test_semantic_recall_synonyms():
    """验收标准本尊：搜"难过"召回字面零重合的"眼泪在眼眶里打转"。"""
    hit = memories.save_memory(date="2026-05-29", content="那晚眼泪在眼眶里打转")
    memories.save_memory(date="2026-07-01", content="服务器部署完成")
    results = memories.search_memories("难过")
    assert results[0]["id"] == hit["id"]


def test_rrf_literal_match_ranks_first():
    """两腿都命中的排最前：字面带"难过"的 > 只有语义相近的。"""
    lit = memories.save_memory(date="2026-07-02", content="今天很难过")
    sem = memories.save_memory(date="2026-07-03", content="眼泪在眼眶里打转")
    ids = [r["id"] for r in memories.search_memories("难过")]
    assert ids.index(lit["id"]) < ids.index(sem["id"])


def test_query_failure_degrades_to_keyword(monkeypatch):
    saved = memories.save_memory(date="2026-07-05", content="今天很难过")
    monkeypatch.setattr(embeddings, "embed_texts", boom)
    assert [r["id"] for r in memories.search_memories("难过")] == [saved["id"]]


def test_vector_space_filter():
    p = memories.save_memory(date="2026-07-01", content="眼泪打转", space="personal")
    memories.save_memory(date="2026-07-02", content="哭了一场", space="ember")
    ids = [r["id"] for r in memories.search_memories("难过", space="personal")]
    assert ids == [p["id"]]


def test_model_switch_invalidates_then_rebuild(monkeypatch):
    """换模型 = 改 .env + 跑 rebuild：中间态安全降级，重建后语义腿复活。"""
    a = memories.save_memory(date="2026-05-29", content="那晚眼泪在眼眶里打转")
    monkeypatch.setenv("EMBEDDING_MODEL", "test-model-b")
    # 旧模型指纹自动失效：语义腿空转，纯关键词搜不到同义词
    assert memories.search_memories("难过") == []
    stats = embeddings.rebuild_embeddings()
    assert stats["model"] == "test-model-b"
    assert stats["embedded_now"] == 1
    assert [r["id"] for r in memories.search_memories("难过")] == [a["id"]]
    assert {r["model"] for r in _emb_rows()} == {"test-model-b"}


def test_rebuild_backfills_missing(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts", boom)
    saved = memories.save_memory(date="2026-07-05", content="眼泪在眼眶里打转")
    assert _emb_rows() == []
    monkeypatch.setattr(embeddings, "embed_texts", fake_embed)
    stats = embeddings.rebuild_embeddings()
    assert stats["embedded_now"] == 1
    assert [r["id"] for r in memories.search_memories("难过")] == [saved["id"]]


def test_delete_cascades_fingerprint():
    saved = memories.save_memory(date="2026-07-05", content="临时记忆")
    assert len(_emb_rows()) == 1
    with get_conn() as conn:
        conn.execute("DELETE FROM memories WHERE id = ?", (saved["id"],))
    assert _emb_rows() == []


def test_rebuild_works_as_first_entry_in_fresh_process(monkeypatch):
    """P1 回归（codex 审出）：python -m app.embeddings 独立跑时 rebuild 是
    进程里第一个入口，没人开过连接、vec 状态未探测——不得误报扩展不可用。"""
    import app.db as db

    monkeypatch.setattr(embeddings, "embed_texts", boom)
    saved = memories.save_memory(date="2026-07-05", content="眼泪在眼眶里打转")
    monkeypatch.setattr(embeddings, "embed_texts", fake_embed)
    monkeypatch.setattr(db, "_vec_status", None)  # 模拟全新进程
    stats = embeddings.rebuild_embeddings()
    assert stats["embedded_now"] == 1
    assert [r["id"] for r in memories.search_memories("难过")] == [saved["id"]]


def test_disabled_without_key_keeps_old_behavior(monkeypatch):
    """没配 key = 语义腿关闭：save 结果不带 embedded 字段，搜索纯关键词。"""
    monkeypatch.delenv("EMBEDDING_API_KEY")
    saved = memories.save_memory(date="2026-07-05", content="今天很难过")
    assert "embedded" not in saved
    assert _emb_rows() == []
    assert [r["id"] for r in memories.search_memories("难过")] == [saved["id"]]
