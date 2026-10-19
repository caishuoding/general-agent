"""备份并清空旧单用户数据，供登录模块升级时一次性执行。"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from general_agent.auth.upgrade import run_auth_upgrade  # noqa: E402
from general_agent.memory.mem0_adapter import Mem0Client  # noqa: E402


def parse_args() -> argparse.Namespace:
    """解析数据库地址和应用数据目录，默认沿用应用环境变量。"""
    parser = argparse.ArgumentParser(description="备份并清空旧单用户业务数据")
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "DATABASE_URL",
            "postgresql://general_agent:local-development-only@127.0.0.1:15432/general_agent",
        ),
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("APP_DATA_DIR", ".local")),
    )
    parser.add_argument(
        "--mem0-url",
        default=os.environ.get("MEM0_API_URL", "http://127.0.0.1:8888"),
    )
    parser.add_argument(
        "--mem0-user-id",
        default=os.environ.get("MEM0_USER_ID", "local-person"),
    )
    return parser.parse_args()


def clear_old_memories(url: str, user_id: str) -> None:
    """删除旧单用户空间中的全部 Mem0 记忆，失败时阻止清空业务数据。"""
    with Mem0Client(base_url=url) as client:
        while True:
            entries = client.get_all(user_id)
            if not entries:
                return
            for entry in entries:
                memory_id = entry.get("id")
                if memory_id:
                    client.delete(str(memory_id))


def main() -> None:
    """执行一次升级并打印备份位置或跳过原因。"""
    args = parse_args()
    backup = run_auth_upgrade(
        args.database_url,
        args.data_dir,
        clear_memories=lambda: clear_old_memories(args.mem0_url, args.mem0_user_id),
    )
    if backup is None:
        print("升级清空已执行过，未改动任何数据。")
    else:
        print(f"备份完成：{backup}")
        print("旧业务数据已清空；下一次启动应用会创建账号结构。")


if __name__ == "__main__":
    main()
