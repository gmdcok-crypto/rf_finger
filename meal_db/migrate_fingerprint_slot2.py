"""
employees 테이블에 지문 슬롯1 템플릿 컬럼 추가 (기존 DB 1회 실행).

앱과 동일한 환경 변수: MEAL_DB_HOST, MEAL_DB_PORT, MEAL_DB_USER, MEAL_DB_PASSWORD, MEAL_DB_NAME

  python -m meal_db.migrate_fingerprint_slot2
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
            if not _has_column(cur, "employees", "fingerprint_template_2"):
                if _has_column(cur, "employees", "fingerprint_uid_2"):
                    cur.execute(
                        "ALTER TABLE employees ADD COLUMN fingerprint_template_2 MEDIUMTEXT NULL AFTER fingerprint_uid_2"
                    )
                else:
                    cur.execute(
                        "ALTER TABLE employees ADD COLUMN fingerprint_template_2 MEDIUMTEXT NULL AFTER fingerprint_template"
                    )
                print("OK: fingerprint_template_2 추가")
            else:
                print("건너뜀: fingerprint_template_2 이미 있음")
        print("마이그레이션 완료.")
        return 0
    except Exception as e:
        print("실패:", e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
