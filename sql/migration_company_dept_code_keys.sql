-- 기존 DB에 적용: 회사·부서 숫자 id 제거, code를 기본 키로 사용.
-- 사원 id / 식수 id / 정책 id 는 유지.
-- 부서코드가 중복이거나 회사코드가 비어 있으면 실행하지 말 것.
-- 권장: python -m meal_db.migrate_company_dept_code_keys

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

ALTER TABLE departments DROP FOREIGN KEY fk_dept_company;
ALTER TABLE employees DROP FOREIGN KEY fk_emp_company;
ALTER TABLE employees DROP FOREIGN KEY fk_emp_dept;
ALTER TABLE policies DROP FOREIGN KEY fk_policy_company;

ALTER TABLE departments ADD COLUMN company_code VARCHAR(32) NULL;
ALTER TABLE employees ADD COLUMN company_code VARCHAR(32) NULL;
ALTER TABLE employees ADD COLUMN department_code VARCHAR(32) NULL;
ALTER TABLE policies ADD COLUMN company_code VARCHAR(32) NULL;

UPDATE departments d
JOIN companies c ON d.company_id = c.id
SET d.company_code = c.code;

UPDATE employees e
JOIN companies c ON e.company_id = c.id
SET e.company_code = c.code;

UPDATE employees e
LEFT JOIN departments d ON e.department_id = d.id
SET e.department_code = d.code;

UPDATE policies p
LEFT JOIN companies c ON p.company_id = c.id
SET p.company_code = c.code;

ALTER TABLE companies DROP INDEX uq_companies_code;
ALTER TABLE departments DROP INDEX uq_dept_company_code;
ALTER TABLE employees DROP INDEX uq_emp_company_no;

ALTER TABLE companies MODIFY id BIGINT NOT NULL;
ALTER TABLE companies DROP PRIMARY KEY, ADD PRIMARY KEY (code);
ALTER TABLE companies DROP COLUMN id;

ALTER TABLE departments MODIFY id BIGINT NOT NULL;
ALTER TABLE departments DROP PRIMARY KEY;
ALTER TABLE departments DROP COLUMN id, DROP COLUMN company_id;
ALTER TABLE departments MODIFY company_code VARCHAR(32) NOT NULL;
ALTER TABLE departments ADD PRIMARY KEY (code);

ALTER TABLE employees DROP COLUMN company_id, DROP COLUMN department_id;
ALTER TABLE employees MODIFY company_code VARCHAR(32) NOT NULL;
ALTER TABLE employees ADD UNIQUE KEY uq_emp_company_no (company_code, emp_no);

ALTER TABLE policies DROP COLUMN company_id;

ALTER TABLE departments
  ADD CONSTRAINT fk_dept_company
  FOREIGN KEY (company_code) REFERENCES companies(code)
  ON DELETE CASCADE ON UPDATE CASCADE;

ALTER TABLE employees
  ADD CONSTRAINT fk_emp_company
  FOREIGN KEY (company_code) REFERENCES companies(code)
  ON DELETE CASCADE ON UPDATE CASCADE;

ALTER TABLE employees
  ADD CONSTRAINT fk_emp_dept
  FOREIGN KEY (department_code) REFERENCES departments(code)
  ON DELETE SET NULL ON UPDATE CASCADE;

ALTER TABLE policies
  ADD CONSTRAINT fk_policy_company
  FOREIGN KEY (company_code) REFERENCES companies(code)
  ON DELETE CASCADE ON UPDATE CASCADE;

SET FOREIGN_KEY_CHECKS = 1;
