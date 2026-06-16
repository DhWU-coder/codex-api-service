"""FastAPI 应用入口和 OpenAI-compatible 路由。"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .admin import patch_config_file, safe_config_snapshot
from .auth import CodexAuth
from .codex_client import CodexClient, CodexHTTPStatusError
from .config import AppConfig, load_config
from .openai_compat import (
    build_chat_completion,
    build_chat_delta_sse,
    build_chat_usage_sse,
    build_response_object,
    chat_messages_to_codex_input,
    codex_event_completed_response,
    codex_event_text_delta,
    encode_sse,
    normalize_responses_input,
    response_stream_event,
)
from .request_log import RequestLogStore
from .usage_log import UsageLogger


# Codex OAuth backend 当前用 priority 表示快速服务层；本地 API 仍接受 fast 作为用户语义。
CODEX_FAST_SERVICE_TIER = "priority"


def create_app(*, config: AppConfig | None = None, codex_client: Any | None = None) -> FastAPI:
    """创建 FastAPI 应用，测试可注入 fake Codex client。"""
    # 未显式传入配置时，从当前项目根目录加载 config.yaml。
    app_config = config or load_config()
    auth = CodexAuth(
        auth_path=app_config.auth.auth_path,
        import_auth_path=app_config.auth.import_auth_path,
    )
    client = codex_client or CodexClient(auth=auth, config=app_config.codex)
    usage_logger = UsageLogger(project_root=app_config.project_root, usage_config=app_config.usage)
    request_log = RequestLogStore()
    app = FastAPI(title="Codex OpenAI API Service", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        """返回简单健康检查结果。"""
        return {"status": "ok"}

    @app.get("/v1/models")
    async def list_models(request: Request) -> dict[str, Any]:
        """返回 OpenAI-compatible 模型列表。"""
        _require_local_auth(request, app_config)
        started = time.perf_counter()
        created = int(time.time())
        response_body = {
            "object": "list",
            "data": [
                {"id": model, "object": "model", "created": created, "owned_by": "codex-oauth"}
                for model in app_config.codex.available_models
            ],
        }
        request_log.record(
            method="GET",
            path="/v1/models",
            model=None,
            status_code=200,
            duration_ms=_duration_ms(started),
        )
        return response_body

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Any:
        """处理 OpenAI Chat Completions 请求。"""
        _require_local_auth(request, app_config)
        started = time.perf_counter()
        body = await request.json()
        model = str(body.get("model") or app_config.codex.default_model)
        payload = _chat_body_to_codex_payload(body, app_config, model)
        if bool(body.get("stream")):
            return StreamingResponse(
                _stream_chat_completion(client, payload, model, usage_logger, request_log, started),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        try:
            codex_response = await client.create_response(payload)
        except Exception as error:
            _raise_non_stream_error(
                error=error,
                method="POST",
                path="/v1/chat/completions",
                model=model,
                request_log=request_log,
                started=started,
            )
        usage_logger.log(model=model, usage=codex_response.get("usage"), request_id=_request_id(codex_response))
        request_log.record(
            method="POST",
            path="/v1/chat/completions",
            model=model,
            status_code=200,
            duration_ms=_duration_ms(started),
            usage=codex_response.get("usage"),
            request_id=_request_id(codex_response),
        )
        return JSONResponse(build_chat_completion(codex_response, model=model))

    @app.post("/v1/responses")
    async def responses(request: Request) -> Any:
        """处理 OpenAI Responses API 请求。"""
        _require_local_auth(request, app_config)
        started = time.perf_counter()
        body = await request.json()
        model = str(body.get("model") or app_config.codex.default_model)
        payload = _responses_body_to_codex_payload(body, app_config, model)
        if bool(body.get("stream")):
            return StreamingResponse(
                _stream_response(client, payload, model, usage_logger, request_log, started),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        try:
            codex_response = await client.create_response(payload)
        except Exception as error:
            _raise_non_stream_error(
                error=error,
                method="POST",
                path="/v1/responses",
                model=model,
                request_log=request_log,
                started=started,
            )
        usage_logger.log(model=model, usage=codex_response.get("usage"), request_id=_request_id(codex_response))
        request_log.record(
            method="POST",
            path="/v1/responses",
            model=model,
            status_code=200,
            duration_ms=_duration_ms(started),
            usage=codex_response.get("usage"),
            request_id=_request_id(codex_response),
        )
        return JSONResponse(build_response_object(codex_response, model=model))

    @app.get("/admin/config")
    async def admin_config(request: Request) -> dict[str, Any]:
        """返回控制台安全配置快照。"""
        _require_local_auth(request, app_config)
        return safe_config_snapshot(app_config)

    @app.patch("/admin/config")
    async def admin_config_patch(request: Request) -> dict[str, Any]:
        """写入控制台支持的配置字段。"""
        _require_local_auth(request, app_config)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="config patch must be an object")
        return patch_config_file(project_root=app_config.project_root, patch=body)

    @app.get("/admin/requests")
    async def admin_requests(request: Request, limit: int = 100) -> dict[str, Any]:
        """返回最近 API 请求元数据。"""
        _require_local_auth(request, app_config)
        return {"items": request_log.list_recent(limit=limit)}

    @app.get("/ui", response_class=HTMLResponse)
    async def ui_index() -> Any:
        """返回 React 控制台入口或构建缺失提示页。"""
        index_path = _ui_static_root() / "index.html"
        if index_path.exists():
            return FileResponse(index_path)
        return HTMLResponse(_fallback_ui_html())

    # 如果前端已经构建，挂载静态资源目录供 /ui 的 index.html 引用。
    static_root = _ui_static_root()
    assets_root = static_root / "assets"
    if assets_root.exists():
        app.mount("/assets", StaticFiles(directory=assets_root), name="ui-assets")

    # 保存配置和 client，便于调试或外部测试读取应用状态。
    app.state.config = app_config
    app.state.codex_client = client
    app.state.usage_logger = usage_logger
    app.state.request_log = request_log
    return app


def _require_local_auth(request: Request, config: AppConfig) -> None:
    """在配置 local_api_key 时校验本地 Bearer token。"""
    api_key = config.api.local_api_key
    if not api_key:
        return
    expected = f"Bearer {api_key}"
    if request.headers.get("authorization") != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _chat_body_to_codex_payload(body: dict[str, Any], config: AppConfig, model: str) -> dict[str, Any]:
    """把 Chat Completions 请求体转换为 Codex payload。"""
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="messages must be a list")
    payload = _base_codex_payload(config, model)
    payload["input"] = chat_messages_to_codex_input(messages)
    _copy_optional_generation_fields(body, payload)
    return payload


def _responses_body_to_codex_payload(body: dict[str, Any], config: AppConfig, model: str) -> dict[str, Any]:
    """把 Responses API 请求体转换为 Codex payload。"""
    if "input" not in body:
        raise HTTPException(status_code=400, detail="input is required")
    payload = _base_codex_payload(config, model)
    payload["input"] = normalize_responses_input(body["input"])
    if isinstance(body.get("instructions"), str):
        payload["instructions"] = body["instructions"]
    _copy_optional_generation_fields(body, payload)
    return payload


def _base_codex_payload(config: AppConfig, model: str) -> dict[str, Any]:
    """生成 Codex backend 的基础 payload。"""
    payload: dict[str, Any] = {
        "model": model,
        "instructions": config.codex.instructions,
        "stream": True,
        "store": False,
        "reasoning": {"effort": config.codex.reasoning_effort, "summary": "auto"},
    }
    if config.codex.include_reasoning:
        payload["include"] = ["reasoning.encrypted_content"]
    if config.codex.fast_mode:
        # 快速模式在本地叫 fast，但 OAuth backend 实测接受 priority 作为 service_tier。
        payload["service_tier"] = CODEX_FAST_SERVICE_TIER
    return payload


def _copy_optional_generation_fields(source: dict[str, Any], target: dict[str, Any]) -> None:
    """复制常见生成参数，兼容 OpenAI SDK 客户端传参。"""
    # OpenAI SDK 常见采样、工具和格式参数由本地服务兼容接收，但 Codex OAuth backend 不支持，不能透传。
    if isinstance(source.get("reasoning"), dict):
        target["reasoning"] = source["reasoning"]
    elif isinstance(source.get("reasoning_effort"), str):
        target["reasoning"] = {"effort": source["reasoning_effort"], "summary": "auto"}
    _apply_service_tier_override(source, target)


def _apply_service_tier_override(source: dict[str, Any], target: dict[str, Any]) -> None:
    """根据请求体覆盖 Codex fast service tier。"""
    # fast_mode 是本服务提供的布尔开关，优先级高于 service_tier 字符串。
    if "fast_mode" in source:
        if _request_bool(source["fast_mode"]):
            target["service_tier"] = CODEX_FAST_SERVICE_TIER
        else:
            target.pop("service_tier", None)
        return

    # service_tier 贴近 Codex CLI/config.toml 语义，方便外部客户端直接传参。
    if "service_tier" in source:
        tier = str(source["service_tier"]).strip().lower()
        if tier in {"fast", CODEX_FAST_SERVICE_TIER}:
            target["service_tier"] = CODEX_FAST_SERVICE_TIER
        elif tier in {"", "auto", "default", "standard"}:
            target.pop("service_tier", None)
        else:
            target["service_tier"] = tier


def _request_bool(value: Any) -> bool:
    """把请求体里的布尔开关规范化，兼容少量字符串写法。"""
    # JSON 客户端通常会传 bool；字符串兼容命令行 curl 手写场景。
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "fast"}
    return bool(value)


async def _stream_chat_completion(
    client: Any,
    payload: dict[str, Any],
    model: str,
    usage_logger: UsageLogger,
    request_log: RequestLogStore,
    started: float,
) -> AsyncIterator[str]:
    """把 Codex SSE 转成 Chat Completions SSE。"""
    response_id = "resp_stream"
    created = int(time.time())
    completed_response: dict[str, Any] | None = None
    try:
        async for event in client.stream_response(payload):
            completed = codex_event_completed_response(event)
            if completed is not None:
                completed_response = completed
                response_id = str(completed.get("id") or response_id)
                usage = completed.get("usage")
                if isinstance(usage, dict):
                    yield build_chat_usage_sse(response_id=response_id, model=model, usage=usage, created=created)
                continue
            delta = codex_event_text_delta(event)
            if delta is not None:
                yield build_chat_delta_sse(response_id=response_id, model=model, delta=delta, created=created)
    except Exception as error:
        # 流式响应已经开始后不能再改 HTTP 状态码，只能用 SSE error 事件优雅结束。
        message = _friendly_error_message(error)
        status_code = _error_status_code(error)
        request_log.record(
            method="POST",
            path="/v1/chat/completions",
            model=model,
            status_code=status_code,
            duration_ms=_duration_ms(started),
            error=message,
        )
        yield _stream_error_sse(message=message, status_code=status_code)
        yield "data: [DONE]\n\n"
        return
    if completed_response is not None:
        usage_logger.log(
            model=model,
            usage=completed_response.get("usage"),
            request_id=_request_id(completed_response),
        )
        request_log.record(
            method="POST",
            path="/v1/chat/completions",
            model=model,
            status_code=200,
            duration_ms=_duration_ms(started),
            usage=completed_response.get("usage"),
            request_id=_request_id(completed_response),
        )
    yield "data: [DONE]\n\n"


async def _stream_response(
    client: Any,
    payload: dict[str, Any],
    model: str,
    usage_logger: UsageLogger,
    request_log: RequestLogStore,
    started: float,
) -> AsyncIterator[str]:
    """把 Codex SSE 转成 Responses API SSE。"""
    completed_response: dict[str, Any] | None = None
    try:
        async for event in client.stream_response(payload):
            completed = codex_event_completed_response(event)
            if completed is not None:
                completed_response = completed
            yield response_stream_event(event)
    except Exception as error:
        # Responses API 流也保持 SSE 完整结束，避免客户端看到网络层断流。
        message = _friendly_error_message(error)
        status_code = _error_status_code(error)
        request_log.record(
            method="POST",
            path="/v1/responses",
            model=model,
            status_code=status_code,
            duration_ms=_duration_ms(started),
            error=message,
        )
        yield _stream_error_sse(message=message, status_code=status_code)
        yield "data: [DONE]\n\n"
        return
    if completed_response is not None:
        usage_logger.log(
            model=model,
            usage=completed_response.get("usage"),
            request_id=_request_id(completed_response),
        )
        request_log.record(
            method="POST",
            path="/v1/responses",
            model=model,
            status_code=200,
            duration_ms=_duration_ms(started),
            usage=completed_response.get("usage"),
            request_id=_request_id(completed_response),
        )
    yield "data: [DONE]\n\n"


def _request_id(response: dict[str, Any]) -> str | None:
    """从响应中读取 request/response id。"""
    value = response.get("id") or response.get("request_id")
    return value if isinstance(value, str) else None


def _raise_non_stream_error(
    *,
    error: Exception,
    method: str,
    path: str,
    model: str,
    request_log: RequestLogStore,
    started: float,
) -> None:
    """记录非流式请求错误，并转换成 OpenAI 客户端可读的 JSON HTTP 错误。"""
    # 非流式响应尚未发送，可以直接使用正确 HTTP 状态码和 JSON detail。
    message = _friendly_error_message(error)
    status_code = _error_status_code(error)
    request_log.record(
        method=method,
        path=path,
        model=model,
        status_code=status_code,
        duration_ms=_duration_ms(started),
        error=message,
    )
    raise HTTPException(status_code=status_code, detail=message)


def _friendly_error_message(error: Exception) -> str:
    """把上游异常转换成前端可展示的短错误。"""
    if isinstance(error, CodexHTTPStatusError):
        try:
            body = json.loads(error.body)
        except json.JSONDecodeError:
            return str(error)
        detail = body.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
    return str(error)


def _error_status_code(error: Exception) -> int:
    """从异常中提取适合请求日志展示的状态码。"""
    # CodexHTTPStatusError 和 CodexUnexpectedResponseError 都带 status_code 字段。
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    return 500


def _stream_error_sse(*, message: str, status_code: int) -> str:
    """构造流式错误 SSE 事件。"""
    return encode_sse({"error": {"message": message, "status_code": status_code}})


def _duration_ms(started: float) -> int:
    """计算从 started 到当前的毫秒耗时。"""
    return int((time.perf_counter() - started) * 1000)


def _ui_static_root() -> Path:
    """返回 React 控制台构建产物目录。"""
    return Path(__file__).resolve().parent / "static" / "ui"


def _fallback_ui_html() -> str:
    """生成前端未构建时的轻量提示页面。"""
    return """<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Codex API Console</title>
  </head>
  <body>
    <main style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; padding: 32px;">
      <h1>Codex API Console</h1>
      <p>前端还没有构建，请运行 <code>npm --prefix frontend run build</code>。</p>
    </main>
  </body>
</html>"""


def _print_config(config: AppConfig) -> None:
    """打印不含密钥的有效配置，供启动前检查。"""
    safe_config = {
        "server": {"host": config.server.host, "port": config.server.port},
        "api": {"local_api_key_configured": bool(config.api.local_api_key)},
        "codex": {
            "default_model": config.codex.default_model,
            "available_models": list(config.codex.available_models),
            "responses_url": config.codex.responses_url,
            "timeout_seconds": config.codex.timeout_seconds,
            "reasoning_effort": config.codex.reasoning_effort,
        },
        "usage": {"enabled": config.usage.enabled, "path": str(config.usage.path)},
    }
    print(json.dumps(safe_config, ensure_ascii=False, indent=2))


def _startup_urls(config: AppConfig) -> dict[str, str]:
    """生成启动时展示给用户的本地访问地址。"""
    # 0.0.0.0 是监听地址，不适合用户直接复制访问；展示时转换成本机地址。
    display_host = "127.0.0.1" if config.server.host == "0.0.0.0" else config.server.host
    server_url = f"http://{config.server.host}:{config.server.port}"
    display_base = f"http://{display_host}:{config.server.port}"
    return {
        "server": server_url,
        "api": f"{display_base}/v1",
        "console": f"{display_base}/ui",
        "health": f"{display_base}/health",
    }


def _print_startup_banner(config: AppConfig) -> None:
    """打印启动入口，帮助用户直接找到控制台地址。"""
    urls = _startup_urls(config)
    # flush=True 保证 launchd 日志和前台终端都能尽快看到入口地址。
    print("", flush=True)
    print("Codex API Service starting", flush=True)
    print(f"  Server:  {urls['server']}", flush=True)
    print(f"  API:     {urls['api']}", flush=True)
    print(f"  Console: {urls['console']}", flush=True)
    print(f"  Health:  {urls['health']}", flush=True)
    print("", flush=True)


def main() -> None:
    """命令行入口，支持打印配置或启动服务。"""
    parser = argparse.ArgumentParser(description="Run local Codex OpenAI-compatible API service.")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml.")
    parser.add_argument("--print-config", action="store_true", help="Print effective config without secrets.")
    args = parser.parse_args()
    config = load_config(project_root=Path.cwd(), config_path=Path(args.config))
    if args.print_config:
        _print_config(config)
        return
    _print_startup_banner(config)
    uvicorn.run(create_app(config=config), host=config.server.host, port=config.server.port)


if __name__ == "__main__":
    main()
