"""
F22 단말에 등록된 지문 템플릿을 읽어 employees 테이블에 저장한다.
단말 user_id = 사번(emp_no), 읽기 단말은 설정의 번호 1번·마스터·첫 단말 우선 (f22_pyzk.pick_f22_terminal_for_import).

기본( fid 인자 전부 생략 ): 단말에서 fid 0~9 를 순서대로 확인해, 템플릿이 있는 슬롯만
최대 4개를 DB 칸 1~4에 넣는다(다른 단말로 보낼 때는 fid 0~3 에 대응).
"""

from __future__ import annotations

import base64
import os
from typing import Any, Dict, Optional, Tuple

from meal_db.connection import get_connection
from meal_db.local_client import LocalMealClient


def _get_template(conn, uid: int, fid: int):
    finger = conn.get_user_template(uid, temp_id=fid, user_id="")
    if finger is not None and finger is not False:
        return getattr(finger, "template", None) or b""
    try:
        bulk = conn.get_templates() or []
    except Exception:
        bulk = []
    for f in bulk:
        if int(getattr(f, "uid", -1)) != int(uid):
            continue
        if int(getattr(f, "fid", -1)) == int(fid):
            return getattr(f, "template", None) or b""
    return None


def _discover_templates_in_fid_order(conn, uid: int, max_slots: int = 4) -> list[tuple[int, bytes]]:
    """단말 fid 0~9 를 앞에서부터 훑어, 템플릿이 있는 슬롯만 최대 max_slots 개 (fid 오름차순)."""
    out: list[tuple[int, bytes]] = []
    for fid in range(10):
        if len(out) >= max_slots:
            break
        tb = _get_template(conn, uid, fid)
        if tb is not None and len(tb) > 0:
            out.append((fid, tb))
    return out


def import_fingerprint_templates_from_f22_terminal(
    emp_no: str,
    *,
    fid0: Optional[int] = None,
    fid1: Optional[int] = None,
    fid2: Optional[int] = None,
    fid3: Optional[int] = None,
) -> Tuple[bool, Any]:
    """
    단말에서 지문 템플릿을 읽어 DB에 저장한다.

    fid0~fid3 를 모두 생략(None)이면: 단말에서 fid 0~9 순으로 읽어, 데이터가 있는 슬롯만
    최대 4개까지 DB 1~4칸에 순서대로 넣는다 (설정의 enroll_fid 와 무관).
    하나라도 지정하면: 예전처럼 해당 fid 네 칸만 읽는다(None 이면 마스터 단말 enroll_fid 기본값).

    Returns:
        (True, employee_dict) — _row_to_employee 형식, 템플릿 b64 필드 포함
        (False, str) — 오류 메시지
    """
    emp_no = str(emp_no or "").strip()
    if not emp_no:
        return False, "사번이 비었습니다."

    from meal_db.f22_att_mode import f22_pyzk_terminal_push_skipped

    skipped, skip_msg = f22_pyzk_terminal_push_skipped()
    if skipped:
        return False, skip_msg

    try:
        pwd = int(os.environ.get("ZK_DEVICE_COMM_PASSWORD", "0") or "0")
    except ValueError:
        pwd = 0

    import f22_pyzk
    from zk import ZK

    with get_connection() as conn_sql:
        cur = conn_sql.cursor()
        cur.execute(
            "SELECT emp_no FROM employees WHERE emp_no = %s LIMIT 1",
            (emp_no,),
        )
        row = cur.fetchone()
    if not row:
        return False, "DB에 해당 사번 사원이 없습니다."

    client = LocalMealClient()
    settings = client.get_device_settings() or {}
    t0 = f22_pyzk.pick_f22_terminal_for_import(settings)
    if t0 is None:
        return False, "설정에 F22 단말(IP)이 없습니다. 설정 > 단말기를 확인하세요."

    def _efid(key: str, default: int) -> int:
        try:
            return max(0, min(9, int(t0.get(key, default))))
        except (TypeError, ValueError):
            return default

    auto_discover = (
        fid0 is None and fid1 is None and fid2 is None and fid3 is None
    )
    if not auto_discover:
        f0 = max(0, min(9, int(fid0))) if fid0 is not None else _efid("enroll_fid0", 0)
        f1 = max(0, min(9, int(fid1))) if fid1 is not None else _efid("enroll_fid1", 1)
        f2 = max(0, min(9, int(fid2))) if fid2 is not None else _efid("enroll_fid2", 2)
        f3 = max(0, min(9, int(fid3))) if fid3 is not None else _efid("enroll_fid3", 3)

    host = str(t0.get("ip") or t0.get("host") or "").strip()
    try:
        port = int(t0.get("port") or 4370)
    except (TypeError, ValueError):
        port = 4370

    zk = ZK(host, port=port, password=pwd, timeout=25, ommit_ping=True)
    conn = None
    try:
        conn = zk.connect()
        try:
            conn.disable_device()
        except Exception:
            pass
        users = conn.get_users()
        u = next((x for x in (users or []) if str(x.user_id).strip() == emp_no), None)
        if u is None:
            return False, "단말에 해당 사번(user_id) 사용자가 없습니다. 단말에 사원·카드를 먼저 등록하세요."

        tpl_keys = [
            "fingerprint_template_b64",
            "fingerprint_template_b64_2",
            "fingerprint_template_b64_3",
            "fingerprint_template_b64_4",
        ]
        fid_keys = [
            "fingerprint_slot_fid_1",
            "fingerprint_slot_fid_2",
            "fingerprint_slot_fid_3",
            "fingerprint_slot_fid_4",
        ]

        if auto_discover:
            found = _discover_templates_in_fid_order(conn, u.uid, 4)
            if not found:
                return False, (
                    "단말에 해당 사번의 지문 템플릿이 없습니다. "
                    "단말에서 지문을 등록한 뒤 다시 시도하세요."
                )
            payload: Dict[str, Any] = {}
            for j in range(4):
                if j < len(found):
                    fid_num, tb = found[j]
                    payload[tpl_keys[j]] = base64.b64encode(tb).decode("ascii")
                    payload[fid_keys[j]] = int(fid_num)
                else:
                    payload[tpl_keys[j]] = None
                    payload[fid_keys[j]] = None
        else:
            t0b = _get_template(conn, u.uid, f0)
            t1b = _get_template(conn, u.uid, f1)
            t2b = _get_template(conn, u.uid, f2)
            t3b = _get_template(conn, u.uid, f3)
            tpls = (t0b, t1b, t2b, t3b)
            fids = (f0, f1, f2, f3)

            if t0b is None or len(t0b) == 0:
                return False, (
                    "단말 fid=%d 에서 첫 번째 템플릿을 읽지 못했습니다. "
                    "설정의 손가락(fid)이 단말 등록 슬롯과 맞는지 확인하세요." % f0
                )

            payload = {}
            for j in range(4):
                tb = tpls[j]
                if tb is not None and len(tb) > 0:
                    payload[tpl_keys[j]] = base64.b64encode(tb).decode("ascii")
                    payload[fid_keys[j]] = int(fids[j])
                else:
                    payload[tpl_keys[j]] = None
                    payload[fid_keys[j]] = None

        ok, res = client.update_employee(emp_no, payload)
        if not ok:
            return False, str(res)

        with get_connection() as conn2:
            cur2 = conn2.cursor()
            cur2.execute("SELECT * FROM employees WHERE emp_no = %s", (emp_no,))
            er = cur2.fetchone()
        if not er:
            return False, "DB에서 사원을 다시 읽지 못했습니다."
        emp_dict = client._row_to_employee(dict(er), "")
        return True, emp_dict
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
