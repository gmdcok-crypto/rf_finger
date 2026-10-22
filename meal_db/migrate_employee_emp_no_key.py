"""
employees 숫자 id를 제거하고 사번(emp_no)을 기본 키로 바꾼다 (기존 DB 1회 실행).

  python -m meal_db.migrate_employee_emp_no_key

식수 기록 meal_logs.employee_id 는 emp_no 로 옮긴다.
식수 기록 자신의 id, 정책 id, 관리자 id 는 유지한다.
사번을 수정하면 식수 기록의 사번도 ON UPDATE CASCADE 로 따라간다.
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


def _drop_index_if_exists(cur, table: str, index_name: str) -> None:
    cur.execute(
        """
        SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s
        LIMIT 1
        """,
        (table, index_name),
    )
    if cur.fetchone():
        cur.execute("ALTER TABLE `%s` DROP INDEX `%s`" % (table, index_name))
        print("인덱스 제거: %s.%s" % (table, index_name))


def _scalar(cur, sql: str) -> int:
    cur.execute(sql)
    row = cur.fetchone()
    if not row:
        return 0
    return int(next(iter(row.values())))


def _drop_meal_log_employee_fks(cur) -> None:
    cur.execute(
        """
        SELECT TABLE_NAME, CONSTRAINT_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'meal_logs'
          AND REFERENCED_TABLE_NAME = 'employees'
          AND REFERENCED_COLUMN_NAME IS NOT NULL
        """
    )
    seen = set()
    for row in cur.fetchall() or []:
        key = (row["TABLE_NAME"], row["CONSTRAINT_NAME"])
        if key in seen:
            continue
        seen.add(key)
        cur.execute(
            "ALTER TABLE `%s` DROP FOREIGN KEY `%s`" % (row["TABLE_NAME"], row["CONSTRAINT_NAME"])
        )
        print("FK 제거: %s.%s" % key)


def main() -> int:
    try:
        ensure_driver()
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            if (
                not _has_column(cur, "employees", "id")
                and _has_column(cur, "meal_logs", "emp_no")
                and not _has_column(cur, "meal_logs", "employee_id")
            ):
                print("건너뜀: 사번 키로 이미 전환됨")
                return 0
            if not _has_column(cur, "employees", "id"):
                print("employees.id 가 없습니다. 스키마를 확인하세요.", file=sys.stderr)
                return 1

            dup = _scalar(
                cur,
                "SELECT COUNT(*) AS c FROM (SELECT emp_no FROM employees GROUP BY emp_no HAVING COUNT(*) > 1) t",
            )
            if dup:
                print("사번이 중복입니다. emp_no 를 기본 키로 만들 수 없습니다.", file=sys.stderr)
                return 1
            blank = _scalar(
                cur,
                "SELECT COUNT(*) AS c FROM employees WHERE emp_no IS NULL OR TRIM(emp_no) = ''",
            )
            if blank:
                print("비어 있는 사번이 있습니다.", file=sys.stderr)
                return 1

            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            _drop_meal_log_employee_fks(cur)

            if not _has_column(cur, "meal_logs", "emp_no"):
                cur.execute("ALTER TABLE meal_logs ADD COLUMN emp_no VARCHAR(64) NULL")
            if _has_column(cur, "meal_logs", "employee_id"):
                cur.execute(
                    """
                    UPDATE meal_logs m
                    JOIN employees e ON m.employee_id = e.id
                    SET m.emp_no = e.emp_no
                    """
                )
                missing = _scalar(
                    cur,
                    "SELECT COUNT(*) AS c FROM meal_logs WHERE emp_no IS NULL OR TRIM(emp_no) = ''",
                )
                if missing:
                    print("사번에 연결되지 않은 식수 기록이 있습니다.", file=sys.stderr)
                    return 1
                cur.execute(
                    """
                    SELECT DISTINCT INDEX_NAME
                    FROM INFORMATION_SCHEMA.STATISTICS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'meal_logs'
                      AND COLUMN_NAME = 'employee_id'
                      AND INDEX_NAME <> 'PRIMARY'
                    """
                )
                for idx in cur.fetchall() or []:
                    _drop_index_if_exists(cur, "meal_logs", idx["INDEX_NAME"])
                cur.execute("ALTER TABLE meal_logs DROP COLUMN employee_id")

            cur.execute("ALTER TABLE meal_logs MODIFY emp_no VARCHAR(64) NOT NULL")
            _drop_index_if_exists(cur, "meal_logs", "idx_meal_logs_emp_created")
            cur.execute(
                "ALTER TABLE meal_logs ADD KEY idx_meal_logs_emp_created (emp_no, created_at)"
            )

            _drop_index_if_exists(cur, "employees", "uq_emp_company_no")
            cur.execute("ALTER TABLE employees MODIFY id BIGINT NOT NULL")
            cur.execute("ALTER TABLE employees DROP PRIMARY KEY")
            cur.execute("ALTER TABLE employees DROP COLUMN id")
            cur.execute("ALTER TABLE employees ADD PRIMARY KEY (emp_no)")
            cur.execute(
                """
                ALTER TABLE meal_logs
                ADD CONSTRAINT fk_log_emp
                FOREIGN KEY (emp_no) REFERENCES employees(emp_no)
                ON DELETE RESTRICT ON UPDATE CASCADE
                """
            )
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
            conn.commit()
            print("OK: employees.id 제거, emp_no 기본 키로 전환")
        return 0
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
