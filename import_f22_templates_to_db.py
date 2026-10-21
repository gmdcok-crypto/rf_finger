"""
F22 단말에 등록된 지문 템플릿을 읽어 로컬 DB employees 에 저장한다.

  - 단말 user_id 는 사번(emp_no)과 같다고 가정한다 (ZK user_id).
  - DB: fingerprint_template ~ fingerprint_template_4 에 base64 저장 (fid 0~3).

인자 없이 실행: 단말 fid 0~9 중 지문이 있는 슬롯만 앞에서부터 최대 4개를 DB에 저장.
  python import_f22_templates_to_db.py --emp-no 111
  --fid0 등을 주면: 해당 fid만 지정 모드(생략 슬롯은 설정의 enroll_fid 기본).
  python import_f22_templates_to_db.py --emp-no 111 --fid0 5 --fid1 6
"""
from __future__ import annotations

import argparse
import sys

from meal_db.connection import load_meal_db_dotenv


def main() -> int:
    ap = argparse.ArgumentParser(description="F22 지문 템플릿 -> DB 저장")
    ap.add_argument("--emp-no", required=True, help="사번 (= 단말 user_id)")
    ap.add_argument(
        "--fid0",
        type=int,
        default=None,
        help="첫 손가락 슬롯(fid). 생략 시 등록 단말 설정 enroll_fid0",
    )
    ap.add_argument(
        "--fid1",
        type=int,
        default=None,
        help="둘째 손가락 슬롯(fid). 생략 시 등록 단말 설정 enroll_fid1",
    )
    ap.add_argument(
        "--fid2",
        type=int,
        default=None,
        help="셋째 손가락 슬롯(fid). 생략 시 등록 단말 설정 enroll_fid2",
    )
    ap.add_argument(
        "--fid3",
        type=int,
        default=None,
        help="넷째 손가락 슬롯(fid). 생략 시 등록 단말 설정 enroll_fid3",
    )
    args = ap.parse_args()
    emp_no = str(args.emp_no).strip()

    load_meal_db_dotenv()

    from meal_db.f22_import_from_terminal import import_fingerprint_templates_from_f22_terminal

    ok, res = import_fingerprint_templates_from_f22_terminal(
        emp_no,
        fid0=args.fid0,
        fid1=args.fid1,
        fid2=args.fid2,
        fid3=args.fid3,
    )
    if not ok:
        print(res, file=sys.stderr)
        return 1

    print("OK emp_no=%s DB 반영 완료" % emp_no)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
