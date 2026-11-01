"""集中主从智能体的运行上限，调用额度不表示费用或 token 配额。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentLimits:
    """统一回答、账号与应用配额；在线排队计入期限，应用离线时冻结剩余期限。"""
    workers: int = 6
    per_account: int = 3
    per_answer: int = 3
    queue_size: int = 64
    max_agents: int = 6
    max_runs: int = 3
    main_rounds: int = 24
    child_rounds: int = 12
    tool_retries: int = 5
    document_calls: int = 5
    skill_rounds: int = 128
    skill_model_calls: int = 384
    skill_seconds: int = 3600
    skill_command_concurrency: int = 3
    skill_command_seconds: int = 900
    skill_command_default_seconds: int = 300
    skill_poll_seconds: int = 20
    context_protocol_reserve_tokens: int = 512
    context_tool_reserve_tokens: int = 256
    skill_instruction_reserve_tokens: int = 2048
    compaction_target_margin_tokens: int = 2048
    summary_tokens_min: int = 256
    summary_tokens_max: int = 4096
    summary_request_reserve_tokens: int = 1024
    model_calls: int = 96
    stream_attempts: int = 3
    search_calls: int = 5
    fetch_calls: int = 10
    answer_seconds: int = 600
    run_seconds: int = 180


LIMITS = AgentLimits()
