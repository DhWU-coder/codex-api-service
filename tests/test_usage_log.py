import json
from pathlib import Path

from codex_api_service.config import UsageConfig
from codex_api_service.usage_log import UsageLogger, extract_usage


def test_extract_usage_maps_openai_and_responses_usage_fields() -> None:
    """验证 usage 字段能从 OpenAI/Responses 风格字段中提取真实 token。"""
    # 组合常见 OpenAI usage 字段，包含缓存和 reasoning 明细。
    usage = extract_usage(
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_tokens_details": {"cached_tokens": 3},
            "output_tokens_details": {"reasoning_tokens": 2},
        }
    )

    # 日志规范要求统一为 total/input/output/cached/reasoning。
    assert usage == {
        "total": 15,
        "input": 10,
        "cached": 3,
        "output": 5,
        "reasoning": 2,
    }


def test_extract_usage_returns_none_when_real_counts_are_missing() -> None:
    """验证缺少真实 token 数时不会估算 usage。"""
    # 只有文本或无关字段时，不能反推 token 用量。
    usage = extract_usage({"output_text": "hello"})

    # 返回 None 表示调用方应该跳过日志写入。
    assert usage is None


def test_usage_logger_appends_jsonl_without_prompt_or_secret(tmp_path: Path) -> None:
    """验证日志追加 JSONL 且不会记录 prompt、响应正文或密钥。"""
    # 构造临时日志配置，避免污染真实项目目录。
    log_path = tmp_path / ".codex-usage" / "usage.jsonl"
    logger = UsageLogger(
        project_root=tmp_path,
        usage_config=UsageConfig(path=log_path),
        session_id="test-session",
    )

    # 写入一条带真实 token 数的 usage 事件。
    wrote = logger.log(
        model="gpt-5.5",
        usage={
            "prompt_tokens": 7,
            "completion_tokens": 4,
            "total_tokens": 11,
            "prompt_tokens_details": {"cached_tokens": 1},
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
        request_id="resp_123",
        cwd=tmp_path,
    )

    # 成功写入后应生成单行 JSONL。
    assert wrote is True
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1

    # JSONL 中只保留统计字段和运行元数据。
    event = json.loads(lines[0])
    assert event["schema_version"] == "codex-usage.project-log.v1"
    assert event["source"] == "codex-api-service"
    assert event["channel"] == "Codex API Service"
    assert event["provider"] == "openai-codex"
    assert event["auth"] == "codex-oauth"
    assert event["api_surface"] == "chatgpt-codex-responses"
    assert event["project_root"] == str(tmp_path)
    assert event["cwd"] == str(tmp_path)
    assert event["session_id"] == "test-session"
    assert event["request_id"] == "resp_123"
    assert event["model"] == "gpt-5.5"
    assert event["usage"] == {
        "total": 11,
        "input": 7,
        "cached": 1,
        "output": 4,
        "reasoning": 2,
    }

    # 原始 prompt、响应文本、token、Authorization 等敏感内容不应出现。
    serialized = lines[0]
    assert "prompt" not in serialized.lower()
    assert "authorization" not in serialized.lower()
    assert "access_token" not in serialized.lower()


def test_usage_logger_migrates_legacy_source_and_channel(tmp_path: Path) -> None:
    """验证旧 usage 日志会迁移到项目来源字段语义。"""
    # 旧版本把 OAuth 认证方式写进 source/channel，需要迁移成项目名。
    log_path = tmp_path / ".codex-usage" / "usage.jsonl"
    log_path.parent.mkdir(parents=True)
    legacy_event = {
        "schema_version": "codex-usage.project-log.v1",
        "timestamp": "2026-06-16T16:31:52.406Z",
        "source": "codex-oauth",
        "channel": "Codex OAuth",
        "project_root": str(tmp_path),
        "cwd": str(tmp_path),
        "session_id": "run-legacy",
        "model": "gpt-5.5",
        "usage": {"total": 11, "input": 7, "cached": 1, "output": 4, "reasoning": 2},
    }
    custom_event = {
        "schema_version": "codex-usage.project-log.v1",
        "timestamp": "2026-06-16T16:32:52.406Z",
        "source": "custom-agent",
        "channel": "Custom Agent",
        "project_root": str(tmp_path),
        "cwd": str(tmp_path),
        "session_id": "run-custom",
        "model": "gpt-5.5",
        "usage": {"total": 5, "input": 3, "cached": 0, "output": 2, "reasoning": 0},
    }
    log_path.write_text(
        "\n".join(
            json.dumps(event, ensure_ascii=False)
            for event in (legacy_event, custom_event)
        )
        + "\n",
        encoding="utf-8",
    )

    # 初始化日志器时会迁移已有历史，避免用户手动处理 JSONL。
    UsageLogger(project_root=tmp_path, usage_config=UsageConfig(path=log_path), session_id="test-session")

    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["source"] == "codex-api-service"
    assert lines[0]["channel"] == "Codex API Service"
    assert lines[0]["provider"] == "openai-codex"
    assert lines[0]["auth"] == "codex-oauth"
    assert lines[0]["api_surface"] == "chatgpt-codex-responses"

    # 自定义来源不能被迁移覆盖，但缺失的上游元数据仍然可以补齐。
    assert lines[1]["source"] == "custom-agent"
    assert lines[1]["channel"] == "Custom Agent"
    assert lines[1]["provider"] == "openai-codex"
    assert lines[1]["auth"] == "codex-oauth"
    assert lines[1]["api_surface"] == "chatgpt-codex-responses"
