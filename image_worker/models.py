"""OpenRouter image models the worker accepts. Standard library only: the VPS probe script imports it too.

Each entry copies that model's own GET /api/v1/images/models/{id}/endpoints (checked one by one, 2026-11-05):
params = optional parameters with the values (enum) or (min, max) range the endpoint allows; references = its
input_references max; passthrough = keys its allowed_passthrough_parameters accept, sent under
provider.options[provider] (provider = the endpoint's provider_slug). scripts/openrouter-image-probe.py
compares all of it with the live listing.
"""

API_BASE = "https://openrouter.ai/api/v1"
CODEX_REFERENCES = 5  # Codex's native edit tool stops at five.
DEFAULT_QUALITY = "high"  # sent to models with a quality knob when the caller picks none (as since #34)

# GPT Image 2.5 Flare and Sunburst list exactly these. Their endpoints also take output_compression, but
# without output_format they only return PNG, and OpenAI rejects compression for PNG, so it is not offered.
GPT_IMAGE_2_5 = {
    "quality": ("auto", "low", "medium", "high", "xhigh", "max"),
    "aspect_ratio": ("1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "21:9", "auto"),
    "background": ("auto", "transparent", "opaque"),
    "n": (1, 10),
}
# GPT Image 2.6 Flare and Sunburst: the same four, plus output_format and output_compression.
GPT_IMAGE_2_6 = {**GPT_IMAGE_2_5, "output_format": ("png", "jpeg", "webp"), "output_compression": (0, 100)}
# The 2.6 endpoints only name it in allowed_passthrough_parameters; the values are from the 2.6 model pages:
# "the complexity control (low, medium, or high)".
COMPLEXITY = ("low", "medium", "high")

OPENROUTER_MODELS = {
    "openai/gpt-image-2.6-flare": {
        "label": "Flare 2.6", "use": "快，日常出图、改图", "references": 16,
        "params": GPT_IMAGE_2_6, "provider": "openai", "passthrough": {"complexity": COMPLEXITY},
    },
    "openai/gpt-image-2.6-sunburst": {
        "label": "Sunburst 2.6", "use": "细节多，连续改图也能保住细节", "references": 16,
        "params": GPT_IMAGE_2_6, "provider": "openai", "passthrough": {"complexity": COMPLEXITY},
    },
    "openai/gpt-image-2.5-flare": {
        "label": "Flare 2.5", "use": "日常、快", "references": 16,
        "params": GPT_IMAGE_2_5, "provider": "openai", "passthrough": {},
    },
    "openai/gpt-image-2.5-sunburst": {
        "label": "Sunburst 2.5", "use": "定稿、精准", "references": 16,
        "params": GPT_IMAGE_2_5, "provider": "openai", "passthrough": {},
    },
    # Three endpoints (google-vertex/us, google-ai-studio, google-vertex/global), all listing the same.
    # resolution is listed too but not offered.
    "google/gemini-3.1-flash-image": {
        "label": "Nano Banana 2", "use": "Google 的，快，复杂画面、反复改图都行", "references": 14,
        "params": {
            "aspect_ratio": ("1:1", "1:4", "1:8", "2:3", "3:2", "3:4", "4:1", "4:3", "4:5", "5:4", "8:1",
                             "9:16", "16:9", "21:9"),
            "n": (1, 1),
        },
        "passthrough": {},
    },
    # resolution and seed are listed too but not offered.
    "bytedance-seed/seedream-5-0-flash": {
        "label": "Seedream 5 Flash", "use": "便宜，试方向用", "references": 14,
        "params": {
            "aspect_ratio": ("1:1", "1:2", "2:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9",
                             "9:19.5", "19.5:9", "9:20", "20:9", "9:21", "21:9", "auto"),
            "n": (1, 1),
        },
        "passthrough": {},
    },
}

CODEX_LABEL = "Codex 订阅（gpt-image-2）"
# The optional parameters a caller may choose per image (n is separate: it makes several jobs).
OPTION_NAMES = ("quality", "complexity", "aspect_ratio", "background", "output_format", "output_compression")


def label(model):
    return OPENROUTER_MODELS[model]["label"] if model else CODEX_LABEL


def reference_limit(model):
    return OPENROUTER_MODELS[model]["references"] if model else CODEX_REFERENCES


def check_options(model, given):
    """Sort the caller's optional parameters for one model: (options to send, notes on the ones it cannot take).

    A value the model does not allow raises ValueError with a readable reason, so nothing is queued.
    quality equal to the default is dropped: that request is the same as choosing nothing.
    """
    spec = OPENROUTER_MODELS.get(model) if model else None
    options, ignored = {}, []
    for name in OPTION_NAMES:
        value = given.get(name)
        if value is None or value == "":
            continue
        if spec is None:
            ignored.append(f"{name}（不传 model 走 Codex 订阅，用不了）")
            continue
        allowed = spec["passthrough"].get(name) or spec["params"].get(name)
        if allowed is None:
            ignored.append(f"{name}（{spec['label']} 用不了）")
            continue
        if name == "output_compression":
            if isinstance(value, bool) or not isinstance(value, int) or not allowed[0] <= value <= allowed[1]:
                raise ValueError(f"output_compression 要是 {allowed[0]}–{allowed[1]} 的整数。")
        else:
            value = str(value).strip() if name == "aspect_ratio" else str(value).strip().lower()
            if value not in allowed:
                raise ValueError(f"{name} 不认识「{value[:40]}」；{spec['label']} 可选：{' / '.join(allowed)}。")
        options[name] = value
    if "output_compression" in options and options.get("output_format") not in ("jpeg", "webp"):
        raise ValueError("output_compression 只对 jpeg / webp 有用：要压缩就同时传 output_format=jpeg 或 webp。")
    if options.get("background") == "transparent" and options.get("output_format") == "jpeg":
        raise ValueError("透明背景（background=transparent）不能配 jpeg，jpeg 存不了透明；换 png 或 webp。")
    if options.get("quality") == DEFAULT_QUALITY:
        del options["quality"]
    return options, ignored


def request_body(model, prompt, reference_urls=(), options=None):
    """The exact POST /api/v1/images body: the model's default quality plus what check_options let through."""
    spec = OPENROUTER_MODELS[model]
    options = options or {}
    body = {"model": model, "prompt": prompt, "n": 1}
    if "quality" in spec["params"]:
        body["quality"] = options.get("quality", DEFAULT_QUALITY)
    body.update({name: value for name, value in options.items() if name in spec["params"] and name != "quality"})
    passthrough = {name: value for name, value in options.items() if name in spec["passthrough"]}
    if passthrough:
        # API reference, ProviderOptions: "keyed by provider slug ... Only options for the matched provider are forwarded".
        body["provider"] = {"options": {spec["provider"]: passthrough}}
    if reference_urls:
        body["input_references"] = [{"type": "image_url", "image_url": {"url": url}} for url in reference_urls]
    return body
