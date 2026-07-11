# codex-api-service

[English README](README_en.md)

本项目把本机 Codex OAuth 登录封装成 OpenAI-compatible API 服务，默认监听：

```text
http://127.0.0.1:1219/v1
```

支持：

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /anthropic/messages`
- `POST /anthropic/v1/messages`
- `GET /anthropic/v1/models`
- `GET /ui` 本地控制台
- `GET /admin/health` 控制台运行状态
- `GET /admin/models` 控制台模型目录
- 非流式和 `stream: true` 流式 SSE
- 成功响应后写入 `.codex-usage/usage.jsonl`

## 安装依赖

```bash
source .venv/bin/activate
pip install -e ".[dev]"
```

## Codex OAuth

服务会优先使用本服务 auth 文件；如果没有，会自动导入已有 Codex OAuth 文件，默认位置是 `~/.codex/auth.json`。

你可以先完成 Codex 登录：

```bash
codex
```

如果本机没有可用 token，首次 API 调用会触发浏览器 OAuth 登录流程。

## 配置

公开仓库只提供 `config_example.yaml`。本机使用时复制一份为 `config.yaml`，这个文件默认会被 `.gitignore` 忽略，避免把本地密钥、路径或个人配置提交出去。

```bash
cp config_example.yaml config.yaml
```

如果删除或缺失 `config.yaml`，服务会使用内置默认值。

常用字段：

```yaml
server:
  host: 127.0.0.1
  port: 1219

api:
  local_api_key:

codex:
  default_model: gpt-5.5
  model_request_defaults:
    gpt-5.5:
      reasoning_effort: high
      fast_mode: true

usage:
  enabled: true
  path: .codex-usage/usage.jsonl
  source: codex-api-service
  channel: Codex API Service
  provider: openai-codex
  auth: codex-oauth
  api_surface: chatgpt-codex-responses

auth:
  auth_path:
  import_auth_path:
```

也可以用环境变量临时设置本地 API key：

```bash
export CODEX_API_SERVICE_KEY=local-secret
```

## 最简单启动方式

在项目目录执行：

```bash
source .venv/bin/activate
python -m codex_api_service.app
```

看到 `Uvicorn running on http://127.0.0.1:1219` 就表示启动了。这个方式会占住当前终端，关闭终端服务就停。
启动时也会打印可复制地址；如果 `server.host` 是 `0.0.0.0`，还会额外打印局域网地址：

```text
Codex API Service starting
  Server:      http://127.0.0.1:1219
  Local API:   http://127.0.0.1:1219/v1
  Local UI:    http://127.0.0.1:1219/ui
  Health:      http://127.0.0.1:1219/health
```

另开一个终端检查：

```bash
curl http://127.0.0.1:1219/health
curl http://127.0.0.1:1219/v1/models
curl http://127.0.0.1:1219/admin/models
```

启动前查看有效配置，输出不会包含密钥：

```bash
python -m codex_api_service.app --print-config
```

## 挂成后台服务

后台服务脚本会优先使用项目 `.venv`；如果找不到虚拟环境，会打印 `WARNING` 并回退到全局 Python。

### macOS

macOS 使用 launchd 用户服务。安装并启动：

```bash
bash scripts/install_launchd_service.sh
```

安装后服务名是：

```text
com.codex-api-service.local
```

常用管理命令：

```bash
# 查看是否加载。
launchctl print gui/$(id -u)/com.codex-api-service.local

# 重启服务。
launchctl kickstart -k gui/$(id -u)/com.codex-api-service.local

# 停止并卸载服务。
bash scripts/uninstall_launchd_service.sh
```

日志位置：

```text
./logs/launchd.out.log
./logs/launchd.err.log
```

### Ubuntu

Ubuntu 使用 systemd 用户服务，不需要 sudo。安装并启动：

```bash
bash scripts/install_systemd_user_service.sh
```

安装后服务名是：

```text
codex-api-service.service
```

常用管理命令：

```bash
# 查看状态。
systemctl --user status codex-api-service.service

# 重启服务。
systemctl --user restart codex-api-service.service

# 查看实时日志。
journalctl --user -u codex-api-service -f

# 停止并卸载服务。
bash scripts/uninstall_systemd_user_service.sh
```

### Windows

Windows 使用当前用户的 Task Scheduler 计划任务。用 PowerShell 在项目目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1
```

安装后任务名是：

```text
CodexApiService
```

常用管理命令：

```powershell
# 查看任务。
Get-ScheduledTask -TaskName CodexApiService

# 手动启动任务。
Start-ScheduledTask -TaskName CodexApiService

# 停止任务。
Stop-ScheduledTask -TaskName CodexApiService

# 停止并卸载任务。
powershell -ExecutionPolicy Bypass -File scripts\uninstall_windows_task.ps1
```

日志位置：

```text
.\logs\windows.out.log
.\logs\windows.err.log
```

## 本地控制台

服务启动后打开：

```text
http://127.0.0.1:1219/ui
```

控制台包含三个页面：

- 聊天：直接用本服务的 `/v1/chat/completions` 流式聊天。
- 请求日志：查看最近请求的接口、模型、状态、耗时和 token 用量，支持筛选和展开详情。
- 配置：编辑默认模型、usage、认证路径和 API key。
- 模型配置：按 Codex CLI 动态目录为每个真实模型设置默认 reasoning effort 和快速模式。
- 状态条：展示 OAuth、模型请求配置、usage 写入和 Codex CLI 版本状态。

聊天区支持 Markdown fenced code block 展示和代码块复制。

公开仓库不提交 `codex_api_service/static/ui/` 构建产物。首次克隆或修改 `frontend/` 源码后，需要重新构建控制台：

```bash
npm --prefix frontend install
npm --prefix frontend run build
```

## OpenAI SDK 示例

调用方未显式传参时，服务使用 `codex.model_request_defaults` 中对应真实模型的缺省值；未配置的模型固定使用
`reasoning_effort="medium"` 且关闭快速模式。请求体可以用 `reasoning_effort`、`reasoning.effort`、
`fast_mode` 或 `service_tier` 临时覆盖模型缺省值。
服务会把快速模式映射成当前 Codex OAuth backend 可用的 `service_tier="priority"`。

兼容层会接收 OpenAI SDK 常见参数，例如 `temperature`、`top_p`、`max_tokens`、
`max_output_tokens`、`response_format`、`tools`、`tool_choice`、`stop`、`seed` 和
penalty 参数。Codex OAuth backend 当前不支持这些采样、工具和格式参数，所以服务会本地忽略它们，
避免上游返回 `Unsupported parameter`。

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:1219/v1",
    api_key="local-secret",  # 未配置 local_api_key 时可以填任意字符串。
)

response = client.chat.completions.create(
    model="gpt-5.5",
    messages=[{"role": "user", "content": "你好，介绍一下你自己"}],
    extra_body={"fast_mode": False},  # 显式参数优先；不填则使用该模型的 Web/YAML 缺省值。
)

print(response.choices[0].message.content)
```

流式：

```python
stream = client.chat.completions.create(
    model="gpt-5.5",
    messages=[{"role": "user", "content": "写一个 Python 快排"}],
    stream=True,
    stream_options={"include_usage": True},
)

for chunk in stream:
    if chunk.choices:
        print(chunk.choices[0].delta.content or "", end="")
```

Responses API：

```python
response = client.responses.create(
    model="gpt-5.5",
    input="用三句话解释 Codex OAuth 本地代理服务",
)

print(response.output_text)
```

Anthropic Messages API：

```python
from anthropic import Anthropic

client = Anthropic(
    base_url="http://127.0.0.1:1219/anthropic",
    api_key="local-secret",  # 对应 config.yaml 的 local_api_key；未配置时可以填任意字符串。
)

message = client.messages.create(
    model="gpt-5.5",
    max_tokens=1024,
    system="保持简洁",
    messages=[{"role": "user", "content": "你好，介绍一下你自己"}],
)

print(message.content[0].text)
```

Anthropic 流式：

```python
with client.messages.stream(
    model="gpt-5.5",
    max_tokens=1024,
    messages=[{"role": "user", "content": "写一个 Python 快排"}],
) as stream:
    for text in stream.text_stream:
        print(text, end="")
```

`/anthropic/messages` 和 `/anthropic/v1/messages` 会把 Anthropic `tools` 转成 Responses function tools，把 `tool_choice` 转成
Responses tool choice。模型返回 function call 时，非流式响应会输出 `tool_use` content block；
流式响应会输出 `content_block_start`、`input_json_delta` 和 `content_block_stop`。

有些网关会在 provider base URL 后硬拼 `/v1/models` 做连接测试；请把 base URL 填成
`http://127.0.0.1:1219/anthropic`，不要填到 `/anthropic/messages`。它会命中
`/anthropic/v1/models`。该接口返回 Anthropic-native 模型列表格式；为了通过 Claude-3p
对模型名的 Anthropic family 过滤，模型 id 和 `display_name` 都会使用不含 `gpt`、`codex`
或 `openai` 的 `claude-sonnet-...` 发现别名，例如 `claude-sonnet-5-5`，并带上
`anthropic_family_tier="sonnet"`。请求 `/anthropic/messages` 时，这个发现别名、旧版
`claude-gpt-...` 别名以及裸 `sonnet` tier 都会自动映射回真实 Codex 模型名。
Claude-3p 网关推理会把 base URL 拼成 `/anthropic/v1/messages`，该路径也会走同一套映射。

图片输入支持 Anthropic `image` content block：

- `source.type = "base64"` 会转成 `data:<media_type>;base64,...` 的 `input_image`
- `source.type = "url"` 会转成 URL 形式的 `input_image`

## 用量日志

当 Codex backend 返回真实 usage 时，服务会追加写入：

```text
.codex-usage/usage.jsonl
```

日志只包含 token 统计和运行元数据，不记录 prompt、completion、OAuth token、API key、Authorization header 或完整请求/响应正文。

控制台请求日志会持久化到：

```text
./logs/requests.jsonl
```

该日志同样只记录请求元数据，不记录 prompt 或响应正文。

## 测试

```bash
python -m pytest -v
```
