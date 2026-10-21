-- 기존 DB에 슬롯2 지문 템플릿 컬럼 추가 (한 번만 실행)
-- fingerprint_uid / fingerprint_uid_2 는 더 이상 사용하지 않음(schema 참고)
-- mysql -u root -p meal_rf_finger < sql/migration_employee_fingerprint_slot2.sql

ALTER TABLE employees
  ADD COLUMN fingerprint_template_2 MEDIUMTEXT NULL AFTER fingerprint_template;
