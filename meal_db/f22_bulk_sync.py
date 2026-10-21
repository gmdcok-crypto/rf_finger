"""DB 재직 사원 → F22 단말 일괄 재동기화 (UPDATE만, DELETE 없음)."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from meal_db.f22_sync import sync_employee_to_f22_terminals


def _terminal_label(t: dict) -> str:
    dno = str(t.get("device_no") or t.get("terminal_no") or "").strip()
    if dno:
        return dno
    ip = str(t.get("ip") or t.get("host") or "").strip()
    sn = str(t.get("serial_no") or t.get("adms_sn") or t.get("device_sn") or "").strip()
    return ip or sn or "단말"


def _employee_has_sync_payload(emp: dict) -> bool:
    rf = (emp.get("rf_card_uid") or emp.get("card_uid") or "").strip()
    if rf:
        return True
    for k in (
        "fingerprint_template_b64",
        "fingerprint_template_b64_2",
        "fingerprint_template_b64_3",
        "fingerprint_template_b64_4",
    ):
        if (emp.get(k) or "").strip():
            return True
    return False


def bulk_resync_active_employees(
    settings: dict | None,
    employees: List[dict],
    terminals_only: Optional[List[dict]] = None,
    *,
    cancel_check: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[int, int, str, int, int, int], None]] = None,
) -> Dict[str, Any]:
    """
    재직 사원 전원(또는 목록)을 단말에 UPDATE 동기화.

    on_progress(cur, total, message, ok_emp, fail_emp, skip_emp)
  Returns summary dict with flat failure rows for UI.
    """
    if not isinstance(settings, dict):
        settings = {}
    total = len(employees or [])
    ok_emp = 0
    fail_emp = 0
    skip_emp = 0
    flat_failures: List[Tuple[str, bool, str]] = []
    per_emp_failures: List[str] = []

    for idx, emp in enumerate(employees or []):
        if cancel_check and cancel_check():
            break
        if not isinstance(emp, dict):
            continue
        eno = str(emp.get("emp_no") or "").strip()
        name = str(emp.get("name") or "").strip()
        label_emp = "%s (%s)" % (name or eno, eno) if eno else (name or "?")

        if not _employee_has_sync_payload(emp):
            skip_emp += 1
            if on_progress:
                on_progress(
                    idx + 1,
                    total,
                    "%s — 카드·지문 없음, 건너뜀" % label_emp,
                    ok_emp,
                    fail_emp,
                    skip_emp,
                )
            continue

        if on_progress:
            term_hint = ""
            if terminals_only and len(terminals_only) == 1:
                term_hint = " · %s" % _terminal_label(terminals_only[0])
            on_progress(
                idx + 1,
                total,
                "%s%s 전송 중…" % (label_emp, term_hint),
                ok_emp,
                fail_emp,
                skip_emp,
            )

        rows = sync_employee_to_f22_terminals(
            settings,
            eno,
            name or eno,
            str(emp.get("rf_card_uid") or emp.get("card_uid") or "").strip(),
            "",
            emp.get("fingerprint_template_b64"),
            emp.get("fingerprint_template_b64_2"),
            emp.get("fingerprint_template_b64_3"),
            emp.get("fingerprint_template_b64_4"),
            emp.get("fingerprint_slot_fid_1"),
            emp.get("fingerprint_slot_fid_2"),
            emp.get("fingerprint_slot_fid_3"),
            emp.get("fingerprint_slot_fid_4"),
            terminals_only=terminals_only,
        )

        if not rows:
            skip_emp += 1
            continue

        emp_ok = all(isinstance(r, (list, tuple)) and len(r) >= 2 and r[1] for r in rows)
        if emp_ok:
            ok_emp += 1
        else:
            fail_emp += 1
            for r in rows:
                if not isinstance(r, (list, tuple)) or len(r) < 3:
                    continue
                tlab, ok_r, msg = r[0], r[1], r[2]
                if not ok_r:
                    flat_failures.append((str(tlab), False, str(msg)))
                    per_emp_failures.append("[%s] %s — %s" % (tlab, label_emp, msg))

        if on_progress:
            on_progress(
                idx + 1,
                total,
                "%s — %s" % (label_emp, "완료" if emp_ok else "일부 실패"),
                ok_emp,
                fail_emp,
                skip_emp,
            )

    cancelled = bool(cancel_check and cancel_check())
    return {
        "total": total,
        "ok_emp": ok_emp,
        "fail_emp": fail_emp,
        "skip_emp": skip_emp,
        "cancelled": cancelled,
        "flat_failures": flat_failures,
        "per_emp_failures": per_emp_failures,
    }
