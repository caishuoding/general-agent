"""在服务器本机恢复唯一管理员密码。"""

import argparse
import getpass
import os
import sys
from pathlib import Path
from uuid import uuid4

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from general_agent.auth.passwords import hash_password, validate_new_password  # noqa: E402


def recover_admin(database_url: str, new_password: str) -> None:
    """接收数据库地址和新密码，更新唯一管理员凭据、吊销会话并追加恢复审计。"""
    validate_new_password(new_password, "admin")
    with psycopg.connect(database_url) as connection:
        # 锁定唯一账号，保证改密、会话吊销和审计处于同一事务。
        admin = connection.execute("SELECT id FROM admin_accounts WHERE username='admin' FOR UPDATE").fetchone()
        if admin is None:
            raise LookupError("管理员账号不存在，请先启动应用完成数据库迁移。")
        admin_id = admin[0]
        connection.execute(
            "UPDATE admin_accounts SET password_hash=%s, must_change_password=FALSE, "
            "password_changed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=%s",
            (hash_password(new_password), admin_id),
        )
        connection.execute("DELETE FROM admin_sessions WHERE user_id=%s", (admin_id,))
        connection.execute(
            "INSERT INTO admin_audit_log (id,admin_user_id,target_user_id,resource_type,action) "
            "VALUES (%s,%s,NULL,'administrator','recover_password')",
            (uuid4(), admin_id),
        )


def main() -> None:
    """从环境读取数据库地址并交互输入新密码，完成后只输出结果状态。"""
    parser = argparse.ArgumentParser(description="在服务器本机恢复唯一管理员密码")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args()
    if not args.database_url:
        parser.error("需要 DATABASE_URL 或 --database-url。")
    if not sys.stdin.isatty():
        parser.error("请在服务器的交互式终端中运行。")
    password = getpass.getpass("新管理员密码：")
    confirmation = getpass.getpass("再次输入：")
    if password != confirmation:
        parser.error("两次输入的密码不一致。")
    recover_admin(args.database_url, password)
    print("管理员密码已更新，旧登录会话已失效。")


if __name__ == "__main__":
    main()
