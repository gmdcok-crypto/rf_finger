meal_rf_finger — PC UI 시작용
=============================

복사된 것
---------
- pc_app.py  ← meal_manage 프로젝트의 PC 관리자 앱 전체 (단일 파일)

UI만 분리된 상태가 아님
-----------------------
- 이 저장소에는 Qt Designer .ui 파일이 없고, 화면·스타일·DB 연동·프린터 등이
  pc_app.py 한 파일과 meal_db 모듈에 들어 있습니다.
- FastAPI·원격 REST/WebSocket 클라이언트는 포함하지 않으며, PC 앱이 MariaDB에 직접 붙습니다.

가상환경 (권장, 프로젝트 루트 기준)
-----------------------------------
  python -m venv .venv

  PowerShell (실행 정책 오류 시: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned)
    .\.venv\Scripts\Activate.ps1

  cmd.exe
    .venv\Scripts\activate.bat

  패키지 설치 (활성화 후 또는 venv python으로 직접)
    pip install -r requirements.txt
    또는  .\.venv\Scripts\pip install -r requirements.txt

  앱 실행 (활성화된 터미널에서)
    python pc_app.py

  Cursor/VS Code: 명령 팔레트 → "Python: Select Interpreter" →
    .\.venv\Scripts\python.exe 선택

데이터베이스 (항상 로컬 MariaDB / MySQL 호환)
--------------------------------------------
- 이 앱은 원격 API 로그인 없이 **로컬 DB만** 사용합니다.
1) 테이블 생성: 프로젝트 루트에서
     python meal_db\apply_schema.py
   (또는 GUI 클라이언트에서 sql/schema_meal_rf_finger.sql 실행)
   연결은 MEAL_DB_* 환경 변수·기본값(root/빈비번/127.0.0.1/meal_rf_finger) 사용.
   DB 드라이버는 MariaDB 공식 Python 커넥터(pip 패키지 mariadb)입니다.
2) 연결 환경 변수 (필요 시):
   MEAL_DB_HOST, MEAL_DB_PORT, MEAL_DB_USER, MEAL_DB_PASSWORD, MEAL_DB_NAME
   (기본 DB 이름: meal_rf_finger)
3) 실행: python pc_app.py (별도 MEAL_USE_LOCAL_DB 설정 불필요)
