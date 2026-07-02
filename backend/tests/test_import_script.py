import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "import_claude_export.py"

FAKE_EXPORT = [
    {
        "uuid": "aaaa1111-0000-0000-0000-000000000000",
        "name": "余烬测试对话",
        "chat_messages": [
            {"uuid": "m1111111-0", "sender": "human", "created_at": "2026-05-29T21:14:00Z",
             "text": "今天有点难过"},
            {"uuid": "m2222222-0", "sender": "assistant", "created_at": "2026-05-29T21:15:00Z",
             "content": [{"type": "text", "text": "我在呢，说说看"}]},
            {"uuid": "m3333333-0", "sender": "human", "created_at": "2026-05-29T21:16:00Z",
             "text": ""},  # 空消息应被跳过
        ],
    },
    {"uuid": "bbbb2222-0000-0000-0000-000000000000", "name": "另一个对话", "chat_messages": []},
]


def run(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=True
    )


def test_inventory_lists_conversations(tmp_path):
    export = tmp_path / "conversations.json"
    export.write_text(json.dumps(FAKE_EXPORT, ensure_ascii=False), encoding="utf-8")
    out = run("inventory", str(export)).stdout
    assert "共 2 个对话" in out
    assert "余烬测试对话" in out


PLUGIN_EXPORT = [  # 浏览器插件导出格式：messages / contentBlocks / createdAt / 带 BOM
    {
        "uuid": "cccc3333-0000-0000-0000-000000000000",
        "name": "插件格式对话",
        "messages": [
            {"uuid": "p1111111-0", "sender": "assistant", "createdAt": "2026-04-18T05:01:25Z",
             "contentBlocks": [
                 {"type": "thinking", "thinking": "内心独白不该被提取"},
                 {"type": "text", "text": "说出口的话才算数"},
             ],
             "searchText": "内心独白不该被提取 说出口的话才算数"},
        ],
    }
]


def test_plugin_format_with_bom_skips_thinking(tmp_path):
    export = tmp_path / "single.json"
    export.write_bytes(b"\xef\xbb\xbf" + json.dumps(PLUGIN_EXPORT, ensure_ascii=False).encode("utf-8"))
    workdir = tmp_path / "work"
    run("slice", str(export), "--conversation", "插件格式", "--out", str(workdir))
    group = (workdir / "group_001.txt").read_text(encoding="utf-8")
    assert group == "[2026-04-18T05:01][id=p1111111] Claude: 说出口的话才算数"
    assert "内心独白" not in group


def test_slice_groups_with_source_ids_and_anchor(tmp_path):
    export = tmp_path / "conversations.json"
    export.write_text(json.dumps(FAKE_EXPORT, ensure_ascii=False), encoding="utf-8")
    workdir = tmp_path / "work"
    run("slice", str(export), "--conversation", "余烬测试", "--out", str(workdir), "--group-size", "1")

    group1 = (workdir / "group_001.txt").read_text(encoding="utf-8")
    assert "[2026-05-29T21:14][id=m1111111] 轩: 今天有点难过" == group1
    group2 = (workdir / "group_002.txt").read_text(encoding="utf-8")
    assert "Claude: 我在呢" in group2  # content 块格式也能解析

    meta = json.loads((workdir / "meta.json").read_text(encoding="utf-8"))
    assert meta["total_messages"] == 2  # 空消息被跳过
    assert meta["total_groups"] == 2
    assert meta["done_groups"] == []  # 锚点从空开始
