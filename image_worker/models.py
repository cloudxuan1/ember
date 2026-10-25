"""OpenRouter image models the worker accepts. Standard library only: the VPS probe script imports it too.

quality / references record what GET /api/v1/images/models/{id}/endpoints reports for each model;
scripts/openrouter-image-probe.py checks them against the live listing.
"""

API_BASE = "https://openrouter.ai/api/v1"
MAX_REFERENCES = 5  # Codex's native edit tool also stops at five.

OPENROUTER_MODELS = {
    "openai/gpt-image-2.5-flare": {"label": "Flare", "use": "日常、快", "quality": True, "references": True},
    "openai/gpt-image-2.5-sunburst": {"label": "Sunburst", "use": "定稿、精准", "quality": True, "references": True},
    "google/gemini-3.1-flash-image": {"label": "Nano Banana 2", "use": "Gemini 3.1 Flash Image", "quality": False, "references": True},
    "bytedance-seed/seedream-5-0-flash": {"label": "Seedream 5 Flash", "use": "便宜，试方向用", "quality": False, "references": True},
}

CODEX_LABEL = "Codex 订阅（gpt-image-2）"


def label(model):
    return OPENROUTER_MODELS[model]["label"] if model else CODEX_LABEL


def request_body(model, prompt, reference_urls=()):
    """The exact POST /api/v1/images body. Only parameters the model's endpoints list are sent."""
    spec = OPENROUTER_MODELS[model]
    body = {"model": model, "prompt": prompt, "n": 1}
    if spec["quality"]:
        body["quality"] = "high"
    if reference_urls:
        body["input_references"] = [{"type": "image_url", "image_url": {"url": url}} for url in reference_urls]
    return body
