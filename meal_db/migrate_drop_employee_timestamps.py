"""
employees 테이블에서 사용하지 않는 created_at, updated_at 컬럼 제거 (기존 DB 1회 실행).

  python -m meal_db.migrate_drop_employee_timestamps

식수 기록 meal_logs.created_at 은 그대로 둡니다. 컬럼이 이미 없으면 건너뜁니다.
"""

from __future__ import annotations

import sys

from meal_db.connection import get_connection, ensure_driver


def _has_column(cur, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s
        LIMIT 1
        """,
        (table, column),
    )
    return cur.fetchone() is not None


def main() -> int:
    try:
        ensure_driver()
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            for column in ("updated_at", "created_at"):
                if _has_column(cur, "employees", column):
                    cur.execute("ALTER TABLE employees DROP COLUMN %s" % column)
                    print("OK: employees.%s 컬럼 제거" % column)
                else:
                    print("건너뜀: employees.%s 없음" % column)
            conn.commit()
        return 0
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
