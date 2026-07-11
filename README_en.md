# codex-api-service

[中文 README](README.md)

This project wraps a local Codex OAuth session as an OpenAI-compatible API service.
By default, it listens on:

```text
http://127.0.0.1:1219/v1
```

Supported endpoints and features:

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /anthropic/messages`
- `POST /anthropic/v1/messages`
- `GET /anthropic/v1/models`
- `GET /ui` local console
- `GET /admin/health` console runtime status
- `GET /admin/models` console model catalog
- Non-streaming responses and `stream: true` SSE streaming
- Successful requests can be written to `.codex-usage/usage.jsonl`

## Install

```bash
source .venv/bin/activate
pip install -e ".[dev]"
```

## Codex OAuth

The service first uses its own auth file. If no local service auth is available, it automatically imports an existing Codex OAuth file from the default location:

```text
~/.codex/auth.json
```

You can complete Codex login first:

```bash
codex
```

If no usable token exists on the machine, the first API request will start the browser OAuth flow.

## Configuration

The public repository only includes `config_example.yaml`. Copy it to `config.yaml` for local use.
`config.yaml` is ignored by Git by default, so local keys, paths, and personal settings are not committed.

```bash
cp config_example.yaml config.yaml
```

If `config.yaml` is missing, the service uses built-in defaults.

Common fields:

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

You can also set the local API key with an environment variable:

```bash
export CODEX_API_SERVICE_KEY=local-secret
```

## Start Locally

Run this from the project directory:

```bash
source .venv/bin/activate
python -m codex_api_service.app
```

When you see `Uvicorn running on http://127.0.0.1:1219`, the service is running.
This mode keeps the current terminal occupied; closing the terminal stops the service.

The startup output also prints copyable URLs. If `server.host` is `0.0.0.0`, it also prints LAN URLs:

```text
Codex API Service starting
  Server:      http://127.0.0.1:1219
  Local API:   http://127.0.0.1:1219/v1
  Local UI:    http://127.0.0.1:1219/ui
  Health:      http://127.0.0.1:1219/health
```

Check the service from another terminal:

```bash
curl http://127.0.0.1:1219/health
curl http://127.0.0.1:1219/v1/models
curl http://127.0.0.1:1219/admin/models
```

Print the effective configuration without secrets:

```bash
python -m codex_api_service.app --print-config
```

## Run as a Background Service

Background service scripts prefer the project `.venv`. If the virtual environment is missing, they print a `WARNING` and fall back to global Python.

### macOS

macOS uses a launchd user service. Install and start it with:

```bash
bash scripts/install_launchd_service.sh
```

The service label is:

```text
com.codex-api-service.local
```

Common management commands:

```bash
# Check whether the service is loaded.
launchctl print gui/$(id -u)/com.codex-api-service.local

# Restart the service.
launchctl kickstart -k gui/$(id -u)/com.codex-api-service.local

# Stop and uninstall the service.
bash scripts/uninstall_launchd_service.sh
```

Log files:

```text
./logs/launchd.out.log
./logs/launchd.err.log
```

### Ubuntu

Ubuntu uses a systemd user service and does not require sudo. Install and start it with:

```bash
bash scripts/install_systemd_user_service.sh
```

The service name is:

```text
codex-api-service.service
```

Common management commands:

```bash
# Check status.
systemctl --user status codex-api-service.service

# Restart the service.
systemctl --user restart codex-api-service.service

# Follow live logs.
journalctl --user -u codex-api-service -f

# Stop and uninstall the service.
bash scripts/uninstall_systemd_user_service.sh
```

### Windows

Windows uses a per-user Task Scheduler task. Run this from PowerShell in the project directory:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1
```

The task name is:

```text
CodexApiService
```

Common management commands:

```powershell
# Inspect the task.
Get-ScheduledTask -TaskName CodexApiService

# Start the task manually.
Start-ScheduledTask -TaskName CodexApiService

# Stop the task.
Stop-ScheduledTask -TaskName CodexApiService

# Stop and uninstall the task.
powershell -ExecutionPolicy Bypass -File scripts\uninstall_windows_task.ps1
```

Log files:

```text
.\logs\windows.out.log
.\logs\windows.err.log
```

## Local Console

After the service starts, open:

```text
http://127.0.0.1:1219/ui
```

The console includes three pages:

- Chat: stream chat directly through `/v1/chat/completions`.
- Request logs: inspect recent endpoints, models, status codes, latency, and token usage, with filtering and expandable details.
- Configuration: edit the default model, usage logging, authentication paths, and API key.
- Model configuration: set the default reasoning effort and fast mode for every real model in the dynamic Codex CLI catalog.
- Status strip: show OAuth, Fast, usage logging, and Codex CLI version status.

The chat page supports Markdown fenced code blocks and per-block copy actions.

The public repository does not commit `codex_api_service/static/ui/` build output.
After the first clone, or after editing `frontend/`, rebuild the console:

```bash
npm --prefix frontend install
npm --prefix frontend run build
```

## OpenAI SDK Example

When callers omit explicit request options, the service uses the real model's entry in `codex.model_request_defaults`.
Unconfigured models use `reasoning_effort="medium"` with fast mode disabled. Requests can override model defaults with
`reasoning_effort`, `reasoning.effort`, `fast_mode`, or `service_tier`.
The service maps fast mode to the Codex OAuth backend value `service_tier="priority"`.

The compatibility layer accepts common OpenAI SDK parameters, including `temperature`, `top_p`, `max_tokens`, `max_output_tokens`, `response_format`, `tools`, `tool_choice`, `stop`, `seed`, and penalty parameters.
The current Codex OAuth backend does not support these sampling, tool, or format options, so the service ignores them locally to avoid upstream `Unsupported parameter` errors.

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:1219/v1",
    api_key="local-secret",  # Any string works when local_api_key is not configured.
)

response = client.chat.completions.create(
    model="gpt-5.5",
    messages=[{"role": "user", "content": "Hello, introduce yourself"}],
    extra_body={"fast_mode": False},  # Explicit values win; omit this to use the model default.
)

print(response.choices[0].message.content)
```

Streaming:

```python
stream = client.chat.completions.create(
    model="gpt-5.5",
    messages=[{"role": "user", "content": "Write a Python quicksort"}],
    stream=True,
    stream_options={"include_usage": True},
)

for chunk in stream:
    if chunk.choices:
        print(chunk.choices[0].delta.content or "", end="")
```

Responses API:

```python
response = client.responses.create(
    model="gpt-5.5",
    input="Explain this local Codex OAuth proxy service in three sentences",
)

print(response.output_text)
```

Anthropic Messages API:

```python
from anthropic import Anthropic

client = Anthropic(
    base_url="http://127.0.0.1:1219/anthropic",
    api_key="local-secret",  # Matches config.yaml local_api_key; any string works if unset.
)

message = client.messages.create(
    model="gpt-5.5",
    max_tokens=1024,
    system="Keep it concise",
    messages=[{"role": "user", "content": "Hello, introduce yourself"}],
)

print(message.content[0].text)
```

Anthropic streaming:

```python
with client.messages.stream(
    model="gpt-5.5",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Write a Python quicksort"}],
) as stream:
    for text in stream.text_stream:
        print(text, end="")
```

`/anthropic/messages` and `/anthropic/v1/messages` map Anthropic `tools` to
Responses function tools and map `tool_choice` to Responses tool choice. When
the model returns a function call, non-streaming responses include an Anthropic
`tool_use` content block; streaming responses emit `content_block_start`,
`input_json_delta`, and `content_block_stop`.

Some gateways append `/v1/models` to the provider base URL during connection
tests. Use `http://127.0.0.1:1219/anthropic` as the base URL so discovery reaches
`/anthropic/v1/models`; do not set the base URL to `/anthropic/messages`. This
endpoint returns the Anthropic-native model list shape. To pass Claude-3p's
Anthropic-family model-name filter, model IDs and `display_name` use a
`claude-sonnet-...` discovery alias that does not contain `gpt`, `codex`, or
`openai`, such as `claude-sonnet-5-5`, and include
`anthropic_family_tier="sonnet"`. Requests to `/anthropic/messages` map that
discovery alias, old `claude-gpt-...` aliases, and the bare `sonnet` tier back
to the real Codex model name automatically.
Claude-3p gateway inference appends `/v1/messages` to the base URL, so
`/anthropic/v1/messages` uses the same mapping.

Image input supports Anthropic `image` content blocks:

- `source.type = "base64"` becomes a `data:<media_type>;base64,...` `input_image`
- `source.type = "url"` becomes a URL `input_image`

## Usage Logs

When the Codex backend returns real usage data, the service appends it to:

```text
.codex-usage/usage.jsonl
```

The log only stores token statistics and runtime metadata.
It does not record prompts, completions, OAuth tokens, API keys, Authorization headers, or full request and response bodies.

Console request logs are persisted to:

```text
./logs/requests.jsonl
```

This log also stores request metadata only, without prompts or response bodies.

## Tests

```bash
python -m pytest -v
```
