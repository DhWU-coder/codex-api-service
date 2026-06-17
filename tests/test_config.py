from pathlib import Path

from codex_api_service.config import load_config


def test_load_config_uses_default_values_when_yaml_is_missing(tmp_path: Path) -> None:
    """验证没有 config.yaml 时服务使用本地默认配置。"""
    # 指向一个不存在的配置文件，模拟首次启动项目的状态。
    config_path = tmp_path / "missing-config.yaml"

    # 加载配置时传入临时项目根目录，避免测试写入真实项目目录。
    config = load_config(project_root=tmp_path, config_path=config_path)

    # 默认服务监听本机 1219 端口，匹配用户指定的本地 API 端口。
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 1219

    # 默认模型沿用参考实现中的 Codex 模型名。
    assert config.codex.default_model == "gpt-5.5"
    assert config.codex.reasoning_effort == "medium"
    assert config.codex.fast_mode is True

    # 默认日志必须落到项目根目录下，便于 codex-usage 导入整个项目。
    assert config.usage.enabled is True
    assert config.usage.path == tmp_path / ".codex-usage" / "usage.jsonl"
    assert config.usage.source == "codex-api-service"
    assert config.usage.channel == "Codex API Service"
    assert config.usage.provider == "openai-codex"
    assert config.usage.auth == "codex-oauth"
    assert config.usage.api_surface == "chatgpt-codex-responses"


def test_load_config_merges_yaml_overrides(tmp_path: Path) -> None:
    """验证 config.yaml 中的配置能够覆盖默认值。"""
    # 写入一个只覆盖部分字段的配置文件，未写字段继续继承默认值。
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "server:",
                "  host: 0.0.0.0",
                "  port: 1219",
                "api:",
                "  local_api_key: local-secret",
                "codex:",
                "  default_model: gpt-5.5-mini",
                "  reasoning_effort: high",
                "  fast_mode: false",
                "usage:",
                "  path: logs/codex-usage.jsonl",
                "  source: custom-service",
                "  channel: Custom Service",
                "  provider: openai",
                "  auth: api-key",
                "  api_surface: openai-chat-completions",
            ]
        ),
        encoding="utf-8",
    )

    # 加载配置后应当得到默认值和 YAML 覆盖值合并后的结果。
    config = load_config(project_root=tmp_path, config_path=config_path)

    # server/api/codex 字段来自 YAML 覆盖。
    assert config.server.host == "0.0.0.0"
    assert config.server.port == 1219
    assert config.api.local_api_key == "local-secret"
    assert config.codex.default_model == "gpt-5.5-mini"
    assert config.codex.reasoning_effort == "high"
    assert config.codex.fast_mode is False

    # 相对日志路径应解析到项目根目录下。
    assert config.usage.path == tmp_path / "logs" / "codex-usage.jsonl"
    assert config.usage.source == "custom-service"
    assert config.usage.channel == "Custom Service"
    assert config.usage.provider == "openai"
    assert config.usage.auth == "api-key"
    assert config.usage.api_surface == "openai-chat-completions"


def test_load_config_supports_environment_api_key_override(tmp_path: Path, monkeypatch) -> None:
    """验证环境变量可以覆盖本地 API key，便于临时启动服务。"""
    # 设置环境变量模拟命令行临时注入本地访问密钥。
    monkeypatch.setenv("CODEX_API_SERVICE_KEY", "env-secret")

    # 未提供 YAML 时，local_api_key 应直接来自环境变量。
    config = load_config(project_root=tmp_path, config_path=tmp_path / "none.yaml")

    # 环境变量优先级高于默认空值。
    assert config.api.local_api_key == "env-secret"


def test_load_config_uses_import_auth_path_name(tmp_path: Path) -> None:
    """验证新配置名 import_auth_path 能指定已有 OAuth 文件导入位置。"""
    # 新命名强调“导入已有 Codex OAuth 文件”，避免误解成运行时调用 CLI。
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "auth:",
                "  auth_path: local/auth.json",
                "  import_auth_path: shared/auth.json",
            ]
        ),
        encoding="utf-8",
    )

    # 加载配置后，新字段应被解析成项目根目录下的绝对路径。
    config = load_config(project_root=tmp_path, config_path=config_path)

    assert config.auth.auth_path == tmp_path / "local" / "auth.json"
    assert config.auth.import_auth_path == tmp_path / "shared" / "auth.json"


def test_load_config_keeps_legacy_codex_cli_auth_path_alias(tmp_path: Path) -> None:
    """验证旧配置名 codex_cli_auth_path 仍可兼容读取。"""
    # 老配置不应该因为字段改名而立即失效。
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "auth:",
                "  codex_cli_auth_path: legacy/auth.json",
            ]
        ),
        encoding="utf-8",
    )

    # 旧字段会映射到新的 import_auth_path 语义。
    config = load_config(project_root=tmp_path, config_path=config_path)

    assert config.auth.import_auth_path == tmp_path / "legacy" / "auth.json"
