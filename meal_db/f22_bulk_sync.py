"""DB 재직 사원과 단말 사용자를 맞춘다. DB에 없는 사번은 삭제하고, 재직 사원은 UPDATE."""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from meal_db.f22_sync import sync_employee_to_f22_terminals


def _terminal_label(t: dict) -> str:
    from meal_db.f22_sync import sync_terminal_name

    return sync_terminal_name(t)


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


def _keep_emp_nos(employees: List[dict]) -> Set[str]:
    """단말에 남길 사번. 재직이면서 카드 또는 지문이 있는 사람."""
    keep: Set[str] = set()
    for emp in employees or []:
        if not isinstance(emp, dict):
            continue
        eno = str(emp.get("emp_no") or "").strip()
        if eno and _employee_has_sync_payload(emp):
            keep.add(eno)
    return keep


def _sync_terminals(settings: dict | None, terminals_only: Optional[List[dict]]) -> List[dict]:
    if terminals_only is not None:
        return [t for t in terminals_only if isinstance(t, dict)]
    terminals = (settings or {}).get("f22_terminals") if isinstance(settings, dict) else None
    if not isinstance(terminals, list):
        return []
    return [t for t in terminals if isinstance(t, dict)]


def _prune_terminals_to_db(
    settings: dict | None,
    terminals_only: Optional[List[dict]],
    keep: Set[str],
    total: int,
    *,
    cancel_check: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[int, int, str, int, int, int], None]] = None,
) -> Dict[str, Any]:
    from meal_db.f22_att_mode import f22_att_mode_is_adms

    def _note(msg: str) -> None:
        if on_progress:
            on_progress(0, total, msg, 0, 0, 0)

    if f22_att_mode_is_adms():
        from meal_db.adms_user_sync import prune_terminal_users_via_adms

        return prune_terminal_users_via_adms(
            settings,
            keep,
            terminals_only,
            cancel_check=cancel_check,
            on_progress=_note,
        )

    import f22_pyzk

    try:
        pwd = int(os.environ.get("ZK_DEVICE_COMM_PASSWORD", "0") or "0")
    except ValueError:
        pwd = 0
    deleted = 0
    delete_fail = 0
    kept_admin = 0
    errors: List[str] = []
    for t in _sync_terminals(settings, terminals_only):
        if cancel_check and cancel_check():
            return {
                "ok": True,
                "deleted": deleted,
                "delete_fail": delete_fail,
                "kept_admin": kept_admin,
                "cancelled": True,
                "errors": errors,
            }
        ip = str(t.get("ip") or t.get("host") or "").strip()
        label = _terminal_label(t)
        if not ip:
            errors.append("[%s] IP 없음 — 삭제를 건너뜀" % label)
            continue
        try:
            port = int(t.get("port") or 4370)
        except (TypeError, ValueError):
            port = 4370
        _note("%s 사용자 목록과 DB 비교 중…" % label)
        ok, msg, n_del, n_admin = f22_pyzk.prune_users_not_in(ip, port, pwd, keep)
        kept_admin += n_admin
        if not ok:
            delete_fail += 1
            errors.append("[%s] 사용자 목록 정리 실패 — %s" % (label, msg))
            continue
        deleted += n_del
    return {
        "ok": not errors,
        "deleted": deleted,
        "delete_fail": delete_fail,
        "kept_admin": kept_admin,
        "cancelled": False,
        "errors": errors,
    }


def bulk_resync_active_employees(
    settings: dict | None,
    employees: List[dict],
    terminals_only: Optional[List[dict]] = None,
    *,
    cancel_check: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[int, int, str, int, int, int], None]] = None,
) -> Dict[str, Any]:
    """
    재직 사원 중 카드·지문이 있는 사람을 단말에 맞춘다.
    그 목록에 없는 일반 사용자는 단말에서 삭제한다.

    on_progress(cur, total, message, ok_emp, fail_emp, skip_emp)
    """
    if not isinstance(settings, dict):
        settings = {}
    total = len(employees or [])
    ok_emp = 0
    fail_emp = 0
    skip_emp = 0
    flat_failures: List[Tuple[str, bool, str]] = []
    per_emp_failures: List[str] = []
    keep = _keep_emp_nos(employees or [])
    pruned = _prune_terminals_to_db(
        settings,
        terminals_only,
        keep,
        total,
        cancel_check=cancel_check,
        on_progress=on_progress,
    )
    deleted_emp = int(pruned.get("deleted") or 0)
    delete_fail = int(pruned.get("delete_fail") or 0)
    kept_admin = int(pruned.get("kept_admin") or 0)
    for err in pruned.get("errors") or []:
        per_emp_failures.append(str(err))
        flat_failures.append(("단말", False, str(err)))
    if pruned.get("cancelled"):
        return {
            "total": total,
            "ok_emp": ok_emp,
            "fail_emp": fail_emp,
            "skip_emp": skip_emp,
            "deleted_emp": deleted_emp,
            "delete_fail": delete_fail,
            "kept_admin": kept_admin,
            "cancelled": True,
            "flat_failures": flat_failures,
            "per_emp_failures": per_emp_failures,
        }

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
        "deleted_emp": deleted_emp,
        "delete_fail": delete_fail,
        "kept_admin": kept_admin,
        "cancelled": cancelled,
        "flat_failures": flat_failures,
        "per_emp_failures": per_emp_failures,
    }
