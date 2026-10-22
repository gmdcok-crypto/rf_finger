-- MariaDB / MySQL 8+ 호환 — 데이터베이스 meal_rf_finger 에 적용
-- mysql -u root -p meal_rf_finger < sql/schema_meal_rf_finger.sql

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

CREATE TABLE IF NOT EXISTS companies (
  code VARCHAR(32) NOT NULL PRIMARY KEY,
  name VARCHAR(255) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS departments (
  code VARCHAR(32) NOT NULL PRIMARY KEY,
  company_code VARCHAR(32) NOT NULL,
  name VARCHAR(255) NOT NULL,
  CONSTRAINT fk_dept_company FOREIGN KEY (company_code) REFERENCES companies(code) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS employees (
  emp_no VARCHAR(64) NOT NULL PRIMARY KEY,
  company_code VARCHAR(32) NOT NULL,
  department_code VARCHAR(32) NULL,
  name VARCHAR(255) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE',
  rf_card_uid VARCHAR(128) NULL,
  fingerprint_template MEDIUMTEXT NULL,
  fingerprint_template_2 MEDIUMTEXT NULL,
  fingerprint_template_3 MEDIUMTEXT NULL,
  fingerprint_template_4 MEDIUMTEXT NULL,
  fingerprint_slot_fid_1 TINYINT UNSIGNED NULL,
  fingerprint_slot_fid_2 TINYINT UNSIGNED NULL,
  fingerprint_slot_fid_3 TINYINT UNSIGNED NULL,
  fingerprint_slot_fid_4 TINYINT UNSIGNED NULL,
  CONSTRAINT fk_emp_company FOREIGN KEY (company_code) REFERENCES companies(code) ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT fk_emp_dept FOREIGN KEY (department_code) REFERENCES departments(code) ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS policies (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  company_code VARCHAR(32) NULL,
  meal_type VARCHAR(64) NOT NULL,
  start_time TIME NOT NULL,
  end_time TIME NOT NULL,
  base_price INT NOT NULL DEFAULT 0,
  guest_price INT NOT NULL DEFAULT 0,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_policy_company FOREIGN KEY (company_code) REFERENCES companies(code) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS meal_logs (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  emp_no VARCHAR(64) NOT NULL,
  policy_id BIGINT NULL,
  created_at DATETIME NOT NULL,
  guest_count INT NOT NULL DEFAULT 0,
  final_price INT NOT NULL DEFAULT 0,
  is_void TINYINT(1) NOT NULL DEFAULT 0,
  void_reason VARCHAR(512) NULL,
  auth_uid VARCHAR(128) NULL,
  path VARCHAR(64) NULL DEFAULT 'local',
  CONSTRAINT fk_log_emp FOREIGN KEY (emp_no) REFERENCES employees(emp_no) ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT fk_log_policy FOREIGN KEY (policy_id) REFERENCES policies(id) ON DELETE SET NULL,
  KEY idx_meal_logs_created (created_at),
  KEY idx_meal_logs_emp_created (emp_no, created_at),
  UNIQUE KEY uq_meal_logs_auth_uid (auth_uid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS auth_failure_logs (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  created_at DATETIME NOT NULL,
  card_no VARCHAR(128) NULL,
  terminal_device VARCHAR(128) NULL,
  terminal_label VARCHAR(128) NULL,
  adms_sn VARCHAR(64) NULL,
  event_code INT NULL,
  event_label VARCHAR(128) NULL,
  verify_type VARCHAR(64) NULL,
  pin_raw VARCHAR(64) NULL,
  auth_uid VARCHAR(128) NOT NULL,
  recorded_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_auth_failure_auth_uid (auth_uid),
  KEY idx_auth_failure_created (created_at),
  KEY idx_auth_failure_card (card_no, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS device_settings (
  id TINYINT NOT NULL PRIMARY KEY DEFAULT 1,
  payload_json LONGTEXT NOT NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS attlog_clear_settings (
  id TINYINT NOT NULL PRIMARY KEY DEFAULT 1,
  enabled TINYINT(1) NOT NULL DEFAULT 0,
  minutes_after_meal_end INT NOT NULL DEFAULT 10,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS admins (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  emp_no VARCHAR(64) NOT NULL,
  name VARCHAR(255) NOT NULL,
  password_hash VARCHAR(512) NULL,
  is_verified TINYINT(1) NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_admins_emp_no (emp_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET FOREIGN_KEY_CHECKS = 1;

INSERT IGNORE INTO companies (code, name) VALUES ('HQ', '본사');
INSERT IGNORE INTO departments (code, company_code, name) VALUES ('DEFAULT', 'HQ', '기본부서');
INSERT IGNORE INTO device_settings (id, payload_json) VALUES (1, '{}');
INSERT IGNORE INTO attlog_clear_settings (id, enabled, minutes_after_meal_end) VALUES (1, 1, 10);

-- 기본 관리자 1명 (초기 비밀번호 평문: meal1234!)
INSERT INTO admins (emp_no, name, is_verified, password_hash)
SELECT 'admin', '관리자', 0, 'pbkdf2_sha256$310000$0123456789abcdef0123456789abcdef$537309fef35cc373b417f3e570f42bdbad7426bbea9c650b4eb1cce4361b3718'
FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM admins LIMIT 1);
