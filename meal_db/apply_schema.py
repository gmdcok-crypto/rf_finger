"""
meal_rf_finger DB 생성 + sql/schema_meal_rf_finger.sql 적용.
환경 변수: MEAL_DB_HOST, MEAL_DB_PORT, MEAL_DB_USER, MEAL_DB_PASSWORD, MEAL_DB_NAME
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCHEMA = _ROOT / "sql" / "schema_meal_rf_finger.sql"


def _strip_sql_comments(sql: str) -> str:
    lines = []
    for line in sql.splitlines():
        s = line.strip()
        if s.startswith("--"):
            continue
        lines.append(line)
    return "\n".join(lines)


def main() -> int:
    from meal_db.connection import load_meal_db_dotenv

    load_meal_db_dotenv()
    try:
        import mariadb
    except ImportError:
        print("MariaDB Connector/Python 필요: pip install mariadb", file=sys.stderr)
        return 1

    host = os.environ.get("MEAL_DB_HOST", "127.0.0.1")
    port = int(os.environ.get("MEAL_DB_PORT", "3306"))
    user = os.environ.get("MEAL_DB_USER", "root")
    password = os.environ.get("MEAL_DB_PASSWORD", "")
    db_name = os.environ.get("MEAL_DB_NAME", "meal_rf_finger")
    if not re.match(r"^[a-zA-Z0-9_]+$", db_name):
        print("MEAL_DB_NAME 은 영문·숫자·밑줄만 허용됩니다.", file=sys.stderr)
        return 1

    if not _SCHEMA.is_file():
        print("스키마 파일 없음:", _SCHEMA, file=sys.stderr)
        return 1

    sql_text = _strip_sql_comments(_SCHEMA.read_text(encoding="utf-8"))

    base_kw = dict(host=host, port=port, user=user, password=password, autocommit=True)

    try:
        conn = mariadb.connect(**base_kw)
    except Exception as e:
        print("DB 연결 실패:", e, file=sys.stderr)
        return 1

    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE DATABASE IF NOT EXISTS `%s` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                % db_name
            )
    finally:
        conn.close()

    try:
        conn = mariadb.connect(**base_kw, database=db_name)
    except Exception as e:
        print("DB 선택 실패:", e, file=sys.stderr)
        return 1

    try:
        with conn.cursor() as cur:
            for part in sql_text.split(";"):
                stmt = part.strip()
                if not stmt:
                    continue
                cur.execute(stmt)
        print("OK:", db_name, "스키마 적용 완료 (%s)" % _SCHEMA.name)
        return 0
    except Exception as e:
        print("스키마 실행 오류:", e, file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
