"""chat-lite 思考彩蛋词库：整份词库存成一份带版本号的 JSON 文档，和记忆表完全分开。

为什么存成一份文档而不拆表：前端本来就按「一整份词库」读写，冲突检测只需比一个版本号；
MCP 的增删改也是「读出 → 改一处 → 带版本写回」，同一套校验。词条量是百来条，不存在性能问题。

版本号：每次写入 +1；写入方必须带上「我是基于哪个版本改的」（base_version），
对不上就是另一台设备已经改过 → 抛 VersionConflict，由调用方决定怎么办（网页会弹提示让轩选）。
云端还没有词库时 base_version 传 0（首次同步 = 把浏览器里现有的词库搬上来）。
历史：每次覆盖前把旧版本存进 thinking_library_history，只留最近 HISTORY_KEEP 份，误删能找回。

文档格式：
{
  "settings": {"egg": bool, "translate": bool, "marauder": bool},
  "series": [{"id": str, "name": str, "builtin": bool, "enabled": bool,
              "words": [{"en": str, "zh": str}]}]
}
内置系列（builtin=true）的原版词留在前端代码里当「恢复默认」的来源，这里存的是当前生效的样子。
"""

import copy
import json
import re
import secrets

from app.db import get_conn

HISTORY_KEEP = 20
SERIES_MAX = 50
WORDS_MAX = 300
NAME_MAX = 20
TEXT_MAX = 80
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")
SETTING_KEYS = ("egg", "translate", "marauder")


class VersionConflict(Exception):
    """云端版本和调用方的 base_version 对不上。current 是云端现在的样子（get_library 的返回）。"""

    def __init__(self, current: dict):
        super().__init__("词库已被另一端修改")
        self.current = current


def _text(value, field: str, limit: int, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} 不能为空")
    if len(value) > limit:
        raise ValueError(f"{field} 最多 {limit} 个字符")
    return value


def normalize_words(words, where: str = "words") -> list[dict]:
    if not isinstance(words, list):
        raise ValueError(f"{where} 必须是数组")
    if len(words) > WORDS_MAX:
        raise ValueError(f"{where} 最多 {WORDS_MAX} 个词")
    out = []
    for i, word in enumerate(words):
        if not isinstance(word, dict):
            raise ValueError(f"{where}[{i}] 必须是对象")
        out.append({
            "en": _text(word.get("en"), f"{where}[{i}].en", TEXT_MAX),
            "zh": _text(word.get("zh"), f"{where}[{i}].zh", TEXT_MAX, required=False),
        })
    return out


def normalize_library(data) -> dict:
    """校验并清洗整份词库；不合格抛 ValueError（中文原因）。"""
    if not isinstance(data, dict):
        raise ValueError("词库必须是对象")
    settings = data.get("settings") or {}
    if not isinstance(settings, dict):
        raise ValueError("settings 必须是对象")
    clean_settings = {}
    for key in SETTING_KEYS:
        value = settings.get(key, True)
        if not isinstance(value, bool):
            raise ValueError(f"settings.{key} 必须是布尔值")
        clean_settings[key] = value
    series = data.get("series")
    if not isinstance(series, list):
        raise ValueError("series 必须是数组")
    if len(series) > SERIES_MAX:
        raise ValueError(f"最多 {SERIES_MAX} 个系列")
    seen = set()
    clean_series = []
    for i, item in enumerate(series):
        if not isinstance(item, dict):
            raise ValueError(f"series[{i}] 必须是对象")
        sid = item.get("id")
        if not isinstance(sid, str) or not ID_RE.match(sid):
            raise ValueError(f"series[{i}].id 只能用小写字母、数字和连字符")
        if sid in seen:
            raise ValueError(f"系列 id 重复：{sid}")
        seen.add(sid)
        for flag in ("builtin", "enabled"):
            if not isinstance(item.get(flag, False if flag == "builtin" else True), bool):
                raise ValueError(f"series[{i}].{flag} 必须是布尔值")
        clean_series.append({
            "id": sid,
            "name": _text(item.get("name"), f"series[{i}].name", NAME_MAX),
            "builtin": item.get("builtin", False),
            "enabled": item.get("enabled", True),
            "words": normalize_words(item.get("words", []), f"series[{i}].words"),
        })
    return {"settings": clean_settings, "series": clean_series}


def _row_to_library(row) -> dict:
    if row is None:
        return {"version": 0, "data": None, "updated_at": None, "updated_by": ""}
    return {
        "version": row["version"],
        "data": json.loads(row["data"]),
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"] or "",
    }


def get_library() -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM thinking_library WHERE id = 1").fetchone()
    return _row_to_library(row)


def save_library(data, base_version: int, by: str) -> dict:
    """带版本写入整份词库。版本对不上抛 VersionConflict，不写任何东西。"""
    clean = normalize_library(data)
    if not isinstance(base_version, int) or isinstance(base_version, bool) or base_version < 0:
        raise ValueError("base_version 必须是非负整数")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")  # 读版本 → 写入之间不让别的写入插队
        row = conn.execute("SELECT * FROM thinking_library WHERE id = 1").fetchone()
        current = _row_to_library(row)
        if current["version"] != base_version:
            conn.rollback()
            raise VersionConflict(current)
        new_version = base_version + 1
        payload = json.dumps(clean, ensure_ascii=False)
        if row is None:
            conn.execute(
                "INSERT INTO thinking_library (id, data, version, updated_by) VALUES (1, ?, ?, ?)",
                (payload, new_version, by),
            )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO thinking_library_history (version, data, updated_at, updated_by) VALUES (?, ?, ?, ?)",
                (row["version"], row["data"], row["updated_at"], row["updated_by"]),
            )
            conn.execute(
                "DELETE FROM thinking_library_history WHERE version <= ?",
                (new_version - 1 - HISTORY_KEEP,),
            )
            conn.execute(
                "UPDATE thinking_library SET data = ?, version = ?, updated_by = ?, updated_at = datetime('now','+8 hours') WHERE id = 1",
                (payload, new_version, by),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM thinking_library WHERE id = 1").fetchone()
        return _row_to_library(row)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


# ---------- MCP 用的逐项修改：读出 → 改一处 → 带版本写回 ----------


def _find(series: list[dict], series_id: str | None) -> dict:
    if not series_id:
        raise ValueError("要指定 series_id")
    for item in series:
        if item["id"] == series_id:
            return item
    raise ValueError(f"没有这个系列：{series_id}")


def _find_word(series: dict, en: str | None) -> int:
    en = _text(en, "en", TEXT_MAX)
    for i, word in enumerate(series["words"]):
        if word["en"].lower() == en.lower():
            return i
    raise ValueError(f"「{series['name']}」里没有这个词：{en}")


def edit_library(
    action: str,
    *,
    series_id: str | None = None,
    name: str | None = None,
    words: list | None = None,
    en: str | None = None,
    new_en: str | None = None,
    zh: str | None = None,
    enabled: bool | None = None,
    setting: str | None = None,
    value: bool | None = None,
    by: str = "mcp",
) -> dict:
    current = get_library()
    if current["data"] is None:
        raise ValueError("云端还没有词库：先在 chat-lite 网页打开一次（开着云同步），把现有词库搬上来")
    data = copy.deepcopy(current["data"])
    series = data["series"]
    summary = ""

    if action == "add_series":
        new_id = f"custom-{secrets.token_hex(4)}"
        new_name = _text(name, "name", NAME_MAX)
        series.append({"id": new_id, "name": new_name, "builtin": False, "enabled": True,
                       "words": normalize_words(words or [])})
        series_id = new_id
        summary = f"新建系列「{new_name}」"
    elif action == "rename_series":
        target = _find(series, series_id)
        old = target["name"]
        target["name"] = _text(name, "name", NAME_MAX)
        summary = f"「{old}」改名为「{target['name']}」"
    elif action == "delete_series":
        target = _find(series, series_id)
        if target["builtin"]:
            raise ValueError("内置系列不能删，可以用 set_series_enabled 关掉")
        series.remove(target)
        summary = f"删除系列「{target['name']}」"
    elif action == "set_series_enabled":
        target = _find(series, series_id)
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是布尔值")
        target["enabled"] = enabled
        summary = f"「{target['name']}」{'打开' if enabled else '关闭'}"
    elif action == "add_words":
        target = _find(series, series_id)
        added = normalize_words(words or [])
        if not added:
            raise ValueError("words 至少给一个词")
        existing = {w["en"].lower() for w in target["words"]}
        fresh = [w for w in added if w["en"].lower() not in existing]
        target["words"].extend(fresh)
        summary = f"「{target['name']}」加了 {len(fresh)} 个词" + (f"（{len(added) - len(fresh)} 个已存在，跳过）" if len(fresh) < len(added) else "")
    elif action == "update_word":
        target = _find(series, series_id)
        i = _find_word(target, en)
        word = target["words"][i]
        if new_en is not None:
            word["en"] = _text(new_en, "new_en", TEXT_MAX)
        if zh is not None:
            word["zh"] = _text(zh, "zh", TEXT_MAX, required=False)
        summary = f"「{target['name']}」改了一个词：{word['en']} / {word['zh']}"
    elif action == "delete_word":
        target = _find(series, series_id)
        i = _find_word(target, en)
        removed = target["words"].pop(i)
        summary = f"「{target['name']}」删了一个词：{removed['en']}"
    elif action == "set_setting":
        if setting not in SETTING_KEYS:
            raise ValueError(f"setting 只能是 {' / '.join(SETTING_KEYS)}")
        if not isinstance(value, bool):
            raise ValueError("value 必须是布尔值")
        data["settings"][setting] = value
        summary = f"设置 {setting} = {'开' if value else '关'}"
    else:
        raise ValueError("action 只能是 add_series / rename_series / delete_series / set_series_enabled / add_words / update_word / delete_word / set_setting")

    saved = save_library(data, current["version"], by)
    result = {"ok": True, "summary": summary, "version": saved["version"]}
    if series_id and action != "delete_series":
        result["series"] = next((s for s in saved["data"]["series"] if s["id"] == series_id), None)
    return result
