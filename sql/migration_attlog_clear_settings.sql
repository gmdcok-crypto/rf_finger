-- 단말 출입 로그 자동 동기화·삭제 시각 설정 (식사 정책 end_time + N분)
-- mysql -u ... meal_rf_finger < sql/migration_attlog_clear_settings.sql

SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS attlog_clear_settings (
  id TINYINT NOT NULL PRIMARY KEY DEFAULT 1,
  enabled TINYINT(1) NOT NULL DEFAULT 0,
  minutes_after_meal_end INT NOT NULL DEFAULT 10,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO attlog_clear_settings (id, enabled, minutes_after_meal_end) VALUES (1, 1, 10);
