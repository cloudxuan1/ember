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

# 表结构与 docs/项目计划.md 第 2 节保持一致
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
    created_at  TEXT DEFAULT (datetime('now','+8 hours')),  -- 存的时间（系统记，平时不显示）
    updated_at  TEXT,                    -- 最后改的时间；没改过为空
    deleted_at  TEXT,                    -- 删了先藏起来：非空 = 已删，过了恢复期限才真删
    deleted_by  TEXT,                    -- xuan / ke
    title       TEXT,                    -- 卡的标题（年轮卡，2026-10）；空 = 网页显示正文开头
    starred     INTEGER NOT NULL DEFAULT 0  -- ⭐收藏
);
CREATE INDEX IF NOT EXISTS idx_mem_date  ON memories(date);
CREATE INDEX IF NOT EXISTS idx_mem_tier  ON memories(tier);
CREATE INDEX IF NOT EXISTS idx_mem_topic ON memories(topic);
CREATE INDEX IF NOT EXISTS idx_mem_space ON memories(space);

-- 年轮（app/rings.py）：一件事一张卡（memories 一行），下面一圈一圈是哪天、发生了什么。
-- 卡片日期 = 最新一圈的日期。deleted_at 是删掉某一圈（整张卡删除仍看 memories.deleted_at）；
-- 卡真删时圈跟着级联删。
CREATE TABLE IF NOT EXISTS memory_rings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id  INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    date       TEXT NOT NULL,          -- 这一圈的事发生（或说出口）的那天，不写未来的日子
    content    TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now','+8 hours')),
    updated_at TEXT,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_rings_mem  ON memory_rings(memory_id, id);
CREATE INDEX IF NOT EXISTS idx_rings_date ON memory_rings(date);

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

-- 按圈算的语义指纹（年轮卡 PR2）：每一圈一份；太长的圈切成几段，一段一份（app/embeddings.py chunk_spans）。
-- 搜的时候哪一段对上，整张卡就算命中，结果里带上是哪一圈。上面的整条指纹表照常维护，当退路（PR5 再删）。
-- char_from / char_to = 这一段在这一圈正文里的起止（摘录用）；text_hash = 算指纹那一刻拼给模型的文本
-- （标题 + 便利贴 + 这一段）的摘要，对不上 = 过时，补算时重算。圈 / 卡真删时级联删。
CREATE TABLE IF NOT EXISTS ring_embeddings (
    ring_id    INTEGER NOT NULL REFERENCES memory_rings(id) ON DELETE CASCADE,
    memory_id  INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    chunk      INTEGER NOT NULL,    -- 这一圈的第几段，从 0 起
    char_from  INTEGER NOT NULL,
    char_to    INTEGER NOT NULL,
    text_hash  TEXT NOT NULL,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    embedding  BLOB NOT NULL,
    created_at TEXT DEFAULT (datetime('now','+8 hours')),
    PRIMARY KEY (ring_id, chunk)
);
CREATE INDEX IF NOT EXISTS idx_ring_emb_mem ON ring_embeddings(memory_id);
CREATE INDEX IF NOT EXISTS idx_ring_emb_model ON ring_embeddings(model);
-- 圈的正文不管从哪条路改了（改卡时圈跟着走、搬家对齐、以后的改圈、退回旧程序期间），
-- 这一圈的旧指纹立刻作废：搜索绝不按改之前的意思命中；缺了的由写入时重算或补算补上。
CREATE TRIGGER IF NOT EXISTS ring_content_changed AFTER UPDATE OF content ON memory_rings
WHEN NEW.content IS NOT OLD.content
BEGIN
    DELETE FROM ring_embeddings WHERE ring_id = NEW.id;
END;

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
    """V4 就地升级老库，全部幂等（memory_drafts 那条只对还没做单库搬家的老库有意义）：
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


MEMORY_NEW_COLUMNS = {  # 后来加的列 → 补列时的类型
    "updated_at": "TEXT", "deleted_at": "TEXT", "deleted_by": "TEXT",  # 单库改造（2026-10）
    "title": "TEXT", "starred": "INTEGER NOT NULL DEFAULT 0",          # 年轮卡（2026-10）
}


def _add_memory_columns(conn: sqlite3.Connection) -> None:
    """老库的 memories 补后来加的列（CREATE TABLE IF NOT EXISTS 不会给已有表加列）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
    for col, decl in MEMORY_NEW_COLUMNS.items():
        if cols and col not in cols:
            conn.execute(f"ALTER TABLE memories ADD COLUMN {col} {decl}")


def upgrade(conn: sqlite3.Connection) -> None:
    """建表 + 就地补列，不搬数据。"""
    _migrate(conn)
    _add_memory_columns(conn)
    conn.executescript(SCHEMA + EDGES_DDL)


def migrate_all(conn: sqlite3.Connection) -> tuple[dict | None, dict | None]:
    """启动时的全套：建表补列 → 单库搬家 → 年轮卡搬家，返回两份搬家报告（不用搬的是 None）。
    两次搬家都只在还有没搬的东西时才动手，所以每次启动都跑一遍没关系。"""
    from app import migrate_rings, migrate_single_store  # 搬家要用 memories 的工具函数，延迟导入免循环

    upgrade(conn)
    return migrate_single_store.migrate(conn), migrate_rings.migrate(conn)


def init_db() -> dict | None:
    """建表 + 就地升级 + 搬家（见 migrate_all）。返回单库搬家的报告（老库第一次才有，平时 None）；
    年轮卡搬家的报告自己打日志、写文件。"""
    conn = get_conn()
    try:
        return migrate_all(conn)[0]
    finally:
        conn.close()
