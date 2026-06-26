// ember 代理 Worker
// 作用：给前端请求偷偷加上 OpenRouter 的 API key，并把流式回复原样转回前端。
// API key 存在 Cloudflare 的 Secret 里（变量名 OPENROUTER_API_KEY），不写进代码、不暴露给前端。

// 只允许这个来源的网页调用（你的 GitHub Pages 域名）。换域名就改这一行。
const ALLOWED_ORIGIN = "https://cloudxuan1.github.io";

const OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions";
const DEFAULT_MODEL = "anthropic/claude-opus-4-6";

export default {
  async fetch(request, env) {
    // 浏览器发真正请求前会先发一个 OPTIONS 预检，这里直接放行。
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders() });
    }
    if (request.method !== "POST") {
      return json({ error: "只接受 POST 请求" }, 405);
    }

    let payload;
    try {
      payload = await request.json();
    } catch {
      return json({ error: "请求体不是合法 JSON" }, 400);
    }

    const { messages, model } = payload;
    if (!Array.isArray(messages) || messages.length === 0) {
      return json({ error: "messages 必须是非空数组" }, 400);
    }

    let upstream;
    try {
      upstream = await fetch(OPENROUTER_URL, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${env.OPENROUTER_API_KEY}`,
          "Content-Type": "application/json",
          "HTTP-Referer": ALLOWED_ORIGIN,
          "X-Title": "ember",
        },
        body: JSON.stringify({
          model: model || DEFAULT_MODEL,
          messages,
          stream: true,
        }),
      });
    } catch (err) {
      return json({ error: "连接 OpenRouter 失败：" + err.message }, 502);
    }

    // 把上游响应原样透传：成功时是 SSE 流，失败时是 JSON 错误体。再补上 CORS。
    const headers = corsHeaders();
    const ct = upstream.headers.get("Content-Type");
    if (ct) headers["Content-Type"] = ct;
    headers["Cache-Control"] = "no-cache";

    return new Response(upstream.body, { status: upstream.status, headers });
  },
};

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}

function json(obj, status = 200) {
  const headers = corsHeaders();
  headers["Content-Type"] = "application/json";
  return new Response(JSON.stringify(obj), { status, headers });
}
