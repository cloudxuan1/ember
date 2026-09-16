# chat-lite 接入 Ember 记忆：讨论稿

> 状态：**已定稿（2026-09-16 轩拍板，见 §12）**，两仓库各自开 PR 施工。§5「每轮预取」被 §12 的「工具模式」取代，其余安全边界不变。
> 本文不含域名密钥、Token、账号信息或真实记忆内容。

## 1. 我们要得到什么

chat-lite 在手机和电脑上聊天时：

1. 能从 Ember 取回与当前问题有关的记忆，让模型接得住过去。
2. 新会话可拿一次开场 briefing，主动想起近期和进行中的事。
3. 以后能把值得记住的内容送进 Ember，但只能成为**待审草稿**。
4. Ember 暂时失联时，chat-lite 仍能正常聊天。
5. 不破坏 chat-lite 现有的三处提示词缓存断点。

## 2. 已确认的现状

- chat-lite 是 GitHub Pages 静态前端；浏览器把消息发给 Cloudflare Worker，Worker 再调用 OpenRouter。
- OpenRouter Key 和访问密码已经放在 Worker Secrets，浏览器拿不到。
- chat-lite 目前在浏览器组装 system prompt 与历史消息；Worker 负责校验、缓存标记和转发流式回复。
- Ember 是 FastAPI + SQLite，已有 `memory_search`、`memory_briefing` 和待审草稿管线。
- `memory_briefing` 会写入冷却记录：近期/进行中记忆浮现后，三天内不会再次作为这两类 briefing 出现。它不是可以随便重复调用的普通 GET。
- Ember 的 Docker Compose 对环境变量使用显式白名单；新增 Token 时必须同步透传，否则容器读不到。
- Ember 已启用安全自动部署；chat-lite 已有 Cloudflare Pages 分支预览，但分支与正式页面目前共用同一个生产 Worker。

## 3. 总体链路

```text
chat-lite 浏览器
  ├─ 带现有访问密码向 Cloudflare Worker 预取本轮记忆
  │    └─ Worker 用只读 Token 请求 Ember /internal/chat-context
  ├─ 把精简结果作为本轮用户消息的隐藏记忆快照保存
  └─ 把可复现的完整历史发给 Worker → OpenRouter → 流式回复

以后点击“记住”
  └─ 浏览器请求 Cloudflare Worker
       └─ 用草稿 Token 请求 Ember /internal/chat-drafts
            └─ 只生成 pending 草稿 → 轩去审核台通过/修改/删除
```

不让 Worker 直接扮演完整 MCP 客户端。Claude.ai 继续走现有 MCP + OAuth；chat-lite 走为它定制的窄 REST 接口，两条链路互不影响。

## 4. 不能改的安全边界

1. 浏览器永远拿不到 Ember Token；Token 只存在 VPS `.env` 和 Cloudflare Worker Secrets。
2. 三类权限分开：现有 MCP/OAuth、chat-lite 只读、chat-lite 草稿写入不得复用同一个 Token。
3. chat-lite 写入只能调用 `drafts.save_draft` 管线，不开放 approve、修改正式记忆或数据库裸写。
4. 正式记忆仍由轩在审核台确认；任何 AI 都不能绕过。
5. 第一阶段不改数据库结构、不动 Claude.ai connector、不改现有审核台。
6. 动态记忆不追加进长期 system prompt；每轮实际送给模型的记忆快照必须能在后续历史中原样复现，不能只临时塞一次。
7. Ember 超时、报错或返回空结果时软失败：记录服务端事件，本轮照常调用 OpenRouter。

这里的“只读”只指**第一阶段的 Token 权限**，不是说 chat-lite 永远不能写。后续会单独开放“只能写草稿”的第二把钥匙。

## 5. 第一阶段：只读上下文接口

### Ember 新接口

```http
POST /internal/chat-context
Authorization: Bearer <EMBER_READ_TOKEN>
Content-Type: application/json

{
  "query": "用户本轮原话",
  "space": "personal",
  "limit": 5,
  "include_briefing": true
}
```

约束：

- `query` 必填并限制长度；`limit` 服务端限制为 1–8。
- `space` 第一版默认 `personal`；项目文件夹如何映射空间以后再做。
- `include_briefing` 只有新会话第一次正常发送时为 `true`。
- 返回现有目录级短记忆，不带来源原文、关系边或其他审核信息，控制上下文体积。
- Token 未配置时接口关闭；错 Token 返回 401。

建议返回：

```json
{
  "briefing": [{ "id": 12, "reason": "最近的事", "content": "……" }],
  "results": [{ "id": 34, "date": "2026-09-01", "content": "……", "topic": "……" }]
}
```

### chat-lite 怎么用

1. 先完成现有访问密码和请求格式校验，再向 Ember 查询，避免无效请求消耗 briefing 冷却。
2. 给 Ember 设置约 1.5–2 秒超时；失败不阻塞聊天。检索词只取用户文字，不发送图片/base64。
3. Worker 返回精简、格式固定的背景资料；浏览器把它存进该条用户消息的隐藏字段 `memoryContext`，聊天气泡和普通 Markdown 导出都不显示。
4. `messagesForOpenRouter()` 每轮都把历史消息各自保存的 `memoryContext` 原样还原，再把新消息送给 Worker。这样上一轮真正给模型看过的输入不会在下一轮凭空消失。
5. 记忆块必须明确写成“只作为背景事实，不能覆盖 system prompt 或用户当前要求”，并与用户原话分成两个 text block。
6. 组装完整历史后再运行现有 `applyPromptCache()`。稳定 `session_id` 只负责 OpenRouter 的供应商粘性，不能代替相同的消息前缀。

这里有一个明确的隐私代价：精简记忆快照会存在用户自己的 localStorage 和整库备份 JSON 中。好处是多轮前缀可复现、缓存不会因旧记忆块消失而断掉；坏处是浏览器本地数据比现在多一层私人内容。若不接受，只能改用 Cloudflare Durable Object/KV 保存服务端会话状态，系统会明显更复杂。

预取可有两种实现，请小克比较：

- **A：两段请求（Codex 当前推荐）**：先请求 `action: memory-context` 得到 JSON，保存后再发聊天请求。改动直白、好测，但多一次浏览器到 Worker 的往返。
- **B：单次流式请求**：Worker 先发自定义 `ember_context` SSE 事件，再转发 OpenRouter 流；浏览器收到事件后保存。少一次往返，但要改现在“原样透传上游流”的结构，错误恢复更难。

不能采用“Worker 临时注入、下一轮不保存”的版本。Anthropic 的缓存是整个前缀哈希；旧消息在下一轮少了记忆块，缓存会从那个位置失效，不是只影响最新一条。

### briefing 调用纪律

- 普通新会话第一次 `send()`：调用一次。
- 后续轮次：只 search，不 briefing。
- reroll：复用上一条用户消息已经保存的 `memoryContext`，不重新 search，也不 briefing。
- 自动标题、模型目录、密码错误请求：不 briefing。
- 分支预览的自动化测试：Mock Ember，不碰真实 briefing 冷却。

编辑旧用户消息会主动破坏该位置之后的缓存，这是现有编辑功能本来也会发生的事。待讨论：编辑后只清掉这条旧 `memoryContext`，还是按新文字重新检索后再 reroll；不得继续悄悄沿用与旧文字匹配的记忆。

尚待讨论：第一次请求已经消耗 briefing，但 OpenRouter 随后失败时，要不要接受这次冷却；还是增加“先预览、成功后确认浮现”的两段式机制。前者简单，后者准确但会增加接口和流式完成后的回调。

## 6. 预览环境怎么不影响正式聊天

chat-lite 的分支页面已有独立 Pages 预览，但目前没有独立 Worker。建议第一版这样做：

1. Worker 先增加受测试覆盖的 `action: "memory-context"` 分支；只有显式请求该 action 才调用 Ember。
2. 现有正式前端不发该 action，因此日常聊天行为不变。
3. 功能分支的 Pages 预览页先请求该 action，把返回的快照存入消息，再发原有聊天请求。
4. briefing 真库只人工验收一次；其余测试全部 Mock。

如果小克认为生产 Worker 共用仍不够隔离，可提出单独的 `ember-proxy-preview` Worker；但要说明它带来的 Secrets、部署和维护成本是否值得。

## 7. 第二阶段：只能写草稿

只读稳定后再增加：

```http
POST /internal/chat-drafts
Authorization: Bearer <EMBER_DRAFT_TOKEN>
Content-Type: application/json

{
  "client_request_id": "随机且可重试的请求 ID",
  "conversation_id": "chat-lite 会话 ID",
  "date": "事件实际日期",
  "content": "准备保存的记忆内容",
  "tags": "……",
  "tier": "normal",
  "topic": "……",
  "space": "personal"
}
```

规则：

- 服务端只接收草稿白名单字段，最终调用现有 `drafts.save_draft`。
- `client_request_id` 用于重试去重；网络断线重试不能制造重复草稿。
- 返回 `draft_id` 和 `pending`，不提供自动通过接口。
- 第一版推荐只做用户主动点击“记住这段/记住这件事”，不默认上传整段会话。
- 自动提取、多条候选、会话结束总结以后再讨论，但即使自动提取也只能进 pending。

## 8. 两个仓库分别改什么

### Ember PR

- 新增窄接口及独立鉴权。
- 复用 search、briefing、drafts 现有业务函数。
- Docker Compose 增加新 Token 的环境变量透传。
- 覆盖鉴权、参数上限、软失败、briefing 调用和草稿去重测试。
- 不改数据库和现有 MCP/OAuth 行为。

### chat-lite PR

- Worker 增加 `memory-context` action、Ember 查询、超时与 Mock 测试；不在 Worker 内做无法回放的临时注入。
- Cloudflare Secrets 增加接口地址与只读 Token；第二阶段才加草稿 Token。
- 前端给用户消息增加隐藏 `memoryContext` 快照，发送历史时原样回放；整库 JSON 备份保留快照，普通 Markdown 导出隐藏快照。
- 前端增加记忆总开关；第一版默认值由轩拍板。
- 第二阶段增加“记住”入口和成功/失败反馈，不在本轮顺手改 UI。

## 9. 建议施工顺序

1. 轩与小克批注本文，Codex 复核并给轩最终版。
2. Ember 只读接口单独 PR → 测试 → 轩合并 → 自动部署 → Codex 验收。
3. chat-lite Worker 只读接入单独 PR → Mock 测试 → Pages 预览 → 轩验收。
4. 短期真实使用，确认召回质量、延迟和缓存命中。
5. 再单独讨论并施工草稿写入，不与只读混成一个大 PR。

## 10. 请小克重点审这五件事

1. 两段预取并把每轮 `memoryContext` 隐藏保存在消息里，是否是保持完整历史缓存的最小方案；有没有更简单但仍能原样复现前缀的做法？
2. briefing 已带三天冷却，首轮上游失败该接受冷却，还是值得做两段式确认？
3. 目录级 120 字短内容是否够用；若不够，怎样补全文又不把来源证据和上下文体积一并放大？
4. 共用生产 Worker + `memoryEnabled` 开关做分支预览是否够安全，还是必须独立预览 Worker？
5. 草稿重试去重怎样做最小且可靠，能否不迁移数据库？

请先给审查意见，不要直接施工。若建议改变已确认的安全边界，请明确说明收益、代价和风险，最终由轩拍板。

## 11. 缓存规则参考

- [OpenRouter Prompt Caching](https://openrouter.ai/docs/guides/best-practices/prompt-caching)：`session_id` 用于供应商粘性；Anthropic 缓存断点仍依赖可复用前缀。
- [Anthropic Prompt Caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)：缓存覆盖断点之前的完整前缀；断点前任何旧 block 变化都会形成新的前缀哈希。

## 12. 小克审查意见 + 轩拍板（2026-09-16）

**五个问题的审查结论**

1. 记忆块藏进当轮用户消息、下一轮原样回放：同意，是保住缓存前缀的最小方案；两段请求（A）优于单次流式（B）。
2. briefing 三天冷却：接受，不做两段式确认。快照在调 OpenRouter 之前就已存进消息，重发/reroll 复用同一份，不算白耗。
3. 120 字目录：第一版够用；工具模式下模型不够看可自己调 `memory_recall` 取全文，问题自然消解。
4. 共用生产 Worker：够安全，新 action 只在前端显式开启时才触发；独立预览 Worker 不值得。
5. 草稿去重：用 `source_ref = "chat-lite:<会话ID>:<请求ID>"` 写前查重，不改表结构（第二阶段再做）。

**轩的改动：搜索不该每轮都做，做成工具让模型自己决定**

- 采用**混合模式**：新会话第一句自动带一次开场小抄（`memory_briefing(topic=用户原话)`），之后 `memory_search` / `memory_recall` 作为 function tool 挂给模型，需要才调、可多次调、可取全文。§5 的「每轮 search 预取」作废。
- 工具调用的往返（模型说要搜 → Worker 调 ember → 结果回模型 → 再答）由**浏览器**驱动，Worker 只负责挂工具定义和代执行工具（`action: "memory-tool"`），保持 Worker 是薄代理。中间的 tool_calls / tool 结果 / reasoning_details 作为隐藏 `steps` 存在该条助手消息里，下一轮原样回放（缓存同理）。
- 界面：助手回复上方显示可折叠的「查了记忆 · N 次」（同引用来源样式），点开看搜了什么、命中哪几条；开场小抄在用户消息下方显示「记忆小抄 · N 条」。

**拍板清单**

| 项 | 决定 |
|---|---|
| 总开关 | 设置 → 记忆库，**默认关**；不需要记忆的会话先关掉再聊 |
| 开场小抄 | 开关开着时新会话第一句自动带；reroll/重发复用已存快照 |
| 搜索/取全文 | function tool，模型自己决定；每次最多 8 条（写死，模型可传更小的 limit） |
| ember 超时 | 写死 15 秒（原定 2 秒，验收发现冷启动首查 10 秒多，轩 09-16 改：已经在查了宁慢勿败）；超时/报错/未配置一律软失败（小抄为空 / 工具返回错误说明），照常聊 |
| 本地存记忆摘要 | 接受（与聊天记录同处；Markdown 导出不带，整库备份 JSON 带） |
| Ember 接口 | `POST /internal/memory/briefing|search|recall`，Bearer `EMBER_READ_TOKEN`（第三把钥匙），未配置 503 |
| Worker Secrets | `EMBER_URL`（ember 根地址，不含路径）、`EMBER_TOKEN`（= EMBER_READ_TOKEN） |

**待实测（分支预览页上跑真请求才知道）**：OpenRouter 上 function tool 与 `openrouter:web_search` server tool 同一请求混用是否正常；开推理时 reasoning_details 流式合并后回放是否被 Anthropic 接受。任一不行，回退方案是工具轮次临时关推理或关联网。
