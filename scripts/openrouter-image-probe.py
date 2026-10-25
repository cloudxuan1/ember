#!/usr/bin/env python3
"""OpenRouter 生图核对，在 VPS 仓库根目录跑，只用标准库。

  python3 scripts/openrouter-image-probe.py              # 只核对白名单 ID 和参数，不花钱
  python3 scripts/openrouter-image-probe.py --generate   # 每个白名单模型真画一张，再拿一张做参考图改图（花钱）

密钥从 /root/ember/.env 的 OPENROUTER_IMAGE_KEY 读（--env-file 可改）：不打印、不写盘、不进命令行参数。
请求体用 image_worker/models.py 的 request_body，和 worker 发的一模一样。
屏幕只打印核对结果和数字；完整返回（图片 base64 换成长度和摘要）与图片存进 700 权限的私有目录。
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from image_worker.models import API_BASE, OPENROUTER_MODELS, request_body  # noqa: E402

PROMPT = "编的测试图：一只橘猫坐在窗台上看雨，水彩风格"
EDIT_PROMPT = "同一只猫、同样的画风，把背景换成雪天"


def read_key(env_file):
    for line in Path(env_file).read_text().splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "OPENROUTER_IMAGE_KEY":
            return value.strip().strip('"').strip("'")
    return ""


class Client:
    def __init__(self, key, base=API_BASE):
        self.key, self.base = key, base

    def redact(self, text):
        text = str(text).replace(self.key, "[已隐藏]") if self.key else str(text)
        return re.sub(r"sk-or-[A-Za-z0-9_-]+", "[已隐藏]", text)

    def call(self, method, path, body=None, timeout=60):
        request = urllib.request.Request(self.base + path, method=method,
                                         data=None if body is None else json.dumps(body).encode(),
                                         headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"})
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read(64 * 1024)
        elapsed = round(time.monotonic() - started, 1)
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"_not_json": self.redact(raw[:300].decode(errors="replace"))}
        return status, payload, elapsed


def strip_images(payload):
    """Replace base64 with length + digest so saved responses stay small and readable."""
    if isinstance(payload, dict):
        out = {}
        for key, value in payload.items():
            if key == "b64_json" and isinstance(value, str):
                out[key] = {"_chars": len(value), "_prefix": value[:30], "_sha256": hashlib.sha256(value.encode()).hexdigest()}
            else:
                out[key] = strip_images(value)
        return out
    if isinstance(payload, list):
        return [strip_images(item) for item in payload]
    return payload


def save(folder, name, data, redact=str):
    """Images are written as-is; everything JSON goes through the key redaction first."""
    path = folder / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data if isinstance(data, bytes) else redact(json.dumps(data, ensure_ascii=False, indent=2)).encode())


def generation(client, folder, name, body, results):
    status, payload, elapsed = client.call("POST", "/images", body, timeout=420)
    save(folder, name + ".response.json", strip_images(payload), client.redact)
    row = {"status": status, "seconds": elapsed, "top_keys": sorted(payload) if isinstance(payload, dict) else type(payload).__name__}
    data = payload.get("data") if isinstance(payload, dict) else None
    if status == 200 and isinstance(data, list) and data:
        item = data[0]
        row["data_count"], row["item_keys"], row["media_type"] = len(data), sorted(item), item.get("media_type")
        encoded = item.get("b64_json") or ""
        row["starts_with_data_url"] = encoded.startswith("data:")
        raw = base64.b64decode(encoded.partition(",")[2] if encoded.startswith("data:") else encoded)
        row["image_bytes"], row["magic"] = len(raw), raw[:8].hex()
        save(folder, name + ".image", raw)
        results.setdefault("_images", {})[name] = (raw, item.get("media_type") or "image/png")
    else:
        error = payload.get("error") if isinstance(payload, dict) else None
        row["error"] = client.redact(error.get("message") if isinstance(error, dict) else payload)[:300]
    usage = payload.get("usage") if isinstance(payload, dict) else None
    row["usage"] = usage
    row["cost"] = usage.get("cost") if isinstance(usage, dict) else None
    results[name] = row
    print(f"  {name}: HTTP {status}, {elapsed}s, cost={row['cost']}, media_type={row.get('media_type')}, "
          f"bytes={row.get('image_bytes')}, keys={row['top_keys']}" + (f", error={row['error']}" if "error" in row else ""))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default="/root/ember/.env")
    parser.add_argument("--generate", action="store_true", help="真画（花钱）")
    parser.add_argument("--reference-model", default="openai/gpt-image-2.5-flare")
    parser.add_argument("--out", help="私有目录，默认 /tmp/ember-openrouter-probe-<时间>")
    args = parser.parse_args()
    key = read_key(args.env_file)
    if not key:
        sys.exit("OPENROUTER_IMAGE_KEY 不在 " + args.env_file)
    print("密钥：在（不打印）")
    client = Client(key)
    folder = Path(args.out or "/tmp/ember-openrouter-probe-" + time.strftime("%Y%m%dT%H%M%S"))
    folder.mkdir(mode=0o700)
    summary = {"models": {}, "generate": {}}

    status, listing, _ = client.call("GET", "/images/models")
    save(folder, "images-models.json", listing, client.redact)
    items = listing.get("data") if status == 200 and isinstance(listing, dict) else None
    ids = {item.get("id") for item in items if isinstance(item, dict)} if isinstance(items, list) else set()
    print(f"GET /images/models: HTTP {status}, {len(ids)} 个模型")
    present = []
    for model, spec in OPENROUTER_MODELS.items():
        if model not in ids:
            author = model.split("/")[0]
            similar = sorted(i for i in ids if i and i.startswith(author + "/"))
            print(f"✗ {model} 不在清单里；同作者的有：{similar}")
            summary["models"][model] = {"present": False, "same_author": similar}
            continue
        present.append(model)
        status, endpoints, _ = client.call("GET", f"/images/models/{model}/endpoints")
        save(folder, model.replace("/", "__") + ".endpoints.json", endpoints, client.redact)
        params = {}
        for endpoint in endpoints.get("endpoints", []) if isinstance(endpoints, dict) else []:
            for name, descriptor in (endpoint.get("supported_parameters") or {}).items():
                params.setdefault(name, descriptor)
        quality = params.get("quality")
        quality_ok = quality is not None and ("values" not in quality or "high" in quality["values"])
        references_ok = "input_references" in params
        verdict = "OK" if (quality_ok, references_ok) == (spec["quality"], spec["references"]) else "要改 image_worker/models.py"
        print(f"✓ {model}: endpoints HTTP {status}；参数 {sorted(params)}；quality→{quality_ok}（白名单写 {spec['quality']}），"
              f"input_references→{references_ok}（白名单写 {spec['references']}）：{verdict}")
        summary["models"][model] = {"present": True, "parameters": params, "quality": quality_ok,
                                    "references": references_ok, "verdict": verdict}

    if args.generate:
        print("真画（每个模型一张）：")
        for model in present:
            generation(client, folder, model.replace("/", "__"), request_body(model, PROMPT), summary["generate"])
        images = summary["generate"].pop("_images", {})
        source = images.get(args.reference_model.replace("/", "__")) or next(iter(images.values()), None)
        if source and args.reference_model in present:
            raw, media_type = source
            url = f"data:{media_type};base64," + base64.b64encode(raw).decode()
            print("参考图改图：")
            generation(client, folder, "reference__" + args.reference_model.replace("/", "__"),
                       request_body(args.reference_model, EDIT_PROMPT, [url]), summary["generate"])
            summary["generate"].pop("_images", None)

    status, key_info, _ = client.call("GET", "/key")
    data = key_info.get("data", {}) if isinstance(key_info, dict) else {}
    numbers = {k: data.get(k) for k in ("limit", "limit_remaining", "limit_reset", "usage", "usage_daily")}
    print(f"GET /key: HTTP {status}，{numbers}")
    summary["key"] = {"status": status, **numbers}
    save(folder, "summary.json", summary, client.redact)
    print("私有目录：", folder)


if __name__ == "__main__":
    main()
