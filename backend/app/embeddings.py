"""语义指纹（V5）：写入时生成、软失败、可重建；模型永不写死在代码里。

- API / 模型全部来自环境变量（OpenAI 兼容 /embeddings 接口）：换模型或换提供商
  只改 .env，然后跑 `python -m app.embeddings` 重算。
- 指纹存普通表（不用 vec0 虚拟表——维度是数据不是表结构），每行带 model 列：
  搜索只用与当前配置同模型的行，换模型后旧指纹自动失效，安全降级纯关键词。
- 软失败：API 挂了保存照常入库（指纹缺席，rebuild 补）；查询挂了降级关键词。

年轮卡 PR2 起按圈算（ring_embeddings）：每一圈一份，太长的圈切成几段、一段一份（chunk_spans）；
搜的时候一张卡取最像的那一段，结果里带上是哪一圈。整条指纹（memory_embeddings）照常维护当退路：
还没有按圈指纹的卡（上线后补算之前）用它；PR5 再删。
"""

import hashlib
import json
import math
import os
import re
import struct
import sys

import httpx

from app.db import get_conn, vec_available

EMBED_TIMEOUT = 15.0  # 单条/查询；保存在写事务里调 API，超时要短
REBUILD_TIMEOUT = 60.0
REBUILD_BATCH = 32

# 分段（2026-10 调研：中文检索 200–400 token 一段最稳，段越小一句心情越不容易被冲淡）：
# 不超过 CHUNK_MAX 字的圈整圈一段；更长的按空行分段，相邻短段合起来不超过 CHUNK_TARGET；
# 单段太长按句号这类断句处切，再不行按逗号，最后才硬切；同一段里切开的，下一块开头带上
# 上一块的最后一句（不超过 CHUNK_OVERLAP 字），免得一句话被切在中间；不到 CHUNK_MIN 字的碎块并进邻块。
CHUNK_TARGET = 250
CHUNK_MAX = 400
CHUNK_OVERLAP = 60
CHUNK_MIN = 30
HARD_OVERLAP = 30  # 连逗号都没有、只能硬切时的重叠

_LEVELS = (  # 断点从粗到细：空行 → 句末（带后引号）/ 换行 → 逗号
    re.compile(r"\n[^\S\n]*\n\s*"),  # 中间那一行只有空白（含全角空格、不换行空格）也算空行
    re.compile(r"[。！？!?…；;]+[”’」』）)\]】》]*\"?|\n"),  # 半角引号只带一个：后面紧跟的可能是下一句的开引号
    re.compile(r"[，、,：:]+[”’」』）)\]]*"),
)


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
    if [d["index"] for d in data] != list(range(len(texts))):  # 少给 / 错位：当失败处理，别把指纹配错段
        raise RuntimeError(f"embedding 返回了 {len(data)} 条，要的是 {len(texts)} 条")
    vectors = [d["embedding"] for d in data]
    try:  # 整批先验过再交给写库：坏向量走现有软失败，不能把记忆保存一起回滚
        dim = None
        for vector in vectors:
            if not isinstance(vector, list) or not vector or (dim is not None and len(vector) != dim):
                raise ValueError("向量格式或维度不一致")
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
                raise ValueError("向量必须是有限数值")
            _pack(vector)  # float32 溢出也算坏回包，别等存到一半才发现
            dim = len(vector)
    except (TypeError, ValueError, OverflowError, struct.error) as err:
        raise RuntimeError("embedding 返回了无效向量，整批未写入") from err
    return vectors


def memory_text(content: str, topic: str = "", tags: str = "") -> str:
    """拼给模型算整条指纹的文本：主题和标签是语义的一部分，一起进指纹。"""
    return "\n".join(p for p in (topic, tags, content) if p)


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _store(conn, memory_id: int, vector: list[float]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO memory_embeddings (memory_id, model, dim, embedding)
           VALUES (?, ?, ?, ?)""",
        (memory_id, current_model(), len(vector), _pack(vector)),
    )


# ---------- 按圈分段 ----------


def _strip(text: str, s: int, e: int) -> tuple[int, int] | None:
    """[s, e) 去掉两头空白；全是空白返回 None。"""
    while s < e and text[s].isspace():
        s += 1
    while e > s and text[e - 1].isspace():
        e -= 1
    return (s, e) if s < e else None


def _cut(text: str, s: int, e: int, pattern: re.Pattern) -> list[tuple[int, int]]:
    """按一级断点把 [s, e) 切成小块，断点（标点）留在前一块末尾。"""
    out, pos = [], s
    for m in pattern.finditer(text, s, e):
        piece = _strip(text, pos, m.end())
        if piece:
            out.append(piece)
        pos = m.end()
    piece = _strip(text, pos, e)
    if piece:
        out.append(piece)
    return out


def _atoms(text: str, s: int, e: int, level: int, para: int) -> list[tuple[int, int, int, bool]]:
    """把一段切成不超过 CHUNK_MAX 的小块：(起, 止, 第几个空行段, 是不是硬切的)。"""
    if e - s <= CHUNK_MAX:
        return [(s, e, para, False)]
    if level == len(_LEVELS):  # 连逗号都没有：每 CHUNK_TARGET 字硬切一刀，前后重叠一点
        out, pos = [], s
        while True:
            end = min(pos + CHUNK_TARGET, e)
            if e - end < CHUNK_MIN:
                end = e
            piece = _strip(text, pos, end)
            if piece:
                out.append((*piece, para, True))
            if end == e:
                return out
            pos = end - HARD_OVERLAP
    out = []
    for i, (ps, pe) in enumerate(_cut(text, s, e, _LEVELS[level])):
        out += _atoms(text, ps, pe, level + 1, i if level == 0 else para)
    return out


def chunk_spans(text: str) -> list[tuple[int, int]]:
    """一圈正文切成几段，返回每段在正文里的起止（左闭右开，下标是字符位置）。规矩见文件头的分段常量。"""
    whole = _strip(text, 0, len(text))
    if whole is None:
        return []
    if whole[1] - whole[0] <= CHUNK_MAX:
        return [whole]
    chunks: list[list[int]] = []
    last = None  # 上一个装进去的小块
    for atom in _atoms(text, whole[0], whole[1], 0, 0):
        s, e, para, hard = atom
        if chunks and e - chunks[-1][0] <= CHUNK_TARGET:
            chunks[-1][1] = e
        else:
            start = s
            if (last is not None and last[2] == para and not last[3] and not hard
                    and last[1] - last[0] <= CHUNK_OVERLAP and e - last[0] <= CHUNK_MAX):
                start = last[0]  # 同一段里切开的：带上上一块最后一句
            if chunks and start <= chunks[-1][0]:  # 上一块就只有那一句：直接接上，别留一块被下一块整个盖住的
                chunks[-1][1] = e
            else:
                chunks.append([start, e])
        last = atom
    merged: list[list[int]] = []
    for c in chunks:  # 碎块并进前一块；第一块太碎就并进后一块（中间隔着一大片空白的不并）
        tiny = c[1] - c[0] < CHUNK_MIN or (merged and merged[-1][1] - merged[-1][0] < CHUNK_MIN)
        if merged and tiny and max(c[1], merged[-1][1]) - merged[-1][0] <= CHUNK_MAX + CHUNK_MIN:
            merged[-1][1] = max(merged[-1][1], c[1])
        else:
            merged.append(c)
    return [(s, e) for s, e in merged]


def ring_text(title: str | None, tags: str | None, piece: str) -> str:
    """拼给模型算这一段指纹的文本：标题和便利贴放在每段前面当上下文（心情便利贴也是意思的一部分）。"""
    from app.memories import normalize_tags  # memories 引用本模块，延迟导入免循环

    return "\n".join(p for p in ((title or "").strip(), normalize_tags(tags or ""), piece) if p)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _ring_plans(conn, where: str = "1", params: tuple = ()) -> list[dict]:
    """还在的圈（删掉的圈不算）各自该有哪几段：[{ring_id, memory_id, chunks: [(段号, 起, 止, 文本, 摘要)]}]。"""
    plans = []
    for r in conn.execute(
        f"""SELECT r.id, r.memory_id, r.content, m.title, m.tags
            FROM memory_rings r JOIN memories m ON m.id = r.memory_id
            WHERE r.deleted_at IS NULL AND {where} ORDER BY r.id""",
        params,
    ).fetchall():
        chunks = []
        for i, (s, e) in enumerate(chunk_spans(r["content"] or "")):
            text = ring_text(r["title"], r["tags"], r["content"][s:e])
            chunks.append((i, s, e, text, _hash(text)))
        plans.append({"ring_id": r["id"], "memory_id": r["memory_id"], "chunks": chunks})
    return plans


def _stale(conn, plans: list[dict], memory_id: int | None = None) -> list[dict]:
    """当前模型下指纹缺了 / 过时了（文本或摘录坐标对不上）的圈。给了 memory_id 只看这张卡的。"""
    sql = "SELECT ring_id, chunk, char_from, char_to, text_hash FROM ring_embeddings WHERE model = ?"
    params: list = [current_model()]
    if memory_id is not None:
        sql += " AND memory_id = ?"
        params.append(memory_id)
    have: dict[int, list] = {}
    for r in conn.execute(sql + " ORDER BY ring_id, chunk", params):
        have.setdefault(r["ring_id"], []).append((r["chunk"], r["char_from"], r["char_to"], r["text_hash"]))
    return [p for p in plans if [(c[0], c[1], c[2], c[4]) for c in p["chunks"]] != have.get(p["ring_id"], [])]


def _store_ring(conn, plan: dict, vectors: list[list[float]]) -> None:
    conn.execute("DELETE FROM ring_embeddings WHERE ring_id = ?", (plan["ring_id"],))
    conn.executemany(
        """INSERT INTO ring_embeddings
           (ring_id, memory_id, chunk, char_from, char_to, text_hash, model, dim, embedding)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (plan["ring_id"], plan["memory_id"], i, s, e, h, current_model(), len(v), _pack(v))
            for (i, s, e, _, h), v in zip(plan["chunks"], vectors)
        ],
    )


def _drop_outdated(conn, memory_id: int, plans: list[dict]) -> None:
    """算不出新指纹时：这张卡还在的圈上、跟现在的文本对不上的分段指纹删掉（不管哪个模型算的），
    留着它搜索会按旧意思命中；删了就是缺席，关键词照常、补算能补。删掉的圈不管（搜不到它，恢复了照旧能用）。"""
    live = {p["ring_id"] for p in plans}
    keep = {(p["ring_id"], c[0], c[4]) for p in plans for c in p["chunks"]}
    for r in conn.execute(
        "SELECT ring_id, chunk, text_hash FROM ring_embeddings WHERE memory_id = ?", (memory_id,)
    ).fetchall():
        if r["ring_id"] in live and (r["ring_id"], r["chunk"], r["text_hash"]) not in keep:
            conn.execute("DELETE FROM ring_embeddings WHERE ring_id = ? AND chunk = ?", (r["ring_id"], r["chunk"]))


def embed_card(conn, memory_id: int, whole: bool = True) -> bool:
    """在调用方事务里给一张卡补齐指纹（不提交）：whole=True 时重算整条指纹（内容 / 主题 / 标签变了）；
    各圈的分段指纹缺了或过时了的重算，没变的不动。一次 API 调用全算完。

    任何失败都吞掉——卡照常入库：重算不了的整条指纹和过时的分段指纹删掉（留着会按旧意思被召回，
    而 rebuild 只补缺席的），缺席的由 rebuild_embeddings() 事后补。全部补齐返回 True。"""
    plans = _ring_plans(conn, "r.memory_id = ?", (memory_id,))
    if enabled() and vec_available():
        stale = _stale(conn, plans, memory_id)
        row = conn.execute("SELECT content, topic, tags FROM memories WHERE id = ?", (memory_id,)).fetchone()
        texts = [memory_text(row["content"], row["topic"], row["tags"])] if whole else []
        texts += [c[3] for p in stale for c in p["chunks"]]
        if not texts:
            return True
        try:
            vectors = embed_texts(texts)
        except Exception as e:
            print(f"[embed] memory {memory_id} 指纹生成失败（记忆已保存，rebuild 可补）: {e}", flush=True)
        else:
            if whole:
                _store(conn, memory_id, vectors.pop(0))
            for p in stale:
                n = len(p["chunks"])
                _store_ring(conn, p, vectors[:n])
                vectors = vectors[n:]
            return True
    if whole:
        conn.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (memory_id,))
    _drop_outdated(conn, memory_id, plans)
    return False


# ---------- 搜索 ----------


def vector_search(
    query: str,
    k: int = 40,
    where: tuple[list[str], list] = ([], []),
    rings_where: tuple[list[str], list] = ([], []),
) -> list[dict]:
    """语义腿：按距离从近到远返回至多 k 张卡 [{"id", "ring_id", "char_from", "char_to"}]。
    一张卡的距离 = 它最像的那一段（ring_id / char_from / char_to 就是那一段）；
    还没有当前模型分段指纹的卡退回整条指纹，这时 ring_id 为 None。
    where = 卡的筛选条件（SQL 里卡的别名是 m），先筛再取前 k 张，范围内的不会被范围外的挤掉；
    rings_where = 只拿哪些圈去比（别名 r，按日期筛时 = 范围里的圈）。

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
    conds, extra = where
    card = "".join(f" AND {c}" for c in conds)
    ring_conds, ring_extra = rings_where
    ring = "".join(f" AND {c}" for c in ring_conds)
    model, dim, packed = current_model(), len(qvec), _pack(qvec)
    # MIN() 聚合时，同一行里的其他列取的就是距离最小的那一段（SQLite 的规矩）
    by_ring = f"""SELECT e.memory_id, e.ring_id, e.char_from, e.char_to,
                         MIN(vec_distance_cosine(e.embedding, ?)) AS d
                  FROM ring_embeddings e
                  JOIN memory_rings r ON r.id = e.ring_id
                  JOIN memories m ON m.id = e.memory_id
                  WHERE e.model = ? AND e.dim = ? AND r.deleted_at IS NULL AND m.deleted_at IS NULL{ring}{card}
                  GROUP BY e.memory_id ORDER BY d LIMIT ?"""
    by_whole = f"""SELECT m.id AS memory_id, NULL AS ring_id, NULL AS char_from, NULL AS char_to,
                          vec_distance_cosine(w.embedding, ?) AS d
                   FROM memory_embeddings w JOIN memories m ON m.id = w.memory_id
                   WHERE w.model = ? AND w.dim = ? AND m.deleted_at IS NULL{card}
                     AND NOT EXISTS (SELECT 1 FROM ring_embeddings e JOIN memory_rings r ON r.id = e.ring_id
                                     WHERE e.memory_id = m.id AND e.model = ? AND e.dim = ?
                                       AND r.deleted_at IS NULL)
                   ORDER BY d LIMIT ?"""
    with get_conn() as conn:
        rows = conn.execute(by_ring, [packed, model, dim, *ring_extra, *extra, k]).fetchall()
        rows += conn.execute(by_whole, [packed, model, dim, *extra, model, dim, k]).fetchall()
    rows.sort(key=lambda r: (r["d"], r["memory_id"]))
    return [
        {"id": r["memory_id"], "ring_id": r["ring_id"], "char_from": r["char_from"], "char_to": r["char_to"]}
        for r in rows[:k]
    ]


# ---------- 补算 ----------


def rebuild_embeddings(batch_size: int = REBUILD_BATCH, dry_run: bool = False) -> dict:
    """给缺席或过时（≠当前模型）的记忆补算整条指纹，再给缺了 / 过时了的圈补算分段指纹。

    逐批提交——中断重跑不漏不重（锚点推进的老规矩）；一圈的几段总在同一批里，不会只算一半。
    服务开着也能跑：调 API 时不占写锁，回来写之前拿写锁重读一遍，这期间被改过 / 删掉的卡和圈跳过
    （改的那一下已经自己算过了；没算上的下次再跑会补），不会拿旧文本的指纹盖掉新的。
    换模型的完整流程：改 .env 的 EMBEDDING_MODEL → 重启 → 跑本函数（或 python -m app.embeddings）。
    dry_run=True 只数要算多少（条数、段数、字数），不调 API、不写库。
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
        plans = _ring_plans(conn)
        stale = _stale(conn, plans)
        if dry_run:
            whole_texts = [memory_text(r["content"], r["topic"], r["tags"]) for r in rows]
            ring_texts = [c[3] for p in stale for c in p["chunks"]]
            return {
                "model": current_model(),
                "dry_run": True,
                "memories_to_embed": len(rows),
                "rings_total": len(plans),
                "rings_to_embed": len(stale),
                "ring_chunks_to_embed": len(ring_texts),
                "chars_to_embed": sum(map(len, whole_texts + ring_texts)),
            }
        total_missing, embedded, skipped = len(rows), 0, 0
        for i in range(0, total_missing, batch_size):
            batch = rows[i : i + batch_size]
            texts = [memory_text(r["content"], r["topic"], r["tags"]) for r in batch]
            vectors = embed_texts(texts, timeout=REBUILD_TIMEOUT)
            conn.execute("BEGIN IMMEDIATE")
            now = {
                r["id"]: memory_text(r["content"], r["topic"], r["tags"])
                for r in conn.execute(
                    "SELECT id, content, topic, tags FROM memories WHERE id IN (SELECT value FROM json_each(?))",
                    (json.dumps([r["id"] for r in batch]),),
                )
            }
            for row, text, vector in zip(batch, texts, vectors):
                if now.get(row["id"]) != text:  # 算的这会儿被改了 / 删了
                    skipped += 1
                    continue
                _store(conn, row["id"], vector)
                embedded += 1
            conn.commit()  # 逐批落盘，断点续跑的锚点
        rings_now = chunks_now = 0
        pending: list[dict] = []
        for n, plan in enumerate(stale, start=1):
            pending.append(plan)
            size = sum(len(p["chunks"]) for p in pending)
            if size < batch_size and n < len(stale):
                continue
            texts = [c[3] for p in pending for c in p["chunks"]]
            vectors = embed_texts(texts, timeout=REBUILD_TIMEOUT) if texts else []
            conn.execute("BEGIN IMMEDIATE")
            now = {
                p["ring_id"]: p["chunks"]
                for p in _ring_plans(
                    conn, "r.id IN (SELECT value FROM json_each(?))", (json.dumps([p["ring_id"] for p in pending]),)
                )
            }
            for p in pending:
                k = len(p["chunks"])
                part, vectors = vectors[:k], vectors[k:]
                if now.get(p["ring_id"]) != p["chunks"]:  # 文本或坐标变了都跳过，不能盖回旧摘录位置
                    skipped += 1
                    continue
                _store_ring(conn, p, part)
                rings_now += 1
                chunks_now += k
            conn.commit()
            pending = []
        up_to_date = conn.execute(
            "SELECT COUNT(*) FROM memory_embeddings WHERE model = ?", (current_model(),)
        ).fetchone()[0]
        rings_left = len(_stale(conn, _ring_plans(conn)))
    return {
        "model": current_model(),
        "embedded_now": embedded,
        "total_with_current_model": up_to_date,
        "rings_embedded_now": rings_now,
        "ring_chunks_now": chunks_now,
        "rings_total": len(plans),
        "rings_up_to_date": len(plans) - rings_left,
        "skipped_changed_meanwhile": skipped,
    }


USAGE = "用法：python -m app.embeddings [--dry-run]（--dry-run 只数要算多少，不调 API、不写库）"


def main(argv: list[str]) -> int:
    args = argv[1:]
    if args not in ([], ["--dry-run"]):
        print(USAGE, file=sys.stderr)
        return 2
    print(json.dumps(rebuild_embeddings(dry_run=bool(args)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # VPS 上：docker exec ember python -m app.embeddings [--dry-run]
    raise SystemExit(main(sys.argv))
