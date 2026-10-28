"""集中主从智能体的运行上限，调用额度不表示费用或 token 配额。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentLimits:
    """统一回答、账号与应用配额，排队也计入执行截止时间。"""
    workers: int = 6
    per_account: int = 3
    per_answer: int = 3
    queue_size: int = 64
    max_agents: int = 6
    max_runs: int = 3
    main_rounds: int = 24
    child_rounds: int = 12
    tool_retries: int = 5
    model_calls: int = 96
    stream_attempts: int = 3
    web_calls: int = 6
    answer_seconds: int = 600
    run_seconds: int = 180


LIMITS = AgentLimits()
