"""SQLite 连接与建表。库是从日志原文提取的派生索引，可随时重建。"""

import os
import sqlite3
from pathlib import Path

try:
    import sqlite_vec
except ImportError:  # 没装扩展也能跑：语义腿关闭，纯关键词一切照常
    sqlite_vec = None

_vec_status: bool | None = None  # None=没试过 / True=可用 / False=加载失败（不再重试）


def vec_available() -> bool:
    return bool(_vec_status)

# 边表单独成块：V4 迁移重建老表时要原样复用这份 DDL
EDGES_DDL = """
CREATE TABLE IF NOT EXISTS memory_edges (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id    INTEGER NOT NULL REFERENCES memories(id),
    to_id      INTEGER NOT NULL REFERENCES memories(id),
    relation   TEXT DEFAULT 'related'
               CHECK (relation IN ('led_to','same_as','contradicts','supersedes','related')),
    created_by TEXT DEFAULT '',    -- 谁断言的这条边：extraction / mcp / ...
    created_at TEXT DEFAULT (datetime('now','+8 hours')),
    UNIQUE (from_id, to_id, relation)
);
"""

# 表结构与 docs/施工计划.md 第 2 节保持一致
SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,           -- 事件真实日期（从日志抠，不是导入日）
    content     TEXT NOT NULL,           -- 原话 + 一句上下文，永不被摘要覆盖
    tags        TEXT DEFAULT '',         -- 逗号分隔，可检索
    tier        TEXT DEFAULT 'normal',   -- anchor / normal / process
    topic       TEXT DEFAULT '',         -- 主题/实体，去重合并的钩子
    space       TEXT DEFAULT 'personal', -- personal(核心) / ember / vps / ...
    start_date  TEXT,                    -- 区间型起点（点事件留空）
    end_date    TEXT,                    -- 区间型终点；状态不存死，读时现算（V3）
    is_resolved INTEGER DEFAULT 0,
    superseded_by INTEGER REFERENCES memories(id),
    created_at  TEXT DEFAULT (datetime('now','+8 hours'))
);
CREATE INDEX IF NOT EXISTS idx_mem_date  ON memories(date);
CREATE INDEX IF NOT EXISTS idx_mem_tier  ON memories(tier);
CREATE INDEX IF NOT EXISTS idx_mem_topic ON memories(topic);
CREATE INDEX IF NOT EXISTS idx_mem_space ON memories(space);

CREATE TABLE IF NOT EXISTS memory_sources (
    memory_id  INTEGER NOT NULL REFERENCES memories(id),
    source_ref TEXT,   -- 指向 ember-logs 私有仓库的哪份文件哪一段（轻引用，不存全文）
    quote      TEXT    -- 关键原话片段
);

-- 导入草稿暂存区（V3 审核台）：提取结果先进这里，人工通过后才写入 memories。
-- 拒绝的行保留 rejected 状态不删——这张表同时兼任导入审计记录。
CREATE TABLE IF NOT EXISTS memory_drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    content     TEXT NOT NULL,
    tags        TEXT DEFAULT '',
    tier        TEXT DEFAULT 'normal',
    topic       TEXT DEFAULT '',
    space       TEXT DEFAULT 'personal',
    start_date  TEXT,
    end_date    TEXT,
    source_ref  TEXT,               -- 通过时随记忆一起写入 memory_sources
    quote       TEXT,
    links       TEXT DEFAULT '',    -- 边建议 JSON（V4）：目标 memory_id 或同批 draft_id，通过时随记忆写入

    batch       TEXT DEFAULT '',    -- 提取批次，如 4.18起点/group_003（锚点推进的单位）
    status      TEXT DEFAULT 'pending',  -- pending / approved / rejected
    memory_id   INTEGER REFERENCES memories(id),  -- 通过后指向正式记忆
    created_at  TEXT DEFAULT (datetime('now','+8 hours')),
    reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_draft_status ON memory_drafts(status);
CREATE INDEX IF NOT EXISTS idx_draft_batch  ON memory_drafts(batch);

-- 语义指纹（V5）：普通表不用 vec0 虚拟表——维度是数据不是表结构，换模型不用改 DDL。
-- model 列记录"谁算的这枚指纹"：搜索只用与当前配置同模型的行，换模型后旧行自动失效。
-- 记忆删除（审核台撤回）时指纹级联跟着删，派生数据不留孤儿。
CREATE TABLE IF NOT EXISTS memory_embeddings (
    memory_id  INTEGER PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    embedding  BLOB NOT NULL,     -- float32 序列化（struct pack）
    created_at TEXT DEFAULT (datetime('now','+8 hours'))
);
CREATE INDEX IF NOT EXISTS idx_emb_model ON memory_embeddings(model);
"""


def db_path() -> Path:
    return Path(os.environ.get("EMBER_DB", "data/ember.db"))


def get_conn() -> sqlite3.Connection:
    global _vec_status
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # sqlite-vec 扩展按连接加载（只提供 vec_distance_cosine 等函数）；
    # 失败一次就不再重试，语义腿关闭，其余功能不受影响。
    if sqlite_vec is not None and _vec_status is not False:
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            _vec_status = True
        except Exception:
            _vec_status = False
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """V4 就地升级老库，全部幂等：
    - memory_edges 补 created_by/created_at + UNIQUE + relation CHECK（重建搬行，行数 0 也走同一条路）
    - memory_drafts 补 links 列
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(memory_edges)").fetchall()}
    if cols and "created_at" not in cols:
        conn.execute("ALTER TABLE memory_edges RENAME TO _edges_pre_v4")
        conn.executescript(EDGES_DDL)
        conn.execute(
            """INSERT OR IGNORE INTO memory_edges (id, from_id, to_id, relation)
               SELECT id, from_id, to_id, relation FROM _edges_pre_v4"""
        )
        conn.execute("DROP TABLE _edges_pre_v4")
    draft_cols = {r["name"] for r in conn.execute("PRAGMA table_info(memory_drafts)").fetchall()}
    if draft_cols and "links" not in draft_cols:
        conn.execute("ALTER TABLE memory_drafts ADD COLUMN links TEXT DEFAULT ''")


def init_db() -> None:
    with get_conn() as conn:
        _migrate(conn)
        conn.executescript(SCHEMA + EDGES_DDL)
