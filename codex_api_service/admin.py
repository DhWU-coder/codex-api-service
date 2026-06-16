"""管理台后端接口的配置读写工具。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .config import AppConfig, load_config


def safe_config_snapshot(config: AppConfig) -> dict[str, Any]:
    """生成不包含密钥明文的配置快照。"""
    return {
        "server": {"host": config.server.host, "port": config.server.port},
        "api": {"local_api_key_configured": bool(config.api.local_api_key)},
        "codex": {
            "default_model": config.codex.default_model,
            "available_models": list(config.codex.available_models),
            "reasoning_effort": config.codex.reasoning_effort,
            "timeout_seconds": config.codex.timeout_seconds,
            "include_reasoning": config.codex.include_reasoning,
            "fast_mode": config.codex.fast_mode,
        },
        "usage": {"enabled": config.usage.enabled, "path": str(config.usage.path)},
        "auth": {
            "auth_path": str(config.auth.auth_path) if config.auth.auth_path else "",
            "import_auth_path": str(config.auth.import_auth_path) if config.auth.import_auth_path else "",
        },
        "config_path": str(config.project_root / "config.yaml"),
    }


def patch_config_file(*, project_root: Path, patch: dict[str, Any]) -> dict[str, Any]:
    """把 UI 提交的白名单配置写入 config.yaml。"""
    # 读取已有 YAML，保留未被 UI 管理的字段。
    config_path = project_root / "config.yaml"
    before_config = load_config(project_root=project_root, config_path=config_path)
    current = _read_yaml(config_path)

    # 只允许写入控制台明确支持的字段，避免误写复杂配置。
    if isinstance(patch.get("api"), dict):
        api_patch = patch["api"]
        if "local_api_key" in api_patch:
            _ensure_section(current, "api")["local_api_key"] = _empty_to_none(api_patch["local_api_key"])

    if isinstance(patch.get("codex"), dict):
        codex_patch = patch["codex"]
        codex_section = _ensure_section(current, "codex")
        for field_name in ("default_model", "reasoning_effort"):
            if field_name in codex_patch:
                codex_section[field_name] = str(codex_patch[field_name])
        if "fast_mode" in codex_patch:
            # fast_mode 是布尔开关，写入 YAML 时保留原生 true/false。
            codex_section["fast_mode"] = bool(codex_patch["fast_mode"])

    if isinstance(patch.get("usage"), dict):
        usage_patch = patch["usage"]
        if "enabled" in usage_patch:
            _ensure_section(current, "usage")["enabled"] = bool(usage_patch["enabled"])

    if isinstance(patch.get("auth"), dict):
        auth_patch = patch["auth"]
        auth_section = _ensure_section(current, "auth")
        for field_name in ("auth_path", "import_auth_path"):
            if field_name in auth_patch:
                auth_section[field_name] = _empty_to_none(auth_patch[field_name])

    # safe_dump 会重写 YAML 格式，但内容简单可读，适合本地配置文件。
    config_path.write_text(
        yaml.safe_dump(current, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    after_config = load_config(project_root=project_root, config_path=config_path)
    restart_required = _restart_required(before_config, after_config)
    return {
        "restart_required": restart_required,
        "applied": not restart_required,
        "config_path": str(config_path),
    }


def _read_yaml(path: Path) -> dict[str, Any]:
    """读取 YAML 配置文件，不存在时返回空 dict。"""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _ensure_section(data: dict[str, Any], section_name: str) -> dict[str, Any]:
    """确保 YAML 顶层 section 是 dict。"""
    section = data.get(section_name)
    if not isinstance(section, dict):
        section = {}
        data[section_name] = section
    return section


def _empty_to_none(value: Any) -> str | None:
    """把 UI 表单空字符串转换成 YAML null。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _restart_required(before: AppConfig, after: AppConfig) -> bool:
    """判断配置变更是否必须重启服务才能完整生效。"""
    # 监听地址和端口由 uvicorn 启动时绑定，运行中不能无损切换。
    if before.server != after.server:
        return True

    # OAuth 文件路径变化会影响 CodexAuth 实例，保持重启语义最清晰。
    if before.auth != after.auth:
        return True

    # 其余当前 UI 支持的字段都可以在运行中的 app_config/client/logger 上热更新。
    return False
