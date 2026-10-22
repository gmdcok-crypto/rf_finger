-- 기존 DB: 사원 숫자 id 제거, 사번(emp_no)을 기본 키로 사용.
-- 식수 기록 meal_logs.employee_id → emp_no.
-- 식수 기록 id / 정책 id / 관리자 id 는 유지.
-- 사번 중복·빈 사번이 있으면 실행하지 말 것.
-- 권장: python -m meal_db.migrate_employee_emp_no_key

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

ALTER TABLE meal_logs DROP FOREIGN KEY fk_log_emp;

ALTER TABLE meal_logs ADD COLUMN emp_no VARCHAR(64) NULL;

UPDATE meal_logs m
JOIN employees e ON m.employee_id = e.id
SET m.emp_no = e.emp_no;

ALTER TABLE meal_logs DROP INDEX idx_meal_logs_emp_created;
ALTER TABLE meal_logs DROP COLUMN employee_id;
ALTER TABLE meal_logs MODIFY emp_no VARCHAR(64) NOT NULL;
ALTER TABLE meal_logs ADD KEY idx_meal_logs_emp_created (emp_no, created_at);

ALTER TABLE employees DROP INDEX uq_emp_company_no;
ALTER TABLE employees MODIFY id BIGINT NOT NULL;
ALTER TABLE employees DROP PRIMARY KEY;
ALTER TABLE employees DROP COLUMN id;
ALTER TABLE employees ADD PRIMARY KEY (emp_no);

ALTER TABLE meal_logs
  ADD CONSTRAINT fk_log_emp
  FOREIGN KEY (emp_no) REFERENCES employees(emp_no)
  ON DELETE RESTRICT ON UPDATE CASCADE;

SET FOREIGN_KEY_CHECKS = 1;
