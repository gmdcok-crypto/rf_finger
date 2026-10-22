-- 기존 DB에 적용: employees 의 created_at, updated_at 제거 (앱에서 미사용)
-- 식수 기록 meal_logs.created_at 은 유지
-- mysql -u ... meal_rf_finger < sql/migration_drop_employee_timestamps.sql

SET NAMES utf8mb4;

ALTER TABLE employees DROP COLUMN updated_at;
ALTER TABLE employees DROP COLUMN created_at;
