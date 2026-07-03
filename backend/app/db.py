"""SQLite 连接与建表。库是从日志原文提取的派生索引，可随时重建。"""

import os
import sqlite3
from pathlib import Path

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

CREATE TABLE IF NOT EXISTS memory_edges (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id  INTEGER NOT NULL REFERENCES memories(id),
    to_id    INTEGER NOT NULL REFERENCES memories(id),
    relation TEXT DEFAULT 'related'  -- led_to / same_as / contradicts / supersedes / related
);

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
    batch       TEXT DEFAULT '',    -- 提取批次，如 4.18起点/group_003（锚点推进的单位）
    status      TEXT DEFAULT 'pending',  -- pending / approved / rejected
    memory_id   INTEGER REFERENCES memories(id),  -- 通过后指向正式记忆
    created_at  TEXT DEFAULT (datetime('now','+8 hours')),
    reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_draft_status ON memory_drafts(status);
CREATE INDEX IF NOT EXISTS idx_draft_batch  ON memory_drafts(batch);
"""


def db_path() -> Path:
    return Path(os.environ.get("EMBER_DB", "data/ember.db"))


def get_conn() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
