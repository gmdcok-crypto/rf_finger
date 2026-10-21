-- 각 템플릿이 단말의 어느 fid 슬롯에서 왔는지(0~9). 동기화 시 같은 fid에 기록한다.
SET NAMES utf8mb4;

ALTER TABLE employees ADD COLUMN fingerprint_slot_fid_1 TINYINT UNSIGNED NULL AFTER fingerprint_template_4;
ALTER TABLE employees ADD COLUMN fingerprint_slot_fid_2 TINYINT UNSIGNED NULL AFTER fingerprint_slot_fid_1;
ALTER TABLE employees ADD COLUMN fingerprint_slot_fid_3 TINYINT UNSIGNED NULL AFTER fingerprint_slot_fid_2;
ALTER TABLE employees ADD COLUMN fingerprint_slot_fid_4 TINYINT UNSIGNED NULL AFTER fingerprint_slot_fid_3;
