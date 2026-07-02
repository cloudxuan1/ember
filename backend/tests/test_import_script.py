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
