"""语义指纹（V5）：写入时生成、软失败、可重建；模型永不写死在代码里。

- API / 模型全部来自环境变量（OpenAI 兼容 /embeddings 接口）：换模型或换提供商
  只改 .env，然后跑 `python -m app.embeddings` 重算。
- 指纹存普通表（不用 vec0 虚拟表——维度是数据不是表结构），每行带 model 列：
  搜索只用与当前配置同模型的行，换模型后旧指纹自动失效，安全降级纯关键词。
- 软失败：API 挂了保存照常入库（指纹缺席，rebuild 补）；查询挂了降级关键词。
"""

import os
import struct
import sys

import httpx

from app.db import get_conn, vec_available

EMBED_TIMEOUT = 15.0  # 单条/查询；保存在写事务里调 API，超时要短
REBUILD_TIMEOUT = 60.0
REBUILD_BATCH = 32


def api_base() -> str:
    return os.environ.get("EMBEDDING_API_BASE", "https://openrouter.ai/api/v1").rstrip("/")


def current_model() -> str:
    return os.environ.get("EMBEDDING_MODEL", "")


def enabled() -> bool:
    """key 和模型都配了才开语义腿；没配 = 纯关键词模式，一切照常。"""
    return bool(os.environ.get("EMBEDDING_API_KEY") and current_model())


def embed_texts(texts: list[str], timeout: float = EMBED_TIMEOUT) -> list[list[float]]:
    resp = httpx.post(
        f"{api_base()}/embeddings",
        headers={"Authorization": f"Bearer {os.environ['EMBEDDING_API_KEY']}"},
        json={"model": current_model(), "input": texts},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = sorted(resp.json()["data"], key=lambda d: d["index"])
    return [d["embedding"] for d in data]


def memory_text(content: str, topic: str = "", tags: str = "") -> str:
    """拼给模型算指纹的文本：主题和标签是语义的一部分，一起进指纹。"""
    return "\n".join(p for p in (topic, tags, content) if p)


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _store(conn, memory_id: int, vector: list[float]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO memory_embeddings (memory_id, model, dim, embedding)
           VALUES (?, ?, ?, ?)""",
        (memory_id, current_model(), len(vector), _pack(vector)),
    )


def embed_memory(conn, memory_id: int, content: str, topic: str = "", tags: str = "") -> bool:
    """在调用方事务里写指纹（不提交）。任何失败都吞掉——记忆本体照常入库，
    缺席的指纹由 rebuild_embeddings() 事后补。"""
    if not (enabled() and vec_available()):
        return False
    try:
        vector = embed_texts([memory_text(content, topic, tags)])[0]
        _store(conn, memory_id, vector)
        return True
    except Exception as e:
        print(f"[embed] memory {memory_id} 指纹生成失败（记忆已保存，rebuild 可补）: {e}", flush=True)
        return False


def vector_search(query: str, space: str | None = None, k: int = 40) -> list[int]:
    """语义腿：返回按余弦距离升序的 memory_id 列表。

    API / 扩展不可用、或库里还没有当前模型的指纹 → 返回 []，
    调用方（search_memories）自动降级纯关键词。
    """
    if not (enabled() and vec_available()):
        return []
    try:
        qvec = embed_texts([query])[0]
    except Exception as e:
        print(f"[embed] 查询指纹失败，本次降级纯关键词: {e}", flush=True)
        return []
    sql = """SELECT e.memory_id
             FROM memory_embeddings e JOIN memories m ON m.id = e.memory_id
             WHERE e.model = ? AND e.dim = ?"""
    params: list = [current_model(), len(qvec)]
    if space:
        sql += " AND m.space = ?"
        params.append(space)
    sql += " ORDER BY vec_distance_cosine(e.embedding, ?) LIMIT ?"
    params += [_pack(qvec), k]
    with get_conn() as conn:
        return [r["memory_id"] for r in conn.execute(sql, params).fetchall()]


def rebuild_embeddings(batch_size: int = REBUILD_BATCH) -> dict:
    """给缺席或过时（≠当前模型）的记忆补算指纹。

    逐批提交——中断重跑不漏不重（锚点推进的老规矩）。换模型的完整流程：
    改 .env 的 EMBEDDING_MODEL → 重启 → 跑本函数（或 python -m app.embeddings）。
    """
    if not enabled():
        raise RuntimeError("EMBEDDING_API_KEY / EMBEDDING_MODEL 没配，语义检索未启用")
    if not vec_available():
        raise RuntimeError("sqlite-vec 扩展加载失败，无法重建")
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT m.id, m.content, m.topic, m.tags FROM memories m
               LEFT JOIN memory_embeddings e ON e.memory_id = m.id
               WHERE e.memory_id IS NULL OR e.model != ?
               ORDER BY m.id""",
            (current_model(),),
        ).fetchall()
        total_missing, embedded = len(rows), 0
        for i in range(0, total_missing, batch_size):
            batch = rows[i : i + batch_size]
            vectors = embed_texts(
                [memory_text(r["content"], r["topic"], r["tags"]) for r in batch],
                timeout=REBUILD_TIMEOUT,
            )
            for row, vector in zip(batch, vectors):
                _store(conn, row["id"], vector)
            conn.commit()  # 逐批落盘，断点续跑的锚点
            embedded += len(batch)
        up_to_date = conn.execute(
            "SELECT COUNT(*) FROM memory_embeddings WHERE model = ?", (current_model(),)
        ).fetchone()[0]
    return {
        "model": current_model(),
        "embedded_now": embedded,
        "total_with_current_model": up_to_date,
    }


if __name__ == "__main__":  # VPS 上：docker exec ember python -m app.embeddings
    print(rebuild_embeddings(), file=sys.stdout)
