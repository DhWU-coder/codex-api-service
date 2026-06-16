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
    assert event["source"] == "codex-oauth"
    assert event["channel"] == "Codex OAuth"
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
