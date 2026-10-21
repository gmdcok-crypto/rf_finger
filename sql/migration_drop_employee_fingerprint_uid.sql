-- 기존 DB에 적용: employees 테이블에서 지문 UID 컬럼 제거 (템플릿만 유지)
-- mysql -u ... meal_rf_finger < sql/migration_drop_employee_fingerprint_uid.sql

SET NAMES utf8mb4;

ALTER TABLE employees DROP COLUMN fingerprint_uid;
ALTER TABLE employees DROP COLUMN fingerprint_uid_2;
