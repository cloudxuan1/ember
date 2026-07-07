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
    """惰性探测：加载发生在 get_conn 里，但新进程可能还没开过连接就来问
    （如 python -m app.embeddings 独立重建），先开一次触发探测再回答。"""
    global _vec_status
    if _vec_status is None:
        try:
            get_conn().close()
        except Exception:
            return False
        if _vec_status is None:  # sqlite_vec 没安装时 get_conn 不会碰状态
            _vec_status = False
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

-- 浮现日志（V6b）：①③ 层的冷却依据——同一条记忆 3 天内不重复递。
-- ② 话题共振层不记也不查这张表（相关记忆永远该到场）。
CREATE TABLE IF NOT EXISTS briefing_log (
    memory_id   INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    surfaced_at TEXT DEFAULT (datetime('now','+8 hours'))
);
CREATE INDEX IF NOT EXISTS idx_briefing_mem ON briefing_log(memory_id, surfaced_at);

-- 聊天网关（终局试验田）：对话原文永存，回流提取只从这里读。
-- reflow_anchor_id = 回流锚点：该会话已提取到哪条消息（锚点逐组推进，失败不漏不重）。
CREATE TABLE IF NOT EXISTS conversations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT DEFAULT '',
    reflow_anchor_id INTEGER DEFAULT 0,
    created_at       TEXT DEFAULT (datetime('now','+8 hours')),
    updated_at       TEXT DEFAULT (datetime('now','+8 hours'))
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    role            TEXT NOT NULL CHECK (role IN ('user','assistant')),
    content         TEXT NOT NULL,
    memory_meta     TEXT DEFAULT '',   -- 本轮注入了哪些记忆（JSON）——prompt 透明不黑箱
    created_at      TEXT DEFAULT (datetime('now','+8 hours'))
);
CREATE INDEX IF NOT EXISTS idx_chatmsg_conv ON chat_messages(conversation_id, id);
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
