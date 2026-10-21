-- employees: 지문 템플릿 슬롯 4개 (fid 0~3 전송 대응). 한 번만 실행.
ALTER TABLE employees ADD COLUMN fingerprint_template_3 MEDIUMTEXT NULL AFTER fingerprint_template_2;
ALTER TABLE employees ADD COLUMN fingerprint_template_4 MEDIUMTEXT NULL AFTER fingerprint_template_3;
