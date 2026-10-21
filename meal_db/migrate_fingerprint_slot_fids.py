"""
employees 에 fingerprint_slot_fid_1~4 추가 (단말 fid 0~9, 템플릿 칸과 1:1).

  python -m meal_db.migrate_fingerprint_slot_fids
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
    cols = [
        ("fingerprint_slot_fid_1", "fingerprint_template_4"),
        ("fingerprint_slot_fid_2", "fingerprint_slot_fid_1"),
        ("fingerprint_slot_fid_3", "fingerprint_slot_fid_2"),
        ("fingerprint_slot_fid_4", "fingerprint_slot_fid_3"),
    ]
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            for name, after in cols:
                if not _has_column(cur, "employees", name):
                    cur.execute(
                        "ALTER TABLE employees ADD COLUMN %s TINYINT UNSIGNED NULL AFTER %s"
                        % (name, after)
                    )
                    print("OK: %s 추가" % name)
                else:
                    print("건너뜀: %s 이미 있음" % name)
            conn.commit()
        return 0
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
