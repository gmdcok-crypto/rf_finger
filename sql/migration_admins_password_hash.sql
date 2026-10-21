-- 관리자 로그인용 password_hash (초기 평문: meal1234!)
-- mysql -u ... meal_rf_finger < sql/migration_admins_password_hash.sql

SET NAMES utf8mb4;

ALTER TABLE admins
  ADD COLUMN password_hash VARCHAR(512) NULL AFTER name;

-- PBKDF2-SHA256, 310000 rounds, 고정 salt(마이그레이션 재현용). 평문: meal1234!
UPDATE admins SET password_hash = 'pbkdf2_sha256$310000$0123456789abcdef0123456789abcdef$537309fef35cc373b417f3e570f42bdbad7426bbea9c650b4eb1cce4361b3718'
WHERE password_hash IS NULL OR password_hash = '';

INSERT INTO admins (emp_no, name, is_verified, password_hash)
SELECT 'admin', '관리자', 0, 'pbkdf2_sha256$310000$0123456789abcdef0123456789abcdef$537309fef35cc373b417f3e570f42bdbad7426bbea9c650b4eb1cce4361b3718'
FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM admins LIMIT 1);
