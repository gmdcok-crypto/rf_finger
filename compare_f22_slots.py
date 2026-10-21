"""
F22 단말: fid 0~3 (프로그램 전송 슬롯) vs fid 5,6 (단말이 다른 번호에 넣은 경우 예시) 비교.
DB employees 의 템플릿 1~4칸이 있으면 단말 슬롯과도 비교.
"""
from __future__ import annotations

import base64
import os
import sys


def _tpl_meta(b: bytes) -> str:
    if not b:
        return "(비어 있음)"
    n = len(b)
    head = b[:16].hex()
    ss21 = b[2:6] == b"SS21" if len(b) >= 6 else False
    return "len=%d  hex[:16]=%s  offset2-5==SS21: %s" % (n, head, ss21)


def _same(a: bytes | None, b: bytes | None) -> bool:
    if not a or not b:
        return False
    return a == b


def _pct_equal(a: bytes, b: bytes) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    same = sum(1 for i in range(n) if a[i] == b[i])
    return 100.0 * same / n


def main() -> int:
    user_id = (os.environ.get("COMPARE_USER_ID") or "111").strip()
    try:
        pwd = int(os.environ.get("ZK_DEVICE_COMM_PASSWORD", "0") or "0")
    except ValueError:
        pwd = 0

    from meal_db.connection import load_meal_db_dotenv
    from meal_db.local_client import LocalMealClient
    from zk import ZK

    load_meal_db_dotenv()
    settings = LocalMealClient().get_device_settings() or {}
    import f22_pyzk

    t0 = f22_pyzk.pick_f22_terminal_for_import(settings)
    if t0 is None:
        print("f22_terminals 에 IP 없음", file=sys.stderr)
        return 1
    host = str(t0.get("ip") or t0.get("host") or "").strip()
    try:
        port = int(t0.get("port") or 4370)
    except (TypeError, ValueError):
        port = 4370

    zk = ZK(host, port=port, password=pwd, timeout=25, ommit_ping=True)
    conn = zk.connect()
    try:
        try:
            conn.disable_device()
        except Exception:
            pass
        users = conn.get_users()
        u = next((x for x in (users or []) if str(x.user_id).strip() == user_id), None)
        if u is None:
            print("user_id=%r 없음" % user_id)
            return 1

        slots = {}
        for fid in (0, 1, 2, 3, 5, 6):
            finger = conn.get_user_template(u.uid, temp_id=fid, user_id="")
            if finger is None or finger is False:
                slots[fid] = None
            else:
                slots[fid] = getattr(finger, "template", None) or b""

        print("=== 단말 %s:%s  user_id=%s  내부 uid=%s ===\n" % (host, port, user_id, u.uid))
        _labels = {
            0: "fid0 (전송 첫 슬롯)",
            1: "fid1 (전송 둘째)",
            2: "fid2 (전송 셋째)",
            3: "fid3 (전송 넷째)",
            5: "fid5 (단말 직접등록 예시)",
            6: "fid6 (단말 직접등록 예시)",
        }
        for fid in (0, 1, 2, 3, 5, 6):
            t = slots.get(fid)
            label = _labels[fid]
            print("[%s] %s" % (label, _tpl_meta(t or b"")))

        t0_, t1_, t2_, t3_, t5, t6 = (
            slots.get(0),
            slots.get(1),
            slots.get(2),
            slots.get(3),
            slots.get(5),
            slots.get(6),
        )
        print("\n--- 동일 여부 (바이트 완전 일치) ---")
        print("  fid0 == fid5:", _same(t0_, t5))
        print("  fid1 == fid6:", _same(t1_, t6))
        print("  fid0 == fid6:", _same(t0_, t6))
        print("  fid1 == fid5:", _same(t1_, t5))
        print("  fid2 == fid5:", _same(t2_, t5))
        print("  fid3 == fid6:", _same(t3_, t6))
        if t5 and t6:
            print("  fid5 vs fid6 (앞 min(len) 바이트 일치율): %.2f%%" % _pct_equal(t5, t6))

        print("\n--- 의미 ---")
        print("  우리 프로그램은 DB 4칸을 단말 fid 0~3 에 맞춰 전송합니다.")
        print("  단말에서 다른 번호(예: 5,6)에만 등록했다면 위 슬롯과 바이트가 다를 수 있습니다.")

        # DB 템플릿 (Live20R 등)
        db0 = db1 = db2 = db3 = None
        try:
            from meal_db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT emp_no, fingerprint_template, fingerprint_template_2,
                           fingerprint_template_3, fingerprint_template_4
                    FROM employees WHERE emp_no = %s LIMIT 1
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
            if row:
                if row.get("fingerprint_template"):
                    try:
                        db0 = base64.b64decode(row["fingerprint_template"])
                    except Exception:
                        db0 = None
                if row.get("fingerprint_template_2"):
                    try:
                        db1 = base64.b64decode(row["fingerprint_template_2"])
                    except Exception:
                        db1 = None
                if row.get("fingerprint_template_3"):
                    try:
                        db2 = base64.b64decode(row["fingerprint_template_3"])
                    except Exception:
                        db2 = None
                if row.get("fingerprint_template_4"):
                    try:
                        db3 = base64.b64decode(row["fingerprint_template_4"])
                    except Exception:
                        db3 = None
        except Exception as ex:
            print("\nDB 조회 생략:", ex)

        if db0 or db1 or db2 or db3:
            print("\n=== DB employees (emp_no=%s) 템플릿 ===" % user_id)
            print("[DB slot0 -> fid0]", _tpl_meta(db0 or b""))
            print("[DB slot1 -> fid1]", _tpl_meta(db1 or b""))
            print("[DB slot2 -> fid2]", _tpl_meta(db2 or b""))
            print("[DB slot3 -> fid3]", _tpl_meta(db3 or b""))
            print("\n--- DB vs 단말 fid0~3 ---")
            if db0 and t0_:
                print("  DB0 == 단말 fid0:", _same(db0, t0_), "  앞바이트 일치율: %.2f%%" % _pct_equal(db0, t0_))
            if db1 and t1_:
                print("  DB1 == 단말 fid1:", _same(db1, t1_), "  앞바이트 일치율: %.2f%%" % _pct_equal(db1, t1_))
            if db2 and t2_:
                print("  DB2 == 단말 fid2:", _same(db2, t2_), "  앞바이트 일치율: %.2f%%" % _pct_equal(db2, t2_))
            if db3 and t3_:
                print("  DB3 == 단말 fid3:", _same(db3, t3_), "  앞바이트 일치율: %.2f%%" % _pct_equal(db3, t3_))
            print("\n--- DB vs 단말 fid5,6 (참고) ---")
            if db0 and t5:
                print("  DB0 == 단말 fid5:", _same(db0, t5), "  앞바이트 일치율 vs fid5: %.2f%%" % _pct_equal(db0, t5))
            if db1 and t6:
                print("  DB1 == 단말 fid6:", _same(db1, t6), "  앞바이트 일치율 vs fid6: %.2f%%" % _pct_equal(db1, t6))
            if db0 and t6:
                print("  DB0 == 단말 fid6:", _same(db0, t6))
            if db1 and t5:
                print("  DB1 == 단말 fid5:", _same(db1, t5))
        else:
            print("\n(DB에 emp_no=%s 지문 템플릿 없음 — 저장 후 다시 실행하면 DB 비교 가능)" % user_id)

        return 0
    finally:
        try:
            conn.enable_device()
        except Exception:
            pass
        try:
            conn.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
