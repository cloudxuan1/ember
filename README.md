# ember

个人 AI 长期记忆中枢。核心是**关系的连续性**——窗口关闭不等于遗忘；项目协作记忆是有用的附加，不是主体。

长期方向：Claude.ai / ChatGPT / Claude Code / Codex / 自写客户端通过 MCP / REST 连接同一个记忆底座。

当前版本：**V2 记忆链路上线**（SQLite 三表 + 5 个 MCP 工具，claude.ai 可通过自定义 connector 连接；`MCP_PATH` 随机路径即访问密钥）。完整路线见 [docs/施工计划.md](docs/施工计划.md)。

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

VPS 部署与 Cloudflare Tunnel 配置见 [docs/项目详情.md](docs/项目详情.md) 的「部署和运维」。

## 文档

| 文件 | 用途 |
|---|---|
| `CLAUDE.md` | AI 协作规则 + 项目快照（每次会话必读） |
| `docs/项目详情.md` | 完整技术参考，按需查阅 |
| `docs/施工计划.md` | 版本路线 V0 → V7 与架构决定 |
