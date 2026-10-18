"""轩在网页「设置」里能调的几样，存在 settings 表（一行一个 key）。没存过的用默认值。

- history_keep：每条记忆留几版旧内容，1 或 2（默认 1）。调小时多出来的旧版立刻扔掉。
- activity_days：最近动静显示多少天（默认 30），也是删掉的记忆还能恢复的期限，过了才真删。
- ke_can_delete：小克能不能用 memory_delete 删记忆（默认开）。
"""

import json

from app.db import get_conn

DEFAULTS = {"history_keep": 1, "activity_days": 30, "ke_can_delete": True}
HISTORY_KEEP_CHOICES = (1, 2)
ACTIVITY_DAYS_RANGE = (1, 365)


def get_settings(conn=None) -> dict:
    def _read(c) -> dict:
        stored = {r["key"]: json.loads(r["value"]) for r in c.execute("SELECT key, value FROM settings")}
        return {k: stored.get(k, v) for k, v in DEFAULTS.items()}

    if conn is not None:
        return _read(conn)
    with get_conn() as c:
        return _read(c)


def _validate(key: str, value):
    if key == "history_keep":
        if isinstance(value, bool) or value not in HISTORY_KEEP_CHOICES:
            raise ValueError("history_keep 只能是 1 或 2")
    elif key == "activity_days":
        lo, hi = ACTIVITY_DAYS_RANGE
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise ValueError(f"activity_days 要是 {lo}–{hi} 的整数")
    elif key == "ke_can_delete":
        if not isinstance(value, bool):
            raise ValueError("ke_can_delete 要是 true / false")
    else:
        raise ValueError(f"没有这个设置：{key}")
    return value


def update_settings(patch: dict) -> dict:
    """改设置（只收认识的 key，整批校验过了才写）。history_keep 调小时顺手把多的旧版扔掉。"""
    from app import history  # history 读 settings，反过来延迟导入免循环

    if not isinstance(patch, dict) or not patch:
        raise ValueError("要改的设置不能为空")
    clean = {k: _validate(k, v) for k, v in patch.items()}
    with get_conn() as conn:
        for key, value in clean.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )
        if "history_keep" in clean:
            history.trim_all(conn, clean["history_keep"])
        return get_settings(conn)
