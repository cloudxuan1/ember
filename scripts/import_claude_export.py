#!/usr/bin/env python3
"""claude.ai 导出文件的解析与切组工具（V2 试导入用）。

用法：
  python scripts/import_claude_export.py inventory <conversations.json>
      列出导出里所有对话：序号、标题、消息数、时间范围。先看清库存再动手。

  python scripts/import_claude_export.py slice <conversations.json> --conversation <标题子串或uuid> --out <工作目录> [--group-size 30]
      把一个对话切成分组文本（group_001.txt ...），每行带时间戳和来源 id：
      [2026-05-29T21:14][id=abc12345] 轩: 内容
      同时写 meta.json 记录锚点（done_groups）。提取记忆时逐组推进：
      一组的草稿全部审完入库后才把组号记进锚点，中断重跑不漏不重。

设计纪律（见 docs/项目计划.md 第 4 节）：
  - 原文只读，不改写不搬动；本脚本只产出工作文件
  - 单条消息解析失败记录跳过，不崩整个批次（软失败）
"""

import argparse
import json
import sys
from pathlib import Path


def load_export(path: str) -> list[dict]:
    # utf-8-sig：兼容带 BOM 头的文件（部分导出工具会加）
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        data = [data]  # 单对话文件顶层可能直接是对象
    if not isinstance(data, list):
        raise SystemExit(f"预期顶层是对话列表或对话对象，拿到 {type(data).__name__}")
    return data


def conv_messages(conv: dict) -> list[dict]:
    """官方导出用 chat_messages，浏览器插件导出用 messages。"""
    return conv.get("chat_messages") or conv.get("messages") or []


def message_time(msg: dict) -> str:
    return msg.get("created_at") or msg.get("createdAt") or ""


def message_text(msg: dict) -> str:
    """text 字段优先；content / contentBlocks 块列表只取 text 块。

    thinking 块是模型内心独白，不算说出口的话，提取记忆时跳过。
    searchText 混入了 thinking 内容，同理不用。
    """
    if msg.get("text"):
        return msg["text"]
    blocks = msg.get("content") or msg.get("contentBlocks") or []
    parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    return "\n".join(p for p in parts if p)


def conv_time_range(conv: dict) -> tuple[str, str]:
    times = [message_time(m) for m in conv_messages(conv) if message_time(m)]
    return (min(times)[:10], max(times)[:10]) if times else ("?", "?")


def cmd_inventory(args) -> None:
    convs = load_export(args.export)
    print(f"共 {len(convs)} 个对话：\n")
    for i, c in enumerate(convs):
        n = len(conv_messages(c))
        start, end = conv_time_range(c)
        print(f"[{i:3d}] {start} ~ {end}  {n:4d} 条  {c.get('name', '(无标题)')}  uuid={c.get('uuid', '?')[:8]}")


def find_conversation(convs: list[dict], key: str) -> dict:
    hits = [c for c in convs if key in (c.get("name") or "") or (c.get("uuid") or "").startswith(key)]
    if not hits:
        raise SystemExit(f"没找到标题或 uuid 匹配「{key}」的对话，先用 inventory 看看库存。")
    if len(hits) > 1:
        names = "\n".join(f"  - {c.get('name')} (uuid={c.get('uuid', '')[:8]})" for c in hits)
        raise SystemExit(f"匹配到 {len(hits)} 个对话，说得再具体点：\n{names}")
    return hits[0]


def cmd_slice(args) -> None:
    convs = load_export(args.export)
    conv = find_conversation(convs, args.conversation)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    speaker = {"human": args.human, "assistant": args.assistant}
    lines, skipped = [], 0
    for msg in conv_messages(conv):
        try:
            text = message_text(msg).strip()
            if not text:
                continue
            ts = message_time(msg)[:16]  # 2026-05-29T21:14
            mid = (msg.get("uuid") or "")[:8]
            who = speaker.get(msg.get("sender", ""), msg.get("sender", "?"))
            lines.append(f"[{ts}][id={mid}] {who}: {text}")
        except Exception as e:  # 软失败：单条坏了不崩批次
            skipped += 1
            print(f"  跳过一条消息（{e}）", file=sys.stderr)

    groups = [lines[i : i + args.group_size] for i in range(0, len(lines), args.group_size)]
    for gi, group in enumerate(groups, start=1):
        (out / f"group_{gi:03d}.txt").write_text("\n\n".join(group), encoding="utf-8")

    meta = {
        "export_file": str(Path(args.export).resolve()),
        "conversation_uuid": conv.get("uuid"),
        "conversation_name": conv.get("name"),
        "group_size": args.group_size,
        "total_messages": len(lines),
        "total_groups": len(groups),
        "skipped_messages": skipped,
        "done_groups": [],  # 锚点：这一组的草稿全部审完入库后，才把组号加进来
    }
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"「{conv.get('name')}」：{len(lines)} 条消息 → {len(groups)} 组（每组 ≤{args.group_size}），跳过 {skipped} 条")
    print(f"工作目录：{out}（meta.json 里的 done_groups 是断点锚点）")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_inv = sub.add_parser("inventory", help="列出导出里的所有对话")
    p_inv.add_argument("export", help="conversations.json 路径")
    p_inv.set_defaults(func=cmd_inventory)

    p_slice = sub.add_parser("slice", help="把一个对话切成带来源 id 的分组文本")
    p_slice.add_argument("export", help="conversations.json 路径")
    p_slice.add_argument("--conversation", required=True, help="对话标题子串或 uuid 前缀")
    p_slice.add_argument("--out", required=True, help="工作目录（产出 group_*.txt 和 meta.json）")
    p_slice.add_argument("--group-size", type=int, default=30, help="每组消息数（默认 30）")
    p_slice.add_argument("--human", default="轩", help="human 侧显示名")
    p_slice.add_argument("--assistant", default="Claude", help="assistant 侧显示名")
    p_slice.set_defaults(func=cmd_slice)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
