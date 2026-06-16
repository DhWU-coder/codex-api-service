"""加载本地 API 服务配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Codex backend 的默认 Responses 地址，沿用参考实现中的 endpoint。
DEFAULT_CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"

# 默认系统指令保持和参考 provider 一致，避免服务行为突然漂移。
DEFAULT_INSTRUCTIONS = "You are Codex, a coding agent based on GPT-5."


@dataclass(frozen=True)
class ServerConfig:
    """描述本地 FastAPI 服务监听配置。"""

    host: str = "127.0.0.1"
    port: int = 1219


@dataclass(frozen=True)
class ApiConfig:
    """描述本地 OpenAI-compatible API 访问控制配置。"""

    local_api_key: str | None = None


@dataclass(frozen=True)
class CodexConfig:
    """描述访问 Codex backend 时需要的模型和请求配置。"""

    default_model: str = "gpt-5.5"
    available_models: tuple[str, ...] = ("gpt-5.5",)
    responses_url: str = DEFAULT_CODEX_RESPONSES_URL
    timeout_seconds: int = 120
    reasoning_effort: str = "medium"
    instructions: str = DEFAULT_INSTRUCTIONS
    include_reasoning: bool = True
    fast_mode: bool = True


@dataclass(frozen=True)
class UsageConfig:
    """描述 codex-usage JSONL 日志写入配置。"""

    path: Path
    enabled: bool = True
    source: str = "codex-oauth"
    channel: str = "Codex OAuth"


@dataclass(frozen=True)
class AuthConfig:
    """描述 Codex OAuth 凭据文件位置。"""

    auth_path: Path | None = None
    import_auth_path: Path | None = None


@dataclass(frozen=True)
class AppConfig:
    """聚合服务运行所需的全部配置。"""

    project_root: Path
    server: ServerConfig = field(default_factory=ServerConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    usage: UsageConfig | None = None
    auth: AuthConfig = field(default_factory=AuthConfig)

    def __post_init__(self) -> None:
        """补齐依赖 project_root 的 usage 默认值。"""
        # dataclass frozen 后需要使用 object.__setattr__ 写入派生默认值。
        if self.usage is None:
            object.__setattr__(
                self,
                "usage",
                UsageConfig(path=self.project_root / ".codex-usage" / "usage.jsonl"),
            )


def load_config(
    *,
    project_root: Path | str | None = None,
    config_path: Path | str | None = None,
) -> AppConfig:
    """读取 config.yaml 并与默认配置合并。"""
    # 项目根目录默认取当前工作目录，保证从项目根启动时行为直观。
    root = Path(project_root or Path.cwd()).expanduser().resolve()

    # 配置文件默认位于项目根目录下；不存在时使用纯默认配置。
    path = Path(config_path) if config_path is not None else root / "config.yaml"
    raw_config = _read_yaml(path)

    # 按模块分别构建配置，避免把未知字段泄漏到 dataclass 构造器。
    server = ServerConfig(
        host=str(_nested_get(raw_config, ["server", "host"], "127.0.0.1")),
        port=int(_nested_get(raw_config, ["server", "port"], 1219)),
    )
    api = ApiConfig(
        local_api_key=_optional_str(
            os.environ.get("CODEX_API_SERVICE_KEY")
            or os.environ.get("LOCAL_API_KEY")
            or _nested_get(raw_config, ["api", "local_api_key"], None)
        )
    )

    # available_models 默认至少包含 default_model，保证 /v1/models 有可用模型。
    default_model = str(_nested_get(raw_config, ["codex", "default_model"], "gpt-5.5"))
    configured_models = _nested_get(raw_config, ["codex", "available_models"], None)
    available_models = _normalize_models(configured_models, default_model)
    codex = CodexConfig(
        default_model=default_model,
        available_models=available_models,
        responses_url=str(_nested_get(raw_config, ["codex", "responses_url"], DEFAULT_CODEX_RESPONSES_URL)),
        timeout_seconds=int(_nested_get(raw_config, ["codex", "timeout_seconds"], 120)),
        reasoning_effort=str(_nested_get(raw_config, ["codex", "reasoning_effort"], "medium")),
        instructions=str(_nested_get(raw_config, ["codex", "instructions"], DEFAULT_INSTRUCTIONS)),
        include_reasoning=bool(_nested_get(raw_config, ["codex", "include_reasoning"], True)),
        fast_mode=_bool_value(_nested_get(raw_config, ["codex", "fast_mode"], True)),
    )

    # 日志路径支持相对路径；相对路径统一解析到项目根目录下。
    usage_path = _resolve_path(root, _nested_get(raw_config, ["usage", "path"], ".codex-usage/usage.jsonl"))
    usage = UsageConfig(
        path=usage_path,
        enabled=bool(_nested_get(raw_config, ["usage", "enabled"], True)),
        source=str(_nested_get(raw_config, ["usage", "source"], "codex-oauth")),
        channel=str(_nested_get(raw_config, ["usage", "channel"], "Codex OAuth")),
    )

    # 认证路径可选；未配置时由 auth 模块按 Codex 默认规则解析。
    import_auth_value = _nested_get(raw_config, ["auth", "import_auth_path"], None)
    if import_auth_value is None:
        # 兼容早期配置名；语义仍然是“导入已有 Codex OAuth 文件”。
        import_auth_value = _nested_get(raw_config, ["auth", "codex_cli_auth_path"], None)
    auth = AuthConfig(
        auth_path=_optional_path(root, _nested_get(raw_config, ["auth", "auth_path"], None)),
        import_auth_path=_optional_path(root, import_auth_value),
    )

    return AppConfig(project_root=root, server=server, api=api, codex=codex, usage=usage, auth=auth)


def _read_yaml(path: Path) -> dict[str, Any]:
    """读取 YAML 文件；文件缺失时返回空配置。"""
    # config.yaml 是可选文件，不存在时不能阻止服务启动。
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"config file must contain a mapping: {path}")
    return data


def _nested_get(data: dict[str, Any], keys: list[str], default: Any) -> Any:
    """从嵌套 dict 读取字段，缺失时返回默认值。"""
    current: Any = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _optional_str(value: Any) -> str | None:
    """把空字符串规范化为 None。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resolve_path(root: Path, value: Any) -> Path:
    """把配置里的路径解析成绝对 Path。"""
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _optional_path(root: Path, value: Any) -> Path | None:
    """解析可选路径字段。"""
    if value is None or str(value).strip() == "":
        return None
    return _resolve_path(root, value)


def _bool_value(value: Any) -> bool:
    """把 YAML 或环境来源的布尔值规范化为 bool。"""
    # YAML 原生 true/false 会直接得到 bool；字符串常见于手写或环境变量拼接。
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _normalize_models(value: Any, default_model: str) -> tuple[str, ...]:
    """规范化可用模型列表，并确保包含默认模型。"""
    # YAML 里没写列表时，只暴露默认模型。
    if value is None:
        return (default_model,)
    if not isinstance(value, list):
        raise ValueError("codex.available_models must be a list when configured")
    models = [str(item).strip() for item in value if str(item).strip()]
    if default_model not in models:
        models.insert(0, default_model)
    return tuple(models)
