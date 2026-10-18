-- 单库改造之前（origin/main 199b937）的完整建表语句，迁移测试用来造老库。

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

-- 已入库记忆的修改提议（app/memory_edits.py）：AI 经 MCP memory_edit 提交，轩在审核台确认才覆盖。
-- 只存提议的新值（NULL = 该字段不改），不存旧值——"改前"永远现读 memories，确认后不留旧版本。
-- 一条记忆最多一张待确认提议（UNIQUE），再改合并进来、version +1；记忆被删（撤回）时提议跟着删。
-- AUTOINCREMENT：删掉的提议号不复用，开着的旧卡片不会确认到另一条提议上。
CREATE TABLE IF NOT EXISTS memory_edits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id  INTEGER NOT NULL UNIQUE REFERENCES memories(id) ON DELETE CASCADE,
    content    TEXT,
    tags       TEXT,
    topic      TEXT,
    reason     TEXT DEFAULT '',    -- AI 写给轩看的一句理由
    base       TEXT NOT NULL DEFAULT '{}',  -- {字段: 提议时现值的哈希}（不是旧文）：对不上 = 这个字段在提议后又被改过
    version    INTEGER NOT NULL DEFAULT 1,
    created_by TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','+8 hours')),
    updated_at TEXT DEFAULT (datetime('now','+8 hours'))
);

-- chat-lite 思考彩蛋词库（app/thinking_words.py）：和记忆完全无关，只是借住同一个库。
-- 整份词库一行 JSON + 版本号（写入必须带 base_version，对不上即冲突）；覆盖前旧版进 history，留最近 20 份。
CREATE TABLE IF NOT EXISTS thinking_library (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    data       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    updated_by TEXT DEFAULT '',    -- web / mcp
    updated_at TEXT DEFAULT (datetime('now','+8 hours'))
);
CREATE TABLE IF NOT EXISTS thinking_library_history (
    version    INTEGER PRIMARY KEY,
    data       TEXT NOT NULL,
    updated_by TEXT DEFAULT '',
    updated_at TEXT
);

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
