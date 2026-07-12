# ember

个人 AI 长期记忆中枢。核心是**关系的连续性**——窗口关闭不等于遗忘；项目协作记忆是有用的附加，不是主体。

长期方向：Claude.ai / ChatGPT / Claude Code / Codex / 自写客户端通过 MCP / REST 连接同一个记忆底座。

当前状态见 [交接.md](交接.md)（现状+待办，每次开工先读）。完整版本路线见 [docs/项目计划.md](docs/项目计划.md)。

## 本地运行

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8300
# 验证：curl http://127.0.0.1:8300/health
```

## Docker 运行

本地测试只启 ember 服务（不带 tunnel，无需 TUNNEL_TOKEN）：

```bash
docker compose up -d --build ember
curl http://127.0.0.1:8300/health
```

VPS 部署才全量启动（含 tunnel，需先在 `.env` 填 TUNNEL_TOKEN）：

```bash
docker compose up -d --build
```

## 部署

VPS 部署与 Cloudflare Tunnel 配置见 [交接.md](交接.md) 的「技术参考 → 部署和运维」。

## 客户端接入

MCP 端点带 OAuth 门禁（随机路径 + 授权页口令 + Bearer token 三层）。Claude Code / claude.ai / ChatGPT 各自的接入方式见 [交接.md](交接.md) 的「技术参考 → MCP」一节。

## 文档

| 文件 | 用途 |
|---|---|
| `CLAUDE.md` | AI 协作规则（基本不变，每次会话必读） |
| `交接.md` | 唯一状态源：仪表盘（现状+待办）+ 历史日志 + 技术参考，每次开工先读 |
| `docs/项目计划.md` | 设计决定为什么这么定 + 版本路线 V0 → V7，不常翻 |
| `docs/产出/` | 零散产出物（使用说明书、调研笔记等） |
