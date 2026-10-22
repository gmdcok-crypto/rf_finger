"""
회사·부서의 숫자 id를 제거하고 code를 기본 키로 바꾼다 (기존 DB 1회 실행).

  python -m meal_db.migrate_company_dept_code_keys

사원 id, 식수 기록 id, 정책 id는 유지한다.
부서·사원·정책의 회사 참조와 사원의 부서 참조는 코드로 옮긴다.
코드 변경 시 하위 행은 ON UPDATE CASCADE로 따라간다.
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


def _drop_fks_referencing(cur, tables: tuple) -> None:
    placeholders = ",".join(["%s"] * len(tables))
    cur.execute(
        """
        SELECT TABLE_NAME, CONSTRAINT_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = DATABASE()
          AND REFERENCED_TABLE_NAME IN (%s)
          AND REFERENCED_COLUMN_NAME IS NOT NULL
        """
        % placeholders,
        tables,
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


def _scalar(cur, sql: str):
    cur.execute(sql)
    row = cur.fetchone()
    if not row:
        return 0
    return int(next(iter(row.values())))


def main() -> int:
    try:
        ensure_driver()
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            if not _has_column(cur, "companies", "id") and _has_column(cur, "employees", "company_code"):
                print("건너뜀: 회사·부서 코드 키로 이미 전환됨")
                return 0
            if not _has_column(cur, "companies", "id"):
                print("companies.id 가 없습니다. 스키마를 확인하세요.", file=sys.stderr)
                return 1

            dup_dept = _scalar(
                cur,
                "SELECT COUNT(*) AS c FROM (SELECT code FROM departments GROUP BY code HAVING COUNT(*) > 1) t",
            )
            if dup_dept:
                print("부서코드가 회사 간에 중복입니다. code를 기본 키로 만들 수 없습니다.", file=sys.stderr)
                return 1
            blank = _scalar(
                cur,
                """
                SELECT
                  (SELECT COUNT(*) FROM companies WHERE code IS NULL OR TRIM(code) = '')
                  + (SELECT COUNT(*) FROM departments WHERE code IS NULL OR TRIM(code) = '')
                AS c
                """,
            )
            if blank:
                print("회사코드 또는 부서코드가 비어 있습니다.", file=sys.stderr)
                return 1
            orphan_emp = _scalar(
                cur,
                """
                SELECT COUNT(*) AS c FROM employees e
                LEFT JOIN companies c ON e.company_id = c.id
                WHERE c.id IS NULL
                """,
            )
            if orphan_emp:
                print("회사에 연결되지 않은 사원이 있습니다.", file=sys.stderr)
                return 1

            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            _drop_fks_referencing(cur, ("companies", "departments"))

            if not _has_column(cur, "departments", "company_code"):
                cur.execute("ALTER TABLE departments ADD COLUMN company_code VARCHAR(32) NULL")
            if not _has_column(cur, "employees", "company_code"):
                cur.execute("ALTER TABLE employees ADD COLUMN company_code VARCHAR(32) NULL")
            if not _has_column(cur, "employees", "department_code"):
                cur.execute("ALTER TABLE employees ADD COLUMN department_code VARCHAR(32) NULL")
            if not _has_column(cur, "policies", "company_code"):
                cur.execute("ALTER TABLE policies ADD COLUMN company_code VARCHAR(32) NULL")

            cur.execute(
                """
                UPDATE departments d
                JOIN companies c ON d.company_id = c.id
                SET d.company_code = c.code
                """
            )
            cur.execute(
                """
                UPDATE employees e
                JOIN companies c ON e.company_id = c.id
                SET e.company_code = c.code
                """
            )
            cur.execute(
                """
                UPDATE employees e
                LEFT JOIN departments d ON e.department_id = d.id
                SET e.department_code = d.code
                """
            )
            cur.execute(
                """
                UPDATE policies p
                LEFT JOIN companies c ON p.company_id = c.id
                SET p.company_code = c.code
                """
            )

            missing_dept_company = _scalar(
                cur,
                "SELECT COUNT(*) AS c FROM departments WHERE company_code IS NULL OR TRIM(company_code) = ''",
            )
            missing_emp_company = _scalar(
                cur,
                "SELECT COUNT(*) AS c FROM employees WHERE company_code IS NULL OR TRIM(company_code) = ''",
            )
            if missing_dept_company or missing_emp_company:
                conn.rollback()
                print("회사코드 백필에 실패한 행이 있습니다.", file=sys.stderr)
                return 1

            _drop_index_if_exists(cur, "companies", "uq_companies_code")
            _drop_index_if_exists(cur, "departments", "uq_dept_company_code")
            _drop_index_if_exists(cur, "employees", "uq_emp_company_no")

            cur.execute("ALTER TABLE companies MODIFY id BIGINT NOT NULL")
            cur.execute("ALTER TABLE companies DROP PRIMARY KEY, ADD PRIMARY KEY (code)")
            cur.execute("ALTER TABLE companies DROP COLUMN id")

            cur.execute("ALTER TABLE departments MODIFY id BIGINT NOT NULL")
            cur.execute("ALTER TABLE departments DROP PRIMARY KEY")
            cur.execute("ALTER TABLE departments DROP COLUMN id, DROP COLUMN company_id")
            cur.execute("ALTER TABLE departments MODIFY company_code VARCHAR(32) NOT NULL")
            cur.execute("ALTER TABLE departments ADD PRIMARY KEY (code)")

            cur.execute("ALTER TABLE employees DROP COLUMN company_id, DROP COLUMN department_id")
            cur.execute("ALTER TABLE employees MODIFY company_code VARCHAR(32) NOT NULL")
            cur.execute(
                "ALTER TABLE employees ADD UNIQUE KEY uq_emp_company_no (company_code, emp_no)"
            )

            cur.execute("ALTER TABLE policies DROP COLUMN company_id")

            cur.execute(
                """
                ALTER TABLE departments
                ADD CONSTRAINT fk_dept_company
                FOREIGN KEY (company_code) REFERENCES companies(code)
                ON DELETE CASCADE ON UPDATE CASCADE
                """
            )
            cur.execute(
                """
                ALTER TABLE employees
                ADD CONSTRAINT fk_emp_company
                FOREIGN KEY (company_code) REFERENCES companies(code)
                ON DELETE CASCADE ON UPDATE CASCADE
                """
            )
            cur.execute(
                """
                ALTER TABLE employees
                ADD CONSTRAINT fk_emp_dept
                FOREIGN KEY (department_code) REFERENCES departments(code)
                ON DELETE SET NULL ON UPDATE CASCADE
                """
            )
            cur.execute(
                """
                ALTER TABLE policies
                ADD CONSTRAINT fk_policy_company
                FOREIGN KEY (company_code) REFERENCES companies(code)
                ON DELETE CASCADE ON UPDATE CASCADE
                """
            )
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
            conn.commit()
            print("OK: 회사·부서 id 제거, code 기본 키로 전환")
        return 0
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
