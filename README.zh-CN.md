# ChatGPT-Web2API 中文说明

把 ChatGPT 网页版变成本地 OpenAI 兼容 API 和 MCP 服务。

本项目不逆向 token、不手动处理 Turnstile/PoW，也不直接模拟 ChatGPT 后端完整协议。它启动一个真实 Chrome 浏览器，通过 Chrome DevTools Protocol（CDP）控制 `chatgpt.com` 页面：输入消息、点击发送、读取回复，再对外提供 HTTP API 和 MCP 工具。

## 项目定位

本仓库的主要使用目标，是把 ChatGPT Web 中的 GPT-5.5 Pro / Pro 扩展模式接入本机环境，并架设成 MCP 服务器，供 Codex 在进行 coding plan、方案设计、架构评审和复杂实现拆解时调用。

换句话说，它更像是 Codex 的“教师模型”或“外部评审模型”通道：Codex 负责读代码、改代码和执行本地验证；GPT-5.5 Pro 通过 MCP 提供更强的规划、推理和交叉审查能力。这样可以在不直接使用 OpenAI API key 的情况下，复用已登录 ChatGPT 网页端的模型能力。

## 适用场景

- 你有 ChatGPT Plus/Pro 账号，希望在本机或可信服务器上通过程序调用 ChatGPT Web。
- 你想让 Codex、Claude Desktop、Cursor 等 MCP 客户端复用已登录的 ChatGPT 网页会话。
- 你希望使用 OpenAI Python SDK 兼容格式调用本地代理。
- 你需要访问 ChatGPT Projects、Custom GPTs、会话列表、记忆等网页端能力。
- 你希望把 GPT-5.5 Pro / Pro 扩展模式作为 Codex coding plan 的教师模型或评审模型。

## 工作原理

```text
你的代码 / MCP 客户端
        |
        | OpenAI-compatible HTTP / MCP
        v
ChatGPT-Web2API
        |
        | Chrome DevTools Protocol
        v
真实 Chrome 浏览器（已登录 chatgpt.com）
```

核心流程：

1. 启动 Chrome，并开启本地 CDP 调试端口。
2. 连接到 `chatgpt.com` 页面对应的 CDP websocket。
3. 在页面上下文里读取登录态和 access token。
4. 找到输入框 `#prompt-textarea`，插入消息。
5. 点击发送按钮。
6. 轮询 DOM 中新的 assistant 消息，流式返回增量文本。
7. 最后通过 ChatGPT conversation API 拉取最终文本做校准。

## 快速开始

### 本机运行

```bash
pip install chatgpt-web2api
chatgpt-web2api
```

第一次启动会打开 Chrome。请在浏览器里登录 ChatGPT，登录完成后服务会开始监听：

```text
http://127.0.0.1:8080
```

测试调用：

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"你好，简单介绍一下你自己"}]}'
```

### OpenAI SDK 示例

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8080/v1",
    api_key="not-needed",
)

response = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "2+2 等于多少？"}],
)

print(response.choices[0].message.content)
```

## Docker Compose 部署

服务器或无桌面环境建议使用 Docker Compose，并通过导出的 ChatGPT cookies 注入登录态。

1. 在本机浏览器打开 `chatgpt.com` 并登录。
2. 用 Cookie-Editor 等扩展导出 `chatgpt.com` cookies。
3. 保存到项目目录：

```bash
mkdir -p cookies
cp /path/to/cookies.json cookies/cookies.json
```

4. 启动服务：

```bash
docker compose up -d --build
```

5. 检查健康状态：

```bash
curl http://localhost:8080/health
```

Compose 默认只暴露 API 端口 `8080`。不要暴露 Chrome CDP 端口 `9222`，它可以控制已登录浏览器。

## MCP 使用

如果你已经运行了 `chatgpt-web2api`，可以在 MCP 客户端里配置：

```json
{
  "mcpServers": {
    "chatgpt": {
      "command": "chatgpt-web2api-mcp",
      "args": ["--cdp-port", "9222"]
    }
  }
}
```

MCP 服务会复用同一个 Chrome 登录会话，提供聊天、模型列表、项目、会话、记忆和 Custom GPT 等工具。

在 Codex 场景中，推荐把该 MCP 作为规划和评审辅助工具使用。例如：

- 让 Codex 先阅读本地代码并整理上下文。
- 通过 `chat_completion` 调用 GPT-5.5 Pro / Pro 扩展模式讨论技术方案。
- 把外部模型给出的建议转化为可执行 task list。
- 再由 Codex 在本地完成代码修改、测试和提交。

常见模型参数可以使用 `gpt-5-5-pro` 或项目中配置的 Pro 扩展别名；实际可用模型以 `list_models` 返回为准。

## 常用配置

环境变量：

```bash
W2A_PORT=8080
W2A_HOST=127.0.0.1
W2A_CDP_PORT=9222
W2A_HEADLESS=false
W2A_DEFAULT_MODEL=auto
W2A_API_KEYS=sk-local-1,sk-local-2
```

Docker 中会默认设置：

```bash
W2A_HOST=0.0.0.0
W2A_HEADLESS=true
W2A_CHROME_EXTRA_ARGS="--no-sandbox --disable-dev-shm-usage"
```

## 安全提醒

- 不要把 `9222` CDP 端口映射到公网或局域网。
- 如果 API 绑定 `0.0.0.0`，建议配置 `W2A_API_KEYS` 或只在可信内网使用。
- cookies 和 Chrome profile 等同于登录态，请不要提交到 Git。
- Docker 部署时，`cookies/` 已在 `.dockerignore` 中排除。
- 本项目适合个人或可信环境使用，不建议直接作为公网多用户服务。

## 已知限制

- ChatGPT 网页 DOM 改版可能导致选择器失效。
- 浏览器是单会话模型，同一实例一次只适合处理一个聊天请求。
- Headless Chrome 可能触发风控，cookies 过期后需要重新导出。
- 当前主要支持文本输入，图片/文件上传仍属于后续扩展方向。

## 更多文档

- 英文说明：[README.md](README.md)
- 部署指南：[docs/deployment.md](docs/deployment.md)
- API 参考：[docs/api-reference.md](docs/api-reference.md)
- 架构说明：[docs/architecture.md](docs/architecture.md)
