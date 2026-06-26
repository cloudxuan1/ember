# ember-proxy （Cloudflare Worker）

前端 → **这个 Worker** → OpenRouter。
它只干两件事：给请求加上 API key（key 存在 Cloudflare Secret 里，不进代码）、把流式回复透传回前端。

---

## 部署：二选一

### 方式 A — Cloudflare 网页后台（最简单，不用装东西）

1. 登录 [dash.cloudflare.com](https://dash.cloudflare.com) → 左侧 **Workers & Pages** → **Create** → **Create Worker**。
2. 给它起个名（建议 `ember-proxy`）→ **Deploy**（先随便部署一版）。
3. 点 **Edit code**，把本目录 [`worker.js`](worker.js) 的全部内容粘进去，覆盖默认代码 → **Deploy**。
4. 回到 Worker 页 → **Settings** → **Variables and Secrets** → **Add**：
   - 类型选 **Secret**
   - 名称填 `OPENROUTER_API_KEY`
   - 值填你的 OpenRouter key（在 [openrouter.ai/keys](https://openrouter.ai/keys) 申请）
   - **Save / Deploy**
5. Worker 页顶部会显示它的网址，形如 `https://ember-proxy.你的子域.workers.dev`。**复制这个网址。**

### 方式 B — wrangler 命令行

```bash
cd worker
npx wrangler deploy
npx wrangler secret put OPENROUTER_API_KEY   # 按提示粘贴 key
```
部署完命令行会打印 Worker 网址。

---

## 部署后

把上一步拿到的 Worker 网址，填进项目根目录 [`../index.html`](../index.html) 顶部的 `WORKER_URL` 常量，替换占位地址。

---

## 注意

- `ALLOWED_ORIGIN`（worker.js 顶部）写死成了 `https://cloudxuan1.github.io`。如果你的 GitHub Pages 域名不是这个，改成你的，否则浏览器会因 CORS 拦截请求。
- 默认模型 `anthropic/claude-opus-4.6` 写在 worker.js 顶部；前端不传 model 时用它。
