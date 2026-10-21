-- 단말 반영 건(auth_uid) DB 유니크: 재실행·pull 중복 삽입 방지
-- mysql -u ... meal_rf_finger < sql/migration_meal_logs_auth_uid_unique.sql
--
-- 수동 입력 등 auth_uid 가 NULL 인 행은 여러 개 허용(MySQL/MariaDB UNIQUE + NULL 규칙).

SET NAMES utf8mb4;

DELETE m1 FROM meal_logs m1
INNER JOIN meal_logs m2
  ON m1.auth_uid = m2.auth_uid
  AND m1.auth_uid IS NOT NULL
  AND TRIM(m1.auth_uid) <> ''
  AND m1.id > m2.id;

ALTER TABLE meal_logs
  ADD UNIQUE KEY uq_meal_logs_auth_uid (auth_uid);
