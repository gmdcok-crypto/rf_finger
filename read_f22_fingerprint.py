"""
F22 단말에서 지정 user_id의 지문 템플릿을 읽어 출력한다.
  - IP/포트: DB device_settings.payload_json 의 f22_terminals (첫 번째 단말)
  - 통신 비번: ZK_DEVICE_COMM_PASSWORD (미설정 시 0)

사용:
  python read_f22_fingerprint.py
  python read_f22_fingerprint.py --user-id 111
"""
from __future__ import annotations

import argparse
import base64
import os
import sys


def _main() -> int:
    ap = argparse.ArgumentParser(description="F22에서 지문 템플릿 읽기")
    ap.add_argument("--user-id", default="111", help="단말 사번(user_id), 기본 111")
    ap.add_argument(
        "--fids",
        default="0,1,2,3,4,5,6,7,8,9",
        help="읽을 템플릿 인덱스(temp_id), 쉼표 구분. 기본 0~9",
    )
    args = ap.parse_args()
    user_id = str(args.user_id).strip()
    try:
        fids = [int(x.strip()) for x in str(args.fids).split(",") if x.strip() != ""]
    except ValueError:
        print("잘못된 --fids", file=sys.stderr)
        return 2

    try:
        pwd = int(os.environ.get("ZK_DEVICE_COMM_PASSWORD", "0") or "0")
    except ValueError:
        pwd = 0

    from meal_db.connection import load_meal_db_dotenv
    from meal_db.local_client import LocalMealClient

    load_meal_db_dotenv()
    settings = LocalMealClient().get_device_settings() or {}
    import f22_pyzk

    t0 = f22_pyzk.pick_f22_terminal_for_import(settings)
    if t0 is None:
        print("device_settings 에 F22 단말이 없습니다. 설정에서 단말 IP를 저장하세요.", file=sys.stderr)
        return 1

    host = str(t0.get("ip") or t0.get("host") or "").strip()
    try:
        port = int(t0.get("port") or 4370)
    except (TypeError, ValueError):
        port = 4370

    from zk import ZK

    print("단말: %s:%s  비번: %s  user_id: %s" % (host, port, pwd, user_id))
    zk = ZK(host, port=port, password=pwd, timeout=25, ommit_ping=True)
    conn = None
    try:
        conn = zk.connect()
    except Exception as e:
        print("연결 실패:", e, file=sys.stderr)
        return 1

    try:
        try:
            conn.disable_device()
        except Exception:
            pass

        users = conn.get_users()
        u = None
        for x in users or []:
            if str(x.user_id).strip() == user_id:
                u = x
                break
        if u is None:
            print("단말에 user_id=%r 인 사용자가 없습니다." % user_id)
            sample = []
            for x in (users or [])[:30]:
                sample.append(str(getattr(x, "user_id", "")).strip())
            if sample:
                print("단말에 등록된 user_id 예시 (최대 30명): " + ", ".join(sample))
            return 1

        print("내부 uid=%s  name=%r" % (u.uid, getattr(u, "name", "")))

        def _print_finger(label: str, finger) -> None:
            tpl = getattr(finger, "template", None) or b""
            fid = getattr(finger, "fid", "?")
            valid = getattr(finger, "valid", "?")
            b64 = base64.b64encode(tpl).decode("ascii")
            print("  %s  fid=%s valid=%s  len=%d" % (label, fid, valid, len(tpl)))
            print("         b64(앞80자)=%s..." % (b64[:80],))
            print("         hex(앞32)=%s" % tpl[:32].hex())

        any_ok = False
        for fid in fids:
            finger = conn.get_user_template(u.uid, temp_id=fid, user_id="")
            if finger is None or finger is False:
                print("  get_user_template fid=%s  (없음 또는 읽기 실패)" % fid)
                continue
            _print_finger("get_user_template", finger)
            any_ok = True

        if not any_ok:
            print("get_user_template 실패 - get_templates() 전체 덤프에서 uid=%s 만 필터" % u.uid)
            try:
                bulk = conn.get_templates() or []
            except Exception as ex:
                print("get_templates 예외:", ex, file=sys.stderr)
                bulk = []
            for finger in bulk:
                if int(getattr(finger, "uid", -1)) != int(u.uid):
                    continue
                _print_finger("get_templates", finger)
                any_ok = True

        if not any_ok:
            print("등록된 지문 템플릿을 읽지 못했습니다.")
            return 1
        return 0
    finally:
        if conn is not None:
            try:
                conn.enable_device()
            except Exception:
                pass
            try:
                conn.disconnect()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(_main())
