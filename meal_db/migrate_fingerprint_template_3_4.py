"""
employees 테이블에 지문 슬롯 3·4 템플릿 컬럼 추가 (기존 DB 1회 실행).

  python -m meal_db.migrate_fingerprint_template_3_4
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
            if not _has_column(cur, "employees", "fingerprint_template_3"):
                cur.execute(
                    "ALTER TABLE employees ADD COLUMN fingerprint_template_3 MEDIUMTEXT NULL AFTER fingerprint_template_2"
                )
                print("OK: fingerprint_template_3 추가")
            else:
                print("건너뜀: fingerprint_template_3 이미 있음")
            if not _has_column(cur, "employees", "fingerprint_template_4"):
                cur.execute(
                    "ALTER TABLE employees ADD COLUMN fingerprint_template_4 MEDIUMTEXT NULL AFTER fingerprint_template_3"
                )
                print("OK: fingerprint_template_4 추가")
            else:
                print("건너뜀: fingerprint_template_4 이미 있음")
            conn.commit()
        return 0
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
