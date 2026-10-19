-- 年轮卡之前（origin/main f8a61fd，单库改造后）的完整建表语句，年轮卡迁移测试用来造老库。

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
    created_at  TEXT DEFAULT (datetime('now','+8 hours')),  -- 存的时间（系统记，平时不显示）
    updated_at  TEXT,                    -- 最后改的时间；没改过为空
    deleted_at  TEXT,                    -- 删了先藏起来：非空 = 已删，过了恢复期限才真删
    deleted_by  TEXT                     -- xuan / ke
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

-- 一条记忆只存一份（2026-10 单库改造）：谁改都直接改 memories，改前的整条旧值挤进这里，
-- 每条记忆只留最近 N 版（设置 history_keep，1 或 2），更早的扔掉。记忆真删时跟着删。
CREATE TABLE IF NOT EXISTS memory_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id   INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    snapshot    TEXT NOT NULL,          -- 被换下来的那一版：可改字段的 JSON
    replaced_by TEXT NOT NULL,          -- 谁的改动把它换下来：xuan / ke
    action      TEXT NOT NULL DEFAULT 'edit',  -- edit / revert
    replaced_at TEXT DEFAULT (datetime('now','+8 hours'))
);
CREATE INDEX IF NOT EXISTS idx_history_mem ON memory_history(memory_id, id);

-- 最近动静：谁（xuan 轩 / ke 小克）几点存了 / 改了 / 回退了 / 删了 / 恢复了哪条。
-- 不挂外键：记忆真删后流水照样能显示（title 存当时的开头）；超过显示期限的行清掉。
-- history_id = 这次改动换下来的那一版（edit / revert），回退按钮靠它；
-- undone = 这一行已经被撤销（改动被回退 / 删除被恢复）。
CREATE TABLE IF NOT EXISTS activity_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT DEFAULT (datetime('now','+8 hours')),
    actor      TEXT NOT NULL CHECK (actor IN ('xuan','ke')),
    action     TEXT NOT NULL CHECK (action IN ('save','edit','revert','delete','restore')),
    memory_id  INTEGER NOT NULL,
    title      TEXT DEFAULT '',
    history_id INTEGER,
    undone     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_activity_at ON activity_log(at);
CREATE INDEX IF NOT EXISTS idx_activity_mem ON activity_log(memory_id, id);

-- 轩在网页设置里能调的几样（app/settings.py 管默认值和校验）。
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
