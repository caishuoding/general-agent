"""从旧数据升级备份恢复业务 CSV 和附件目录。"""

import argparse
import json
import sys
import tarfile
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def restore_backup(database_url: str, backup_dir: Path, data_dir: Path) -> None:
    """把备份中的业务表 CSV 写入结构兼容的数据库，并解压附件归档。"""
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    with psycopg.connect(database_url) as connection:
        for table in manifest["tables"]:
            source = backup_dir / "database" / f"{table}.csv"
            if not source.is_file():
                raise FileNotFoundError(f"备份缺少表文件：{source}")
            statement = sql.SQL("COPY {} FROM STDIN WITH CSV HEADER").format(
                sql.Identifier(table)
            )
            with connection.cursor() as cursor:
                with source.open("rb") as input_file, cursor.copy(statement) as copy:
                    while chunk := input_file.read(65536):
                        copy.write(chunk)
    attachment_dir = data_dir / "attachments"
    attachment_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(backup_dir / "attachments.tar.gz", "r:gz") as archive:
        archive.extractall(attachment_dir, filter="data")


def parse_args() -> argparse.Namespace:
    """解析恢复目标数据库、备份目录和附件目标目录。"""
    parser = argparse.ArgumentParser(description="恢复登录升级前的数据备份")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    """执行恢复并打印结果。"""
    args = parse_args()
    restore_backup(args.database_url, args.backup_dir, args.data_dir)
    print("备份已恢复到目标数据库和附件目录。")


if __name__ == "__main__":
    main()
